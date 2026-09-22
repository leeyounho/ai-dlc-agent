"""Bounded local source snapshots, separate from control state and credentials.

This prepares inactive workspaces. Live hostile-process filesystem containment
belongs to the installed OS runner; these checks are not an OS sandbox.
"""

from dataclasses import dataclass
import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import uuid

from .. import validation as v
from ..config.loader import assert_separate_paths
from ..errors import AgentError
from ..storage.journal import _mkdir, _native, _no_links, _publish, _sync_directory

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 64 * 1024 * 1024
MAX_FILES = 10000


def relative_path(value: str, *, pattern=False) -> str:
    v.string(value, "workspace.path")
    parts = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (parts.is_absolute() or windows.drive or "\\" in value or ":" in value
            or any(part in {"..", ".git"} for part in (p.lower() for p in parts.parts))
            or any(part.endswith((".", " ")) for part in parts.parts)
            or (not pattern and any(c in value for c in "*?[]"))):
        raise AgentError("WORKSPACE_PATH", "Expected a contained project path.")
    if not pattern:
        for part in parts.parts:
            stem = part.split(".")[0].upper()
            if stem in {"CON", "NUL", "AUX", "PRN", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}:
                raise AgentError("WORKSPACE_PATH", "Reserved device names are not project files.")
    return str(parts)


def local_root(value: Path) -> Path:
    root = Path(os.path.abspath(v.local_path(value)))
    _no_links(root)
    return root


def contained(root: Path, relative: str) -> Path:
    path = root / relative_path(relative)
    _no_links(path)
    return path


def read_file(root: Path, relative: str, *, limit=MAX_FILE_BYTES) -> bytes:
    path = contained(root, relative)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(_native(path), flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise AgentError("WORKSPACE_FILE", "Only bounded, singly linked regular files are accepted.")
            content = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if (len(content) > limit or len(content) != after.st_size
                    or (before.st_size, before.st_mtime_ns, before.st_ino, before.st_mode)
                    != (after.st_size, after.st_mtime_ns, after.st_ino, after.st_mode)):
                raise AgentError("WORKSPACE_CHANGED", "Source changed while it was being read.")
            return content
    except OSError:
        raise AgentError("WORKSPACE_READ", "Unable to read the expected workspace file.") from None


def files_under(root: Path) -> tuple[str, ...]:
    _no_links(root)
    pending, files = [root], []
    visited, total_bytes = 0, 0
    while pending:
        directory = pending.pop()
        with os.scandir(_native(directory)) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_FILES:
                    raise AgentError("WORKSPACE_SIZE", "Workspace contains too many directory entries.")
                path = directory / entry.name
                _no_links(path)
                relative = path.relative_to(root).as_posix()
                relative_path(relative)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    # Windows DirEntry's cached stat omits link-count data.
                    # Read the file's actual metadata instead of treating 0 as
                    # evidence of a hard link or silently skipping the check.
                    info = _native(path).stat(follow_symlinks=False)
                    total_bytes += info.st_size
                    if info.st_nlink != 1:
                        raise AgentError("WORKSPACE_FILE", "Hardlinked workspace files are not accepted.")
                    if total_bytes > MAX_TREE_BYTES:
                        raise AgentError("WORKSPACE_SIZE", "Workspace exceeds the total size limit.")
                    files.append(relative)
                    if len(files) > MAX_FILES:
                        raise AgentError("WORKSPACE_SIZE", "Workspace contains too many files.")
                else:
                    raise AgentError("WORKSPACE_FILE", "Workspace contains a nonregular file.")
    return tuple(sorted(files))


def matches(path: str, pattern: str) -> bool:
    relative_path(pattern, pattern=True)
    while True:
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if not pattern.startswith("**/"):
            return False
        pattern = pattern[3:]


@dataclass(frozen=True)
class SourceFile:
    path: str
    size: int
    sha256: str
    executable: bool

    def __post_init__(self):
        relative_path(self.path)
        v.integer(self.size, "file.size", minimum=0)
        v.boolean(self.executable, "file.executable")
        if type(self.sha256) is not str or len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise AgentError("SOURCE_MANIFEST", "Invalid source file digest.")

    def document(self):
        return {"path": self.path, "size": self.size, "sha256": self.sha256, "executable": self.executable}


@dataclass(frozen=True)
class Workspace:
    id: str
    root: Path
    files: tuple[SourceFile, ...]
    digest: str

    def __post_init__(self):
        v.identifier(self.id, "workspace_id")
        local_root(self.root)
        if (not self.files or len(self.files) > MAX_FILES or sum(file.size for file in self.files) > MAX_TREE_BYTES
                or len({file.path.casefold() for file in self.files}) != len(self.files)
                or self.digest != v.canonical_digest([file.document() for file in self.files])):
            raise AgentError("SOURCE_MANIFEST", "Source manifest identity is inconsistent.")

    def document(self):
        return {"workspace_id": self.id, "root": str(self.root), "source_digest": self.digest,
                "files": [file.document() for file in self.files]}

    def verify(self, *, generated_patterns: tuple[str, ...] = ()):
        actual = set(files_under(self.root))
        expected = {file.path for file in self.files}
        if expected - actual or any(not any(matches(path, pattern) for pattern in generated_patterns) for path in actual - expected):
            raise AgentError("SOURCE_TREE_CHANGED", "Source files or unapproved generated files changed the input tree.")
        for file in self.files:
            data = read_file(self.root, file.path)
            executable = bool(_native(contained(self.root, file.path)).stat().st_mode & stat.S_IXUSR) if os.name == "posix" else False
            if len(data) != file.size or hashlib.sha256(data).hexdigest() != file.sha256 or executable != file.executable:
                raise AgentError("SOURCE_TREE_CHANGED", "A source file no longer matches the planned digest.")


class WorkspaceManager:
    def __init__(self, root: Path, *, protected_roots: tuple[Path, ...]):
        self.root = local_root(root)
        self.protected_roots = tuple(local_root(path) for path in protected_roots)
        for protected in self.protected_roots:
            assert_separate_paths([self.root, protected])

    def prepare(self, source: Path, paths: tuple[str, ...]) -> Workspace:
        """Import an explicit source list; never run Git hooks, scripts, or URLs."""
        source = local_root(source)
        for protected in (self.root, *self.protected_roots):
            assert_separate_paths([source, protected])
        if not paths or len(paths) > MAX_FILES:
            raise AgentError("WORKSPACE_SIZE", "An explicit bounded source file list is required.")
        normalized = tuple(relative_path(path) for path in paths)
        if len({path.casefold() for path in normalized}) != len(normalized):
            raise AgentError("WORKSPACE_DUPLICATE", "Source paths must be unique across supported filesystems.")
        identifier = "ws-" + uuid.uuid4().hex
        directory = self.root / identifier
        tree = directory / "tree"
        _mkdir(tree)
        files, total = [], 0
        for relative in sorted(normalized):
            data = read_file(source, relative)
            executable = bool(_native(contained(source, relative)).stat().st_mode & stat.S_IXUSR) if os.name == "posix" else False
            total += len(data)
            if total > MAX_TREE_BYTES:
                raise AgentError("WORKSPACE_SIZE", "Source snapshot exceeds the total size limit.")
            target = contained(tree, relative)
            _mkdir(target.parent)
            descriptor = os.open(_native(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                                 0o700 if executable else 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(target.parent)
            files.append(SourceFile(relative, len(data), hashlib.sha256(data).hexdigest(), executable))
        digest = v.canonical_digest([file.document() for file in files])
        workspace = Workspace(identifier, tree, tuple(files), digest)
        _publish(directory / "manifest.json", workspace.document())
        workspace.verify()
        return workspace
