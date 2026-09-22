from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import socket
import unittest

from tests.support import temporary_directory

from ai_dlc.cli import main
from ai_dlc.errors import AgentError
from ai_dlc.evaluation.offline import OfflineNetworkGuard
from ai_dlc.evaluation.runner import (compare_reports, load_plan, load_suite, read_report,
                                      render_report, run_suite, save_report)

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "evaluation/core.plan.json"


class EvaluationAndCliTests(unittest.TestCase):
    def invoke(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(map(str, args)))
        return code, stdout.getvalue(), stderr.getvalue()

    def suite(self):
        return load_suite(load_plan(PLAN)["suite_file"])

    def test_workflow_demo_persists_history_and_inspect_reads_current_state(self):
        with temporary_directory() as temp:
            code, stdout, stderr = self.invoke("workflow", "demo", "--output", temp)
            self.assertEqual(code, 0, stderr)
            report = json.loads(stdout)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["intercepted_network_attempts"], 0)
            self.assertFalse(report["eligible_for_release"])
            self.assertEqual(len(report["steps"]), 10)
            self.assertEqual(report["journal_records"], 10)
            self.assertTrue(all(check["passed"] for check in report["checks"]))
            self.assertTrue(Path(report["report_json"]).is_file())
            code, stdout, stderr = self.invoke("workflow", "inspect", "--state-root", report["state_root"],
                "--instance", "local-workflow-demo", "--repository-id", 1, "--issue", 1)
            self.assertEqual(code, 0, stderr)
            state = json.loads(stdout)["state"]
            self.assertEqual(state["status"], "waiting_human")
            self.assertEqual(state["requirements"]["revision"], "req-0002")
            self.assertIsNone(state["requirement_approval"])

    def test_execution_demo_runs_synthetic_processes_and_reports_expected_failures(self):
        with temporary_directory() as temp:
            code, stdout, stderr = self.invoke("execution", "demo", "--output", temp)
            self.assertEqual(code, 0, stderr)
            summary = json.loads(stdout)
            self.assertEqual(summary["counts"], {"total": 6, "passed": 6})
            self.assertEqual(summary["intercepted_network_attempts"], 0)
            self.assertFalse(summary["eligible_for_release"])
            report = json.loads(Path(summary["report_json"]).read_text(encoding="utf-8"))
            self.assertTrue(Path(summary["report_markdown"]).is_file())
            self.assertTrue(all(case["launch_count_after_redelivery"] == 1 for case in report["cases"]))
            self.assertEqual(report["cases"][0]["actual"]["verification"]["executed"], 1)

    def test_core_cases_exercise_real_logic_and_never_certify_release(self):
        report = run_suite(self.suite())
        self.assertEqual(report["status"], "pass", [r for r in report["cases"] if r["status"] != "pass"])
        self.assertGreaterEqual(report["counts"]["total"], 30)
        self.assertEqual(report["intercepted_network_attempts"], 0)
        self.assertFalse(report["eligible_for_release"])
        self.assertIn("actual_llm_quality", report["not_evaluated"])

    def test_wrong_oracle_fails_instead_of_reporting_perfect_score(self):
        suite = self.suite()
        case = next(c for c in suite["cases"] if c["id"] == "global-default")
        case["expected"]["model_id"] = "beta"
        report = run_suite(suite)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertIn("global-default", render_report(report))

    def test_python_network_guard_intercepts_without_socket_or_dns(self):
        original = socket.getaddrinfo
        with OfflineNetworkGuard() as guard:
            with self.assertRaises(AgentError) as raised:
                socket.getaddrinfo("never-contact.invalid", 443)
            self.assertEqual(raised.exception.code, "NETWORK_ATTEMPT_BLOCKED")
        self.assertEqual(guard.attempts, 1)
        self.assertIs(socket.getaddrinfo, original)

    def test_reports_persist_validate_and_compare(self):
        report = run_suite(self.suite())
        with temporary_directory() as temp:
            directory = save_report(report, Path(temp))
            self.assertTrue((directory / "report.md").is_file())
            reloaded = read_report(directory)
            self.assertEqual(reloaded, report)
            comparison = compare_reports(reloaded, report)
            self.assertEqual(comparison["regressions"], [])
            self.assertFalse(comparison["eligible_for_release"])

    def test_report_tampering_detected(self):
        report = run_suite(self.suite())
        with temporary_directory() as temp:
            path = Path(temp) / "report.json"
            for change in (lambda r: r.update(eligible_for_release=True),
                           lambda r: r["counts"].update(total=999),
                           lambda r: r["cases"][0].update(status="fail")):
                modified = deepcopy(report)
                change(modified)
                path.write_text(json.dumps(modified), encoding="utf-8")
                with self.assertRaises(AgentError):
                    read_report(path)

    def test_different_suites_cannot_be_compared(self):
        a = run_suite(self.suite())
        b = deepcopy(a)
        b["suite_digest"] = "different"
        with self.assertRaises(AgentError) as raised:
            compare_reports(a, b)
        self.assertEqual(raised.exception.code, "EVAL_SUITE_MISMATCH")

    def test_regression_comparison_uses_case_ids(self):
        suite = self.suite()
        a = run_suite(suite)
        b = deepcopy(a)
        b["cases"][0]["status"] = "fail"
        self.assertEqual(compare_reports(a, b)["regressions"], [a["cases"][0]["case_id"]])

    def test_fixture_path_escape_denied(self):
        with temporary_directory() as temp:
            path = Path(temp) / "plan.json"
            path.write_text(json.dumps({"schema_version": 1, "evaluation_type": "local_contract",
                                       "suite_file": "../outside.json", "results_root": "results"}), encoding="utf-8")
            with self.assertRaises(AgentError) as raised:
                load_plan(path)
            self.assertEqual(raised.exception.code, "EVAL_FIXTURE")

    def test_cli_config_and_routes_are_offline(self):
        with OfflineNetworkGuard() as guard:
            code, stdout, stderr = self.invoke("validate-config", "--config", ROOT / "config/production.example.json",
                                               "--repository", ROOT / "config/repository.example.json",
                                               "--compare-config", ROOT / "config/test-external.example.json")
        self.assertEqual(code, 0, stderr)
        result = json.loads(stdout)
        self.assertTrue(result["valid"])
        self.assertFalse(result["production_ready"])
        self.assertEqual(guard.attempts, 0)
        code, stdout, stderr = self.invoke("route-model", "--config", ROOT / "config/production.example.json",
                                           "--purpose", "implementation", "--require-ready")
        self.assertEqual(code, 3, stderr)
        self.assertEqual(json.loads(stdout)["model_id"], "gpt-oss")

    def test_cli_json_error_is_sanitized_and_nonzero(self):
        with temporary_directory() as temp:
            path = Path(temp) / "invalid.json"
            path.write_text('{"secret": "do-not-print",}', encoding="utf-8")
            code, stdout, stderr = self.invoke("validate-config", "--config", path)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn("do-not-print", stderr)
        self.assertEqual(json.loads(stderr)["error"]["code"], "CONFIG_JSON")

    def test_cli_evaluation_run_report_compare(self):
        with temporary_directory() as temp:
            code, stdout, stderr = self.invoke("eval", "run", "--plan", PLAN, "--output", temp)
            self.assertEqual(code, 0, stderr)
            report_path = json.loads(stdout)["report_json"]
            code, stdout, stderr = self.invoke("eval", "report", "--evaluation", report_path)
            self.assertEqual(code, 0, stderr)
            self.assertIn("로컬", stdout)
            code, stdout, stderr = self.invoke("eval", "compare", "--baseline", report_path, "--candidate", report_path)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["regressions"], [])


if __name__ == "__main__":
    unittest.main()
