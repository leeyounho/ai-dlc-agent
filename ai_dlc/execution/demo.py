"""Reproducible local command cases; no repository code or external services."""

from pathlib import Path
from types import MappingProxyType
import uuid

from .. import validation as v
from ..config.types import CommandProfile, ProjectProfile, RepositoryConfig, Routes, WorkflowPolicy
from ..evaluation.offline import OfflineNetworkGuard
from ..evaluation.process_fixture import FixtureProcessRunner
from ..evaluation.runner import source_digest
from ..storage import FileJournal, TaskKey
from ..storage.journal import _mkdir, _native, _publish
from ..workflow import WorkflowEngine
from ..workflow.demo import _DemoGateway
from .coordinator import ExecutionCoordinator
from .ports import ExecutionPlan
from .workspace import WorkspaceManager, local_root

SCENARIOS = (("pass", "succeeded", None), ("nonzero", "failed", "COMMAND_EXIT"),
             ("missing", "failed", "JUNIT_MISSING"), ("zero", "failed", "JUNIT_FAILED_OR_EMPTY"),
             ("skipped", "failed", "JUNIT_FAILED_OR_EMPTY"), ("mutate", "failed", "SOURCE_TREE_CHANGED"))


def run_demo(output: Path) -> dict:
    directory = local_root(output) / ("execution-" + uuid.uuid4().hex)
    records = []
    with OfflineNetworkGuard() as network, FileJournal(directory / "state") as store:
        source = directory / "fixture-source"
        _mkdir(source)
        _native(source / "input.txt").write_text("Synthetic source snapshot.\n", encoding="utf-8")
        manager = WorkspaceManager(directory / "workspaces", protected_roots=(store.root,))
        for number, (mode, expected_status, expected_reason) in enumerate(SCENARIOS, 1):
            key = TaskKey("local-execution-demo", 1, number)
            gateway = _DemoGateway(key)
            command = CommandProfile("fixture-python", (mode,), ".", 5, ("reports/*.xml",))
            policy = WorkflowPolicy("on_approval", "automatic", frozenset(), frozenset())
            roles = MappingProxyType({role: frozenset({1}) for role in
                ("requirement_approver", "starter", "design_approver", "operator", "deployment_approver")})
            repo = RepositoryConfig(True, key.instance_id, key.repository_id, frozenset(), Routes(None, MappingProxyType({})),
                v.canonical_digest({"fixture": "execution-demo-v1", "mode": mode}), policy, roles,
                ProjectProfile("command", "local-fixture-only", MappingProxyType({"unit_test": command}), ()))
            workflow = WorkflowEngine(store, repo, gateway)
            workflow.capture_source(key, expected_revision=0, event_id="source")
            workflow.normalize(key, {"summary": "Exercise the command contract", "scope": ["Synthetic input"],
                                    "acceptance_criteria": ["Judge the observed command and test evidence"], "open_questions": []},
                               expected_revision=1, event_id="requirements")
            workflow.handle_comment(key, gateway.add("/aidlc approve requirements req-0001"), expected_revision=2)
            workflow.propose_design(key, {"summary": "Run a fixed local fixture", "changes": ["No repository implementation"],
                                         "validation_plan": ["Evaluate source stability and JUnit evidence"], "open_questions": []},
                                    requirements_revision="req-0001", expected_revision=3, event_id="design")
            snapshot = manager.prepare(source, ("input.txt",))
            plan = ExecutionPlan("unit_test", "local-fixture-only", command, snapshot, "junit", ("reports/**",))
            runner = FixtureProcessRunner()
            coordinator = ExecutionCoordinator(workflow, runner)
            run_id = "run-" + uuid.uuid4().hex
            state = store.read(key)
            coordinator.reserve(key, run_id, plan, expected_revision=state["state_revision"], cancellation_epoch=state["cancellation_epoch"])
            result = coordinator.execute(key, run_id)
            repeated = coordinator.execute(key, run_id)
            passed = (result["status"] == expected_status and result["reason"] == expected_reason
                      and runner.launch_count == 1 and repeated == result)
            records.append({"case": mode, "passed": passed, "issue": number, "run_id": run_id,
                            "expected": {"status": expected_status, "reason": expected_reason}, "actual": result,
                            "launch_count_after_redelivery": runner.launch_count, "source_digest": snapshot.digest})
        result = {"schema_version": 1, "evaluation_type": "local_execution_contract", "source_digest": source_digest(),
                  "suite_digest": v.canonical_digest(SCENARIOS), "status": "pass" if all(r["passed"] for r in records) else "fail",
                  "counts": {"total": len(records), "passed": sum(r["passed"] for r in records)}, "cases": records,
                  "intercepted_network_attempts": network.attempts, "network_guard_scope": "controller_python_process",
                  "eligible_for_release": False, "state_root": str(directory / "state"),
                  "report_json": str(directory / "report.json"), "report_markdown": str(directory / "report.md"),
                  "limitations": ["Bundled synthetic subprocesses only", "No production OS isolation or egress validation",
                                  "No actual LLM, GHES, Maven, repository scripts, PR, or deployment"]}
        _publish(directory / "report.json", result)
        lines = ["# Local execution contract", "", f"Result: {result['status']}", "", "Eligible for release: false", "",
                 "| Case | Expected run status | Actual run status | Contract passed |", "| --- | --- | --- | --- |"]
        lines.extend(f"| {r['case']} | {r['expected']['status']} | {r['actual']['status']} | {r['passed']} |" for r in records)
        lines.extend(["", "Each command is a bundled synthetic local process. Failed fixture runs are expected failure cases.",
                      "A passed contract does not certify real model quality, RHEL isolation, or operational readiness.", ""])
        _native(directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return result
