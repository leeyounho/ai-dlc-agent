from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import threading
import time
import unittest

from ai_dlc.config.loader import parse_connection, parse_repository
from ai_dlc.config.types import NetworkConfig, TlsConfig
from ai_dlc.errors import AgentError
from ai_dlc.models import (AdapterRegistry, Message, ModelConcurrency, ModelError, ModelRegistry,
                           ModelRequest, ModelResponse, ModelRouter, ModelSessions, ToolCall, ToolDefinition)
from ai_dlc.models.types import json_text, validate_response
from ai_dlc.storage.journal import FileJournal, TaskKey
from ai_dlc.service.bootstrap import build_model_components
from ai_dlc.config.loader import load_service
from ai_dlc.evaluation.offline import OfflineNetworkGuard
from ai_dlc.transport import BackendFailure, HttpResponse, HttpTransport
from ai_dlc.validation import read_json
from tests.support import temporary_directory
from tests.test_service_and_transport import FakeBackend, transport_config


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evaluation" / "suites" / "fixtures"
TOOL = ToolDefinition("read_file", "Read an allowed file", json_text({
    "type": "object", "properties": {"path": {"type": "string", "minLength": 1}},
    "required": ["path"], "additionalProperties": False}))
CALL = ToolCall("call_1", "read_file", '{"path":"README.md"}')
CHECKPOINT = {"requirements": "approved req-1", "design": "approved des-1", "diff": "", "verification_summary": "not run"}


def chat(*, calls=False, usage=True):
    message = {"role": "assistant", "content": None if calls else "Done"}
    if calls:
        message["tool_calls"] = [{"id": CALL.id, "type": "function", "function": {
            "name": CALL.name, "arguments": CALL.arguments_json}}]
    result = {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}
    if usage:
        result["usage"] = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    return result


def responses(*, calls=False):
    output = [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque-reasoning"}]
    if calls:
        output.append({"type": "function_call", "call_id": CALL.id, "name": CALL.name,
                       "arguments": CALL.arguments_json, "status": "completed"})
    else:
        output.append({"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "Done", "annotations": []}]})
    return {"status": "completed", "output": output}


def http(value, status=200, headers=None):
    return HttpResponse(status, headers or {}, json_text(value).encode())


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.raw = read_json(FIXTURES / "connection.json")
        self.raw["network"]["internal_hosts"].append("localhost")
        self.raw["llm"]["providers"]["internal"]["base_url"] = "https://localhost/v1"
        self.env = {"EVAL_MODEL_TOKEN": "secret-not-in-errors", "EVAL_ALPHA": "gemma4-configured",
                    "EVAL_BETA": "gpt-oss-configured", "EVAL_TEXT": "text-configured"}
        self.network = NetworkConfig("production", False, frozenset({"localhost"}), frozenset())
        self.key = TaskKey("local-evaluation", 1, 1)

    def setup_runtime(self, store, *outcomes, raw=None, custom=None, clock=lambda: 1, wall_clock=lambda: 100):
        config = parse_connection(raw or self.raw, base_dir=FIXTURES)
        self.router = ModelRouter(ModelRegistry(config))
        self.repository = parse_repository(read_json(FIXTURES / "repository.json"), connection=config)
        self.backend = FakeBackend(*outcomes)
        self.transport = HttpTransport(self.network, transport_config(max_response_bytes=100000),
                                       backend=self.backend, resolver=lambda *_: ("127.0.0.1",))
        self.adapters = AdapterRegistry(self.transport, custom=custom)
        self.concurrency = ModelConcurrency(2, {p.id: p.max_concurrent_requests for p in config.providers.values()})
        self.sessions = ModelSessions(store, self.router, self.adapters, self.concurrency, clock=clock, wall_clock=wall_clock)
        return self.sessions

    def initialize(self, store, *, model_calls=10, tool_calls=10, active_seconds=100):
        if store.read(self.key) is None:
            store.commit(self.key, expected_revision=0, event_id="task-created", event={"kind": "test"},
                         reduce=lambda _: {"workflow_marker": "preserved"})
        self.sessions.create_run(self.key, "run1", model_calls=model_calls, tool_calls=tool_calls,
                                 active_seconds=active_seconds)
        self.open_session()

    def open_session(self, session_id="session1", **overrides):
        options = dict(purpose="implementation", repository=self.repository, environment=self.env,
                       tools=(TOOL,), prompt="Follow the approved scope.", checkpoint=CHECKPOINT)
        options.update(overrides)
        return self.sessions.open_session(self.key, "run1", session_id, **options)

    def generate(self, request_id="request1", session_id="session1", **overrides):
        options = dict(repository=self.repository, environment=self.env, tools=(TOOL,),
                       max_output_tokens=128, timeout_seconds=5)
        options.update(overrides)
        return self.sessions.generate(self.key, "run1", session_id, request_id, **options)

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("secret-not-in-errors", str(raised.exception.as_dict()))
        return raised.exception

    def test_chat_wire_round_trip_and_exactly_once_tool_claim(self):
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, http(chat(calls=True)), http(chat(usage=False)))
            self.initialize(store)
            result = self.generate()
            self.assertEqual(result.tool_calls, (CALL,))
            request = self.backend.calls[0][1]
            self.assertEqual(request.target, "/v1/chat/completions")
            payload = json.loads(request.body)
            self.assertEqual(payload["model"], "gemma4-configured")
            self.assertEqual(payload["max_completion_tokens"], 128)
            self.assertEqual(payload["tools"][0]["function"]["name"], TOOL.name)
            self.assertEqual(self.generate(), result)
            self.assertEqual(len(self.backend.calls), 1)
            self.assert_code("MODEL_REQUEST", lambda: self.generate("request2"))
            self.assert_code("MODEL_TOOLS_PENDING", lambda: self.open_session("session2"))
            self.assertEqual(self.sessions.claim_tool(self.key, "run1", CALL.id), CALL)
            self.assert_code("MODEL_TOOL_ALREADY_CLAIMED", lambda: self.sessions.claim_tool(self.key, "run1", CALL.id))
            self.sessions.finish_tool(self.key, "run1", CALL.id, result="actual file", evidence_ref="blob:abc")
            self.sessions.finish_tool(self.key, "run1", CALL.id, result="actual file", evidence_ref="blob:abc")
            self.assert_code("MODEL_TOOL_COLLISION", lambda: self.sessions.finish_tool(
                self.key, "run1", CALL.id, result="invented", evidence_ref="blob:abc"))
            self.assertIsNone(self.generate("request2").usage)
            history = json.loads(self.backend.calls[1][1].body)["messages"]
            self.assertEqual(history[-1], {"role": "tool", "content": "actual file", "tool_call_id": CALL.id})
            self.assertEqual(store.read(self.key)["workflow_marker"], "preserved")
            self.assertEqual(self.sessions.inspect(self.key, "run1")["tool_calls"], 1)

    def test_responses_wire_replays_reasoning_and_tool_output_in_same_session(self):
        raw = deepcopy(self.raw)
        raw["llm"]["providers"]["internal"]["adapter"] = "openai_responses"
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, http(responses(calls=True)), http(responses()), raw=raw)
            self.initialize(store)
            self.generate()
            self.sessions.claim_tool(self.key, "run1", CALL.id)
            self.sessions.finish_tool(self.key, "run1", CALL.id, result="actual file", evidence_ref="blob:result")
            self.generate("request2")
            request = self.backend.calls[1][1]
            self.assertEqual(request.target, "/v1/responses")
            payload = json.loads(request.body)
            self.assertFalse(payload["store"])
            self.assertIn({"type": "function_call_output", "call_id": CALL.id, "output": "actual file"}, payload["input"])
            self.assertIn("opaque-reasoning", json_text(payload))
            opened = self.open_session("session2", purpose="design")
            self.assertNotIn("opaque-reasoning", json_text(opened))
            self.assertNotIn("call_1", json_text(opened))
            self.assertEqual(opened["definition"]["binding"]["model_id"], "beta")

    def test_malformed_partial_refusal_and_duplicate_responses_fail_closed(self):
        cases = []
        for mutate, code in (
            (lambda v: v["choices"][0].update(finish_reason="length"), "MODEL_INCOMPLETE"),
            (lambda v: v["choices"][0]["message"].update(refusal="No"), "MODEL_REFUSAL"),
            (lambda v: v["choices"][0]["message"].update(content=123), "MODEL_PROTOCOL"),
            (lambda v: v.update(usage={"prompt_tokens": True}), "MODEL_PROTOCOL"),
            (lambda v: v.update(usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 0}), "MODEL_PROTOCOL"),
            (lambda v: v.update(usage={"completion_tokens": 9999}), "MODEL_OUTPUT_LIMIT"),
            (lambda v: v["choices"][0]["message"]["tool_calls"][0]["function"].update(arguments='{"path":true}'), "MODEL_TOOL_ARGUMENTS"),
            (lambda v: v["choices"][0]["message"]["tool_calls"][0]["function"].update(arguments='{"path":"x","shell":"danger"}'), "MODEL_TOOL_ARGUMENTS"),
            (lambda v: v["choices"][0]["message"]["tool_calls"][0]["function"].update(arguments='{"path":"x","path":"y"}'), "MODEL_PROTOCOL"),
            (lambda v: v["choices"][0]["message"]["tool_calls"][0]["function"].update(name="approve_requirements"), "MODEL_TOOL_ARGUMENTS"),
            (lambda v: v["choices"][0]["message"]["tool_calls"].append(deepcopy(v["choices"][0]["message"]["tool_calls"][0])), "MODEL_PROTOCOL"),
        ):
            value = chat(calls=True)
            mutate(value)
            cases.append((http(value), code))
        cases += [(HttpResponse(200, {}, b'{"choices": [], "choices": []}'), "MODEL_PROTOCOL"),
                  (HttpResponse(200, {}, b'broken secret-not-in-errors'), "MODEL_PROTOCOL")]
        for outcome, code in cases:
            with self.subTest(code=code), temporary_directory() as temp, FileJournal(temp / "state") as store:
                self.setup_runtime(store, outcome)
                self.initialize(store)
                self.assert_code(code, self.generate)
                self.assertEqual(self.sessions.inspect(self.key, "run1")["tools"], {})
                self.assert_code(code, self.generate)
                self.assertEqual(len(self.backend.calls), 1)

    def test_responses_rejects_incomplete_and_unregistered_builtin_tools(self):
        raw = deepcopy(self.raw)
        raw["llm"]["providers"]["internal"]["adapter"] = "openai_responses"
        for value, code in (({"status": "incomplete", "output": []}, "MODEL_INCOMPLETE"),
                            ({"status": "completed", "output": [{"type": "web_search_call"}]}, "MODEL_PROTOCOL"),
                            ({"status": "completed", "output": []}, "MODEL_PROTOCOL")):
            with temporary_directory() as temp, FileJournal(temp / "state") as store:
                self.setup_runtime(store, http(value), raw=raw)
                self.initialize(store)
                self.assert_code(code, self.generate)

    def test_http_auth_and_response_loss_never_retry_or_fallback(self):
        for outcome, code in ((http({"secret": "secret-not-in-errors"}, 401), "MODEL_AUTH"),
                              (http({}, 500), "MODEL_EFFECT_UNKNOWN"),
                              (BackendFailure("timeout", request_sent=True), "MODEL_EFFECT_UNKNOWN"),
                              (http({}, 302, {"location": "https://outside.invalid"}), "MODEL_TRANSPORT")):
            with temporary_directory() as temp, FileJournal(temp / "state") as store:
                self.setup_runtime(store, outcome, http(chat()))
                self.initialize(store)
                self.assert_code(code, self.generate)
                self.assert_code(code, self.generate)
                self.assertEqual(len(self.backend.calls), 1)

    def test_rate_limit_retry_has_same_request_endpoint_and_persistent_budget(self):
        now = [100]
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, http({}, 429, {"retry-after": "2"}), http(chat()), wall_clock=lambda: now[0])
            self.initialize(store, model_calls=2)
            self.assert_code("MODEL_RATE_LIMIT", self.generate)
            self.assert_code("MODEL_RETRY_WAIT", self.generate)
            self.assert_code("MODEL_REQUEST_PENDING", lambda: self.generate("new-request"))
            now[0] += 2
            self.generate()
            self.assertEqual(self.backend.calls[0][1], self.backend.calls[1][1])
            self.open_session("session2", purpose="design")
            self.assert_code("MODEL_BUDGET", lambda: self.generate("next", "session2"))
            self.assertEqual(self.sessions.inspect(self.key, "run1")["model_calls"], 2)

    def test_budget_redefinition_output_and_context_bounds_are_denied_before_network(self):
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store)
            self.initialize(store, active_seconds=4)
            self.assert_code("MODEL_BUDGET", self.generate)
            self.assert_code("MODEL_BUDGET_CHANGED", lambda: self.sessions.create_run(
                self.key, "run1", model_calls=999, tool_calls=10, active_seconds=999))
            self.assert_code("MODEL_OUTPUT_LIMIT", lambda: self.generate(max_output_tokens=9999))
            self.open_session("session2", prompt="x" * 9000)
            self.assert_code("MODEL_CONTEXT_LIMIT", lambda: self.generate("next", "session2"))
            self.assertEqual(self.backend.calls, [])

    def test_session_pins_model_name_config_tool_schema_and_repository(self):
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store)
            self.initialize(store)
            changed_env = {**self.env, "EVAL_ALPHA": "silently-changed"}
            self.assert_code("MODEL_SESSION_CHANGED", lambda: self.generate(environment=changed_env))
            self.assert_code("MODEL_SESSION_CHANGED", lambda: self.generate(tools=()))
            self.assert_code("MODEL_SESSION_CHANGED", lambda: self.open_session(prompt="changed"))
            self.assert_code("MODEL_TASK_SCOPE", lambda: self.generate(repository=replace(self.repository, repository_id=2)))
            self.assert_code("MODEL_DENIED", lambda: self.generate(repository=replace(self.repository, enabled=False)))
            self.assertEqual(self.backend.calls, [])

    def test_model_capability_and_missing_custom_adapter_are_blocked(self):
        for capability in ("unknown", "unsupported"):
            raw = deepcopy(self.raw)
            raw["llm"]["models"]["alpha"]["capabilities"]["tool_calls"] = capability
            with temporary_directory() as temp, FileJournal(temp / "state") as store:
                self.setup_runtime(store, raw=raw)
                self.assert_code("MODEL_NOT_CONFIGURED" if capability == "unknown" else "MODEL_DENIED",
                                 lambda: self.initialize(store))
                self.assertEqual(self.backend.calls, [])
        raw = deepcopy(self.raw)
        raw["llm"]["providers"]["internal"].update(adapter="custom", adapter_id="installed")
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, raw=raw)
            self.assert_code("MODEL_NOT_CONFIGURED", lambda: self.initialize(store))

    def test_trusted_custom_adapter_registered_explicitly_and_exception_is_sanitized(self):
        class Custom:
            version = "fixture-v1"

            def generate(self, *args, **kwargs):
                raise ValueError("secret-not-in-errors")

        raw = deepcopy(self.raw)
        raw["llm"]["providers"]["internal"].update(adapter="custom", adapter_id="installed")
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, raw=raw, custom={"installed": Custom()})
            self.initialize(store)
            self.assert_code("MODEL_ADAPTER_ERROR", self.generate)

    def test_cancellation_before_and_during_request_never_releases_tool_calls(self):
        cancellation = threading.Event()
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, http(chat(calls=True)))
            self.initialize(store)
            cancellation.set()
            self.assert_code("MODEL_CANCELLED", lambda: self.generate(cancellation=cancellation))
            self.assertEqual(self.backend.calls, [])
            cancellation.clear()
            exchange = self.backend.exchange

            def cancelling(*args, **kwargs):
                cancellation.set()
                return exchange(*args, **kwargs)

            self.backend.exchange = cancelling
            self.assert_code("MODEL_CANCELLED", lambda: self.generate(cancellation=cancellation))
            self.assertEqual(self.sessions.inspect(self.key, "run1")["tools"], {})

    def test_late_response_is_discarded_and_elapsed_budget_recorded(self):
        times = iter([0, 6, 6])
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, http(chat(calls=True)), clock=lambda: next(times))
            self.initialize(store)
            self.assert_code("MODEL_TIMEOUT", self.generate)
            run = self.sessions.inspect(self.key, "run1")
            self.assertEqual(run["active_seconds"], 6)
            self.assertEqual(run["tools"], {})

    def test_restart_retains_completed_response_and_claimed_tool_without_reexecution(self):
        with temporary_directory() as temp:
            with FileJournal(temp / "state") as store:
                self.setup_runtime(store, http(chat(calls=True)))
                self.initialize(store)
                result = self.generate()
                self.sessions.claim_tool(self.key, "run1", CALL.id)
            with FileJournal(temp / "state") as store:
                self.setup_runtime(store)
                self.assertEqual(self.generate(), result)
                self.assert_code("MODEL_TOOL_ALREADY_CLAIMED", lambda: self.sessions.claim_tool(self.key, "run1", CALL.id))
                self.assert_code("MODEL_TOOLS_PENDING", lambda: self.open_session("session2"))
                self.assertEqual(self.backend.calls, [])
                self.assertEqual(self.sessions.inspect(self.key, "run1")["tool_calls"], 1)

    def test_process_loss_after_reservation_requires_explicit_reconciliation(self):
        with temporary_directory() as temp:
            with FileJournal(temp / "state") as store:
                self.setup_runtime(store)
                self.initialize(store)
                self.backend.exchange = lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt())
                with self.assertRaises(KeyboardInterrupt):
                    self.generate()
            with FileJournal(temp / "state") as store:
                self.setup_runtime(store)
                self.assert_code("MODEL_EFFECT_UNKNOWN", self.generate)
                run = self.sessions.inspect(self.key, "run1")
                self.assertEqual(run["model_calls"], 1)
                self.assertEqual(run["active_seconds"], 5)
                self.assert_code("MODEL_REQUEST_PENDING", lambda: self.open_session("session2"))
                self.sessions.abandon_request(self.key, "run1", "request1", evidence_ref="operator:discard-response")
                self.open_session("session2")
                self.assertEqual(self.sessions.inspect(self.key, "run1")["model_calls"], 1)
                self.assertEqual(self.backend.calls, [])

    def test_live_failure_cannot_overwrite_an_explicit_abandon_checkpoint(self):
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store)
            self.initialize(store)

            def operator_abandons(*args, **kwargs):
                self.sessions.abandon_request(self.key, "run1", "request1", evidence_ref="operator:stopped")
                raise BackendFailure("timeout", request_sent=False)

            self.backend.exchange = operator_abandons
            self.assert_code("MODEL_REQUEST_COLLISION", self.generate)
            run = self.sessions.inspect(self.key, "run1")
            self.assertEqual(run["requests"]["request1"]["status"], "abandoned")
            self.assertEqual(run["active_seconds"], 5)
            self.assertEqual(run["tools"], {})

    def test_request_id_collision_and_repeated_rate_limit_do_not_reset_attempts(self):
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, http({}, 429, {"retry-after": "0"}), http({}, 429, {"retry-after": "0"}))
            self.initialize(store)
            self.assert_code("MODEL_RATE_LIMIT", self.generate)
            self.assert_code("MODEL_REQUEST_COLLISION", lambda: self.generate(max_output_tokens=127))
            self.assert_code("MODEL_RATE_LIMIT", self.generate)
            self.assert_code("MODEL_RATE_LIMIT", self.generate)
            self.assertEqual(len(self.backend.calls), 2)
            run = self.sessions.inspect(self.key, "run1")
            self.assertEqual(run["model_calls"], 2)
            self.assertEqual(run["requests"]["request1"]["status"], "failed")

    def test_provider_and_global_concurrency_is_shared_and_released(self):
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, http(chat()))
            self.initialize(store)
            provider = self.router.resolve("implementation", self.repository).provider
            with self.concurrency.slot(provider), self.concurrency.slot(provider):
                self.assert_code("MODEL_BUSY", self.generate)
                self.assertEqual(self.sessions.inspect(self.key, "run1")["model_calls"], 0)
            self.generate()
            different = replace(provider, id="second")
            global_limiter = ModelConcurrency(1, {provider.id: 2, different.id: 2})
            with global_limiter.slot(different):
                self.assert_code("MODEL_BUSY", lambda: global_limiter.slot(provider).__enter__())

    def test_tool_budget_and_multiple_tool_serialization(self):
        value = chat(calls=True)
        extra = deepcopy(value["choices"][0]["message"]["tool_calls"][0])
        extra["id"] = "call_2"
        value["choices"][0]["message"]["tool_calls"].append(extra)
        with temporary_directory() as temp, FileJournal(temp / "state") as store:
            self.setup_runtime(store, http(value))
            self.initialize(store, tool_calls=1)
            self.generate()
            self.sessions.claim_tool(self.key, "run1", CALL.id)
            self.sessions.finish_tool(self.key, "run1", CALL.id, result="actual", evidence_ref="blob:result")
            self.assert_code("MODEL_BUDGET", lambda: self.sessions.claim_tool(self.key, "run1", "call_2"))
            self.assert_code("MODEL_TOOLS_PENDING", lambda: self.open_session("session2"))
            self.assertEqual(self.sessions.inspect(self.key, "run1")["tool_calls"], 1)

    def test_service_composes_adapters_offline_without_claiming_workflow_readiness(self):
        with temporary_directory() as temp, FileJournal(temp / "state") as store, OfflineNetworkGuard() as guard:
            components = build_model_components(load_service(ROOT / "config/service.example.json"), store)
            self.assertIn("openai_responses", components.adapter_keys)
            self.assertEqual(guard.attempts, 0)
            self.assertIn("MODEL_WORKFLOW_UNCONNECTED", components.reasons)

    def test_socket_cancellation_and_deadline_interrupt_incomplete_response_body(self):
        for mode in ("cancel", "timeout"):
            release, received, cancellation = threading.Event(), threading.Event(), threading.Event()

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    self.rfile.read(int(self.headers["Content-Length"]))
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    self.wfile.flush()
                    received.set()
                    release.wait(5)

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(ROOT / "tests/fixtures/localhost-cert.pem", ROOT / "tests/fixtures/localhost-key.pem")
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            canceller = None
            try:
                with self.subTest(mode=mode), temporary_directory() as temp, FileJournal(temp / "state") as store:
                    self.setup_runtime(store)
                    transport = HttpTransport(self.network, transport_config(max_response_bytes=100000,
                        port=server.server_port, tls=TlsConfig("custom_ca", ROOT / "tests/fixtures/localhost-cert.pem")),
                        resolver=lambda *_: ("127.0.0.1",))
                    selected = self.router.resolve("implementation", self.repository)
                    selected = replace(selected, provider=replace(selected.provider,
                                       base_url=f"https://localhost:{server.server_port}/v1"))
                    if mode == "cancel":
                        def cancel_after_headers():
                            if received.wait(3):
                                cancellation.set()
                        canceller = threading.Thread(target=cancel_after_headers, daemon=True)
                        canceller.start()
                    request = ModelRequest("request", "session", (Message("user", "probe"),), (), 128, 1)
                    started = time.monotonic()
                    error = self.assert_code("MODEL_CANCELLED" if mode == "cancel" else "MODEL_TIMEOUT",
                        lambda: AdapterRegistry(transport).get("openai_chat_completions").generate(
                            request, selected, environment=self.env, cancellation=cancellation))
                    self.assertLess(time.monotonic() - started, 2.5)
                    self.assertEqual(error.effect_state, "unknown")
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if canceller:
                    canceller.join(timeout=5)

    def test_model_http_wire_uses_real_local_tls_socket(self):
        captured = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = json_text(chat()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(ROOT / "tests/fixtures/localhost-cert.pem", ROOT / "tests/fixtures/localhost-key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with temporary_directory() as temp, FileJournal(temp / "state") as store:
                self.setup_runtime(store)
                transport = HttpTransport(self.network, transport_config(
                    max_response_bytes=100000, port=server.server_port,
                    tls=TlsConfig("custom_ca", ROOT / "tests/fixtures/localhost-cert.pem")),
                    resolver=lambda *_: ("127.0.0.1",))
                # Production configuration remains port-443-only. The adapter
                # wire test uses an explicitly configured ephemeral loopback port.
                selected = self.router.resolve("implementation", self.repository)
                selected = replace(selected, provider=replace(selected.provider,
                                   base_url=f"https://localhost:{server.server_port}/v1"))
                request = ModelRequest("request", "session", (Message("user", "probe"),), (), 128, 5)
                adapter = AdapterRegistry(transport).get("openai_chat_completions")
                self.assertEqual(adapter.generate(request, selected, environment=self.env,
                                                  cancellation=threading.Event()).text, "Done")
                self.assertEqual(captured[0][0], "/v1/chat/completions")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ContractTests(unittest.TestCase):
    def test_schema_rejects_unsupported_keywords_and_nonfinite_arguments(self):
        for schema in ({"type": "object", "properties": {}, "required": [], "additionalProperties": True},
                       {"type": "object", "properties": {}, "required": [], "additionalProperties": False, "$ref": "file:///x"}):
            with self.assertRaises(ModelError):
                ToolDefinition("test", "", json_text(schema))
        for text in ('{"x":NaN}', '{"x":1e999}', '[]'):
            with self.assertRaises(ModelError):
                ToolCall("call", "tool", text)

    def test_nested_tool_schema_enforces_types_bounds_enum_and_additional_keys(self):
        tool = ToolDefinition("tool", "", json_text({"type": "object", "properties": {
            "values": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 3}, "maxItems": 2},
            "mode": {"type": "string", "enum": ["read"]}}, "required": ["values", "mode"], "additionalProperties": False}))
        request = ModelRequest("request", "session", (Message("user", "go"),), (tool,), 10, 1)
        for args in ({"values": [True], "mode": "read"}, {"values": [0], "mode": "read"},
                     {"values": [1, 2, 3], "mode": "read"}, {"values": [1], "mode": "write"}):
            with self.assertRaises(ModelError):
                validate_response(ModelResponse("", (ToolCall("call", "tool", json_text(args)),), "tool_calls"), request)


if __name__ == "__main__":
    unittest.main()
