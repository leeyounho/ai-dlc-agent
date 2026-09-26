"""Explicit trusted task registration for the existing fair service scheduler."""

from ..errors import AgentError
from ..execution.workspace import local_root
from ..service.scheduler import ScheduledWork


class AgentDriver:
    def __init__(self):
        self.tasks = {}
        self.observed = {}

    def register(self, key, loop):
        root = local_root(loop.files.workspace.root)
        for other_key, other in self.tasks.items():
            other_root = local_root(other.files.workspace.root)
            if other_key != key and (root == other_root or root in other_root.parents or other_root in root.parents):
                raise AgentError("AGENT_WORKSPACE_SHARED", "Each task must own a separate mutable workspace.")
        if key in self.tasks and self.tasks[key] is not loop:
            raise AgentError("AGENT_ALREADY_REGISTERED", "A live task cannot silently replace its control runtime.")
        self.tasks[key] = loop

    def schedule(self, scheduler):
        count = 0
        for key, loop in self.tasks.items():
            state = loop.store.read(key)
            agent = (state or {}).get("agent")
            if agent is None or agent["in_progress"]:
                continue
            if agent["status"] in {"blocked", "ready_for_pr"} and not loop.needs_requirements_refresh(state):
                continue
            if self.observed.get(key) == state["state_revision"]:
                continue
            # Human commands change the revision while waiting; avoid posting
            # repeated waits or consuming worker capacity on the same evidence.
            work_id = f"agent-{key.instance_id}-{key.repository_id}-{key.issue_number}-{state['state_revision']}"
            def handle(context, loop=loop, key=key):
                result = loop.step(key, cancellation=context.cancel_event)
                if result["status"] == "waiting_human":
                    with loop.store.locked(key):
                        current = loop.store.read(key)
                        if not loop.needs_requirements_refresh(current):
                            try:
                                loop._guard(key, current["agent"]["stage"])
                            except AgentError as error:
                                if error.code == result["reason"]:
                                    self.observed[key] = current["state_revision"]
                return result
            if scheduler.submit(ScheduledWork(work_id, key.repository_id, handle,
                active_timeout_seconds=loop.limits.active_seconds)):
                count += 1
        return count

    def recover(self, key, state):
        loop = self.tasks.get(key)
        if loop is None:
            return {"status": "blocked", "reason": "AGENT_TASK_RUNTIME_UNAVAILABLE"}
        if (state.get("agent") or {}).get("in_progress"):
            return loop.recover(key)
        return None
