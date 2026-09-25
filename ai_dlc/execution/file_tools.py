"""Bounded read/search/patch tools for one credential-free workspace."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
import re
import stat
import threading
from pathlib import Path
import uuid

from .. import validation as v
from ..errors import AgentError
from ..storage.journal import _mkdir, _native, _no_links, _sync_directory
from .path_policy import DEFAULT_DENIED_PATTERNS, denied_path
from .workspace import (MAX_FILES, MAX_FILE_BYTES, MAX_TREE_BYTES, SourceFile,
                        Workspace, contained, files_under, read_file, relative_path)


MAX_LIST_RESULTS = 2000
MAX_READ_BYTES = 1024 * 1024
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_RESULT_BYTES = 512 * 1024
MAX_PATCH_BYTES = 1024 * 1024
MAX_PATCH_FILES = 100
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\n?\Z")


@dataclass(frozen=True)
class WorkspaceState:
    digest: str
    files: tuple[SourceFile, ...]

    def document(self):
        return {"workspace_digest": self.digest,
                "files": [file.document() for file in self.files]}


@dataclass(frozen=True)
class PatchResult:
    before_digest: str
    after_digest: str
    added: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]

    def document(self):
        return {"before_digest": self.before_digest, "after_digest": self.after_digest,
                "added": list(self.added), "modified": list(self.modified),
                "deleted": list(self.deleted)}


@dataclass(frozen=True)
class _Edit:
    path: str
    old: bytes | None
    new: bytes | None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class WorkspaceTools:
    def __init__(self, workspace: Workspace, *, denied_patterns=DEFAULT_DENIED_PATTERNS):
        workspace.verify()
        self.workspace = workspace
        self.denied_patterns = tuple(denied_patterns)
        self._base = {file.path: file for file in workspace.files}
        self._guard = threading.RLock()
        self._command_active = False

    def _path(self, value):
        path = relative_path(value)
        if denied_path(path, self.denied_patterns):
            raise AgentError("WORKSPACE_SENSITIVE", "Path is excluded from model tools.")
        return path

    def state(self) -> WorkspaceState:
        with self._guard:
            files, total = [], 0
            for path in files_under(self.workspace.root):
                self._path(path)
                data = read_file(self.workspace.root, path)
                total += len(data)
                if total > MAX_TREE_BYTES:
                    raise AgentError("WORKSPACE_SIZE", "Workspace exceeds the total size limit.")
                executable = (bool(_native(contained(self.workspace.root, path)).stat().st_mode & stat.S_IXUSR)
                              if os.name == "posix" else self._base.get(path, SourceFile(path, 0, "0" * 64, False)).executable)
                files.append(SourceFile(path, len(data), _digest(data), executable))
            if len(files) > MAX_FILES:
                raise AgentError("WORKSPACE_SIZE", "Workspace contains too many files.")
            if len({file.path.casefold() for file in files}) != len(files):
                raise AgentError("WORKSPACE_DUPLICATE", "Workspace paths collide across supported filesystems.")
            files = tuple(sorted(files, key=lambda item: item.path))
            return WorkspaceState(v.canonical_digest([file.document() for file in files]), files)

    def _expect_state(self, expected_digest):
        if type(expected_digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise AgentError("WORKSPACE_DIGEST", "Expected workspace digest is invalid.")
        state = self.state()
        if state.digest != expected_digest:
            raise AgentError("WORKSPACE_CHANGED", "Workspace changed since the tool request was planned.")
        return state

    def list_files(self, prefix: str | None = None, *, limit=MAX_LIST_RESULTS):
        if type(limit) is not int or limit < 1 or limit > MAX_LIST_RESULTS:
            raise AgentError("TOOL_LIMIT", "File list result limit is invalid.")
        with self._guard:
            normalized = self._path(prefix) if prefix else None
            values = [path for path in files_under(self.workspace.root)
                      if not denied_path(path, self.denied_patterns)
                      and (normalized is None or path == normalized or path.startswith(normalized.rstrip("/") + "/"))]
            if len(values) > limit:
                raise AgentError("TOOL_RESULT_LIMIT", "File list exceeds the requested result limit.")
            return tuple(values)

    def read_text(self, path: str, *, expected_sha256: str | None = None,
                  start_line=1, line_count=1000, max_bytes=MAX_READ_BYTES):
        path = self._path(path)
        if (type(start_line) is not int or start_line < 1 or type(line_count) is not int or line_count < 1
                or type(max_bytes) is not int or max_bytes < 1 or max_bytes > MAX_READ_BYTES):
            raise AgentError("TOOL_LIMIT", "Read range is invalid.")
        with self._guard:
            data = read_file(self.workspace.root, path, limit=min(MAX_FILE_BYTES, max_bytes))
            digest = _digest(data)
            if expected_sha256 is not None and expected_sha256 != digest:
                raise AgentError("WORKSPACE_CHANGED", "File changed since the tool request was planned.")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise AgentError("WORKSPACE_BINARY", "Binary or non-UTF-8 files cannot be returned as model text.") from None
            lines = text.splitlines(keepends=True)
            selected = "".join(lines[start_line - 1:start_line - 1 + line_count])
            if len(selected.encode("utf-8")) > max_bytes:
                raise AgentError("TOOL_RESULT_LIMIT", "Read result exceeds the requested byte limit.")
            return {"path": path, "sha256": digest, "size": len(data), "start_line": start_line,
                    "text": selected, "truncated": start_line - 1 + line_count < len(lines)}

    def search_text(self, query: str, *, prefix: str | None = None,
                    max_matches=MAX_SEARCH_MATCHES, max_result_bytes=MAX_SEARCH_RESULT_BYTES):
        if (type(query) is not str or not query or len(query.encode("utf-8")) > 4096
                or type(max_matches) is not int or max_matches < 1 or max_matches > MAX_SEARCH_MATCHES
                or type(max_result_bytes) is not int or max_result_bytes < 1
                or max_result_bytes > MAX_SEARCH_RESULT_BYTES):
            raise AgentError("TOOL_LIMIT", "Search request exceeds the permitted limits.")
        with self._guard:
            results, result_bytes = [], 0
            for path in self.list_files(prefix, limit=MAX_LIST_RESULTS):
                data = read_file(self.workspace.root, path)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if query not in line:
                        continue
                    excerpt = line[:1000]
                    item = {"path": path, "line": number, "text": excerpt, "sha256": _digest(data)}
                    result_bytes += len(str(item).encode("utf-8"))
                    if len(results) >= max_matches or result_bytes > max_result_bytes:
                        raise AgentError("TOOL_RESULT_LIMIT", "Search result exceeds the requested limit.")
                    results.append(item)
            return tuple(results)

    @contextmanager
    def command_lease(self, *, expected_workspace_digest: str):
        with self._guard:
            if self._command_active:
                raise AgentError("WORKSPACE_BUSY", "A workspace command is already active.")
            self._expect_state(expected_workspace_digest)
            self._command_active = True
        try:
            yield self.workspace.root
        finally:
            with self._guard:
                self._command_active = False

    @staticmethod
    def _header_path(value, prefix):
        value = value.rstrip("\n").split("\t", 1)[0]
        if value == "/dev/null":
            return None
        if not value.startswith(prefix):
            raise AgentError("PATCH_FORMAT", "Patch paths must use a/ and b/ prefixes.")
        return relative_path(value[2:])

    def _parse_patch(self, patch_text: str, originals: dict[str, bytes]) -> tuple[_Edit, ...]:
        if (type(patch_text) is not str or not patch_text
                or len(patch_text.encode("utf-8")) > MAX_PATCH_BYTES
                or "\x00" in patch_text or "\r" in patch_text):
            raise AgentError("PATCH_FORMAT", "Patch text is invalid or too large.")
        lines = patch_text.splitlines(keepends=True)
        edits, position = [], 0
        while position < len(lines):
            if not lines[position].startswith("--- ") or position + 1 >= len(lines):
                raise AgentError("PATCH_FORMAT", "Expected a strict unified patch file header.")
            old_path = self._header_path(lines[position][4:], "a/")
            new_path = self._header_path(lines[position + 1][4:], "b/")
            if old_path is None and new_path is None:
                raise AgentError("PATCH_FORMAT", "Patch cannot have two null paths.")
            path = self._path(new_path or old_path)
            if old_path is not None and new_path is not None and old_path != new_path:
                raise AgentError("PATCH_FORMAT", "Patch renames are not supported.")
            position += 2
            old_data = originals.get(path) if old_path is not None else None
            if old_path is not None and old_data is None:
                raise AgentError("PATCH_CONFLICT", "Patch source file is missing.")
            try:
                original_lines = [] if old_data is None else old_data.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError:
                raise AgentError("WORKSPACE_BINARY", "Binary files cannot be patched.") from None
            output, consumed = [], 0
            saw_hunk = False
            while position < len(lines) and not lines[position].startswith("--- "):
                match = _HUNK.fullmatch(lines[position])
                if not match:
                    raise AgentError("PATCH_FORMAT", "Patch hunk header is invalid.")
                saw_hunk = True
                old_start, old_count = int(match.group(1)), int(match.group(2) or 1)
                new_count = int(match.group(4) or 1)
                target_index = old_start - 1 if old_count else old_start
                if target_index < consumed or target_index > len(original_lines):
                    raise AgentError("PATCH_CONFLICT", "Patch hunk no longer matches the file.")
                output.extend(original_lines[consumed:target_index])
                consumed = target_index
                position += 1
                seen_old = seen_new = 0
                while seen_old < old_count or seen_new < new_count:
                    if position >= len(lines):
                        raise AgentError("PATCH_FORMAT", "Patch hunk ended before its declared counts.")
                    line = lines[position]
                    if not line or line[0] not in " +-":
                        raise AgentError("PATCH_FORMAT", "Patch hunk line is invalid.")
                    content = line[1:]
                    if line[0] in " -":
                        if consumed >= len(original_lines) or original_lines[consumed] != content:
                            raise AgentError("PATCH_CONFLICT", "Patch context does not match current content.")
                        consumed += 1
                        seen_old += 1
                    if line[0] in " +":
                        output.append(content)
                        seen_new += 1
                    if seen_old > old_count or seen_new > new_count:
                        raise AgentError("PATCH_FORMAT", "Patch hunk exceeds its declared counts.")
                    position += 1
                if (seen_old, seen_new) != (old_count, new_count):
                    raise AgentError("PATCH_FORMAT", "Patch hunk line counts are inconsistent.")
            if not saw_hunk:
                raise AgentError("PATCH_FORMAT", "Patch file has no hunks.")
            output.extend(original_lines[consumed:])
            new_data = None if new_path is None else "".join(output).encode("utf-8")
            if new_data is not None and len(new_data) > MAX_FILE_BYTES:
                raise AgentError("WORKSPACE_SIZE", "Patched file exceeds the permitted size.")
            edits.append(_Edit(path, old_data, new_data))
            if len(edits) > MAX_PATCH_FILES:
                raise AgentError("TOOL_LIMIT", "Patch changes too many files.")
        if len({edit.path.casefold() for edit in edits}) != len(edits):
            raise AgentError("PATCH_FORMAT", "Patch contains duplicate file targets.")
        return tuple(edits)

    @staticmethod
    def _replace(path: Path, data: bytes, *, executable=False):
        _no_links(path)
        temporary = path.parent / (".pending-patch-" + uuid.uuid4().hex)
        descriptor = os.open(_native(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0), 0o700 if executable else 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(_native(temporary), _native(path))
            if os.name == "posix":
                os.chmod(_native(path), 0o700 if executable else 0o600)
            _sync_directory(path.parent)
        finally:
            if _native(temporary).exists():
                _native(temporary).unlink()

    def apply_patch(self, patch_text: str, *, expected_workspace_digest: str,
                    expected_files: dict[str, str | None]) -> PatchResult:
        with self._guard:
            if self._command_active:
                raise AgentError("WORKSPACE_BUSY", "Files cannot be patched while a command is active.")
            before = self._expect_state(expected_workspace_digest)
            current = {file.path: read_file(self.workspace.root, file.path) for file in before.files}
            edits = self._parse_patch(patch_text, current)
            if type(expected_files) is not dict:
                raise AgentError("PATCH_EXPECTATION", "Expected file digests must be a mapping.")
            normalized_expected = {self._path(path): digest for path, digest in expected_files.items()}
            if any(digest is not None and (type(digest) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", digest))
                    for digest in normalized_expected.values()):
                raise AgentError("PATCH_EXPECTATION", "Expected file digest is invalid.")
            if set(normalized_expected) != {edit.path for edit in edits}:
                raise AgentError("PATCH_EXPECTATION", "Every patch target requires exactly one expected digest.")
            for edit in edits:
                expected = normalized_expected[edit.path]
                actual = None if edit.old is None else _digest(edit.old)
                if expected != actual:
                    raise AgentError("PATCH_CONFLICT", "Patch target digest is stale.")
            projected = dict(current)
            for edit in edits:
                if edit.new is None:
                    projected.pop(edit.path, None)
                else:
                    projected[edit.path] = edit.new
            if len({path.casefold() for path in projected}) != len(projected):
                raise AgentError("WORKSPACE_DUPLICATE", "Patched paths collide across supported filesystems.")
            if len(projected) > MAX_FILES or sum(len(data) for data in projected.values()) > MAX_TREE_BYTES:
                raise AgentError("WORKSPACE_SIZE", "Patched workspace exceeds the permitted size.")
            try:
                for edit in edits:
                    target = contained(self.workspace.root, edit.path)
                    if edit.old is not None:
                        observed = read_file(self.workspace.root, edit.path)
                        if _digest(observed) != _digest(edit.old):
                            raise AgentError("WORKSPACE_CHANGED", "Patch target changed during application.")
                    if edit.new is None:
                        os.unlink(_native(target))
                        _sync_directory(target.parent)
                    else:
                        _mkdir(target.parent)
                        executable = self._base.get(edit.path, SourceFile(edit.path, 0, "0" * 64, False)).executable
                        self._replace(target, edit.new, executable=executable)
            except AgentError:
                raise
            except OSError:
                raise AgentError("WORKSPACE_IO", "Patch outcome requires workspace re-observation.") from None
            after = self.state()
            added = tuple(sorted(edit.path for edit in edits if edit.old is None))
            deleted = tuple(sorted(edit.path for edit in edits if edit.new is None))
            modified = tuple(sorted(edit.path for edit in edits if edit.old is not None and edit.new is not None))
            return PatchResult(before.digest, after.digest, added, modified, deleted)

    def export_changes(self):
        with self._guard:
            state = self.state()
            current = {file.path: file for file in state.files}
            added = sorted(set(current) - set(self._base))
            deleted = sorted(set(self._base) - set(current))
            modified = sorted(path for path in set(current) & set(self._base)
                              if current[path].document() != self._base[path].document())
            return {"base_digest": self.workspace.digest, "workspace_digest": state.digest,
                    "added": [current[path].document() for path in added],
                    "modified": [current[path].document() for path in modified],
                    "deleted": deleted}
