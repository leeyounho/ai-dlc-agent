from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from ai_dlc.config.loader import parse_connection, parse_repository
from ai_dlc.errors import AgentError
from ai_dlc.evaluation.offline import OfflineNetworkGuard
from ai_dlc.evaluation.process_fixture import FixtureProcessRunner
from ai_dlc.execution.coordinator import ExecutionCoordinator
from ai_dlc.execution.junit import collect_junit
from ai_dlc.execution.ports import ExecutionPlan, ProcessResult
from ai_dlc.execution.workspace import WorkspaceManager, read_file, relative_path
from ai_dlc.storage import FileJournal, TaskKey
from ai_dlc.validation import read_json
from ai_dlc.workflow import WorkflowEngine
from tests.support import temporary_directory
from tests.test_workflow import DESIGN, REQUIREMENTS, FIXTURES, FixtureGateway


class WorkspaceAndJUnitTests(unittest.TestCase):
    def setUp(self):
        temp = temporary_directory()
        self.root = temp.__enter__()
        self.addCleanup(temp.__exit__, None, None, None)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "input.txt").write_text("original", encoding="utf-8")
        self.manager = WorkspaceManager(self.root / "workspaces", protected_roots=(self.root / "state",))

    def test_snapshot_preserves_original_and_detects_modified_missing_and_new_source(self):
        for mutation in ("modified", "missing", "new"):
            with self.subTest(mutation=mutation):
                workspace = self.manager.prepare(self.source, ("input.txt",))
                workspace.verify()
                if mutation == "modified":
                    (workspace.root / "input.txt").write_text("changed", encoding="utf-8")
                elif mutation == "missing":
                    (workspace.root / "input.txt").unlink()
                else:
                    (workspace.root / "unexpected.py").write_text("pass", encoding="utf-8")
                with self.assertRaises(AgentError) as error:
                    workspace.verify(generated_patterns=("reports/**",))
                self.assertEqual(error.exception.code, "SOURCE_TREE_CHANGED")
                self.assertEqual((self.source / "input.txt").read_text(encoding="utf-8"), "original")

    def test_generated_reports_do_not_change_source_identity(self):
        workspace = self.manager.prepare(self.source, ("input.txt",))
        digest = workspace.digest
        (workspace.root / "reports").mkdir()
        (workspace.root / "reports" / "a.xml").write_text("<testsuite/>", encoding="utf-8")
        workspace.verify(generated_patterns=("reports/**",))
        self.assertEqual(workspace.digest, digest)

    def test_traversal_devices_case_collisions_and_control_roots_rejected(self):
        for path in ("../state", "C:/outside", "//never-contact.invalid/share", ".git/config", ".GIT/config",
                     "NUL", "folder/COM1.txt", "trailing.", "dir\\file"):
            with self.subTest(path=path), self.assertRaises(AgentError):
                relative_path(path)
        with self.assertRaises(AgentError):
            self.manager.prepare(self.source, ("input.txt", "INPUT.TXT"))
        with self.assertRaises(AgentError):
            WorkspaceManager(self.root / "state" / "runner", protected_roots=(self.root / "state",))
        with self.assertRaises(AgentError):
            self.manager.prepare(self.root / "state", ("secret.txt",))

    def test_hardlinked_input_is_refused(self):
        alias = self.source / "alias.txt"
        os.link(self.source / "input.txt", alias)
        with self.assertRaises(AgentError) as error:
            self.manager.prepare(self.source, ("alias.txt",))
        self.assertEqual(error.exception.code, "WORKSPACE_FILE")

    def test_oversized_input_is_refused(self):
        with patch("ai_dlc.execution.workspace.MAX_TREE_BYTES", 1):
            with self.assertRaises(AgentError) as error:
                self.manager.prepare(self.source, ("input.txt",))
        self.assertEqual(error.exception.code, "WORKSPACE_SIZE")

    def junit(self, xml, other=None):
        directory = self.source / "target" / "surefire-reports"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "TEST-a.xml").write_bytes(xml if isinstance(xml, bytes) else xml.encode("utf-8"))
        if other:
            (directory / "TEST-b.xml").write_text(other, encoding="utf-8")
        return collect_junit(self.source, ("**/target/surefire-reports/TEST-*.xml", "target/surefire-reports/*.xml"))

    def test_junit_multiple_suites_are_aggregated_without_glob_double_count(self):
        result = self.junit('<testsuites tests="2"><testsuite tests="1"><testcase name="a"/></testsuite>'
                            '<testsuite tests="1" skipped="1"><testcase name="b"><skipped/></testcase></testsuite></testsuites>',
                            '<testsuite tests="1"><testcase name="c"/></testsuite>')
        self.assertEqual((result.reports, result.tests, result.executed, result.skipped), (2, 3, 2, 1))
        self.assertTrue(result.passed)

    def test_zero_tests_all_skipped_and_failure_are_not_success(self):
        for xml in ('<testsuite tests="0"/>', '<testsuite><testcase name="a"><skipped/></testcase></testsuite>',
                    '<testsuite><testcase name="a"><failure/></testcase></testsuite>',
                    '<testsuite><testcase name="a"><error/></testcase></testsuite>'):
            with self.subTest(xml=xml):
                self.assertFalse(self.junit(xml).passed)

    def test_invalid_or_unsafe_xml_and_false_counts_are_rejected_offline(self):
        with OfflineNetworkGuard() as network:
            for xml in ('<!DOCTYPE testsuite SYSTEM "https://never-contact.invalid/entity"><testsuite/>',
                        '<!DOCTYPE testsuite [<!ENTITY a "expansion">]><testsuite/>',
                        '<testsuite tests="999"><testcase name="a"/></testsuite>', '<testsuite tests="-1"/>',
                        '<testsuite><testcase/></testsuite>', '<testsuite><testcase name="a"><failure/><skipped/></testcase></testsuite>',
                        '<testsuite><system-out><testcase name="forged"/></system-out></testsuite>',
                        '<unrelated/>', '<testsuite>', '<testsuite>'.encode('utf-16')):
                with self.subTest(xml=xml), self.assertRaises(AgentError) as error:
                    self.junit(xml)
                self.assertEqual(error.exception.code, "JUNIT_INVALID")
            self.assertEqual(network.attempts, 0)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = temporary_directory()
        self.root = temp.__enter__()
        self.addCleanup(temp.__exit__, None, None, None)
        self.store = FileJournal(self.root / "state").__enter__()
        self.addCleanup(self.store.__exit__, None, None, None)
        self.network = OfflineNetworkGuard().__enter__()
        self.addCleanup(self.network.__exit__, None, None, None)
        self.raw = read_json(FIXTURES / "repository.json")
        self.connection = parse_connection(read_json(FIXTURES / "connection.json"), base_dir=FIXTURES)
        self.key = TaskKey(self.raw["github_instance_id"], self.raw["repository_id"], 1)
        self.gateway = FixtureGateway(self.key)
        self.runner = FixtureProcessRunner()
        source = self.root / "source"
        source.mkdir()
        (source / "input.txt").write_text("original", encoding="utf-8")
        self.workspace = WorkspaceManager(self.root / "workspaces", protected_roots=(self.store.root,)).prepare(source, ("input.txt",))

    def tearDown(self):
        for handle in self.runner.handles.values():
            if handle.poll() is None:
                handle.cancel("cancelled")
                handle.process.wait(timeout=5)
                handle.poll()
        self.assertEqual(self.network.attempts, 0)

    def state(self):
        return self.store.read(self.key)

    def comment(self, body):
        cid = self.gateway.add(body)
        return self.workflow.handle_comment(self.key, cid, expected_revision=self.state()["state_revision"])

    def ready(self, mode="pass", *, verification="junit"):
        self.raw["workflow"].update(start_policy="on_approval", design_mode="automatic")
        self.raw["project"].update(adapter="command", toolchain_id="local-fixture-only", junit_report_patterns=[],
            command_overrides={"unit_test": {"executable_id": "fixture-python", "argv": [mode], "cwd": ".",
                                             "timeout_seconds": 1 if mode == "timeout" else 5,
                                             "report_patterns": ["reports/*.xml"]}})
        repository = parse_repository(self.raw, connection=self.connection)
        self.workflow = WorkflowEngine(self.store, repository, self.gateway)
        self.workflow.capture_source(self.key, expected_revision=0, event_id="source")
        self.workflow.normalize(self.key, deepcopy(REQUIREMENTS), expected_revision=1, event_id="requirements")
        self.comment("/aidlc approve requirements req-0001")
        self.workflow.propose_design(self.key, deepcopy(DESIGN), requirements_revision="req-0001",
                                     expected_revision=3, event_id="design")
        self.plan = ExecutionPlan("unit_test", "local-fixture-only", repository.project.commands["unit_test"], self.workspace,
                                  verification, ("reports/**",))
        self.coordinator = ExecutionCoordinator(self.workflow, self.runner)

    def reserve(self, run_id="run-first"):
        state = self.state()
        return self.coordinator.reserve(self.key, run_id, self.plan, expected_revision=state["state_revision"],
                                         cancellation_epoch=state["cancellation_epoch"])

    def execute(self):
        self.reserve()
        return self.coordinator.execute(self.key, "run-first", self.plan)

    def wait_running(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.state().get("execution", {}).get("status") == "running":
                return
            time.sleep(0.01)
        self.fail("Fixture never reached running state")

    def test_actual_process_and_junit_result_are_bound_to_recorded_source(self):
        self.ready()
        result = self.execute()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["verification"]["executed"], 1)
        self.assertEqual(result["result"]["exit_code"], 0)
        self.assertEqual(self.state()["phase"], "implementation")
        self.assertNotEqual(self.state()["status"], "succeeded")
        history = [record["event"]["kind"] for record in self.store.history(self.key)]
        self.assertEqual(history[-4:], ["execution_reserved", "execution_dispatching", "execution_running", "execution_succeeded"])
        self.assertEqual(self.store.blob(self.key, result["plan_digest"])["workspace"]["source_digest"], self.workspace.digest)

    def test_exit_code_only_does_not_claim_tests_were_verified(self):
        self.ready("missing", verification="exit_code")
        result = self.execute()
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["verification"]["tests_verified"])

    def test_duplicate_reservation_and_dispatch_do_not_rerun_command(self):
        self.ready()
        first = self.execute()
        self.assertEqual(self.reserve(), first)
        self.assertEqual(self.coordinator.execute(self.key, "run-first", self.plan), first)
        self.assertEqual(self.runner.launch_count, 1)
        changed = replace(self.plan, verification="exit_code")
        with self.assertRaises(AgentError) as error:
            self.coordinator.reserve(self.key, "run-first", changed, expected_revision=0, cancellation_epoch=0)
        self.assertEqual(error.exception.code, "RUN_COLLISION")

    def test_no_default_host_execution_or_unregistered_command(self):
        self.ready()
        state = self.state()
        with self.assertRaises(AgentError) as error:
            ExecutionCoordinator(self.workflow).reserve(self.key, "run-default", self.plan,
                expected_revision=state["state_revision"], cancellation_epoch=state["cancellation_epoch"])
        self.assertEqual(error.exception.code, "RUNNER_UNCONFIGURED")
        with self.assertRaises(AgentError) as error:
            self.coordinator.reserve(self.key, "run-unknown", replace(self.plan, command_id="unknown"),
                expected_revision=state["state_revision"], cancellation_epoch=state["cancellation_epoch"])
        self.assertEqual(error.exception.code, "COMMAND_NOT_REGISTERED")
        self.assertEqual(self.runner.launch_count, 0)

    def test_nonzero_exit_missing_zero_skipped_failure_mutation_and_new_files_fail(self):
        # Each subcase needs its own approved task/workspace, so use separate Issue keys.
        for index, (mode, reason) in enumerate((("nonzero", "COMMAND_EXIT"), ("missing", "JUNIT_MISSING"),
                ("zero", "JUNIT_FAILED_OR_EMPTY"), ("skipped", "JUNIT_FAILED_OR_EMPTY"), ("failure", "JUNIT_FAILED_OR_EMPTY"),
                ("mutate", "SOURCE_TREE_CHANGED"), ("unexpected_file", "SOURCE_TREE_CHANGED"))):
            with self.subTest(mode=mode):
                self.key = replace(self.key, issue_number=index + 1)
                self.gateway = FixtureGateway(self.key)
                self.runner = FixtureProcessRunner()
                self.workspace = WorkspaceManager(self.root / f"workspaces-{index}", protected_roots=(self.store.root,)).prepare(
                    self.root / "source", ("input.txt",))
                self.ready(mode)
                result = self.execute()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["reason"], reason)
                self.assertEqual(self.state()["status"], "failed")

    def test_stale_reports_are_rejected_before_launch(self):
        self.ready()
        (self.workspace.root / "reports").mkdir()
        (self.workspace.root / "reports" / "old.xml").write_text('<testsuite><testcase name="old"/></testsuite>', encoding="utf-8")
        with self.assertRaises(AgentError):
            self.reserve()
        self.assertEqual(self.runner.launch_count, 0)

    def test_changed_approval_after_reservation_prevents_launch(self):
        self.ready()
        self.reserve()
        self.gateway.permissions[1] = "read"
        result = self.coordinator.execute(self.key, "run-first", self.plan)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.runner.launch_count, 0)

    def test_timeout_and_output_limit_confirm_process_exit(self):
        self.ready("timeout")
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["termination"], "timeout")
        self.assertTrue(result["result"]["process_tree_stopped"])

    def test_output_limit_does_not_allow_zero_exit_to_pass(self):
        self.ready("output_limit", verification="exit_code")
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["result"]["termination"], "output_limit")

    def test_stop_running_command_remains_running_until_termination_confirmed(self):
        self.ready("timeout")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.execute)
            self.wait_running()
            stopped = self.comment("/aidlc stop")
            self.assertEqual(stopped.state["status"], "running")
            result = future.result(timeout=5)
        self.assertTrue(result["result"]["process_tree_stopped"])
        self.assertEqual(self.state()["status"], "paused")
        self.comment("/aidlc resume")
        self.assertEqual(self.state()["status"], "queued")

    def test_cancel_running_command_waits_for_observed_exit(self):
        self.ready("timeout")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.execute)
            self.wait_running()
            cancelled = self.comment("/aidlc cancel")
            self.assertEqual(cancelled.state["status"], "cancel_requested")
            self.assertFalse(cancelled.state["cancelled"])
            future.result(timeout=5)
        self.assertEqual(self.state()["status"], "cancelled")

    def test_launch_response_loss_is_not_retried_after_recovery(self):
        self.ready()
        actual_start = self.runner.start
        def response_lost(*args):
            actual_start(*args)
            raise OSError("lost response")
        self.reserve()
        with patch.object(self.runner, "start", side_effect=response_lost):
            result = self.coordinator.execute(self.key, "run-first", self.plan)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.state()["status"], "blocked")
        replacement = ExecutionCoordinator(self.workflow, FixtureProcessRunner())
        self.assertEqual(replacement.recover(self.key, "run-first", self.plan)["status"], "uncertain")
        self.assertEqual(replacement.execute(self.key, "run-first", self.plan)["status"], "uncertain")
        self.assertEqual(self.runner.launch_count, 1)

    def test_reserved_run_survives_reopen_and_launches_only_once(self):
        self.ready()
        self.reserve()
        self.store.__exit__()
        self.store.__enter__()
        self.store.recover(self.key)
        replacement = ExecutionCoordinator(self.workflow, self.runner)
        self.assertEqual(replacement.load_plan(self.key, "run-first").document(), self.plan.document())
        result = replacement.execute(self.key, "run-first")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.runner.launch_count, 1)

    def test_journal_failure_before_dispatch_starts_no_process(self):
        self.ready()
        self.reserve()
        with patch.object(self.store, "_snapshot", side_effect=OSError("disk full")):
            with self.assertRaises(AgentError):
                self.coordinator.execute(self.key, "run-first", self.plan)
        self.assertEqual(self.runner.launch_count, 0)

    def test_unknown_process_tree_cannot_be_recorded_as_success(self):
        self.ready()
        self.reserve()
        self.coordinator._commit(self.key, "run-first", "dispatching", {})
        digest = hashlib.sha256(b"").hexdigest()
        result = ProcessResult(0, "completed", digest, digest, 0, 0, False)
        with patch.object(self.runner, "inspect", return_value=result):
            outcome = self.coordinator.recover(self.key, "run-first", self.plan)
        self.assertEqual(outcome["status"], "uncertain")
        self.assertEqual(self.state()["status"], "blocked")

    def test_inspect_failure_records_uncertainty_and_never_relaunches(self):
        self.ready()
        self.reserve()
        self.coordinator._commit(self.key, "run-first", "dispatching", {})
        with patch.object(self.runner, "inspect", side_effect=OSError("unavailable")):
            result = self.coordinator.recover(self.key, "run-first")
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.runner.launch_count, 0)

    def test_lost_response_can_be_reconciled_with_runner_owned_completion(self):
        self.ready()
        self.reserve()
        actual_start = self.runner.start
        def response_lost(*args):
            handle = actual_start(*args)
            handle.process.wait(timeout=5)
            raise OSError("lost response")
        with patch.object(self.runner, "start", side_effect=response_lost):
            self.coordinator.execute(self.key, "run-first")
        result = self.coordinator.recover(self.key, "run-first")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.runner.launch_count, 1)

    def test_active_approval_revocation_stops_process_and_discards_result(self):
        self.ready("timeout")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.execute)
            self.wait_running()
            self.gateway.permissions[1] = "read"
            result = future.result(timeout=5)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["reason"], "COMMAND_FORBIDDEN")
        self.assertTrue(result["result"]["process_tree_stopped"])

    def test_source_manifest_cannot_lie_about_digest_or_use_remote_paths(self):
        self.ready()
        document = self.plan.document()
        document["workspace"]["source_digest"] = "0" * 64
        with self.assertRaises(AgentError) as error:
            ExecutionPlan.from_document(document)
        self.assertEqual(error.exception.code, "SOURCE_MANIFEST")
        document = self.plan.document()
        document["workspace"]["root"] = "//never-contact.invalid/workspace"
        with self.assertRaises(AgentError) as error:
            ExecutionPlan.from_document(document)
        self.assertEqual(error.exception.code, "CONFIG_PATH")

    def test_disabled_repository_still_records_observed_process_termination(self):
        self.ready("timeout")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.execute)
            self.wait_running()
            self.workflow.repository = replace(self.workflow.repository, enabled=False)
            result = future.result(timeout=5)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["reason"], "REPOSITORY_DISABLED")
        self.assertTrue(result["result"]["process_tree_stopped"])

    def test_runtime_change_after_reservation_prevents_dispatch(self):
        self.ready()
        self.reserve()
        with patch.object(self.runner, "preflight", return_value="0" * 64):
            result = self.coordinator.execute(self.key, "run-first")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "RUNTIME_CHANGED")
        self.assertEqual(self.runner.launch_count, 0)

    def test_process_observation_error_cancels_owned_child_and_blocks_reexecution(self):
        self.ready("timeout")
        actual_start = self.runner.start
        def broken_observer(*args):
            handle = actual_start(*args)
            handle.poll = lambda: (_ for _ in ()).throw(OSError("observation unavailable"))
            return handle
        with patch.object(self.runner, "start", side_effect=broken_observer):
            result = self.execute()
        handle = self.runner.handles["run-first"]
        handle.process.wait(timeout=5)
        del handle.poll  # Restore class method for cleanup and real inspection.
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["reason"], "PROCESS_OBSERVATION_FAILED")
        self.assertEqual(self.state()["status"], "blocked")
