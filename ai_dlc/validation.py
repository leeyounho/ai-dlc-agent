"""Small strict JSON primitives shared by configuration and evaluation.

Never coerce input, execute expressions, expand environment variables, or include
input values in validation errors. Runtime integrations can wrap these contracts.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from .errors import AgentError

MAX_JSON_BYTES = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def fail(code: str, field: str, message: str) -> None:
    raise AgentError(code, message, field=field)


def obj(value, field: str, required: set[str], optional: set[str] | None = None) -> dict:
    if type(value) is not dict:
        fail("CONFIG_TYPE", field, "Expected an object.")
    if not required <= value.keys():
        fail("CONFIG_MISSING_FIELD", field, "Required fields are missing.")
    if value.keys() - required - (optional or set()):
        fail("CONFIG_UNKNOWN_FIELD", field, "Unrecognized fields are not allowed.")
    return value


def mapping(value, field: str, *, nonempty: bool = True) -> dict:
    if type(value) is not dict or (nonempty and not value):
        fail("CONFIG_TYPE", field, "Expected an object with valid identifiers.")
    for key in value:
        identifier(key, field)
    return value


def string(value, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        fail("CONFIG_TYPE", field, "Expected a nonempty string without surrounding whitespace.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail("CONFIG_VALUE", field, "Control characters are not allowed.")
    return value


def identifier(value, field: str) -> str:
    value = string(value, field)
    if not IDENTIFIER.fullmatch(value):
        fail("CONFIG_VALUE", field, "Invalid identifier.")
    return value


def env_name(value, field: str) -> str:
    value = string(value, field)
    if not ENVIRONMENT_NAME.fullmatch(value):
        fail("CONFIG_VALUE", field, "Expected an environment variable name, not its value.")
    return value


def enum(value, choices, field: str):
    if type(value) is not str or value not in choices:
        fail("CONFIG_VALUE", field, "Unrecognized option.")
    return value


def boolean(value, field: str) -> bool:
    if type(value) is not bool:
        fail("CONFIG_TYPE", field, "Expected a boolean.")
    return value


def integer(value, field: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        fail("CONFIG_TYPE", field, "Expected an integer in the permitted range.")
    return value


def array(value, field: str, *, nonempty: bool = False) -> list:
    if type(value) is not list or (nonempty and not value):
        fail("CONFIG_TYPE", field, "Expected an array.")
    return value


def unique_strings(value, field: str, *, nonempty: bool = False) -> tuple[str, ...]:
    items = tuple(string(x, field) for x in array(value, field, nonempty=nonempty))
    if len(set(items)) != len(items):
        fail("CONFIG_DUPLICATE", field, "Duplicate entries are not allowed.")
    return items


def version(value, expected: int) -> None:
    if type(value) is not int or value != expected:
        fail("CONFIG_VERSION", "schema_version", "Unsupported schema version for this document type.")


def canonical_digest(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def local_path(value, field: str = "path") -> Path:
    """Reject explicit remote/device paths before filesystem resolution or I/O."""
    try:
        text = os.fspath(value)
    except TypeError:
        fail("CONFIG_PATH", field, "Expected a local filesystem path.")
    string(text, field)
    normalized = text.replace("\\", "/")
    if normalized.startswith("//") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text) or text.lower().startswith("file:"):
        fail("CONFIG_PATH", field, "Remote and device filesystem paths are not allowed.")
    return Path(text)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("CONFIG_DUPLICATE", "$", "Duplicate JSON keys are not allowed.")
        result[key] = value
    return result


def _invalid_constant(_value):
    fail("CONFIG_VALUE", "$", "Non-finite JSON numbers are not allowed.")


def read_json(path: Path) -> dict:
    path = local_path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
    except (OSError, ValueError):
        raise AgentError("CONFIG_READ", "Unable to read the requested local JSON file.") from None
    return decode_json(raw)


def decode_json(raw: bytes) -> dict:
    """Decode bounded JSON bytes after the caller has enforced its I/O boundary."""
    if len(raw) > MAX_JSON_BYTES:
        raise AgentError("CONFIG_SIZE", "JSON file exceeds the size limit.")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs,
                          parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise AgentError("CONFIG_JSON", "Invalid JSON document.") from None
    if type(data) is not dict:
        fail("CONFIG_TYPE", "$", "Expected a JSON object.")
    return data
