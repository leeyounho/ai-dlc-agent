"""Bounded in-process scheduler with repository round-robin fairness."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import inspect
import threading
import time
from typing import Callable

from ..errors import AgentError


@dataclass(frozen=True)
class ScheduledWork:
    work_id: str
    repository_id: int
    handler: Callable
    kind: str = "control"
    provider_id: str | None = None
    active_timeout_seconds: float | None = None

    def __post_init__(self):
        if (type(self.work_id) is not str or not self.work_id
                or type(self.repository_id) is not int or self.repository_id < 0
                or self.kind not in {"control", "model", "run"}
                or (self.kind == "model" and not self.provider_id)
                or (self.active_timeout_seconds is not None
                    and (type(self.active_timeout_seconds) not in {int, float}
                         or self.active_timeout_seconds <= 0))
                or not callable(self.handler)):
            raise AgentError("SCHEDULER_WORK", "Scheduled work is invalid.")


@dataclass(frozen=True)
class WorkContext:
    work_id: str
    started_monotonic: float
    deadline_monotonic: float | None
    cancel_event: threading.Event
    clock: Callable[[], float] = time.monotonic

    @property
    def cancellation_requested(self) -> bool:
        return self.cancel_event.is_set()

    def remaining_seconds(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return max(0.0, self.deadline_monotonic - self.clock())


@dataclass
class _Active:
    work: ScheduledWork
    context: WorkContext
    future: object
    timed_out: bool = False


class FairScheduler:
    """FIFO within each repository and round-robin between repositories.

    Waiting work consumes no active-time budget.  A timeout is cooperative: it
    requests cancellation and keeps the capacity occupied until the handler is
    observed stopped, so an overrun can never be reported as successful.
    """

    def __init__(self, *, global_limit: int, repository_limit: int, model_limit: int,
                 provider_limits: dict[str, int] | None = None, clock=time.monotonic):
        for value in (global_limit, repository_limit, model_limit):
            if type(value) is not int or value < 1:
                raise AgentError("SCHEDULER_LIMIT", "Concurrency limits must be positive integers.")
        provider_limits = dict(provider_limits or {})
        if any(type(key) is not str or not key or type(value) is not int or value < 1
               for key, value in provider_limits.items()):
            raise AgentError("SCHEDULER_LIMIT", "Provider concurrency limits are invalid.")
        self.global_limit = global_limit
        self.repository_limit = repository_limit
        self.model_limit = model_limit
        self.provider_limits = provider_limits
        self.clock = clock
        self._queues: dict[int, deque[ScheduledWork]] = {}
        self._rotation = deque()
        self._active: dict[str, _Active] = {}
        self._known = set()
        self._results: dict[str, dict] = {}
        self._accepting = True
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=global_limit, thread_name_prefix="ai-dlc")

    @property
    def accepting(self):
        return self._accepting

    def submit(self, work: ScheduledWork) -> bool:
        with self._lock:
            if not self._accepting:
                raise AgentError("SCHEDULER_CLOSED", "The scheduler is no longer accepting work.")
            if work.work_id in self._known:
                return False
            self._known.add(work.work_id)
            self._results.pop(work.work_id, None)
            queue = self._queues.setdefault(work.repository_id, deque())
            queue.append(work)
            if work.repository_id not in self._rotation:
                self._rotation.append(work.repository_id)
            return True

    def close_intake(self):
        with self._lock:
            self._accepting = False

    def _counts(self):
        repositories, providers, models = {}, {}, 0
        for active in self._active.values():
            work = active.work
            repositories[work.repository_id] = repositories.get(work.repository_id, 0) + 1
            if work.kind == "model":
                models += 1
                providers[work.provider_id] = providers.get(work.provider_id, 0) + 1
        return repositories, providers, models

    def _eligible(self, work, repositories, providers, models):
        if len(self._active) >= self.global_limit:
            return False, "GLOBAL_CONCURRENCY_LIMIT"
        if repositories.get(work.repository_id, 0) >= self.repository_limit:
            return False, "REPOSITORY_CONCURRENCY_LIMIT"
        limit = self.provider_limits.get(work.provider_id)
        if work.kind == "model" and limit is not None and providers.get(work.provider_id, 0) >= limit:
            return False, "PROVIDER_CONCURRENCY_LIMIT"
        if work.kind == "model" and models >= self.model_limit:
            return False, "MODEL_CONCURRENCY_LIMIT"
        return True, None

    @staticmethod
    def _invoke(work, context):
        try:
            parameters = inspect.signature(work.handler).parameters
        except (TypeError, ValueError):
            parameters = {"context": None}
        return work.handler() if not parameters else work.handler(context)

    def _reap(self):
        for work_id, active in tuple(self._active.items()):
            if (active.context.deadline_monotonic is not None
                    and self.clock() >= active.context.deadline_monotonic
                    and not active.future.done()):
                active.timed_out = True
                active.context.cancel_event.set()
            if not active.future.done():
                continue
            try:
                value = active.future.result()
                self._results[work_id] = ({"status": "timed_out", "next_action": "inspect_and_recover"}
                                          if active.timed_out else {"status": "completed", "result": value})
            except Exception as error:
                result = {"status": "failed", "error_type": type(error).__name__,
                          "next_action": "retry_after_observation"}
                if isinstance(error, AgentError):
                    result["error_code"] = error.code
                self._results[work_id] = result
                # The handler did not commit completion. A durable source such as
                # the webhook inbox may offer the same identity again next cycle.
                self._known.discard(work_id)
            del self._active[work_id]

    def tick(self) -> int:
        with self._lock:
            self._reap()
            dispatched = 0
            skipped = 0
            while self._rotation and len(self._active) < self.global_limit:
                repository_id = self._rotation.popleft()
                queue = self._queues.get(repository_id)
                if not queue:
                    self._queues.pop(repository_id, None)
                    continue
                repositories, providers, models = self._counts()
                work = queue[0]
                eligible, _reason = self._eligible(work, repositories, providers, models)
                self._rotation.append(repository_id)
                if not eligible:
                    skipped += 1
                    if skipped >= len(self._rotation):
                        break
                    continue
                skipped = 0
                queue.popleft()
                if not queue:
                    self._queues.pop(repository_id, None)
                    self._rotation.remove(repository_id)
                started = self.clock()
                deadline = (started + work.active_timeout_seconds
                            if work.active_timeout_seconds is not None else None)
                context = WorkContext(work.work_id, started, deadline, threading.Event(), self.clock)
                future = self._executor.submit(self._invoke, work, context)
                self._active[work.work_id] = _Active(work, context, future)
                dispatched += 1
            return dispatched

    def snapshot(self) -> dict:
        with self._lock:
            self._reap()
            repositories, providers, models = self._counts()
            pending = []
            for repository_id in self._rotation:
                for work in self._queues.get(repository_id, ()):
                    _ok, reason = self._eligible(work, repositories, providers, models)
                    pending.append({"work_id": work.work_id, "repository_id": repository_id,
                                    "kind": work.kind, "waiting_reason": reason or "FAIR_QUEUE",
                                    "next_action": "wait_for_capacity"})
            return {"accepting": self._accepting, "pending": pending,
                    "active": [{"work_id": item.work.work_id,
                                "repository_id": item.work.repository_id,
                                "kind": item.work.kind,
                                "active_seconds": max(0.0, self.clock() - item.context.started_monotonic),
                                "cancellation_requested": item.context.cancellation_requested}
                               for item in self._active.values()],
                    "completed": dict(self._results), "active_by_repository": repositories,
                    "active_models": models, "active_by_provider": providers}

    def acknowledge_completed(self):
        """Release checkpointed result metadata while retaining active identities."""
        with self._lock:
            completed = tuple(self._results)
            self._results.clear()
            for work_id in completed:
                if work_id not in self._active:
                    self._known.discard(work_id)

    def shutdown(self, grace_seconds: float) -> tuple[str, ...]:
        if type(grace_seconds) not in {int, float} or grace_seconds < 0:
            raise AgentError("SCHEDULER_LIMIT", "Shutdown grace must be non-negative.")
        self.close_intake()
        deadline = self.clock() + grace_seconds
        with self._lock:
            for active in self._active.values():
                active.context.cancel_event.set()
        while self.clock() < deadline:
            with self._lock:
                self._reap()
                if not self._active:
                    break
            time.sleep(min(0.01, max(0.0, deadline - self.clock())))
        with self._lock:
            self._reap()
            queued = {work.work_id for queue in self._queues.values() for work in queue}
            unfinished = tuple(sorted({*self._active, *queued}))
            self._queues.clear()
            self._rotation.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
        return unfinished
