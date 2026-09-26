"""Explicit non-streaming protocols over the existing policy-enforcing transport."""

from contextlib import contextmanager
from dataclasses import replace
import threading
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

from ..errors import AgentError
from ..transport import HttpRequest, HttpTransport
from .types import (ModelError, ModelResponse, ToolCall, Usage, identifier, json_text,
                    json_value, positive, require, validate_response)


class ModelConcurrency:
    """Share one instance across all sessions/providers in a service process."""

    def __init__(self, global_limit, provider_limits):
        positive(global_limit)
        for limit in provider_limits.values():
            positive(limit)
        self.global_limit = global_limit
        self.provider_limits = dict(provider_limits)
        self._active = {}
        self._lock = threading.Lock()

    @contextmanager
    def slot(self, provider):
        with self._lock:
            require(self.provider_limits.get(provider.id) == provider.max_concurrent_requests,
                    "MODEL_CONCURRENCY_CONFIG")
            if (sum(self._active.values()) >= self.global_limit
                    or self._active.get(provider.id, 0) >= self.provider_limits[provider.id]):
                raise ModelError("MODEL_BUSY", retryable=True)
            self._active[provider.id] = self._active.get(provider.id, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._active[provider.id] -= 1


class AdapterRegistry:
    """Only bootstrap-installed objects, never import paths supplied by a repo/model."""

    def __init__(self, transport, *, custom=None):
        adapters = {"openai_chat_completions": ChatCompletionsAdapter(transport),
                    "openai_responses": ResponsesAdapter(transport)}
        for name, adapter in (custom or {}).items():
            identifier(name)
            require(callable(getattr(adapter, "generate", None))
                    and type(getattr(adapter, "version", None)) is str and bool(adapter.version),
                    "MODEL_ADAPTER_CONFIG")
            adapters["custom:" + name] = adapter
        self.adapters = MappingProxyType(adapters)

    @property
    def keys(self):
        return frozenset(self.adapters)

    def get(self, key):
        require(key in self.adapters, "MODEL_NOT_CONFIGURED")
        return self.adapters[key]


def _usage(value, input_key, output_key):
    if value is None:
        return None
    require(type(value) is dict)
    return Usage(value.get(input_key), value.get(output_key), value.get("total_tokens"))


def _call(value, *, chat=False):
    require(type(value) is dict)
    if chat:
        require(value.get("type") == "function" and type(value.get("function")) is dict)
        function = value["function"]
        return ToolCall(value.get("id"), function.get("name"), function.get("arguments"))
    require(value.get("status", "completed") == "completed", "MODEL_INCOMPLETE")
    return ToolCall(value.get("call_id"), value.get("name"), value.get("arguments"))


class _HttpAdapter:
    def __init__(self, transport: HttpTransport):
        self.transport = transport

    def generate(self, request, selected, *, environment, cancellation):
        if cancellation.is_set():
            raise ModelError("MODEL_CANCELLED")
        body = self.encode(request, environment[selected.model.model_env])
        provider = selected.provider
        parsed = urlsplit(provider.base_url)
        require(not parsed.query and not parsed.fragment, "MODEL_ENDPOINT")
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + self.path, "", ""))
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth = provider.auth
        if auth.type == "bearer":
            headers["Authorization"] = "Bearer " + environment[auth.token_env]
        elif auth.type == "header":
            headers[auth.header_name] = environment[auth.value_env]
        else:
            require(auth.type == "none", "MODEL_NOT_CONFIGURED")
        try:
            response = self.transport.request(HttpRequest("POST", url, headers, json_text(body).encode("utf-8")),
                                              boundary=provider.boundary, timeout_seconds=request.timeout_seconds,
                                              cancellation=cancellation)
        except AgentError as error:
            codes = {"EFFECT_UNKNOWN": ("MODEL_EFFECT_UNKNOWN", False, "unknown"),
                     "HTTP_CANCELLED": ("MODEL_CANCELLED", False, "unknown"),
                     "HTTP_TIMEOUT": ("MODEL_TIMEOUT", False, "unknown"),
                     "NETWORK_UNAVAILABLE": ("MODEL_UNAVAILABLE", True, "none")}
            code, retry, effect = codes.get(error.code, ("MODEL_TRANSPORT", False, "none"))
            raise ModelError(code, retryable=retry, effect_state=effect) from None
        if cancellation.is_set():
            raise ModelError("MODEL_CANCELLED", effect_state="unknown")
        if response.status in {401, 403}:
            raise ModelError("MODEL_AUTH")
        if response.status == 429:
            # Retrying is a scheduler decision, never a hidden POST loop. The
            # session counts each attempt and honors a bounded Retry-After value.
            error = ModelError("MODEL_RATE_LIMIT", retryable=True)
            raw_delay = response.headers.get("retry-after", "1")
            error.retry_after = int(raw_delay) if raw_delay.isascii() and raw_delay.isdigit() else None
            raise error
        if response.status in {408, 500, 502, 503, 504}:
            raise ModelError("MODEL_EFFECT_UNKNOWN", effect_state="unknown")
        require(response.status == 200, "MODEL_HTTP")
        try:
            value = json_value(response.body.decode("utf-8"))
            require(type(value) is dict and value.get("error") is None)
            result = self.decode(value)
            request_id = response.headers.get("x-request-id")
            if request_id is not None:
                result = replace(result, provider_request_id=identifier(request_id))
            return validate_response(result, request)
        except ModelError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
            raise ModelError("MODEL_PROTOCOL") from None


class ChatCompletionsAdapter(_HttpAdapter):
    version = "chat-completions-v1"
    path = "/chat/completions"

    def encode(self, request, model):
        messages = []
        for message in request.messages:
            require(message.provider_items_json is None, "MODEL_SESSION_PROTOCOL")
            item = {"role": message.role, "content": message.content}
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                item["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                    "name": c.name, "arguments": c.arguments_json}} for c in message.tool_calls]
            messages.append(item)
        body = {"model": model, "messages": messages, "stream": False,
                "max_completion_tokens": request.max_output_tokens}
        if request.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name,
                              "description": t.description, "parameters": json_value(t.parameters_json),
                              "strict": False}} for t in request.tools]
        return body

    def decode(self, value):
        choices = value.get("choices")
        require(type(choices) is list and len(choices) == 1 and type(choices[0]) is dict)
        choice = choices[0]
        reason = choice.get("finish_reason")
        require(reason in {"stop", "tool_calls"}, "MODEL_INCOMPLETE")
        message = choice.get("message")
        require(type(message) is dict and message.get("role") == "assistant")
        require(not message.get("refusal"), "MODEL_REFUSAL")
        calls = message.get("tool_calls", [])
        require(type(calls) is list)
        text = message.get("content")
        require(type(text) is str or text is None and bool(calls))
        return ModelResponse(text or "", tuple(_call(c, chat=True) for c in calls), reason,
                             _usage(value.get("usage"), "prompt_tokens", "completion_tokens"))


class ResponsesAdapter(_HttpAdapter):
    version = "responses-v1"
    path = "/responses"

    def encode(self, request, model):
        items = []
        for message in request.messages:
            if message.provider_items_json is not None:
                items.extend(json_value(message.provider_items_json))
            elif message.role == "tool":
                items.append({"type": "function_call_output", "call_id": message.tool_call_id,
                              "output": message.content})
            else:
                if message.content:
                    items.append({"role": message.role, "content": message.content})
                items.extend({"type": "function_call", "call_id": c.id, "name": c.name,
                              "arguments": c.arguments_json} for c in message.tool_calls)
        body = {"model": model, "input": items, "stream": False, "store": False,
                "include": ["reasoning.encrypted_content"], "max_output_tokens": request.max_output_tokens}
        if request.tools:
            body["tools"] = [{"type": "function", "name": t.name, "description": t.description,
                              "parameters": json_value(t.parameters_json), "strict": False} for t in request.tools]
        return body

    def decode(self, value):
        require(value.get("status") == "completed" and value.get("incomplete_details") is None,
                "MODEL_INCOMPLETE")
        output = value.get("output")
        require(type(output) is list and bool(output))
        texts, calls, replay = [], [], []
        for item in output:
            require(type(item) is dict)
            kind = item.get("type")
            if kind == "message":
                require(item.get("role") == "assistant" and item.get("status") == "completed",
                        "MODEL_INCOMPLETE")
                content = item.get("content")
                require(type(content) is list)
                for part in content:
                    require(type(part) is dict)
                    require(part.get("type") != "refusal", "MODEL_REFUSAL")
                    require(part.get("type") == "output_text" and type(part.get("text")) is str)
                    texts.append(part["text"])
                replay.append({"type": "message", "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": p["text"], "annotations": []}
                                           for p in content]})
            elif kind == "function_call":
                call = _call(item)
                calls.append(call)
                replay.append({"type": "function_call", "call_id": call.id, "name": call.name,
                               "arguments": call.arguments_json})
            elif kind == "reasoning":
                require(type(item.get("id")) is str and type(item.get("summary")) is list)
                require(item.get("status", "completed") == "completed", "MODEL_INCOMPLETE")
                encrypted = item.get("encrypted_content")
                require(encrypted is None or type(encrypted) is str)
                replay.append({key: item[key] for key in ("type", "id", "summary", "encrypted_content")
                               if key in item})
            else:
                raise ModelError("MODEL_PROTOCOL")
        require(bool(texts or calls))
        return ModelResponse("".join(texts), tuple(calls), "tool_calls" if calls else "stop",
                             _usage(value.get("usage"), "input_tokens", "output_tokens"),
                             provider_items_json=json_text(replay))
