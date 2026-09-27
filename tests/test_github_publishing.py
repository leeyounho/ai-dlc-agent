from copy import deepcopy
import unittest

from ai_dlc.errors import AgentError
from ai_dlc.github.client import GitHubApiClient, RepositoryBinding
from ai_dlc.github.publishing import GitHubPublicationGateway
from ai_dlc.storage import TaskKey
from tests.test_github import FakeAuthenticator, FakeHttp


class GitHubPublicationTests(unittest.TestCase):
    def setUp(self):
        self.key = TaskKey("internal", 1, 10)
        self.client = GitHubApiClient(RepositoryBinding("internal", 1, "example/repo", 2), FakeAuthenticator(), FakeHttp())
        self.scopes = []
        self.client.verify_repository_scope = lambda: self.scopes.append(True)
        self.gateway = GitHubPublicationGateway(self.client, bot_actor_id=7, app_id=8)

    def test_all_state_pagination_finds_closed_pr_and_retains_manual_body(self):
        irrelevant = {"number": 1, "head": {"ref": "other"}, "body": "human"}
        pull = {"number": 15, "body": "<!-- marker -->\nhuman edit", "title": "Title", "state": "closed",
                "head": {"ref": "aidlc/task", "sha": "a" * 40, "repo": {"id": 1}},
                "base": {"ref": "main", "repo": {"id": 1}}, "user": {"id": 7, "type": "Bot"},
                "html_url": "https://github.example/example/repo/pull/15"}
        self.client.http = FakeHttp([irrelevant] * 100, [pull])
        result = self.gateway.pulls(self.key, "<!-- marker -->", "aidlc/task")
        self.assertEqual(result[0]["state"], "closed")
        self.assertEqual(result[0]["body"], pull["body"])
        self.assertEqual([c[2]["state"] for c in self.client.http.calls], ["all", "all"])
        self.assertEqual(len(self.scopes), 2)

    def test_cross_repository_branch_cannot_be_adopted(self):
        self.client.http = FakeHttp([{"number": 15, "body": "marker", "head": {"ref": "task", "repo": {"id": 9}},
                                     "base": {"repo": {"id": 1}}}])
        with self.assertRaises(AgentError) as raised:
            self.gateway.pulls(self.key, "marker", "task")
        self.assertEqual(raised.exception.code, "PUBLISH_PR_REPOSITORY")

    def test_create_is_draft_and_uses_scoped_api(self):
        self.client.http = FakeHttp({"number": 15})
        document = {"title": "Final change", "body": "Full design and evidence", "branch": "aidlc/task", "base": "main"}
        self.gateway.create_pull(self.key, document)
        method, path, options = self.client.http.calls[0]
        self.assertEqual((method, path), ("POST", "/repos/example/repo/pulls"))
        self.assertTrue(options["body"]["draft"])
        self.assertEqual(options["body"]["head"], "aidlc/task")

    def test_check_requires_own_app_and_exact_head_query(self):
        check = {"id": 50, "app": {"id": 8}, "name": "AI-DLC", "head_sha": "a" * 40,
                 "external_id": "task-evidence", "status": "completed", "conclusion": "success", "output": {}}
        wrong = deepcopy(check)
        wrong["app"]["id"] = 9
        self.client.http = FakeHttp({"check_runs": [check]}, {"check_runs": [wrong]})
        self.assertEqual(self.gateway.checks(self.key, "a" * 40, "task-evidence")[0]["id"], 50)
        self.assertIn("/commits/" + "a" * 40, self.client.http.calls[0][1])
        with self.assertRaises(AgentError) as raised:
            self.gateway.checks(self.key, "a" * 40, "task-evidence")
        self.assertEqual(raised.exception.code, "PUBLISH_CHECK_OWNER")


if __name__ == "__main__":
    unittest.main()
