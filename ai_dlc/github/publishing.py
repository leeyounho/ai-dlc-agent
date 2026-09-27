"""GitHub PR/check effects over the same App-scoped policy transport."""

from ..models.types import require
from ..validation import integer


class GitHubPublicationGateway:
    def __init__(self, client, *, bot_actor_id, app_id):
        integer(bot_actor_id, "bot_actor_id")
        integer(app_id, "app_id")
        self.client, self.bot_actor_id, self.app_id = client, bot_actor_id, app_id

    def policy(self, key):
        self.client._key(key)
        return self.client.repository_policy()

    def _call(self, key, method, path, **kwargs):
        self.client._key(key)
        self.client.verify_repository_scope()
        return self.client.http.request_json(method, f"/repos/{self.client.binding.path}" + path,
                                            authorization=self.client._authorization(), **kwargs)

    def pulls(self, key, marker, branch):
        found = []
        for page in range(1, 101):
            documents = self._call(key, "GET", "/pulls", page=page, state="all", response_type="array")
            for item in documents:
                require(type(item) is dict, "PUBLISH_API_PROTOCOL")
                head, base = item.get("head") or {}, item.get("base") or {}
                body = item.get("body") or ""
                if not (type(body) is str and (body.startswith(marker) or head.get("ref") == branch)):
                    continue
                integer(item.get("number"), "pr.number")
                require((base.get("repo") or {}).get("id") == key.repository_id
                        and (head.get("repo") or {}).get("id") == key.repository_id, "PUBLISH_PR_REPOSITORY")
                user = item.get("user") or {}
                found.append({"number": item["number"], "body": body, "title": item.get("title"),
                    "head": head.get("sha"), "branch": head.get("ref"), "base": base.get("ref"),
                    "state": item.get("state"), "url": item.get("html_url"),
                    "owned": user.get("id") == self.bot_actor_id and user.get("type") == "Bot"})
            if len(documents) < 100:
                return found
        require(False, "PUBLISH_PAGINATION_LIMIT")

    def create_pull(self, key, document):
        return self._call(key, "POST", "/pulls", body={"title": document["title"], "body": document["body"],
            "head": document["branch"], "base": document["base"], "draft": True})

    def checks(self, key, head, external_id):
        from ..execution.git_workspace import GitRevision
        GitRevision(head, head)
        found = []
        for page in range(1, 101):
            document = self._call(key, "GET", f"/commits/{head}/check-runs", page=page)
            items = document.get("check_runs")
            require(type(items) is list, "PUBLISH_API_PROTOCOL")
            for item in items:
                if item.get("external_id") == external_id:
                    require((item.get("app") or {}).get("id") == self.app_id, "PUBLISH_CHECK_OWNER")
                    integer(item.get("id"), "check.id")
                    found.append({k: item.get(k) for k in ("id", "name", "head_sha", "external_id", "status", "conclusion", "output")})
            if len(items) < 100:
                return found
        require(False, "PUBLISH_PAGINATION_LIMIT")

    def create_check(self, key, document):
        return self._call(key, "POST", "/check-runs", body=document)

    def invalidate_check(self, key, check_id):
        integer(check_id, "check.id")
        return self._call(key, "PATCH", f"/check-runs/{check_id}",
                          body={"status": "completed", "conclusion": "action_required"})
