from dataclasses import replace
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_dlc.config import load_service
from ai_dlc.config.loader import parse_connection, parse_repository
from ai_dlc.errors import AgentError
from ai_dlc.github.auth import (GitHubAppAuthenticator, InstallationToken, RsaSha256Signer,
                                _SHA256_DIGEST_INFO)
from ai_dlc.github.client import GitHubApiClient, RepositoryBinding
from ai_dlc.github.endpoint import GitHubWebhookEndpoint
from ai_dlc.github.http import GitHubHttp
from ai_dlc.github.inbox import WebhookInbox
from ai_dlc.github.processor import GitHubEventProcessor
from ai_dlc.github.webhook import GitHubWebhookReceiver, MAX_WEBHOOK_BYTES
from ai_dlc.storage import FileJournal, TaskKey
from ai_dlc.transport import HttpResponse
from ai_dlc.validation import read_json
from ai_dlc.workflow import CommentObservation, IssueObservation, WorkflowEngine
from tests.support import temporary_directory


ROOT = Path(__file__).resolve().parents[1]
CERTIFICATES = ROOT / "tests" / "fixtures"
WORKFLOW_FIXTURES = ROOT / "evaluation" / "suites" / "fixtures"
REQUIREMENTS = {"summary": "Observe current GitHub facts", "scope": ["Webhook processing"],
                "acceptance_criteria": ["Never trust mutable webhook fields"],
                "open_questions": []}


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request_json(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


class FakeAuthenticator:
    def __init__(self, token="INSTALLATION-SECRET"):
        self.token = token
        self.calls = []

    def installation_token(self, installation_id, repository_id):
        self.calls.append((installation_id, repository_id))
        return InstallationToken(self.token, datetime(2030, 1, 1, tzinfo=timezone.utc),
                                 installation_id, repository_id)


class GitHubAuthTests(unittest.TestCase):
    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as error:
            callback()
        self.assertEqual(error.exception.code, code)
        return error.exception

    def github(self):
        bundle = load_service(ROOT / "config" / "service.example.json")
        return replace(bundle.service.github,
                       private_key_file=CERTIFICATES / "localhost-key.pem")

    @staticmethod
    def decode_part(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def test_app_jwt_has_bounded_claims_and_valid_rs256_signature(self):
        signer = RsaSha256Signer.from_file(CERTIFICATES / "localhost-key.pem")
        auth = GitHubAppAuthenticator(self.github(), FakeHttp(), app_id=12345, signer=signer,
                                      clock=lambda: 1_800_000_000)
        token = auth.app_jwt()
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        self.assertEqual(json.loads(self.decode_part(encoded_header)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(self.decode_part(encoded_payload)),
                         {"exp": 1_800_000_540, "iat": 1_799_999_940, "iss": 12345})
        signature = self.decode_part(encoded_signature)
        recovered = pow(int.from_bytes(signature, "big"), signer.public_exponent,
                        signer.modulus).to_bytes(len(signature), "big")
        digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(
            (encoded_header + "." + encoded_payload).encode("ascii")).digest()
        self.assertTrue(recovered.startswith(b"\x00\x01\xff"))
        self.assertEqual(recovered[-len(digest_info) - 1:], b"\x00" + digest_info)
        self.assertNotIn("PRIVATE", repr(auth))

    def test_installation_token_is_repository_scoped_cached_and_secret_safe(self):
        http = FakeHttp({"token": "INSTALLATION-SECRET", "expires_at": "2027-01-15T09:00:00Z"})
        signer = RsaSha256Signer.from_file(CERTIFICATES / "localhost-key.pem")
        auth = GitHubAppAuthenticator(self.github(), http, app_id=123, signer=signer,
                                      clock=lambda: 1_800_000_000)
        first = auth.installation_token(77, 1001)
        second = auth.installation_token(77, 1001)
        self.assertIs(first, second)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][2]["body"], {"repository_ids": [1001]})
        self.assertNotIn("INSTALLATION-SECRET", repr(first))

    def test_missing_or_unsupported_private_key_fails_without_payload(self):
        github = replace(self.github(), private_key_file=ROOT / "missing-private-key.pem")
        error = self.assert_code("GITHUB_KEY", lambda: GitHubAppAuthenticator.from_environment(
            github, FakeHttp(), environment={github.app_id_env: "123"}))
        self.assertNotIn(str(github.private_key_file), str(error.as_dict()))
        self.assert_code("GITHUB_AUTH", lambda: GitHubAppAuthenticator.from_environment(
            self.github(), FakeHttp(), environment={self.github().app_id_env: "secret-value"}))


class GitHubClientTests(unittest.TestCase):
    def setUp(self):
        self.binding = RepositoryBinding("corp-ghes", 1001, "example/project", 77)
        self.key = TaskKey("corp-ghes", 1001, 10)
        self.repo = {"id": 1001, "full_name": "example/project", "disabled": False, "archived": False}

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as error:
            callback()
        self.assertEqual(error.exception.code, code)

    def test_issue_comment_and_current_permission_are_reread(self):
        issue = {"number": 10, "title": "Original", "body": "Body", "updated_at": "2026-01-01T00:00:00Z",
                 "state": "open", "user": {"id": 5, "type": "User"}}
        comment = {"id": 90, "body": "/aidlc stop", "created_at": "2026-01-02T00:00:00Z",
                   "updated_at": "2026-01-02T00:00:00Z", "issue_url": "https://host/api/v3/repos/example/project/issues/10",
                   "user": {"id": 7, "type": "User"}}
        user = {"id": 7, "login": "operator"}
        permission = {"permission": "push"}
        http = FakeHttp(self.repo, issue, self.repo, comment, self.repo, user, permission)
        auth = FakeAuthenticator()
        client = GitHubApiClient(self.binding, auth, http)
        observed_issue = client.issue(self.key)
        observed_comment = client.comment(self.key, 90)
        observed_permission = client.permission(self.key, 7)
        self.assertEqual(observed_issue.body, "Body")
        self.assertEqual(observed_comment.actor_id, 7)
        self.assertEqual(observed_permission, "write")
        self.assertEqual(len(auth.calls), 7)
        self.assertTrue(all(call == (77, 1001) for call in auth.calls))

    def test_deleted_comment_and_scope_changes_fail_closed(self):
        client = GitHubApiClient(self.binding, FakeAuthenticator(), FakeHttp(self.repo, None))
        self.assertIsNone(client.comment(self.key, 90))
        disabled = {**self.repo, "archived": True}
        denied = GitHubApiClient(self.binding, FakeAuthenticator(), FakeHttp(disabled))
        self.assert_code("GITHUB_REPOSITORY_DISABLED", lambda: denied.issue(self.key))
        mismatch = GitHubApiClient(self.binding, FakeAuthenticator(), FakeHttp({**self.repo, "id": 999}))
        self.assert_code("GITHUB_PROTOCOL", lambda: mismatch.issue(self.key))

    def test_cross_repository_comment_reference_and_unknown_issue_state_are_rejected(self):
        wrong_comment = {"id": 90, "body": "/aidlc stop",
                         "created_at": "2026-01-02T00:00:00Z",
                         "updated_at": "2026-01-02T00:00:00Z",
                         "issue_url": "https://host/api/v3/repos/other/project/issues/10",
                         "user": {"id": 7, "type": "User"}}
        comment_client = GitHubApiClient(
            self.binding, FakeAuthenticator(), FakeHttp(self.repo, wrong_comment))
        self.assert_code("GITHUB_PROTOCOL", lambda: comment_client.comment(self.key, 90))
        bad_issue = {"number": 10, "title": "Title", "body": "", "updated_at": "v1",
                     "state": "unknown", "user": {"id": 5, "type": "User"}}
        issue_client = GitHubApiClient(self.binding, FakeAuthenticator(), FakeHttp(self.repo, bad_issue))
        self.assert_code("GITHUB_PROTOCOL", lambda: issue_client.issue(self.key))


class GitHubHttpTests(unittest.TestCase):
    def test_api_version_is_explicit_and_capability_errors_are_classified(self):
        transport = FakeTransport(HttpResponse(200, {}, b'{"ok":true}'),
                                  HttpResponse(422, {}, b'{"message":"secret"}'))
        http = GitHubHttp("https://github.internal.example/api/v3", transport,
                          api_version="2022-11-28")
        self.assertEqual(http.request_json("GET", "/app", authorization="Bearer SECRET"),
                         {"ok": True})
        request = transport.requests[0]
        self.assertEqual(request.url, "https://github.internal.example/api/v3/app")
        self.assertEqual(request.headers["X-GitHub-Api-Version"], "2022-11-28")
        with self.assertRaises(AgentError) as caught:
            http.request_json("POST", "/unsupported", authorization="Bearer SECRET", body={})
        self.assertEqual(caught.exception.code, "GITHUB_CAPABILITY_UNAVAILABLE")
        self.assertNotIn("secret", str(caught.exception.as_dict()).lower())


class WebhookInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.store = FileJournal(self.root).__enter__()
        self.addCleanup(self.store.__exit__, None, None, None)
        self.inbox = WebhookInbox(self.store)
        self.receiver = GitHubWebhookReceiver(self.inbox, "WEBHOOK-SECRET")
        self.endpoint = GitHubWebhookEndpoint(self.receiver)

    def payload(self, *, action="created"):
        return {"action": action, "installation": {"id": 77},
                "repository": {"id": 1001, "full_name": "example/project"},
                "issue": {"number": 10}, "comment": {"id": 90}}

    def delivery(self, payload=None, *, delivery="delivery-1", event="issue_comment", secret="WEBHOOK-SECRET"):
        raw = json.dumps(payload or self.payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        headers = {"X-Hub-Signature-256": signature, "X-GitHub-Delivery": delivery,
                   "X-GitHub-Event": event}
        return headers, raw

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as error:
            callback()
        self.assertEqual(error.exception.code, code)

    def test_signed_raw_delivery_is_durable_deduplicated_and_processed_once(self):
        headers, raw = self.delivery()
        first = self.receiver.receive(headers, raw)
        second = self.receiver.receive(headers, raw)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.raw_body, raw)
        self.assertEqual(first.metadata["comment_id"], 90)
        self.assertEqual(len(self.inbox.pending()), 1)
        self.assertTrue(self.inbox.mark_processed(first, {"status": "handled", "task_revision": 4}))
        self.assertFalse(self.inbox.mark_processed(first, {"status": "handled", "task_revision": 4}))
        self.assertEqual(self.inbox.pending(), ())
        self.assert_code("WEBHOOK_COLLISION", lambda: self.inbox.mark_processed(
            first, {"status": "different"}))

    def test_tamper_size_unknown_action_and_delivery_collision_fail_before_effect(self):
        headers, raw = self.delivery()
        tampered = raw + b" "
        self.assert_code("WEBHOOK_SIGNATURE", lambda: self.receiver.receive(headers, tampered))
        self.assertEqual(self.inbox.pending(), ())
        self.assert_code("WEBHOOK_SIZE", lambda: self.receiver.receive(headers, b"x" * (MAX_WEBHOOK_BYTES + 1)))
        unsupported_headers, unsupported = self.delivery({**self.payload(), "action": "labeled"})
        self.assert_code("WEBHOOK_UNSUPPORTED",
                         lambda: self.receiver.receive(unsupported_headers, unsupported))
        accepted = self.receiver.receive(*self.delivery())
        changed_headers, changed = self.delivery({**self.payload(), "comment": {"id": 91}})
        self.assert_code("WEBHOOK_COLLISION", lambda: self.receiver.receive(changed_headers, changed))
        self.assertEqual(accepted.delivery_id, "delivery-1")

    def test_response_loss_after_commit_recovers_as_duplicate(self):
        headers, raw = self.delivery()
        from ai_dlc.github import inbox as inbox_module
        original = inbox_module._publish

        def publish_then_lose(path, value, **kwargs):
            original(path, value, **kwargs)
            raise OSError("simulated response loss")

        with patch.object(inbox_module, "_publish", side_effect=publish_then_lose):
            self.assert_code("INBOX_IO", lambda: self.receiver.receive(headers, raw))
        self.store.__exit__()
        self.store = FileJournal(self.root).__enter__()
        self.addCleanup(self.store.__exit__, None, None, None)
        self.inbox = WebhookInbox(self.store)
        self.receiver = GitHubWebhookReceiver(self.inbox, "WEBHOOK-SECRET")
        recovered = self.receiver.receive(headers, raw)
        self.assertTrue(recovered.duplicate)
        self.assertEqual(len(self.inbox.pending()), 1)

    def test_http_endpoint_returns_only_after_commit_and_maps_safe_errors(self):
        headers, raw = self.delivery()
        accepted = self.endpoint.handle("POST", "/hooks/github", headers, raw)
        self.assertEqual(accepted.status, 202)
        self.assertEqual(json.loads(accepted.body),
                         {"accepted": True, "delivery_id": "delivery-1", "duplicate": False})
        self.assertEqual(len(self.inbox.pending()), 1)

        tampered = self.endpoint.handle("POST", "/hooks/github", headers, raw + b" ")
        self.assertEqual((tampered.status, json.loads(tampered.body)["error"]),
                         (401, "WEBHOOK_SIGNATURE"))
        self.assertEqual(self.endpoint.handle("GET", "/hooks/github", {}, b"").status, 405)
        self.assertEqual(self.endpoint.handle("POST", "/other", {}, b"").status, 404)


class LiveGateway:
    def __init__(self, task):
        self.current_issue = IssueObservation(task, 1, "Current title", "Current body", "v1")
        self.comments = {}
        self.permissions = {1: "write"}

    def issue(self, task):
        return self.current_issue

    def comment(self, task, comment_id):
        return self.comments.get(comment_id)

    def permission(self, task, actor_id):
        return self.permissions.get(actor_id, "none")


class GitHubEventProcessorTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.store = FileJournal(self.root).__enter__()
        self.addCleanup(self.store.__exit__, None, None, None)
        self.inbox = WebhookInbox(self.store)
        self.receiver = GitHubWebhookReceiver(self.inbox, "WEBHOOK-SECRET")
        connection = parse_connection(read_json(WORKFLOW_FIXTURES / "connection.json"),
                                      base_dir=WORKFLOW_FIXTURES)
        self.repo = parse_repository(read_json(WORKFLOW_FIXTURES / "repository.json"),
                                     connection=connection)
        self.key = TaskKey(self.repo.instance_id, self.repo.repository_id, 10)
        self.gateway = LiveGateway(self.key)
        self.bindings = []
        self.scope_events = []
        self.processor = GitHubEventProcessor(
            self.inbox, self.store, {self.repo.repository_id: self.repo},
            gateway_factory=self.gateway_for,
            scope_reconciler=self.reconcile_scope,
        )

    def gateway_for(self, binding):
        self.bindings.append(binding)
        return self.gateway

    def reconcile_scope(self, metadata, *, event_id):
        self.scope_events.append((metadata, event_id))
        return {"affected_repository_ids": metadata["repository_ids"]}

    def receive(self, event, action, *, delivery, comment_id=None, extra=None):
        payload = {"action": action, "installation": {"id": 77},
                   "repository": {"id": self.repo.repository_id,
                                  "full_name": "fixture/java-example"}}
        if event in {"issues", "issue_comment"}:
            payload["issue"] = {"number": self.key.issue_number, "title": "STALE PAYLOAD"}
        if event == "issue_comment":
            payload["comment"] = {"id": comment_id, "body": "/aidlc cancel",
                                  "user": {"id": 999, "type": "Bot"}}
        if extra:
            payload.update(extra)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = "sha256=" + hmac.new(b"WEBHOOK-SECRET", raw, hashlib.sha256).hexdigest()
        return self.receiver.receive({"X-Hub-Signature-256": signature,
                                      "X-GitHub-Delivery": delivery,
                                      "X-GitHub-Event": event}, raw)

    def prepare_requirements(self):
        receipt = self.receive("issues", "opened", delivery="issue-opened")
        self.assertEqual(self.processor.process(receipt)["status"],
                         "source_and_approvals_reconciled")
        state = self.store.read(self.key)
        engine = WorkflowEngine(self.store, self.repo, self.gateway)
        engine.normalize(self.key, REQUIREMENTS, expected_revision=state["state_revision"],
                         event_id="requirements-ready")

    def command(self, comment_id, body, *, delivery=None):
        self.gateway.comments[comment_id] = CommentObservation(
            self.key, comment_id, 1, "User", body,
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        return self.processor.process(self.receive(
            "issue_comment", "created", delivery=delivery or f"command-{comment_id}",
            comment_id=comment_id))

    def test_created_comment_uses_live_body_actor_and_permission_not_payload(self):
        self.prepare_requirements()
        self.gateway.comments[90] = CommentObservation(
            self.key, 90, 1, "User", "/aidlc approve requirements req-0001",
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        receipt = self.receive("issue_comment", "created", delivery="comment-created",
                               comment_id=90)
        result = self.processor.process(receipt)
        state = self.store.read(self.key)
        self.assertEqual(result["status"], "command_applied")
        self.assertEqual(state["requirement_approval"]["actor_id"], 1)
        self.assertEqual(state["requirement_approval"]["comment_id"], 90)
        source = self.store.blob(self.key, state["source_digest"])
        self.assertEqual((source["title"], source["body"]), ("Current title", "Current body"))
        self.assertEqual(self.bindings[-1].installation_id, 77)
        self.assertEqual(self.inbox.pending(), ())

    def test_bot_and_edited_commands_are_durable_rejections(self):
        self.prepare_requirements()
        for comment_id, actor_type, updated_at, expected in (
            (91, "Bot", "2026-01-01T00:00:00Z", "COMMAND_ACTOR"),
            (92, "User", "2026-01-02T00:00:00Z", "COMMAND_EDITED"),
        ):
            self.gateway.comments[comment_id] = CommentObservation(
                self.key, comment_id, 1, actor_type, "/aidlc approve requirements req-0001",
                "2026-01-01T00:00:00Z", updated_at)
            receipt = self.receive("issue_comment", "created",
                                   delivery=f"rejected-{comment_id}", comment_id=comment_id)
            result = self.processor.process(receipt)
            self.assertEqual((result["status"], result["rejection"]),
                             ("command_rejected", expected))
        self.assertIsNone(self.store.read(self.key)["requirement_approval"])
        self.assertEqual(self.inbox.pending(), ())

    def test_all_control_commands_follow_live_observations(self):
        self.prepare_requirements()
        self.assertEqual(self.command(
            90, "/aidlc approve requirements req-0001")["status"], "command_applied")
        self.assertEqual(self.command(91, "/aidlc start req-0001")["status"], "command_applied")
        self.assertTrue(self.store.read(self.key)["started"])
        self.command(92, "/aidlc stop")
        self.assertEqual(self.store.read(self.key)["status"], "paused")
        self.command(93, "/aidlc resume")
        self.assertNotEqual(self.store.read(self.key)["status"], "paused")
        self.command(94, "/aidlc cancel")
        self.assertEqual(self.store.read(self.key)["status"], "cancelled")

        deleted = self.receive("issue_comment", "deleted", delivery="after-cancel-delete",
                               comment_id=90)
        result = self.processor.process(deleted)
        self.assertEqual((result["status"], result["rejection"]),
                         ("source_rejected", "TASK_TERMINAL"))
        self.assertEqual(self.inbox.pending(), ())

    def test_permission_lookup_failure_stays_pending_then_retries_fail_closed(self):
        self.prepare_requirements()
        self.gateway.comments[90] = CommentObservation(
            self.key, 90, 1, "User", "/aidlc approve requirements req-0001",
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        receipt = self.receive("issue_comment", "created", delivery="permission-unavailable",
                               comment_id=90)
        with patch.object(self.gateway, "permission",
                          side_effect=AgentError("GITHUB_UNAVAILABLE", "Unavailable.")):
            with self.assertRaises(AgentError) as caught:
                self.processor.process(receipt)
        self.assertEqual(caught.exception.code, "GITHUB_UNAVAILABLE")
        self.assertIsNone(self.store.read(self.key)["requirement_approval"])
        self.assertEqual(len(self.inbox.pending()), 1)
        self.assertEqual(self.processor.process_pending()[0]["status"], "command_applied")
        self.assertIsNotNone(self.store.read(self.key)["requirement_approval"])

    def test_processing_result_loss_replays_without_duplicate_workflow_effect(self):
        self.prepare_requirements()
        self.gateway.comments[90] = CommentObservation(
            self.key, 90, 1, "User", "/aidlc approve requirements req-0001",
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        receipt = self.receive("issue_comment", "created", delivery="processor-response-loss",
                               comment_id=90)
        with patch.object(self.inbox, "mark_processed",
                          side_effect=AgentError("INBOX_IO", "Response lost.")):
            with self.assertRaises(AgentError):
                self.processor.process(receipt)
        revision = self.store.read(self.key)["state_revision"]
        self.assertIsNotNone(self.store.read(self.key)["requirement_approval"])
        self.assertEqual(self.processor.process_pending()[0]["status"], "command_applied")
        self.assertEqual(self.store.read(self.key)["state_revision"], revision)
        self.assertEqual(self.inbox.pending(), ())

    def test_edit_or_delete_reconciles_existing_approval_and_redelivery_is_idempotent(self):
        self.prepare_requirements()
        original = CommentObservation(
            self.key, 90, 1, "User", "/aidlc approve requirements req-0001",
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        self.gateway.comments[90] = original
        created = self.receive("issue_comment", "created", delivery="approval", comment_id=90)
        self.processor.process(created)
        self.assertIsNotNone(self.store.read(self.key)["requirement_approval"])

        self.gateway.comments[90] = replace(original, updated_at="2026-01-02T00:00:00Z")
        edited = self.receive("issue_comment", "edited", delivery="approval-edited", comment_id=90)
        result = self.processor.process(edited)
        self.assertEqual(result["status"], "approvals_reconciled")
        self.assertIsNone(self.store.read(self.key)["requirement_approval"])
        revision = self.store.read(self.key)["state_revision"]
        duplicate = self.receiver.receive(
            {"X-Hub-Signature-256": "sha256=" + hmac.new(
                b"WEBHOOK-SECRET", edited.raw_body, hashlib.sha256).hexdigest(),
             "X-GitHub-Delivery": "approval-edited", "X-GitHub-Event": "issue_comment"},
            edited.raw_body)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(self.processor.process_pending(), ())
        self.assertEqual(self.store.read(self.key)["state_revision"], revision)

        self.gateway.comments.pop(90)
        deleted = self.receive("issue_comment", "deleted", delivery="approval-deleted", comment_id=90)
        self.assertEqual(self.processor.process(deleted)["status"], "approvals_reconciled")

    def test_out_of_order_created_event_cannot_restore_edited_approval(self):
        self.prepare_requirements()
        self.gateway.comments[90] = CommentObservation(
            self.key, 90, 1, "User", "/aidlc approve requirements req-0001",
            "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")
        edited = self.receive("issue_comment", "edited", delivery="edited-first", comment_id=90)
        created = self.receive("issue_comment", "created", delivery="created-late", comment_id=90)
        self.processor.process(edited)
        result = self.processor.process(created)
        self.assertEqual(result["rejection"], "COMMAND_EDITED")
        self.assertIsNone(self.store.read(self.key)["requirement_approval"])
        self.assertEqual(self.inbox.pending(), ())

    def test_scope_changes_are_delegated_and_persisted(self):
        receipt = self.receive(
            "installation_repositories", "removed", delivery="scope-removed",
            extra={"repositories_removed": [{"id": self.repo.repository_id}]})
        result = self.processor.process(receipt)
        self.assertEqual(result["status"], "scope_reconciled")
        metadata, event_id = self.scope_events[0]
        self.assertEqual(metadata["repository_ids"], [self.repo.repository_id])
        self.assertTrue(event_id.startswith("github-"))
        self.assertEqual(self.inbox.pending(), ())

        installed = self.receive("installation", "created", delivery="installation-created",
                                 extra={"repositories": [{"id": self.repo.repository_id}]})
        self.assertEqual(self.processor.process(installed)["status"], "scope_reconciled")

    def test_issue_event_reconciles_current_permission_revocation(self):
        self.prepare_requirements()
        self.gateway.comments[90] = CommentObservation(
            self.key, 90, 1, "User", "/aidlc approve requirements req-0001",
            "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        self.processor.process(self.receive(
            "issue_comment", "created", delivery="permission-approval", comment_id=90))
        self.assertIsNotNone(self.store.read(self.key)["requirement_approval"])
        self.gateway.permissions[1] = "read"
        result = self.processor.process(self.receive(
            "issues", "edited", delivery="permission-recheck"))
        self.assertEqual(result["status"], "source_and_approvals_reconciled")
        self.assertIsNone(self.store.read(self.key)["requirement_approval"])

    def test_bot_authored_issue_is_rejected_once_without_creating_task(self):
        self.gateway.current_issue = replace(self.gateway.current_issue, author_type="Bot")
        receipt = self.receive("issues", "opened", delivery="bot-issue")
        result = self.processor.process(receipt)
        self.assertEqual((result["status"], result["rejection"]),
                         ("source_rejected", "ISSUE_ACTOR"))
        self.assertIsNone(self.store.read(self.key))
        self.assertEqual(self.inbox.pending(), ())


if __name__ == "__main__":
    unittest.main()
