"""Strict offline loaders for connection, repository, and service documents."""

from datetime import date
import ipaddress
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
import re

from .. import validation as v
from ..errors import AgentError
from .network import authorize_host, authorize_url, normalize_host, url_host
from .types import (AuthConfig, CommandProfile, ConfigurationReadiness, ConnectionConfig,
                    ExecutionServiceConfig, GitHubServiceConfig, Model, NetworkConfig,
                    KnowledgeConfig, ProjectProfile, Provider, PURPOSES, RepositoryConfig, Routes,
                    ServiceBundle, ServiceConfig, ServiceLimits, TlsConfig, TransportConfig,
                    TransportRoute, WebConfig, WorkflowPolicy)


def _routes(raw: dict, field: str, *, nullable: bool = False) -> Routes:
    v.obj(raw, field, {"default_model", "by_purpose"})
    default = raw["default_model"]
    if not (nullable and default is None):
        v.identifier(default, field + ".default_model")
    overrides = v.mapping(raw["by_purpose"], field + ".by_purpose", nonempty=False)
    for purpose, model in overrides.items():
        v.enum(purpose, PURPOSES, field + ".purpose")
        v.identifier(model, field + ".model")
    return Routes(default, MappingProxyType(dict(overrides)))


def _auth(raw: dict) -> AuthConfig:
    v.obj(raw, "auth", {"type"}, {"token_env", "header_name", "value_env"})
    kind = v.enum(raw["type"], {"unconfigured", "none", "bearer", "header"}, "auth.type")
    fields = {"unconfigured": {"type"}, "none": {"type"},
              "bearer": {"type", "token_env"}, "header": {"type", "header_name", "value_env"}}
    v.obj(raw, "auth", fields[kind])
    if kind == "bearer":
        return AuthConfig(kind, token_env=v.env_name(raw["token_env"], "auth.token_env"))
    if kind == "header":
        header = v.string(raw["header_name"], "auth.header_name")
        if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header)
                or header.lower() in {"host", "connection", "content-length", "transfer-encoding",
                                      "cookie", "proxy-authorization", "content-type", "accept"}):
            v.fail("CONFIG_HEADER", "auth.header_name", "Header is not permitted for custom authentication.")
        return AuthConfig(kind, header_name=header, value_env=v.env_name(raw["value_env"], "auth.value_env"))
    return AuthConfig(kind)


def _directory(value: str, base: Path, field: str) -> Path:
    value = v.string(value, field)
    if "${" in value or value.startswith("~"):
        v.fail("CONFIG_PATH", field, "Path interpolation is not supported.")
    try:
        path = v.local_path(value, field)
        return (base / path).resolve()
    except (OSError, ValueError, RuntimeError):
        v.fail("CONFIG_PATH", field, "Invalid local directory path.")


def assert_separate_paths(paths) -> None:
    paths = list(paths)
    for index, path in enumerate(paths):
        for other in paths[index + 1:]:
            if path == other or path in other.parents or other in path.parents:
                raise AgentError("CONFIG_PATH_OVERLAP", "Managed directories must not overlap.")


def _assert_references_outside_managed(references, managed_directories) -> None:
    for reference in references:
        for directory in managed_directories:
            if (reference == directory or directory in reference.parents
                    or reference in directory.parents):
                raise AgentError("CONFIG_PATH_OVERLAP",
                                 "Trusted configuration references must be outside managed directories.")


def _relative_path(value, field: str) -> str:
    value = v.string(value, field)
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive or "\\" in value
            or ".." in posix.parts or ".git" in posix.parts or ":" in value):
        v.fail("CONFIG_PATH", field, "Expected a path contained in the project workspace.")
    return value


def parse_connection(raw: dict, *, base_dir: Path) -> ConnectionConfig:
    v.obj(raw, "$", {"schema_version", "profile", "network", "llm", "maven", "workspace", "storage"})
    v.version(raw["schema_version"], 2)
    profile = v.enum(raw["profile"], {"production", "test"}, "profile")
    n = v.obj(raw["network"], "network", {"external_access", "internal_hosts", "external_hosts", "follow_redirects"})
    allow_external = v.boolean(n["external_access"], "network.external_access")
    if v.boolean(n["follow_redirects"], "network.follow_redirects"):
        v.fail("NETWORK_REDIRECT", "network.follow_redirects", "Redirect following is not supported.")
    host_sets = []
    for field in ("internal_hosts", "external_hosts"):
        hosts = [normalize_host(h) for h in v.unique_strings(n[field], "network." + field)]
        if len(set(hosts)) != len(hosts):
            v.fail("CONFIG_DUPLICATE", "network." + field, "Hostnames must be unique after normalization.")
        host_sets.append(frozenset(hosts))
    internal, external = host_sets
    if internal & external:
        v.fail("NETWORK_BOUNDARY", "network", "Internal and external destinations must be disjoint.")
    if (profile == "production" and (allow_external or external)) or (external and not allow_external):
        v.fail("NETWORK_DENIED", "network", "External destinations are not permitted in this profile.")
    network = NetworkConfig(profile, allow_external, internal, external)
    llm = v.obj(raw["llm"], "llm", {"providers", "models", "routing", "failure_policy"})
    v.enum(llm["failure_policy"], {"stop"}, "llm.failure_policy")
    providers = {}
    for pid, value in v.mapping(llm["providers"], "llm.providers").items():
        p = v.obj(value, "provider", {"boundary", "adapter", "base_url", "auth", "max_concurrent_requests"}, {"adapter_id"})
        boundary = v.enum(p["boundary"], {"internal", "external"}, "provider.boundary")
        if boundary == "external" and (profile != "test" or not allow_external):
            v.fail("NETWORK_DENIED", "provider.boundary", "External providers cannot be registered in this profile.")
        adapter = v.enum(p["adapter"], {"unconfigured", "openai_chat_completions", "openai_responses", "custom"}, "provider.adapter")
        adapter_id = p.get("adapter_id")
        if adapter == "custom":
            v.identifier(adapter_id, "provider.adapter_id")
        elif "adapter_id" in p:
            v.fail("CONFIG_VALUE", "provider.adapter_id", "Only custom adapters accept an adapter identifier.")
        authorize_url(network, p["base_url"], boundary)
        providers[pid] = Provider(pid, boundary, adapter, p["base_url"], _auth(p["auth"]),
                                  v.integer(p["max_concurrent_requests"], "provider.max_concurrent_requests"), adapter_id)
    models = {}
    for mid, value in v.mapping(llm["models"], "llm.models").items():
        m = v.obj(value, "model", {"provider", "model_env", "capabilities", "context_window_tokens", "max_output_tokens"})
        provider = v.identifier(m["provider"], "model.provider")
        if provider not in providers:
            v.fail("MODEL_REFERENCE", "model.provider", "Model references an unregistered provider.")
        c = v.obj(m["capabilities"], "model.capabilities", {"tool_calls", "structured_output", "streaming"})
        for key, capability in c.items():
            v.enum(capability, {"supported", "unsupported", "unknown"}, "model.capabilities." + key)
        context, output = m["context_window_tokens"], m["max_output_tokens"]
        for key in ("context_window_tokens", "max_output_tokens"):
            if m[key] is not None:
                v.integer(m[key], "model." + key)
        if context is not None and output is not None and output >= context:
            v.fail("MODEL_LIMITS", "model", "Output limit must be smaller than the context window.")
        models[mid] = Model(mid, provider, v.env_name(m["model_env"], "model.model_env"),
                            c["tool_calls"], c["structured_output"], c["streaming"], context, output)
    routes = _routes(llm["routing"], "llm.routing")
    if {routes.default_model, *routes.by_purpose.values()} - models.keys():
        v.fail("MODEL_REFERENCE", "llm.routing", "Route references an unregistered model.")
    maven = v.obj(raw["maven"], "maven", {"repository_url", "mirror_of", "local_cache_dir"}, {"username_env", "password_env"})
    authorize_url(network, maven["repository_url"])
    v.enum(maven["mirror_of"], {"*"}, "maven.mirror_of")
    if ("username_env" in maven) != ("password_env" in maven):
        v.fail("CONFIG_MISSING_FIELD", "maven", "Maven credential references must be specified together.")
    for key in ("username_env", "password_env"):
        if key in maven:
            v.env_name(maven[key], "maven." + key)
    workspace = v.obj(raw["workspace"], "workspace", {"root"})
    storage = v.obj(raw["storage"], "storage", {"state_dir", "log_dir"})
    directories = {"workspace": _directory(workspace["root"], base_dir, "workspace.root"),
                   "state": _directory(storage["state_dir"], base_dir, "storage.state_dir"),
                   "logs": _directory(storage["log_dir"], base_dir, "storage.log_dir"),
                   "maven_cache": _directory(maven["local_cache_dir"], base_dir, "maven.local_cache_dir")}
    assert_separate_paths(directories.values())
    credential_envs = {x for p in providers.values() for x in (p.auth.token_env, p.auth.value_env) if x}
    credential_envs.update(maven[key] for key in ("username_env", "password_env") if key in maven)
    return ConnectionConfig(profile, network, MappingProxyType(providers), MappingProxyType(models),
                            routes, maven["repository_url"], MappingProxyType(directories),
                            frozenset(credential_envs), v.canonical_digest(raw))


def parse_repository(raw: dict, *, connection: ConnectionConfig) -> RepositoryConfig:
    v.obj(raw, "$", {"schema_version", "enabled", "github_instance_id", "repository_id", "display_name", "intake",
                     "model_routing", "workflow", "roles", "project", "knowledge", "deployment"})
    v.version(raw["schema_version"], 2)
    enabled = v.boolean(raw["enabled"], "enabled")
    instance = v.identifier(raw["github_instance_id"], "github_instance_id")
    rid = v.integer(raw["repository_id"], "repository_id")
    v.string(raw["display_name"], "display_name")
    v.obj(raw["intake"], "intake", {"mode"})
    v.enum(raw["intake"]["mode"], {"all_human_issues"}, "intake.mode")
    mr = v.obj(raw["model_routing"], "model_routing", {"allowed_models", "default_model", "by_purpose"})
    allowed = frozenset(v.unique_strings(mr["allowed_models"], "model_routing.allowed_models", nonempty=True))
    if allowed - connection.models.keys():
        v.fail("MODEL_REFERENCE", "model_routing.allowed_models", "Repository references an unregistered model.")
    routing = _routes({"default_model": mr["default_model"], "by_purpose": mr["by_purpose"]}, "model_routing", nullable=True)
    selected = set(routing.by_purpose.values()) | ({routing.default_model} if routing.default_model is not None else set())
    if selected - allowed:
        v.fail("MODEL_NOT_ALLOWED", "model_routing", "Repository overrides must use allowed models.")
    w = v.obj(raw["workflow"], "workflow", {"requirement_approval_required", "start_policy", "design_mode", "merge_mode",
               "required_human_reviews", "deployment_approval", "parent_approval_covers_children", "release_scope", "allowed_issue_overrides"})
    if not v.boolean(w["requirement_approval_required"], "workflow.requirement_approval_required"):
        v.fail("APPROVAL_REQUIRED", "workflow", "Requirement approval cannot be disabled.")
    enums = {"start_policy": {"explicit", "on_approval"}, "design_mode": {"collaborative", "automatic"},
             "merge_mode": {"manual", "automatic"}, "deployment_approval": {"manual", "automatic"}, "release_scope": {"issue", "parent"}}
    for key, options in enums.items():
        v.enum(w[key], options, "workflow." + key)
    v.integer(w["required_human_reviews"], "workflow.required_human_reviews", minimum=0)
    v.boolean(w["parent_approval_covers_children"], "workflow.parent_approval_covers_children")
    overrides = v.obj(w["allowed_issue_overrides"], "workflow.allowed_issue_overrides", {"start_policy", "design_mode"})
    for key, values in overrides.items():
        for value in v.unique_strings(values, "workflow.allowed_issue_overrides." + key):
            v.enum(value, enums[key], "workflow.allowed_issue_overrides." + key)
    roles = v.obj(raw["roles"], "roles", {"requirement_approver", "starter", "design_approver", "operator", "deployment_approver"})
    for role in roles.values():
        v.obj(role, "role", {"minimum_repository_permission", "actor_ids"})
        v.enum(role["minimum_repository_permission"], {"write"}, "role.minimum_repository_permission")
        ids = v.array(role["actor_ids"], "role.actor_ids")
        for actor in ids:
            v.integer(actor, "role.actor_ids")
        if len(set(ids)) != len(ids):
            v.fail("CONFIG_DUPLICATE", "role.actor_ids", "Actor IDs must be unique.")
    project = v.obj(raw["project"], "project", {"adapter", "toolchain_id", "rules_source", "command_overrides", "junit_report_patterns"})
    adapter = v.enum(project["adapter"], {"java_maven", "command"}, "project.adapter")
    v.identifier(project["toolchain_id"], "project.toolchain_id")
    v.enum(project["rules_source"], {"repository"}, "project.rules_source")
    commands = {}
    for name, command in v.mapping(project["command_overrides"], "project.command_overrides", nonempty=False).items():
        v.obj(command, "command", {"executable_id", "argv", "cwd", "timeout_seconds", "report_patterns"})
        v.identifier(command["executable_id"], "command.executable_id")
        for arg in v.array(command["argv"], "command.argv"):
            v.string(arg, "command.argv")
        _relative_path(command["cwd"], "command.cwd")
        v.integer(command["timeout_seconds"], "command.timeout_seconds")
        for pattern in v.unique_strings(command["report_patterns"], "command.report_patterns"):
            _relative_path(pattern, "command.report_patterns")
        commands[name] = CommandProfile(command["executable_id"], tuple(command["argv"]), command["cwd"],
                                        command["timeout_seconds"], tuple(command["report_patterns"]))
    for pattern in v.unique_strings(project["junit_report_patterns"], "project.junit_report_patterns", nonempty=adapter == "java_maven"):
        _relative_path(pattern, "project.junit_report_patterns")
    k = v.obj(raw["knowledge"], "knowledge", {"adr_path_if_absent", "kb_path_if_absent"})
    for path in k.values():
        _relative_path(path, "knowledge.path")
    d = v.obj(raw["deployment"], "deployment", {"required_by_default", "environment_profile_files"})
    v.boolean(d["required_by_default"], "deployment.required_by_default")
    v.unique_strings(d["environment_profile_files"], "deployment.environment_profile_files")
    policy = WorkflowPolicy(w["start_policy"], w["design_mode"], frozenset(overrides["start_policy"]),
                            frozenset(overrides["design_mode"]))
    role_ids = MappingProxyType({role: frozenset(value["actor_ids"]) for role, value in roles.items()})
    execution = ProjectProfile(adapter, project["toolchain_id"], MappingProxyType(commands),
                               tuple(project["junit_report_patterns"]), project["rules_source"])
    knowledge = KnowledgeConfig(k["adr_path_if_absent"], k["kb_path_if_absent"])
    return RepositoryConfig(enabled, instance, rid, allowed, routing, v.canonical_digest(raw), policy,
                            role_ids, execution, knowledge)


def load_connection(path: Path) -> ConnectionConfig:
    path = v.local_path(path).resolve()
    return parse_connection(v.read_json(path), base_dir=path.parent)


def load_repository(path: Path, *, connection: ConnectionConfig) -> RepositoryConfig:
    return parse_repository(v.read_json(Path(path)), connection=connection)


def _reference(value, base_dir: Path, field: str) -> Path:
    """Resolve a trusted local reference without requiring it to exist yet."""
    return _directory(value, base_dir, field)


def _bounded_integer(value, field: str, maximum: int) -> int:
    result = v.integer(value, field)
    if result > maximum:
        v.fail("CONFIG_TYPE", field, "Expected an integer in the permitted range.")
    return result


def _cidrs(value, field: str, *, nonempty: bool = False):
    result = []
    for item in v.unique_strings(value, field, nonempty=nonempty):
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError:
            v.fail("CONFIG_VALUE", field, "Expected a canonical IP network.")
        if network.prefixlen == 0:
            v.fail("CONFIG_VALUE", field, "An unrestricted address range is not permitted.")
        result.append(network)
    if len(set(result)) != len(result):
        v.fail("CONFIG_DUPLICATE", field, "Address ranges must be unique after normalization.")
    return tuple(result)


def parse_service(raw: dict, *, base_dir: Path, connection: ConnectionConfig) -> ServiceConfig:
    required = {"schema_version", "connection_profile_file", "github", "web", "execution",
                "transport", "limits", "repository_profile_files"}
    v.obj(raw, "$", required)
    v.version(raw["schema_version"], 1)
    connection_file = _reference(raw["connection_profile_file"], base_dir, "connection_profile_file")

    github_raw = v.obj(raw["github"], "github", {"instance_id", "web_base_url", "api_base_url",
                       "app_id_env", "private_key_file", "webhook_secret_env"}, {"api_version"})
    instance_id = v.identifier(github_raw["instance_id"], "github.instance_id")
    authorize_url(connection.network, github_raw["web_base_url"])
    authorize_url(connection.network, github_raw["api_base_url"])
    api_version = github_raw.get("api_version")
    if api_version is not None:
        api_version = v.string(api_version, "github.api_version")
        try:
            if not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", api_version):
                raise ValueError
            date.fromisoformat(api_version)
        except ValueError:
            v.fail("CONFIG_VALUE", "github.api_version", "Expected an explicit REST API version date.")
    github = GitHubServiceConfig(
        instance_id,
        github_raw["web_base_url"],
        github_raw["api_base_url"],
        v.env_name(github_raw["app_id_env"], "github.app_id_env"),
        _reference(github_raw["private_key_file"], base_dir, "github.private_key_file"),
        v.env_name(github_raw["webhook_secret_env"], "github.webhook_secret_env"),
        api_version,
    )

    web_required = {"bind_host", "port", "public_url", "tls_mode", "trusted_proxy_cidrs",
                    "identity_adapter", "client_id_env", "client_secret_env",
                    "session_absolute_seconds", "session_idle_seconds", "permission_cache_seconds",
                    "sse_heartbeat_seconds", "poll_interval_seconds"}
    web_raw = v.obj(raw["web"], "web", web_required, {"tls_certificate_file", "tls_private_key_file"})
    try:
        bind_address = ipaddress.ip_address(v.string(web_raw["bind_host"], "web.bind_host"))
    except ValueError:
        v.fail("CONFIG_VALUE", "web.bind_host", "Expected an explicit local IP address.")
    tls_mode = v.enum(web_raw["tls_mode"], {"reverse_proxy", "direct"}, "web.tls_mode")
    tls_fields = {"tls_certificate_file", "tls_private_key_file"}
    present_tls_fields = tls_fields & web_raw.keys()
    if tls_mode == "direct" and present_tls_fields != tls_fields:
        v.fail("CONFIG_MISSING_FIELD", "web", "Direct TLS requires both certificate and private key references.")
    if tls_mode == "reverse_proxy" and present_tls_fields:
        v.fail("CONFIG_UNKNOWN_FIELD", "web", "Reverse-proxy mode cannot load direct TLS key material.")
    trusted_cidrs = _cidrs(web_raw["trusted_proxy_cidrs"], "web.trusted_proxy_cidrs",
                           nonempty=tls_mode == "reverse_proxy")
    if tls_mode == "reverse_proxy" and not bind_address.is_loopback:
        v.fail("CONFIG_VALUE", "web.bind_host", "Reverse-proxy mode must bind to a loopback address.")
    absolute = v.integer(web_raw["session_absolute_seconds"], "web.session_absolute_seconds")
    idle = v.integer(web_raw["session_idle_seconds"], "web.session_idle_seconds")
    if idle > absolute:
        v.fail("CONFIG_VALUE", "web.session_idle_seconds", "Idle lifetime cannot exceed absolute lifetime.")
    permission_cache = _bounded_integer(web_raw["permission_cache_seconds"],
                                        "web.permission_cache_seconds", 60)
    url_host(web_raw["public_url"])
    web = WebConfig(
        str(bind_address), _bounded_integer(web_raw["port"], "web.port", 65535),
        web_raw["public_url"], tls_mode, trusted_cidrs,
        v.enum(web_raw["identity_adapter"], {"ghes_user"}, "web.identity_adapter"),
        v.env_name(web_raw["client_id_env"], "web.client_id_env"),
        v.env_name(web_raw["client_secret_env"], "web.client_secret_env"),
        absolute, idle, permission_cache,
        v.integer(web_raw["sse_heartbeat_seconds"], "web.sse_heartbeat_seconds"),
        v.integer(web_raw["poll_interval_seconds"], "web.poll_interval_seconds"),
        _reference(web_raw["tls_certificate_file"], base_dir, "web.tls_certificate_file")
        if "tls_certificate_file" in web_raw else None,
        _reference(web_raw["tls_private_key_file"], base_dir, "web.tls_private_key_file")
        if "tls_private_key_file" in web_raw else None,
    )

    execution_raw = v.obj(raw["execution"], "execution", {"runner_pool_profile_file",
                          "toolchains_profile_file", "egress_profile_file", "artifact_root",
                          "global_concurrency", "repository_concurrency", "model_concurrency"})
    execution = ExecutionServiceConfig(
        _reference(execution_raw["runner_pool_profile_file"], base_dir, "execution.runner_pool_profile_file"),
        _reference(execution_raw["toolchains_profile_file"], base_dir, "execution.toolchains_profile_file"),
        _reference(execution_raw["egress_profile_file"], base_dir, "execution.egress_profile_file"),
        _directory(execution_raw["artifact_root"], base_dir, "execution.artifact_root"),
        v.integer(execution_raw["global_concurrency"], "execution.global_concurrency"),
        v.integer(execution_raw["repository_concurrency"], "execution.repository_concurrency"),
        v.integer(execution_raw["model_concurrency"], "execution.model_concurrency"),
    )
    assert_separate_paths([*connection.directories.values(), execution.artifact_root])

    transport_raw = v.obj(raw["transport"], "transport", {"tls", "proxy", "dns_mode", "routes",
                          "connect_timeout_seconds", "read_timeout_seconds", "max_response_bytes",
                          "read_retry_attempts"})
    tls_raw = v.obj(transport_raw["tls"], "transport.tls", {"mode"}, {"ca_bundle_file"})
    trust_mode = v.enum(tls_raw["mode"], {"system", "custom_ca"}, "transport.tls.mode")
    if trust_mode == "custom_ca" and "ca_bundle_file" not in tls_raw:
        v.fail("CONFIG_MISSING_FIELD", "transport.tls", "Custom CA mode requires a bundle reference.")
    if trust_mode == "system" and "ca_bundle_file" in tls_raw:
        v.fail("CONFIG_UNKNOWN_FIELD", "transport.tls", "System trust mode cannot load a custom CA bundle.")
    tls = TlsConfig(trust_mode, _reference(tls_raw["ca_bundle_file"], base_dir,
                    "transport.tls.ca_bundle_file") if "ca_bundle_file" in tls_raw else None)
    proxy_raw = v.obj(transport_raw["proxy"], "transport.proxy", {"mode"})
    proxy_mode = v.enum(proxy_raw["mode"], {"none"}, "transport.proxy.mode")
    dns_mode = v.enum(transport_raw["dns_mode"], {"system"}, "transport.dns_mode")
    route_map = {}
    for route_raw in v.array(transport_raw["routes"], "transport.routes"):
        route_raw = v.obj(route_raw, "transport.route", {"host", "port", "address_ranges"})
        host = normalize_host(route_raw["host"])
        authorize_host(connection.network, host)
        port = _bounded_integer(route_raw["port"], "transport.route.port", 65535)
        key = (host, port)
        if key in route_map:
            v.fail("CONFIG_DUPLICATE", "transport.routes", "Routes must be unique by host and port.")
        route_map[key] = TransportRoute(host, port, _cidrs(route_raw["address_ranges"],
                                                          "transport.route.address_ranges"))
    transport = TransportConfig(
        tls, proxy_mode, dns_mode, MappingProxyType(route_map),
        v.integer(transport_raw["connect_timeout_seconds"], "transport.connect_timeout_seconds"),
        v.integer(transport_raw["read_timeout_seconds"], "transport.read_timeout_seconds"),
        v.integer(transport_raw["max_response_bytes"], "transport.max_response_bytes"),
        _bounded_integer(transport_raw["read_retry_attempts"], "transport.read_retry_attempts", 5),
    )

    limit_fields = {"model_calls_per_run", "tool_calls_per_run", "repair_iterations",
                    "model_timeout_seconds", "command_timeout_seconds", "active_run_timeout_seconds",
                    "command_termination_grace_seconds", "log_bytes_per_run"}
    limits_raw = v.obj(raw["limits"], "limits", limit_fields)
    limits = ServiceLimits(*(v.integer(limits_raw[name], "limits." + name) for name in (
        "model_calls_per_run", "tool_calls_per_run", "repair_iterations", "model_timeout_seconds",
        "command_timeout_seconds", "active_run_timeout_seconds",
        "command_termination_grace_seconds", "log_bytes_per_run")))

    repository_files = tuple(_reference(item, base_dir, "repository_profile_files")
                             for item in v.unique_strings(raw["repository_profile_files"],
                                                          "repository_profile_files", nonempty=True))
    if len(set(repository_files)) != len(repository_files):
        v.fail("CONFIG_DUPLICATE", "repository_profile_files",
               "Repository profile references must be unique after normalization.")
    credential_envs = frozenset({github.app_id_env, github.webhook_secret_env, web.client_id_env,
                                 web.client_secret_env, *connection.credential_envs})
    credential_files = {github.private_key_file}
    if web.tls_private_key_file:
        credential_files.add(web.tls_private_key_file)
    trusted_references = {connection_file, github.private_key_file,
                          execution.runner_pool_profile_file, execution.toolchains_profile_file,
                          execution.egress_profile_file, *repository_files}
    if tls.ca_bundle_file:
        trusted_references.add(tls.ca_bundle_file)
    if web.tls_certificate_file:
        trusted_references.add(web.tls_certificate_file)
    if web.tls_private_key_file:
        trusted_references.add(web.tls_private_key_file)
    _assert_references_outside_managed(trusted_references,
                                       [*connection.directories.values(), execution.artifact_root])
    return ServiceConfig(connection_file, github, web, execution, transport, limits, repository_files,
                         credential_envs, frozenset(credential_files), v.canonical_digest(raw))


def load_service(path: Path) -> ServiceBundle:
    path = v.local_path(path).resolve()
    raw = v.read_json(path)
    required = {"schema_version", "connection_profile_file", "github", "web", "execution",
                "transport", "limits", "repository_profile_files"}
    v.obj(raw, "$", required)
    connection_file = _reference(raw["connection_profile_file"], path.parent, "connection_profile_file")
    connection = load_connection(connection_file)
    service = parse_service(raw, base_dir=path.parent, connection=connection)
    repositories = {}
    for profile_file in service.repository_profile_files:
        repository = load_repository(profile_file, connection=connection)
        if repository.instance_id != service.github.instance_id:
            v.fail("CONFIG_REFERENCE", "repository.github_instance_id",
                   "Repository and service GitHub instances must match.")
        if repository.repository_id in repositories:
            v.fail("CONFIG_DUPLICATE", "repository_profile_files", "Repository IDs must be unique.")
        repositories[repository.repository_id] = repository
    return ServiceBundle(service, connection, MappingProxyType(repositories))


def inspect_service_readiness(bundle: ServiceBundle, *, environment) -> ConfigurationReadiness:
    """Inspect local prerequisites without exposing reference names, paths, or values."""
    service, connection = bundle.service, bundle.connection
    reasons = set()
    file_requirements = {
        "GITHUB_PRIVATE_KEY_UNAVAILABLE": service.github.private_key_file,
        "RUNNER_POOL_PROFILE_UNAVAILABLE": service.execution.runner_pool_profile_file,
        "TOOLCHAINS_PROFILE_UNAVAILABLE": service.execution.toolchains_profile_file,
        "EGRESS_PROFILE_UNAVAILABLE": service.execution.egress_profile_file,
    }
    if service.transport.tls.ca_bundle_file is not None:
        file_requirements["TLS_CA_BUNDLE_UNAVAILABLE"] = service.transport.tls.ca_bundle_file
    if service.web.tls_certificate_file is not None:
        file_requirements["WEB_TLS_CERTIFICATE_UNAVAILABLE"] = service.web.tls_certificate_file
    if service.web.tls_private_key_file is not None:
        file_requirements["WEB_TLS_PRIVATE_KEY_UNAVAILABLE"] = service.web.tls_private_key_file
    for reason, path in file_requirements.items():
        if not path.is_file():
            reasons.add(reason)
    for reference in service.credential_envs:
        value = environment.get(reference)
        if not (isinstance(value, str) and value.strip() and len(value) <= 65536
                and not any(ord(char) < 32 or ord(char) == 127 for char in value)):
            reasons.add("CREDENTIAL_VALUE_UNAVAILABLE")
    required_destinations = {url_host(service.github.web_base_url), url_host(service.github.api_base_url),
                             url_host(connection.maven_url)}
    required_destinations.update(url_host(provider.base_url) for provider in connection.providers.values())
    for host in required_destinations:
        route = service.transport.routes.get((host, 443))
        if route is None:
            reasons.add("TRANSPORT_ROUTE_UNAVAILABLE")
        elif not route.address_ranges:
            reasons.add("TRANSPORT_ROUTE_ADDRESSES_UNCONFIGURED")
    if any(provider.adapter == "unconfigured" for provider in connection.providers.values()):
        reasons.add("MODEL_ADAPTER_UNCONFIGURED")
    if any(provider.auth.type == "unconfigured" for provider in connection.providers.values()):
        reasons.add("MODEL_AUTH_UNCONFIGURED")
    for model in connection.models.values():
        value = environment.get(model.model_env)
        if not (isinstance(value, str) and value.strip() and len(value) <= 65536
                and not any(ord(char) < 32 or ord(char) == 127 for char in value)):
            reasons.add("MODEL_NAME_UNAVAILABLE")
        if model.context_window_tokens is None or model.max_output_tokens is None:
            reasons.add("MODEL_LIMITS_UNCONFIGURED")
        if model.tool_calls == "unknown":
            reasons.add("MODEL_CAPABILITIES_UNCONFIRMED")
    if any(url_host(url).endswith(".example") for url in
           [service.github.api_base_url, *(provider.base_url for provider in connection.providers.values())]):
        reasons.add("ENDPOINT_PLACEHOLDER")
    if not any(repository.enabled for repository in bundle.repositories.values()):
        reasons.add("NO_ENABLED_REPOSITORY")
    status = "configuration_pending" if reasons else "ready_for_transport_consumers"
    return ConfigurationReadiness(status, tuple(sorted(reasons)))


def assert_service_isolation(first: ServiceBundle, second: ServiceBundle) -> None:
    managed = lambda bundle: [*bundle.connection.directories.values(), bundle.service.execution.artifact_root]
    assert_separate_paths([*managed(first), *managed(second)])
    if first.service.credential_envs & second.service.credential_envs:
        raise AgentError("CONFIG_CREDENTIAL_OVERLAP",
                         "Service profiles must not share credential environment references.")
    if first.service.credential_files & second.service.credential_files:
        raise AgentError("CONFIG_CREDENTIAL_OVERLAP", "Service profiles must not share credential files.")
    try:
        _assert_references_outside_managed(first.service.credential_files, managed(second))
        _assert_references_outside_managed(second.service.credential_files, managed(first))
    except AgentError:
        raise AgentError("CONFIG_CREDENTIAL_OVERLAP",
                         "Service credential files must be outside the other profile's managed directories.") from None
