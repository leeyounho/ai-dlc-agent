from dataclasses import replace
from contextlib import redirect_stderr, redirect_stdout
import http.client
from io import StringIO
import json
import threading
import time
from types import MappingProxyType
import unittest
from unittest.mock import patch

from tests.support import temporary_directory

from ai_dlc.config.loader import load_service
from ai_dlc.cli import main
from ai_dlc.errors import AgentError
from ai_dlc.service.http_server import ServiceHttpServer
from ai_dlc.service.runtime import ServiceRuntime
from ai_dlc.service.scheduler import FairScheduler, ScheduledWork
from ai_dlc.storage import FileJournal, TaskKey


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class FairSchedulerTests(unittest.TestCase):
    def test_fifo_per_repository_and_round_robin_between_repositories(self):
        order = []
        scheduler = FairScheduler(global_limit=1, repository_limit=1, model_limit=1)
        try:
            for work_id, repository in (("a1", 1), ("a2", 1), ("b1", 2)):
                scheduler.submit(ScheduledWork(
                    work_id, repository, lambda _context, work_id=work_id: order.append(work_id)))
            while len(scheduler.snapshot()["completed"]) < 3:
                scheduler.tick()
                time.sleep(0.005)
            self.assertEqual(order, ["a1", "b1", "a2"])
        finally:
            scheduler.shutdown(1)

    def test_repository_model_and_provider_limits_are_all_enforced(self):
        release = threading.Event()
        scheduler = FairScheduler(global_limit=4, repository_limit=1, model_limit=2,
                                  provider_limits={"p": 1})
        try:
            block = lambda context: release.wait(context.remaining_seconds() or 2)
            scheduler.submit(ScheduledWork("p1", 1, block, kind="model", provider_id="p"))
            scheduler.submit(ScheduledWork("same-repo", 1, block, kind="control"))
            scheduler.submit(ScheduledWork("same-provider", 2, block, kind="model", provider_id="p"))
            scheduler.submit(ScheduledWork("other-provider", 3, block, kind="model", provider_id="q"))
            scheduler.submit(ScheduledWork("model-limited", 4, block, kind="model", provider_id="r"))
            scheduler.tick()
            snapshot = scheduler.snapshot()
            self.assertEqual({item["work_id"] for item in snapshot["active"]},
                             {"p1", "other-provider"})
            reasons = {item["work_id"]: item["waiting_reason"] for item in snapshot["pending"]}
            self.assertEqual(reasons["same-repo"], "REPOSITORY_CONCURRENCY_LIMIT")
            self.assertEqual(reasons["same-provider"], "PROVIDER_CONCURRENCY_LIMIT")
            self.assertEqual(reasons["model-limited"], "MODEL_CONCURRENCY_LIMIT")
        finally:
            release.set()
            scheduler.shutdown(1)

    def test_active_timeout_requests_cancellation_and_never_reports_success(self):
        now = [10.0]
        scheduler = FairScheduler(global_limit=1, repository_limit=1, model_limit=1,
                                  clock=lambda: now[0])

        def handler(context):
            context.cancel_event.wait(1)
            return "late-success"

        try:
            scheduler.submit(ScheduledWork("overrun", 1, handler, active_timeout_seconds=5))
            scheduler.tick()
            now[0] = 16.0
            scheduler.tick()
            self.assertTrue(wait_until(lambda: scheduler.snapshot()["completed"].get("overrun") is not None))
            self.assertEqual(scheduler.snapshot()["completed"]["overrun"]["status"], "timed_out")
        finally:
            scheduler.shutdown(0)

    def test_shutdown_returns_active_and_never_started_work_for_recovery(self):
        release = threading.Event()
        scheduler = FairScheduler(global_limit=1, repository_limit=1, model_limit=1)
        scheduler.submit(ScheduledWork("active", 1, lambda _context: release.wait(2)))
        scheduler.submit(ScheduledWork("queued", 2, lambda _context: None))
        scheduler.tick()
        unfinished = scheduler.shutdown(0)
        release.set()
        self.assertEqual(unfinished, ("active", "queued"))
        self.assertFalse(scheduler.accepting)

    def test_failed_durable_work_can_be_resubmitted_with_the_same_identity(self):
        attempts = []
        scheduler = FairScheduler(global_limit=1, repository_limit=1, model_limit=1)

        def handler(_context):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise AgentError("TRANSIENT_READ", "retry")
            return "processed"

        try:
            work = ScheduledWork("delivery", 1, handler)
            self.assertTrue(scheduler.submit(work))
            scheduler.tick()
            self.assertTrue(wait_until(lambda: scheduler.snapshot()["completed"].get("delivery") is not None))
            self.assertTrue(scheduler.submit(work))
            scheduler.tick()
            self.assertTrue(wait_until(
                lambda: scheduler.snapshot()["completed"].get("delivery", {}).get("status") == "completed"))
            self.assertEqual(attempts, [1, 2])
        finally:
            scheduler.shutdown(1)


class ServiceRuntimeTests(unittest.TestCase):
    def bundle(self, state_root):
        bundle = load_service(ROOT / "config" / "service.example.json")
        directories = dict(bundle.connection.directories)
        directories["state"] = state_root
        return replace(bundle, connection=replace(
            bundle.connection, directories=MappingProxyType(directories)))

    def test_restart_recovers_tasks_and_reports_unclean_previous_epoch(self):
        with temporary_directory() as temp:
            bundle = self.bundle(temp / "state")
            key = TaskKey(bundle.service.github.instance_id, 1, 7)
            with FileJournal(bundle.connection.directories["state"]) as store:
                store.commit(key, expected_revision=0, event_id="initial", event={"kind": "test"},
                             reduce=lambda _old: {"status": "waiting_human", "execution": None})
                first = ServiceRuntime(bundle, store)
                first.start()
                first.scheduler.shutdown(0)
            with FileJournal(bundle.connection.directories["state"]) as store:
                restarted = ServiceRuntime(bundle, store)
                status = restarted.start()
                self.assertTrue(status["unclean_restart"])
                self.assertEqual(status["recovered_tasks"], 1)
                restarted.shutdown(grace_seconds=0)

    def test_active_execution_is_blocked_until_it_can_be_reobserved(self):
        with temporary_directory() as temp:
            bundle = self.bundle(temp / "state")
            key = TaskKey(bundle.service.github.instance_id, 1, 8)
            with FileJournal(bundle.connection.directories["state"]) as store:
                store.commit(key, expected_revision=0, event_id="active", event={"kind": "test"},
                             reduce=lambda _old: {"status": "running",
                                                  "execution": {"status": "running"}})
                runtime = ServiceRuntime(bundle, store)
                status = runtime.start()
                self.assertIn("RECOVERY_ACTION_REQUIRED", status["health"]["reasons"])
                self.assertEqual(status["recovery_required"][0]["task"], key.as_dict())
                runtime.shutdown(grace_seconds=0)

    def test_checkpoint_failure_closes_dispatch_and_never_claims_ready(self):
        with temporary_directory() as temp:
            bundle = self.bundle(temp / "state")
            with FileJournal(bundle.connection.directories["state"]) as store:
                runtime = ServiceRuntime(bundle, store)
                runtime.start()
                with patch("ai_dlc.service.runtime._publish", side_effect=OSError):
                    with self.assertRaises(AgentError) as raised:
                        runtime.tick()
                self.assertEqual(raised.exception.code, "STATE_IO")
                self.assertFalse(runtime.scheduler.accepting)
                self.assertFalse(runtime.health()["ready"])
                runtime.scheduler.shutdown(0)

    def test_http_liveness_is_distinct_from_readiness(self):
        with temporary_directory() as temp:
            bundle = self.bundle(temp / "state")
            web = replace(bundle.service.web, bind_host="127.0.0.1", port=0,
                          tls_mode="reverse_proxy", tls_certificate_file=None,
                          tls_private_key_file=None)
            with FileJournal(bundle.connection.directories["state"]) as store:
                runtime = ServiceRuntime(bundle, store)
                runtime.start()
                server = ServiceHttpServer(web, runtime)
                server.start()
                try:
                    connection = http.client.HTTPConnection(*server.address, timeout=2)
                    connection.request("GET", "/health/live")
                    self.assertEqual(connection.getresponse().status, 200)
                    connection.close()
                    connection = http.client.HTTPConnection(*server.address, timeout=2)
                    connection.request("GET", "/health/ready")
                    self.assertEqual(connection.getresponse().status, 503)
                    connection.close()
                finally:
                    server.stop()
                    runtime.shutdown(grace_seconds=0)

    def test_serve_once_recovers_checkpoints_and_exits_not_ready_honestly(self):
        with temporary_directory() as temp:
            connection = json.loads((ROOT / "config" / "production.example.json").read_text(encoding="utf-8"))
            service = json.loads((ROOT / "config" / "service.example.json").read_text(encoding="utf-8"))
            repository = json.loads((ROOT / "config" / "repository.example.json").read_text(encoding="utf-8"))
            connection["maven"]["local_cache_dir"] = "managed/maven"
            connection["workspace"]["root"] = "managed/workspace"
            connection["storage"]["state_dir"] = "managed/state"
            connection["storage"]["log_dir"] = "managed/logs"
            service["connection_profile_file"] = "connection.json"
            service["repository_profile_files"] = ["repository.json"]
            service["github"]["private_key_file"] = "references/github.pem"
            service["execution"]["runner_pool_profile_file"] = "references/runner.json"
            service["execution"]["toolchains_profile_file"] = "references/toolchains.json"
            service["execution"]["egress_profile_file"] = "references/egress.json"
            service["execution"]["artifact_root"] = "managed/artifacts"
            (temp / "connection.json").write_text(json.dumps(connection), encoding="utf-8")
            (temp / "repository.json").write_text(json.dumps(repository), encoding="utf-8")
            service_file = temp / "service.json"
            service_file.write_text(json.dumps(service), encoding="utf-8")
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["serve", "--config", str(service_file), "--once"])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, 3, stderr.getvalue())
            self.assertEqual(report["mode"], "once")
            self.assertFalse(report["health"]["ready"])
            self.assertTrue((temp / "managed" / "state" / "service" / "current.json").is_file())


if __name__ == "__main__":
    unittest.main()
