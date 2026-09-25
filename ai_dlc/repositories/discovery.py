"""Deterministic repository inspection that proposes, but never executes, commands."""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Mapping
import xml.etree.ElementTree as ET

from .. import validation as v
from ..config.types import CommandProfile, ProjectProfile
from ..errors import AgentError
from ..execution.workspace import Workspace, read_file, relative_path
from ..github.client import RepositoryPolicyObservation


MAX_RULE_BYTES = 512 * 1024
RULE_PRIORITIES = MappingProxyType({
    "readme": 10,
    "build": 20,
    "workflow": 30,
    "agents": 40,
    "codeowners": 50,
    "github_policy": 60,
    "repository_profile": 80,
    "operator_toolchain": 100,
})
_COMMAND_HINT = re.compile(r"(?im)^\s*(?:build|test|validation)\s+command\s*[:=]\s*`?([^`\r\n]+)`?\s*$")


@dataclass(frozen=True)
class RepositoryRule:
    kind: str
    source: str
    commit: str
    sha256: str
    priority: int
    summary: str


@dataclass(frozen=True)
class DiscoveryConflict:
    code: str
    message: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class TrustedToolchainProfile:
    """Operator-owned ceiling. Repository content cannot alter these fields."""

    id: str
    executable_ids: frozenset[str]
    jdk_major: int | None = None
    maven_executable_id: str | None = None
    python_executable_id: str | None = None
    maven_settings_id: str | None = None
    maven_cache_id: str | None = None
    network_profile: str = "offline"
    command_timeout_seconds: int = 900

    def __post_init__(self):
        v.identifier(self.id, "toolchain.id")
        if not self.executable_ids:
            raise AgentError("TOOLCHAIN_PROFILE", "Toolchain must allow at least one executable ID.")
        for executable in self.executable_ids:
            v.identifier(executable, "toolchain.executable_id")
        for executable in (self.maven_executable_id, self.python_executable_id):
            if executable is not None and executable not in self.executable_ids:
                raise AgentError("TOOLCHAIN_PROFILE", "Toolchain executable is not in its allowlist.")
        if self.jdk_major is not None:
            v.integer(self.jdk_major, "toolchain.jdk_major")
        for reference in (self.maven_settings_id, self.maven_cache_id):
            if reference is not None:
                v.identifier(reference, "toolchain.maven_reference")
        v.identifier(self.network_profile, "toolchain.network_profile")
        v.integer(self.command_timeout_seconds, "toolchain.command_timeout_seconds")


@dataclass(frozen=True)
class ExecutionProfile:
    basis_commit: str
    adapter: str
    toolchain_id: str
    commands: Mapping[str, CommandProfile]
    rules: tuple[RepositoryRule, ...]
    conflicts: tuple[DiscoveryConflict, ...]
    modules: tuple[str, ...]
    maven_profiles: tuple[str, ...]
    junit_report_patterns: tuple[str, ...]
    maven_settings_id: str | None
    maven_cache_id: str | None
    network_profile: str
    repository_policy: RepositoryPolicyObservation | None
    status: str
    reasons: tuple[str, ...]

    def document(self):
        return {
            "basis_commit": self.basis_commit,
            "adapter": self.adapter,
            "toolchain_id": self.toolchain_id,
            "commands": {name: {
                "executable_id": command.executable_id,
                "argv": list(command.argv), "cwd": command.cwd,
                "timeout_seconds": command.timeout_seconds,
                "report_patterns": list(command.report_patterns),
            } for name, command in sorted(self.commands.items())},
            "rules": [rule.__dict__ for rule in self.rules],
            "conflicts": [conflict.__dict__ for conflict in self.conflicts],
            "modules": list(self.modules),
            "maven_profiles": list(self.maven_profiles),
            "junit_report_patterns": list(self.junit_report_patterns),
            "maven_settings_id": self.maven_settings_id,
            "maven_cache_id": self.maven_cache_id,
            "network_profile": self.network_profile,
            "repository_policy": None if self.repository_policy is None else {
                "default_branch": self.repository_policy.default_branch,
                "protected": self.repository_policy.protected,
                "required_status_checks": list(self.repository_policy.required_status_checks),
                "required_approving_reviews": self.repository_policy.required_approving_reviews,
                "require_code_owner_reviews": self.repository_policy.require_code_owner_reviews,
                "rulesets": [list(rule) for rule in self.repository_policy.rulesets],
            },
            "status": self.status,
            "reasons": list(self.reasons),
        }


def _text(workspace: Workspace, path: str, *, limit=MAX_RULE_BYTES) -> str:
    try:
        return read_file(workspace.root, path, limit=limit).decode("utf-8")
    except UnicodeDecodeError:
        raise AgentError("REPOSITORY_TEXT", "Repository instruction file is not UTF-8 text.") from None


def _kind(path: str) -> str | None:
    name = PurePosixPath(path).name
    lowered = path.lower()
    if name.lower().startswith("readme"):
        return "readme"
    if name.lower() == "agents.md":
        return "agents"
    if lowered in {"codeowners", ".github/codeowners", "docs/codeowners"}:
        return "codeowners"
    if lowered.startswith(".github/workflows/") and lowered.endswith((".yml", ".yaml")):
        return "workflow"
    if (name == "pom.xml" or name in {"mvnw", "mvnw.cmd", "Makefile"}
            or lowered.endswith(("build.sh", "build.ps1", "pyproject.toml"))):
        return "build"
    return None


def _rule_files(workspace: Workspace, commit: str):
    rules, hints = [], {}
    for item in workspace.files:
        kind = _kind(item.path)
        if kind is None:
            continue
        content = _text(workspace, item.path)
        summary = f"{kind} source ({len(content.splitlines())} lines)"
        rules.append(RepositoryRule(kind, item.path, commit, item.sha256,
                                    RULE_PRIORITIES[kind], summary))
        for match in _COMMAND_HINT.finditer(content):
            hint = " ".join(match.group(1).split())
            if len(hint) <= 500:
                hints.setdefault(hint, []).append(item.path)
    return rules, hints


def _direct_value(element, name):
    return next((child.text.strip() for child in element
                 if child.tag.rsplit("}", 1)[-1] == name and child.text and child.text.strip()), None)


def _maven(workspace: Workspace, project: ProjectProfile):
    files = {item.path for item in workspace.files}
    if "pom.xml" not in files:
        return (), (), tuple(project.junit_report_patterns), (), ("MAVEN_POM_MISSING",), ()
    modules, profiles, reports, conflicts, reasons = [], [], set(project.junit_report_patterns), [], []
    pending, visited = ["pom.xml"], set()
    java_versions = set()
    while pending:
        path = pending.pop(0)
        if path in visited:
            continue
        visited.add(path)
        raw = read_file(workspace.root, path, limit=MAX_RULE_BYTES)
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise AgentError("MAVEN_XML", "Maven project XML cannot declare a DTD or entity.")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise AgentError("MAVEN_XML", "Maven project XML is invalid.") from None
        base = PurePosixPath(path).parent
        for plugin in (item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "plugin"):
            if _direct_value(plugin, "artifactId") != "maven-compiler-plugin":
                continue
            for element in plugin.iter():
                if (element.tag.rsplit("}", 1)[-1] in {"source", "target", "release"}
                        and element.text and element.text.strip()):
                    java_versions.add(element.text.strip())
        for element in root.iter():
            local = element.tag.rsplit("}", 1)[-1]
            if local == "module" and element.text and element.text.strip():
                module = (base / element.text.strip()).as_posix().rstrip("/")
                relative_path(module)
                module_pom = f"{module}/pom.xml"
                if module_pom not in files:
                    conflicts.append(DiscoveryConflict("MAVEN_MODULE_MISSING",
                                                       "Declared Maven module has no tracked pom.xml.",
                                                       (path, module_pom)))
                else:
                    modules.append(module)
                    pending.append(module_pom)
            if local == "profile":
                profile_id = next((child.text.strip() for child in element
                                   if child.tag.rsplit("}", 1)[-1] == "id" and child.text), None)
                if profile_id:
                    profiles.append(profile_id)
            if local in {"maven.compiler.source", "maven.compiler.target", "maven.compiler.release"}:
                if element.text and element.text.strip():
                    java_versions.add(element.text.strip())
            if local == "reportsDirectory" and element.text and element.text.strip():
                value = element.text.strip()
                if "${" in value:
                    reasons.append("MAVEN_REPORT_PATH_UNRESOLVED")
                else:
                    report_dir = (base / value).as_posix()
                    relative_path(report_dir)
                    reports.add(f"{report_dir.rstrip('/')}/*.xml")
        module_prefix = "" if path == "pom.xml" else str(base).rstrip("/") + "/"
        reports.add(module_prefix + "target/surefire-reports/TEST-*.xml")
        reports.add(module_prefix + "target/failsafe-reports/TEST-*.xml")
    return (tuple(sorted(set(modules))), tuple(sorted(set(profiles))), tuple(sorted(reports)),
            tuple(conflicts), tuple(sorted(set(reasons))), tuple(sorted(java_versions)))


def discover_execution_profile(workspace: Workspace, commit: str, project: ProjectProfile,
                               toolchain: TrustedToolchainProfile, *,
                               policy: RepositoryPolicyObservation | None = None) -> ExecutionProfile:
    """Inspect a fixed snapshot and return an approval-bound execution proposal."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise AgentError("GIT_REVISION", "Repository discovery requires an exact commit.")
    workspace.verify()
    rules, hints = _rule_files(workspace, commit)
    conflicts, reasons = [], []
    modules, profiles, reports = (), (), tuple(project.junit_report_patterns)
    java_versions = ()
    if project.toolchain_id != toolchain.id:
        reasons.append("TOOLCHAIN_ID_MISMATCH")
    if len(hints) > 1:
        sources = tuple(sorted({path for paths in hints.values() for path in paths}))
        conflicts.append(DiscoveryConflict("REPOSITORY_COMMAND_CONFLICT",
                                           "Repository instruction files name different commands; confirmation is required.",
                                           sources))
    commands = dict(project.commands)
    for command in commands.values():
        if command.executable_id not in toolchain.executable_ids:
            reasons.append("EXECUTABLE_NOT_APPROVED")
        if command.timeout_seconds > toolchain.command_timeout_seconds:
            reasons.append("COMMAND_TIMEOUT_EXCEEDS_TOOLCHAIN")
        if command.cwd != "." and not any(
                item.path.startswith(command.cwd.rstrip("/") + "/") for item in workspace.files):
            reasons.append("COMMAND_CWD_MISSING")
    if project.adapter == "java_maven":
        modules, profiles, reports, maven_conflicts, maven_reasons, java_versions = _maven(workspace, project)
        conflicts.extend(maven_conflicts)
        reasons.extend(maven_reasons)
        if toolchain.jdk_major is None:
            reasons.append("JDK_VERSION_UNSET")
        if toolchain.maven_settings_id is None:
            reasons.append("MAVEN_SETTINGS_UNSET")
        if toolchain.maven_cache_id is None:
            reasons.append("MAVEN_CACHE_UNSET")
        forbidden = {"-s", "--settings", "-gs", "--global-settings"}
        if any(arg in forbidden or arg.startswith("-Dmaven.repo.local=")
               for command in commands.values() for arg in command.argv):
            reasons.append("MAVEN_RUNTIME_OVERRIDE_FORBIDDEN")
        normalized_java = {value[2:] if value.startswith("1.") else value for value in java_versions}
        if any("${" in value for value in java_versions):
            reasons.append("MAVEN_JDK_VERSION_UNRESOLVED")
        elif toolchain.jdk_major is not None and normalized_java and normalized_java != {str(toolchain.jdk_major)}:
            conflicts.append(DiscoveryConflict("JDK_VERSION_CONFLICT",
                                               "Maven compiler version differs from the trusted JDK profile.",
                                               ("pom.xml", "toolchain:" + toolchain.id)))
        if not commands:
            if toolchain.maven_executable_id is None:
                reasons.append("MAVEN_EXECUTABLE_UNSET")
            else:
                commands["test"] = CommandProfile(toolchain.maven_executable_id, ("-B", "test"), ".",
                                                  toolchain.command_timeout_seconds, reports)
    elif not commands:
        reasons.append("COMMAND_UNSET")
    if policy is not None:
        digest = v.canonical_digest({
            "default_branch": policy.default_branch, "protected": policy.protected,
            "required_status_checks": policy.required_status_checks,
            "required_approving_reviews": policy.required_approving_reviews,
            "require_code_owner_reviews": policy.require_code_owner_reviews,
            "rulesets": policy.rulesets,
        })
        summary = (f"default={policy.default_branch}; protected={str(policy.protected).lower()}; "
                   f"checks={len(policy.required_status_checks)}; rulesets={len(policy.rulesets)}")
        rules.append(RepositoryRule("github_policy", "github:repository-policy", commit, digest,
                                    RULE_PRIORITIES["github_policy"], summary))
    rules.extend((
        RepositoryRule("repository_profile", "config:repository", commit,
                       v.canonical_digest({
                           "adapter": project.adapter, "toolchain_id": project.toolchain_id,
                           "rules_source": project.rules_source,
                           "commands": {name: {
                               "executable_id": command.executable_id, "argv": command.argv,
                               "cwd": command.cwd, "timeout_seconds": command.timeout_seconds,
                               "report_patterns": command.report_patterns,
                           } for name, command in sorted(project.commands.items())},
                           "junit_report_patterns": project.junit_report_patterns,
                       }),
                       RULE_PRIORITIES["repository_profile"], "Configured adapter and command overrides"),
        RepositoryRule("operator_toolchain", "operator:toolchain/" + toolchain.id, commit,
                       v.canonical_digest({
                           "id": toolchain.id, "executable_ids": sorted(toolchain.executable_ids),
                           "jdk_major": toolchain.jdk_major,
                           "maven_executable_id": toolchain.maven_executable_id,
                           "python_executable_id": toolchain.python_executable_id,
                           "maven_settings_id": toolchain.maven_settings_id,
                           "maven_cache_id": toolchain.maven_cache_id,
                           "network_profile": toolchain.network_profile,
                           "command_timeout_seconds": toolchain.command_timeout_seconds,
                       }),
                       RULE_PRIORITIES["operator_toolchain"], "Executable, network, and runtime ceiling"),
    ))
    reasons.extend(conflict.code for conflict in conflicts)
    reasons = tuple(sorted(set(reasons)))
    status = "confirmation_required" if reasons else "ready_for_approval"
    return ExecutionProfile(commit, project.adapter, toolchain.id, MappingProxyType(commands),
                            tuple(sorted(rules, key=lambda item: (item.priority, item.source))),
                            tuple(conflicts), modules, profiles, reports,
                            toolchain.maven_settings_id, toolchain.maven_cache_id,
                            toolchain.network_profile, policy, status, reasons)
