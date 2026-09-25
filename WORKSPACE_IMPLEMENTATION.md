# Secure Git workspace and model file tools

Issue #5 adds a control-plane Git importer and a separate credential-free model
workspace. It does not run repository code and it does not publish a branch.

## Source resolution

`GitWorkspaceManager` requires mutually isolated control, publisher, and model
workspace roots, and accepts an exact remote allowlist and an explicit installed
Git executable. `resolve(remote, ref)` obtains a full commit and tree identity.
`prepare(...)` requires that approved commit again; a moved ref fails with
`GIT_REF_CHANGED` before a workspace is returned.

Git runs only in a separate bare control directory. Each invocation supplies a
fresh environment that disables system/global config, terminal prompts,
credential helpers, hooks, non-HTTPS protocols, proxy inheritance, and HTTP
redirects. The worktree is produced from verified blob objects instead of
`git checkout`, so attributes, smudge filters, hooks, and URL rewrites are not
executed. The original repository is only read and is never modified.

The resulting tree has no `.git`, remote URL, credential, state, or sensitive
path. Its manifest records the approved commit/tree, a remote digest, every
tracked path/size/content digest/Git executable mode, and the materialized source
digest. Symlinks and unsupported modes fail closed. Case-colliding and unsafe
cross-platform paths are rejected.

Submodules are denied by default. An allowed submodule needs an exact path,
allowlisted remote, ref, and matching gitlink commit; its blobs are flattened
without nested `.git` metadata. Nested submodules remain denied. LFS pointers are
denied by default. A path allowlist plus a trusted control-plane loader is needed,
and returned content must match the pointer SHA-256 and size before publication.

## Model tools

`WorkspaceTools` exposes bounded `list_files`, `read_text`, literal
`search_text`, strict unified `apply_patch`, and `export_changes` operations.
All paths are workspace-relative and every scan rechecks symlinks, hardlinks,
file type, file count, per-file size, total bytes, and cross-platform collisions.
Binary data is never returned as text.

Patch application requires both the complete current workspace digest and one
expected digest per target. Context and hunk counts must match exactly. Writes
use same-directory temporary files, fsync, and atomic replacement. A command
lease prevents patching while an approved runner command is active; modifications
made outside the lease invalidate the workspace digest before the next patch.
Disk or replacement failure is reported as an uncertain workspace outcome, never
as successful completion.

## Boundaries still requiring integration

- HTTPS fetching still relies on the host's separately enforced DNS/egress and
  CA policy; this change includes only local bare-repository integration tests.
- The LFS loader is a trusted port, not a bundled network client.
- Branch publication and PR creation belong to Issue #10.
- Live hostile process containment belongs to the RHEL runner in Issue #7.
