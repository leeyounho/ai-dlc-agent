from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_dlc.config.loader import parse_connection, parse_repository
from ai_dlc.errors import AgentError
from ai_dlc.evaluation.offline import OfflineNetworkGuard
from ai_dlc.storage import FileJournal, TaskKey
from ai_dlc.validation import canonical_digest, read_json
from ai_dlc.workflow import CommentObservation, IssueObservation, WorkflowEngine
from ai_dlc.workflow.commands import parse_command
from tests.support import temporary_directory

FIXTURES = Path(__file__).resolve().parents[1] / "evaluation" / "suites" / "fixtures"
REQUIREMENTS = {"summary": "Record evaluation environment", "scope": ["Report metadata only"],
                "acceptance_criteria": ["No hostnames or credentials in reports"], "open_questions": []}
DESIGN = {"summary": "Collect runtime and OS family", "changes": ["Extend report schema"],
          "validation_plan": ["Assert allowed metadata fields and compatibility"], "open_questions": []}


class FixtureGateway:
    """In-memory observations only; intentionally not a production authenticator."""

    def __init__(self, task):
        self.current_issue = IssueObservation(task, 1, "Original title", "  Original\ntext\r\n", "version-1")
        self.comments = {}
        self.permissions = {1: "write", 2: "write", 3: "read"}

    def issue(self, task):
        return self.current_issue

    def comment(self, task, comment_id):
        return self.comments.get(comment_id)

    def permission(self, task, actor_id):
        return self.permissions.get(actor_id, "none")

    def add(self, body, *, actor_id=1, actor_type="User", comment_id=None):
        cid = comment_id or max(self.comments, default=99) + 1
        self.comments[cid] = CommentObservation(self.current_issue.task, cid, actor_id, actor_type,
                                                 body, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        return cid


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.root = self.temp.__enter__()
        self.store = FileJournal(self.root).__enter__()
        self.addCleanup(self.store.__exit__, None, None, None)
        self.network = OfflineNetworkGuard().__enter__()
        self.addCleanup(self.network.__exit__, None, None, None)
        self.connection = parse_connection(read_json(FIXTURES / "connection.json"), base_dir=FIXTURES)
        self.raw = read_json(FIXTURES / "repository.json")
        self.key = TaskKey(self.raw["github_instance_id"], self.raw["repository_id"], 10)
        self.gateway = FixtureGateway(self.key)
        self.configure()
        self.events = 0

    def tearDown(self):
        self.assertEqual(self.network.attempts, 0)

    def configure(self):
        self.repo = parse_repository(self.raw, connection=self.connection)
        self.engine = WorkflowEngine(self.store, self.repo, self.gateway)

    def event(self):
        self.events += 1
        return f"event-{self.events}"

    def state(self):
        return self.store.read(self.key)

    def revision(self):
        return (self.state() or {}).get("state_revision", 0)

    def capture(self):
        return self.engine.capture_source(self.key, expected_revision=self.revision(), event_id=self.event())

    def normalize(self, document=REQUIREMENTS):
        return self.engine.normalize(self.key, deepcopy(document), expected_revision=self.revision(), event_id=self.event())

    def comment(self, text, **kwargs):
        cid = self.gateway.add(text, **kwargs)
        return self.engine.handle_comment(self.key, cid, expected_revision=self.revision())

    def design(self, document=DESIGN):
        return self.engine.propose_design(self.key, deepcopy(document), requirements_revision=self.state()["requirements"]["revision"],
                                          expected_revision=self.revision(), event_id=self.event())

    def gate(self, **kwargs):
        state = self.state()
        return self.engine.implementation_gate(self.key, expected_revision=kwargs.get("revision", state["state_revision"]),
                                               cancellation_epoch=kwargs.get("epoch", state["cancellation_epoch"]))

    def assert_code(self, code, callback):
        revision = self.revision()
        with self.assertRaises(AgentError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(revision, self.revision(), "Denied operations must not mutate state")

    def ready(self, *, automatic=False):
        if automatic:
            self.raw["workflow"].update(start_policy="on_approval", design_mode="automatic")
            self.configure()
        self.capture()
        self.normalize()
        self.comment("/aidlc approve requirements req-0001")
        if not automatic:
            self.comment("/aidlc start req-0001")
        self.design()
        if not automatic:
            self.comment("/aidlc approve design des-0001")
        return self.gate()

    def test_explicit_start_and_collaborative_design_are_independent_gates(self):
        self.capture()
        self.assertEqual((self.state()["phase"], self.state()["status"]), ("requirements", "queued"))
        self.normalize()
        self.assert_code("GATE_CLOSED", self.gate)
        self.assert_code("APPROVAL_REQUIRED", self.design)
        self.comment("/aidlc approve requirements req-0001")
        self.assertEqual(self.state()["status"], "waiting_human")
        self.assert_code("START_REQUIRED", self.design)
        self.comment("/aidlc start req-0001")
        self.assertEqual((self.state()["phase"], self.state()["status"]), ("design", "queued"))
        self.design()
        self.assertEqual(self.state()["status"], "waiting_human")
        self.comment("/aidlc approve design des-0001")
        self.assertEqual(self.gate().requirements_revision, "req-0001")
        self.assertEqual((self.state()["phase"], self.state()["status"]), ("implementation", "queued"))

    def test_automatic_flow_still_requires_human_requirements_approval(self):
        self.raw["workflow"].update(start_policy="on_approval", design_mode="automatic")
        self.configure()
        self.capture()
        self.normalize()
        self.assert_code("APPROVAL_REQUIRED", self.design)
        self.comment("/aidlc approve requirements req-0001")
        self.design()
        self.assertEqual(self.gate().design_revision, "des-0001")
        self.assertIsNone(self.state()["design_approval"])

    def test_issue_override_requires_starter_and_repository_allowlist(self):
        self.capture()
        self.normalize()
        self.raw["roles"]["starter"]["actor_ids"] = [2]
        self.configure()
        self.engine.refresh_policy(self.key, expected_revision=self.revision(), event_id=self.event())
        self.normalize()
        text = "/aidlc approve requirements req-0002 start=on_approval design=automatic"
        self.assert_code("COMMAND_FORBIDDEN", lambda: self.comment(text))
        self.raw["roles"]["starter"]["actor_ids"] = [1]
        self.raw["workflow"]["allowed_issue_overrides"]["design_mode"] = []
        self.configure()
        self.engine.refresh_policy(self.key, expected_revision=self.revision(), event_id=self.event())
        self.normalize()
        self.assert_code("OVERRIDE_FORBIDDEN", lambda: self.comment(text.replace("req-0002", "req-0003")))

    def test_permitted_overrides_reach_implementation_without_design_comment(self):
        self.capture()
        self.normalize()
        self.comment("/aidlc approve requirements req-0001 start=on_approval design=automatic")
        self.design()
        self.gate()
        self.assertEqual(self.state()["requirement_approval"]["roles"], ["requirement_approver", "starter"])

    def test_original_text_and_all_revisions_survive_source_change(self):
        old = self.ready()
        original_digest = self.state()["original_source_digest"]
        original = self.store.blob(self.key, original_digest)
        self.assertEqual(original["body"], "  Original\ntext\r\n")
        self.gateway.current_issue = replace(self.gateway.current_issue, body="Revised source", version="version-2")
        self.assert_code("SOURCE_CHANGED", self.gate)
        self.capture()
        self.normalize()
        self.assertEqual(self.state()["requirements"]["revision"], "req-0002")
        self.assertEqual(self.state()["source_revision"], 2)
        self.assertIsNone(self.state()["requirement_approval"])
        self.assertIsNone(self.state()["design"])
        self.assertGreater(self.state()["cancellation_epoch"], old.cancellation_epoch)
        self.assertEqual(self.store.blob(self.key, original_digest), original)
        self.assert_code("REVISION_STALE", lambda: self.comment("/aidlc approve requirements req-0001"))

    def test_renormalizing_or_amending_requires_new_approval(self):
        self.ready()
        self.comment("/aidlc amend req-0001\nInclude environment comparison.", actor_id=2)
        self.assertIsNone(self.state()["requirements"])
        amendment = self.store.blob(self.key, self.state()["amendments"][0])
        self.assertIn("Include environment", amendment["body"])
        self.normalize()
        self.assertEqual(self.state()["requirements"]["revision"], "req-0002")
        self.assert_code("GATE_CLOSED", self.gate)

    def test_design_revision_invalidates_collaborative_approval(self):
        self.ready()
        self.design()
        self.assertIsNone(self.state()["design_approval"])
        self.assert_code("GATE_CLOSED", self.gate)
        self.assert_code("REVISION_STALE", lambda: self.comment("/aidlc approve design des-0001"))
        self.comment("/aidlc approve design des-0002")
        self.gate()

    def test_open_questions_block_approval_and_automatic_design(self):
        self.capture()
        document = deepcopy(REQUIREMENTS)
        document["open_questions"] = ["Which environments?"]
        self.normalize(document)
        self.assert_code("QUESTIONS_OPEN", lambda: self.comment("/aidlc approve requirements req-0001"))
        self.normalize()
        self.comment("/aidlc approve requirements req-0002 start=on_approval design=automatic")
        document = deepcopy(DESIGN)
        document["open_questions"] = ["How to compare missing values?"]
        self.design(document)
        self.assert_code("GATE_CLOSED", self.gate)

    def test_bot_edited_unauthorized_and_quoted_comments_cannot_approve(self):
        self.capture()
        self.normalize()
        for text in ("> /aidlc approve requirements req-0001", "```\n/aidlc approve requirements req-0001\n```",
                     "Discussion\n/aidlc approve requirements req-0001", "    /aidlc approve requirements req-0001"):
            self.assert_code("COMMAND_IGNORED", lambda: self.comment(text))
        self.assert_code("COMMAND_ACTOR", lambda: self.comment("/aidlc approve requirements req-0001", actor_type="Bot"))
        self.assert_code("COMMAND_FORBIDDEN", lambda: self.comment("/aidlc approve requirements req-0001", actor_id=2))
        self.assert_code("COMMAND_FORBIDDEN", lambda: self.comment("/aidlc approve requirements req-0001", actor_id=3))
        cid = self.gateway.add("/aidlc approve requirements req-0001")
        self.gateway.comments[cid] = replace(self.gateway.comments[cid], updated_at="later")
        self.assert_code("COMMAND_EDITED", lambda: self.engine.handle_comment(self.key, cid, expected_revision=self.revision()))

    def test_deleted_approval_denied_before_reconcile_and_never_reused(self):
        self.ready()
        cid = self.state()["requirement_approval"]["comment_id"]
        original = self.gateway.comments.pop(cid)
        self.assert_code("APPROVAL_REVOKED", self.gate)
        self.engine.reconcile(self.key, expected_revision=self.revision(), event_id=self.event())
        self.assertIsNone(self.state()["requirement_approval"])
        self.gateway.comments[cid] = original
        result = self.engine.handle_comment(self.key, cid, expected_revision=2)
        self.assertTrue(result.duplicate)
        self.assertIsNone(result.state["requirement_approval"])
        self.assert_code("GATE_CLOSED", self.gate)

    def test_permission_revocation_and_edited_design_are_rechecked(self):
        self.ready()
        self.gateway.permissions[1] = "read"
        self.assert_code("COMMAND_FORBIDDEN", self.gate)
        self.gateway.permissions[1] = "write"
        cid = self.state()["design_approval"]["comment_id"]
        self.gateway.comments[cid] = replace(self.gateway.comments[cid], updated_at="later")
        self.assert_code("COMMAND_EDITED", self.gate)
        self.engine.reconcile(self.key, expected_revision=self.revision(), event_id=self.event())
        self.assertIsNone(self.state()["design_approval"])
        self.assertIsNotNone(self.state()["requirement_approval"])

    def test_resume_does_not_create_or_replace_approval(self):
        basis = self.ready()
        self.comment("/aidlc stop")
        self.assertEqual(self.state()["status"], "paused")
        self.assert_code("STATE_CONFLICT", lambda: self.gate(revision=basis.state_revision, epoch=basis.cancellation_epoch))
        self.assert_code("GATE_CLOSED", self.gate)
        cid = self.state()["requirement_approval"]["comment_id"]
        original = self.gateway.comments.pop(cid)
        self.assert_code("APPROVAL_REVOKED", lambda: self.comment("/aidlc resume"))
        self.gateway.comments[cid] = original
        self.comment("/aidlc resume")
        self.gate()

    def test_cancel_is_terminal_and_closed_issue_is_not_success(self):
        self.ready()
        self.comment("/aidlc cancel")
        self.assertEqual(self.state()["status"], "cancelled")
        self.assert_code("TASK_TERMINAL", lambda: self.comment("/aidlc resume"))
        self.assert_code("TASK_TERMINAL", self.normalize)

    def test_manually_closed_issue_blocks_gate_then_is_recorded_as_cancelled(self):
        self.ready()
        self.gateway.current_issue = replace(self.gateway.current_issue, open=False)
        self.assert_code("ISSUE_CLOSED", self.gate)
        self.capture()
        self.assertEqual(self.state()["status"], "cancelled")

    def test_recovery_preserves_gate_but_rereads_remote_approval(self):
        self.ready()
        before = self.state()
        self.store.__exit__()
        self.store.__enter__()
        self.assertEqual(self.store.recover(self.key), before)
        self.gate()
        self.gateway.comments.pop(before["start_approval"]["comment_id"])
        self.assert_code("APPROVAL_REVOKED", self.gate)

    def test_policy_change_requires_new_normalization_and_approval(self):
        self.ready()
        self.raw["workflow"]["design_mode"] = "automatic"
        self.configure()
        self.assert_code("POLICY_CHANGED", self.gate)
        self.engine.refresh_policy(self.key, expected_revision=self.revision(), event_id=self.event())
        self.assertIsNone(self.state()["requirements"])
        self.assertEqual(self.state()["phase"], "requirements")
        self.assert_code("GATE_CLOSED", self.gate)

    def test_checkpoint_failure_cannot_leave_an_open_gate(self):
        self.ready()
        with patch.object(self.store, "_snapshot", side_effect=OSError("disk full")):
            result = self.engine.reconcile(self.key, expected_revision=self.revision(), event_id=self.event())
        self.assertFalse(result.snapshot_current)
        self.assert_code("STATE_UNHEALTHY", self.gate)

    def test_observation_failures_never_silently_grant_permission(self):
        self.ready()
        with patch.object(self.gateway, "permission", side_effect=AgentError("UNAVAILABLE", "Unavailable.")):
            self.assert_code("UNAVAILABLE", self.gate)
            self.assert_code("UNAVAILABLE", lambda: self.engine.reconcile(self.key, expected_revision=self.revision(), event_id=self.event()))

    def test_misbound_observation_and_disabled_repository_rejected(self):
        self.gateway.current_issue = replace(self.gateway.current_issue, task=TaskKey("other", 1, 10))
        self.assert_code("OBSERVATION_INVALID", self.capture)
        self.raw["enabled"] = False
        self.configure()
        self.assert_code("REPOSITORY_DISABLED", self.capture)

    def test_command_options_are_strict(self):
        for body in ("/aidlc", "/aidlc approve requirements", "/aidlc stop now", "/aidlc start req-0001 unknown=yes",
                     "/aidlc start req-0001 design=automatic design=automatic", "/aidlc amend req-0001",
                     "/aidlc approve design des-0001 start=on_approval"):
            with self.subTest(body=body), self.assertRaises(AgentError) as caught:
                parse_command(body)
            self.assertEqual(caught.exception.code, "COMMAND_INVALID")

    def test_language_neutral_profile_does_not_change_workflow(self):
        self.raw["project"].update(adapter="command", toolchain_id="python-approved", junit_report_patterns=[],
            command_overrides={"test": {"executable_id": "python", "argv": ["-m", "unittest", "discover", "-s", "tests"],
                                         "cwd": ".", "timeout_seconds": 120, "report_patterns": []}})
        self.configure()
        self.assertEqual(self.repo.project.commands["test"].argv[0], "-m")
        with self.assertRaises(TypeError):
            self.repo.project.commands["unexpected"] = self.repo.project.commands["test"]
        self.ready()
