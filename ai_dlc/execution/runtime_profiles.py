"""Strict installer-owned profiles for the privileged runner boundary."""

from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_network, IPv4Network, IPv6Network
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping
import re

from .. import validation as v
from ..config.network import normalize_host
from .workspace import relative_path


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _digest(value, field):
    if type(value) is not str or not _DIGEST.fullmatch(value):
        v.fail("CONFIG_VALUE", field, "Expected a SHA-256 digest.")
    return value


def _posix_absolute(value, field):
    value = v.string(value, field)
    path = PurePosixPath(value)
    if (not path.is_absolute() or "\\" in value or ".." in path.parts or value != str(path)
            or any(part in {".", ""} for part in path.parts[1:])):
        v.fail("CONFIG_PATH", field, "Expected a normalized absolute POSIX path.")
    return value


def _identifier_map(raw, field):
    return v.mapping(raw, field, nonempty=True)


@dataclass(frozen=True)
class PlatformProfile:
    id: str
    os_id: str
    os_major: int
    architecture: str
    process_isolation: str
    network_enforcement: str
    filesystem_enforcement: str
    helper_path: str
    helper_sha256: str
    verification: str
    evidence_sha256: str
    verified_at: str | None


@dataclass(frozen=True)
class RunnerSlot:
    id: str
    uid: int
    gid: int
    home_root: str
    egress_profile_id: str


@dataclass(frozen=True)
class ResourceLimits:
    id: str
    cpu_seconds: int
    memory_bytes: int
    processes: int
    file_bytes: int
    output_bytes: int
    disk_bytes: int


@dataclass(frozen=True)
class RunnerPoolProfile:
    id: str
    workspace_root: str
    protected_roots: tuple[str, ...]
    platforms: Mapping[str, PlatformProfile]
    slots: tuple[RunnerSlot, ...]
    resources: Mapping[str, ResourceLimits]


@dataclass(frozen=True)
class ExecutableSpec:
    id: str
    path: str
    sha256: str
    version: str


@dataclass(frozen=True)
class ApprovedCommand:
    id: str
    executable_id: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    resource_profile_id: str
    verification: str
    report_patterns: tuple[str, ...]
    generated_patterns: tuple[str, ...]


@dataclass(frozen=True)
class MavenRuntime:
    settings_file: str
    settings_sha256: str
    truststore_file: str
    truststore_sha256: str
    cache_mode: str
    credential_profile_id: str | None


@dataclass(frozen=True)
class ToolchainProfile:
    id: str
    platform_ids: frozenset[str]
    egress_profile_ids: frozenset[str]
    executables: Mapping[str, ExecutableSpec]
    commands: Mapping[str, ApprovedCommand]
    maven: MavenRuntime | None


@dataclass(frozen=True)
class EgressRoute:
    scheme: str
    host: str
    port: int
    address_ranges: tuple[IPv4Network | IPv6Network, ...]
    purpose: str
    boundary: str


@dataclass(frozen=True)
class EgressProfile:
    id: str
    mode: str
    subject: str
    default_policy: str
    dns_mode: str
    proxy_mode: str
    enforcement: str
    evidence_sha256: str
    routes: tuple[EgressRoute, ...]


@dataclass(frozen=True)
class RuntimeProfiles:
    runner_pool: RunnerPoolProfile
    toolchains: Mapping[str, ToolchainProfile]
    egress: Mapping[str, EgressProfile]
    digest: str


def _parse_runner_pool(raw):
    v.obj(raw, "runner_pool", {"schema_version", "profile_id", "workspace_root", "protected_roots",
                                "platforms", "slots", "resources"})
    v.version(raw["schema_version"], 1)
    profile_id = v.identifier(raw["profile_id"], "runner_pool.profile_id")
    workspace_root = _posix_absolute(raw["workspace_root"], "runner_pool.workspace_root")
    protected = tuple(_posix_absolute(item, "runner_pool.protected_roots")
                      for item in v.unique_strings(raw["protected_roots"], "runner_pool.protected_roots", nonempty=True))
    workspace = PurePosixPath(workspace_root)
    if any(workspace == PurePosixPath(item) or workspace in PurePosixPath(item).parents
           or PurePosixPath(item) in workspace.parents for item in protected):
        v.fail("CONFIG_PATH_OVERLAP", "runner_pool", "Workspace and protected roots must be disjoint.")
    platforms = {}
    for platform_id, value in _identifier_map(raw["platforms"], "runner_pool.platforms").items():
        value = v.obj(value, "runner_pool.platform", {
            "os_id", "os_major", "architecture", "process_isolation", "network_enforcement",
            "filesystem_enforcement", "helper_path", "helper_sha256", "verification",
            "evidence_sha256", "verified_at",
        })
        os_id = v.enum(value["os_id"], {"rhel"}, "platform.os_id")
        os_major = v.integer(value["os_major"], "platform.os_major")
        if os_major not in {7, 8, 9}:
            v.fail("CONFIG_VALUE", "platform.os_major", "Only RHEL 7, 8, and 9 profiles are recognized.")
        architecture = v.enum(value["architecture"], {"x86_64", "aarch64"}, "platform.architecture")
        process_isolation = v.enum(value["process_isolation"], {"cgroup_v1", "cgroup_v2"},
                                   "platform.process_isolation")
        network_enforcement = v.enum(value["network_enforcement"],
                                     {"iptables_owner", "nftables_cgroup"},
                                     "platform.network_enforcement")
        expected_network = "nftables_cgroup" if process_isolation == "cgroup_v2" else "iptables_owner"
        if network_enforcement != expected_network:
            v.fail("CONFIG_VALUE", "platform.network_enforcement",
                   "Network enforcement does not match the cgroup generation.")
        filesystem = v.enum(value["filesystem_enforcement"], {"posix_uid"},
                            "platform.filesystem_enforcement")
        verification = v.enum(value["verification"], {"verified", "unverified"},
                              "platform.verification")
        verified_at = value["verified_at"]
        if verified_at is not None:
            v.string(verified_at, "platform.verified_at")
            try:
                instant = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    raise ValueError
            except ValueError:
                v.fail("CONFIG_VALUE", "platform.verified_at", "Expected a timezone-aware ISO timestamp.")
        if verification == "verified" and verified_at is None:
            v.fail("CONFIG_MISSING_FIELD", "platform.verified_at", "Verified platforms require evidence time.")
        helper_digest = _digest(value["helper_sha256"], "platform.helper_sha256")
        evidence_digest = _digest(value["evidence_sha256"], "platform.evidence_sha256")
        if verification == "verified" and "0" * 64 in {helper_digest, evidence_digest}:
            v.fail("CONFIG_VALUE", "platform.verification", "Verified evidence cannot use placeholder digests.")
        platforms[platform_id] = PlatformProfile(
            platform_id, os_id, os_major, architecture, process_isolation, network_enforcement,
            filesystem, _posix_absolute(value["helper_path"], "platform.helper_path"),
            helper_digest, verification, evidence_digest, verified_at,
        )
    for platform in platforms.values():
        helper = PurePosixPath(platform.helper_path)
        if not any(helper == PurePosixPath(root) or PurePosixPath(root) in helper.parents for root in protected):
            v.fail("CONFIG_PATH", "platform.helper_path", "Runner helper must be under a protected root.")
    slots = []
    for item in v.array(raw["slots"], "runner_pool.slots", nonempty=True):
        item = v.obj(item, "runner_pool.slot", {"id", "uid", "gid", "home_root", "egress_profile_id"})
        slots.append(RunnerSlot(v.identifier(item["id"], "slot.id"),
                                v.integer(item["uid"], "slot.uid"),
                                v.integer(item["gid"], "slot.gid"),
                                _posix_absolute(item["home_root"], "slot.home_root"),
                                v.identifier(item["egress_profile_id"], "slot.egress_profile_id")))
    if (len({slot.id for slot in slots}) != len(slots)
            or len({slot.uid for slot in slots}) != len(slots)
            or len({slot.gid for slot in slots}) != len(slots)
            or len({slot.home_root for slot in slots}) != len(slots)):
        v.fail("CONFIG_DUPLICATE", "runner_pool.slots", "Runner slot IDs, UIDs, GIDs, and homes must be unique.")
    if any(PurePosixPath(slot.home_root) == workspace or PurePosixPath(slot.home_root) in workspace.parents
           or workspace in PurePosixPath(slot.home_root).parents for slot in slots):
        v.fail("CONFIG_PATH_OVERLAP", "runner_pool.slots", "Runner homes and workspace root must be disjoint.")
    resources = {}
    for resource_id, value in _identifier_map(raw["resources"], "runner_pool.resources").items():
        value = v.obj(value, "runner_pool.resource", {"cpu_seconds", "memory_bytes", "processes",
                                                       "file_bytes", "output_bytes", "disk_bytes"})
        resources[resource_id] = ResourceLimits(resource_id, *(
            v.integer(value[key], "resource." + key) for key in
            ("cpu_seconds", "memory_bytes", "processes", "file_bytes", "output_bytes", "disk_bytes")
        ))
        limits = resources[resource_id]
        if limits.file_bytes > limits.disk_bytes or limits.output_bytes > limits.disk_bytes:
            v.fail("CONFIG_VALUE", "runner_pool.resources", "File and output limits cannot exceed the disk limit.")
    return RunnerPoolProfile(profile_id, workspace_root, protected, MappingProxyType(platforms),
                             tuple(slots), MappingProxyType(resources))


def _patterns(value, field):
    result = v.unique_strings(value, field)
    for pattern in result:
        relative_path(pattern, pattern=True)
    return result


def _parse_toolchains(raw, pool):
    v.obj(raw, "toolchains", {"schema_version", "profile_id", "toolchains"})
    v.version(raw["schema_version"], 1)
    v.identifier(raw["profile_id"], "toolchains.profile_id")
    result = {}
    executable_paths = set()
    for toolchain_id, value in _identifier_map(raw["toolchains"], "toolchains.toolchains").items():
        value = v.obj(value, "toolchain", {"platform_ids", "egress_profile_ids", "executables", "commands"}, {"maven"})
        platform_ids = frozenset(v.unique_strings(value["platform_ids"], "toolchain.platform_ids", nonempty=True))
        egress_profile_ids = frozenset(v.unique_strings(value["egress_profile_ids"],
                                                         "toolchain.egress_profile_ids", nonempty=True))
        if platform_ids - pool.platforms.keys():
            v.fail("CONFIG_REFERENCE", "toolchain.platform_ids", "Toolchain references an unknown platform.")
        executables = {}
        for executable_id, executable in _identifier_map(value["executables"], "toolchain.executables").items():
            executable = v.obj(executable, "toolchain.executable", {"path", "sha256", "version"})
            path = _posix_absolute(executable["path"], "executable.path")
            if path in executable_paths:
                v.fail("CONFIG_DUPLICATE", "executable.path", "Executable paths must be unique.")
            executable_paths.add(path)
            executables[executable_id] = ExecutableSpec(executable_id, path,
                _digest(executable["sha256"], "executable.sha256"),
                v.string(executable["version"], "executable.version"))
        commands = {}
        for command_id, command in _identifier_map(value["commands"], "toolchain.commands").items():
            command = v.obj(command, "toolchain.command", {"executable_id", "argv", "cwd", "timeout_seconds",
                "resource_profile_id", "verification", "report_patterns", "generated_patterns"})
            executable_id = v.identifier(command["executable_id"], "command.executable_id")
            if executable_id not in executables:
                v.fail("CONFIG_REFERENCE", "command.executable_id", "Command references an unknown executable.")
            resource = v.identifier(command["resource_profile_id"], "command.resource_profile_id")
            if resource not in pool.resources:
                v.fail("CONFIG_REFERENCE", "command.resource_profile_id", "Command references an unknown resource profile.")
            argv = tuple(v.string(arg, "command.argv") for arg in v.array(command["argv"], "command.argv"))
            cwd = relative_path(command["cwd"])
            verification = v.enum(command["verification"], {"exit_code", "junit"}, "command.verification")
            reports = _patterns(command["report_patterns"], "command.report_patterns")
            generated = _patterns(command["generated_patterns"], "command.generated_patterns")
            if verification == "junit" and not reports:
                v.fail("CONFIG_TYPE", "command.report_patterns", "JUnit commands require report patterns.")
            commands[command_id] = ApprovedCommand(command_id, executable_id, argv, cwd,
                v.integer(command["timeout_seconds"], "command.timeout_seconds"), resource,
                verification, reports, generated)
        maven = None
        if "maven" in value:
            item = v.obj(value["maven"], "toolchain.maven", {"settings_file", "settings_sha256",
                "truststore_file", "truststore_sha256", "cache_mode", "credential_profile_id"})
            credential = item["credential_profile_id"]
            if credential is not None:
                credential = v.identifier(credential, "maven.credential_profile_id")
            maven = MavenRuntime(_posix_absolute(item["settings_file"], "maven.settings_file"),
                _digest(item["settings_sha256"], "maven.settings_sha256"),
                _posix_absolute(item["truststore_file"], "maven.truststore_file"),
                _digest(item["truststore_sha256"], "maven.truststore_sha256"),
                v.enum(item["cache_mode"], {"per_run"}, "maven.cache_mode"), credential)
        result[toolchain_id] = ToolchainProfile(toolchain_id, platform_ids, egress_profile_ids,
            MappingProxyType(executables), MappingProxyType(commands), maven)
    return MappingProxyType(result)


def _parse_egress(raw):
    v.obj(raw, "egress", {"schema_version", "profile_id", "profiles"})
    v.version(raw["schema_version"], 1)
    v.identifier(raw["profile_id"], "egress.profile_id")
    result = {}
    for profile_id, value in _identifier_map(raw["profiles"], "egress.profiles").items():
        value = v.obj(value, "egress.profile", {"mode", "subject", "default_policy", "dns_mode", "proxy_mode",
                                                "enforcement", "evidence_sha256", "routes"})
        mode = v.enum(value["mode"], {"production", "test"}, "egress.mode")
        routes = []
        for route in v.array(value["routes"], "egress.routes"):
            route = v.obj(route, "egress.route", {"scheme", "host", "port", "address_ranges", "purpose", "boundary"})
            ranges = []
            for address in v.unique_strings(route["address_ranges"], "egress.address_ranges", nonempty=True):
                try:
                    network = ip_network(address, strict=True)
                except ValueError:
                    v.fail("CONFIG_VALUE", "egress.address_ranges", "Expected a canonical IP network.")
                if network.prefixlen == 0:
                    v.fail("CONFIG_VALUE", "egress.address_ranges", "A default route is not permitted.")
                ranges.append(network)
            port = v.integer(route["port"], "egress.port")
            if port > 65535:
                v.fail("CONFIG_VALUE", "egress.port", "Network port is outside the valid range.")
            boundary = v.enum(route["boundary"], {"internal", "test_external"}, "egress.boundary")
            if mode == "production" and boundary != "internal":
                v.fail("NETWORK_DENIED", "egress.boundary", "Production runner egress must remain internal.")
            routes.append(EgressRoute(v.enum(route["scheme"], {"https", "http", "tcp"}, "egress.scheme"),
                                      normalize_host(route["host"]), port, tuple(ranges),
                                      v.identifier(route["purpose"], "egress.purpose"), boundary))
        identities = [(route.scheme, route.host, route.port, route.address_ranges) for route in routes]
        if len(set(identities)) != len(identities):
            v.fail("CONFIG_DUPLICATE", "egress.routes", "Egress routes must be unique.")
        enforcement = v.enum(value["enforcement"], {"verified", "unverified"}, "egress.enforcement")
        evidence = _digest(value["evidence_sha256"], "egress.evidence_sha256")
        if enforcement == "verified" and evidence == "0" * 64:
            v.fail("CONFIG_VALUE", "egress.evidence_sha256", "Verified egress cannot use placeholder evidence.")
        result[profile_id] = EgressProfile(
            profile_id, mode, v.enum(value["subject"], {"runner"}, "egress.subject"),
            v.enum(value["default_policy"], {"deny"}, "egress.default_policy"),
            v.enum(value["dns_mode"], {"controlled", "disabled"}, "egress.dns_mode"),
            v.enum(value["proxy_mode"], {"none"}, "egress.proxy_mode"),
            enforcement, evidence, tuple(routes),
        )
    return MappingProxyType(result)


def parse_runtime_profiles(runner_pool_raw, toolchains_raw, egress_raw):
    pool = _parse_runner_pool(runner_pool_raw)
    toolchains = _parse_toolchains(toolchains_raw, pool)
    egress = _parse_egress(egress_raw)
    if {slot.egress_profile_id for slot in pool.slots} - egress.keys():
        v.fail("CONFIG_REFERENCE", "runner_pool.slots", "Runner slot references an unknown egress profile.")
    slot_egress = {slot.egress_profile_id for slot in pool.slots}
    for toolchain in toolchains.values():
        if toolchain.egress_profile_ids - egress.keys():
            v.fail("CONFIG_REFERENCE", "toolchain.egress_profile_ids", "Toolchain references unknown egress.")
        if toolchain.egress_profile_ids - slot_egress:
            v.fail("CONFIG_REFERENCE", "toolchain.egress_profile_ids", "No runner slot provides required egress.")
    workspace = PurePosixPath(pool.workspace_root)
    protected = tuple(PurePosixPath(path) for path in pool.protected_roots)
    for toolchain in toolchains.values():
        paths = [PurePosixPath(item.path) for item in toolchain.executables.values()]
        if toolchain.maven:
            paths.extend((PurePosixPath(toolchain.maven.settings_file),
                          PurePosixPath(toolchain.maven.truststore_file)))
        if any(path == workspace or workspace in path.parents for path in paths):
            v.fail("CONFIG_PATH_OVERLAP", "toolchains", "Trusted runtime files cannot be inside workspaces.")
        if any(not any(path == root or root in path.parents for root in protected) for path in paths):
            v.fail("CONFIG_PATH", "toolchains", "Trusted runtime files must be under a protected root.")
    digest = v.canonical_digest({"runner_pool": runner_pool_raw, "toolchains": toolchains_raw,
                                 "egress": egress_raw})
    return RuntimeProfiles(pool, toolchains, egress, digest)


def load_runtime_profiles(runner_pool_path: Path, toolchains_path: Path,
                          egress_path: Path) -> RuntimeProfiles:
    return parse_runtime_profiles(v.read_json(runner_pool_path), v.read_json(toolchains_path),
                                  v.read_json(egress_path))
