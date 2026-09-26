from collections import deque
from dataclasses import replace
from pathlib import Path
import threading
import time
import unittest

from ai_dlc.agents import AgentDriver, AgentLimits, AgentLoop
from ai_dlc.agents.status import StatusPublisher, render_status
from ai_dlc.config.loader import parse_connection, parse_repository
from ai_dlc.errors import AgentError
from ai_dlc.evaluation.offline import OfflineNetworkGuard
from ai_dlc.evaluation.process_fixture import FixtureProcessRunner
from ai_dlc.execution.file_tools import WorkspaceTools
from ai_dlc.execution.workspace import WorkspaceManager
from ai_dlc.models import AdapterRegistry, ModelConcurrency, ModelRegistry, ModelResponse, ModelRouter, ModelSessions, ToolCall
from ai_dlc.models.types import json_text
from ai_dlc.service.scheduler import FairScheduler
from ai_dlc.storage import FileJournal, TaskKey
from ai_dlc.validation import canonical_digest, read_json
from ai_dlc.workflow import WorkflowEngine
from tests.support import temporary_directory
from tests.test_workflow import FixtureGateway, REQUIREMENTS, DESIGN, FIXTURES


class ScriptedModel:
    version = "scripted-agent-v1"

    def __init__(self):
        self.outputs, self.calls = deque(), []

    def add(self, purpose, output):
        self.outputs.append((purpose, output))

    def generate(self, request, selected, **options):
        self.calls.append((request, selected))
        purpose, output = self.outputs.popleft()
        if purpose != selected.purpose:
            raise AssertionError("Unexpected purpose")
        return output(request, **options) if callable(output) else output


class StatusFixture:
    def __init__(self):
        self.comments, self.creates, self.updates = [], 0, 0
        self.lose_response = False

    def find(self, key, marker):
        return [dict(c) for c in self.comments if c["body"].startswith(marker)]

    def create(self, key, body):
        self.creates += 1
        self.comments.append({"id": 700, "body": body})
        if self.lose_response:
            self.lose_response = False
            raise AgentError("EFFECT_UNKNOWN", "Lost response.")

    def update(self, key, comment_id, body, *, expected_body):
        assert self.comments[0]["body"] == expected_body
        self.updates += 1
        self.comments[0]["body"] = body


def text(value):
    return ModelResponse(value if type(value) is str else json_text(value), (), "stop")


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.store = FileJournal(self.root / "state").__enter__()
        self.addCleanup(lambda: self.store.__exit__())
        self.offline = OfflineNetworkGuard().__enter__()
        self.addCleanup(self.offline.__exit__, None, None, None)
        raw = read_json(FIXTURES / "connection.json")
        raw["llm"]["providers"]["internal"].update(adapter="custom", adapter_id="scripted")
        for model in raw["llm"]["models"].values():
            model["context_window_tokens"] = 131072
        self.connection = parse_connection(raw, base_dir=FIXTURES)
        self.raw = read_json(FIXTURES / "repository.json")
        self.raw["workflow"].update(start_policy="on_approval", design_mode="automatic")
        self.raw["project"].update(adapter="command", toolchain_id="local-fixture-only", junit_report_patterns=[],
            command_overrides={"unit_test": {"executable_id": "fixture-python", "argv": ["source_assertion"],
                "cwd": ".", "timeout_seconds": 5, "report_patterns": ["reports/*.xml"]}})
        self.key = TaskKey("local-evaluation", 1, 10)
        self.gateway = FixtureGateway(self.key)
        self.model = ScriptedModel()
        self.runner = FixtureProcessRunner()
        self.env = {"EVAL_MODEL_TOKEN": "fixture-only", "EVAL_ALPHA": "alpha", "EVAL_BETA": "beta", "EVAL_TEXT": "text"}
        source = self.root / "source"
        source.mkdir()
        (source / "input.txt").write_bytes(b"broken\n")
        self.workspace = WorkspaceManager(self.root / "workspaces", protected_roots=(self.store.root,)).prepare(source, ("input.txt",))
        self.files = WorkspaceTools(self.workspace)
        self.verifications = WorkspaceManager(self.root / "verification", protected_roots=(self.store.root,))
        self.status = StatusFixture()
        self.configure()

    def tearDown(self):
        for handle in self.runner.handles.values():
            if handle.poll() is None:
                handle.cancel("cancelled")
                handle.process.wait(timeout=5)
        self.assertEqual(self.offline.attempts, 0)

    def configure(self, limits=AgentLimits(model_timeout_seconds=5, max_output_tokens=1024), *, publish=False):
        self.repo = parse_repository(self.raw, connection=self.connection)
        self.workflow = WorkflowEngine(self.store, self.repo, self.gateway)
        self.sessions = ModelSessions(self.store, ModelRouter(ModelRegistry(self.connection)),
            AdapterRegistry(None, custom={"scripted": self.model}), ModelConcurrency(2, {"internal": 2}))
        self.loop = AgentLoop(self.workflow, self.sessions, self.files, self.verifications, self.runner,
            environment=self.env, limits=limits, generated_patterns=("reports/**",), monitor_interval=0.02,
            status_publisher=StatusPublisher(self.store, self.status) if publish else None)

    def state(self):
        return self.store.read(self.key)

    def comment(self, body, **kwargs):
        cid = self.gateway.add(body, **kwargs)
        return self.workflow.handle_comment(self.key, cid, expected_revision=self.state()["state_revision"])

    def start(self):
        self.workflow.capture_source(self.key, expected_revision=0, event_id="source")
        self.loop.start(self.key, "run-one")

    def ready(self):
        self.start()
        self.model.add("requirements", text(REQUIREMENTS))
        self.assertEqual(self.loop.step(self.key)["stage"], "design")
        self.comment("/aidlc approve requirements req-0001")
        if self.repo.workflow.start_policy == "explicit":
            self.assertEqual(self.loop.step(self.key)["reason"], "START_REQUIRED")
            self.comment("/aidlc start req-0001")
        self.model.add("design", text(DESIGN))
        self.assertEqual(self.loop.step(self.key)["stage"], "implementation")
        if self.repo.workflow.design_mode == "collaborative":
            self.assertEqual(self.loop.step(self.key)["reason"], "APPROVAL_REQUIRED")
            self.comment("/aidlc approve design des-0001")

    def patch_response(self, request, **kwargs):
        state = self.files.state()
        call = ToolCall("patch-" + request.request_id, "apply_patch", json_text({
            "patch": "--- a/input.txt\n+++ b/input.txt\n@@ -1 +1 @@\n-broken\n+fixed\n",
            "workspace_digest": state.digest,
            "expected_files": [{"path": "input.txt", "sha256": state.files[0].sha256}]}))
        return ModelResponse("", (call,), "tool_calls")

    def finish(self, *, patch=True):
        if patch:
            self.model.add("implementation", self.patch_response)
            self.assertEqual(self.loop.step(self.key)["status"], "queued")
        self.model.add("implementation", text("Implemented"))
        self.model.add("test_generation", text("Tests prepared"))
        self.assertEqual(self.loop.step(self.key)["stage"], "test_generation")
        self.assertEqual(self.loop.step(self.key)["stage"], "verification")
        result = self.loop.step(self.key)
        if result["stage"] == "review":
            self.model.add("review", text({"summary": "Reviewed", "findings": []}))
            result = self.loop.step(self.key)
        return result

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_automatic_flow_preserves_original_runs_real_test_and_advisory_review(self):
        self.configure(publish=True)
        self.ready()
        self.assertEqual(self.state()["state_revision"], len(self.store.history(self.key)))
        result = self.finish()
        self.assertEqual(result["status"], "ready_for_pr")
        self.assertEqual(self.runner.launch_count, 1)
        self.assertTrue(result["checks"][0]["verification"]["passed"])
        self.assertEqual(result["checks"][0]["source_digest"], self.files.state().digest)
        self.assertFalse(result["review"]["human_approval"])
        self.assertIsNone(self.state()["design_approval"])
        original = self.store.blob(self.key, self.state()["original_source_digest"])
        self.assertEqual(original["body"], "  Original\ntext\r\n")
        self.assertEqual(self.status.creates, 1)
        self.assertGreater(self.status.updates, 0)
        self.assertIn("참고 의견", self.status.comments[0]["body"])
        self.assertEqual((self.root / "source/input.txt").read_text(), "broken\n")

    def test_explicit_collaborative_flow_waits_for_both_human_gates(self):
        self.raw["workflow"].update(start_policy="explicit", design_mode="collaborative")
        self.configure()
        self.ready()
        self.assertIsNotNone(self.state()["start_approval"])
        self.assertIsNotNone(self.state()["design_approval"])
        self.assertEqual(self.finish()["status"], "ready_for_pr")

    def test_questions_and_split_proposals_never_create_approval(self):
        self.start()
        document = {**REQUIREMENTS, "open_questions": ["Which report format?"], "split_proposals": ["Separate UI work"]}
        self.model.add("requirements", text(document))
        self.loop.step(self.key)
        self.assert_code("QUESTIONS_OPEN", lambda: self.comment("/aidlc approve requirements req-0001"))
        self.assertEqual(self.loop.step(self.key)["status"], "waiting_human")
        body = render_status(self.store, self.key)
        self.assertIn("Which report format?", body)
        self.assertIn("Separate UI work", body)
        self.assertEqual(len(self.model.calls), 1)

    def test_failed_real_test_is_returned_to_repair_context_and_retested(self):
        self.ready()
        failed = self.finish(patch=False)
        self.assertEqual(failed["stage"], "implementation")
        self.assertEqual(failed["repair_iterations"], 1)
        self.assertFalse(failed["checks"][0]["verification"]["passed"])
        result = self.finish()
        self.assertEqual(result["status"], "ready_for_pr")
        self.assertEqual(self.runner.launch_count, 2)
        repair_request = self.model.calls[4][0]
        self.assertIn("JUNIT_FAILED_OR_EMPTY", repair_request.messages[1].content)
        self.assertEqual(result["checks"][0]["source_digest"], self.workspace.digest)
        self.assertNotEqual(result["checks"][1]["source_digest"], self.workspace.digest)

    def test_repair_budget_exhaustion_retains_diff_and_failed_evidence(self):
        self.configure(AgentLimits(repair_iterations=0, model_timeout_seconds=5, max_output_tokens=1024))
        self.ready()
        result = self.finish(patch=False)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "AGENT_REPAIR_BUDGET")
        self.assertEqual(len(result["checks"]), 1)
        self.assertIn("workspace_digest", result["diff"])

    def test_model_protocol_injection_and_readonly_review_cannot_patch(self):
        self.ready()
        forbidden = ToolCall("forged", "approve_requirements", "{}")
        self.model.add("implementation", ModelResponse("", (forbidden,), "tool_calls"))
        result = self.loop.step(self.key)
        self.assertEqual(result["reason"], "MODEL_TOOL_ARGUMENTS")
        self.assertEqual((self.workspace.root / "input.txt").read_text(), "broken\n")
        self.assertEqual(self.runner.launch_count, 0)

    def test_review_does_not_advertise_write_or_command_tools(self):
        self.ready()
        self.finish()
        request, selected = self.model.calls[-1]
        self.assertEqual(selected.purpose, "review")
        self.assertEqual({tool.name for tool in request.tools}, {"list_files", "read_file", "search_text"})
        self.assertNotIn("human_approval", json_text(self.state()["requirement_approval"]))

    def test_permission_revocation_during_model_call_stops_before_patch(self):
        self.ready()
        def revoked(request, **options):
            self.gateway.permissions[1] = "read"
            options["cancellation"].wait(1)
            return self.patch_response(request)
        self.model.add("implementation", revoked)
        result = self.loop.step(self.key)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "MODEL_CANCELLED")
        self.assertEqual(self.files.state().digest, self.workspace.digest)

    def test_source_change_and_stop_prevent_new_model_calls(self):
        self.ready()
        count = len(self.model.calls)
        self.comment("/aidlc stop")
        self.assertEqual(self.loop.step(self.key)["reason"], "AGENT_PAUSED")
        self.comment("/aidlc resume")
        self.gateway.current_issue = replace(self.gateway.current_issue, body="new requirements", version="v2")
        self.assertEqual(self.loop.step(self.key)["reason"], "SOURCE_CHANGED")
        self.assertEqual(len(self.model.calls), count)

    def test_amendment_restarts_normalization_without_resetting_budget_or_approval(self):
        self.ready()
        calls = self.sessions.inspect(self.key, "run-one")["model_calls"]
        self.comment("/aidlc amend req-0001\nUse XML output and resolve the question.")
        self.model.add("requirements", text({**REQUIREMENTS, "summary": "XML output"}))
        result = self.loop.step(self.key)
        self.assertEqual(result["stage"], "design")
        self.assertEqual(self.state()["requirements"]["revision"], "req-0002")
        self.assertIsNone(self.state()["requirement_approval"])
        self.assertEqual(self.loop.step(self.key)["reason"], "APPROVAL_REQUIRED")
        self.assertEqual(self.sessions.inspect(self.key, "run-one")["model_calls"], calls + 1)
        self.assertIn("Use XML output", self.model.calls[-1][0].messages[1].content)
        self.assertIn("#issuecomment-", render_status(self.store, self.key))

    def test_cancel_is_terminal_before_model_dispatch(self):
        self.ready()
        self.comment("/aidlc cancel")
        self.assertEqual(self.loop.step(self.key)["reason"], "TASK_TERMINAL")
        self.assertEqual(len(self.model.calls), 2)

    def test_policy_change_blocks_before_model_dispatch(self):
        self.ready()
        self.raw["workflow"]["design_mode"] = "collaborative"
        self.configure()
        self.assertEqual(self.loop.step(self.key)["reason"], "POLICY_CHANGED")
        self.assertEqual(len(self.model.calls), 2)

    def test_model_budget_exhaustion_keeps_current_diff(self):
        self.configure(AgentLimits(model_calls=2, model_timeout_seconds=5, max_output_tokens=1024))
        self.ready()
        result = self.loop.step(self.key)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "MODEL_BUDGET")
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(result["diff"]["workspace_digest"], self.workspace.digest)

    def test_active_budget_exhaustion_does_not_dispatch(self):
        self.configure(AgentLimits(active_seconds=1, model_timeout_seconds=5, max_output_tokens=1024))
        self.start()
        self.assertEqual(self.loop.step(self.key)["reason"], "AGENT_ACTIVE_BUDGET")
        self.assertEqual(len(self.model.calls), 0)

    def test_context_overflow_preserves_source_and_diff_without_model_call(self):
        self.gateway.current_issue = replace(self.gateway.current_issue, body="x" * 131000)
        self.start()
        result = self.loop.step(self.key)
        self.assertEqual(result["reason"], "MODEL_CONTEXT_LIMIT")
        self.assertEqual(len(self.model.calls), 0)
        self.assertEqual(len(self.store.blob(self.key, self.state()["original_source_digest"])["body"]), 131000)
        self.assertEqual(result["diff"]["workspace_digest"], self.workspace.digest)

    def test_duplicate_tool_id_never_reapplies_patch(self):
        self.ready()
        response = self.patch_response(type("Request", (), {"request_id": "same"})())
        self.model.add("implementation", response)
        self.loop.step(self.key)
        self.model.add("implementation", response)
        result = self.loop.step(self.key)
        self.assertEqual(result["reason"], "MODEL_TOOL_ARGUMENTS")
        self.assertEqual(self.sessions.inspect(self.key, "run-one")["tool_calls"], 1)
        self.assertEqual((self.workspace.root / "input.txt").read_text(), "fixed\n")

    def test_model_requested_checks_feed_actual_failure_into_next_tool_round(self):
        self.ready()
        self.model.add("implementation", ModelResponse("", (ToolCall("check-one", "run_checks", "{}"),), "tool_calls"))
        result = self.loop.step(self.key)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["checks"][0]["reason"], "JUNIT_FAILED_OR_EMPTY")
        def repair(request, **options):
            self.assertEqual(request.messages[-1].role, "tool")
            self.assertIn("JUNIT_FAILED_OR_EMPTY", request.messages[-1].content)
            return self.patch_response(request)
        self.model.add("implementation", repair)
        self.loop.step(self.key)
        self.assertEqual(self.finish(patch=False)["status"], "ready_for_pr")
        self.assertEqual(self.runner.launch_count, 2)

    def test_cancellation_during_real_command_never_becomes_verified(self):
        self.raw["project"]["command_overrides"]["unit_test"]["argv"] = ["timeout"]
        self.configure()
        self.ready()
        self.model.add("implementation", text("Implemented"))
        self.model.add("test_generation", text("Tests prepared"))
        self.loop.step(self.key)
        self.loop.step(self.key)
        cancellation = threading.Event()
        start = self.runner.start
        def cancel_after_start(*args):
            handle = start(*args)
            cancellation.set()
            return handle
        self.runner.start = cancel_after_start
        result = self.loop.step(self.key, cancellation=cancellation)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("verified_digest", result)
        self.assertEqual(result["checks"][0]["result"]["termination"], "cancelled")
        self.assertTrue(result["checks"][0]["result"]["process_tree_stopped"])

    def test_change_after_checks_cannot_be_bound_to_successful_evidence(self):
        self.ready()
        self.model.add("implementation", self.patch_response)
        self.model.add("implementation", text("Implemented"))
        self.model.add("test_generation", text("Tests prepared"))
        for _ in range(3):
            self.loop.step(self.key)
        checks = self.loop._checks
        def change_after_checks(*args):
            result = checks(*args)
            (self.workspace.root / "input.txt").write_bytes(b"untested\n")
            return result
        self.loop._checks = change_after_checks
        result = self.loop.step(self.key)
        self.assertEqual(result["reason"], "WORKSPACE_CHANGED")
        self.assertNotIn("verified_digest", result)

    def test_timestamp_only_observation_does_not_revoke_approval(self):
        self.ready()
        before = self.state()
        self.gateway.current_issue = replace(self.gateway.current_issue, version="comment-updated-at")
        self.workflow.capture_source(self.key, expected_revision=before["state_revision"], event_id="timestamp-only")
        self.assertEqual(self.state()["requirements"], before["requirements"])
        self.assertEqual(self.state()["requirement_approval"], before["requirement_approval"])
        self.assertEqual(self.state()["source_revision"], before["source_revision"])
        self.assertNotEqual(self.state()["source_observation_digest"], before["source_observation_digest"])

    def test_restart_after_patch_effect_does_not_reapply_or_reset_budgets(self):
        self.ready()
        apply = self.files.apply_patch
        def interrupted(*args, **kwargs):
            apply(*args, **kwargs)
            raise KeyboardInterrupt()
        self.files.apply_patch = interrupted
        self.model.add("implementation", self.patch_response)
        with self.assertRaises(KeyboardInterrupt):
            self.loop.step(self.key)
        budget = self.sessions.inspect(self.key, "run-one")["tool_calls"]
        self.store.__exit__()
        self.store = FileJournal(self.root / "state").__enter__()
        self.files = WorkspaceTools.reopen(self.workspace)
        self.configure()
        result = self.loop.recover(self.key)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["diff"]["modified"][0]["path"], "input.txt")
        self.assert_code("AGENT_RECOVERY_REQUIRED", lambda: self.loop.step(self.key))
        self.assertEqual(self.sessions.inspect(self.key, "run-one")["tool_calls"], budget)
        self.assertEqual((self.workspace.root / "input.txt").read_text(), "fixed\n")

    def test_status_response_loss_reconciles_without_duplicate_and_preserves_human_edit(self):
        self.start()
        publisher = StatusPublisher(self.store, self.status)
        self.status.lose_response = True
        self.assert_code("EFFECT_UNKNOWN", lambda: publisher.publish(self.key))
        publisher.publish(self.key)
        self.assertEqual(self.status.creates, 1)
        self.status.comments[0]["body"] += "\nHuman note"
        self.assert_code("STATUS_COMMENT_EDITED", lambda: publisher.publish(self.key))
        self.assertEqual(self.status.updates, 0)

    def test_ready_result_is_invalidated_after_source_changes(self):
        self.ready()
        self.finish()
        (self.workspace.root / "input.txt").write_text("other", encoding="utf-8")
        result = self.loop.step(self.key)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "WORKSPACE_CHANGED")

    def test_driver_uses_scheduler_and_waits_without_spinning_on_human_gate(self):
        self.start()
        self.model.add("requirements", text(REQUIREMENTS))
        driver = AgentDriver()
        driver.register(self.key, self.loop)
        scheduler = FairScheduler(global_limit=1, repository_limit=1, model_limit=1)
        try:
            for _ in range(100):
                driver.schedule(scheduler)
                scheduler.tick()
                scheduler.acknowledge_completed()
                if self.state()["agent"]["status"] == "waiting_human":
                    break
                time.sleep(0.02)
            self.assertEqual(self.state()["agent"]["status"], "waiting_human")
            time.sleep(0.04)
            scheduler.acknowledge_completed()
            self.assertEqual(driver.schedule(scheduler), 0)
            self.assertEqual(len(self.model.calls), 1)
        finally:
            scheduler.shutdown(1)


if __name__ == "__main__":
    unittest.main()
