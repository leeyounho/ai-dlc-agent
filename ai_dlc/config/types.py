from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network
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
    credential_envs: frozenset[str]
    digest: str


@dataclass(frozen=True)
class TlsConfig:
    mode: str
    ca_bundle_file: Path | None = None


@dataclass(frozen=True)
class TransportRoute:
    host: str
    port: int
    address_ranges: tuple[IPv4Network | IPv6Network, ...]


@dataclass(frozen=True)
class TransportConfig:
    tls: TlsConfig
    proxy_mode: str
    dns_mode: str
    routes: Mapping[tuple[str, int], TransportRoute]
    connect_timeout_seconds: int
    read_timeout_seconds: int
    max_response_bytes: int
    read_retry_attempts: int


@dataclass(frozen=True)
class GitHubServiceConfig:
    instance_id: str
    web_base_url: str
    api_base_url: str
    app_id_env: str
    private_key_file: Path
    webhook_secret_env: str
    api_version: str | None = None


@dataclass(frozen=True)
class WebConfig:
    bind_host: str
    port: int
    public_url: str
    tls_mode: str
    trusted_proxy_cidrs: tuple[IPv4Network | IPv6Network, ...]
    identity_adapter: str
    client_id_env: str
    client_secret_env: str
    session_absolute_seconds: int
    session_idle_seconds: int
    permission_cache_seconds: int
    sse_heartbeat_seconds: int
    poll_interval_seconds: int
    tls_certificate_file: Path | None = None
    tls_private_key_file: Path | None = None


@dataclass(frozen=True)
class ExecutionServiceConfig:
    runner_pool_profile_file: Path
    toolchains_profile_file: Path
    egress_profile_file: Path
    artifact_root: Path
    global_concurrency: int
    repository_concurrency: int
    model_concurrency: int


@dataclass(frozen=True)
class ServiceLimits:
    model_calls_per_run: int
    tool_calls_per_run: int
    repair_iterations: int
    model_timeout_seconds: int
    command_timeout_seconds: int
    active_run_timeout_seconds: int
    command_termination_grace_seconds: int
    log_bytes_per_run: int


@dataclass(frozen=True)
class ServiceConfig:
    connection_profile_file: Path
    github: GitHubServiceConfig
    web: WebConfig
    execution: ExecutionServiceConfig
    transport: TransportConfig
    limits: ServiceLimits
    repository_profile_files: tuple[Path, ...]
    credential_envs: frozenset[str]
    credential_files: frozenset[Path]
    digest: str


@dataclass(frozen=True)
class ServiceBundle:
    service: ServiceConfig
    connection: ConnectionConfig
    repositories: Mapping[int, "RepositoryConfig"]


@dataclass(frozen=True)
class ConfigurationReadiness:
    status: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons)}


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
    rules_source: str = "repository"


@dataclass(frozen=True)
class KnowledgeConfig:
    adr_path_if_absent: str = "docs/adr"
    kb_path_if_absent: str = "docs/knowledge"


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
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
