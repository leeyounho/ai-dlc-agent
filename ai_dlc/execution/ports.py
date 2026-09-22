"""Trusted installed runner boundary; no default host-shell fallback."""

from dataclasses import dataclass
from typing import Protocol

from .. import validation as v
from ..config.types import CommandProfile
from ..errors import AgentError
from .workspace import SourceFile, Workspace, local_root, relative_path


@dataclass(frozen=True)
class ExecutionPlan:
    command_id: str
    toolchain_id: str
    command: CommandProfile
    workspace: Workspace
    verification: str
    generated_patterns: tuple[str, ...] = ()

    def __post_init__(self):
        v.identifier(self.command_id, "command_id")
        v.identifier(self.toolchain_id, "toolchain_id")
        v.enum(self.verification, {"exit_code", "junit"}, "verification")
        v.identifier(self.command.executable_id, "executable_id")
        v.integer(self.command.timeout_seconds, "timeout_seconds")
        relative_path(self.command.cwd)
        for arg in self.command.argv:
            v.string(arg, "argv")
        for pattern in (*self.command.report_patterns, *self.generated_patterns):
            relative_path(pattern, pattern=True)
        if self.verification == "junit" and not self.command.report_patterns:
            raise AgentError("JUNIT_MISSING", "JUnit verification requires report patterns.")

    def document(self):
        return {"command_id": self.command_id, "toolchain_id": self.toolchain_id,
                "command": {"executable_id": self.command.executable_id, "argv": list(self.command.argv),
                            "cwd": self.command.cwd, "timeout_seconds": self.command.timeout_seconds,
                            "report_patterns": list(self.command.report_patterns)},
                "workspace": self.workspace.document(), "verification": self.verification,
                "generated_patterns": list(self.generated_patterns)}

    @classmethod
    def from_document(cls, raw):
        v.obj(raw, "execution_plan", {"command_id", "toolchain_id", "command", "workspace", "verification", "generated_patterns"})
        command = v.obj(raw["command"], "command", {"executable_id", "argv", "cwd", "timeout_seconds", "report_patterns"})
        workspace = v.obj(raw["workspace"], "workspace", {"workspace_id", "root", "source_digest", "files"})
        files = []
        for item in v.array(workspace["files"], "workspace.files", nonempty=True):
            v.obj(item, "file", {"path", "size", "sha256", "executable"})
            files.append(SourceFile(**item))
        profile = CommandProfile(command["executable_id"], tuple(v.array(command["argv"], "command.argv")), command["cwd"],
                                 command["timeout_seconds"], v.unique_strings(command["report_patterns"], "report_patterns"))
        snapshot = Workspace(workspace["workspace_id"], local_root(workspace["root"]), tuple(files), workspace["source_digest"])
        return cls(raw["command_id"], raw["toolchain_id"], profile, snapshot, raw["verification"],
                   v.unique_strings(raw["generated_patterns"], "generated_patterns"))


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    termination: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    process_tree_stopped: bool

    def document(self):
        return dict(self.__dict__)


class RunHandle(Protocol):
    @property
    def identity(self) -> dict: ...

    def poll(self) -> ProcessResult | None: ...

    def cancel(self, reason: str) -> None: ...


class RunnerPort(Protocol):
    def preflight(self, plan: ExecutionPlan) -> str:
        """Return a runtime digest only after required isolation/egress checks."""
        ...

    def start(self, run_id: str, plan: ExecutionPlan) -> RunHandle: ...

    def inspect(self, run_id: str, identity: dict | None) -> ProcessResult | None:
        """None means unknown/still running, never absent or safe to relaunch."""
        ...


class UnconfiguredRunner:
    def preflight(self, plan):
        raise AgentError("RUNNER_UNCONFIGURED", "An installed runner with verified isolation and egress enforcement is required.")

    def start(self, run_id, plan):
        raise AgentError("RUNNER_UNCONFIGURED", "There is no host-shell execution fallback.")

    def inspect(self, run_id, identity):
        return None
