from types import SimpleNamespace
import threading
import time
import unittest

from ai_dlc.publishing.driver import PublicationDriver
from ai_dlc.service.scheduler import FairScheduler
from ai_dlc.storage import TaskKey


class PublicationDriverTests(unittest.TestCase):
    def test_registered_ready_task_is_polled_and_recovery_is_not_blindly_reissued(self):
        key = TaskKey("internal", 1, 10)
        now, calls = [100.0], []
        state = {"agent": {"status": "waiting_human"}}
        done = threading.Event()
        def run(task, **kwargs):
            calls.append(task)
            done.set()
            return {"status": "published"}
        publisher = SimpleNamespace(store=SimpleNamespace(read=lambda key: state),
            git=SimpleNamespace(branch=lambda key: "aidlc/task"), loop=SimpleNamespace(publisher=None), run=run)
        driver = PublicationDriver(interval_seconds=30, clock=lambda: now[0])
        driver.register(key, publisher)
        scheduler = FairScheduler(global_limit=1, repository_limit=1, model_limit=1)
        try:
            self.assertEqual(driver.schedule(scheduler), 0)
            state["agent"]["status"] = "ready_for_pr"
            self.assertEqual(driver.schedule(scheduler), 1)
            self.assertEqual(driver.schedule(scheduler), 0)
            scheduler.tick()
            self.assertTrue(done.wait(2))
            deadline = time.monotonic() + 2
            while scheduler.snapshot()["active"] and time.monotonic() < deadline:
                time.sleep(0.01)
            scheduler.acknowledge_completed()
            self.assertEqual(driver.schedule(scheduler), 0)
            state["publication"] = {"stage": "pr_pending"}
            state["agent"]["status"] = "blocked"
            now[0] += 30
            self.assertEqual(driver.schedule(scheduler), 1)
            self.assertEqual(calls, [key])
        finally:
            scheduler.shutdown(1)
