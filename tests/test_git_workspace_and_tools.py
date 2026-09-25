import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from tests.support import temporary_directory

from ai_dlc.errors import AgentError
from ai_dlc.execution import GitWorkspaceManager, WorkspaceTools


class GitFixture:
    def __init__(self, root):
        executable = shutil.which("git")
        if executable is None:
            raise unittest.SkipTest("Git is unavailable")
        self.git = Path(executable).resolve()
        self.source = root / "source"
        self.remote = root / "remote.git"
        self.source.mkdir()
        self.run("init", "--initial-branch=main", str(self.source))
        self.write("README.md", "fixture\n")
        self.write("src/app.py", "print('one')\n")
        self.write("script.sh", "#!/bin/sh\necho safe\n")
        self.write(".gitattributes", "payload.txt filter=untrusted\n")
        self.write("payload.txt", "raw payload\n")
        self.run("-C", str(self.source), "add", ".")
        self.run("-C", str(self.source), "update-index", "--chmod=+x", "script.sh")
        self.run("-C", str(self.source), "-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.invalid", "commit", "-m", "fixture")
        self.commit = self.text("-C", str(self.source), "rev-parse", "HEAD")
        self.tree = self.text("-C", str(self.source), "show", "-s", "--format=%T", "HEAD")
        self.run("clone", "--bare", str(self.source), str(self.remote))

    def run(self, *arguments, env=None):
        subprocess.run([str(self.git), *arguments], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=env)

    def text(self, *arguments):
        return subprocess.check_output([str(self.git), *arguments], text=True).strip()

    def write(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))


class SecureGitWorkspaceTests(unittest.TestCase):
    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def manager(self, root, fixture, *, extra_remotes=(), lfs_loader=None):
        return GitWorkspaceManager(
            root / "workspaces", root / "control", root / "publisher",
            protected_roots=(root / "state",),
            approved_remotes=(str(fixture.remote.resolve()), *map(str, extra_remotes)),
            git_executable=fixture.git, allow_local_remotes=True, lfs_loader=lfs_loader,
        )

    def test_resolves_exact_commit_tree_and_materializes_without_git_metadata(self):
        with temporary_directory() as temp:
            fixture = GitFixture(temp)
            manager = self.manager(temp, fixture)
            self.assertEqual(manager.resolve(str(fixture.remote), "refs/heads/main").commit,
                             fixture.commit)
            checkout = manager.prepare(str(fixture.remote), "refs/heads/main",
                                       expected_commit=fixture.commit)
            self.assertEqual(checkout.revision.commit, fixture.commit)
            self.assertEqual(checkout.revision.tree, fixture.tree)
            self.assertFalse((checkout.workspace.root / ".git").exists())
            self.assertNotIn(str(fixture.remote), str(checkout.document()))
            self.assertEqual((checkout.workspace.root / "payload.txt").read_text(encoding="utf-8"),
                             "raw payload\n")
            modes = {item.path: item.executable for item in checkout.tracked_files}
            self.assertTrue(modes["script.sh"])
            checkout.workspace.verify()

    def test_ref_movement_bad_ref_and_unapproved_remote_fail_before_workspace(self):
        with temporary_directory() as temp:
            fixture = GitFixture(temp)
            manager = self.manager(temp, fixture)
            self.assert_code("GIT_REF_CHANGED", lambda: manager.prepare(
                str(fixture.remote), "refs/heads/main", expected_commit="0" * 40))
            self.assert_code("GIT_REF", lambda: manager.resolve(str(fixture.remote), "--upload-pack=evil"))
            other = temp / "other.git"
            fixture.run("clone", "--bare", str(fixture.source), str(other))
            self.assert_code("GIT_REMOTE_DENIED", lambda: manager.resolve(str(other), "refs/heads/main"))
            self.assertEqual(list((temp / "workspaces").glob("ws-*")), [])

    def test_inherited_git_config_and_checkout_filters_are_not_used(self):
        with temporary_directory() as temp:
            fixture = GitFixture(temp)
            malicious = temp / "malicious-home"
            malicious.mkdir()
            (malicious / ".gitconfig").write_text(
                "[url \"https://attacker.invalid/\"]\n\tinsteadOf = file://\n"
                "[credential]\n\thelper = !echo leaked\n"
                "[filter \"untrusted\"]\n\tsmudge = never-run\n", encoding="utf-8")
            environment = dict(os.environ)
            environment.update(HOME=str(malicious), GIT_CONFIG_COUNT="1",
                               GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=str(malicious))
            with patch.dict(os.environ, environment, clear=True):
                checkout = self.manager(temp, fixture).prepare(
                    str(fixture.remote), "refs/heads/main", expected_commit=fixture.commit)
            self.assertEqual((checkout.workspace.root / "payload.txt").read_bytes(), b"raw payload\n")

    def test_sensitive_and_lfs_paths_are_rejected_instead_of_exposed(self):
        with temporary_directory() as temp:
            fixture = GitFixture(temp)
            fixture.write("secret-token.txt", "do-not-copy\n")
            fixture.run("-C", str(fixture.source), "add", "secret-token.txt")
            fixture.run("-C", str(fixture.source), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-m", "secret")
            fixture.run("--git-dir", str(fixture.remote), "fetch", str(fixture.source),
                        "+refs/heads/main:refs/heads/main")
            commit = fixture.text("-C", str(fixture.source), "rev-parse", "HEAD")
            manager = self.manager(temp, fixture)
            self.assert_code("WORKSPACE_SENSITIVE", lambda: manager.prepare(
                str(fixture.remote), "refs/heads/main", expected_commit=commit))

        with temporary_directory() as temp:
            fixture = GitFixture(temp)
            fixture.write("asset.bin", "version https://git-lfs.github.com/spec/v1\n"
                          "oid sha256:" + "0" * 64 + "\nsize 10\n")
            fixture.run("-C", str(fixture.source), "add", "asset.bin")
            fixture.run("-C", str(fixture.source), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-m", "lfs")
            fixture.run("--git-dir", str(fixture.remote), "fetch", str(fixture.source),
                        "+refs/heads/main:refs/heads/main")
            commit = fixture.text("-C", str(fixture.source), "rev-parse", "HEAD")
            manager = self.manager(temp, fixture)
            self.assert_code("GIT_LFS_DENIED", lambda: manager.prepare(
                str(fixture.remote), "refs/heads/main", expected_commit=commit))
            self.assert_code("GIT_LFS_UNAVAILABLE", lambda: manager.prepare(
                str(fixture.remote), "refs/heads/main", expected_commit=commit,
                allowed_lfs_paths=("asset.bin",)))
            actual = b"large object\n"
            digest = hashlib.sha256(actual).hexdigest()
            fixture.write("asset.bin", "version https://git-lfs.github.com/spec/v1\n"
                          f"oid sha256:{digest}\nsize {len(actual)}\n")
            fixture.run("-C", str(fixture.source), "add", "asset.bin")
            fixture.run("-C", str(fixture.source), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-m", "valid-lfs")
            fixture.run("--git-dir", str(fixture.remote), "fetch", str(fixture.source),
                        "+refs/heads/main:refs/heads/main")
            commit = fixture.text("-C", str(fixture.source), "rev-parse", "HEAD")
            calls = []
            loader = lambda remote, path, oid, size: calls.append((remote, path, oid, size)) or actual
            checkout = self.manager(temp, fixture, lfs_loader=loader).prepare(
                str(fixture.remote), "refs/heads/main", expected_commit=commit,
                allowed_lfs_paths=("asset.bin",))
            self.assertEqual((checkout.workspace.root / "asset.bin").read_bytes(), actual)
            self.assertEqual(calls[0][1:], ("asset.bin", digest, len(actual)))

    def test_submodule_requires_exact_path_remote_ref_and_gitlink_commit(self):
        with temporary_directory() as temp:
            fixture = GitFixture(temp)
            sub_root = temp / "sub-fixture"
            sub_root.mkdir()
            submodule = GitFixture(sub_root)
            fixture.run("-C", str(fixture.source), "-c", "protocol.file.allow=always",
                        "submodule", "add", str(submodule.remote), "vendor/approved")
            fixture.run("-C", str(fixture.source), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-am", "submodule")
            fixture.run("--git-dir", str(fixture.remote), "fetch", str(fixture.source),
                        "+refs/heads/main:refs/heads/main")
            commit = fixture.text("-C", str(fixture.source), "rev-parse", "HEAD")
            manager = self.manager(temp, fixture, extra_remotes=(submodule.remote.resolve(),))
            self.assert_code("GIT_SUBMODULE_DENIED", lambda: manager.prepare(
                str(fixture.remote), "refs/heads/main", expected_commit=commit))
            checkout = manager.prepare(
                str(fixture.remote), "refs/heads/main", expected_commit=commit,
                allowed_submodules={"vendor/approved": (str(submodule.remote), "refs/heads/main")})
            self.assertEqual((checkout.workspace.root / "vendor/approved/README.md").read_bytes(),
                             b"fixture\n")
            self.assertFalse((checkout.workspace.root / "vendor/approved/.git").exists())

    def test_git_symlink_is_rejected(self):
        with temporary_directory() as temp:
            fixture = GitFixture(temp)
            object_id = subprocess.check_output(
                [str(fixture.git), "-C", str(fixture.source), "hash-object", "-w", "--stdin"],
                input=b"README.md").decode("ascii").strip()
            fixture.run("-C", str(fixture.source), "update-index", "--add", "--cacheinfo",
                        f"120000,{object_id},link")
            fixture.run("-C", str(fixture.source), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-m", "symlink")
            fixture.run("--git-dir", str(fixture.remote), "fetch", str(fixture.source),
                        "+refs/heads/main:refs/heads/main")
            commit = fixture.text("-C", str(fixture.source), "rev-parse", "HEAD")
            self.assert_code("GIT_SYMLINK_DENIED", lambda: self.manager(temp, fixture).prepare(
                str(fixture.remote), "refs/heads/main", expected_commit=commit))


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self.context = temporary_directory()
        self.temp = self.context.__enter__()
        self.fixture = GitFixture(self.temp)
        manager = GitWorkspaceManager(
            self.temp / "workspaces", self.temp / "control", self.temp / "publisher",
            protected_roots=(self.temp / "state",),
            approved_remotes=(str(self.fixture.remote.resolve()),),
            git_executable=self.fixture.git, allow_local_remotes=True)
        self.checkout = manager.prepare(str(self.fixture.remote), "refs/heads/main",
                                        expected_commit=self.fixture.commit)
        self.tools = WorkspaceTools(self.checkout.workspace)

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_bounded_list_read_and_literal_search_return_digests(self):
        files = self.tools.list_files("src")
        self.assertEqual(files, ("src/app.py",))
        result = self.tools.read_text("src/app.py", start_line=1, line_count=1)
        self.assertEqual(result["text"], "print('one')\n")
        self.assertEqual(result["sha256"], hashlib.sha256(result["text"].encode()).hexdigest())
        matches = self.tools.search_text("print(")
        self.assertEqual([(item["path"], item["line"]) for item in matches], [("src/app.py", 1)])
        self.assert_code("TOOL_RESULT_LIMIT", lambda: self.tools.list_files(limit=1))
        self.assert_code("WORKSPACE_PATH", lambda: self.tools.read_text(".git/config"))

    def test_patch_uses_workspace_and_file_compare_and_swap(self):
        before = self.tools.state()
        file_digest = self.tools.read_text("src/app.py")["sha256"]
        patch_text = ("--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n"
                      "-print('one')\n+print('two')\n")
        self.assert_code("PATCH_CONFLICT", lambda: self.tools.apply_patch(
            patch_text, expected_workspace_digest=before.digest,
            expected_files={"src/app.py": "0" * 64}))
        result = self.tools.apply_patch(
            patch_text,
            expected_workspace_digest=before.digest, expected_files={"src/app.py": file_digest})
        self.assertNotEqual(result.before_digest, result.after_digest)
        self.assertEqual(result.modified, ("src/app.py",))
        self.assertEqual((self.checkout.workspace.root / "src/app.py").read_text(encoding="utf-8"),
                         "print('two')\n")
        self.assertEqual((self.fixture.source / "src/app.py").read_text(encoding="utf-8"),
                         "print('one')\n")
        self.assert_code("WORKSPACE_CHANGED", lambda: self.tools.apply_patch(
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-print('two')\n+print('three')\n",
            expected_workspace_digest=before.digest, expected_files={"src/app.py": file_digest}))

    def test_add_delete_export_and_patch_conflict(self):
        before = self.tools.state()
        readme = self.tools.read_text("README.md")["sha256"]
        patch_text = ("--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+new\n"
                      "--- a/README.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-fixture\n")
        result = self.tools.apply_patch(
            patch_text, expected_workspace_digest=before.digest,
            expected_files={"new.txt": None, "README.md": readme})
        self.assertEqual(result.added, ("new.txt",))
        self.assertEqual(result.deleted, ("README.md",))
        changes = self.tools.export_changes()
        self.assertEqual([item["path"] for item in changes["added"]], ["new.txt"])
        self.assertEqual(changes["deleted"], ["README.md"])
        current = self.tools.state()
        self.assert_code("PATCH_CONFLICT", lambda: self.tools.apply_patch(
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-wrong\n+changed\n",
            expected_workspace_digest=current.digest,
            expected_files={"src/app.py": self.tools.read_text("src/app.py")["sha256"]}))

    def test_external_change_and_command_lease_block_stale_patch(self):
        before = self.tools.state()
        digest = self.tools.read_text("src/app.py")["sha256"]
        patch_text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-print('one')\n+print('two')\n"
        with self.tools.command_lease(expected_workspace_digest=before.digest):
            self.assert_code("WORKSPACE_BUSY", lambda: self.tools.apply_patch(
                patch_text, expected_workspace_digest=before.digest,
                expected_files={"src/app.py": digest}))
        (self.checkout.workspace.root / "src/app.py").write_text("external\n", encoding="utf-8")
        self.assert_code("WORKSPACE_CHANGED", lambda: self.tools.apply_patch(
            patch_text, expected_workspace_digest=before.digest,
            expected_files={"src/app.py": digest}))

    def test_hardlink_and_new_sensitive_path_are_rejected(self):
        target = self.checkout.workspace.root / "src/app.py"
        outside = self.temp / "outside.txt"
        outside.write_bytes(b"outside\n")
        target.unlink()
        try:
            os.link(outside, target)
        except OSError:
            self.skipTest("Hardlinks are unavailable")
        self.assert_code("WORKSPACE_FILE", self.tools.state)

        target.unlink()
        target.write_bytes(b"restored\n")
        (self.checkout.workspace.root / "secret-token.txt").write_bytes(b"secret\n")
        self.assert_code("WORKSPACE_SENSITIVE", self.tools.export_changes)

    def test_patch_rejects_cross_platform_case_collision_before_write(self):
        before = self.tools.state()
        self.assert_code("WORKSPACE_DUPLICATE", lambda: self.tools.apply_patch(
            "--- /dev/null\n+++ b/SRC/APP.PY\n@@ -0,0 +1 @@\n+collision\n",
            expected_workspace_digest=before.digest, expected_files={"SRC/APP.PY": None}))
        self.assertEqual((self.checkout.workspace.root / "src" / "app.py").read_bytes(),
                         b"print('one')\n")


if __name__ == "__main__":
    unittest.main()
