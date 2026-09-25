from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_dlc.config.types import CommandProfile
from ai_dlc.errors import AgentError
from ai_dlc.execution.installed_runner import HostObservation, InstalledRunner, NativeHelperClient
from ai_dlc.execution.ports import ExecutionPlan
from ai_dlc.execution.runtime_profiles import parse_runtime_profiles
from ai_dlc.validation import read_json


DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
ROOT = Path(__file__).resolve().parents[1]


def profile_documents():
    runner = {
        "schema_version": 1,
        "profile_id": "installed-runners",
        "workspace_root": "/var/lib/ai-dlc/workspaces",
        "protected_roots": ["/opt/ai-dlc", "/var/lib/ai-dlc/control"],
        "platforms": {
            "rhel9-x86_64": {
                "os_id": "rhel", "os_major": 9, "architecture": "x86_64",
                "process_isolation": "cgroup_v2", "network_enforcement": "nftables_cgroup",
                "filesystem_enforcement": "posix_uid",
                "helper_path": "/opt/ai-dlc/bin/runner-helper", "helper_sha256": DIGEST,
                "verification": "verified", "evidence_sha256": OTHER_DIGEST,
                "verified_at": "2026-09-25T00:00:00Z",
            },
        },
        "slots": [
            {"id": "slot-1", "uid": 21001, "gid": 21000,
             "home_root": "/var/lib/ai-dlc/homes/slot-1", "egress_profile_id": "runner-offline"},
        ],
        "resources": {
            "small": {"cpu_seconds": 60, "memory_bytes": 268435456, "processes": 32,
                      "file_bytes": 67108864, "output_bytes": 65536, "disk_bytes": 536870912},
        },
    }
    toolchains = {
        "schema_version": 1, "profile_id": "installed-toolchains",
        "toolchains": {
            "python-approved": {
                "platform_ids": ["rhel9-x86_64"],
                "egress_profile_ids": ["runner-offline"],
                "executables": {
                    "python": {"path": "/opt/ai-dlc/runtime/python/bin/python3",
                               "sha256": DIGEST, "version": "3.12.11"},
                },
                "commands": {
                    "unit_test": {"executable_id": "python", "argv": ["-m", "unittest"],
                                  "cwd": ".", "timeout_seconds": 60,
                                  "resource_profile_id": "small", "verification": "exit_code",
                                  "report_patterns": [], "generated_patterns": []},
                },
            },
            "java8-maven-approved": {
                "platform_ids": ["rhel9-x86_64"],
                "egress_profile_ids": ["runner-offline"],
                "executables": {
                    "maven": {"path": "/opt/ai-dlc/runtime/maven/bin/mvn",
                              "sha256": DIGEST, "version": "3.9.11"},
                },
                "commands": {
                    "unit_test": {"executable_id": "maven", "argv": ["-B", "test"],
                                  "cwd": ".", "timeout_seconds": 300,
                                  "resource_profile_id": "small", "verification": "junit",
                                  "report_patterns": ["**/target/surefire-reports/TEST-*.xml"],
                                  "generated_patterns": ["**/target/**"]},
                },
                "maven": {"settings_file": "/opt/ai-dlc/runtime/maven/settings.xml",
                          "settings_sha256": DIGEST,
                          "truststore_file": "/opt/ai-dlc/runtime/java/lib/security/cacerts",
                          "truststore_sha256": DIGEST, "cache_mode": "per_run",
                          "credential_profile_id": "maven-readonly"},
            },
        },
    }
    egress = {
        "schema_version": 1, "profile_id": "installed-egress",
        "profiles": {
            "runner-offline": {"mode": "production", "subject": "runner", "default_policy": "deny",
                               "dns_mode": "disabled", "proxy_mode": "none",
                               "enforcement": "verified", "evidence_sha256": DIGEST,
                               "routes": []},
        },
    }
    return runner, toolchains, egress


class FakeWorkspace:
    id = "ws-test"
    root = "/var/lib/ai-dlc/workspaces/ws-test/tree"
    digest = OTHER_DIGEST
    files = ()

    def __init__(self):
        self.verifications = 0

    def verify(self, **_kwargs):
        self.verifications += 1


class FakeHost:
    def __init__(self, observation=None):
        self.observation = observation or HostObservation("rhel", 9, "x86_64", "cgroup_v2")

    def observe(self):
        return self.observation


class FakeVerifier:
    def __init__(self):
        self.calls = []

    def verify(self, path, digest, *, executable):
        self.calls.append((path, digest, executable))


class FakeHelper:
    def __init__(self):
        self.calls = []
        self.identities = {}
        self.results = {}

    def request(self, platform, operation, payload):
        self.calls.append((platform.id, operation, deepcopy(payload)))
        if operation == "preflight":
            return {"ready": True, "runtime_digest": payload["runtime_digest"]}
        if operation == "start":
            run_id = payload["run_id"]
            if run_id in self.identities:
                raise AgentError("RUN_COLLISION", "already running")
            identity = {
                "run_id": run_id, "token": "c" * 64, "platform_id": platform.id,
                "slot_id": "slot-1", "uid": 21001, "pid": 1234, "start_ticks": 555,
                "cgroup": "aidlc/slot-1/run", "runtime_digest": payload["runtime_digest"],
                "workspace_digest": payload["workspace_digest"], "resource_profile_id": "small",
                "egress_profile_id": "runner-offline",
            }
            self.identities[run_id] = identity
            return {"state": "running", "identity": deepcopy(identity)}
        if operation == "inspect":
            identity = self.identities.get(payload["run_id"])
            if identity is None:
                return {"state": "unknown"}
            result = self.results.get(payload["run_id"])
            if result is None:
                return {"state": "running"}
            return {"state": "finished", "identity": deepcopy(identity), "result": deepcopy(result)}
        if operation == "cancel":
            return {"accepted": True}
        raise AssertionError(operation)


def finished_result(**changes):
    result = {"exit_code": 0, "termination": "completed", "stdout_sha256": DIGEST,
              "stderr_sha256": DIGEST, "stdout_bytes": 10, "stderr_bytes": 0,
              "process_tree_stopped": True, "residual_processes": 0, "uid_reusable": True}
    result.update(changes)
    return result


class InstalledRunnerTests(unittest.TestCase):
    def setUp(self):
        self.runner_raw, self.toolchain_raw, self.egress_raw = profile_documents()
        self.profiles = parse_runtime_profiles(self.runner_raw, self.toolchain_raw, self.egress_raw)
        self.workspace = FakeWorkspace()
        self.command = CommandProfile("python", ("-m", "unittest"), ".", 30, ())
        self.plan = ExecutionPlan("unit_test", "python-approved", self.command,
                                  self.workspace, "exit_code")
        self.helper, self.verifier = FakeHelper(), FakeVerifier()

    def installed(self, profiles=None, host=None):
        return InstalledRunner(profiles or self.profiles, helper=self.helper,
                               host_probe=host or FakeHost(), verifier=self.verifier)

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)

    def test_profiles_bind_rhel_uid_resources_toolchain_and_default_deny_egress(self):
        self.assertEqual("deny", self.profiles.egress["runner-offline"].default_policy)
        self.assertEqual(21001, self.profiles.runner_pool.slots[0].uid)
        self.assertEqual("per_run", self.profiles.toolchains["java8-maven-approved"].maven.cache_mode)
        self.assertEqual("maven-readonly",
                         self.profiles.toolchains["java8-maven-approved"].maven.credential_profile_id)
        raw = deepcopy(self.runner_raw)
        raw["platforms"]["rhel9-x86_64"]["os_major"] = 10
        self.assert_code("CONFIG_VALUE", lambda: parse_runtime_profiles(
            raw, self.toolchain_raw, self.egress_raw))
        raw = deepcopy(self.runner_raw)
        raw["platforms"]["rhel9-x86_64"]["network_enforcement"] = "iptables_owner"
        self.assert_code("CONFIG_VALUE", lambda: parse_runtime_profiles(
            raw, self.toolchain_raw, self.egress_raw))
        raw = deepcopy(self.runner_raw)
        raw["platforms"]["rhel9-x86_64"]["helper_sha256"] = "0" * 64
        self.assert_code("CONFIG_VALUE", lambda: parse_runtime_profiles(
            raw, self.toolchain_raw, self.egress_raw))

    def test_shipped_rhel_matrix_is_explicitly_unverified(self):
        profiles = parse_runtime_profiles(
            read_json(ROOT / "runtime" / "runner-pool.example.json"),
            read_json(ROOT / "runtime" / "toolchains.example.json"),
            read_json(ROOT / "runtime" / "egress.example.json"),
        )
        self.assertEqual({7, 8, 9}, {item.os_major for item in profiles.runner_pool.platforms.values()})
        self.assertTrue(all(item.verification == "unverified"
                            for item in profiles.runner_pool.platforms.values()))
        self.assertTrue(all(item.enforcement == "unverified" for item in profiles.egress.values()))

    def test_preflight_attests_exact_runtime_without_secrets_or_shell(self):
        digest = self.installed().preflight(self.plan)
        self.assertEqual(64, len(digest))
        payload = self.helper.calls[-1][2]
        self.assertEqual(("python", ["-m", "unittest"]),
                         (payload["executable_id"], payload["argv"]))
        self.assertFalse(any(name in str(payload).casefold()
                             for name in ("secret", "token_env", "app_key", "proxy")))
        self.assertGreaterEqual(self.workspace.verifications, 1)
        self.assertIn(("/opt/ai-dlc/bin/runner-helper", DIGEST, True), self.verifier.calls)

    def test_unverified_or_unsupported_platform_and_egress_fail_closed(self):
        raw = deepcopy(self.runner_raw)
        raw["platforms"]["rhel9-x86_64"].update(verification="unverified", verified_at=None)
        profiles = parse_runtime_profiles(raw, self.toolchain_raw, self.egress_raw)
        self.assert_code("RUNNER_PLATFORM_UNVERIFIED", lambda: self.installed(profiles).preflight(self.plan))
        self.assert_code("RUNNER_OS_UNSUPPORTED", lambda: self.installed(
            host=FakeHost(HostObservation("rhel", 8, "x86_64", "cgroup_v1"))).preflight(self.plan))
        egress = deepcopy(self.egress_raw)
        egress["profiles"]["runner-offline"]["enforcement"] = "unverified"
        profiles = parse_runtime_profiles(self.runner_raw, self.toolchain_raw, egress)
        self.assert_code("RUNNER_EGRESS_UNVERIFIED", lambda: self.installed(profiles).preflight(self.plan))

    def test_only_exact_installer_argv_timeout_and_workspace_are_accepted(self):
        changed = ExecutionPlan("unit_test", "python-approved",
            CommandProfile("python", ("-c", "print(1)"), ".", 30, ()), self.workspace, "exit_code")
        self.assert_code("RUNNER_COMMAND", lambda: self.installed().preflight(changed))
        changed = ExecutionPlan("unit_test", "python-approved",
            CommandProfile("python", ("-m", "unittest"), ".", 61, ()), self.workspace, "exit_code")
        self.assert_code("RUNNER_COMMAND", lambda: self.installed().preflight(changed))
        outside = FakeWorkspace()
        outside.root = "/var/lib/other-task/tree"
        changed = ExecutionPlan("unit_test", "python-approved", self.command, outside, "exit_code")
        self.assert_code("RUNNER_WORKSPACE", lambda: self.installed().preflight(changed))

    def test_restart_inspection_uses_full_identity_and_never_relaunches(self):
        first = self.installed()
        handle = first.start("run-one", self.plan)
        self.assertIsNone(handle.poll())
        self.helper.results["run-one"] = finished_result()
        replacement = self.installed()
        result = replacement.inspect("run-one", handle.identity)
        self.assertEqual((0, "completed", True),
                         (result.exit_code, result.termination, result.process_tree_stopped))
        self.assertEqual(1, sum(operation == "start" for _, operation, _ in self.helper.calls))
        bad = handle.identity
        bad["start_ticks"] += 1
        self.assert_code("RUNNER_PROTOCOL", lambda: replacement.inspect("run-one", bad))

    def test_cancel_is_only_a_request_and_residual_process_or_uid_quarantine_is_uncertain(self):
        handle = self.installed().start("run-two", self.plan)
        handle.cancel("timeout")
        self.assertIsNone(handle.poll())
        self.helper.results["run-two"] = finished_result(
            termination="timeout", process_tree_stopped=True, residual_processes=1,
            uid_reusable=False,
        )
        result = handle.poll()
        self.assertEqual("timeout", result.termination)
        self.assertFalse(result.process_tree_stopped)

    def test_resource_limit_outcome_is_explicit(self):
        handle = self.installed().start("run-resource", self.plan)
        self.helper.results["run-resource"] = finished_result(
            exit_code=-9, termination="resource_limit",
        )
        result = handle.poll()
        self.assertEqual("resource_limit", result.termination)
        self.assertTrue(result.process_tree_stopped)

    def test_profile_rejects_default_route_path_overlap_and_unknown_resource(self):
        egress = deepcopy(self.egress_raw)
        egress["profiles"]["runner-offline"]["routes"] = [{
            "scheme": "https", "host": "maven.internal.example", "port": 443,
            "address_ranges": ["0.0.0.0/0"], "purpose": "maven", "boundary": "internal",
        }]
        self.assert_code("CONFIG_VALUE", lambda: parse_runtime_profiles(
            self.runner_raw, self.toolchain_raw, egress))
        egress["profiles"]["runner-offline"]["routes"][0].update(
            address_ranges=["203.0.113.0/24"], boundary="test_external")
        self.assert_code("NETWORK_DENIED", lambda: parse_runtime_profiles(
            self.runner_raw, self.toolchain_raw, egress))
        runner = deepcopy(self.runner_raw)
        runner["protected_roots"] = ["/var/lib/ai-dlc"]
        self.assert_code("CONFIG_PATH_OVERLAP", lambda: parse_runtime_profiles(
            runner, self.toolchain_raw, self.egress_raw))
        toolchains = deepcopy(self.toolchain_raw)
        toolchains["toolchains"]["python-approved"]["commands"]["unit_test"]["resource_profile_id"] = "large"
        self.assert_code("CONFIG_REFERENCE", lambda: parse_runtime_profiles(
            self.runner_raw, toolchains, self.egress_raw))

    def test_native_helper_invocation_has_fixed_binary_argv_and_empty_secret_environment(self):
        platform = self.profiles.runner_pool.platforms["rhel9-x86_64"]
        verifier = FakeVerifier()

        class Process:
            returncode = 0

            def __init__(self, args, **kwargs):
                self.args, self.kwargs = args, kwargs

            def communicate(self, encoded, timeout):
                self.encoded, self.timeout = encoded, timeout
                response = {"protocol": 1, "ok": True, "result": {"ready": True}}
                return json.dumps(response).encode("utf-8"), b"ignored"

        created = []

        def factory(args, **kwargs):
            process = Process(args, **kwargs)
            created.append(process)
            return process

        with patch("ai_dlc.execution.installed_runner.subprocess.Popen", side_effect=factory):
            result = NativeHelperClient(verifier).request(platform, "preflight", {"profile_id": "safe"})
        self.assertEqual({"ready": True}, result)
        process = created[0]
        self.assertEqual(["/opt/ai-dlc/bin/runner-helper", "request-v1"], process.args)
        self.assertEqual({"LANG": "C", "LC_ALL": "C"}, process.kwargs["env"])
        self.assertFalse(process.kwargs["shell"])
        self.assertEqual("/", process.kwargs["cwd"])
        request = json.loads(process.encoded)
        self.assertEqual((1, "preflight"), (request["protocol"], request["operation"]))


if __name__ == "__main__":
    unittest.main()
