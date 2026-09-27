"""Bounded polling of registered publishers in the existing service scheduler."""

import threading
import time

from ..models.types import require
from ..service.scheduler import ScheduledWork


class PublicationDriver:
    def __init__(self, *, interval_seconds=30, clock=time.monotonic):
        require(type(interval_seconds) in {int, float} and interval_seconds >= 1, "PUBLISH_POLL_INTERVAL")
        self.interval, self.clock = interval_seconds, clock
        self.tasks, self.next_poll, self.active = {}, {}, set()
        self.lock = threading.RLock()

    def register(self, key, publisher):
        with self.lock:
            require(key not in self.tasks or self.tasks[key] is publisher, "PUBLISH_ALREADY_REGISTERED")
            self.tasks[key] = publisher

    def schedule(self, scheduler):
        count = 0
        with self.lock:
            for key, publisher in self.tasks.items():
                if key in self.active or self.clock() < self.next_poll.get(key, 0):
                    continue
                state = publisher.store.read(key) or {}
                if not state.get("publication") and (state.get("agent") or {}).get("status") != "ready_for_pr":
                    continue
                def handle(context, key=key, publisher=publisher):
                    try:
                        require(not context.cancel_event.is_set(), "PUBLISH_CANCELLED")
                        result = publisher.run(key, cancellation=context.cancel_event)
                        if publisher.loop.publisher is not None:
                            publisher.loop.publisher.publish(key)
                        return result
                    finally:
                        with self.lock:
                            self.active.discard(key)
                            self.next_poll[key] = self.clock() + self.interval
                if scheduler.submit(ScheduledWork("publication-" + publisher.git.branch(key), key.repository_id, handle)):
                    self.active.add(key)
                    count += 1
        return count
