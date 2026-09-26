"""Value-only model contracts. Model output never grants tool permissions."""

from dataclasses import asdict, dataclass
import json
import math
import re

from ..errors import AgentError
from ..validation import decode_json_value


class ModelError(AgentError):
    def __init__(self, code, *, retryable=False, effect_state="none"):
        super().__init__(code, "Model operation could not be completed under the configured contract.")
        self.retryable = retryable
        self.effect_state = effect_state

    def as_dict(self):
        return {**super().as_dict(), "retryable": self.retryable, "effect_state": self.effect_state}


def require(condition, code="MODEL_PROTOCOL"):
    if not condition:
        raise ModelError(code)


def json_text(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ModelError("MODEL_PROTOCOL") from None


def json_value(text):
    require(type(text) is str)
    try:
        return decode_json_value(text.encode("utf-8"))
    except (AgentError, UnicodeError):
        raise ModelError("MODEL_PROTOCOL") from None


def identifier(value):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None)
    return value


def positive(value):
    require(type(value) is int and value > 0, "MODEL_REQUEST")


def validate_schema(schema, depth=0):
    """Deliberately closed JSON Schema subset; unsupported rules are never ignored."""
    require(depth <= 16 and type(schema) is dict, "MODEL_TOOL_SCHEMA")
    kind = schema.get("type")
    require(type(kind) is str, "MODEL_TOOL_SCHEMA")
    allowed = {"type", "description", "enum"}
    if kind == "object":
        allowed |= {"properties", "required", "additionalProperties"}
        props, required = schema.get("properties"), schema.get("required")
        require(type(props) is dict and type(required) is list
                and all(type(k) is str for k in required)
                and len(set(required)) == len(required) and set(required) <= props.keys()
                and schema.get("additionalProperties") is False, "MODEL_TOOL_SCHEMA")
        for name, child in props.items():
            require(type(name) is str and bool(name), "MODEL_TOOL_SCHEMA")
            validate_schema(child, depth + 1)
    elif kind == "array":
        allowed |= {"items", "minItems", "maxItems"}
        validate_schema(schema.get("items"), depth + 1)
    elif kind == "string":
        allowed |= {"minLength", "maxLength"}
    elif kind in {"number", "integer"}:
        allowed |= {"minimum", "maximum"}
    else:
        require(kind in {"boolean", "null"}, "MODEL_TOOL_SCHEMA")
    require(not schema.keys() - allowed, "MODEL_TOOL_SCHEMA")
    if "description" in schema:
        require(type(schema["description"]) is str, "MODEL_TOOL_SCHEMA")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            require(type(schema[key]) is int and schema[key] >= 0, "MODEL_TOOL_SCHEMA")
    for key in ("minimum", "maximum"):
        if key in schema:
            require(type(schema[key]) in {int, float} and math.isfinite(schema[key]), "MODEL_TOOL_SCHEMA")
    for low, high in (("minLength", "maxLength"), ("minItems", "maxItems"), ("minimum", "maximum")):
        if low in schema and high in schema:
            require(schema[low] <= schema[high], "MODEL_TOOL_SCHEMA")
    if "enum" in schema:
        require(type(schema["enum"]) is list and bool(schema["enum"]), "MODEL_TOOL_SCHEMA")
        json_text(schema["enum"])


def validate_arguments(value, schema):
    kind = schema["type"]
    valid = {"object": type(value) is dict, "array": type(value) is list,
             "string": type(value) is str, "integer": type(value) is int,
             "number": type(value) in {int, float}, "boolean": type(value) is bool,
             "null": value is None}[kind]
    require(valid, "MODEL_TOOL_ARGUMENTS")
    if "enum" in schema:
        require(json_text(value) in [json_text(x) for x in schema["enum"]], "MODEL_TOOL_ARGUMENTS")
    if kind == "object":
        require(set(schema["required"]) <= value.keys()
                and not value.keys() - schema["properties"].keys(), "MODEL_TOOL_ARGUMENTS")
        for key, child in value.items():
            validate_arguments(child, schema["properties"][key])
    elif kind == "array":
        for child in value:
            validate_arguments(child, schema["items"])
    if kind in {"string", "array"}:
        low, high = ("minLength", "maxLength") if kind == "string" else ("minItems", "maxItems")
        require(schema.get(low, 0) <= len(value) <= schema.get(high, 2**31), "MODEL_TOOL_ARGUMENTS")
    if kind in {"integer", "number"}:
        require(math.isfinite(value) and schema.get("minimum", -math.inf) <= value
                <= schema.get("maximum", math.inf), "MODEL_TOOL_ARGUMENTS")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters_json: str

    def __post_init__(self):
        identifier(self.name)
        require(type(self.description) is str, "MODEL_TOOL_SCHEMA")
        schema = json_value(self.parameters_json)
        validate_schema(schema)
        require(schema["type"] == "object", "MODEL_TOOL_SCHEMA")
        object.__setattr__(self, "parameters_json", json_text(schema))


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments_json: str

    def __post_init__(self):
        identifier(self.id)
        identifier(self.name)
        value = json_value(self.arguments_json)
        require(type(value) is dict, "MODEL_TOOL_ARGUMENTS")
        object.__setattr__(self, "arguments_json", json_text(value))


@dataclass(frozen=True)
class Message:
    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    # Replayable provider items are confined to the same Responses session.
    provider_items_json: str | None = None

    def __post_init__(self):
        require(type(self.role) is str and self.role in {"system", "user", "assistant", "tool"}
                and type(self.content) is str)
        require(type(self.tool_calls) is tuple and all(type(c) is ToolCall for c in self.tool_calls))
        require(not self.tool_calls or self.role == "assistant")
        require((self.role == "tool") == (self.tool_call_id is not None))
        if self.tool_call_id is not None:
            identifier(self.tool_call_id)
        if self.provider_items_json is not None:
            require(self.role == "assistant" and type(json_value(self.provider_items_json)) is list)


@dataclass(frozen=True)
class ModelRequest:
    request_id: str
    session_id: str
    messages: tuple[Message, ...]
    tools: tuple[ToolDefinition, ...]
    max_output_tokens: int
    timeout_seconds: int

    def __post_init__(self):
        identifier(self.request_id)
        identifier(self.session_id)
        positive(self.max_output_tokens)
        positive(self.timeout_seconds)
        require(type(self.messages) is tuple and bool(self.messages)
                and all(type(m) is Message for m in self.messages), "MODEL_REQUEST")
        require(type(self.tools) is tuple and all(type(t) is ToolDefinition for t in self.tools), "MODEL_REQUEST")
        require(len({t.name for t in self.tools}) == len(self.tools), "MODEL_REQUEST")
        pending, seen = set(), set()
        for message in self.messages:
            if message.role == "tool":
                require(message.tool_call_id in pending, "MODEL_REQUEST")
                pending.remove(message.tool_call_id)
            else:
                require(not pending, "MODEL_REQUEST")
            for call in message.tool_calls:
                require(call.id not in seen, "MODEL_REQUEST")
                seen.add(call.id)
                pending.add(call.id)
        require(not pending, "MODEL_REQUEST")


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None

    def __post_init__(self):
        for value in (self.input_tokens, self.output_tokens, self.total_tokens):
            require(value is None or type(value) is int and value >= 0)
        if all(x is not None for x in (self.input_tokens, self.output_tokens, self.total_tokens)):
            require(self.input_tokens + self.output_tokens == self.total_tokens)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    usage: Usage | None = None
    provider_request_id: str | None = None
    provider_items_json: str | None = None

    def __post_init__(self):
        require(type(self.text) is str and type(self.tool_calls) is tuple
                and all(type(c) is ToolCall for c in self.tool_calls))
        require(type(self.finish_reason) is str and self.finish_reason in {"stop", "tool_calls"}
                and bool(self.tool_calls) == (self.finish_reason == "tool_calls"))
        require(len({c.id for c in self.tool_calls}) == len(self.tool_calls))
        require(self.usage is None or type(self.usage) is Usage)
        if self.provider_request_id is not None:
            identifier(self.provider_request_id)
        if self.provider_items_json is not None:
            require(type(json_value(self.provider_items_json)) is list)

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(value["text"], tuple(ToolCall(**c) for c in value["tool_calls"]),
                   value["finish_reason"], Usage(**value["usage"]) if value["usage"] is not None else None,
                   value["provider_request_id"], value["provider_items_json"])


def validate_response(response, request):
    require(type(response) is ModelResponse)
    definitions = {t.name: json_value(t.parameters_json) for t in request.tools}
    historical = {c.id for m in request.messages for c in m.tool_calls}
    for call in response.tool_calls:
        require(call.name in definitions and call.id not in historical, "MODEL_TOOL_ARGUMENTS")
        validate_arguments(json_value(call.arguments_json), definitions[call.name])
    if response.usage and response.usage.output_tokens is not None:
        require(response.usage.output_tokens <= request.max_output_tokens, "MODEL_OUTPUT_LIMIT")
    return response
