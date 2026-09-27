"""Build raw Git objects outside model workspaces and publish one leased ref."""

import hashlib
import os

from .. import validation as v
from ..errors import AgentError
from ..execution.git_workspace import GitRevision
from ..execution.workspace import MAX_FILE_BYTES, read_file
from ..models.types import require
from ..storage.journal import _mkdir


class GitPublisher:
    def __init__(self, manager, remote, *, network_command=None):
        self.manager = manager
        self.remote = manager._remote(remote, manager.allow_local_remotes)
        require(self.remote in manager.approved_remotes, "GIT_REMOTE_DENIED")
        self.remote_digest = hashlib.sha256(self.remote.encode()).hexdigest()
        # HTTPS auth/CA/OS egress must be supplied by an installed control-plane
        # adapter. Never inherit a user's Git credentials or proxy environment.
        self.network_command = network_command

    def _network(self, arguments, *, cwd=None):
        if "://" in self.remote:
            require(self.network_command is not None, "PUBLISH_NETWORK_UNCONFIGURED")
            result = self.network_command(tuple(arguments), cwd=cwd)
            require(type(result) is bytes and len(result) <= 8192, "PUBLISH_NETWORK_PROTOCOL")
            return result
        return self.manager._command(arguments, cwd=cwd, output_limit=8192)

    @staticmethod
    def branch(key):
        return f"aidlc/{key.repository_id}/{key.issue_number}-" + v.canonical_digest(key.as_dict())[:12]

    def refs(self, base, branch):
        require(base != branch, "PUBLISH_DEFAULT_BRANCH")
        for ref in (base, branch):
            self.manager._ref(ref)
        output = self._network(["ls-remote", "--refs", self.remote, "refs/heads/" + base, "refs/heads/" + branch])
        found = {}
        try:
            for line in output.decode("ascii").splitlines():
                sha, ref = line.split("\t")
                require(ref in {"refs/heads/" + base, "refs/heads/" + branch} and ref not in found, "PUBLISH_REF_PROTOCOL")
                GitRevision(sha, sha)
                found[ref] = sha
        except (ValueError, UnicodeError):
            raise AgentError("PUBLISH_REF_PROTOCOL", "Remote refs are malformed.") from None
        require("refs/heads/" + base in found, "PUBLISH_BASE_MISSING")
        return {"base": found["refs/heads/" + base], "head": found.get("refs/heads/" + branch)}

    def directory(self, operation_id):
        v.identifier(operation_id, "publication.id")
        require(len(operation_id) <= 64, "PUBLISH_ID")
        from ..execution.workspace import contained
        return contained(self.manager.publisher_root, operation_id + ".git")

    def build(self, key, checkout, files, *, operation_id, base, expected_digest, timestamp):
        require(checkout.remote_digest == self.remote_digest, "PUBLISH_REMOTE_CHANGED")
        v.integer(timestamp, "publication.timestamp")
        branch = self.branch(key)
        observed = self.refs(base, branch)
        require(observed == {"base": checkout.revision.commit, "head": None}, "PUBLISH_REF_CHANGED")
        directory = self.directory(operation_id)
        require(not directory.exists(), "PUBLISH_BUILD_RECOVERY_REQUIRED")
        _mkdir(directory)
        git = lambda args, **kw: self.manager._command(args, cwd=directory, **kw)
        git(["init", "--bare", "--template=", "."])
        self._network(["fetch", "--no-tags", "--depth=1", "--no-recurse-submodules", self.remote,
                       "refs/heads/" + base], cwd=directory)
        parent = git(["rev-parse", "--verify", "FETCH_HEAD^{commit}"]).decode().strip()
        require(parent == checkout.revision.commit, "PUBLISH_BASE_CHANGED")
        original = {f.path: f for f in checkout.tracked_files}
        entries = self.manager._entries(directory, checkout.revision)
        require(set(original) == {e[0] for e in entries}, "PUBLISH_MATERIALIZATION_UNSUPPORTED")
        for path, oid, executable, kind in entries:
            require(kind == "blob", "PUBLISH_MATERIALIZATION_UNSUPPORTED")
            data = git(["cat-file", "blob", oid], output_limit=MAX_FILE_BYTES)
            require(hashlib.sha256(data).hexdigest() == original[path].sha256
                    and executable == original[path].executable, "PUBLISH_MATERIALIZATION_UNSUPPORTED")
        observed_files = files.state()
        require(observed_files.digest == expected_digest, "WORKSPACE_CHANGED")
        tree = {}
        for file in observed_files.files:
            data = read_file(files.workspace.root, file.path)
            require(hashlib.sha256(data).hexdigest() == file.sha256, "WORKSPACE_CHANGED")
            oid = git(["hash-object", "-w", "--no-filters", "--stdin"], input_bytes=data).decode().strip()
            executable = file.executable if os.name != "nt" else original.get(file.path, file).executable
            node = tree
            parts = file.path.split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = ("100755" if executable else "100644", oid)

        def write_tree(node):
            rows = []
            for name, value in sorted(node.items()):
                mode, kind, oid = ("040000", "tree", write_tree(value)) if type(value) is dict else (value[0], "blob", value[1])
                rows.append(f"{mode} {kind} {oid}\t{name}".encode("utf-8") + b"\0")
            return git(["mktree", "-z"], input_bytes=b"".join(rows)).decode().strip()

        tree_id = write_tree(tree)
        require(tree_id != checkout.revision.tree, "PUBLISH_NO_CHANGES")
        identity = f"AI-DLC <aidlc@localhost> {timestamp} +0000"
        message = (f"tree {tree_id}\nparent {parent}\nauthor {identity}\ncommitter {identity}\n\n"
                   f"Implement issue #{key.issue_number}\n\nSource-Digest: {expected_digest}\n")
        commit = git(["hash-object", "-t", "commit", "-w", "--stdin"], input_bytes=message.encode()).decode().strip()
        GitRevision(commit, tree_id)
        require(files.state().digest == expected_digest, "WORKSPACE_CHANGED")
        return {"commit": commit, "tree": tree_id, "parent": parent, "branch": branch,
                "base": base, "source_digest": expected_digest, "operation_id": operation_id}

    def push(self, key, candidate, *, expected_head=None):
        require(candidate["branch"] == self.branch(key) and candidate["branch"] != candidate["base"], "PUBLISH_BRANCH_DENIED")
        # This version creates one immutable candidate per task. Existing refs
        # require explicit reconciliation; even a fast-forward is not adopted.
        require(expected_head is None, "PUBLISH_UPDATE_REQUIRES_REVALIDATION")
        require(self.refs(candidate["base"], candidate["branch"]) == {"base": candidate["parent"], "head": expected_head},
                "PUBLISH_REF_CHANGED")
        directory = self.directory(candidate["operation_id"])
        tree = self.manager._command(["show", "-s", "--format=%T", candidate["commit"]], cwd=directory).decode().strip()
        require(tree == candidate["tree"], "PUBLISH_TREE_CHANGED")
        ref = "refs/heads/" + candidate["branch"]
        self._network(["push", "--porcelain", "--no-verify", "--force-with-lease=" + ref + ":",
                       self.remote, candidate["commit"] + ":" + ref], cwd=directory)
