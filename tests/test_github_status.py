from dataclasses import replace
import unittest

from ai_dlc.errors import AgentError
from ai_dlc.github.client import GitHubApiClient, RepositoryBinding
from ai_dlc.github.http import GitHubHttp
from ai_dlc.github.status import GitHubStatusGateway
from ai_dlc.storage import TaskKey
from ai_dlc.transport import HttpResponse
from ai_dlc.workflow import CommentObservation
from tests.test_github import FakeAuthenticator, FakeHttp, FakeTransport


class StatusGatewayTests(unittest.TestCase):
    def setUp(self):
        self.key = TaskKey("internal", 1, 10)
        self.client = GitHubApiClient(RepositoryBinding("internal", 1, "example/repository", 2),
                                     FakeAuthenticator(), FakeHttp())
        self.scope_calls = 0
        def scope():
            self.scope_calls += 1
        self.client.verify_repository_scope = scope
        self.gateway = GitHubStatusGateway(self.client, bot_actor_id=7)

    def test_pagination_finds_owned_bot_only_and_does_not_duplicate_second_page(self):
        ordinary = {"id": 1, "user": {"id": 7, "type": "User"}, "body": "<!-- marker -->"}
        bot = {"id": 200, "user": {"id": 7, "type": "Bot"}, "body": "<!-- marker -->\nstatus"}
        self.client.http = FakeHttp([ordinary] * 100, [bot])
        self.assertEqual(self.gateway.find(self.key, "<!-- marker -->"), [{"id": 200, "body": bot["body"]}])
        self.assertEqual([c[2]["page"] for c in self.client.http.calls], [1, 2])
        self.assertEqual(self.scope_calls, 1)

    def test_update_requires_current_owned_body(self):
        observation = CommentObservation(self.key, 200, 7, "Bot", "before", "v1", "v2")
        self.client.comment = lambda key, cid: observation
        self.client.http = FakeHttp({"id": 200})
        self.gateway.update(self.key, 200, "after", expected_body="before")
        self.assertEqual(self.client.http.calls[0][0], "PATCH")
        observation = replace(observation, body="human edit")
        with self.assertRaises(AgentError) as raised:
            self.gateway.update(self.key, 200, "after", expected_body="before")
        self.assertEqual(raised.exception.code, "STATUS_COMMENT_EDITED")
        self.assertEqual(len(self.client.http.calls), 1)

    def test_http_pagination_is_bounded_read_only_and_preserves_api_prefix(self):
        transport = FakeTransport(HttpResponse(200, {}, b"[]"))
        http = GitHubHttp("https://github.internal/api/v3", transport)
        http.request_json("GET", "/repos/example/repository/issues/10/comments", authorization="Bearer fixture",
                          response_type="array", page=2)
        self.assertEqual(transport.requests[0].url,
                         "https://github.internal/api/v3/repos/example/repository/issues/10/comments?per_page=100&page=2")
        for method, page in (("POST", 2), ("GET", 101), ("GET", True)):
            with self.assertRaises(AgentError):
                http.request_json(method, "/repos/example/repository/issues/10/comments", authorization="fixture", page=page)
        self.assertEqual(len(transport.requests), 1)


if __name__ == "__main__":
    unittest.main()
