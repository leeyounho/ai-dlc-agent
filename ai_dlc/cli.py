"""Configuration and offline evaluation CLI; no service listener is started here."""

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .config.loader import (assert_separate_paths, assert_service_isolation,
                            inspect_service_readiness, load_connection, load_repository,
                            load_service)
from .config.types import PURPOSES
from .errors import AgentError
from .evaluation.runner import (compare_reports, load_plan, load_suite, read_report,
                                render_report, run_suite, save_report)
from .models.router import ModelRegistry, ModelRouter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-dlc", description="Internal AI-DLC configuration, workflow, and offline evaluation")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-config", help="Validate connection v2 and optional repository v2 without network access")
    validate.add_argument("--config", type=Path, required=True)
    validate.add_argument("--repository", type=Path)
    validate.add_argument("--compare-config", type=Path, help="Also reject overlapping managed directories in a second connection profile")
    service = commands.add_parser("validate-service", help="Validate a service v1 bundle and inspect local readiness without network access")
    service.add_argument("--service", type=Path, required=True)
    service.add_argument("--compare-service", type=Path, help="Also reject shared runtime paths and credential references")
    service.add_argument("--require-ready", action="store_true", help="Exit 3 while local service prerequisites remain unconfigured")
    route = commands.add_parser("route-model", help="Explain model choice and configuration readiness; never call a model")
    route.add_argument("--config", type=Path, required=True)
    route.add_argument("--repository", type=Path)
    route.add_argument("--purpose", choices=sorted(PURPOSES), required=True)
    route.add_argument("--require-ready", action="store_true", help="Exit 3 if pre-dispatch configuration is not ready")
    evaluation = commands.add_parser("eval", help="Local contract evaluation; not an operational performance benchmark")
    subs = evaluation.add_subparsers(dest="eval_command", required=True)
    for name in ("validate", "run"):
        cmd = subs.add_parser(name)
        cmd.add_argument("--plan", required=True, type=Path)
        if name == "run":
            cmd.add_argument("--output", type=Path, help="Override local report output directory")
    report = subs.add_parser("report")
    report.add_argument("--evaluation", type=Path, required=True)
    compare = subs.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    workflow = commands.add_parser("workflow", help="Offline workflow scenario and local state inspection")
    workflow_subs = workflow.add_subparsers(dest="workflow_command", required=True)
    demo = workflow_subs.add_parser("demo", help="Run synthetic approval/recovery cases; never contact GitHub or a model")
    demo.add_argument("--output", type=Path, default=Path("var/workflow-evaluations"))
    inspect = workflow_subs.add_parser("inspect", help="Read a task's verified journal; requires exclusive state ownership")
    inspect.add_argument("--state-root", type=Path, required=True)
    inspect.add_argument("--instance", required=True)
    inspect.add_argument("--repository-id", type=int, required=True)
    inspect.add_argument("--issue", type=int, required=True)
    execution = commands.add_parser("execution", help="Evaluate the common executor using bundled local subprocess fixtures")
    execution_subs = execution.add_subparsers(dest="execution_command", required=True)
    execution_demo = execution_subs.add_parser("demo", help="Run fixed synthetic commands; never execute repository scripts")
    execution_demo.add_argument("--output", type=Path, default=Path("var/execution-evaluations"))
    return parser


def _emit(value, *, error: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=sys.stderr if error else sys.stdout)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-service":
            bundle = load_service(args.service)
            if args.compare_service:
                assert_service_isolation(bundle, load_service(args.compare_service))
            readiness = inspect_service_readiness(bundle, environment=os.environ)
            _emit({"valid": True, "validation_scope": "structure_references_and_local_readiness",
                   "profile": bundle.connection.profile, "service_digest": bundle.service.digest,
                   "connection_digest": bundle.connection.digest,
                   "repository_count": len(bundle.repositories), "readiness": readiness.as_dict(),
                   "network_calls": 0, "production_ready": False})
            return 3 if args.require_ready and readiness.status != "ready_for_transport_consumers" else 0
        if args.command == "execution":
            from .execution.demo import run_demo
            report = run_demo(args.output)
            _emit({key: report[key] for key in ("evaluation_type", "status", "counts", "intercepted_network_attempts",
                                               "eligible_for_release", "state_root", "report_json", "report_markdown")})
            return 0 if report["status"] == "pass" else 1
        if args.command == "workflow":
            if args.workflow_command == "demo":
                from .workflow.demo import run_demo
                _emit(run_demo(args.output))
            else:
                from .storage import FileJournal, TaskKey
                key = TaskKey(args.instance, args.repository_id, args.issue)
                with FileJournal(args.state_root, create=False) as store:
                    state = store.read(key)
                    if state is None:
                        raise AgentError("TASK_MISSING", "No committed task exists for this identity.")
                    _emit({"task": key.as_dict(), "state": state, "production_ready": False})
            return 0
        if args.command in ("validate-config", "route-model"):
            config = load_connection(args.config)
            repository = load_repository(args.repository, connection=config) if args.repository else None
            router = ModelRouter(ModelRegistry(config))
            if args.command == "validate-config":
                if args.compare_config:
                    other = load_connection(args.compare_config)
                    assert_separate_paths([*config.directories.values(), *other.directories.values()])
                selections = [router.resolve(purpose, repository).summary() for purpose in sorted(PURPOSES)]
                _emit({"valid": True, "validation_scope": "structure_and_logical_policy", "profile": config.profile,
                       "config_digest": config.digest, "provider_count": len(config.providers), "model_count": len(config.models),
                       "repository_enabled": repository.enabled if repository else None,
                       "production_ready": False, "routes": selections})
                return 0
            selected = router.resolve(args.purpose, repository)
            preflight = router.preflight(args.purpose, repository, environment=os.environ, available_adapters=frozenset())
            _emit({**selected.summary(), "preflight": preflight.as_dict(), "network_calls": 0, "production_ready": False})
            return 3 if args.require_ready and preflight.status != "ready_for_registered_adapter" else 0
        if args.eval_command in ("validate", "run"):
            plan = load_plan(args.plan)
            suite = load_suite(plan["suite_file"])
            if args.eval_command == "validate":
                _emit({"valid": True, "evaluation_type": "local_contract", "suite_id": suite["suite_id"],
                       "suite_digest": suite["digest"], "case_count": len(suite["cases"]), "eligible_for_release": False})
                return 0
            result = run_suite(suite)
            destination = save_report(result, args.output or plan["results_root"])
            _emit({"evaluation_id": result["evaluation_id"], "status": result["status"], "counts": result["counts"],
                   "intercepted_network_attempts": result["intercepted_network_attempts"],
                   "report_json": str(destination / "report.json"), "report_markdown": str(destination / "report.md"),
                   "eligible_for_release": False})
            return 0 if result["status"] == "pass" else 1
        if args.eval_command == "report":
            report = read_report(args.evaluation)
            print(render_report(report), end="")
            return 0 if report["status"] == "pass" else 1
        comparison = compare_reports(read_report(args.baseline), read_report(args.candidate))
        _emit(comparison)
        return 1 if comparison["regressions"] else 0
    except AgentError as error:
        _emit({"error": error.as_dict()}, error=True)
        return 2
    except KeyboardInterrupt:
        _emit({"error": {"code": "INTERRUPTED", "message": "Operation interrupted."}}, error=True)
        return 130
    except Exception:
        _emit({"error": {"code": "INTERNAL_ERROR", "message": "Unexpected local error; no exception payload was published."}}, error=True)
        return 2
