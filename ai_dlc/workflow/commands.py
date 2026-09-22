"""Parse a single explicit command from the first nonblank comment line."""

from dataclasses import dataclass, field

from ..errors import AgentError


@dataclass(frozen=True)
class Command:
    name: str
    revision: str | None = None
    options: dict[str, str] = field(default_factory=dict)
    explanation: str = ""


def parse_command(body: str) -> Command | None:
    lines = body.splitlines()
    position = next((i for i, line in enumerate(lines) if line.strip()), None)
    if position is None:
        return None
    line = lines[position]
    # Four spaces/tabs, quotes, and fences are Markdown content, not commands.
    if line.startswith(("    ", "\t")):
        return None
    line = line.strip()
    if not (line == "/aidlc" or line.startswith("/aidlc ")):
        return None
    words = line.split()[1:]
    explanation = "\n".join(lines[position + 1:]).strip()
    if words and words[0] == "approve" and len(words) >= 3 and words[1] in {"requirements", "design"}:
        name, revision, rest = "approve_" + words[1], words[2], words[3:]
        permitted = {"start", "design"} if words[1] == "requirements" else set()
    elif words and words[0] in {"start", "amend"} and len(words) >= 2:
        name, revision, rest = words[0], words[1], words[2:]
        permitted = {"design"} if name == "start" else set()
    elif len(words) == 1 and words[0] in {"stop", "resume", "cancel", "status"}:
        return Command(words[0], explanation=explanation)
    else:
        raise AgentError("COMMAND_INVALID", "Unsupported command or missing arguments.")
    options = {}
    for token in rest:
        parts = token.split("=")
        if len(parts) != 2 or parts[0] not in permitted or parts[0] in options or not parts[1]:
            raise AgentError("COMMAND_INVALID", "Unknown, duplicate, or malformed command option.")
        options[parts[0]] = parts[1]
    if name == "amend" and not explanation:
        raise AgentError("COMMAND_INVALID", "An amendment must include its proposed change below the command.")
    return Command(name, revision, options, explanation)
