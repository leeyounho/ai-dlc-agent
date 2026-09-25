"""Control-side adapter for an installer-owned, fail-closed RHEL helper."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform as platform_module
import re
import stat
import subprocess
from typing import Protocol

from .. import validation as v
from ..errors import AgentError
from .ports import ExecutionPlan, ProcessResult
from .runtime_profiles import PlatformProfile, RuntimeProfiles


MAX_HELPER_RESPONSE_BYTES = 1024 * 1024
MAX_TRUSTED_FILE_BYTES = 512 * 1024 * 1024
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class HostObservation:
    os_id: str
    os_major: int
    architecture: str
    process_isolation: str


class HostProbe(Protocol):
    def observe(self) -> HostObservation: ...


class SystemHostProbe:
    """Observe local platform facts; never infer another Linux distribution as RHEL."""

    def observe(self):
        if os.name != "posix" or not Path("/etc/os-release").is_file():
            raise AgentError("RUNNER_OS_UNSUPPORTED", "The installed runner requires a verified RHEL host.")
        try:
            raw = Path("/etc/os-release").read_bytes()
            if len(raw) > 64 * 1024:
                raise ValueError
            values = {}
            for line in raw.decode("utf-8").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value.strip().strip('"')
            os_id = values["ID"].casefold()
            major = int(values["VERSION_ID"].split(".", 1)[0])
        except (OSError, UnicodeError, KeyError, ValueError):
            raise AgentError("RUNNER_OS_UNSUPPORTED", "Unable to verify the installed RHEL identity.") from None
        architecture = platform_module.machine().casefold()
        if architecture == "amd64":
            architecture = "x86_64"
        isolation = ("cgroup_v2" if Path("/sys/fs/cgroup/cgroup.controllers").is_file()
                     else "cgroup_v1")
        return HostObservation(os_id, major, architecture, isolation)


class TrustedFileVerifier:
    """Verify root-owned immutable regular files without following a final symlink."""

    def verify(self, path: str, expected_sha256: str, *, executable: bool):
        candidate = Path(path)
        try:
            for parent in candidate.parents:
                info = os.lstat(parent)
                if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or (hasattr(info, "st_uid") and info.st_uid != 0)
                        or info.st_mode & 0o022):
                    raise AgentError("RUNNER_TRUST", "Trusted runtime parent directory is writable or unowned.")
        except AgentError:
            raise
        except OSError:
            raise AgentError("RUNNER_TRUST", "Unable to verify trusted runtime directories.") from None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_size > MAX_TRUSTED_FILE_BYTES
                        or (hasattr(before, "st_uid") and before.st_uid != 0)
                        or before.st_mode & 0o022
                        or (executable and not before.st_mode & 0o111)):
                    raise AgentError("RUNNER_TRUST", "Trusted runtime file ownership or mode is invalid.")
                digest = hashlib.sha256()
                total = 0
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                if ((before.st_size, before.st_mtime_ns, before.st_ino, before.st_mode)
                        != (after.st_size, after.st_mtime_ns, after.st_ino, after.st_mode)
                        or total != after.st_size or digest.hexdigest() != expected_sha256):
                    raise AgentError("RUNNER_TRUST", "Trusted runtime file changed or has an unexpected digest.")
        except AgentError:
            raise
        except OSError:
            raise AgentError("RUNNER_TRUST", "Unable to verify a trusted runtime file.") from None


class HelperClient(Protocol):
    def request(self, platform: PlatformProfile, operation: str, payload: dict) -> dict: ...


class NativeHelperClient:
    """Invoke the fixed native helper with JSON, no shell, PATH, proxy, or inherited secrets."""

    def __init__(self, verifier=None, *, timeout_seconds=15):
        self.verifier = verifier or TrustedFileVerifier()
        self.timeout_seconds = v.integer(timeout_seconds, "helper.timeout_seconds")

    def request(self, platform, operation, payload):
        v.enum(operation, {"preflight", "start", "inspect", "cancel"}, "helper.operation")
        self.verifier.verify(platform.helper_path, platform.helper_sha256, executable=True)
        request = {"protocol": 1, "operation": operation, "payload": payload}
        try:
            encoded = json.dumps(request, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False).encode("ascii")
        except (TypeError, ValueError):
            raise AgentError("RUNNER_PROTOCOL", "Runner helper request is invalid.") from None
        if len(encoded) > MAX_HELPER_RESPONSE_BYTES:
            raise AgentError("RUNNER_PROTOCOL", "Runner helper request exceeds its size limit.")
        process = None
        try:
            process = subprocess.Popen(
                [platform.helper_path, "request-v1"], cwd="/", env={"LANG": "C", "LC_ALL": "C"},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, close_fds=True, start_new_session=True,
            )
            stdout, _stderr = process.communicate(encoded, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            if process is not None:
                process.kill()
                process.wait()
            raise AgentError("RUNNER_HELPER_UNKNOWN", "Runner helper outcome is unknown after timeout.") from None
        except OSError:
            raise AgentError("RUNNER_HELPER_UNAVAILABLE", "Unable to invoke the installed runner helper.") from None
        if process.returncode != 0:
            raise AgentError("RUNNER_HELPER_DENIED", "Runner helper rejected the operation.")
        if len(stdout) > MAX_HELPER_RESPONSE_BYTES:
            raise AgentError("RUNNER_PROTOCOL", "Runner helper response exceeds its size limit.")
        try:
            response = v.decode_json(stdout)
            v.obj(response, "helper_response", {"protocol", "ok", "result"})
            if response["protocol"] != 1 or response["ok"] is not True or type(response["result"]) is not dict:
                raise ValueError
            return response["result"]
        except (AgentError, ValueError):
            raise AgentError("RUNNER_PROTOCOL", "Runner helper returned an invalid response.") from None


class _InstalledHandle:
    def __init__(self, runner, run_id, identity):
        self.runner, self.run_id, self._identity = runner, run_id, dict(identity)

    @property
    def identity(self):
        return dict(self._identity)

    def poll(self):
        return self.runner.inspect(self.run_id, self._identity)

    def cancel(self, reason):
        self.runner._cancel(self.run_id, self._identity, reason)


class InstalledRunner:
    """RunnerPort backed only by a verified installer-owned helper and profiles."""

    def __init__(self, profiles: RuntimeProfiles, *, helper: HelperClient | None = None,
                 host_probe: HostProbe | None = None, verifier=None):
        self.profiles = profiles
        self.verifier = verifier or TrustedFileVerifier()
        self.helper = helper or NativeHelperClient(self.verifier)
        self.host_probe = host_probe or SystemHostProbe()

    def check_installation(self):
        """Verify installed files and host evidence without starting repository code."""
        selected = self._platform()
        supported = [toolchain for toolchain in self.profiles.toolchains.values()
                     if selected.id in toolchain.platform_ids]
        if not supported:
            raise AgentError("RUNNER_TOOLCHAIN", "No installed toolchain supports this RHEL platform.")
        for toolchain in supported:
            for executable in toolchain.executables.values():
                self.verifier.verify(executable.path, executable.sha256, executable=True)
            if toolchain.maven is not None:
                self.verifier.verify(toolchain.maven.settings_file, toolchain.maven.settings_sha256,
                                     executable=False)
                self.verifier.verify(toolchain.maven.truststore_file, toolchain.maven.truststore_sha256,
                                     executable=False)
            if any(self.profiles.egress[item].enforcement != "verified"
                   for item in toolchain.egress_profile_ids):
                raise AgentError("RUNNER_EGRESS_UNVERIFIED", "Runner egress enforcement is not verified.")
        return v.canonical_digest({"profiles": self.profiles.digest, "platform": selected.id,
                                   "evidence": selected.evidence_sha256,
                                   "toolchains": sorted(item.id for item in supported)})

    def _platform(self):
        host = self.host_probe.observe()
        matching = [item for item in self.profiles.runner_pool.platforms.values()
                    if (item.os_id, item.os_major, item.architecture, item.process_isolation)
                    == (host.os_id, host.os_major, host.architecture, host.process_isolation)]
        if len(matching) != 1:
            raise AgentError("RUNNER_OS_UNSUPPORTED", "Host does not match exactly one installed RHEL profile.")
        selected = matching[0]
        if selected.verification != "verified":
            raise AgentError("RUNNER_PLATFORM_UNVERIFIED", "This RHEL platform has not passed isolation verification.")
        self.verifier.verify(selected.helper_path, selected.helper_sha256, executable=True)
        return selected

    def _command(self, plan):
        toolchain = self.profiles.toolchains.get(plan.toolchain_id)
        if toolchain is None:
            raise AgentError("RUNNER_TOOLCHAIN", "Execution references an uninstalled toolchain.")
        command = toolchain.commands.get(plan.command_id)
        if command is None:
            raise AgentError("RUNNER_COMMAND", "Execution references an unapproved command.")
        expected = (command.executable_id, command.argv, command.cwd, command.verification,
                    command.report_patterns, command.generated_patterns)
        actual = (plan.command.executable_id, plan.command.argv, plan.command.cwd, plan.verification,
                  plan.command.report_patterns, plan.generated_patterns)
        if actual != expected or plan.command.timeout_seconds > command.timeout_seconds:
            raise AgentError("RUNNER_COMMAND", "Execution command differs from the installer-approved argv contract.")
        executable = toolchain.executables[command.executable_id]
        return toolchain, command, executable

    def _workspace(self, plan):
        workspace = PurePosixPath(str(plan.workspace.root))
        allowed = PurePosixPath(self.profiles.runner_pool.workspace_root)
        if not workspace.is_absolute() or ".." in workspace.parts:
            raise AgentError("RUNNER_WORKSPACE", "Workspace path is not an absolute RHEL path.")
        try:
            workspace.relative_to(allowed)
        except ValueError:
            raise AgentError("RUNNER_WORKSPACE", "Workspace is outside the installed runner root.") from None
        for protected in self.profiles.runner_pool.protected_roots:
            protected_path = PurePosixPath(protected)
            if workspace == protected_path or protected_path in workspace.parents or workspace in protected_path.parents:
                raise AgentError("RUNNER_WORKSPACE", "Workspace overlaps a protected service path.")
        plan.workspace.verify()

    def _runtime(self, plan):
        platform = self._platform()
        toolchain, command, executable = self._command(plan)
        if platform.id not in toolchain.platform_ids:
            raise AgentError("RUNNER_TOOLCHAIN", "Toolchain is not verified for this RHEL platform.")
        self._workspace(plan)
        self.verifier.verify(executable.path, executable.sha256, executable=True)
        if toolchain.maven is not None:
            self.verifier.verify(toolchain.maven.settings_file, toolchain.maven.settings_sha256,
                                 executable=False)
            self.verifier.verify(toolchain.maven.truststore_file, toolchain.maven.truststore_sha256,
                                 executable=False)
        egress_ids = set(toolchain.egress_profile_ids)
        if any(self.profiles.egress[item].enforcement != "verified" for item in egress_ids):
            raise AgentError("RUNNER_EGRESS_UNVERIFIED", "Runner egress enforcement is not verified.")
        runtime_digest = v.canonical_digest({
            "profiles_digest": self.profiles.digest, "platform_id": platform.id,
            "platform_evidence": platform.evidence_sha256, "toolchain_id": toolchain.id,
            "executable": {"id": executable.id, "path": executable.path,
                           "sha256": executable.sha256, "version": executable.version},
            "command_id": command.id, "resource_profile_id": command.resource_profile_id,
            "egress": sorted((item, self.profiles.egress[item].evidence_sha256) for item in egress_ids),
            "workspace_digest": plan.workspace.digest,
        })
        return platform, toolchain, command, executable, runtime_digest

    @staticmethod
    def _plan_payload(plan, toolchain, command, executable, runtime_digest):
        return {
            "run_id": None, "runtime_digest": runtime_digest,
            "workspace_id": plan.workspace.id, "workspace_root": str(plan.workspace.root),
            "workspace_digest": plan.workspace.digest, "toolchain_id": plan.toolchain_id,
            "command_id": plan.command_id, "executable_id": executable.id,
            "argv": list(plan.command.argv), "cwd": plan.command.cwd,
            "timeout_seconds": plan.command.timeout_seconds,
            "resource_profile_id": command.resource_profile_id,
            "egress_profile_ids": sorted(toolchain.egress_profile_ids),
        }

    def preflight(self, plan):
        platform, toolchain, command, executable, digest = self._runtime(plan)
        response = self.helper.request(platform, "preflight",
            self._plan_payload(plan, toolchain, command, executable, digest))
        if response != {"ready": True, "runtime_digest": digest}:
            raise AgentError("RUNNER_PROTOCOL", "Runner helper did not attest to the exact runtime.")
        return digest

    def _identity(self, run_id, raw, runtime_digest=None, workspace_digest=None, *,
                  platform_id=None, resource_profile_id=None, egress_profile_ids=None):
        required = {"run_id", "token", "platform_id", "slot_id", "uid", "pid", "start_ticks",
                    "cgroup", "runtime_digest", "workspace_digest", "resource_profile_id",
                    "egress_profile_id"}
        if type(raw) is not dict or set(raw) != required:
            raise AgentError("RUNNER_PROTOCOL", "Runner identity fields are invalid.")
        if (raw["run_id"] != run_id or type(raw["token"]) is not str or not _TOKEN.fullmatch(raw["token"])
                or not all(type(raw[key]) is str and raw[key] for key in
                           ("platform_id", "slot_id", "cgroup", "runtime_digest", "workspace_digest",
                            "resource_profile_id", "egress_profile_id"))
                or type(raw["uid"]) is not int or raw["uid"] < 1
                or type(raw["pid"]) is not int or raw["pid"] < 1
                or type(raw["start_ticks"]) is not int or raw["start_ticks"] < 1
                or not _TOKEN.fullmatch(raw["runtime_digest"])
                or not _TOKEN.fullmatch(raw["workspace_digest"])
                or runtime_digest is not None and raw["runtime_digest"] != runtime_digest
                or workspace_digest is not None and raw["workspace_digest"] != workspace_digest):
            raise AgentError("RUNNER_PROTOCOL", "Runner identity does not match the execution request.")
        slots = {slot.id: slot for slot in self.profiles.runner_pool.slots}
        slot = slots.get(raw["slot_id"])
        if (raw["platform_id"] not in self.profiles.runner_pool.platforms
                or slot is None or slot.uid != raw["uid"]
                or slot.egress_profile_id != raw["egress_profile_id"]
                or raw["resource_profile_id"] not in self.profiles.runner_pool.resources):
            raise AgentError("RUNNER_PROTOCOL", "Runner identity references an unconfigured isolation scope.")
        if (platform_id is not None and raw["platform_id"] != platform_id
                or resource_profile_id is not None and raw["resource_profile_id"] != resource_profile_id
                or egress_profile_ids is not None and raw["egress_profile_id"] not in egress_profile_ids):
            raise AgentError("RUNNER_PROTOCOL", "Runner identity exceeds the requested isolation scope.")
        return dict(raw)

    def start(self, run_id, plan):
        v.identifier(run_id, "run_id")
        platform, toolchain, command, executable, digest = self._runtime(plan)
        payload = self._plan_payload(plan, toolchain, command, executable, digest)
        payload["run_id"] = run_id
        response = self.helper.request(platform, "start", payload)
        v.obj(response, "helper.start", {"state", "identity"})
        if response["state"] != "running":
            raise AgentError("RUNNER_PROTOCOL", "Runner helper did not confirm a running process.")
        identity = self._identity(run_id, response["identity"], digest, plan.workspace.digest,
                                  platform_id=platform.id,
                                  resource_profile_id=command.resource_profile_id,
                                  egress_profile_ids=toolchain.egress_profile_ids)
        return _InstalledHandle(self, run_id, identity)

    @staticmethod
    def _result(raw):
        v.obj(raw, "helper.result", {"exit_code", "termination", "stdout_sha256", "stderr_sha256",
                                     "stdout_bytes", "stderr_bytes", "process_tree_stopped",
                                     "residual_processes", "uid_reusable"})
        if type(raw["exit_code"]) is not int:
            raise AgentError("RUNNER_PROTOCOL", "Runner exit code is invalid.")
        termination = v.enum(raw["termination"],
            {"completed", "cancelled", "timeout", "output_limit", "resource_limit"},
            "helper.termination")
        for field in ("stdout_sha256", "stderr_sha256"):
            if not _TOKEN.fullmatch(raw[field] if type(raw[field]) is str else ""):
                raise AgentError("RUNNER_PROTOCOL", "Runner stream digest is invalid.")
        stdout_bytes = v.integer(raw["stdout_bytes"], "helper.stdout_bytes", minimum=0)
        stderr_bytes = v.integer(raw["stderr_bytes"], "helper.stderr_bytes", minimum=0)
        stopped = v.boolean(raw["process_tree_stopped"], "helper.process_tree_stopped")
        residual = v.integer(raw["residual_processes"], "helper.residual_processes", minimum=0)
        reusable = v.boolean(raw["uid_reusable"], "helper.uid_reusable")
        return ProcessResult(raw["exit_code"], termination, raw["stdout_sha256"], raw["stderr_sha256"],
                             stdout_bytes, stderr_bytes, stopped and residual == 0 and reusable)

    def inspect(self, run_id, identity):
        v.identifier(run_id, "run_id")
        platform = self._platform()
        checked = None if identity is None else self._identity(run_id, identity)
        response = self.helper.request(platform, "inspect", {"run_id": run_id, "identity": checked})
        v.obj(response, "helper.inspect", {"state"}, {"identity", "result"})
        state = v.enum(response["state"], {"running", "finished", "unknown"}, "helper.state")
        if state in {"running", "unknown"}:
            return None
        if "identity" not in response or "result" not in response:
            raise AgentError("RUNNER_PROTOCOL", "Finished runner response is incomplete.")
        observed = self._identity(run_id, response["identity"])
        if checked is not None and observed != checked:
            raise AgentError("RUNNER_PROTOCOL", "Runner identity changed during inspection.")
        return self._result(response["result"])

    def _cancel(self, run_id, identity, reason):
        reason = v.enum(reason, {"cancelled", "timeout", "output_limit"}, "cancel.reason")
        platform = self._platform()
        checked = self._identity(run_id, identity)
        response = self.helper.request(platform, "cancel",
                                       {"run_id": run_id, "identity": checked, "reason": reason})
        if response != {"accepted": True}:
            raise AgentError("RUNNER_PROTOCOL", "Runner helper did not accept the cancellation request.")
