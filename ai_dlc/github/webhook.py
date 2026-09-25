"""GitHub webhook authentication and bounded event normalization."""

from dataclasses import asdict, dataclass
import hashlib
import hmac
import re

from ..errors import AgentError
from ..validation import decode_json
from .inbox import InboxReceipt, WebhookInbox


MAX_WEBHOOK_BYTES = 1024 * 1024
_ACTIONS = {
    "issues": frozenset({"opened", "edited", "reopened", "closed"}),
    "issue_comment": frozenset({"created", "edited", "deleted"}),
    "installation": frozenset({"created", "deleted", "suspend", "unsuspend",
                               "new_permissions_accepted"}),
    "installation_repositories": frozenset({"added", "removed"}),
    "repository": frozenset({"archived", "unarchived", "edited", "renamed", "deleted", "transferred"}),
    "ping": frozenset({"ping"}),
}


@dataclass(frozen=True)
class WebhookMetadata:
    event: str
    action: str
    repository_id: int | None
    repository_full_name: str | None
    repository_ids: tuple[int, ...]
    installation_id: int | None
    issue_number: int | None
    comment_id: int | None

    def as_dict(self):
        value = asdict(self)
        value["repository_ids"] = list(self.repository_ids)
        return value


class GitHubWebhookReceiver:
    def __init__(self, inbox: WebhookInbox, webhook_secret: str):
        if (type(webhook_secret) is not str or not webhook_secret or len(webhook_secret) > 65536
                or any(ord(char) < 32 or ord(char) == 127 for char in webhook_secret)):
            raise AgentError("WEBHOOK_SECRET", "Webhook secret is unavailable or invalid.")
        self.inbox = inbox
        self._secret = webhook_secret.encode("utf-8")

    @classmethod
    def from_environment(cls, inbox, github, *, environment):
        value = environment.get(github.webhook_secret_env)
        if not isinstance(value, str):
            raise AgentError("WEBHOOK_SECRET", "Webhook secret is unavailable or invalid.")
        return cls(inbox, value)

    def receive(self, headers, raw_body: bytes) -> InboxReceipt:
        if type(raw_body) is not bytes or len(raw_body) > MAX_WEBHOOK_BYTES:
            raise AgentError("WEBHOOK_SIZE", "Webhook payload exceeds the permitted size.")
        try:
            normalized = {str(name).lower(): value for name, value in headers.items()}
            signature = normalized["x-hub-signature-256"]
            delivery_id = normalized["x-github-delivery"]
            event = normalized["x-github-event"]
            if not all(type(value) is str for value in (signature, delivery_id, event)):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise AgentError("WEBHOOK_HEADERS", "Required GitHub webhook headers are invalid.") from None
        expected = "sha256=" + hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature) or not hmac.compare_digest(signature, expected):
            raise AgentError("WEBHOOK_SIGNATURE", "GitHub webhook signature verification failed.")
        payload = decode_json(raw_body)
        action = "ping" if event == "ping" else payload.get("action")
        if event not in _ACTIONS or action not in _ACTIONS[event]:
            raise AgentError("WEBHOOK_UNSUPPORTED", "Webhook event or action is not supported.")
        metadata = self._metadata(event, action, payload)
        return self.inbox.accept(delivery_id, raw_body, metadata.as_dict())

    def _metadata(self, event, action, payload):
        repository = payload.get("repository")
        installation = payload.get("installation")
        repository_id = None
        full_name = None
        installation_id = None
        if repository is not None:
            if (type(repository) is not dict or type(repository.get("id")) is not int
                    or repository["id"] < 1 or type(repository.get("full_name")) is not str
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository["full_name"])):
                raise AgentError("WEBHOOK_PAYLOAD", "Webhook repository identity is invalid.")
            repository_id, full_name = repository["id"], repository["full_name"]
        if installation is not None:
            if type(installation) is not dict or type(installation.get("id")) is not int or installation["id"] < 1:
                raise AgentError("WEBHOOK_PAYLOAD", "Webhook installation identity is invalid.")
            installation_id = installation["id"]
        issue_number = None
        comment_id = None
        if event in {"issues", "issue_comment"}:
            issue = payload.get("issue")
            if (repository_id is None or installation_id is None or type(issue) is not dict
                    or type(issue.get("number")) is not int or issue["number"] < 1):
                raise AgentError("WEBHOOK_PAYLOAD", "Webhook Issue identity is invalid.")
            issue_number = issue["number"]
        if event == "issue_comment":
            comment = payload.get("comment")
            if type(comment) is not dict or type(comment.get("id")) is not int or comment["id"] < 1:
                raise AgentError("WEBHOOK_PAYLOAD", "Webhook comment identity is invalid.")
            comment_id = comment["id"]
        repository_ids = []
        for field in ("repositories_added", "repositories_removed", "repositories"):
            if field in payload:
                if type(payload[field]) is not list:
                    raise AgentError("WEBHOOK_PAYLOAD", "Webhook repository list is invalid.")
                for item in payload[field]:
                    if type(item) is not dict or type(item.get("id")) is not int or item["id"] < 1:
                        raise AgentError("WEBHOOK_PAYLOAD", "Webhook repository list is invalid.")
                    repository_ids.append(item["id"])
        if event == "repository" and (repository_id is None or installation_id is None):
            raise AgentError("WEBHOOK_PAYLOAD", "Webhook repository event lacks installation scope.")
        if event == "installation_repositories" and installation_id is None:
            raise AgentError("WEBHOOK_PAYLOAD", "Webhook repository scope event lacks installation identity.")
        if event == "installation" and installation_id is None:
            raise AgentError("WEBHOOK_PAYLOAD", "Webhook installation event lacks installation identity.")
        return WebhookMetadata(event, action, repository_id, full_name,
                               tuple(sorted(set(repository_ids))), installation_id,
                               issue_number, comment_id)
