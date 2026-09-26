"""Bot-owned Issue status comments with current identity/scope observations."""

from ..errors import AgentError
from ..validation import integer


class GitHubStatusGateway:
    def __init__(self, client, *, bot_actor_id):
        integer(bot_actor_id, "bot_actor_id")
        self.client, self.bot_actor_id = client, bot_actor_id

    def find(self, key, marker):
        self.client._key(key)
        self.client.verify_repository_scope()
        found = []
        for page in range(1, 101):
            comments = self.client.http.request_json("GET",
                f"/repos/{self.client.binding.path}/issues/{key.issue_number}/comments",
                authorization=self.client._authorization(), response_type="array", page=page)
            for document in comments:
                user = document.get("user") or {}
                body = document.get("body")
                if (type(body) is str and body.startswith(marker) and user.get("type") == "Bot"
                        and user.get("id") == self.bot_actor_id):
                    integer(document.get("id"), "comment_id")
                    found.append({"id": document["id"], "body": body})
            if len(comments) < 100:
                return found
        raise AgentError("STATUS_PAGINATION_LIMIT", "Comment scan is incomplete; publishing is blocked.")

    def create(self, key, body):
        self.client._key(key)
        self.client.verify_repository_scope()
        return self.client.http.request_json("POST", f"/repos/{self.client.binding.path}/issues/{key.issue_number}/comments",
                                            authorization=self.client._authorization(), body={"body": body})

    def update(self, key, comment_id, body, *, expected_body):
        observed = self.client.comment(key, comment_id)
        if (observed is None or observed.actor_type != "Bot" or observed.actor_id != self.bot_actor_id
                or observed.body != expected_body):
            raise AgentError("STATUS_COMMENT_EDITED", "Changed or non-owned status comments cannot be overwritten.")
        return self.client.http.request_json("PATCH", f"/repos/{self.client.binding.path}/issues/comments/{comment_id}",
                                            authorization=self.client._authorization(), body={"body": body})
