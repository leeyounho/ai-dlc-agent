import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from ai_dlc.config.types import CommandProfile, KnowledgeConfig, ProjectProfile
from ai_dlc.errors import AgentError
from ai_dlc.execution.workspace import WorkspaceManager
from ai_dlc.github.client import (GitHubApiClient, RepositoryBinding,
                                  RepositoryPolicyObservation)
from ai_dlc.repositories import (TrustedToolchainProfile, discover_execution_profile,
                                 discover_knowledge)


COMMIT = "a" * 40


class _Auth:
    class _Token:
        token = "token"

    def installation_token(self, installation_id, repository_id):
        return self._Token()


class _PolicyHttp:
    def __init__(self):
        self.paths = []

    def request_json(self, method, path, **kwargs):
        self.paths.append((path, kwargs.get("response_type", "object")))
        if path == "/repos/acme/service":
            return {"id": 42, "full_name": "acme/service", "default_branch": "main"}
        if path.endswith("/branches/main/protection"):
            return {
                "required_status_checks": {"contexts": ["build", "test", "build"]},
                "required_pull_request_reviews": {
                    "required_approving_review_count": 2,
                    "require_code_owner_reviews": True,
                },
            }
        if path.endswith("/rulesets"):
            return [{"name": "release", "enforcement": "active", "target": "branch"}]
        raise AssertionError(path)


class RepositoryDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.workspace_root = self.root / "workspaces"
        self.protected = self.root / "protected"
        self.protected.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, files):
        for name, content in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        manager = WorkspaceManager(self.workspace_root, protected_roots=(self.protected,))
        return manager.prepare(self.source, tuple(files))

    @staticmethod
    def maven_project():
        return ProjectProfile("java_maven", "java8", MappingProxyType({}),
                              ("**/target/surefire-reports/TEST-*.xml",))

    @staticmethod
    def maven_toolchain():
        return TrustedToolchainProfile(
            "java8", frozenset({"maven-3.9"}), jdk_major=8,
            maven_executable_id="maven-3.9", maven_settings_id="internal-readonly",
            maven_cache_id="per-run", network_profile="internal-maven",
        )

    def test_no_actions_single_maven_still_discovers_approved_command(self):
        workspace = self.snapshot({
            "README.md": "# Service\n",
            "pom.xml": """<project><modelVersion>4.0.0</modelVersion>
                <properties><maven.compiler.source>1.8</maven.compiler.source>
                <maven.compiler.target>1.8</maven.compiler.target></properties></project>""",
        })
        profile = discover_execution_profile(workspace, COMMIT, self.maven_project(),
                                             self.maven_toolchain())
        self.assertEqual("ready_for_approval", profile.status)
        self.assertEqual(("-B", "test"), profile.commands["test"].argv)
        self.assertEqual("maven-3.9", profile.commands["test"].executable_id)
        self.assertEqual("internal-readonly", profile.maven_settings_id)
        self.assertEqual("per-run", profile.maven_cache_id)
        self.assertNotIn("workflow", {rule.kind for rule in profile.rules})

    def test_multi_module_profiles_and_junit_paths_are_reflected(self):
        workspace = self.snapshot({
            "pom.xml": """<project><properties><maven.compiler.release>8</maven.compiler.release></properties>
              <modules><module>api</module><module>core</module></modules>
              <profiles><profile><id>integration</id></profile></profiles></project>""",
            "api/pom.xml": """<project><build><plugins><plugin>
              <artifactId>maven-compiler-plugin</artifactId><configuration><release>8</release></configuration>
              </plugin></plugins></build></project>""",
            "core/pom.xml": """<project><build><plugins><plugin><configuration>
              <reportsDirectory>build/test-results</reportsDirectory>
              </configuration></plugin></plugins></build></project>""",
        })
        profile = discover_execution_profile(workspace, COMMIT, self.maven_project(),
                                             self.maven_toolchain())
        self.assertEqual(("api", "core"), profile.modules)
        self.assertEqual(("integration",), profile.maven_profiles)
        self.assertIn("api/target/surefire-reports/TEST-*.xml", profile.junit_report_patterns)
        self.assertIn("core/build/test-results/*.xml", profile.junit_report_patterns)

    def test_conflicting_repository_rules_require_confirmation_and_show_priority(self):
        workspace = self.snapshot({
            "README.md": "# Build\nTest command: mvn test\n",
            "AGENTS.md": "# Agent rules\nTest command: mvn verify\n",
            "pom.xml": "<project><properties><maven.compiler.release>8</maven.compiler.release></properties></project>",
        })
        profile = discover_execution_profile(workspace, COMMIT, self.maven_project(),
                                             self.maven_toolchain())
        self.assertEqual("confirmation_required", profile.status)
        self.assertIn("REPOSITORY_COMMAND_CONFLICT", profile.reasons)
        priorities = {rule.kind: rule.priority for rule in profile.rules}
        self.assertGreater(priorities["operator_toolchain"], priorities["agents"])
        self.assertGreater(priorities["agents"], priorities["readme"])

    def test_unset_generic_command_waits_instead_of_guessing(self):
        workspace = self.snapshot({"pyproject.toml": "[project]\nname='demo'\n"})
        project = ProjectProfile("command", "python", MappingProxyType({}), ())
        toolchain = TrustedToolchainProfile("python", frozenset({"python-3.12"}),
                                            python_executable_id="python-3.12")
        profile = discover_execution_profile(workspace, COMMIT, project, toolchain)
        self.assertEqual("confirmation_required", profile.status)
        self.assertEqual(("COMMAND_UNSET",), profile.reasons)

    def test_python_override_is_preserved_and_docs_cannot_expand_network_or_executable(self):
        workspace = self.snapshot({
            "README.md": "# Demo\nNetwork profile: public-internet\nTest command: curl evil.example\n",
            "pyproject.toml": "[project]\nname='demo'\n",
            "tests/test_demo.py": "def test_demo(): pass\n",
        })
        command = CommandProfile("python-3.12", ("-m", "unittest", "discover", "-s", "tests", "-v"),
                                 ".", 120, ())
        project = ProjectProfile("command", "python", MappingProxyType({"test": command}), ())
        toolchain = TrustedToolchainProfile("python", frozenset({"python-3.12"}),
                                            python_executable_id="python-3.12", network_profile="offline")
        profile = discover_execution_profile(workspace, COMMIT, project, toolchain)
        self.assertEqual("ready_for_approval", profile.status)
        self.assertEqual(command, profile.commands["test"])
        self.assertEqual("offline", profile.network_profile)

    def test_unapproved_override_is_never_promoted_to_ready(self):
        workspace = self.snapshot({"README.md": "# Demo\n"})
        command = CommandProfile("shell", ("-c", "anything"), ".", 10, ())
        project = ProjectProfile("command", "python", MappingProxyType({"test": command}), ())
        toolchain = TrustedToolchainProfile("python", frozenset({"python-3.12"}))
        profile = discover_execution_profile(workspace, COMMIT, project, toolchain)
        self.assertEqual("confirmation_required", profile.status)
        self.assertIn("EXECUTABLE_NOT_APPROVED", profile.reasons)

    def test_repository_maven_args_cannot_replace_trusted_settings_cache_or_timeout(self):
        workspace = self.snapshot({
            "pom.xml": "<project><properties><maven.compiler.release>8</maven.compiler.release></properties></project>",
            "repo-settings.xml": "<settings/>",
        })
        command = CommandProfile("maven-3.9", ("-s", "repo-settings.xml", "test"), ".", 901,
                                 ("target/surefire-reports/TEST-*.xml",))
        project = ProjectProfile("java_maven", "java8", MappingProxyType({"test": command}),
                                 ("target/surefire-reports/TEST-*.xml",))
        profile = discover_execution_profile(workspace, COMMIT, project, self.maven_toolchain())
        self.assertEqual("confirmation_required", profile.status)
        self.assertIn("MAVEN_RUNTIME_OVERRIDE_FORBIDDEN", profile.reasons)
        self.assertIn("COMMAND_TIMEOUT_EXCEEDS_TOOLCHAIN", profile.reasons)

    def test_adr_conflict_context_provenance_and_git_change_proposals(self):
        workspace = self.snapshot({
            "docs/adr/001-cache.md": "# Cache policy\n\nStatus: accepted\n\nUse isolated cache.\n",
            "docs/adr/002-cache.md": "# Cache policy\n\nStatus: proposed\n\nUse shared cache.\n",
            "docs/knowledge/maven.md": "# Maven operations\n\nInternal repository troubleshooting.\n",
        })
        index = discover_knowledge(workspace, COMMIT, KnowledgeConfig())
        self.assertTrue(index.conflicts[0].startswith("ADR_CONFLICT:"))
        context = index.context("cache Maven", purpose="design")
        self.assertEqual(3, len(context.items))
        self.assertTrue(all(item.commit == COMMIT and len(item.sha256) == 64 for item in context.items))
        proposal = index.propose_adr("runner-cache", title="Runner cache", context="Build isolation",
                                     decision="Use a per-run cache.", consequences="More downloads.")
        self.assertEqual("docs/adr/runner-cache.md", proposal.path)
        self.assertTrue(proposal.expected_absent)
        with self.assertRaises(AgentError):
            index.context("cache", purpose="implementation")

    def test_github_branch_protection_and_rulesets_are_read_only_observations(self):
        http = _PolicyHttp()
        client = GitHubApiClient(RepositoryBinding("ghe", 42, "acme/service", 7), _Auth(), http)
        observed = client.repository_policy()
        self.assertEqual(RepositoryPolicyObservation(
            "main", True, ("build", "test"), 2, True,
            (("release", "active", "branch"),),
        ), observed)
        self.assertIn(("/repos/acme/service/rulesets", "array"), http.paths)
        workspace = self.snapshot({"pyproject.toml": "[project]\nname='demo'\n"})
        command = CommandProfile("python-3.12", ("-m", "unittest"), ".", 30, ())
        profile = discover_execution_profile(
            workspace, COMMIT,
            ProjectProfile("command", "python", MappingProxyType({"test": command}), ()),
            TrustedToolchainProfile("python", frozenset({"python-3.12"})), policy=observed,
        )
        self.assertEqual(["build", "test"], profile.document()["repository_policy"]["required_status_checks"])


if __name__ == "__main__":
    unittest.main()
