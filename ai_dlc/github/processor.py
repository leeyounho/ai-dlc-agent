"""Turn durable webhook deliveries into workflow transitions using live API facts."""

from collections.abc import Callable, Mapping

from ..config.types import RepositoryConfig
from ..errors import AgentError
from ..storage import FileJournal, TaskKey
from ..workflow import WorkflowEngine
from .client import RepositoryBinding
from .inbox import InboxReceipt, WebhookInbox


_DETERMINISTIC_REJECTIONS = frozenset({
    "ALREADY_APPROVED", "ALREADY_STARTED", "APPROVAL_REQUIRED", "APPROVAL_REVOKED",
    "COMMAND_ACTOR", "COMMAND_EDITED", "COMMAND_FORBIDDEN", "COMMAND_IGNORED",
    "COMMAND_INVALID", "COMMAND_READ_ONLY", "DESIGN_AUTOMATIC", "GATE_CLOSED",
    "ISSUE_ACTOR", "ISSUE_CLOSED", "OVERRIDE_FORBIDDEN", "QUESTIONS_OPEN",
    "REVISION_STALE", "START_REQUIRED", "TASK_CANCELLING", "TASK_MISSING",
    "TASK_NOT_PAUSED", "TASK_TERMINAL",
})
_RECONCILE_AFTER_REJECTION = frozenset({
    "APPROVAL_REVOKED", "COMMAND_ACTOR", "COMMAND_EDITED", "COMMAND_FORBIDDEN",
})


class GitHubEventProcessor:
    def __init__(self, inbox: WebhookInbox, store: FileJournal,
                 repositories: Mapping[int, RepositoryConfig], *, gateway_factory: Callable,
                 scope_reconciler: Callable[..., dict]):
        self.inbox = inbox
        self.store = store
        self.repositories = dict(repositories)
        self.gateway_factory = gateway_factory
        self.scope_reconciler = scope_reconciler

    @staticmethod
    def _event_id(receipt: InboxReceipt, suffix: str):
        return "github-" + receipt.delivery_digest[:32] + "-" + suffix

    def _reconcile(self, engine, key, revision, receipt):
        try:
            commit = engine.reconcile(key, expected_revision=revision,
                                      event_id=self._event_id(receipt, "reconcile"))
            return commit.revision, None
        except AgentError as error:
            if error.code not in _DETERMINISTIC_REJECTIONS:
                raise
            return revision, error.code

    def process_pending(self) -> tuple[dict, ...]:
        return tuple(self.process(receipt) for receipt in self.inbox.pending())

    def process(self, receipt: InboxReceipt) -> dict:
        metadata = receipt.metadata
        event, action = metadata.get("event"), metadata.get("action")
        if event in {"installation", "installation_repositories", "repository", "ping"}:
            result = self.scope_reconciler(
                dict(metadata), event_id=self._event_id(receipt, "scope"))
            if type(result) is not dict:
                raise AgentError("WEBHOOK_PROCESSOR", "Scope reconciler returned an invalid result.")
            persisted = {"status": "scope_reconciled", "event": event, "action": action,
                         "result": result}
            self.inbox.mark_processed(receipt, persisted)
            return persisted
        if event not in {"issues", "issue_comment"}:
            raise AgentError("WEBHOOK_PROCESSOR", "Inbox contains an unsupported event type.")
        repository_id = metadata.get("repository_id")
        repository = self.repositories.get(repository_id)
        if repository is None:
            result = {"status": "ignored_unconfigured_repository", "event": event, "action": action}
            self.inbox.mark_processed(receipt, result)
            return result
        if not repository.enabled:
            result = {"status": "ignored_disabled_repository", "event": event, "action": action}
            self.inbox.mark_processed(receipt, result)
            return result
        binding = RepositoryBinding(repository.instance_id, repository.repository_id,
                                    metadata.get("repository_full_name"), metadata.get("installation_id"))
        issue_number = metadata.get("issue_number")
        if type(issue_number) is not int:
            raise AgentError("WEBHOOK_PROCESSOR", "Inbox event lacks an Issue identity.")
        key = TaskKey(repository.instance_id, repository.repository_id, issue_number)
        engine = WorkflowEngine(self.store, repository, self.gateway_factory(binding))

        state = self.store.read(key)
        revision = (state or {}).get("state_revision", 0)
        try:
            source = engine.capture_source(key, expected_revision=revision,
                                           event_id=self._event_id(receipt, "source"))
        except AgentError as error:
            if error.code not in _DETERMINISTIC_REJECTIONS:
                raise
            result = {"status": "source_rejected", "event": event, "action": action,
                      "task": key.as_dict(), "rejection": error.code}
            self.inbox.mark_processed(receipt, result)
            return result
        revision = source.revision
        status = "source_observed"
        rejection = None
        reconcile_rejection = None
        if event == "issues" and action != "closed":
            revision, reconcile_rejection = self._reconcile(
                engine, key, revision, receipt)
            status = ("source_and_approvals_reconciled" if reconcile_rejection is None
                      else "source_observed_reconcile_rejected")
        if event == "issue_comment":
            comment_id = metadata.get("comment_id")
            if type(comment_id) is not int:
                raise AgentError("WEBHOOK_PROCESSOR", "Inbox event lacks a comment identity.")
            if action == "created":
                try:
                    commit = engine.handle_comment(key, comment_id, expected_revision=revision)
                    revision, status = commit.revision, "command_applied"
                except AgentError as error:
                    if error.code not in _DETERMINISTIC_REJECTIONS:
                        raise
                    rejection = error.code
                    status = "command_rejected"
                    if error.code in _RECONCILE_AFTER_REJECTION:
                        current = self.store.read(key)
                        revision, reconcile_rejection = self._reconcile(
                            engine, key, current["state_revision"], receipt)
            else:
                revision, reconcile_rejection = self._reconcile(
                    engine, key, revision, receipt)
                status = ("approvals_reconciled" if reconcile_rejection is None
                          else "reconcile_rejected")
        result = {"status": status, "event": event, "action": action,
                  "task": key.as_dict(), "task_revision": revision}
        if rejection is not None:
            result["rejection"] = rejection
        if reconcile_rejection is not None:
            result["reconcile_rejection"] = reconcile_rejection
        self.inbox.mark_processed(receipt, result)
        return result
