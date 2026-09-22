from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests.support import temporary_directory

from ai_dlc.config.loader import (assert_separate_paths, load_connection, parse_connection,
                                  parse_repository)
from ai_dlc.config.network import authorize_url
from ai_dlc.errors import AgentError
from ai_dlc.evaluation.offline import OfflineNetworkGuard
from ai_dlc.models.router import DispatchGuard, ModelRegistry, ModelRouter
from ai_dlc.validation import local_path, read_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evaluation" / "suites" / "fixtures"


class ConfigAndRoutingTests(unittest.TestCase):
    def setUp(self):
        self.raw = read_json(FIXTURES / "connection.json")
        self.repo_raw = read_json(FIXTURES / "repository.json")
        self.env = {"EVAL_MODEL_TOKEN": "unit-test-only", "EVAL_ALPHA": "alpha", "EVAL_BETA": "beta", "EVAL_TEXT": "text"}

    def config(self, raw=None):
        return parse_connection(self.raw if raw is None else raw, base_dir=FIXTURES)

    def router(self):
        config = self.config()
        return ModelRouter(ModelRegistry(config)), parse_repository(self.repo_raw, connection=config)

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as error:
            callback()
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_shipped_profiles_load_offline(self):
        with OfflineNetworkGuard() as network:
            prod = load_connection(ROOT / "config" / "production.example.json")
            external = load_connection(ROOT / "config" / "test-external.example.json")
            self_profile = parse_repository(read_json(ROOT / "config" / "repository.self.example.json"), connection=prod)
        self.assertEqual(network.attempts, 0)
        self.assertEqual(set(prod.models), {"gemma4", "gpt-oss", "deepseek", "gauss"})
        self.assertEqual(set(external.models), {"gpt-test"})
        self.assertFalse(self_profile.enabled)
        self.assertEqual(self_profile.project.adapter, "command")
        assert_separate_paths([*prod.directories.values(), *external.directories.values()])

    def test_duplicate_keys_and_nonfinite_numbers_rejected(self):
        values = [(' {"secret":"do-not-print", "secret":"other"}', "CONFIG_DUPLICATE"),
                  ('{"value": NaN}', "CONFIG_VALUE"), ('{"value": Infinity}', "CONFIG_VALUE"),
                  ('{"secret":"do-not-print",}', "CONFIG_JSON"), ('[]', "CONFIG_TYPE")]
        with temporary_directory() as temp:
            path = Path(temp) / "input.json"
            for content, code in values:
                with self.subTest(code=code, content=content[:8]):
                    path.write_text(content, encoding="utf-8")
                    error = self.assert_code(code, lambda: read_json(path))
                    self.assertNotIn("do-not-print", str(error.as_dict()))

    def test_json_size_bound(self):
        with temporary_directory() as temp:
            path = Path(temp) / "large.json"
            path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
            self.assert_code("CONFIG_SIZE", lambda: read_json(path))

    def test_remote_filesystem_paths_refused_before_io(self):
        for path in (r"\\never-contact.invalid\share\config.json", "//never-contact.invalid/share",
                     r"\\?\C:\config.json", "file://never-contact.invalid/share", "https://never-contact.invalid/config"):
            with self.subTest(kind=path[:6]):
                self.assert_code("CONFIG_PATH", lambda: local_path(path))

    def test_strict_types_and_unknown_nested_fields(self):
        mutations = [
            (lambda c: c.update(schema_version=True), "CONFIG_VERSION"),
            (lambda c: c["network"].update(external_access="false"), "CONFIG_TYPE"),
            (lambda c: c["llm"]["providers"]["internal"].update(max_concurrent_requests=True), "CONFIG_TYPE"),
            (lambda c: c["llm"]["models"]["alpha"].update(context_window_tokens="8192"), "CONFIG_TYPE"),
            (lambda c: c["llm"]["models"]["alpha"]["capabilities"].update(imagined=True), "CONFIG_UNKNOWN_FIELD"),
            (lambda c: c["network"].update(allow_unknown=True), "CONFIG_UNKNOWN_FIELD"),
            (lambda c: c.update(model={}), "CONFIG_UNKNOWN_FIELD"),
            (lambda c: c["llm"].update(failure_policy="fallback"), "CONFIG_VALUE"),
        ]
        for mutate, code in mutations:
            with self.subTest(code=code):
                c = deepcopy(self.raw)
                mutate(c)
                self.assert_code(code, lambda: self.config(c))

    def test_unused_external_provider_is_rejected(self):
        p = deepcopy(self.raw["llm"]["providers"]["internal"])
        p.update(boundary="external", base_url="https://external.invalid/v1")
        self.raw["llm"]["providers"]["unused"] = p
        self.assert_code("NETWORK_DENIED", self.config)

    def test_hostname_case_duplicates_and_boundaries(self):
        self.raw["network"]["internal_hosts"].append("MODELS.INTERNAL.INVALID")
        self.assert_code("CONFIG_DUPLICATE", self.config)
        self.raw["network"]["internal_hosts"].pop()
        self.raw["profile"] = "test"
        self.raw["network"].update(external_access=True, external_hosts=["models.internal.invalid"])
        self.assert_code("NETWORK_BOUNDARY", self.config)

    def test_url_attacks_are_rejected_before_any_network_use(self):
        config = self.config()
        urls = ["http://models.internal.invalid/v1", "https://models.internal.invalid:8443/v1",
                "https://user:password@models.internal.invalid/v1", "https://models.internal.invalid/v1?token=hidden",
                "https://models.internal.invalid/v1#fragment", "https://models.internal.invalid:/v1",
                "https://models.internal.invalid\\@external.invalid", "https://models.internal.invalid/%0d%0aHeader",
                "https://models.internal.invalid./v1", "https://models.internal.invalid.attacker.invalid/v1",
                "https://external.invalid/v1", "https://models.internal.invalid/\nignored"]
        with OfflineNetworkGuard() as guard:
            for url in urls:
                with self.subTest(url=url):
                    with self.assertRaises(AgentError):
                        authorize_url(config.network, url)
        self.assertEqual(guard.attempts, 0)

    def test_https_exact_host_and_default_port_allowed(self):
        config = self.config()
        authorize_url(config.network, "https://MODELS.INTERNAL.INVALID:443/v1", "internal")
        self.assert_code("NETWORK_DENIED", lambda: authorize_url(config.network, "https://models.internal.invalid/v1", "external"))

    def test_internal_openai_adapter_is_not_classified_as_external(self):
        self.assertEqual(self.config().providers["internal"].adapter, "openai_chat_completions")

    def test_external_test_requires_both_explicit_flag_and_destination(self):
        self.raw["profile"] = "test"
        self.raw["network"]["external_access"] = True
        self.raw["llm"]["providers"]["internal"].update(boundary="external", base_url="https://external.invalid/v1")
        self.assert_code("NETWORK_DENIED", self.config)
        self.raw["network"]["external_hosts"] = ["external.invalid"]
        self.assertEqual(self.config().providers["internal"].boundary, "external")

    def test_auth_is_discriminated_and_headers_are_constrained(self):
        for auth, code in [({"type": "none", "token_env": "IGNORED"}, "CONFIG_UNKNOWN_FIELD"),
                           ({"type": "bearer"}, "CONFIG_MISSING_FIELD"),
                           ({"type": "bearer", "token_env": "secret-value!"}, "CONFIG_VALUE"),
                           ({"type": "header", "header_name": "Host", "value_env": "TOKEN"}, "CONFIG_HEADER")]:
            with self.subTest(auth_type=auth["type"]):
                c = deepcopy(self.raw)
                c["llm"]["providers"]["internal"]["auth"] = auth
                self.assert_code(code, lambda: self.config(c))

    def test_custom_adapter_requires_fixed_id(self):
        p = self.raw["llm"]["providers"]["internal"]
        p["adapter"] = "custom"
        self.assert_code("CONFIG_TYPE", self.config)
        p["adapter_id"] = "internal-v1"
        self.assertEqual(self.config().providers["internal"].adapter_key, "custom:internal-v1")

    def test_context_limits_and_maven_auth_pair(self):
        self.raw["llm"]["models"]["alpha"]["max_output_tokens"] = 8192
        self.assert_code("MODEL_LIMITS", self.config)
        self.raw["llm"]["models"]["alpha"]["max_output_tokens"] = 2048
        self.raw["maven"]["username_env"] = "MAVEN_USER"
        self.assert_code("CONFIG_MISSING_FIELD", self.config)

    def test_relative_paths_resolve_against_config_and_overlap_is_denied(self):
        config = self.config()
        self.assertEqual(config.directories["state"], (FIXTURES / "local-data/state").resolve())
        self.raw["storage"]["state_dir"] = "local-data/workspaces/subdirectory"
        self.assert_code("CONFIG_PATH_OVERLAP", self.config)
        self.assert_code("CONFIG_PATH_OVERLAP", lambda: assert_separate_paths([FIXTURES, FIXTURES / "child"]))

    def test_configs_are_immutable_and_digest_uses_content(self):
        config = self.config()
        with self.assertRaises(TypeError):
            config.models["another"] = config.models["alpha"]
        with self.assertRaises(TypeError):
            config.routing.by_purpose["design"] = "alpha"
        changed = deepcopy(self.raw)
        changed["llm"]["routing"]["default_model"] = "beta"
        self.assertNotEqual(config.digest, self.config(changed).digest)
        self.assertEqual(config.digest, self.config(json.loads(json.dumps(self.raw, sort_keys=True))).digest)

    def test_generic_commands_do_not_require_junit_or_a_language_name(self):
        self.repo_raw["project"].update(adapter="command", toolchain_id="approved-sdk", junit_report_patterns=[])
        profile = parse_repository(self.repo_raw, connection=self.config()).project
        self.assertEqual(profile.adapter, "command")
        self.assertEqual(profile.junit_report_patterns, ())
        self.assertEqual(dict(profile.commands), {})  # discovery is still pending, not execution-ready
        self.repo_raw["project"]["adapter"] = "java_maven"
        self.assert_code("CONFIG_TYPE", lambda: parse_repository(self.repo_raw, connection=self.config()))

    def test_command_profile_paths_and_arguments_remain_strict(self):
        self.repo_raw["project"].update(adapter="command", junit_report_patterns=[], command_overrides={
            "test": {"executable_id": "python", "argv": ["-m", "unittest"], "cwd": ".",
                     "timeout_seconds": 120, "report_patterns": []}})
        for bad in ("../outside", "C:/outside", "//never-contact.invalid/repo", ".git/hooks"):
            self.repo_raw["project"]["command_overrides"]["test"]["cwd"] = bad
            self.assert_code("CONFIG_PATH", lambda: parse_repository(self.repo_raw, connection=self.config()))
        command = self.repo_raw["project"]["command_overrides"]["test"]
        command["cwd"] = "."
        command["argv"] = "python -m unittest"
        self.assert_code("CONFIG_TYPE", lambda: parse_repository(self.repo_raw, connection=self.config()))

    def test_all_route_precedence_levels(self):
        router, repo = self.router()
        self.assertEqual(router.resolve("implementation", repo).model.id, "alpha")
        self.assertEqual(router.resolve("design", repo).model.id, "beta")
        self.repo_raw["model_routing"]["default_model"] = "alpha"
        router, repo = self.router()
        self.assertEqual(router.resolve("design", repo).route_source, "repository.default")
        self.repo_raw["model_routing"]["by_purpose"]["design"] = "beta"
        router, repo = self.router()
        self.assertEqual(router.resolve("design", repo).route_source, "repository.purpose")

    def test_repository_denial_does_not_choose_another_allowed_model(self):
        self.repo_raw["model_routing"]["allowed_models"] = ["alpha"]
        router, repo = self.router()
        self.assert_code("MODEL_NOT_ALLOWED", lambda: router.resolve("design", repo))

    def test_repository_unknown_fields_and_requirement_approval(self):
        config = self.config()
        self.repo_raw["workflow"]["requirement_approval_required"] = False
        self.assert_code("APPROVAL_REQUIRED", lambda: parse_repository(self.repo_raw, connection=config))
        self.repo_raw["workflow"]["requirement_approval_required"] = True
        self.repo_raw["workflow"]["assume_approval"] = True
        self.assert_code("CONFIG_UNKNOWN_FIELD", lambda: parse_repository(self.repo_raw, connection=config))

    def test_project_paths_must_remain_inside_workspace(self):
        config = self.config()
        for path in ("../secrets", "/etc/secret", "C:\\secrets", "C:relative", "docs/.git", "docs/../../secret"):
            with self.subTest(path=path):
                self.repo_raw["knowledge"]["adr_path_if_absent"] = path
                self.assert_code("CONFIG_PATH", lambda: parse_repository(self.repo_raw, connection=config))

    def test_disabled_repo_can_be_inspected_but_never_dispatched(self):
        self.repo_raw["enabled"] = False
        router, repo = self.router()
        self.assertEqual(router.resolve("design", repo).model.id, "beta")
        calls = []
        guard = DispatchGuard(router, {"openai_chat_completions": lambda selected: calls.append(selected)})
        self.assert_code("MODEL_DENIED", lambda: guard.dispatch("design", repo, environment=self.env))
        self.assertEqual(calls, [])

    def test_secret_and_model_name_prerequisites_do_not_leak_values(self):
        router, repo = self.router()
        self.env["EVAL_MODEL_TOKEN"] = "SECRET\r\nInjected"
        result = router.preflight("implementation", repo, environment=self.env, available_adapters=frozenset({"openai_chat_completions"}))
        self.assertEqual(result.status, "configuration_pending")
        self.assertIn("AUTH_VALUE_MISSING_OR_INVALID", result.reasons)
        self.assertNotIn("SECRET", str(result.as_dict()))

    def test_text_model_allowed_for_summary_not_coding(self):
        self.repo_raw["model_routing"]["default_model"] = "text-only"
        router, repo = self.router()
        calls = []
        guard = DispatchGuard(router, {"openai_chat_completions": lambda selected: calls.append(selected.model.id)})
        self.assert_code("MODEL_DENIED", lambda: guard.dispatch("implementation", repo, environment=self.env))
        guard.dispatch("summary", repo, environment=self.env)
        self.assertEqual(calls, ["text-only"])

    def test_adapter_failure_never_falls_back_or_prints_exception_payload(self):
        router, repo = self.router()
        calls = []

        def failing(selected):
            calls.append(selected.model.id)
            raise RuntimeError("SECRET-and-prompt-content")

        guard = DispatchGuard(router, {"openai_chat_completions": failing})
        error = self.assert_code("MODEL_ADAPTER_ERROR", lambda: guard.dispatch("implementation", repo, environment=self.env))
        self.assertEqual(calls, ["alpha"])
        self.assertNotIn("SECRET", str(error))

    def test_unknown_purpose_and_unavailable_adapter(self):
        router, repo = self.router()
        self.assert_code("MODEL_PURPOSE", lambda: router.resolve("approval", repo))
        guard = DispatchGuard(router, {})
        self.assert_code("MODEL_NOT_CONFIGURED", lambda: guard.dispatch("implementation", repo, environment=self.env))


if __name__ == "__main__":
    unittest.main()
