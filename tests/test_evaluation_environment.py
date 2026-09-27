from contextlib import ExitStack
from copy import deepcopy
import io
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

from ai_dlc.cli import main
from ai_dlc.errors import AgentError
from ai_dlc.evaluation.environment import FIELDS, collect_environment, compare_environments, report_environment
from ai_dlc.evaluation.runner import compare_reports, load_suite, read_report, render_report, run_suite, save_report
from tests.support import temporary_directory
from tests.test_evaluation_and_cli import ROOT


ENVIRONMENT = {"python_implementation": "cpython", "python_version": "3.12.8", "os_family": "Linux",
               "os_release": "5.14.0-503.el9", "machine_architecture": "x86_64"}


class RuntimeEnvironmentTests(unittest.TestCase):
    def report(self):
        with patch("ai_dlc.evaluation.runner.collect_environment", return_value=dict(ENVIRONMENT)):
            return run_suite(load_suite(ROOT / "evaluation/suites/core.json"))

    def test_capture_is_execution_time_metadata_in_json_and_markdown(self):
        report = self.report()
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["runtime_environment"], ENVIRONMENT)
        with temporary_directory() as root:
            saved = save_report(report, root)
            self.assertEqual(read_report(saved), report)
            text = (saved / "report.md").read_text(encoding="utf-8")
            self.assertIn("3.12.8", text)
            self.assertIn("5.14.0-503.el9", text)
        self.assertFalse(report["eligible_for_release"])

    def test_same_different_and_unknown_preserve_case_comparison(self):
        baseline = self.report()
        candidate = deepcopy(baseline)
        self.assertEqual(compare_reports(baseline, candidate)["environment"]["status"], "same")
        candidate["runtime_environment"]["python_version"] = "3.14.3"
        candidate["cases"][0]["status"] = "fail"
        result = compare_reports(baseline, candidate)
        self.assertEqual(result["environment"]["status"], "different")
        self.assertEqual(result["regressions"], [candidate["cases"][0]["case_id"]])
        self.assertEqual(result["environment"]["differences"]["python_version"], {"baseline": "3.12.8", "candidate": "3.14.3"})
        self.assertEqual(result["environment"]["performance_conclusion"], "not_evaluated")
        candidate["runtime_environment"]["python_version"] = None
        self.assertEqual(compare_reports(baseline, candidate)["environment"]["status"], "unknown")
        candidate["runtime_environment"]["os_family"] = "Windows"
        environment = compare_reports(baseline, candidate)["environment"]
        self.assertEqual(environment["status"], "different")
        self.assertEqual(environment["unknown_fields"], ["python_version"])
        self.assertFalse(result["eligible_for_release"])

    def test_legacy_read_and_compare_never_sample_current_host_or_rewrite_file(self):
        current = self.report()
        legacy = deepcopy(current)
        legacy["schema_version"] = 1
        del legacy["runtime_environment"]
        with temporary_directory() as root:
            path = root / "legacy.json"
            original = json.dumps(legacy).encode()
            path.write_bytes(original)
            with patch("ai_dlc.evaluation.runner.collect_environment", side_effect=AssertionError("No current host inference")):
                loaded = read_report(path)
                self.assertEqual(loaded, legacy)
                self.assertIn("unknown", render_report(loaded))
                result = compare_reports(loaded, current)
                self.assertEqual(result["environment"]["status"], "unknown")
                self.assertEqual(set(result["environment"]["unknown_fields"]), set(FIELDS))
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_versions_fields_types_and_path_values_are_rejected(self):
        report = self.report()
        changes = [lambda r: r.update(schema_version=True), lambda r: r.update(schema_version=3),
                   lambda r: r.pop("runtime_environment"), lambda r: r.update(runtime_environment=[]),
                   lambda r: r["runtime_environment"].update(hostname="private-host"),
                   lambda r: r["runtime_environment"].update(python_version=312),
                   lambda r: r["runtime_environment"].update(os_release="/private/system/path"),
                   lambda r: r["runtime_environment"].update(machine_architecture="x" * 129),
                   lambda r: r.update(schema_version=1)]
        with temporary_directory() as root:
            path = root / "bad.json"
            for change in changes:
                invalid = deepcopy(report)
                change(invalid)
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.subTest(change=change), self.assertRaises(AgentError):
                    read_report(path)

    def test_invalid_metadata_is_rejected_before_report_persistence(self):
        report = self.report()
        report["runtime_environment"]["username"] = "do-not-save"
        with temporary_directory() as root:
            with self.assertRaises(AgentError):
                save_report(report, root)
            self.assertEqual(list(root.iterdir()), [])

    def test_windows_collection_never_queries_identity_environment_or_full_paths(self):
        class ForbiddenEnvironment:
            def __getitem__(self, key):
                raise AssertionError("Environment access")
            def get(self, *args):
                raise AssertionError("Environment access")
        with ExitStack() as stack:
            for target in ("socket.gethostname", "getpass.getuser", "platform.uname", "platform.platform", "os.getenv"):
                stack.enter_context(patch(target, side_effect=AssertionError("Identity access")))
            stack.enter_context(patch("os.environ", ForbiddenEnvironment()))
            stack.enter_context(patch("sys.platform", "win32"))
            stack.enter_context(patch("sys.getwindowsversion", return_value=SimpleNamespace(major=10, minor=0, build=26100), create=True))
            stack.enter_context(patch("ai_dlc.evaluation.environment._windows_architecture", return_value="x86_64"))
            result = collect_environment()
        self.assertEqual(set(result), set(FIELDS))
        self.assertEqual(result["os_release"], "10.0.26100")
        self.assertEqual(result["python_version"], f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    def test_linux_reads_only_kernel_release_and_execution_architecture(self):
        opened = mock_open(read_data="5.14.0-503.el9\n")
        with patch("sys.platform", "linux"), patch("pathlib.Path.open", opened), \
                patch("ai_dlc.evaluation.environment._linux_architecture", return_value="aarch64"), \
                patch("platform.uname", side_effect=AssertionError("No hostname query")):
            result = collect_environment()
        self.assertEqual(result["os_release"], "5.14.0-503.el9")
        self.assertEqual(result["machine_architecture"], "aarch64")
        opened.assert_called_once_with("r", encoding="ascii")
        opened().read.assert_called_once_with(129)

    def test_unavailable_or_invalid_platform_fields_are_unknown(self):
        with patch("sys.platform", "linux"), patch("pathlib.Path.open", side_effect=OSError()), \
                patch("ai_dlc.evaluation.environment._linux_architecture", return_value="/private/path"):
            result = collect_environment()
        self.assertIsNone(result["os_release"])
        self.assertIsNone(result["machine_architecture"])
        self.assertEqual(compare_environments({"schema_version": 2, "runtime_environment": result},
                                             {"schema_version": 2, "runtime_environment": result})["status"], "unknown")

    def test_prerelease_python_version_keeps_release_level_and_serial(self):
        version = SimpleNamespace(major=3, minor=15, micro=0, releaselevel="candidate", serial=2)
        with patch("sys.version_info", version), patch("sys.platform", "unsupported"):
            result = collect_environment()
        self.assertEqual(result["python_version"], "3.15.0rc2")
        self.assertIsNone(result["os_family"])

    def test_cli_compare_exposes_unknown_environment_and_preserves_exit_status(self):
        report = self.report()
        legacy = deepcopy(report)
        legacy.update(schema_version=1, evaluation_id="legacy-report")
        del legacy["runtime_environment"]
        with temporary_directory() as root:
            left, right = save_report(legacy, root), save_report(report, root)
            output = io.StringIO()
            with patch("sys.stdout", output):
                code = main(["eval", "compare", "--baseline", str(left), "--candidate", str(right)])
            result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["environment"]["status"], "unknown")
        self.assertEqual(result["regressions"], [])
        self.assertFalse(result["eligible_for_release"])


if __name__ == "__main__":
    unittest.main()
