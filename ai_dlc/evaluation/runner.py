"""Declarative, local-only evaluations of real config/routing/dispatch code."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import uuid

from .. import __version__, validation as v
from ..config.loader import parse_connection, parse_repository
from ..config.network import authorize_url
from ..errors import AgentError
from ..models.router import DispatchGuard, ModelRegistry, ModelRouter
from .offline import OfflineNetworkGuard

OPERATIONS = {"validate", "resolve", "preflight", "dispatch", "authorize_destination"}


def _contained_file(root: Path, value: str) -> Path:
    value = v.string(value, "suite.fixture")
    path = (root / v.local_path(value, "suite.fixture")).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise AgentError("EVAL_FIXTURE", "Suite fixtures must be existing files inside the suite directory.")
    return path


def load_plan(path: Path) -> dict:
    path = v.local_path(path).resolve()
    raw = v.read_json(path)
    v.obj(raw, "plan", {"schema_version", "evaluation_type", "suite_file", "results_root"})
    v.version(raw["schema_version"], 1)
    v.enum(raw["evaluation_type"], {"local_contract"}, "plan.evaluation_type")
    suite_file = _contained_file(path.parent, raw["suite_file"])
    results = (path.parent / v.local_path(raw["results_root"], "plan.results_root")).resolve()
    return {"suite_file": suite_file, "results_root": results}


def load_suite(path: Path) -> dict:
    path = v.local_path(path).resolve()
    raw = v.read_json(path)
    v.obj(raw, "suite", {"schema_version", "suite_id", "connection_fixture", "repository_fixture", "environment", "cases"})
    v.version(raw["schema_version"], 1)
    v.identifier(raw["suite_id"], "suite.suite_id")
    connection = v.read_json(_contained_file(path.parent, raw["connection_fixture"]))
    repository = v.read_json(_contained_file(path.parent, raw["repository_fixture"]))
    environment = v.mapping(raw["environment"], "suite.environment", nonempty=False)
    for key, value in environment.items():
        v.env_name(key, "suite.environment")
        v.string(value, "suite.environment.value")
    cases = v.array(raw["cases"], "suite.cases", nonempty=True)
    seen = set()
    for case in cases:
        v.obj(case, "case", {"id", "operation", "expected"}, {"config_patch", "repository_patch", "use_repository",
              "purpose", "url", "boundary", "available_adapters", "stub_failure", "environment_patch"})
        cid = v.identifier(case["id"], "case.id")
        if cid in seen:
            raise AgentError("EVAL_DUPLICATE_CASE", "Evaluation case IDs must be unique.")
        seen.add(cid)
        operation = v.enum(case["operation"], OPERATIONS, "case.operation")
        if operation in {"resolve", "preflight", "dispatch"}:
            # Unknown purposes intentionally exercise the router's refusal path.
            v.identifier(case.get("purpose"), "case.purpose")
        if operation == "authorize_destination":
            v.string(case.get("url"), "case.url")
        for key in ("use_repository", "stub_failure"):
            if key in case:
                v.boolean(case[key], "case." + key)
        if "boundary" in case:
            v.enum(case["boundary"], {"internal", "external"}, "case.boundary")
        if "available_adapters" in case:
            v.unique_strings(case["available_adapters"], "case.available_adapters")
        for key in ("config_patch", "repository_patch"):
            _validate_patches(case.get(key, []))
        for key, value in v.mapping(case.get("environment_patch", {}), "case.environment_patch", nonempty=False).items():
            v.env_name(key, "case.environment_patch")
            if value is not None:
                v.string(value, "case.environment_patch.value")
        _validate_expected(case["expected"], operation)
    # The base fixture must be valid; invalid cases are expressed only as patches.
    cfg = parse_connection(connection, base_dir=path.parent)
    parse_repository(repository, connection=cfg)
    return {"suite_id": raw["suite_id"], "cases": cases, "connection": connection, "repository": repository,
            "environment": environment, "base_dir": path.parent,
            "digest": v.canonical_digest({"suite": raw, "connection": connection, "repository": repository})}


def _validate_expected(expected, operation):
    if type(expected) is not dict:
        v.fail("EVAL_EXPECTATION", "case.expected", "Expected result must be an object.")
    outcome = expected.get("outcome")
    outcomes = {"validate": "valid", "resolve": "resolved", "preflight": "preflight",
                "dispatch": "dispatched", "authorize_destination": "authorized"}
    fields = {"outcome", "adapter_calls"}
    if outcome == "error":
        fields.add("code")
        v.identifier(expected.get("code"), "case.expected.code")
    elif outcome != outcomes[operation]:
        v.fail("EVAL_EXPECTATION", "case.expected", "Outcome does not match the operation.")
    elif operation == "resolve":
        fields |= {"model_id", "provider_id", "route_source"}
        for key in ("model_id", "provider_id", "route_source"):
            v.identifier(expected.get(key), "case.expected." + key)
    elif operation == "preflight":
        fields |= {"status", "reasons"}
        v.enum(expected.get("status"), {"configuration_pending", "denied", "ready_for_registered_adapter"}, "case.expected.status")
        v.unique_strings(expected.get("reasons"), "case.expected.reasons")
    elif operation == "dispatch":
        fields.add("model_id")
        v.identifier(expected.get("model_id"), "case.expected.model_id")
    v.obj(expected, "case.expected", fields)
    v.integer(expected["adapter_calls"], "case.expected.adapter_calls", minimum=0)


def _validate_patches(patches):
    for patch in v.array(patches, "case.patches"):
        v.obj(patch, "case.patch", {"op", "path"}, {"value"})
        op = v.enum(patch["op"], {"set", "remove"}, "case.patch.op")
        if op == "set" and "value" not in patch:
            v.fail("EVAL_PATCH", "case.patch", "Set operation requires a value.")
        if op == "remove" and "value" in patch:
            v.fail("EVAL_PATCH", "case.patch", "Remove operation must not have a value.")
        for key in v.array(patch["path"], "case.patch.path", nonempty=True):
            v.string(key, "case.patch.path")


def _apply_patches(document, patches):
    result = deepcopy(document)
    for patch in patches:
        target = result
        for key in patch["path"][:-1]:
            if type(target) is not dict or key not in target:
                raise AgentError("EVAL_PATCH", "Patch parent does not exist.")
            target = target[key]
        if type(target) is not dict:
            raise AgentError("EVAL_PATCH", "Patch parent is not an object.")
        key = patch["path"][-1]
        if patch["op"] == "remove":
            if key not in target:
                raise AgentError("EVAL_PATCH", "Patch removal target does not exist.")
            del target[key]
        else:
            target[key] = deepcopy(patch["value"])
    return result


def _case_result(suite, case):
    # Harness patch errors fail the evaluation; never count them as policy refusals.
    raw = _apply_patches(suite["connection"], case.get("config_patch", []))
    repo_raw = _apply_patches(suite["repository"], case.get("repository_patch", []))
    environment = dict(suite["environment"])
    for key, value in case.get("environment_patch", {}).items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    calls = []

    def recording_adapter(selected):
        calls.append(selected.model.id)
        if case.get("stub_failure", False):
            raise RuntimeError("Synthetic adapter failure.")
        return selected.model.id

    adapters = {key: recording_adapter for key in case.get("available_adapters", ["openai_chat_completions", "openai_responses"])}
    with OfflineNetworkGuard() as network_guard:
        try:
            config = parse_connection(raw, base_dir=suite["base_dir"])
            repository = parse_repository(repo_raw, connection=config) if case.get("use_repository", True) else None
            router = ModelRouter(ModelRegistry(config))
            op = case["operation"]
            if op == "validate":
                actual = {"outcome": "valid"}
            elif op == "resolve":
                result = router.resolve(case["purpose"], repository)
                actual = {"outcome": "resolved", "model_id": result.model.id, "provider_id": result.provider.id,
                          "route_source": result.route_source}
            elif op == "preflight":
                result = router.preflight(case["purpose"], repository, environment=environment, available_adapters=frozenset(adapters))
                actual = {"outcome": "preflight", **result.as_dict()}
            elif op == "dispatch":
                result = DispatchGuard(router, adapters).dispatch(case["purpose"], repository, environment=environment)
                actual = {"outcome": "dispatched", "model_id": result}
            else:
                authorize_url(config.network, case["url"], case.get("boundary"))
                actual = {"outcome": "authorized"}
        except AgentError as error:
            actual = {"outcome": "error", "code": error.code}
        except Exception:
            actual = {"outcome": "error", "code": "EVAL_INTERNAL_ERROR"}
    actual["adapter_calls"] = len(calls)
    expected = deepcopy(case["expected"])
    if "reasons" in expected:
        expected["reasons"] = sorted(expected["reasons"])
    passed = actual == expected and network_guard.attempts == 0
    return {"case_id": case["id"], "operation": case["operation"], "status": "pass" if passed else "fail",
            "expected": expected, "actual": actual, "intercepted_network_attempts": network_guard.attempts}


def source_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def run_suite(suite: dict) -> dict:
    results = []
    for case in suite["cases"]:
        try:
            results.append(_case_result(suite, case))
        except AgentError as error:
            results.append({"case_id": case["id"], "operation": case["operation"], "status": "fail",
                            "expected": case["expected"], "actual": {"outcome": "harness_error", "code": error.code},
                            "intercepted_network_attempts": 0})
    passed = sum(row["status"] == "pass" for row in results)
    return {"schema_version": 1, "evaluation_type": "local_contract",
            "evaluation_id": "eval-" + uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(), "agent_version": __version__,
            "source_digest": source_digest(), "suite_id": suite["suite_id"], "suite_digest": suite["digest"],
            "status": "pass" if passed == len(results) else "fail",
            "counts": {"total": len(results), "passed": passed, "failed": len(results) - passed},
            "intercepted_network_attempts": sum(r["intercepted_network_attempts"] for r in results),
            "eligible_for_release": False,
            "not_evaluated": ["actual_llm_quality", "human_approvals", "ghes", "os_isolation", "rhel_compatibility",
                              "state_recovery", "java_build", "deployment", "operational_readiness"],
            "cases": results}


def render_report(report: dict) -> str:
    lines = ["# 로컬 설정·모델 선택 평가", "", f"평가 ID: {report['evaluation_id']}",
             f"결과: {report['status']} · {report['counts']['passed']}/{report['counts']['total']} 통과", "",
             "이 결과는 로컬 계약 검증입니다. 실제 LLM 성능이나 운영 도입 적합성을 평가하지 않았습니다.", "",
             f"Python 네트워크 호출 차단 기록: {report['intercepted_network_attempts']}건", "",
             f"코드 digest: `{report['source_digest']}`", f"평가 세트 digest: `{report['suite_digest']}`", "",
             "| 사례 | 동작 | 판정 |", "| --- | --- | --- |"]
    for row in report["cases"]:
        lines.append(f"| {row['case_id']} | {row['operation']} | {row['status']} |")
    failed = [row for row in report["cases"] if row["status"] != "pass"]
    if failed:
        lines += ["", "## 실패 근거", "", "```json", json.dumps(failed, ensure_ascii=False, indent=2), "```"]
    lines += ["", "운영 도입 판정: 미평가. 실제 연결·승인·상태 복구·빌드·배포 검증은 후속 구현 대상입니다.", ""]
    return "\n".join(lines)


def save_report(report: dict, results_root: Path) -> Path:
    results_root = v.local_path(results_root)
    v.identifier(report["evaluation_id"], "report.evaluation_id")
    staging = results_root / (".pending-" + uuid.uuid4().hex)
    final = results_root / report["evaluation_id"]
    try:
        results_root.mkdir(parents=True, exist_ok=True)
        # On Windows, inherit the operator-configured directory ACL. Python's
        # special 0700 ACL can exclude a restricted development token. On RHEL,
        # the report directory is private to the service account.
        staging.mkdir(mode=0o700 if os.name == "posix" else 0o777)
        for name, data in (("report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n"),
                           ("report.md", render_report(report))):
            with (staging / name).open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        # Unique IDs and no overwrite. Failed staging is retained as incomplete evidence.
        if final.exists():
            raise AgentError("EVAL_REPORT_EXISTS", "Evaluation ID already exists.")
        staging.rename(final)
    except OSError:
        raise AgentError("EVAL_REPORT_WRITE", "Unable to persist the evaluation report.") from None
    return final


def read_report(path: Path) -> dict:
    path = v.local_path(path)
    if path.is_dir():
        path = path / "report.json"
    result = v.read_json(path)
    v.obj(result, "report", {"schema_version", "evaluation_type", "evaluation_id", "created_at", "agent_version", "source_digest",
          "suite_id", "suite_digest", "status", "counts", "intercepted_network_attempts", "eligible_for_release", "not_evaluated", "cases"})
    v.version(result["schema_version"], 1)
    v.enum(result["evaluation_type"], {"local_contract"}, "report.evaluation_type")
    if result["eligible_for_release"] is not False:
        raise AgentError("EVAL_REPORT_INVALID", "Local contract reports cannot certify a release.")
    for key in ("evaluation_id", "suite_id", "source_digest", "suite_digest"):
        v.string(result[key], "report." + key)
    v.enum(result["status"], {"pass", "fail"}, "report.status")
    rows = v.array(result["cases"], "report.cases", nonempty=True)
    ids = set()
    for row in rows:
        v.obj(row, "report.case", {"case_id", "operation", "status", "expected", "actual", "intercepted_network_attempts"})
        cid = v.identifier(row["case_id"], "report.case_id")
        if cid in ids:
            raise AgentError("EVAL_REPORT_INVALID", "Duplicate report case ID.")
        ids.add(cid)
        v.enum(row["status"], {"pass", "fail"}, "report.case.status")
        v.enum(row["operation"], OPERATIONS, "report.case.operation")
        v.integer(row["intercepted_network_attempts"], "report.case.intercepted_network_attempts", minimum=0)
        expected_pass = row["expected"] == row["actual"] and row["intercepted_network_attempts"] == 0
        if (row["status"] == "pass") != expected_pass:
            raise AgentError("EVAL_REPORT_INVALID", "Case outcome does not agree with its evidence.")
    counts = {"total": len(rows), "passed": sum(r["status"] == "pass" for r in rows), "failed": sum(r["status"] != "pass" for r in rows)}
    if (result["counts"] != counts or result["status"] != ("pass" if counts["failed"] == 0 else "fail")
            or result["intercepted_network_attempts"] != sum(r["intercepted_network_attempts"] for r in rows)):
        raise AgentError("EVAL_REPORT_INVALID", "Report summary does not agree with its evidence.")
    return result


def compare_reports(baseline: dict, candidate: dict) -> dict:
    if baseline["suite_digest"] != candidate["suite_digest"]:
        raise AgentError("EVAL_SUITE_MISMATCH", "Only identical suites and fixtures can be compared.")
    a = {r["case_id"]: r for r in baseline["cases"]}
    b = {r["case_id"]: r for r in candidate["cases"]}
    if a.keys() != b.keys() or any(a[k]["expected"] != b[k]["expected"] for k in a):
        raise AgentError("EVAL_SUITE_MISMATCH", "Report expectations do not match.")
    return {"evaluation_type": "local_contract", "baseline": baseline["evaluation_id"], "candidate": candidate["evaluation_id"],
            "regressions": sorted(k for k in a if a[k]["status"] == "pass" and b[k]["status"] == "fail"),
            "improvements": sorted(k for k in a if a[k]["status"] == "fail" and b[k]["status"] == "pass"),
            "eligible_for_release": False}
