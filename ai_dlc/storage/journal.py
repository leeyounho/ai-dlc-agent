"""Immutable journal records with atomic publication and disposable snapshots.

The state root must be an operator-owned local filesystem, inaccessible to runner
code. Hash chains detect corruption, not an administrator rewriting all history.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import uuid

from .. import validation as v
from ..errors import AgentError


@dataclass(frozen=True)
class TaskKey:
    instance_id: str
    repository_id: int
    issue_number: int

    def __post_init__(self):
        v.identifier(self.instance_id, "instance_id")
        v.integer(self.repository_id, "repository_id")
        v.integer(self.issue_number, "issue_number")

    def as_dict(self):
        return {"instance_id": self.instance_id, "repository_id": self.repository_id,
                "issue_number": self.issue_number}


@dataclass(frozen=True)
class Commit:
    state: dict
    revision: int
    duplicate: bool
    snapshot_current: bool


def _encoded(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > v.MAX_JSON_BYTES:
        raise AgentError("STATE_SIZE", "State record exceeds the permitted size.")
    return data


def _native(path: Path) -> Path:
    """Native I/O name for a path already within the validated local state tree."""
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + os.path.abspath(path))
    return path


def _no_links(path: Path):
    for part in reversed((path, *path.parents)):
        native = _native(part)
        if native.is_symlink() or (hasattr(native, "is_junction") and native.is_junction()):
            raise AgentError("STATE_PATH", "State paths must not contain symbolic links or junctions.")


def _sync_directory(path: Path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _mkdir(path: Path):
    _no_links(path)
    if not _native(path).exists():
        _mkdir(path.parent)
        _native(path).mkdir(mode=0o700 if os.name == "posix" else 0o777)
        _sync_directory(path.parent)
    if not _native(path).is_dir():
        raise AgentError("STATE_PATH", "Expected a state directory.")


def _publish(path: Path, value: dict, *, replace: bool = False):
    """Write/fsync a same-directory temporary, publish, then sync the directory."""
    _no_links(path)
    temporary = path.parent / (".pending-" + uuid.uuid4().hex)
    fd = os.open(_native(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(_native(temporary), _native(path))
        else:
            # Unlike POSIX rename, link cannot overwrite a committed record.
            os.link(_native(temporary), _native(path))
        _sync_directory(path.parent)
    finally:
        if _native(temporary).exists():
            _native(temporary).unlink()


class FileJournal:
    """Context-managed exclusive instance lock plus per-task thread locks.

    All readers of a running instance should use this object. A second process
    fails fast instead of assuming a stale PID means it can steal ownership.
    """

    def __init__(self, root: Path, *, create: bool = True):
        self.root = Path(os.path.abspath(v.local_path(root)))
        _no_links(self.root)
        self._create = create
        self._file = None
        self._locks = {}
        self._guard = threading.RLock()
        self._unhealthy = False

    def __enter__(self):
        if self._file is not None:
            raise AgentError("STATE_LOCKED", "State store is already open.")
        if not self._create and (not _native(self.root).is_dir() or not _native(self.root / "instance.lock").is_file()
                                 or not _native(self.root / "instance.json").is_file()):
            raise AgentError("STATE_MISSING", "The state directory has not been initialized.")
        try:
            _mkdir(self.root)
            path = self.root / "instance.lock"
            _no_links(path)
            self._file = _native(path).open("a+b" if self._create else "r+b")
            if os.name == "nt":
                import msvcrt
                if _native(path).stat().st_size == 0:
                    self._file.write(b"\0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise AgentError("STATE_LOCKED", "Unable to acquire exclusive state ownership.") from None
        self._unhealthy = False
        metadata = self.root / "instance.json"
        try:
            if _native(metadata).exists():
                document = self._read(metadata)
                if (document != {"schema_version": 1, "storage": "ai_dlc_file_journal"}
                        or type(document.get("schema_version")) is not int):
                    raise AgentError("STATE_VERSION", "State directory uses an unsupported storage format.")
            else:
                _publish(metadata, {"schema_version": 1, "storage": "ai_dlc_file_journal"})
        except (OSError, AgentError):
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        if self._file is not None:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            self._file.close()
            self._file = None

    @contextmanager
    def locked(self, key: TaskKey):
        if self._file is None:
            raise AgentError("STATE_CLOSED", "Open the state store before use.")
        with self._guard:
            lock = self._locks.setdefault(key, threading.RLock())
        with lock:
            yield

    def task_path(self, key: TaskKey) -> Path:
        identity = {"instance_id": key.instance_id, "repository_id": key.repository_id}
        path = self.root / "tasks" / v.canonical_digest(identity) / str(key.issue_number)
        _no_links(path)
        return path

    def task_keys(self) -> tuple[TaskKey, ...]:
        """Return identities present in the journal after validating their layout.

        Enumeration is intentionally derived from the first immutable record rather
        than from directory names alone.  This lets restart recovery discover work
        without maintaining a second, crash-sensitive index.
        """
        if self._file is None:
            raise AgentError("STATE_CLOSED", "Open the state store before use.")
        directory = self.root / "tasks"
        _no_links(directory)
        if not _native(directory).exists():
            return ()
        keys = []
        try:
            for repository_dir in sorted(_native(directory).iterdir(), key=lambda item: item.name):
                if not repository_dir.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", repository_dir.name):
                    raise ValueError
                for issue_dir in sorted(repository_dir.iterdir(), key=lambda item: item.name):
                    if not issue_dir.is_dir() or not re.fullmatch(r"[1-9][0-9]*", issue_dir.name):
                        raise ValueError
                    first = issue_dir / "journal" / "00000000000000000001.json"
                    record = self._read(first)
                    task = record.get("task")
                    if type(task) is not dict or set(task) != {"instance_id", "repository_id", "issue_number"}:
                        raise ValueError
                    key = TaskKey(task["instance_id"], task["repository_id"], task["issue_number"])
                    if _native(self.task_path(key)) != Path(issue_dir) or key.issue_number != int(issue_dir.name):
                        raise ValueError
                    keys.append(key)
        except (AgentError, OSError, TypeError, ValueError):
            raise AgentError("STATE_CORRUPT", "Task index is invalid; automatic recovery cannot continue.") from None
        return tuple(keys)

    def assert_healthy(self):
        if self._file is None:
            raise AgentError("STATE_CLOSED", "Open the state store before use.")
        if self._unhealthy:
            raise AgentError("STATE_UNHEALTHY", "A previous storage failure requires reopening and recovery.")

    def _read(self, path):
        _no_links(path)
        try:
            with _native(path).open("rb") as stream:
                return v.decode_json(stream.read(v.MAX_JSON_BYTES + 1))
        except (AgentError, OSError, ValueError):
            raise AgentError("STATE_CORRUPT", "State data is missing, unreadable, or invalid; task cannot advance.") from None

    def _records(self, key: TaskKey) -> list[dict]:
        directory = self.task_path(key) / "journal"
        _no_links(directory)
        if not _native(directory).exists():
            return []
        records, previous, ids, checked_blobs = [], None, set(), set()
        try:
            paths = sorted(p for p in _native(directory).iterdir() if not p.name.startswith(".pending-"))
            for number, path in enumerate(paths, 1):
                if path.name != f"{number:020d}.json":
                    raise ValueError
                record = self._read(path)
                if set(record) != {"schema_version", "task", "revision", "previous_digest", "event_id", "request_digest",
                                   "timestamp", "event", "state", "blobs", "digest"}:
                    raise ValueError
                unsigned = {k: value for k, value in record.items() if k != "digest"}
                if (type(record["schema_version"]) is not int or record["schema_version"] != 1
                        or type(record["revision"]) is not int or record["revision"] != number
                        or v.canonical_digest(record["task"]) != v.canonical_digest(key.as_dict())
                        or record["previous_digest"] != previous
                        or record["digest"] != v.canonical_digest(unsigned)
                        or type(record["state"]) is not dict or type(record["event"]) is not dict
                        or type(record["blobs"]) is not list):
                    raise ValueError
                v.identifier(record["event_id"], "event_id")
                if record["event_id"] in ids:
                    raise ValueError
                ids.add(record["event_id"])
                if record["request_digest"] != v.canonical_digest(record["event"]):
                    raise ValueError
                for digest in record["blobs"]:
                    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                        raise ValueError
                    if digest not in checked_blobs:
                        self._blob(key, digest)
                        checked_blobs.add(digest)
                previous = record["digest"]
                records.append(record)
        except (OSError, ValueError, TypeError, AgentError):
            raise AgentError("STATE_CORRUPT", "Journal continuity or integrity check failed; task cannot advance.") from None
        return records

    def history(self, key: TaskKey) -> list[dict]:
        with self.locked(key):
            return self._records(key)

    def read(self, key: TaskKey) -> dict | None:
        with self.locked(key):
            records = self._records(key)
            return deepcopy(records[-1]["state"]) if records else None

    def _blob(self, key, digest):
        if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AgentError("STATE_CORRUPT", "Invalid content reference.")
        value = self._read(self.task_path(key) / "blobs" / (digest + ".json"))
        if v.canonical_digest(value) != digest:
            raise AgentError("STATE_CORRUPT", "Content digest does not match its reference.")
        return value

    def blob(self, key: TaskKey, digest: str) -> dict:
        with self.locked(key):
            return self._blob(key, digest)

    def _snapshot(self, key, record):
        _publish(self.task_path(key) / "snapshot.json",
                 {"schema_version": 1, "revision": record["revision"], "journal_digest": record["digest"],
                  "state": record["state"]}, replace=True)

    def recover(self, key: TaskKey) -> dict | None:
        with self.locked(key):
            records = self._records(key)
            if not records:
                return None
            try:
                _sync_directory(self.task_path(key) / "blobs")
                _sync_directory(self.task_path(key) / "journal")
                self._snapshot(key, records[-1])
            except (OSError, AgentError):
                self._unhealthy = True
                raise AgentError("STATE_IO", "State recovery could not persist the checkpoint.") from None
            return deepcopy(records[-1]["state"])

    def commit(self, key: TaskKey, *, expected_revision: int, event_id: str, event: dict,
               reduce, blobs: tuple[dict, ...] = ()) -> Commit:
        """Call a trusted reducer under the task lock, then commit exactly once.

        Identical event redelivery returns CURRENT state, never an old approval.
        A collision with changed content is rejected, even with a stale CAS.
        """
        v.integer(expected_revision, "expected_revision", minimum=0)
        v.identifier(event_id, "event_id")
        if type(event) is not dict or any(type(blob) is not dict for blob in blobs):
            raise AgentError("STATE_VALUE", "Events and referenced blobs must be JSON objects.")
        event = deepcopy(event)
        blobs = deepcopy(blobs)
        request_digest = v.canonical_digest(event)
        with self.locked(key):
            self.assert_healthy()
            records = self._records(key)
            revision = len(records)
            old_state = deepcopy(records[-1]["state"]) if records else None
            for record in records:
                if record["event_id"] == event_id:
                    if record["request_digest"] != request_digest:
                        raise AgentError("EVENT_COLLISION", "An event identifier was reused with different content.")
                    return Commit(old_state, revision, True, False)
            if expected_revision != revision:
                raise AgentError("STATE_CONFLICT", "State has changed; reread it before retrying.")
            state = reduce(old_state)
            if type(state) is not dict:
                raise AgentError("STATE_VALUE", "A state reducer must return a document.")
            record = {"schema_version": 1, "task": key.as_dict(), "revision": revision + 1,
                      "previous_digest": records[-1]["digest"] if records else None,
                      "event_id": event_id, "request_digest": request_digest,
                      "timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
                      "state": state, "blobs": [v.canonical_digest(blob) for blob in blobs]}
            record["digest"] = v.canonical_digest(record)
            _encoded(record)
            directory = self.task_path(key)
            committed = False
            try:
                _mkdir(directory / "blobs")
                _mkdir(directory / "journal")
                for blob, digest in zip(blobs, record["blobs"]):
                    path = directory / "blobs" / (digest + ".json")
                    if _native(path).exists():
                        self._blob(key, digest)
                    else:
                        _publish(path, blob)
                _publish(directory / "journal" / f"{revision + 1:020d}.json", record)
                committed = True
                self._snapshot(key, record)
            except (OSError, AgentError):
                self._unhealthy = True
                if not committed:
                    raise AgentError("STATE_IO", "Commit outcome requires recovery; no effect may be dispatched.") from None
            return Commit(deepcopy(state), revision + 1, False, not self._unhealthy)
