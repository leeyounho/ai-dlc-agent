"""Crash-conscious lifecycle for the single persistent Agent process."""

from copy import deepcopy
from datetime import datetime, timezone
import os
import threading
import time
import uuid

from ..config.loader import inspect_service_readiness
from ..errors import AgentError
from ..storage.journal import _mkdir, _native, _publish
from ..workflow.engine import active_execution
from .scheduler import FairScheduler, ScheduledWork


def _now():
    return datetime.now(timezone.utc).isoformat()


class ServiceRuntime:
    """Own recovery, inbox scheduling, health, checkpoints, and shutdown.

    The caller owns the open ``FileJournal`` context and the optional HTTP
    listener.  This separation keeps the lifecycle testable without sockets.
    """

    def __init__(self, bundle, store, *, processor=None, recovery=None,
                 scheduler: FairScheduler | None = None, environment=None,
                 startup_reasons=(), runner_available=False, model_adapters=(),
                 clock=time.monotonic):
        self.bundle, self.store = bundle, store
        self.processor, self.recovery = processor, recovery
        self.environment = dict(os.environ if environment is None else environment)
        self.startup_reasons = tuple(startup_reasons)
        self.runner_available = bool(runner_available)
        self.model_adapters = frozenset(model_adapters)
        self.clock = clock
        execution = bundle.service.execution
        provider_limits = {provider.id: provider.max_concurrent_requests
                           for provider in bundle.connection.providers.values()}
        self.scheduler = scheduler or FairScheduler(
            global_limit=execution.global_concurrency,
            repository_limit=execution.repository_concurrency,
            model_limit=execution.model_concurrency,
            provider_limits=provider_limits,
            clock=clock,
        )
        self.epoch = uuid.uuid4().hex
        self._started = False
        self._stopping = False
        self._live = False
        self._storage_error = None
        self._recovery_required = []
        self._recovered_tasks = 0
        self._unclean_restart = False
        self._status_path = store.root / "service" / "current.json"
        self._lock = threading.RLock()

    def _read_previous(self):
        if not _native(self._status_path).exists():
            return None
        document = self.store._read(self._status_path)
        if (type(document) is not dict or document.get("schema_version") != 1
                or type(document.get("lifecycle")) is not str):
            raise AgentError("STATE_CORRUPT", "Service checkpoint is invalid.")
        return document

    def _document(self, lifecycle: str) -> dict:
        health = self.health()
        return {"schema_version": 1, "epoch": self.epoch,
                "service_digest": self.bundle.service.digest,
                "connection_digest": self.bundle.connection.digest,
                "lifecycle": lifecycle, "updated_at": _now(),
                "unclean_restart": self._unclean_restart,
                "recovered_tasks": self._recovered_tasks,
                "recovery_required": deepcopy(self._recovery_required),
                "health": health, "scheduler": self.scheduler.snapshot()}

    def checkpoint(self, lifecycle: str):
        try:
            _mkdir(self._status_path.parent)
            _publish(self._status_path, self._document(lifecycle), replace=True)
        except (OSError, AgentError):
            self._storage_error = "SERVICE_CHECKPOINT_FAILED"
            self.scheduler.close_intake()
            raise AgentError("STATE_IO", "Service checkpoint could not be persisted; dispatch is closed.") from None

    def start(self) -> dict:
        with self._lock:
            if self._started:
                raise AgentError("SERVICE_STARTED", "Service runtime is already started.")
            previous = self._read_previous()
            self._unclean_restart = bool(previous and previous.get("lifecycle") not in {"stopped"})
            self._live = True
            self._started = True
            try:
                keys = self.store.task_keys()
                for key in keys:
                    state = self.store.recover(key)
                    self._recovered_tasks += 1
                    if state is not None and active_execution(state):
                        if self.recovery is None:
                            self._recovery_required.append({
                                "task": key.as_dict(), "reason": "EXECUTION_REOBSERVATION_REQUIRED",
                                "next_action": "configure_runner_and_reobserve",
                            })
                        else:
                            result = self.recovery(key, state)
                            if result is not None and type(result) is not dict:
                                raise AgentError("RECOVERY_PROTOCOL", "Recovery hook returned an invalid result.")
                            if result and result.get("status") in {"uncertain", "blocked"}:
                                self._recovery_required.append({"task": key.as_dict(), **result})
                self.checkpoint("running")
            except Exception:
                self._live = False
                self._started = False
                raise
            return self.status()

    @staticmethod
    def _receipt_repository(receipt):
        value = receipt.metadata.get("repository_id")
        return value if type(value) is int and value > 0 else 0

    def _schedule_inbox(self):
        if self.processor is None:
            return 0
        count = 0
        for receipt in self.processor.inbox.pending():
            work = ScheduledWork(
                "webhook-" + receipt.delivery_digest,
                self._receipt_repository(receipt),
                lambda _context, receipt=receipt: self.processor.process(receipt),
                kind="control",
                active_timeout_seconds=self.bundle.service.limits.active_run_timeout_seconds,
            )
            if self.scheduler.submit(work):
                count += 1
        return count

    def tick(self) -> dict:
        with self._lock:
            if not self._started or self._stopping:
                raise AgentError("SERVICE_NOT_RUNNING", "Service runtime is not accepting a processing cycle.")
            try:
                scheduled = self._schedule_inbox()
                dispatched = self.scheduler.tick()
                snapshot = self.scheduler.snapshot()
                critical = {"STATE_IO", "STATE_UNHEALTHY", "INBOX_IO", "INBOX_CORRUPT"}
                failure = next((result.get("error_code") for result in snapshot["completed"].values()
                                if result.get("error_code") in critical), None)
                if failure is not None:
                    self._storage_error = failure
                    self.scheduler.close_intake()
                    raise AgentError(failure, "Durable processing failed; dispatch is closed.")
                self.checkpoint("running")
                self.scheduler.acknowledge_completed()
                return {"scheduled": scheduled, "dispatched": dispatched,
                        "scheduler": snapshot}
            except AgentError as error:
                if error.code in {"STATE_IO", "STATE_UNHEALTHY", "INBOX_IO", "INBOX_CORRUPT"}:
                    self._storage_error = error.code
                    self.scheduler.close_intake()
                raise

    def health(self) -> dict:
        readiness = inspect_service_readiness(self.bundle, environment=self.environment)
        reasons = set(readiness.reasons)
        reasons.update(self.startup_reasons)
        if not self.runner_available:
            reasons.add("RUNNER_ADAPTER_UNAVAILABLE")
        required_adapters = {provider.adapter_key for provider in self.bundle.connection.providers.values()
                             if provider.adapter != "unconfigured"}
        if not required_adapters.issubset(self.model_adapters):
            reasons.add("MODEL_RUNTIME_ADAPTER_UNAVAILABLE")
        if self.processor is None:
            reasons.add("GITHUB_PROCESSOR_UNAVAILABLE")
        if self._recovery_required:
            reasons.add("RECOVERY_ACTION_REQUIRED")
        if self._storage_error:
            reasons.add(self._storage_error)
        if self._stopping:
            reasons.add("SERVICE_STOPPING")
        ready = self._live and not reasons and not self._stopping
        return {"live": self._live,
                "ready": ready,
                "readiness": "ready" if ready else "not_ready",
                "reasons": sorted(reasons)}

    def status(self) -> dict:
        return {"epoch": self.epoch, "health": self.health(),
                "unclean_restart": self._unclean_restart,
                "recovered_tasks": self._recovered_tasks,
                "recovery_required": deepcopy(self._recovery_required),
                "scheduler": self.scheduler.snapshot()}

    def run(self, stop_event: threading.Event | None = None):
        stop_event = stop_event or threading.Event()
        if not self._started:
            self.start()
        interval = self.bundle.service.web.poll_interval_seconds
        while not stop_event.is_set():
            self.tick()
            stop_event.wait(interval)

    def shutdown(self, *, grace_seconds: float | None = None) -> dict:
        checkpoint_error = None
        with self._lock:
            if not self._started:
                return self.status()
            self._stopping = True
            self.scheduler.close_intake()
            try:
                self.checkpoint("draining")
            except AgentError as error:
                checkpoint_error = error
        grace = (self.bundle.service.limits.command_termination_grace_seconds
                 if grace_seconds is None else grace_seconds)
        unfinished = self.scheduler.shutdown(grace)
        with self._lock:
            for work_id in unfinished:
                self._recovery_required.append({"work_id": work_id,
                                                "reason": "SHUTDOWN_INTERRUPTED",
                                                "next_action": "reobserve_before_retry"})
            self._live = False
            lifecycle = "recovery_required" if unfinished else "stopped"
            try:
                self.checkpoint(lifecycle)
            except AgentError as error:
                checkpoint_error = checkpoint_error or error
            self._started = False
            status = self.status()
            if checkpoint_error is not None:
                raise checkpoint_error
            return status
