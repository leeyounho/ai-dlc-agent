"""Strict offline loaders for connection v2 and repository v2 documents."""

from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
import re

from .. import validation as v
from ..errors import AgentError
from .network import authorize_url, normalize_host
from .types import (AuthConfig, CommandProfile, ConnectionConfig, Model, NetworkConfig,
                    ProjectProfile, Provider, PURPOSES, RepositoryConfig, Routes, WorkflowPolicy)


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
    return ConnectionConfig(profile, network, MappingProxyType(providers), MappingProxyType(models),
                            routes, maven["repository_url"], MappingProxyType(directories), v.canonical_digest(raw))


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
                               tuple(project["junit_report_patterns"]))
    return RepositoryConfig(enabled, instance, rid, allowed, routing, v.canonical_digest(raw), policy, role_ids, execution)


def load_connection(path: Path) -> ConnectionConfig:
    path = v.local_path(path).resolve()
    return parse_connection(v.read_json(path), base_dir=path.parent)


def load_repository(path: Path, *, connection: ConnectionConfig) -> RepositoryConfig:
    return parse_repository(v.read_json(Path(path)), connection=connection)
