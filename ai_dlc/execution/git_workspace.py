"""Fail-closed Git source resolution and credential-free worktree materialization."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import subprocess
from urllib.parse import urlsplit
import uuid

from .. import validation as v
from ..config.loader import assert_separate_paths
from ..errors import AgentError
from ..storage.journal import _mkdir, _native, _publish, _sync_directory
from .workspace import (MAX_FILES, MAX_FILE_BYTES, MAX_TREE_BYTES, SourceFile,
                        Workspace, contained, local_root, relative_path)
from .path_policy import DEFAULT_DENIED_PATTERNS, denied_path


_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,239}\Z")
_LFS_HEADER = b"version https://git-lfs.github.com/spec/v1\n"


@dataclass(frozen=True)
class GitRevision:
    commit: str
    tree: str

    def __post_init__(self):
        if not _OBJECT_ID.fullmatch(self.commit) or not _OBJECT_ID.fullmatch(self.tree):
            raise AgentError("GIT_REVISION", "Git revision identity is invalid.")

    def document(self):
        return {"commit": self.commit, "tree": self.tree}


@dataclass(frozen=True)
class GitCheckout:
    workspace: Workspace
    revision: GitRevision
    remote_digest: str
    tracked_files: tuple[SourceFile, ...]

    def __post_init__(self):
        if (not re.fullmatch(r"[0-9a-f]{64}", self.remote_digest)
                or len(self.tracked_files) != len(self.workspace.files)):
            raise AgentError("GIT_MANIFEST", "Git checkout manifest is inconsistent.")
        actual = [(item.path, item.size, item.sha256) for item in self.workspace.files]
        tracked = [(item.path, item.size, item.sha256) for item in self.tracked_files]
        if actual != tracked:
            raise AgentError("GIT_MANIFEST", "Git checkout files do not match the tracked manifest.")

    @property
    def source_digest(self):
        return v.canonical_digest([file.document() for file in self.tracked_files])

    def document(self):
        return {"workspace": self.workspace.document(), "revision": self.revision.document(),
                "remote_digest": self.remote_digest, "source_digest": self.source_digest,
                "tracked_files": [file.document() for file in self.tracked_files]}


class GitWorkspaceManager:
    """Resolve an approved remote/ref and export immutable blobs without checkout filters.

    The bare object store is a control-plane directory. The returned workspace
    contains no ``.git`` directory, remote URL, helper, hook, or credential.
    """

    def __init__(self, workspace_root: Path, control_root: Path, publisher_root: Path, *,
                 protected_roots: tuple[Path, ...], approved_remotes,
                 git_executable: Path | str, allow_local_remotes: bool = False,
                 denied_patterns=DEFAULT_DENIED_PATTERNS,
                 lfs_loader=None,
                 command_timeout_seconds: int = 60):
        self.workspace_root = local_root(workspace_root)
        self.control_root = local_root(control_root)
        self.publisher_root = local_root(publisher_root)
        self.protected_roots = tuple(local_root(path) for path in protected_roots)
        assert_separate_paths([self.workspace_root, self.control_root, self.publisher_root,
                               *self.protected_roots])
        self.approved_remotes = frozenset(self._remote(remote, allow_local_remotes)
                                          for remote in approved_remotes)
        if not self.approved_remotes:
            raise AgentError("GIT_REMOTE", "At least one exact approved remote is required.")
        executable = Path(git_executable)
        if not executable.is_absolute() or not executable.is_file():
            raise AgentError("GIT_EXECUTABLE", "Git must be an explicit installed executable.")
        self.git_executable = executable.resolve()
        self.allow_local_remotes = allow_local_remotes
        for remote in self.approved_remotes:
            if "://" not in remote:
                assert_separate_paths([Path(remote), self.workspace_root, self.control_root,
                                       self.publisher_root, *self.protected_roots])
        self.denied_patterns = tuple(denied_patterns)
        self.lfs_loader = lfs_loader
        self.command_timeout_seconds = v.integer(command_timeout_seconds, "git.timeout_seconds")
        _mkdir(self.workspace_root)
        _mkdir(self.control_root)
        _mkdir(self.publisher_root)
        self._environment_root = self.control_root / "environment"
        self._hooks_root = self._environment_root / "hooks"
        _mkdir(self._hooks_root)

    @staticmethod
    def _remote(value, allow_local):
        if type(value) is not str or not value or len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise AgentError("GIT_REMOTE", "Git remote is invalid.")
        if allow_local and (PureWindowsPath(value).drive or "://" not in value):
            return str(local_root(Path(value)))
        parsed = urlsplit(value)
        if parsed.scheme:
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment
                    or not parsed.path.endswith(".git")):
                raise AgentError("GIT_REMOTE", "Only credential-free approved HTTPS Git URLs are accepted.")
            return value
        raise AgentError("GIT_REMOTE", "Local Git remotes are disabled outside fixtures.")

    @staticmethod
    def _ref(value):
        if (type(value) is not str or not _REF.fullmatch(value) or value.startswith("-")
                or value.endswith(("/", ".", ".lock")) or ".." in value or "//" in value
                or "@{" in value or any(part.startswith(".") for part in value.split("/"))):
            raise AgentError("GIT_REF", "Git ref is invalid or ambiguous.")
        return value

    def _environment(self):
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GIT_SSH_COMMAND": "",
            "HOME": str(self._environment_root),
            "XDG_CONFIG_HOME": str(self._environment_root),
            "PATH": str(self.git_executable.parent),
            "LC_ALL": "C",
        }
        for name in ("SystemRoot", "WINDIR"):
            if os.environ.get(name):
                environment[name] = os.environ[name]
        return environment

    def _command(self, arguments, *, cwd=None, input_bytes=None, output_limit=4096):
        common = [str(self.git_executable),
                  "-c", "credential.helper=",
                  "-c", "core.hooksPath=" + str(self._hooks_root),
                  "-c", "core.attributesFile=" + os.devnull,
                  "-c", "http.followRedirects=false",
                  "-c", "http.proxy=",
                  "-c", "protocol.ext.allow=never",
                  "-c", "protocol.ssh.allow=never",
                  "-c", "protocol.git.allow=never"]
        common.extend(("-c", "protocol.file.allow=" + ("always" if self.allow_local_remotes else "never")))
        try:
            result = subprocess.run(
                [*common, *arguments], cwd=str(cwd) if cwd else None,
                env=self._environment(), input=input_bytes, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=self.command_timeout_seconds, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AgentError("GIT_UNAVAILABLE", "Git operation could not be completed safely.") from None
        if result.returncode != 0:
            raise AgentError("GIT_OPERATION", "Git rejected the requested source operation.")
        if len(result.stdout) > output_limit:
            raise AgentError("GIT_OUTPUT", "Git output exceeded the permitted size.")
        return result.stdout

    def _fetch(self, remote: str, ref: str):
        remote = self._remote(remote, self.allow_local_remotes)
        if remote not in self.approved_remotes:
            raise AgentError("GIT_REMOTE_DENIED", "Git remote is not in the exact allowlist.")
        ref = self._ref(ref)
        identifier = "fetch-" + uuid.uuid4().hex
        repository = self.control_root / (identifier + ".git")
        _mkdir(repository)
        try:
            self._command(["init", "--bare", str(repository)])
            self._command(["fetch", "--no-tags", "--force", "--depth=1", "--no-recurse-submodules",
                           remote, ref], cwd=repository)
            commit = self._command(["rev-parse", "--verify", "FETCH_HEAD^{commit}"],
                                   cwd=repository).decode("ascii").strip()
            tree = self._command(["show", "-s", "--format=%T", commit],
                                 cwd=repository).decode("ascii").strip()
            revision = GitRevision(commit, tree)
            return repository, revision, hashlib.sha256(remote.encode("utf-8")).hexdigest()
        except Exception:
            if repository.parent == self.control_root and repository.name.startswith("fetch-"):
                shutil.rmtree(_native(repository), ignore_errors=True)
            raise

    def resolve(self, remote: str, ref: str) -> GitRevision:
        repository, revision, _remote_digest = self._fetch(remote, ref)
        shutil.rmtree(_native(repository), ignore_errors=True)
        return revision

    def _entries(self, repository, revision):
        output = self._command(["ls-tree", "-r", "-z", "--full-tree", revision.commit],
                               cwd=repository, output_limit=MAX_TREE_BYTES)
        entries = []
        for raw in output.split(b"\0"):
            if not raw:
                continue
            try:
                metadata, encoded_path = raw.split(b"\t", 1)
                mode, kind, object_id = metadata.decode("ascii").split(" ")
                path = encoded_path.decode("utf-8")
                relative_path(path)
            except (UnicodeError, ValueError, AgentError):
                raise AgentError("GIT_TREE", "Git tree contains an unsupported path or entry.") from None
            if mode == "160000" and kind == "commit" and _OBJECT_ID.fullmatch(object_id):
                entries.append((path, object_id, False, "submodule"))
                continue
            if mode == "120000":
                raise AgentError("GIT_SYMLINK_DENIED", "Git symlinks are not materialized into model workspaces.")
            if mode not in {"100644", "100755"} or kind != "blob" or not _OBJECT_ID.fullmatch(object_id):
                raise AgentError("GIT_TREE", "Git tree contains an unsupported object type or mode.")
            if denied_path(path, self.denied_patterns):
                raise AgentError("WORKSPACE_SENSITIVE", "Git tree contains a path excluded from model workspaces.")
            entries.append((path, object_id, mode == "100755", "blob"))
        if (not entries or len(entries) > MAX_FILES
                or len({path.casefold() for path, _oid, _executable, _kind in entries}) != len(entries)):
            raise AgentError("GIT_TREE", "Git tree is empty, too large, or path-ambiguous.")
        return sorted(entries)

    def prepare(self, remote: str, ref: str, *, expected_commit: str,
                allowed_lfs_paths=(), allowed_submodules=None) -> GitCheckout:
        if not _OBJECT_ID.fullmatch(expected_commit):
            raise AgentError("GIT_REVISION", "An exact approved commit is required.")
        allowed_lfs = frozenset(relative_path(path) for path in allowed_lfs_paths)
        allowed_submodules = dict(allowed_submodules or {})
        normalized_submodules = {}
        for path, source in allowed_submodules.items():
            path = relative_path(path)
            if (type(source) not in {tuple, list} or len(source) != 2
                    or type(source[0]) is not str or type(source[1]) is not str):
                raise AgentError("GIT_SUBMODULE", "Approved submodule source is invalid.")
            normalized_submodules[path] = (self._remote(source[0], self.allow_local_remotes),
                                           self._ref(source[1]))
        repository, revision, remote_digest = self._fetch(remote, ref)
        if revision.commit != expected_commit:
            shutil.rmtree(_native(repository), ignore_errors=True)
            raise AgentError("GIT_REF_CHANGED", "Git ref no longer resolves to the approved commit.")
        identifier = "ws-" + uuid.uuid4().hex
        directory = self.workspace_root / identifier
        tree_root = directory / "tree"
        _mkdir(tree_root)
        files, tracked_files, total = [], [], 0
        observed_lfs = set()
        submodule_repositories = []
        try:
            materialized = []
            observed_submodules = set()
            for path, object_id, executable, kind in self._entries(repository, revision):
                if kind == "blob":
                    materialized.append((repository, path, object_id, executable, remote))
                    continue
                source = normalized_submodules.get(path)
                if source is None:
                    raise AgentError("GIT_SUBMODULE_DENIED", "Git submodule path is not explicitly approved.")
                submodule_remote, submodule_ref = source
                sub_repository, sub_revision, _digest_value = self._fetch(submodule_remote, submodule_ref)
                submodule_repositories.append(sub_repository)
                if sub_revision.commit != object_id:
                    raise AgentError("GIT_SUBMODULE_CHANGED", "Submodule ref no longer resolves to the gitlink commit.")
                observed_submodules.add(path)
                for child_path, child_object, child_executable, child_kind in self._entries(
                        sub_repository, sub_revision):
                    if child_kind != "blob":
                        raise AgentError("GIT_SUBMODULE_DENIED", "Nested submodules require a separate preparation stage.")
                    combined = relative_path(path.rstrip("/") + "/" + child_path)
                    if denied_path(combined, self.denied_patterns):
                        raise AgentError("WORKSPACE_SENSITIVE", "Submodule contains an excluded model path.")
                    materialized.append((sub_repository, combined, child_object,
                                         child_executable, submodule_remote))
            if set(normalized_submodules) != observed_submodules:
                raise AgentError("GIT_SUBMODULE", "Approved submodule list contains a path absent from the Git tree.")
            if (len(materialized) > MAX_FILES
                    or len({item[1].casefold() for item in materialized}) != len(materialized)):
                raise AgentError("GIT_TREE", "Expanded Git tree is too large or path-ambiguous.")
            for object_repository, path, object_id, executable, object_remote in sorted(
                    materialized, key=lambda item: item[1]):
                size_text = self._command(["cat-file", "-s", object_id], cwd=object_repository).decode("ascii").strip()
                if not size_text.isdigit() or int(size_text) > MAX_FILE_BYTES:
                    raise AgentError("WORKSPACE_SIZE", "Git source file exceeds the permitted size.")
                size = int(size_text)
                total += size
                if total > MAX_TREE_BYTES:
                    raise AgentError("WORKSPACE_SIZE", "Git source tree exceeds the permitted size.")
                data = self._command(["cat-file", "blob", object_id], cwd=object_repository,
                                     output_limit=MAX_FILE_BYTES)
                if len(data) != size:
                    raise AgentError("GIT_OBJECT", "Git blob size changed during materialization.")
                if data.startswith(_LFS_HEADER):
                    if path not in allowed_lfs:
                        raise AgentError("GIT_LFS_DENIED", "Git LFS content requires an explicit path allowlist.")
                    if self.lfs_loader is None:
                        raise AgentError("GIT_LFS_UNAVAILABLE", "An approved Git LFS fetch adapter is not configured.")
                    try:
                        observed_lfs.add(path)
                        lines = data.decode("ascii").splitlines()
                        if (len(lines) != 3 or lines[0] != "version https://git-lfs.github.com/spec/v1"
                                or not re.fullmatch(r"oid sha256:[0-9a-f]{64}", lines[1])
                                or not re.fullmatch(r"size [0-9]+", lines[2])):
                            raise ValueError
                        lfs_digest = lines[1].split(":", 1)[1]
                        lfs_size = int(lines[2].split(" ", 1)[1])
                        if lfs_size > MAX_FILE_BYTES:
                            raise AgentError("WORKSPACE_SIZE", "Git LFS object exceeds the permitted size.")
                        data = self.lfs_loader(object_remote, path, lfs_digest, lfs_size)
                        if (type(data) is not bytes or len(data) != lfs_size
                                or hashlib.sha256(data).hexdigest() != lfs_digest):
                            raise AgentError("GIT_LFS_OBJECT", "Git LFS adapter returned mismatched content.")
                        total += lfs_size - size
                        size = lfs_size
                        if total > MAX_TREE_BYTES:
                            raise AgentError("WORKSPACE_SIZE", "Git source tree exceeds the permitted size.")
                    except AgentError:
                        raise
                    except (UnicodeError, ValueError):
                        raise AgentError("GIT_LFS_OBJECT", "Git LFS pointer is invalid.") from None
                target = contained(tree_root, path)
                _mkdir(target.parent)
                descriptor = os.open(_native(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, "O_BINARY", 0), 0o700 if executable else 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if os.name == "posix":
                    os.chmod(_native(target), 0o700 if executable else 0o600)
                _sync_directory(target.parent)
                digest = hashlib.sha256(data).hexdigest()
                tracked_files.append(SourceFile(path, size, digest, executable))
                files.append(SourceFile(path, size, digest, executable if os.name == "posix" else False))
            if observed_lfs != set(allowed_lfs):
                raise AgentError("GIT_LFS", "Approved LFS path list does not match pointer files in the Git tree.")
            digest = v.canonical_digest([file.document() for file in files])
            workspace = Workspace(identifier, tree_root, tuple(files), digest)
            checkout = GitCheckout(workspace, revision, remote_digest, tuple(tracked_files))
            _publish(directory / "manifest.json", checkout.document())
            workspace.verify()
            return checkout
        except Exception:
            if directory.parent == self.workspace_root and directory.name.startswith("ws-"):
                shutil.rmtree(_native(directory), ignore_errors=True)
            raise
        finally:
            for sub_repository in submodule_repositories:
                if sub_repository.parent == self.control_root and sub_repository.name.startswith("fetch-"):
                    shutil.rmtree(_native(sub_repository), ignore_errors=True)
            if repository.parent == self.control_root and repository.name.startswith("fetch-"):
                shutil.rmtree(_native(repository), ignore_errors=True)
