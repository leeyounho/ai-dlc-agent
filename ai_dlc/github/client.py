"""GitHub REST observations that always reread current remote facts."""

from dataclasses import dataclass
import re
from urllib.parse import quote, urlsplit

from ..errors import AgentError
from ..storage import TaskKey
from ..validation import integer
from ..workflow.observations import CommentObservation, IssueObservation
from .auth import GitHubAppAuthenticator
from .http import GitHubHttp


@dataclass(frozen=True)
class RepositoryBinding:
    instance_id: str
    repository_id: int
    full_name: str
    installation_id: int

    def __post_init__(self):
        integer(self.repository_id, "repository_id")
        integer(self.installation_id, "installation_id")
        if (type(self.instance_id) is not str or not self.instance_id
                or type(self.full_name) is not str
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.full_name)):
            raise AgentError("GITHUB_BINDING", "GitHub repository binding is invalid.")

    @property
    def path(self):
        owner, name = self.full_name.split("/", 1)
        return quote(owner, safe="") + "/" + quote(name, safe="")


class GitHubApiClient:
    def __init__(self, binding: RepositoryBinding, authenticator: GitHubAppAuthenticator,
                 http: GitHubHttp):
        self.binding = binding
        self.authenticator = authenticator
        self.http = http

    def _key(self, task: TaskKey):
        if ((task.instance_id, task.repository_id)
                != (self.binding.instance_id, self.binding.repository_id)):
            raise AgentError("TASK_REPOSITORY", "Task does not match the GitHub repository binding.")

    def _authorization(self):
        token = self.authenticator.installation_token(self.binding.installation_id,
                                                      self.binding.repository_id)
        return "Bearer " + token.token

    def _get(self, path, *, not_found=False):
        return self.http.request_json("GET", path, authorization=self._authorization(),
                                      not_found=not_found)

    def verify_repository_scope(self):
        repository = self._get(f"/repos/{self.binding.path}")
        try:
            if (type(repository["id"]) is not int or repository["id"] != self.binding.repository_id
                    or repository["full_name"] != self.binding.full_name):
                raise ValueError
            if repository.get("disabled") is True or repository.get("archived") is True:
                raise AgentError("GITHUB_REPOSITORY_DISABLED", "GitHub repository is disabled or read-only.")
        except (KeyError, TypeError, ValueError):
            raise AgentError("GITHUB_PROTOCOL", "GitHub repository identity response is invalid.") from None
        return repository

    def issue(self, task: TaskKey) -> IssueObservation:
        self._key(task)
        self.verify_repository_scope()
        document = self._get(f"/repos/{self.binding.path}/issues/{task.issue_number}")
        try:
            if "pull_request" in document:
                raise AgentError("GITHUB_ISSUE", "Pull requests cannot enter through the Issue intake path.")
            user = document["user"]
            if (document["number"] != task.issue_number or type(user["id"]) is not int):
                raise ValueError
            title = document["title"]
            body = document.get("body") or ""
            updated = document["updated_at"]
            state = document["state"]
            actor_type = user["type"]
            if (not all(type(value) is str for value in (title, body, updated, state, actor_type))
                    or state not in {"open", "closed"}):
                raise ValueError
        except AgentError:
            raise
        except (KeyError, TypeError, ValueError):
            raise AgentError("GITHUB_PROTOCOL", "GitHub Issue response is invalid.") from None
        return IssueObservation(task, user["id"], title, body, updated, state == "open", actor_type)

    def comment(self, task: TaskKey, comment_id: int) -> CommentObservation | None:
        self._key(task)
        integer(comment_id, "comment_id")
        self.verify_repository_scope()
        document = self._get(f"/repos/{self.binding.path}/issues/comments/{comment_id}", not_found=True)
        if document is None:
            return None
        try:
            user = document["user"]
            issue_url = urlsplit(document["issue_url"])
            issue_path = f"/repos/{self.binding.path}/issues/{task.issue_number}"
            body, created, updated, actor_type = (document["body"], document["created_at"],
                                                  document["updated_at"], user["type"])
            if (document["id"] != comment_id or issue_url.scheme != "https"
                    or not issue_url.netloc or issue_url.query or issue_url.fragment
                    or not issue_url.path.endswith(issue_path)
                    or type(user["id"]) is not int
                    or not all(type(value) is str for value in (body, created, updated, actor_type))):
                raise ValueError
        except (KeyError, TypeError, ValueError, IndexError):
            raise AgentError("GITHUB_PROTOCOL", "GitHub Issue comment response is invalid.") from None
        return CommentObservation(task, comment_id, user["id"], actor_type, body, created, updated)

    def permission(self, task: TaskKey, actor_id: int) -> str:
        self._key(task)
        integer(actor_id, "actor_id")
        self.verify_repository_scope()
        user = self._get(f"/user/{actor_id}")
        try:
            if (user["id"] != actor_id or type(user["login"]) is not str
                    or not user["login"] or len(user["login"]) > 255
                    or any(ord(char) < 32 or ord(char) == 127 for char in user["login"])):
                raise ValueError
            login = quote(user["login"], safe="")
        except (KeyError, TypeError, ValueError):
            raise AgentError("GITHUB_PROTOCOL", "GitHub user identity response is invalid.") from None
        access = self._get(f"/repos/{self.binding.path}/collaborators/{login}/permission",
                           not_found=True)
        if access is None:
            return "none"
        try:
            permission = access["permission"]
            if type(permission) is not str:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AgentError("GITHUB_PROTOCOL", "GitHub permission response is invalid.") from None
        return {"admin": "admin", "maintain": "maintain", "push": "write",
                "triage": "read", "pull": "read", "read": "read", "write": "write",
                "none": "none"}.get(permission, "none")
