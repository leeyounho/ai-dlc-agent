from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import threading
import unittest

from ai_dlc.errors import AgentError
from ai_dlc.execution import GitWorkspaceManager, WorkspaceTools
from ai_dlc.github.client import RepositoryPolicyObservation
from ai_dlc.publishing import GitPublisher, PublicationCoordinator
from ai_dlc.models import ModelResponse, ToolCall
from ai_dlc.models.types import json_text, json_value
from ai_dlc.storage import FileJournal
from tests.test_git_workspace_and_tools import GitFixture
import tests.test_agent_loop as helpers


NOTES = {"decisions": "Keep fixed source data in input.txt; test in an isolated snapshot.",
         "limitations": "Bundled synthetic subprocess only. No live LLM, RHEL, GHES, or deployment validation.",
         "deployment": "No deployment in this change; separate operator approval required.",
         "recovery": "Keep journal and source; reconcile uncertain effects before any new attempt.",
         "knowledge": "Proposed docs/adr/source-publication.md records the source decision and is verified in this PR.",
         "questions": "No remaining design questions; human PR review is still required."}


class PublicationApi:
    def __init__(self, git):
        self.git = git
        self.pull_records, self.check_records = [], []
        self.creates, self.check_creates = 0, 0
        self.lose = None
        self.current_policy = RepositoryPolicyObservation("main", True, ("human-ci",), 1, True, ())

    def policy(self, key):
        return self.current_policy

    def pulls(self, key, marker, branch):
        result = deepcopy(self.pull_records)
        for pull in result:
            pull["head"] = self.git.refs("main", branch)["head"]
        return result

    def create_pull(self, key, document):
        self.creates += 1
        self.pull_records.append({**document, "number": 15, "owned": True, "state": "open",
                                  "url": "https://github.example/test/repository/pull/15"})
        if self.lose == "pull":
            raise AgentError("EFFECT_UNKNOWN", "Response lost.")

    def checks(self, key, head, external_id):
        return deepcopy([c for c in self.check_records if c["head_sha"] == head and c["external_id"] == external_id])

    def create_check(self, key, document):
        self.check_creates += 1
        self.check_records.append({**deepcopy(document), "id": 50})
        if self.lose == "check":
            raise AgentError("EFFECT_UNKNOWN", "Response lost.")

    def invalidate_check(self, key, check_id):
        self.check_records[0]["conclusion"] = "action_required"


class PublicationTests(unittest.TestCase):
    # Reuse the real workflow/model/file/runner setup without inheriting and
    # executing the unrelated AgentLoop test cases a second time.
    configure = helpers.AgentLoopTests.configure
    state = helpers.AgentLoopTests.state
    comment = helpers.AgentLoopTests.comment
    start = helpers.AgentLoopTests.start
    ready = helpers.AgentLoopTests.ready
    finish = helpers.AgentLoopTests.finish
    assert_code = helpers.AgentLoopTests.assert_code
    tearDown = helpers.AgentLoopTests.tearDown

    def patch_response(self, request, **kwargs):
        response = helpers.AgentLoopTests.patch_response(self, request, **kwargs)
        call = response.tool_calls[0]
        args = json_value(call.arguments_json)
        args["patch"] += "--- /dev/null\n+++ b/docs/adr/source-publication.md\n@@ -0,0 +1,3 @@\n+# Source publication\n+\n+Status: proposed\n"
        args["expected_files"].append({"path": "docs/adr/source-publication.md", "sha256": ""})
        return ModelResponse("", (ToolCall(call.id, call.name, json_text(args)),), "tool_calls")

    def setUp(self):
        helpers.AgentLoopTests.setUp(self)
        fixture_root = self.root / "git-fixture"
        fixture_root.mkdir()
        self.fixture = GitFixture(fixture_root)
        self.fixture.write("input.txt", "broken\n")
        self.fixture.run("-C", str(self.fixture.source), "add", "input.txt")
        self.fixture.run("-C", str(self.fixture.source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-m", "input")
        self.base = self.fixture.text("-C", str(self.fixture.source), "rev-parse", "HEAD")
        self.fixture.run("-C", str(self.fixture.source), "push", str(self.fixture.remote), "main")
        self.manager = GitWorkspaceManager(self.root / "git-workspaces", self.root / "control", self.root / "publishing",
            protected_roots=(self.store.root,), approved_remotes=(str(self.fixture.remote),),
            git_executable=self.fixture.git, allow_local_remotes=True)
        self.checkout = self.manager.prepare(str(self.fixture.remote), "refs/heads/main", expected_commit=self.base)
        self.workspace = self.checkout.workspace
        self.files = WorkspaceTools(self.workspace)
        self.configure()
        self.loop.source_commit = self.base
        self.git = GitPublisher(self.manager, str(self.fixture.remote))
        self.api = PublicationApi(self.git)
        self.publisher = PublicationCoordinator(self.loop, self.checkout, self.git, self.api, notes=NOTES)

    def approved(self):
        self.ready()
        self.assertEqual(self.finish()["status"], "ready_for_pr")

    def advance(self, branch):
        self.fixture.run("-C", str(self.fixture.source), "fetch", str(self.fixture.remote), "refs/heads/" + branch)
        self.fixture.run("-C", str(self.fixture.source), "switch", "--detach", "FETCH_HEAD")
        self.fixture.write("human.txt", "human change\n")
        self.fixture.run("-C", str(self.fixture.source), "add", "human.txt")
        self.fixture.run("-C", str(self.fixture.source), "-c", "user.name=Human", "-c", "user.email=human@example.invalid",
                         "commit", "-m", "human change")
        self.fixture.run("-C", str(self.fixture.source), "push", str(self.fixture.remote), "HEAD:refs/heads/" + branch)

    def test_full_task_publishes_exact_verified_tree_once_and_complete_document(self):
        self.approved()
        result = self.publisher.run(self.key)
        self.assertEqual(result["status"], "published")
        candidate = result["candidate"]
        self.assertEqual(self.fixture.text("--git-dir", str(self.fixture.remote), "rev-parse", "refs/heads/main"), self.base)
        self.assertEqual(self.fixture.text("--git-dir", str(self.fixture.remote), "rev-parse", candidate["commit"] + "^{tree}"), candidate["tree"])
        self.assertEqual(self.fixture.text("--git-dir", str(self.fixture.remote), "show", candidate["commit"] + ":input.txt"), "fixed")
        self.assertIn("Status: proposed", self.fixture.text("--git-dir", str(self.fixture.remote), "show",
                                                            candidate["commit"] + ":docs/adr/source-publication.md"))
        mode = self.fixture.text("--git-dir", str(self.fixture.remote), "ls-tree", candidate["commit"], "script.sh")
        self.assertTrue(mode.startswith("100755"))
        self.assertEqual(self.api.check_records[0]["head_sha"], candidate["commit"])
        self.assertEqual(self.api.check_records[0]["conclusion"], "success")
        body = result["document"]["body"]
        for heading in ("목적과 범위", "인수 조건", "최종 설계", "실제 변경", "검증 결과", "배포와 복구", "관련 지식"):
            self.assertIn(heading, body)
        self.assertNotIn("{{", body)
        self.assertIn("not executed", self.api.check_records[0]["output"]["summary"])
        self.assertEqual(self.publisher.run(self.key)["status"], "published")
        self.assertEqual((self.api.creates, self.api.check_creates), (1, 1))

    def test_push_response_loss_is_reconciled_after_restart_without_repush(self):
        self.approved()
        push = self.git.push
        calls = []
        def lost(*args, **kwargs):
            calls.append(True)
            push(*args, **kwargs)
            raise AgentError("EFFECT_UNKNOWN", "Lost push response.")
        self.git.push = lost
        self.assertEqual(self.publisher.run(self.key)["stage"], "push_pending")
        self.store.__exit__()
        self.store = FileJournal(self.root / "state").__enter__()
        self.files = WorkspaceTools.reopen(self.workspace)
        self.configure()
        self.loop.source_commit = self.base
        self.publisher = PublicationCoordinator(self.loop, self.checkout, self.git, self.api, notes=NOTES)
        self.assertEqual(self.publisher.run(self.key)["status"], "published")
        self.assertEqual(len(calls), 1)

    def test_pr_response_loss_reconciles_without_duplicate(self):
        self.approved()
        self.api.lose = "pull"
        self.assertEqual(self.publisher.run(self.key)["stage"], "pr_pending")
        self.assertEqual(self.publisher.run(self.key)["status"], "published")
        self.assertEqual(self.api.creates, 1)

    def test_check_response_loss_reconciles_exact_head_without_duplicate(self):
        self.approved()
        self.api.lose = "check"
        self.assertEqual(self.publisher.run(self.key)["stage"], "check_pending")
        self.assertEqual(self.publisher.run(self.key)["status"], "published")
        self.assertEqual(self.api.check_creates, 1)

    def test_unknown_unobserved_pr_effect_is_never_retried(self):
        self.approved()
        calls = []
        def unknown(*args):
            calls.append(True)
            raise AgentError("EFFECT_UNKNOWN", "Unknown PR effect.")
        self.api.create_pull = unknown
        self.publisher.run(self.key)
        self.assertEqual(self.publisher.run(self.key)["reason"], "PUBLISH_EFFECT_UNKNOWN")
        self.assertEqual(len(calls), 1)

    def test_human_body_edit_is_preserved_and_approvals_invalidated(self):
        self.approved()
        self.publisher.run(self.key)
        self.api.pull_records[0]["body"] += "\nHuman explanation"
        result = self.publisher.run(self.key)
        self.assertEqual(result["reason"], "PUBLISH_PR_CHANGED")
        self.assertTrue(self.api.pull_records[0]["body"].endswith("Human explanation"))
        self.assertIsNone(self.state()["requirement_approval"])
        self.assertEqual(self.api.check_records[0]["conclusion"], "action_required")

    def test_human_head_update_is_never_overwritten(self):
        self.approved()
        result = self.publisher.run(self.key)
        self.advance(result["branch"])
        moved = self.git.refs("main", result["branch"])["head"]
        self.assertEqual(self.publisher.run(self.key)["reason"], "PUBLISH_REF_CHANGED")
        self.assertEqual(self.git.refs("main", result["branch"])["head"], moved)
        self.assertIsNone(self.state()["requirement_approval"])

    def test_base_movement_invalidates_previous_check_and_approval(self):
        self.approved()
        self.publisher.run(self.key)
        self.advance("main")
        self.assertEqual(self.publisher.run(self.key)["reason"], "PUBLISH_BASE_CHANGED")
        self.assertEqual(self.api.check_records[0]["conclusion"], "action_required")

    def test_source_change_after_verification_prevents_any_remote_write(self):
        self.approved()
        (self.workspace.root / "input.txt").write_bytes(b"untested\n")
        self.assert_code("WORKSPACE_CHANGED", lambda: self.publisher.run(self.key))
        self.assertEqual(self.git.refs("main", self.git.branch(self.key))["head"], None)
        self.assertEqual(self.api.creates, 0)

    def test_existing_unowned_branch_is_not_adopted(self):
        self.approved()
        branch = self.git.branch(self.key)
        self.fixture.run("--git-dir", str(self.fixture.remote), "update-ref", "refs/heads/" + branch, self.base)
        self.assert_code("PUBLISH_REF_CHANGED", lambda: self.publisher.run(self.key))
        self.assertEqual(self.api.creates, 0)

    def test_concurrent_ref_creation_is_rejected_by_exact_empty_lease(self):
        self.approved()
        network = self.git._network
        def race(arguments, **kwargs):
            if arguments[0] == "push":
                self.fixture.run("--git-dir", str(self.fixture.remote), "update-ref", "refs/heads/" + self.git.branch(self.key), self.base)
            return network(arguments, **kwargs)
        self.git._network = race
        result = self.publisher.run(self.key)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.git.refs("main", result["branch"])["head"], self.base)
        self.assertEqual(self.api.creates, 0)

    def test_policy_change_before_publication_cannot_bypass_default_branch(self):
        self.approved()
        self.api.current_policy = replace(self.api.current_policy, default_branch=self.git.branch(self.key))
        self.assert_code("PUBLISH_DEFAULT_BRANCH", lambda: self.publisher.run(self.key))

    def test_stop_during_push_is_not_blocked_by_publisher_lock_and_prevents_pr(self):
        self.approved()
        push = self.git.push
        def stopped(*args, **kwargs):
            push(*args, **kwargs)
            thread = threading.Thread(target=lambda: self.comment("/aidlc stop"))
            thread.start()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "Publisher must release workflow lock during remote calls")
        self.git.push = stopped
        result = self.publisher.run(self.key)
        self.assertEqual(result["reason"], "AGENT_PAUSED")
        self.assertEqual(self.api.creates, 0)
        self.assertIsNotNone(self.git.refs("main", result["branch"])["head"])

    def test_candidate_build_crash_never_rebuilds_or_pushes_without_reconciliation(self):
        self.approved()
        build = self.git.build
        calls = []
        def interrupted(*args, **kwargs):
            calls.append(True)
            build(*args, **kwargs)
            raise KeyboardInterrupt()
        self.git.build = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.publisher.run(self.key)
        result = self.publisher.run(self.key)
        self.assertEqual(result["reason"], "PUBLISH_BUILD_RECOVERY_REQUIRED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.git.refs("main", result["branch"])["head"], None)

    def test_exit_code_only_evidence_is_neutral_not_test_success(self):
        self.raw["project"]["command_overrides"]["unit_test"]["report_patterns"] = []
        self.configure()
        self.loop.source_commit = self.base
        self.publisher = PublicationCoordinator(self.loop, self.checkout, self.git, self.api, notes=NOTES)
        self.approved()
        self.assertEqual(self.publisher.run(self.key)["status"], "published")
        self.assertEqual(self.api.check_records[0]["conclusion"], "neutral")
        self.assertIn("Tests verified: false", self.api.check_records[0]["output"]["summary"])

    def test_https_requires_installed_network_adapter_before_git_dispatch(self):
        remote = "https://github.internal/example/repo.git"
        self.manager.approved_remotes = self.manager.approved_remotes | {remote}
        publisher = GitPublisher(self.manager, remote)
        self.assert_code("PUBLISH_NETWORK_UNCONFIGURED", lambda: publisher.refs("main", publisher.branch(self.key)))

    def test_closed_pr_is_not_recreated(self):
        self.approved()
        self.publisher.run(self.key)
        self.api.pull_records[0]["state"] = "closed"
        self.assertEqual(self.publisher.run(self.key)["reason"], "PUBLISH_PR_CHANGED")
        self.assertEqual(self.api.creates, 1)

    def test_transient_read_failure_can_reconfirm_complete_without_writes(self):
        self.approved()
        self.publisher.run(self.key)
        policy = self.api.policy
        def unavailable(*args):
            raise AgentError("GITHUB_UNAVAILABLE", "Temporary read failure.")
        self.api.policy = unavailable
        self.assertEqual(self.publisher.run(self.key)["status"], "blocked")
        self.api.policy = policy
        self.assertEqual(self.publisher.run(self.key)["status"], "published")
        self.assertEqual((self.api.creates, self.api.check_creates), (1, 1))
