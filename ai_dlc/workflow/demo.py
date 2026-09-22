"""Offline end-to-end control-plane scenario with explicitly synthetic humans.

This gateway cannot authenticate GitHub users. It is used only by the demo, and
no general CLI accepts caller-supplied 'verified' approvals for real repositories.
"""

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
import uuid

from .. import validation as v
from ..config.types import ProjectProfile, RepositoryConfig, Routes, WorkflowPolicy
from ..errors import AgentError
from ..evaluation.offline import OfflineNetworkGuard
from ..storage import FileJournal, TaskKey
from ..storage.journal import _publish
from .engine import WorkflowEngine
from .observations import CommentObservation, IssueObservation


class _DemoGateway:
    def __init__(self, key):
        self.source = IssueObservation(key, 1, "Synthetic evaluation metadata task", "Preserve this original requirement.\n", "v1")
        self.comments = {}

    def issue(self, task):
        return self.source

    def comment(self, task, comment_id):
        return self.comments.get(comment_id)

    def permission(self, task, actor_id):
        return "write" if actor_id == 1 else "none"

    def add(self, body):
        cid = len(self.comments) + 100
        self.comments[cid] = CommentObservation(self.source.task, cid, 1, "User", body,
                                                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
        return cid


def run_demo(output: Path) -> dict:
    output = v.local_path(output)
    run_id = "workflow-" + uuid.uuid4().hex
    directory = output / run_id
    key = TaskKey("local-workflow-demo", 1, 1)
    gateway = _DemoGateway(key)
    policy = WorkflowPolicy("explicit", "collaborative", frozenset({"explicit", "on_approval"}),
                            frozenset({"automatic", "collaborative"}))
    roles = MappingProxyType({role: frozenset({1}) for role in
                              ("requirement_approver", "starter", "design_approver", "operator", "deployment_approver")})
    repo = RepositoryConfig(True, key.instance_id, key.repository_id, frozenset(), Routes(None, MappingProxyType({})),
                            v.canonical_digest({"fixture": "workflow-demo-v1"}), policy, roles,
                            ProjectProfile("command", "synthetic", MappingProxyType({}), ()))
    requirements = {"summary": "Record approved environment metadata", "scope": ["Evaluation report"],
                    "acceptance_criteria": ["Do not include hostname, username, or credentials"], "open_questions": []}
    design = {"summary": "Collect Python and OS metadata", "changes": ["Extend report fields"],
              "validation_plan": ["Check allowed fields and older reports"], "open_questions": []}
    steps, checks = [], []

    def record(label, result):
        state = result.state
        steps.append({"step": label, "revision": result.revision, "phase": state["phase"],
                      "status": state["status"], "cancellation_epoch": state["cancellation_epoch"]})
        return state

    def denied(code, action):
        try:
            action()
        except AgentError as error:
            if error.code != code:
                raise
            checks.append({"check": code, "passed": True})
        else:
            raise AgentError("DEMO_FAILED", "A required workflow gate unexpectedly allowed work.")

    with OfflineNetworkGuard() as network:
        with FileJournal(directory / "state") as store:
            engine = WorkflowEngine(store, repo, gateway)
            state = record("original_preserved", engine.capture_source(key, expected_revision=0, event_id="source-v1"))
            state = record("requirements_normalized", engine.normalize(key, requirements, expected_revision=state["state_revision"], event_id="normalize-v1"))
            denied("GATE_CLOSED", lambda: engine.implementation_gate(key, expected_revision=state["state_revision"], cancellation_epoch=state["cancellation_epoch"]))
            for label, body in (("requirements_approved", "/aidlc approve requirements req-0001"),
                                ("development_started", "/aidlc start req-0001")):
                state = record(label, engine.handle_comment(key, gateway.add(body), expected_revision=state["state_revision"]))
            state = record("design_proposed", engine.propose_design(key, design, requirements_revision="req-0001",
                                                                    expected_revision=state["state_revision"], event_id="design-v1"))
            denied("GATE_CLOSED", lambda: engine.implementation_gate(key, expected_revision=state["state_revision"], cancellation_epoch=state["cancellation_epoch"]))
            state = record("design_approved", engine.handle_comment(key, gateway.add("/aidlc approve design des-0001"), expected_revision=state["state_revision"]))
            basis = engine.implementation_gate(key, expected_revision=state["state_revision"], cancellation_epoch=state["cancellation_epoch"])
            checks.append({"check": "approved_implementation_gate", "passed": True})
            state = record("paused", engine.handle_comment(key, gateway.add("/aidlc stop"), expected_revision=state["state_revision"]))
            denied("STATE_CONFLICT", lambda: engine.implementation_gate(key, expected_revision=basis.state_revision, cancellation_epoch=basis.cancellation_epoch))
            state = record("resumed", engine.handle_comment(key, gateway.add("/aidlc resume"), expected_revision=state["state_revision"]))
        with FileJournal(directory / "state") as store:
            engine = WorkflowEngine(store, repo, gateway)
            recovered = store.recover(key)
            if recovered != state:
                raise AgentError("DEMO_FAILED", "Recovery did not preserve the committed state.")
            engine.implementation_gate(key, expected_revision=state["state_revision"], cancellation_epoch=state["cancellation_epoch"])
            checks.append({"check": "recovered_with_live_revalidation", "passed": True})
            gateway.source = replace(gateway.source, body="Also compare environment differences.\n", version="v2")
            denied("SOURCE_CHANGED", lambda: engine.implementation_gate(key, expected_revision=state["state_revision"], cancellation_epoch=state["cancellation_epoch"]))
            state = record("source_changed", engine.capture_source(key, expected_revision=state["state_revision"], event_id="source-v2"))
            state = record("new_requirements_waiting_approval", engine.normalize(key, requirements,
                           expected_revision=state["state_revision"], event_id="normalize-v2"))
            denied("REVISION_STALE", lambda: engine.handle_comment(key, gateway.add("/aidlc approve requirements req-0001"), expected_revision=state["state_revision"]))
            duplicate = engine.handle_comment(key, 100, expected_revision=2)
            if not duplicate.duplicate or duplicate.state["requirement_approval"] is not None:
                raise AgentError("DEMO_FAILED", "A repeated comment changed the current approval state.")
            checks.append({"check": "redelivery_did_not_restore_old_approval", "passed": True})
            records = store.history(key)
        result = {"schema_version": 1, "evaluation_type": "local_workflow_contract", "run_id": run_id,
                  "status": "pass", "task": key.as_dict(), "steps": steps, "checks": checks,
                  "journal_records": len(records), "journal_digest": records[-1]["digest"],
                  "final_phase": state["phase"], "final_status": state["status"],
                  "intercepted_network_attempts": network.attempts, "eligible_for_release": False,
                  "limitations": ["Synthetic observations; no GHES authentication", "No model or code execution",
                                  "No PR, merge, deployment, or full SDLC evaluation"],
                  "state_root": str((directory / "state").absolute()), "report_json": str((directory / "report.json").absolute())}
        _publish(directory / "report.json", result)
    return result
