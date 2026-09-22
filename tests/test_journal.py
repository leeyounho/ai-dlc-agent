from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from ai_dlc.errors import AgentError
from ai_dlc.storage import FileJournal, TaskKey
from ai_dlc.storage import journal
from ai_dlc.validation import canonical_digest
from tests.support import temporary_directory


class JournalTests(unittest.TestCase):
    key = TaskKey("fixture", 1, 1)

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)

    def commit(self, store, revision=0, event_id="event-one", payload=None, blobs=()):
        return store.commit(self.key, expected_revision=revision, event_id=event_id,
                            event={"value": payload or "one"}, blobs=blobs,
                            reduce=lambda state: {"count": (state or {}).get("count", 0) + 1})

    def test_reopen_replays_journal_and_rebuilds_missing_or_invalid_snapshot(self):
        with temporary_directory() as root:
            with FileJournal(root) as store:
                result = self.commit(store, blobs=({"original": "  body\n"},))
                self.assertTrue(result.snapshot_current)
                path = store.task_path(self.key)
                (path / "snapshot.json").write_text("broken", encoding="utf-8")
            with FileJournal(root) as store:
                self.assertEqual(store.recover(self.key), {"count": 1})
                snapshot = json.loads((path / "snapshot.json").read_text(encoding="utf-8"))
                self.assertEqual(snapshot["revision"], 1)
                self.assertEqual(snapshot["journal_digest"], store.history(self.key)[-1]["digest"])
                (path / "snapshot.json").unlink()
                self.assertEqual(store.read(self.key), {"count": 1})

    def test_duplicate_is_noop_and_returns_current_state_not_old_approval(self):
        with temporary_directory() as root, FileJournal(root) as store:
            self.commit(store)
            self.commit(store, 1, "event-two", payload="two")
            result = self.commit(store)  # stale expected revision is allowed only for identical delivery
            self.assertTrue(result.duplicate)
            self.assertEqual(result.state, {"count": 2})
            self.assertEqual(len(store.history(self.key)), 2)
            self.assert_code("EVENT_COLLISION", lambda: self.commit(store, payload="changed"))
            self.assert_code("STATE_CONFLICT", lambda: self.commit(store, event_id="new-event"))

    def test_concurrent_compare_and_set_has_one_winner(self):
        with temporary_directory() as root, FileJournal(root) as store:
            barrier = threading.Barrier(2)
            def writer(number):
                barrier.wait()
                try:
                    self.commit(store, event_id=f"event-{number}")
                    return "committed"
                except AgentError as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertCountEqual(list(pool.map(writer, [1, 2])), ["committed", "STATE_CONFLICT"])
            self.assertEqual(len(store.history(self.key)), 1)

    def test_second_process_cannot_steal_live_lock(self):
        script = "\n".join([
            "import sys", "from pathlib import Path", "from ai_dlc.storage import FileJournal",
            "from ai_dlc.errors import AgentError", "try:",
            "    with FileJournal(Path(sys.argv[1])): pass", "except AgentError as error:",
            "    print(error.code)", "    raise SystemExit(0 if error.code == 'STATE_LOCKED' else 2)",
            "raise SystemExit(3)"])
        with temporary_directory() as root:
            with FileJournal(root):
                result = subprocess.run([sys.executable, "-c", script, str(root)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "STATE_LOCKED")
            with FileJournal(root) as store:
                self.commit(store)

    def test_corruption_holes_changed_chain_and_missing_blobs_fail_closed(self):
        for damage in ("invalid-json", "state", "chain", "hole", "blob"):
            with self.subTest(damage=damage), temporary_directory() as root, FileJournal(root) as store:
                self.commit(store, blobs=({"original": "preserved"},))
                self.commit(store, 1, "event-two")
                path = store.task_path(self.key)
                first = path / "journal" / "00000000000000000001.json"
                if damage == "invalid-json":
                    first.write_bytes(b"{")
                elif damage in {"state", "chain"}:
                    value = json.loads(first.read_text(encoding="utf-8"))
                    value["state"]["count"] = 99
                    if damage == "chain":
                        value["digest"] = canonical_digest({k: v for k, v in value.items() if k != "digest"})
                    first.write_text(json.dumps(value), encoding="utf-8")
                elif damage == "hole":
                    first.unlink()
                else:
                    next((path / "blobs").glob("*.json")).unlink()
                self.assert_code("STATE_CORRUPT", lambda: store.read(self.key))
                self.assert_code("STATE_CORRUPT", lambda: self.commit(store, 2, "event-three"))

    def test_pending_and_orphan_files_are_not_committed_work(self):
        with temporary_directory() as root, FileJournal(root) as store:
            self.commit(store)
            path = store.task_path(self.key)
            (path / "journal" / ".pending-interrupted").write_text("partial", encoding="utf-8")
            (path / "blobs" / ("0" * 64 + ".json")).write_text("{}", encoding="utf-8")
            self.assertEqual(store.recover(self.key), {"count": 1})

    def test_failure_before_publication_does_not_commit_and_requires_reopen(self):
        with temporary_directory() as root:
            with FileJournal(root) as store:
                with patch.object(journal.os, "link", side_effect=OSError("disk full")):
                    self.assert_code("STATE_IO", lambda: self.commit(store))
                self.assertIsNone(store.read(self.key))
                self.assert_code("STATE_UNHEALTHY", lambda: self.commit(store))
            with FileJournal(root) as store:
                self.assertEqual(self.commit(store).revision, 1)

    def test_failure_after_journal_publication_recovery_deduplicates(self):
        with temporary_directory() as root:
            with FileJournal(root) as store:
                original = journal._publish
                def publish_then_lose_response(path, value, **kwargs):
                    original(path, value, **kwargs)
                    if path.parent.name == "journal":
                        raise OSError("lost local completion")
                with patch.object(journal, "_publish", side_effect=publish_then_lose_response):
                    self.assert_code("STATE_IO", lambda: self.commit(store))
                self.assertEqual(store.read(self.key), {"count": 1})
            with FileJournal(root) as store:
                store.recover(self.key)
                self.assertTrue(self.commit(store).duplicate)
                self.assertEqual(len(store.history(self.key)), 1)

    def test_checkpoint_failure_keeps_committed_journal_but_blocks_further_work(self):
        with temporary_directory() as root:
            with FileJournal(root) as store:
                with patch.object(store, "_snapshot", side_effect=OSError("disk full")):
                    result = self.commit(store)
                self.assertFalse(result.snapshot_current)
                self.assertEqual(result.state, {"count": 1})
                self.assert_code("STATE_UNHEALTHY", store.assert_healthy)
            with FileJournal(root) as store:
                self.assertEqual(store.recover(self.key), {"count": 1})
                self.commit(store, 1, "event-two")

    def test_reducer_error_preserves_previous_revision(self):
        with temporary_directory() as root, FileJournal(root) as store:
            self.commit(store)
            def denied(state):
                raise AgentError("DENIED", "No transition.")
            self.assert_code("DENIED", lambda: store.commit(self.key, expected_revision=1, event_id="denied",
                                                            event={}, reduce=denied))
            self.assertEqual(len(store.history(self.key)), 1)

    def test_namespace_includes_github_instance_and_cannot_escape(self):
        with temporary_directory() as root, FileJournal(root) as store:
            self.assertNotEqual(store.task_path(self.key), store.task_path(TaskKey("other", 1, 1)))
            for values in (("../escape", 1, 1), ("fixture", True, 1), ("fixture", 1, -1)):
                with self.assertRaises(AgentError):
                    TaskKey(*values)
            self.assert_code("CONFIG_PATH", lambda: FileJournal("//never-contact.invalid/state"))

    def test_state_root_symlink_is_refused_where_supported(self):
        with temporary_directory() as root:
            real, link = root / "real", root / "link"
            real.mkdir()
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks is unavailable to this Windows account.")
            self.assert_code("STATE_PATH", lambda: FileJournal(link))

    def test_inspection_does_not_initialize_missing_state(self):
        with temporary_directory() as root:
            target = root / "absent"
            self.assert_code("STATE_MISSING", lambda: FileJournal(target, create=False).__enter__())
            self.assertFalse(target.exists())

    def test_crashed_process_releases_lock_and_recovers_commit_without_snapshot(self):
        script = "\n".join([
            "import os, sys", "from pathlib import Path", "from ai_dlc.storage import FileJournal, TaskKey",
            "store = FileJournal(Path(sys.argv[1])).__enter__()",
            "def crash_after_commit(*args): os._exit(23)",
            "store._snapshot = crash_after_commit",
            "store.commit(TaskKey('fixture', 1, 1), expected_revision=0, event_id='crash-event', event={}, reduce=lambda old: {'count': 1})",
            "raise SystemExit(99)"])
        with temporary_directory() as root:
            result = subprocess.run([sys.executable, "-c", script, str(root)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 23, result.stderr)
            with FileJournal(root) as store:
                self.assertFalse((store.task_path(self.key) / "snapshot.json").exists())
                self.assertEqual(store.recover(self.key), {"count": 1})
                self.assertTrue((store.task_path(self.key) / "snapshot.json").exists())

    def test_corrupt_task_does_not_hide_other_task_history(self):
        with temporary_directory() as root, FileJournal(root) as store:
            self.commit(store)
            other = TaskKey("fixture", 1, 2)
            store.commit(other, expected_revision=0, event_id="other-task", event={}, reduce=lambda _: {"count": 1})
            record = store.task_path(self.key) / "journal" / "00000000000000000001.json"
            record.write_bytes(b"broken")
            self.assert_code("STATE_CORRUPT", lambda: store.read(self.key))
            self.assertEqual(store.read(other), {"count": 1})

    def test_long_local_paths_support_commit_reopen_and_recovery(self):
        with temporary_directory() as root:
            nested = root / ("level-" + "a" * 70) / ("level-" + "b" * 70) / ("level-" + "c" * 70)
            self.assertGreater(len(str(nested)), 260)
            with FileJournal(nested) as store:
                self.commit(store, blobs=({"original": "long local path"},))
            with FileJournal(nested, create=False) as store:
                self.assertEqual(store.recover(self.key), {"count": 1})
                self.assertEqual(store.blob(self.key, canonical_digest({"original": "long local path"})),
                                 {"original": "long local path"})
