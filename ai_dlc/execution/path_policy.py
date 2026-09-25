"""Shared model-workspace path exclusions."""

import fnmatch


DEFAULT_DENIED_PATTERNS = (
    ".git", ".git/**", "**/.git", "**/.git/**",
    ".aidlc", ".aidlc/**", "**/.aidlc", "**/.aidlc/**",
    ".env", ".env.*", "**/.env", "**/.env.*",
    "*secret*", "**/*secret*", "*credential*", "**/*credential*",
    "*.pem", "**/*.pem", "*.key", "**/*.key",
)


def denied_path(path: str, patterns=DEFAULT_DENIED_PATTERNS) -> bool:
    folded = path.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in patterns)
