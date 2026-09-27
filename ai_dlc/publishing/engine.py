"""Commit durable intents before remote effects; recover only observed identities."""

from dataclasses import asdict
import time
import threading
import uuid

from ..errors import AgentError
from ..models.types import require
from ..validation import canonical_digest
from ..workflow.engine import _settle, active_execution
from .document import marker, render_pull


INVALIDATING = {"PUBLISH_REF_CHANGED", "PUBLISH_BASE_CHANGED", "PUBLISH_PR_CHANGED", "PUBLISH_POLICY_CHANGED",
                "WORKSPACE_CHANGED", "PUBLISH_BASIS_CHANGED", "PUBLISH_CHECK_CHANGED", "APPROVAL_REVOKED",
                "COMMAND_FORBIDDEN", "TASK_TERMINAL", "TASK_CANCELLING", "POLICY_CHANGED", "SOURCE_CHANGED",
                "AGENT_PAUSED", "REPOSITORY_DISABLED", "ISSUE_CLOSED", "APPROVAL_REQUIRED", "START_REQUIRED"}


class PublicationCoordinator:
    def __init__(self, loop, checkout, git, gateway, *, notes):
        self.loop, self.store, self.checkout = loop, loop.store, checkout
        self.git, self.gateway, self.notes = git, gateway, dict(notes)
        require(loop.files.workspace.document() == checkout.workspace.document(), "PUBLISH_WORKSPACE")
        require(loop.source_commit == checkout.revision.commit, "PUBLISH_SOURCE_COMMIT")

    def _change(self, key, kind, update):
        with self.store.locked(key):
            history = self.store.history(key)
            def reduce(state):
                update(state)
                state["state_revision"] = len(history) + 1
                return _settle(state)
            result = self.store.commit(key, expected_revision=len(history), event_id="publish-" + uuid.uuid4().hex,
                                       event={"kind": kind}, reduce=reduce)
            self.store.assert_healthy()
            return result.state["publication"]

    @staticmethod
    def _basis(state):
        return {name: state[name] for name in ("source_digest", "requirements", "design", "requirement_approval",
                "start_approval", "design_approval", "cancellation_epoch", "config_digest")}

    def _gate(self, key):
        state = self.loop._guard(key, "review")
        agent = state["agent"]
        require(agent["status"] == "ready_for_pr" and not agent["in_progress"] and not active_execution(state), "PUBLISH_NOT_READY")
        digest = self.loop.files.state().digest
        require(digest == agent["verified_digest"] == agent["diff"]["workspace_digest"], "WORKSPACE_CHANGED")
        review = agent["review"] or {}
        require(review.get("source_digest") == digest and review.get("advisory") is True
                and review.get("human_approval") is False and review.get("findings") == [], "PUBLISH_REVIEW_REQUIRED")
        latest = {check["command_id"]: check for check in agent["checks"]}
        require(all(c in latest and latest[c]["status"] == "succeeded" and latest[c]["source_digest"] == digest
                    for c in self.loop.command_ids), "PUBLISH_CHECKS_REQUIRED")
        for command_id in self.loop.command_ids:
            check = latest[command_id]
            plan = self.loop.coordinator.load_plan(key, check["run_id"])
            require(plan.workspace.digest == digest and canonical_digest(plan.document()) == check["plan_digest"], "PUBLISH_CHECKS_REQUIRED")
            recorded = self.loop.coordinator._existing(key, check["run_id"])
            require(recorded["status"] == "succeeded" and recorded["result"] == check["result"]
                    and recorded["verification"] == check["verification"], "PUBLISH_CHECKS_REQUIRED")
        publication = state.get("publication")
        if publication:
            if publication.get("candidate"):
                require(publication["candidate"]["source_digest"] == digest, "WORKSPACE_CHANGED")
            require(publication["basis"] == self._basis(state), "PUBLISH_BASIS_CHANGED")
            require(publication["notes_digest"] == canonical_digest(self.notes), "PUBLISH_DOCUMENT_CHANGED")
        return state

    def _remote(self, key, publication):
        policy = self.gateway.policy(key)
        require(canonical_digest(asdict(policy)) == canonical_digest(publication["policy"]), "PUBLISH_POLICY_CHANGED")
        require(policy.default_branch == publication["base"], "PUBLISH_BASE_CHANGED")
        refs = self.git.refs(publication["base"], publication["branch"])
        require(refs["base"] == self.checkout.revision.commit, "PUBLISH_BASE_CHANGED")
        return refs

    def _pull(self, key, publication):
        found = self.gateway.pulls(key, marker(key), publication["branch"])
        require(len(found) <= 1, "PUBLISH_PR_CONFLICT")
        if not found:
            return None
        pull, desired = found[0], publication["document"]
        require(pull["owned"] and pull["state"] == "open" and pull["head"] == publication["candidate"]["commit"]
                and all(pull[field] == desired[field] for field in ("body", "title", "base", "branch")), "PUBLISH_PR_CHANGED")
        if publication.get("pull"):
            require(pull["number"] == publication["pull"]["number"], "PUBLISH_PR_CHANGED")
        return pull

    def _check_document(self, key, publication):
        state = self.store.read(key)
        latest = {check["command_id"]: check for check in state["agent"]["checks"]}
        evidence = [latest[c] for c in self.loop.command_ids]
        tests_verified = all((e.get("verification") or {}).get("passed") is True for e in evidence)
        digest = canonical_digest(evidence)
        return {"name": "AI-DLC source verification", "head_sha": publication["candidate"]["commit"],
            "external_id": "aidlc-" + canonical_digest(key.as_dict()) + "-" + digest[:16],
            "status": "completed", "conclusion": "success" if tests_verified else "neutral",
            "output": {"title": "Recorded source verification; no merge/deployment approval",
                       "summary": "Evidence digest: " + digest + "\nSource digest: " + state["agent"]["verified_digest"]
                                  + "\nTests verified: " + str(tests_verified).lower() + "\nDeployment: not executed"}}

    def _find_check(self, key, publication, *, invalidated=False):
        expected = publication["check_document"]
        found = self.gateway.checks(key, expected["head_sha"], expected["external_id"])
        require(len(found) <= 1, "PUBLISH_CHECK_CONFLICT")
        if not found:
            return None
        check = found[0]
        fields = ("name", "head_sha", "external_id", "status", "conclusion")
        require(all(check[field] == ("action_required" if field == "conclusion" and invalidated else expected[field])
                    for field in fields)
                and all((check.get("output") or {}).get(k) == value for k, value in expected["output"].items()),
                "PUBLISH_CHECK_CHANGED")
        if publication.get("check_id"):
            require(check["id"] == publication["check_id"], "PUBLISH_CHECK_CHANGED")
        return check

    def _invalidate_check(self, key, publication):
        if publication.get("invalidation") == "confirmed" or not publication.get("check_document"):
            return
        if not publication.get("check_id"):
            observed = self._find_check(key, publication)
            if observed is None:
                return
            publication = self._change(key, "check_effect_observed", lambda s: s["publication"].update(check_id=observed["id"]))
        if publication.get("invalidation") == "pending":
            require(self._find_check(key, publication, invalidated=True) is not None, "PUBLISH_EFFECT_UNKNOWN")
        else:
            require(self._find_check(key, publication) is not None, "PUBLISH_CHECK_CHANGED")
            self._change(key, "check_invalidation_intent", lambda s: s["publication"].update(invalidation="pending"))
            self.gateway.invalidate_check(key, publication["check_id"])
            require(self._find_check(key, publication, invalidated=True) is not None, "PUBLISH_EFFECT_UNKNOWN")
        self._change(key, "check_invalidation_confirmed", lambda s: s["publication"].update(invalidation="confirmed"))

    def run(self, key, *, cancellation=None):
        # Publishers share a per-task lock, independent of the workflow lock so
        # human stop/revocation events can settle while Git or HTTP is in flight.
        with self.store.locked(key):
            if not hasattr(self.store, "_publication_locks"):
                self.store._publication_locks = {}
            lock = self.store._publication_locks.setdefault(key, threading.RLock())
        with lock:
            publication = (self.store.read(key) or {}).get("publication")
            if publication and publication.get("invalidated"):
                self._invalidate_check(key, publication)
                return self.store.read(key)["publication"]
            try:
                return self._run(key, cancellation=cancellation)
            except AgentError as error:
                publication = (self.store.read(key) or {}).get("publication")
                if not publication:
                    raise
                invalid = error.code in INVALIDATING
                def failed(state):
                    state["publication"].update(status="blocked", reason=error.code)
                    if invalid:
                        state["publication"]["invalidated"] = True
                        state.update(requirement_approval=None, start_approval=None, design_approval=None, started=False,
                                     cancellation_epoch=state["cancellation_epoch"] + 1)
                        state["agent"].update(status="blocked", reason=error.code)
                publication = self._change(key, "publication_blocked", failed)
                if invalid:
                    self._invalidate_check(key, publication)
                return self.store.read(key)["publication"]

    def _run(self, key, *, cancellation=None):
        def running():
            require(cancellation is None or not cancellation.is_set(), "PUBLISH_CANCELLED")
        running()
        state = self._gate(key)
        publication = state.get("publication")
        if publication is None:
            policy = self.gateway.policy(key)
            branch = self.git.branch(key)
            require(policy.default_branch != branch, "PUBLISH_DEFAULT_BRANCH")
            require(self.git.refs(policy.default_branch, branch) == {"base": self.checkout.revision.commit, "head": None},
                    "PUBLISH_REF_CHANGED")
            require(not self.gateway.pulls(key, marker(key), branch), "PUBLISH_PR_CONFLICT")
            initial = {"operation_id": "pub-" + uuid.uuid4().hex, "timestamp": int(time.time()),
                "stage": "candidate_pending", "status": "running", "reason": None, "basis": self._basis(state),
                "policy": asdict(policy), "base": policy.default_branch, "branch": branch,
                "notes_digest": canonical_digest(self.notes)}
            publication = self._change(key, "candidate_intent", lambda s: s.update(publication=initial))
            running()
            candidate = self.git.build(key, self.checkout, self.loop.files, operation_id=publication["operation_id"],
                base=publication["base"], expected_digest=state["agent"]["verified_digest"], timestamp=publication["timestamp"])
            document = render_pull(self.store, key, candidate, self.notes)
            publication = self._change(key, "candidate_confirmed", lambda s: s["publication"].update(
                candidate=candidate, document=document, stage="candidate_confirmed"))
        require(publication["stage"] != "candidate_pending", "PUBLISH_BUILD_RECOVERY_REQUIRED")
        self._gate(key)
        refs = self._remote(key, publication)
        candidate = publication["candidate"]
        stage = publication["stage"]
        if stage == "candidate_confirmed":
            require(refs["head"] is None, "PUBLISH_REF_CHANGED")
            self._change(key, "push_intent", lambda s: s["publication"].update(stage="push_pending"))
            self._gate(key)
            running()
            self.git.push(key, candidate)
            refs = self._remote(key, publication)
            stage = "push_pending"
        if stage == "push_pending":
            require(refs["head"] is not None, "PUBLISH_EFFECT_UNKNOWN")
            require(refs["head"] == candidate["commit"], "PUBLISH_REF_CHANGED")
            publication = self._change(key, "push_confirmed", lambda s: s["publication"].update(stage="push_confirmed"))
            stage = "push_confirmed"
        require(refs["head"] == candidate["commit"], "PUBLISH_REF_CHANGED")
        pull = self._pull(key, publication)
        if stage == "push_confirmed":
            require(pull is None, "PUBLISH_PR_CONFLICT")
            self._gate(key)
            require(self._remote(key, publication)["head"] == candidate["commit"], "PUBLISH_REF_CHANGED")
            self._change(key, "pr_intent", lambda s: s["publication"].update(stage="pr_pending"))
            self._gate(key)
            running()
            self.gateway.create_pull(key, publication["document"])
            pull = self._pull(key, publication)
            stage = "pr_pending"
        if stage == "pr_pending":
            require(pull is not None, "PUBLISH_EFFECT_UNKNOWN")
            publication = self._change(key, "pr_confirmed", lambda s: s["publication"].update(pull=pull, stage="pr_confirmed"))
            stage = "pr_confirmed"
        require(pull is not None, "PUBLISH_PR_CHANGED")
        if stage == "pr_confirmed":
            self._gate(key)
            require(self._remote(key, publication)["head"] == candidate["commit"], "PUBLISH_REF_CHANGED")
            require(self._pull(key, publication) is not None, "PUBLISH_PR_CHANGED")
            document = self._check_document(key, publication)
            publication = self._change(key, "check_intent", lambda s: s["publication"].update(
                check_document=document, stage="check_pending"))
            require(self._find_check(key, publication) is None, "PUBLISH_CHECK_CONFLICT")
            self._gate(key)
            running()
            self.gateway.create_check(key, document)
            stage = "check_pending"
        if stage in {"check_pending", "complete"}:
            check = self._find_check(key, publication)
            require(check is not None, "PUBLISH_EFFECT_UNKNOWN")
            self._gate(key)
            require(self._remote(key, publication)["head"] == candidate["commit"], "PUBLISH_REF_CHANGED")
            self._pull(key, publication)
            running()
            if stage == "complete":
                if publication["status"] != "published":
                    return self._change(key, "publication_reconfirmed", lambda s: s["publication"].update(
                        status="published", reason=None))
                return publication
            return self._change(key, "publication_complete", lambda s: s["publication"].update(
                check_id=check["id"], stage="complete", status="published", reason=None))
        require(False, "PUBLISH_STATE")
