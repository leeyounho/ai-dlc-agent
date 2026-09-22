from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PURPOSES = frozenset({"requirements", "design", "implementation", "test_generation",
                      "review", "summary", "operations_analysis"})
CODING_PURPOSES = frozenset({"implementation", "test_generation"})


@dataclass(frozen=True)
class AuthConfig:
    type: str
    token_env: str | None = None
    header_name: str | None = None
    value_env: str | None = None


@dataclass(frozen=True)
class Provider:
    id: str
    boundary: str
    adapter: str
    base_url: str
    auth: AuthConfig
    max_concurrent_requests: int
    adapter_id: str | None = None

    @property
    def adapter_key(self) -> str:
        return f"custom:{self.adapter_id}" if self.adapter == "custom" else self.adapter


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    model_env: str
    tool_calls: str
    structured_output: str
    streaming: str
    context_window_tokens: int | None
    max_output_tokens: int | None


@dataclass(frozen=True)
class Routes:
    default_model: str | None
    by_purpose: Mapping[str, str]


@dataclass(frozen=True)
class NetworkConfig:
    profile: str
    external_access: bool
    internal_hosts: frozenset[str]
    external_hosts: frozenset[str]


@dataclass(frozen=True)
class ConnectionConfig:
    profile: str
    network: NetworkConfig
    providers: Mapping[str, Provider]
    models: Mapping[str, Model]
    routing: Routes
    maven_url: str
    directories: Mapping[str, Path]
    digest: str


@dataclass(frozen=True)
class WorkflowPolicy:
    start_policy: str
    design_mode: str
    allowed_start_overrides: frozenset[str]
    allowed_design_overrides: frozenset[str]


@dataclass(frozen=True)
class CommandProfile:
    executable_id: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    report_patterns: tuple[str, ...]


@dataclass(frozen=True)
class ProjectProfile:
    adapter: str
    toolchain_id: str
    commands: Mapping[str, CommandProfile]
    junit_report_patterns: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryConfig:
    enabled: bool
    instance_id: str
    repository_id: int
    allowed_models: frozenset[str]
    routing: Routes
    digest: str
    workflow: WorkflowPolicy
    roles: Mapping[str, frozenset[int]]
    project: ProjectProfile
