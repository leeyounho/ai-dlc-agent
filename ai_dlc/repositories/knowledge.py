"""Commit-bound ADR/knowledge indexing and Git-change proposal helpers."""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re

from ..config.types import KnowledgeConfig
from ..errors import AgentError
from ..execution.workspace import Workspace, read_file, relative_path


MAX_KNOWLEDGE_FILE_BYTES = 128 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
_STATUS = re.compile(r"(?im)^\s*(?:status|상태)\s*:\s*([^\r\n]+)")
_SUPERSEDES = re.compile(r"(?im)^\s*(?:supersedes|대체)\s*:\s*([^\r\n]+)")
_WORDS = re.compile(r"[A-Za-z0-9가-힣_.-]{2,}")


@dataclass(frozen=True)
class KnowledgeItem:
    kind: str
    path: str
    commit: str
    sha256: str
    title: str
    status: str
    supersedes: tuple[str, ...]
    text: str

    @property
    def provenance(self):
        return {"commit": self.commit, "path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class KnowledgeContext:
    purpose: str
    query: str
    items: tuple[KnowledgeItem, ...]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeChangeProposal:
    kind: str
    path: str
    content: str
    expected_absent: bool = True


@dataclass(frozen=True)
class KnowledgeIndex:
    commit: str
    items: tuple[KnowledgeItem, ...]
    conflicts: tuple[str, ...]
    adr_directory: str
    kb_directory: str

    def context(self, query: str, *, purpose: str, max_items: int = 5,
                max_bytes: int = MAX_CONTEXT_BYTES) -> KnowledgeContext:
        if purpose not in {"design", "review"}:
            raise AgentError("KNOWLEDGE_PURPOSE", "Knowledge context is limited to design and review.")
        if (type(query) is not str or not query.strip() or len(query.encode("utf-8")) > 4096
                or any(ord(char) < 32 and char not in "\t\n\r" for char in query)
                or not 1 <= max_items <= 20 or not 1 <= max_bytes <= MAX_CONTEXT_BYTES):
            raise AgentError("KNOWLEDGE_QUERY", "Knowledge query limits are invalid.")
        terms = {word.casefold() for word in _WORDS.findall(query)}
        ranked = []
        for item in self.items:
            title_words = {word.casefold() for word in _WORDS.findall(item.title)}
            body_words = {word.casefold() for word in _WORDS.findall(item.text)}
            score = 5 * len(terms & title_words) + len(terms & body_words)
            if score:
                ranked.append((score, item))
        selected, used = [], 0
        for _, item in sorted(ranked, key=lambda value: (-value[0], value[1].path)):
            size = len(item.text.encode("utf-8"))
            if len(selected) >= max_items or used + size > max_bytes:
                continue
            selected.append(item)
            used += size
        return KnowledgeContext(purpose, query.strip(), tuple(selected), self.conflicts)

    def propose_adr(self, slug: str, *, title: str, context: str, decision: str,
                    consequences: str) -> KnowledgeChangeProposal:
        values = (title, context, decision, consequences)
        if (not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
                or any(type(value) is not str or not value.strip() for value in values)):
            raise AgentError("KNOWLEDGE_PROPOSAL", "ADR proposal fields are invalid.")
        path = relative_path(f"{self.adr_directory.rstrip('/')}/{slug}.md")
        content = (f"# {title.strip()}\n\nStatus: proposed\n\n## Context\n\n{context.strip()}\n\n"
                   f"## Decision\n\n{decision.strip()}\n\n## Consequences\n\n{consequences.strip()}\n")
        if len(content.encode("utf-8")) > MAX_KNOWLEDGE_FILE_BYTES:
            raise AgentError("KNOWLEDGE_PROPOSAL", "ADR proposal exceeds the file limit.")
        return KnowledgeChangeProposal("adr", path, content)

    def propose_kb(self, slug: str, *, title: str, body: str) -> KnowledgeChangeProposal:
        if (not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
                or type(title) is not str or not title.strip() or type(body) is not str or not body.strip()):
            raise AgentError("KNOWLEDGE_PROPOSAL", "Knowledge proposal fields are invalid.")
        path = relative_path(f"{self.kb_directory.rstrip('/')}/{slug}.md")
        content = f"# {title.strip()}\n\n{body.strip()}\n"
        if len(content.encode("utf-8")) > MAX_KNOWLEDGE_FILE_BYTES:
            raise AgentError("KNOWLEDGE_PROPOSAL", "Knowledge proposal exceeds the file limit.")
        return KnowledgeChangeProposal("kb", path, content)


def _directory(files: set[str], candidates: tuple[str, ...], fallback: str) -> str:
    for candidate in candidates:
        prefix = candidate.rstrip("/") + "/"
        if any(path.startswith(prefix) for path in files):
            return candidate
    return fallback


def discover_knowledge(workspace: Workspace, commit: str, config: KnowledgeConfig) -> KnowledgeIndex:
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise AgentError("GIT_REVISION", "Knowledge discovery requires an exact commit.")
    workspace.verify()
    relative_path(config.adr_path_if_absent)
    relative_path(config.kb_path_if_absent)
    manifest = {item.path: item for item in workspace.files}
    files = set(manifest)
    adr = _directory(files, ("docs/adr", "adr", "docs/decisions"), config.adr_path_if_absent)
    kb = _directory(files, ("docs/knowledge", "knowledge", "docs/kb"), config.kb_path_if_absent)
    roots = (("adr", adr), ("kb", kb))
    items = []
    for kind, root in roots:
        prefix = root.rstrip("/") + "/"
        for path in sorted(item for item in files if item.startswith(prefix) and item.lower().endswith(".md")):
            raw = read_file(workspace.root, path, limit=MAX_KNOWLEDGE_FILE_BYTES)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise AgentError("KNOWLEDGE_TEXT", "Knowledge file is not UTF-8 text.") from None
            title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), PurePosixPath(path).stem)
            status_match, supersedes_match = _STATUS.search(text), _SUPERSEDES.search(text)
            status = status_match.group(1).strip().casefold() if status_match else "unspecified"
            supersedes = tuple(part.strip() for part in re.split(r"[, ]+", supersedes_match.group(1)) if part.strip()) \
                if supersedes_match else ()
            items.append(KnowledgeItem(kind, path, commit, manifest[path].sha256, title, status,
                                       supersedes, text))
    conflicts = []
    active = [item for item in items if item.kind == "adr"
              and item.status not in {"superseded", "rejected", "deprecated"}]
    by_title = {}
    for item in active:
        by_title.setdefault(item.title.casefold(), []).append(item)
    superseded_refs = {ref.casefold() for item in active for ref in item.supersedes}
    for title, matching in by_title.items():
        unresolved = [item for item in matching
                      if item.path.casefold() not in superseded_refs and PurePosixPath(item.path).name.casefold() not in superseded_refs]
        if len(unresolved) > 1:
            conflicts.append("ADR_CONFLICT:" + ",".join(item.path for item in unresolved))
    return KnowledgeIndex(commit, tuple(items), tuple(conflicts), adr, kb)
