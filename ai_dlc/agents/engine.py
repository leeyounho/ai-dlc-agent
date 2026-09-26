"""One bounded model round per step, connected to real approval/file/runner engines."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import math
import threading
import time
import uuid

from .. import validation as v
from ..errors import AgentError
from ..execution.coordinator import ExecutionCoordinator
from ..execution.ports import ExecutionPlan
from ..models.types import json_text, json_value, require
from ..workflow.engine import _settle, active_execution
from .tools import prompt_for, tools_for


@dataclass(frozen=True)
class AgentLimits:
    model_calls: int = 40
    tool_calls: int = 100
    repair_iterations: int = 3
    active_seconds: int = 3600
    model_timeout_seconds: int = 120
    max_output_tokens: int = 2048

    def __post_init__(self):
        for name, value in asdict(self).items():
            v.integer(value, name, minimum=0 if name == "repair_iterations" else 1)


class AgentLoop:
    def __init__(self, workflow, sessions, workspace_tools, verification_manager, runner, *,
                 environment, limits=AgentLimits(), command_ids=("unit_test",), generated_patterns=(),
                 execution_profile=None, knowledge=None, source_commit=None, status_publisher=None,
                 monitor_interval=0.25, clock=time.monotonic):
        self.workflow, self.store, self.sessions = workflow, workflow.store, sessions
        self.files, self.verification_manager = workspace_tools, verification_manager
        self.coordinator = ExecutionCoordinator(workflow, runner)
        self.environment, self.limits = dict(environment), limits
        self.command_ids, self.generated_patterns = tuple(command_ids), tuple(generated_patterns)
        require(bool(self.command_ids) and len(set(self.command_ids)) == len(self.command_ids), "AGENT_COMMANDS")
        require(all(c in workflow.repository.project.commands for c in self.command_ids), "AGENT_COMMANDS")
        self.profile, self.knowledge, self.source_commit = execution_profile, knowledge, source_commit
        for context in (execution_profile, knowledge):
            if context is not None:
                require(source_commit is not None and getattr(context, "basis_commit", getattr(context, "commit", None))
                        == source_commit, "AGENT_CONTEXT_REVISION")
        self.publisher, self.clock = status_publisher, clock
        require(type(monitor_interval) in {int, float} and 0 < monitor_interval <= 5, "AGENT_MONITOR")
        self.monitor_interval = monitor_interval

    def _change(self, key, kind, update, *, blobs=()):
        with self.store.locked(key):
            history = self.store.history(key)
            def reduce(state):
                require(state is not None, "TASK_MISSING")
                update(state)
                state["state_revision"] = len(history) + 1
                return _settle(state)
            result = self.store.commit(key, expected_revision=len(history), event_id="agent-" + uuid.uuid4().hex,
                                       event={"kind": kind}, reduce=reduce, blobs=blobs)
            self.store.assert_healthy()
            return result.state

    def start(self, key, run_id):
        v.identifier(run_id, "agent_run")
        require(len(run_id) <= 24, "AGENT_RUN_ID")
        with self.store.locked(key):
            self.workflow._key(key)
            state = self.store.read(key)
            self.workflow._state(state)
            self.workflow._source_current(key, state)
            require(not active_execution(state), "AGENT_EXECUTION_ACTIVE")
            definition = {"run_id": run_id, "limits": asdict(self.limits),
                          "workspace": self.files.workspace.document(), "commands": list(self.command_ids),
                          "generated_patterns": list(self.generated_patterns), "config_digest": self.workflow.repository.digest,
                          "source_digest": state["source_digest"], "source_commit": self.source_commit}
            existing = state.get("agent")
            if existing:
                require(existing["definition"] == definition, "AGENT_RUN_EXISTS")
                return existing
            self.sessions.create_run(key, run_id, model_calls=self.limits.model_calls, tool_calls=self.limits.tool_calls,
                                     active_seconds=self.limits.active_seconds)
            initial = {"definition": definition, "stage": "requirements", "status": "queued", "reason": None,
                       "sequence": 0, "round": 0, "session_id": None, "in_progress": None,
                       "repair_iterations": 0, "active_seconds": 0, "checks": [], "review": None,
                       "amendments": [],
                       "diff": self.files.export_changes(), "observed_epoch": state["cancellation_epoch"]}
            return self._change(key, "agent_started", lambda s: s.update(agent=initial))["agent"]

    def _guard(self, key, stage, *, epoch=None):
        state = self.store.read(key)
        self.workflow._key(key)
        self.workflow._state(state)
        self.workflow._source_current(key, state)
        require(not state["paused"], "AGENT_PAUSED")
        agent = state["agent"]
        require(agent["definition"]["source_digest"] == state["source_digest"], "SOURCE_CHANGED")
        require(agent["definition"]["config_digest"] == self.workflow.repository.digest, "POLICY_CHANGED")
        if epoch is not None:
            require(epoch == state["cancellation_epoch"], "AGENT_BASIS_CHANGED")
        if stage != "requirements":
            self.workflow._basis(key, state, design=stage != "design")
        if stage not in {"requirements", "design"}:
            require(state["design"] is not None and not state["design"]["has_open_questions"], "GATE_CLOSED")
        return state

    @contextmanager
    def _monitor(self, key, stage, epoch, cancellation, deadline):
        done, revoked = threading.Event(), []
        def watch():
            while not done.wait(self.monitor_interval):
                try:
                    require(self.clock() < deadline, "AGENT_ACTIVE_BUDGET")
                    self._guard(key, stage, epoch=epoch)
                except Exception as error:
                    revoked.append(error.code if isinstance(error, AgentError) else "AGENT_OBSERVATION_FAILED")
                    cancellation.set()
                    return
        thread = threading.Thread(target=watch, name="agent-approval-watch", daemon=True)
        thread.start()
        try:
            yield revoked
        finally:
            done.set()
            thread.join()

    def _context(self, key, stage):
        state = self.store.read(key)
        agent = state["agent"]
        requirements = self.store.blob(key, state["requirements"]["digest"]) if state["requirements"] else {
            "source": self.store.blob(key, state["source_digest"]),
            "amendments": [self.store.blob(key, digest) for digest in state["amendments"]]}
        design = self.store.blob(key, state["design"]["digest"]) if state["design"] else {}
        context = {"document": design, "rules": self.profile.document() if self.profile else None}
        if self.knowledge is not None and stage in {"design", "review"}:
            selected = self.knowledge.context(requirements.get("summary", "repository change"), purpose=stage)
            context["knowledge"] = [{**item.provenance, "text": item.text} for item in selected.items]
            context["knowledge_conflicts"] = list(selected.conflicts)
        return {"requirements": json_text(requirements), "design": json_text(context),
                "diff": json_text(self.files.export_changes()),
                "verification_summary": json_text({"checks": agent["checks"], "review": agent["review"]})}

    def _transition(self, key, stage):
        def update(state):
            agent = state["agent"]
            agent.update(stage=stage, session_id=None, round=agent["round"] + 1, status="queued", reason=None)
        self._change(key, "agent_stage_changed", update)

    @staticmethod
    def needs_requirements_refresh(state):
        agent = (state or {}).get("agent")
        return bool(agent and state["requirements"] is None and state["amendments"] != agent["amendments"]
                    and agent["stage"] != "requirements" and not agent["in_progress"]
                    and agent["reason"] in {None, "APPROVAL_REQUIRED", "GATE_CLOSED", "AGENT_BASIS_CHANGED",
                                            "MODEL_CANCELLED", "AGENT_CANCELLED"})

    def _checks(self, key, cancellation):
        state = self._guard(key, "verification")
        agent = state["agent"]
        observed = self.files.state()
        results = []
        with self.files.command_lease(expected_workspace_digest=observed.digest):
            for index, command_id in enumerate(self.command_ids):
                require(not cancellation.is_set(), "AGENT_CANCELLED")
                current = self._guard(key, "verification", epoch=state["cancellation_epoch"])
                snapshot = self.verification_manager.prepare(self.files.workspace.root, tuple(f.path for f in observed.files))
                command = self.workflow.repository.project.commands[command_id]
                plan = ExecutionPlan(command_id, self.workflow.repository.project.toolchain_id, command, snapshot,
                                     "junit" if command.report_patterns else "exit_code", self.generated_patterns)
                run_id = "check-" + agent["definition"]["run_id"] + "-" + str(agent["sequence"]) + "-" + str(index)
                self.coordinator.reserve(key, run_id, plan, expected_revision=current["state_revision"],
                                         cancellation_epoch=current["cancellation_epoch"])
                result = self.coordinator.execute(key, run_id, plan, cancellation=cancellation)
                result = {"run_id": run_id, "command_id": command_id, "source_digest": observed.digest,
                          "status": result["status"], "reason": result["reason"], "result": result["result"],
                          "verification": result["verification"], "plan_digest": result["plan_digest"]}
                results.append(result)
                self._change(key, "agent_test_evidence", lambda s: s["agent"]["checks"].append(result))
                require(self.files.state().digest == observed.digest, "WORKSPACE_CHANGED")
                if result["status"] == "failed" and result["reason"] in {"COMMAND_EXIT", "JUNIT_FAILED_OR_EMPTY"}:
                    current = self.store.read(key)
                    self.workflow.prepare_repair(key, run_id, expected_revision=current["state_revision"],
                                                 event_id="repair-" + uuid.uuid4().hex)
                    self._consume_repair(key)
                    break
                require(result["status"] == "succeeded", result["reason"] or "AGENT_VERIFICATION_BLOCKED")
        return results

    def _tool(self, key, stage, call, cancellation, epoch):
        with self.store.locked(key):
            state = self._guard(key, stage, epoch=epoch)
            require(not cancellation.is_set(), "AGENT_CANCELLED")
            require(call.name in {t.name for t in tools_for(stage)}, "AGENT_TOOL_DENIED")
            self.sessions.claim_tool(key, state["agent"]["definition"]["run_id"], call.id)
            args = json_value(call.arguments_json)
            # Patch/read effects share the task lock with stop/policy changes.
            # Long-running commands release it and continuously re-observe gates.
            if call.name == "list_files":
                result = self.files.state().document()
            elif call.name == "read_file":
                result = self.files.read_text(args["path"])
            elif call.name == "search_text":
                result = self.files.search_text(args["query"])
            elif call.name == "apply_patch":
                paths = [item["path"] for item in args["expected_files"]]
                require(len(paths) == len(set(paths)), "PATCH_EXPECTATION")
                result = self.files.apply_patch(args["patch"], expected_workspace_digest=args["workspace_digest"],
                    expected_files={item["path"]: item["sha256"] or None for item in args["expected_files"]}).document()
            else:
                result = None
        if call.name == "run_checks":
            result = self._checks(key, cancellation)
        evidence = {"call_id": call.id, "result": result}
        self._change(key, "agent_tool_evidence", lambda s: None, blobs=(evidence,))
        self.sessions.finish_tool(key, state["agent"]["definition"]["run_id"], call.id,
                                  result=json_text(result), evidence_ref="blob:" + v.canonical_digest(evidence))
        return result

    def _consume_repair(self, key):
        def update(state):
            agent = state["agent"]
            require(agent["repair_iterations"] < self.limits.repair_iterations, "AGENT_REPAIR_BUDGET")
            agent["repair_iterations"] += 1
        self._change(key, "agent_repair", update)

    def _repair(self, key, *, count=True):
        if count:
            self._consume_repair(key)
        self._transition(key, "implementation")

    def _model_round(self, key, stage, cancellation, epoch, deadline):
        state = self.store.read(key)
        agent = state["agent"]
        run_id = agent["definition"]["run_id"]
        session_id = agent["session_id"] or f"s-{run_id}-{agent['round']}-{stage}"
        if not agent["session_id"]:
            self.sessions.open_session(key, run_id, session_id, purpose=stage, repository=self.workflow.repository,
                environment=self.environment, tools=tools_for(stage), prompt=prompt_for(stage), checkpoint=self._context(key, stage))
            self._change(key, "agent_session_bound", lambda s: s["agent"].update(session_id=session_id))
        request_id = f"r-{run_id}-{agent['sequence']}"
        with self._monitor(key, stage, epoch, cancellation, deadline) as revoked:
            response = self.sessions.generate(key, run_id, session_id, request_id, repository=self.workflow.repository,
                environment=self.environment, tools=tools_for(stage), max_output_tokens=self.limits.max_output_tokens,
                timeout_seconds=self.limits.model_timeout_seconds, cancellation=cancellation)
            require(not revoked, revoked[0] if revoked else "AGENT_BASIS_CHANGED")
            self._guard(key, stage, epoch=epoch)
            require(not cancellation.is_set(), "AGENT_CANCELLED")
            if response.tool_calls:
                for call in response.tool_calls:
                    self._tool(key, stage, call, cancellation, epoch)
                return
        with self.store.locked(key):
            current = self._guard(key, stage, epoch=epoch)
            if stage == "requirements":
                self.workflow.normalize(key, json_value(response.text), expected_revision=current["state_revision"],
                                        event_id="normalize-" + request_id)
                self._change(key, "agent_requirements_basis", lambda s: s["agent"].update(
                    amendments=list(s["amendments"])))
                self._transition(key, "design")
            elif stage == "design":
                self.workflow.propose_design(key, json_value(response.text), requirements_revision=current["requirements"]["revision"],
                    expected_revision=current["state_revision"], event_id="design-" + request_id)
                self._transition(key, "implementation")
            elif stage == "implementation":
                self._transition(key, "test_generation")
            elif stage == "test_generation":
                self._transition(key, "verification")
            elif stage == "review":
                review = json_value(response.text)
                v.obj(review, "review", {"summary", "findings"})
                require(type(review["summary"]) is str and type(review["findings"]) is list
                        and all(type(f) is str for f in review["findings"]), "AGENT_REVIEW_PROTOCOL")
                require(self.files.state().digest == current["agent"]["verified_digest"], "WORKSPACE_CHANGED")
                record = {**review, "source_digest": current["agent"]["verified_digest"], "advisory": True,
                          "human_approval": False}
                self._change(key, "agent_reviewed", lambda s: s["agent"].update(review=record))
                if review["findings"]:
                    self._repair(key)
                else:
                    self._change(key, "agent_ready_for_pr", lambda s: s["agent"].update(status="ready_for_pr", reason=None))

    def step(self, key, *, cancellation=None):
        cancellation = cancellation or threading.Event()
        started, reserved = self.clock(), 0
        with self.store.locked(key):
            state = self.store.read(key)
            require(state is not None and "agent" in state, "AGENT_NOT_STARTED")
            agent = state["agent"]
            if agent["in_progress"]:
                raise AgentError("AGENT_RECOVERY_REQUIRED", "An unfinished step requires explicit effect reconciliation.")
            if self.needs_requirements_refresh(state):
                # A verified amend command invalidates all human gates. Reuse
                # settled effects and budgets, never replay pending tool claims.
                try:
                    self._guard(key, "requirements")
                    self.sessions._idle(self.sessions.inspect(key, agent["definition"]["run_id"]))
                except AgentError as error:
                    return self._change(key, "agent_refresh_blocked", lambda s: s["agent"].update(
                        status="blocked", reason=error.code))["agent"]
                self._transition(key, "requirements")
                agent = self.store.read(key)["agent"]
            if agent["status"] == "blocked":
                return agent
            stage = agent["stage"]
            try:
                current = self._guard(key, stage)
                if agent["status"] == "ready_for_pr":
                    require(self.files.state().digest == agent["verified_digest"], "WORKSPACE_CHANGED")
                    return agent
            except AgentError as error:
                waiting = error.code in {"APPROVAL_REQUIRED", "START_REQUIRED", "GATE_CLOSED", "AGENT_PAUSED"}
                status = "waiting_human" if waiting else "blocked"
                if agent["status"] != status or agent["reason"] != error.code:
                    agent = self._change(key, "agent_waiting", lambda s: s["agent"].update(
                        status=status, reason=error.code))["agent"]
                if self.publisher is not None:
                    self.publisher.publish(key)
                return agent
            epoch = current["cancellation_epoch"]
            deadline = started + self.limits.active_seconds - agent["active_seconds"]
            reserved = self.limits.model_timeout_seconds if stage != "verification" else sum(
                self.workflow.repository.project.commands[c].timeout_seconds for c in self.command_ids)
            # A model round may request the configured checks; reserve for both.
            if stage in {"implementation", "test_generation"}:
                reserved += sum(self.workflow.repository.project.commands[c].timeout_seconds for c in self.command_ids)
            if agent["active_seconds"] + reserved > self.limits.active_seconds:
                result = self._change(key, "agent_budget_exhausted", lambda s: s["agent"].update(
                    status="blocked", reason="AGENT_ACTIVE_BUDGET"))["agent"]
                if self.publisher is not None:
                    self.publisher.publish(key)
                return result
            def reserve(state):
                agent = state["agent"]
                agent.update(in_progress={"stage": stage, "epoch": epoch}, sequence=agent["sequence"] + 1,
                             active_seconds=agent["active_seconds"] + reserved, status="running", reason=None)
            self._change(key, "agent_step_reserved", reserve)
        try:
            require(not cancellation.is_set(), "AGENT_CANCELLED")
            if stage == "verification":
                with self._monitor(key, stage, epoch, cancellation, deadline) as revoked:
                    results = self._checks(key, cancellation)
                    require(not revoked, revoked[0] if revoked else "AGENT_BASIS_CHANGED")
                    self._guard(key, stage, epoch=epoch)
                    require(not cancellation.is_set(), "AGENT_CANCELLED")
                if any(r["status"] != "succeeded" for r in results):
                    self._repair(key, count=False)
                else:
                    digest = results[-1]["source_digest"]
                    require(self.files.state().digest == digest, "WORKSPACE_CHANGED")
                    self._change(key, "agent_verified", lambda s: s["agent"].update(verified_digest=digest))
                    self._transition(key, "review")
            else:
                self._model_round(key, stage, cancellation, epoch, deadline)
            self._change(key, "agent_step_completed", lambda s: s["agent"].update(
                status="queued" if s["agent"]["status"] == "running" else s["agent"]["status"]))
        except AgentError as error:
            self._change(key, "agent_blocked", lambda s: s["agent"].update(status="blocked", reason=error.code))
        except Exception:
            self._change(key, "agent_blocked", lambda s: s["agent"].update(status="blocked", reason="AGENT_INTERNAL_ERROR"))
        # BaseException/process loss keeps the durable reservation, including the
        # consumed budget. A normal exception never silently reruns a tool.
        elapsed = max(0, math.ceil(self.clock() - started))
        try:
            diff = self.files.export_changes()
        except AgentError as error:
            diff = {"unavailable": error.code, "base_digest": self.files.workspace.digest}
        def settle(state):
            agent = state["agent"]
            agent.update(in_progress=None, active_seconds=agent["active_seconds"] + elapsed - reserved, diff=diff)
            if agent["active_seconds"] > self.limits.active_seconds:
                agent.update(status="blocked", reason="AGENT_ACTIVE_BUDGET")
        result = self._change(key, "agent_checkpoint", settle)["agent"]
        if self.publisher is not None:
            self.publisher.publish(key)
        return result

    def recover(self, key):
        """Re-observe command effects; never reissue model calls or file patches."""
        state = self.store.read(key)
        require(state is not None and "agent" in state, "AGENT_NOT_STARTED")
        if active_execution(state):
            self.coordinator.recover(key, state["execution"]["run_id"])
        try:
            diff = self.files.export_changes()
        except AgentError as error:
            diff = {"unavailable": error.code}
        return self._change(key, "agent_recovery_checkpoint", lambda s: s["agent"].update(
            status="blocked", reason="AGENT_RECOVERY_REQUIRED", diff=diff))["agent"]

    def resume(self, key):
        """Resume a stopped, fully settled step only after current human gates pass."""
        with self.store.locked(key):
            state = self.store.read(key)
            agent = state["agent"]
            require(agent["reason"] in {"AGENT_PAUSED", "AGENT_BASIS_CHANGED", "MODEL_CANCELLED", "AGENT_CANCELLED"}
                    and not agent["in_progress"] and not active_execution(state), "AGENT_RESUME_DENIED")
            self._guard(key, agent["stage"])
            self.sessions._idle(self.sessions.inspect(key, agent["definition"]["run_id"]))
            return self._change(key, "agent_resumed", lambda s: s["agent"].update(status="queued", reason=None))["agent"]
