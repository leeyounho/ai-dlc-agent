"""Commit-before-dispatch command execution with conservative crash recovery."""

from copy import deepcopy
import re
import time

from .. import validation as v
from ..errors import AgentError
from ..storage import TaskKey
from ..workflow.engine import WorkflowEngine, active_execution, _settle
from .junit import collect_junit, report_paths
from .ports import ExecutionPlan, ProcessResult, RunnerPort, UnconfiguredRunner


class ExecutionCoordinator:
    def __init__(self, workflow: WorkflowEngine, runner: RunnerPort | None = None, *, termination_grace_seconds: int = 10):
        self.workflow, self.store = workflow, workflow.store
        self.runner = runner or UnconfiguredRunner()
        self.termination_grace_seconds = v.integer(termination_grace_seconds, "termination_grace_seconds")

    def _existing(self, key, run_id):
        found = None
        for record in self.store.history(key):
            if record["event"].get("run_id") == run_id and record["event"].get("kind", "").startswith("execution_"):
                found = deepcopy(record["state"]["execution"])
        return found

    def load_plan(self, key: TaskKey, run_id: str) -> ExecutionPlan:
        previous = self._existing(key, run_id)
        if previous is None:
            raise AgentError("RUN_MISSING", "There is no recorded execution plan for this run.")
        return ExecutionPlan.from_document(self.store.blob(key, previous["plan_digest"]))

    def _commit(self, key, run_id, status, update):
        # Recording observed effects must remain possible after repo disable or
        # policy revocation. This method grants no new execution permission.
        with self.store.locked(key):
            state = self.store.read(key)
            def reduce(current):
                if current is None or current.get("execution", {}).get("run_id") != run_id:
                    raise AgentError("RUN_CONFLICT", "The active command is not the requested run.")
                current["execution"].update(update, status=status)
                current["state_revision"] = state["state_revision"] + 1
                return _settle(current)
            return self.store.commit(key, expected_revision=state["state_revision"], event_id=run_id + "-" + status,
                        event={"kind": "execution_" + status, "run_id": run_id, "update": update}, reduce=reduce)

    def reserve(self, key: TaskKey, run_id: str, plan: ExecutionPlan, *, expected_revision: int, cancellation_epoch: int) -> dict:
        v.identifier(run_id, "run_id")
        if len(run_id) > 50:
            raise AgentError("RUN_ID", "Run identifier is too long.")
        document = plan.document()
        digest = v.canonical_digest(document)
        with self.store.locked(key):
            self.workflow._key(key)
            previous = self._existing(key, run_id)
            if previous is not None:
                if previous["plan_digest"] != digest:
                    raise AgentError("RUN_COLLISION", "Run identifier was reused for another execution plan.")
                return previous
            gate = self.workflow.implementation_gate(key, expected_revision=expected_revision, cancellation_epoch=cancellation_epoch)
            project = self.workflow.repository.project
            if project.toolchain_id != plan.toolchain_id or project.commands.get(plan.command_id) != plan.command:
                raise AgentError("COMMAND_NOT_REGISTERED", "Execution must use the configured repository command and toolchain.")
            runtime_digest = self.runner.preflight(plan)
            self._digest(runtime_digest)
            plan.workspace.verify()
            if plan.verification == "junit" and report_paths(plan.workspace.root, plan.command.report_patterns):
                raise AgentError("STALE_REPORTS", "Prepare a clean workspace; preexisting reports cannot verify this run.")
            execution = {"run_id": run_id, "status": "reserved", "plan_digest": digest, "runtime_digest": runtime_digest,
                         "epoch": gate.cancellation_epoch, "requirements_revision": gate.requirements_revision,
                         "design_revision": gate.design_revision, "config_digest": gate.config_digest,
                         "identity": None, "result": None, "verification": None, "reason": None}
            def reduce(state):
                state["execution"] = execution
                return state
            commit = self.workflow._commit(key, expected_revision, run_id + "-reserved",
                        {"kind": "execution_reserved", "run_id": run_id, "plan_digest": digest,
                         "runtime_digest": runtime_digest}, reduce, (document,))
            self.store.assert_healthy()
            return commit.state["execution"]

    @staticmethod
    def _digest(value):
        if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise AgentError("RUNNER_PROTOCOL", "Runner must supply a valid content digest.")

    def _basis(self, key, state, execution):
        self.workflow._key(key)
        self.workflow._state(state)
        if (state["paused"] or state["cancellation_epoch"] != execution["epoch"]
                or state["config_digest"] != execution["config_digest"]
                or state["requirements"] is None or state["design"] is None
                or state["requirements"]["revision"] != execution["requirements_revision"]
                or state["design"]["revision"] != execution["design_revision"]):
            raise AgentError("RUN_BASIS_CHANGED", "Execution no longer has the original approval and cancellation basis.")
        self.workflow._basis(key, state, design=True)

    def _result(self, result):
        if type(result) is not ProcessResult or type(result.exit_code) is not int:
            raise AgentError("RUNNER_PROTOCOL", "Runner returned an invalid process outcome.")
        v.enum(result.termination, {"completed", "cancelled", "timeout", "output_limit"}, "termination")
        for digest in (result.stdout_sha256, result.stderr_sha256):
            self._digest(digest)
        v.integer(result.stdout_bytes, "stdout_bytes", minimum=0)
        v.integer(result.stderr_bytes, "stderr_bytes", minimum=0)
        v.boolean(result.process_tree_stopped, "process_tree_stopped")
        if not result.process_tree_stopped:
            raise AgentError("RUN_UNCERTAIN", "Process-tree termination has not been confirmed.")

    def _finish(self, key, run_id, plan, result, *, reason=None):
        with self.store.locked(key):
            state = self.store.read(key)
            execution = state["execution"]
            if execution["run_id"] != run_id:
                raise AgentError("RUN_CONFLICT", "Another run occupies this task.")
            if not active_execution(state):
                return deepcopy(execution)
            try:
                self._result(result)
            except AgentError:
                if execution["status"] == "uncertain":
                    return deepcopy(execution)
                return self._commit(key, run_id, "uncertain", {"reason": "RUNNER_OUTCOME_UNVERIFIED"}).state["execution"]
            status, verification = "failed", None
            try:
                self._basis(key, state, execution)
            except AgentError as error:
                status, reason = "stale", reason or error.code
            else:
                if result.termination != "completed":
                    reason = reason or result.termination.upper()
                elif result.exit_code != 0:
                    reason = "COMMAND_EXIT"
                else:
                    try:
                        plan.workspace.verify(generated_patterns=plan.generated_patterns)
                        if plan.verification == "junit":
                            junit = collect_junit(plan.workspace.root, plan.command.report_patterns)
                            verification = {"type": "junit", **junit.document()}
                            status = "succeeded" if junit.passed else "failed"
                            reason = None if junit.passed else "JUNIT_FAILED_OR_EMPTY"
                        else:
                            verification = {"type": "exit_code", "command_completed": True, "tests_verified": False}
                            status, reason = "succeeded", None
                    except AgentError as error:
                        reason = error.code
            result_document = result.document()
            commit = self._commit(key, run_id, status, {"result": result_document, "verification": verification, "reason": reason})
            self.store.assert_healthy()
            return commit.state["execution"]

    def execute(self, key: TaskKey, run_id: str, plan: ExecutionPlan | None = None) -> dict:
        """Execute one durable reservation; never infer absence from timeout."""
        handle = None
        plan = plan or self.load_plan(key, run_id)
        with self.store.locked(key):
            self.store.assert_healthy()
            previous = self._existing(key, run_id)
            if previous is None or previous["plan_digest"] != v.canonical_digest(plan.document()):
                raise AgentError("RUN_MISSING", "This exact execution plan has not been reserved.")
            if previous["status"] not in {"reserved", "dispatching", "running", "uncertain"}:
                return previous
            if previous["status"] != "reserved":
                return self.recover(key, run_id, plan)
            state = self.store.read(key)
            if state.get("execution", {}).get("run_id") != run_id:
                raise AgentError("RUN_CONFLICT", "Another command occupies this task.")
            try:
                self._basis(key, state, previous)
                if self.runner.preflight(plan) != previous["runtime_digest"]:
                    raise AgentError("RUNTIME_CHANGED", "Runtime changed after execution planning.")
                plan.workspace.verify()
                if plan.verification == "junit" and report_paths(plan.workspace.root, plan.command.report_patterns):
                    raise AgentError("STALE_REPORTS", "Reports appeared after planning and before dispatch.")
            except AgentError as error:
                return self._commit(key, run_id, "failed", {"reason": error.code}).state["execution"]
            self._commit(key, run_id, "dispatching", {})
            self.store.assert_healthy()
            try:
                handle = self.runner.start(run_id, plan)
                identity = handle.identity
                if type(identity) is not dict or identity.get("run_id") != run_id:
                    raise AgentError("RUNNER_PROTOCOL", "Runner identity does not match the command intent.")
                self._commit(key, run_id, "running", {"identity": identity})
                self.store.assert_healthy()
            except Exception:
                if handle is not None:
                    try:
                        handle.cancel("cancelled")
                    except Exception:
                        pass  # Termination is unconfirmed; preserve uncertainty below.
                # If the launch response was lost, the dispatcher cannot prove no
                # process exists. A damaged store also prevents this write.
                self._commit(key, run_id, "uncertain", {"reason": "LAUNCH_OUTCOME_UNKNOWN"})
                return self.store.read(key)["execution"]
            except BaseException:
                if handle is not None:
                    try:
                        handle.cancel("cancelled")
                    except Exception:
                        pass
                raise
        deadline = time.monotonic() + plan.command.timeout_seconds
        reason, cancelled_at = None, None
        try:
            while True:
                result = handle.poll()
                if result is not None:
                    return self._finish(key, run_id, plan, result, reason=reason)
                if cancelled_at is None:
                    try:
                        with self.store.locked(key):
                            self.store.assert_healthy()
                            self._basis(key, self.store.read(key), previous)
                    except AgentError as error:
                        reason = error.code
                    if reason is not None or time.monotonic() >= deadline:
                        handle.cancel("cancelled" if reason else "timeout")
                        reason = reason or "TIMEOUT"
                        cancelled_at = time.monotonic()
                elif time.monotonic() - cancelled_at > self.termination_grace_seconds:
                    return self._commit(key, run_id, "uncertain", {"reason": "TERMINATION_UNCONFIRMED"}).state["execution"]
                time.sleep(0.02)
        except Exception:
            with self.store.locked(key):
                state = self.store.read(key)
                if active_execution(state) and state["execution"]["status"] != "uncertain":
                    return self._commit(key, run_id, "uncertain", {"reason": "PROCESS_OBSERVATION_FAILED"}).state["execution"]
                raise
        finally:
            try:
                pending = handle.poll() is None
            except Exception:
                pending = True
            if pending:
                try:
                    handle.cancel("cancelled")
                except Exception:
                    pass  # A confirmed stopped tree is required by recovery.

    def recover(self, key: TaskKey, run_id: str, plan: ExecutionPlan | None = None) -> dict:
        plan = plan or self.load_plan(key, run_id)
        with self.store.locked(key):
            self.store.assert_healthy()
            previous = self._existing(key, run_id)
            if previous is None or previous["plan_digest"] != v.canonical_digest(plan.document()):
                raise AgentError("RUN_MISSING", "This exact execution plan is not recorded.")
            if previous["status"] not in {"dispatching", "running", "uncertain"}:
                return previous
            try:
                result = self.runner.inspect(run_id, previous["identity"])
            except Exception:
                result = None
            if result is None:
                if previous["status"] == "uncertain":
                    return previous
                return self._commit(key, run_id, "uncertain", {"reason": "RECOVERY_REQUIRES_INSPECTION"}).state["execution"]
            return self._finish(key, run_id, plan, result)
