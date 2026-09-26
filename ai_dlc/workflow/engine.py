"""Durable pre-implementation workflow, independent of language and model.

This module authorizes *readiness*, and never launches tools, writes GitHub, or
declares full SDLC success. A future dispatcher must recheck the gate under the
same task lock immediately before recording/starting a tool intent.
"""

from copy import deepcopy
from dataclasses import dataclass

from .. import validation as v
from ..config.types import RepositoryConfig
from ..errors import AgentError
from ..storage import FileJournal, TaskKey
from .commands import parse_command
from .observations import CommentObservation, IssueObservation, ObservationGateway

ACTIVE_EXECUTION_STATES = frozenset({"reserved", "dispatching", "running", "uncertain"})


def active_execution(state):
    return (state.get("execution") or {}).get("status") in ACTIVE_EXECUTION_STATES


def _text(value, field, *, empty=False):
    if (type(value) is not str or (not empty and not value.strip()) or len(value.encode("utf-8")) > 128 * 1024
            or any(ord(c) < 32 and c not in "\t\r\n" for c in value)):
        raise AgentError("WORKFLOW_CONTENT", "Expected bounded document text without control characters.", field=field)
    return value


def _document(raw, *, design=False):
    fields = {"summary", "changes", "validation_plan", "open_questions"} if design else {
        "summary", "scope", "acceptance_criteria", "open_questions"}
    v.obj(raw, "document", fields, set() if design else {"split_proposals"})
    _text(raw["summary"], "summary")
    for field in fields - {"summary"}:
        for item in v.array(raw[field], field, nonempty=field != "open_questions"):
            _text(item, field)
    if "split_proposals" in raw:
        for item in v.array(raw["split_proposals"], "split_proposals"):
            _text(item, "split_proposals")


def _settle(state):
    if state["requirements"] is None:
        phase, status = "requirements", "queued"
    elif state["requirement_approval"] is None or not state["started"]:
        phase, status = "requirement_gate", "waiting_human"
    elif state["design"] is None:
        phase, status = "design", "queued"
    elif state["design"]["has_open_questions"] or (state["design_mode"] == "collaborative" and state["design_approval"] is None):
        phase, status = "design", "waiting_human"
    else:
        phase, status = "implementation", "queued"
    state["phase"] = phase
    execution = state.get("execution") or {}
    if active_execution(state):
        status = ("cancel_requested" if state.get("cancel_requested") else "blocked" if execution["status"] == "uncertain"
                  else "queued" if execution["status"] == "reserved" else "running")
    else:
        if state.get("cancel_requested"):
            state.update(cancelled=True, cancel_requested=False)
        if (execution.get("status") in {"failed", "stale"} and execution.get("epoch") == state["cancellation_epoch"]
                and phase == "implementation"):
            status = "failed"
        status = "cancelled" if state["cancelled"] else "paused" if state["paused"] else status
    state["status"] = status
    return state


def _reset_scope(state):
    state.update(requirements=None, requirement_approval=None, started=False, start_approval=None,
                 design=None, design_approval=None, cancellation_epoch=state["cancellation_epoch"] + 1)


@dataclass(frozen=True)
class GateCheck:
    state_revision: int
    cancellation_epoch: int
    requirements_revision: str
    design_revision: str
    config_digest: str


class WorkflowEngine:
    def __init__(self, store: FileJournal, repository: RepositoryConfig, gateway: ObservationGateway):
        self.store, self.repository, self.gateway = store, repository, gateway

    def _key(self, key):
        if (key.instance_id, key.repository_id) != (self.repository.instance_id, self.repository.repository_id):
            raise AgentError("TASK_REPOSITORY", "Task does not belong to the configured repository.")
        if not self.repository.enabled:
            raise AgentError("REPOSITORY_DISABLED", "Repository is disabled.")

    def _state(self, state, *, terminal=False, policy=True):
        if state is None:
            raise AgentError("TASK_MISSING", "Capture the original Issue before processing this task.")
        if policy and state["config_digest"] != self.repository.digest:
            raise AgentError("POLICY_CHANGED", "Repository policy changed; refresh the task and its approvals.")
        if state["cancelled"] and not terminal:
            raise AgentError("TASK_TERMINAL", "A cancelled task cannot be restarted.")
        if state.get("cancel_requested") and not terminal:
            raise AgentError("TASK_CANCELLING", "Cancellation must be reconciled before any further work.")

    def _issue(self, key):
        observation = self.gateway.issue(key)
        if type(observation) is not IssueObservation or observation.task != key:
            raise AgentError("OBSERVATION_INVALID", "Issue observation does not match the requested task.")
        v.integer(observation.author_id, "author_id")
        _text(observation.title, "title")
        _text(observation.body, "body", empty=True)
        v.string(observation.version, "version")
        v.boolean(observation.open, "open")
        if observation.author_type != "User":
            raise AgentError("ISSUE_ACTOR", "Only human-authored Issues may enter this workflow.")
        return observation

    def _source_current(self, key, state):
        observation = self._issue(key)
        if not observation.open:
            raise AgentError("ISSUE_CLOSED", "The Issue is closed; no new work may start.")
        if not self._same_source(key, state["source_digest"], observation.document()):
            raise AgentError("SOURCE_CHANGED", "Issue source changed; preserve and normalize the new revision first.")

    def _same_source(self, key, digest, observed):
        # GitHub's issue updated_at also changes on status comments/labels.
        # Preserve every raw observation, but only source-content changes revoke
        # approval; otherwise the Agent's own status comment invalidates its gate.
        previous = self.store.blob(key, digest)
        return ({k: value for k, value in previous.items() if k != "version"}
                == {k: value for k, value in observed.items() if k != "version"})

    def _comment(self, key, comment_id):
        v.integer(comment_id, "comment_id")
        observation = self.gateway.comment(key, comment_id)
        if observation is None:
            raise AgentError("APPROVAL_REVOKED", "The original command comment no longer exists.")
        if type(observation) is not CommentObservation or observation.task != key or observation.comment_id != comment_id:
            raise AgentError("OBSERVATION_INVALID", "Comment observation does not match the requested task.")
        v.integer(observation.actor_id, "actor_id")
        _text(observation.body, "comment")
        v.string(observation.created_at, "created_at")
        v.string(observation.updated_at, "updated_at")
        if observation.actor_type != "User":
            raise AgentError("COMMAND_ACTOR", "Only a verified human comment may issue workflow commands.")
        if observation.created_at != observation.updated_at:
            raise AgentError("COMMAND_EDITED", "Edited comments cannot be used as new commands or approval evidence.")
        return observation

    def _role(self, key, actor_id, role):
        permission = self.gateway.permission(key, actor_id)
        if permission not in {"write", "maintain", "admin"}:
            raise AgentError("COMMAND_FORBIDDEN", "Current repository write permission is required.")
        if role != "contributor" and actor_id not in self.repository.roles[role]:
            raise AgentError("COMMAND_FORBIDDEN", "The actor is not in the configured role allowlist.")

    def _proof(self, observation, roles):
        for role in roles:
            self._role(observation.task, observation.actor_id, role)
        return {"comment_id": observation.comment_id, "actor_id": observation.actor_id,
                "observation_digest": v.canonical_digest(observation.document()), "roles": list(roles)}

    def _proof_current(self, key, proof):
        if proof is None:
            raise AgentError("APPROVAL_REQUIRED", "A required human approval is missing.")
        observation = self._comment(key, proof["comment_id"])
        if v.canonical_digest(observation.document()) != proof["observation_digest"]:
            raise AgentError("APPROVAL_REVOKED", "The original approval evidence changed.")
        for role in proof["roles"]:
            self._role(key, observation.actor_id, role)

    def _basis(self, key, state, *, design=False):
        self._source_current(key, state)
        self._proof_current(key, state["requirement_approval"])
        if not state["started"]:
            raise AgentError("START_REQUIRED", "Explicit development start is still required.")
        if state["start_approval"] is not None:
            self._proof_current(key, state["start_approval"])
        if design and state["design_mode"] == "collaborative":
            self._proof_current(key, state["design_approval"])

    def _commit(self, key, expected_revision, event_id, event, reducer, blobs=()):
        self._key(key)
        def reduce(state):
            next_state = reducer(state)
            next_state["state_revision"] = expected_revision + 1
            return _settle(next_state)
        return self.store.commit(key, expected_revision=expected_revision, event_id=event_id,
                                 event=event, reduce=reduce, blobs=blobs)

    def capture_source(self, key: TaskKey, *, expected_revision: int, event_id: str):
        self._key(key)
        observation = self._issue(key)
        blob = observation.document()
        digest = v.canonical_digest(blob)
        def reduce(state):
            if state is None:
                state = {"schema_version": 1, "config_digest": self.repository.digest,
                         "source_digest": digest, "original_source_digest": digest, "source_revision": 0,
                         "requirements_count": 0, "design_count": 0, "amendments": [],
                         "paused": False, "cancelled": False, "cancel_requested": False, "cancellation_epoch": 0,
                         "start_policy": self.repository.workflow.start_policy,
                         "design_mode": self.repository.workflow.design_mode}
                _reset_scope(state)
            else:
                self._state(state)
                if not self._same_source(key, state["source_digest"], blob):
                    _reset_scope(state)
                    state["amendments"] = []
            changed = state["source_revision"] == 0 or not self._same_source(key, state["source_digest"], blob)
            if changed:
                state["source_revision"] += 1
                state["source_digest"] = digest
            state["source_observation_digest"] = digest
            if not observation.open:
                state["cancel_requested"] = active_execution(state)
                state["cancelled"] = not active_execution(state)
            return state
        return self._commit(key, expected_revision, event_id, {"kind": "source_observed", "digest": digest}, reduce, (blob,))

    def normalize(self, key: TaskKey, document: dict, *, expected_revision: int, event_id: str):
        document = deepcopy(document)
        _document(document)
        digest = v.canonical_digest(document)
        def reduce(state):
            self._state(state)
            self._source_current(key, state)
            _reset_scope(state)
            state["requirements_count"] += 1
            state["requirements"] = {"revision": f"req-{state['requirements_count']:04d}", "digest": digest,
                                     "source_digest": state["source_digest"], "amendments": list(state["amendments"]),
                                     "has_open_questions": bool(document["open_questions"])}
            return state
        return self._commit(key, expected_revision, event_id, {"kind": "requirements_revised", "digest": digest}, reduce, (document,))

    def propose_design(self, key: TaskKey, document: dict, *, requirements_revision: str, expected_revision: int, event_id: str):
        document = deepcopy(document)
        _document(document, design=True)
        digest = v.canonical_digest(document)
        def reduce(state):
            self._state(state)
            self._basis(key, state)
            if state["requirements"]["revision"] != requirements_revision:
                raise AgentError("REVISION_STALE", "Design must reference the current approved requirements.")
            state["design_count"] += 1
            state["design"] = {"revision": f"des-{state['design_count']:04d}", "digest": digest,
                               "requirements_revision": requirements_revision,
                               "has_open_questions": bool(document["open_questions"])}
            state["design_approval"] = None
            state["cancellation_epoch"] += 1
            return state
        event = {"kind": "design_revised", "digest": digest, "requirements_revision": requirements_revision}
        return self._commit(key, expected_revision, event_id, event, reduce, (document,))

    def _override(self, state, name, value):
        field = "start_policy" if name == "start" else "design_mode"
        allowed = self.repository.workflow.allowed_start_overrides if name == "start" else self.repository.workflow.allowed_design_overrides
        if value != state[field] and value not in allowed:
            raise AgentError("OVERRIDE_FORBIDDEN", "This Issue policy override is not permitted by the repository.")
        state[field] = value

    def handle_comment(self, key: TaskKey, comment_id: int, *, expected_revision: int):
        self._key(key)
        observation = self._comment(key, comment_id)
        command = parse_command(observation.body)
        if command is None:
            raise AgentError("COMMAND_IGNORED", "The first nonblank line is not an explicit workflow command.")
        if command.name == "status":
            # Read permissions belong to the future authenticated GitHub/web layer.
            raise AgentError("COMMAND_READ_ONLY", "Use the authenticated status reader for this command.")
        digest = v.canonical_digest(observation.document())
        def reduce(state):
            name = command.name
            self._state(state, policy=name not in {"stop", "cancel"})
            if name not in {"stop", "cancel"}:
                self._source_current(key, state)
            if name == "approve_requirements":
                req = state["requirements"]
                if req is None or command.revision != req["revision"]:
                    raise AgentError("REVISION_STALE", "Approval must name the current requirements revision.")
                if req["has_open_questions"]:
                    raise AgentError("QUESTIONS_OPEN", "Resolve the requirements questions before approving.")
                if state["requirement_approval"] is not None:
                    raise AgentError("ALREADY_APPROVED", "Requirements already have approval; revise or reconcile before changing it.")
                roles = ["requirement_approver"]
                if (command.options.get("start", state["start_policy"]) == "on_approval"
                        and self.repository.workflow.start_policy != "on_approval"):
                    roles.append("starter")
                proof = self._proof(observation, roles)
                for option, value in command.options.items():
                    self._override(state, option, value)
                state["requirement_approval"] = proof
                state["started"] = state["start_policy"] == "on_approval"
            elif name == "start":
                self._proof_current(key, state["requirement_approval"])
                if command.revision != state["requirements"]["revision"]:
                    raise AgentError("REVISION_STALE", "Start must name the current approved requirements.")
                if state["started"]:
                    raise AgentError("ALREADY_STARTED", "This revision has already been started.")
                state["start_approval"] = self._proof(observation, ["starter"])
                for option, value in command.options.items():
                    self._override(state, option, value)
                state["started"] = True
            elif name == "approve_design":
                self._basis(key, state)
                design = state["design"]
                if design is None or command.revision != design["revision"]:
                    raise AgentError("REVISION_STALE", "Approval must name the current design revision.")
                if design["has_open_questions"]:
                    raise AgentError("QUESTIONS_OPEN", "Resolve the design questions before approving.")
                if state["design_mode"] != "collaborative":
                    raise AgentError("DESIGN_AUTOMATIC", "This task uses automatic design within approved scope.")
                state["design_approval"] = self._proof(observation, ["design_approver"])
            elif name == "amend":
                self._proof(observation, ["contributor"])
                if state["requirements"] is None or command.revision != state["requirements"]["revision"]:
                    raise AgentError("REVISION_STALE", "Amendment must name the current requirements revision.")
                state["amendments"].append(digest)
                _reset_scope(state)
            elif name in {"stop", "cancel", "resume"}:
                self._proof(observation, ["operator"])
                if name == "resume":
                    if not state["paused"]:
                        raise AgentError("TASK_NOT_PAUSED", "Only a paused task may resume.")
                    if state["requirement_approval"] is not None:
                        self._proof_current(key, state["requirement_approval"])
                    if state["start_approval"] is not None:
                        self._proof_current(key, state["start_approval"])
                    if state["design_approval"] is not None:
                        self._proof_current(key, state["design_approval"])
                    state["paused"] = False
                else:
                    state["paused"] = name == "stop"
                    state["cancel_requested"] = name == "cancel" and active_execution(state)
                    state["cancelled"] = name == "cancel" and not active_execution(state)
                state["cancellation_epoch"] += 1
            return state
        return self._commit(key, expected_revision, f"comment-{comment_id}",
                            {"kind": "human_command", "digest": digest}, reduce, (observation.document(),))

    def reconcile(self, key: TaskKey, *, expected_revision: int, event_id: str):
        """Record revoked approvals. Transport/observation failures never grant work."""
        def reduce(state):
            self._state(state)
            self._source_current(key, state)
            revoked = []
            for field in ("requirement_approval", "start_approval", "design_approval"):
                if state[field] is not None:
                    try:
                        self._proof_current(key, state[field])
                    except AgentError as error:
                        if error.code not in {"APPROVAL_REVOKED", "COMMAND_EDITED", "COMMAND_FORBIDDEN", "COMMAND_ACTOR"}:
                            raise
                        revoked.append(field)
            if "requirement_approval" in revoked:
                state.update(requirement_approval=None, started=False, start_approval=None, design_approval=None)
            elif "start_approval" in revoked:
                state.update(start_approval=None, started=False, design_approval=None)
            if "design_approval" in revoked:
                state["design_approval"] = None
            if revoked:
                state["cancellation_epoch"] += 1
            return state
        return self._commit(key, expected_revision, event_id, {"kind": "approvals_reconciled"}, reduce)

    def refresh_policy(self, key: TaskKey, *, expected_revision: int, event_id: str):
        def reduce(state):
            self._state(state, policy=False)
            if state["config_digest"] != self.repository.digest:
                _reset_scope(state)
                state["config_digest"] = self.repository.digest
                state["start_policy"] = self.repository.workflow.start_policy
                state["design_mode"] = self.repository.workflow.design_mode
            return state
        return self._commit(key, expected_revision, event_id,
                            {"kind": "policy_refreshed", "config_digest": self.repository.digest}, reduce)

    def implementation_gate(self, key: TaskKey, *, expected_revision: int, cancellation_epoch: int) -> GateCheck:
        """Fresh gate observation, not a reusable execution token or a tool call."""
        v.integer(expected_revision, "expected_revision")
        v.integer(cancellation_epoch, "cancellation_epoch", minimum=0)
        self._key(key)
        with self.store.locked(key):
            self.store.assert_healthy()
            state = self.store.recover(key)
            self._state(state)
            if state["state_revision"] != expected_revision or state["cancellation_epoch"] != cancellation_epoch:
                raise AgentError("STATE_CONFLICT", "The work basis changed; discard the previous gate result.")
            if active_execution(state) or state["paused"] or state["status"] != "queued" or state["phase"] != "implementation":
                raise AgentError("GATE_CLOSED", "Implementation is not ready under the current task state.")
            self._basis(key, state, design=True)
            return GateCheck(state["state_revision"], state["cancellation_epoch"],
                             state["requirements"]["revision"], state["design"]["revision"], state["config_digest"])

    def prepare_repair(self, key: TaskKey, run_id: str, *, expected_revision: int, event_id: str):
        """Release a confirmed failed test attempt for a bounded, approved repair.

        The old execution and evidence remain immutable in the journal. Unknown,
        stale, mutated-source, interrupted and unverified processes cannot enter
        this path. The agent coordinator separately enforces repair budgets.
        """
        def reduce(state):
            self._state(state)
            self._basis(key, state, design=True)
            execution = state.get("execution") or {}
            if (state["paused"] or execution.get("run_id") != run_id
                    or execution.get("status") != "failed"
                    or execution.get("epoch") != state["cancellation_epoch"]
                    or execution.get("reason") not in {"COMMAND_EXIT", "JUNIT_FAILED_OR_EMPTY"}
                    or not (execution.get("result") or {}).get("process_tree_stopped")):
                raise AgentError("REPAIR_DENIED", "Only a confirmed test failure can enter approved repair.")
            state["execution"] = None
            return state
        return self._commit(key, expected_revision, event_id,
                            {"kind": "repair_requested", "execution_run_id": run_id}, reduce)
