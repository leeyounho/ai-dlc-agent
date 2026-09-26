"""Journal-backed model calls and tool claims; no workflow approval or tool execution."""

from dataclasses import asdict
import math
import threading
import time
import uuid

from ..errors import AgentError
from ..validation import canonical_digest
from .types import (Message, ModelError, ModelRequest, ModelResponse, ToolCall,
                    identifier, json_text, positive, require, validate_response)


def _message(value):
    return Message(value["role"], value["content"], tuple(ToolCall(**c) for c in value["tool_calls"]),
                   value["tool_call_id"], value["provider_items_json"])


class ModelSessions:
    """One shared concurrency limiter per service; state lives beside the task.

    The caller supplies trusted context, policies, secrets and tool definitions.
    It must enforce workflow approvals at each actual tool dispatch (#9). Model
    sessions never infer approval from context or execute a tool themselves.
    """

    def __init__(self, store, router, adapters, concurrency, *, clock=time.monotonic, wall_clock=time.time):
        self.store, self.router = store, router
        self.adapters, self.concurrency = adapters, concurrency
        self.clock, self.wall_clock = clock, wall_clock

    def _change(self, key, operation, change):
        with self.store.locked(key):
            history = self.store.history(key)

            def reduce(state):
                require(state is not None, "MODEL_TASK_MISSING")
                runs = state.setdefault("model_runs", {})
                require(all(type(run.get("schema_version")) is int and run["schema_version"] == 1
                            for run in runs.values()), "MODEL_STATE_VERSION")
                change(runs)
                if "state_revision" in state:
                    state["state_revision"] = len(history) + 1
                return state

            result = self.store.commit(key, expected_revision=len(history), event_id="model-" + uuid.uuid4().hex,
                                       event={"kind": operation}, reduce=reduce)
            self.store.assert_healthy()
            return result.state["model_runs"]

    def inspect(self, key, run_id):
        state = self.store.read(key)
        require(state is not None and run_id in state.get("model_runs", {}), "MODEL_RUN_MISSING")
        run = state["model_runs"][run_id]
        require(type(run.get("schema_version")) is int and run["schema_version"] == 1, "MODEL_STATE_VERSION")
        return run

    def create_run(self, key, run_id, *, model_calls, tool_calls, active_seconds, retry_attempts=2):
        identifier(run_id)
        for value in (model_calls, tool_calls, active_seconds, retry_attempts):
            positive(value)
        require(retry_attempts <= 3, "MODEL_BUDGET")
        limits = {"model_calls": model_calls, "tool_calls": tool_calls, "active_seconds": active_seconds,
                  "retry_attempts": retry_attempts}

        def change(runs):
            if run_id in runs:
                require(runs[run_id]["limits"] == limits, "MODEL_BUDGET_CHANGED")
                return
            runs[run_id] = {"schema_version": 1, "limits": limits, "model_calls": 0, "tool_calls": 0,
                            "active_seconds": 0, "active_session": None, "sessions": {}, "requests": {},
                            "tools": {}}

        return self._change(key, "model_run_created", change)[run_id]

    def _selected(self, purpose, repository, environment):
        preflight = self.router.preflight(purpose, repository, environment=environment,
                                          available_adapters=self.adapters.keys)
        require(preflight.status == "ready_for_registered_adapter",
                "MODEL_DENIED" if preflight.status == "denied" else "MODEL_NOT_CONFIGURED")
        return self.router.resolve(purpose, repository)

    def _binding(self, selected, tools, environment):
        require(not tools or selected.model.tool_calls == "supported", "MODEL_TOOLS_UNAVAILABLE")
        return {**selected.summary(), "model_name_digest": canonical_digest(environment[selected.model.model_env]),
                "tool_schema_digest": canonical_digest([asdict(t) for t in tools]),
                "adapter_version": self.adapters.get(selected.provider.adapter_key).version}

    @staticmethod
    def _idle(run):
        require(not any(r["status"] in {"pending", "retry_wait"} for r in run["requests"].values()),
                "MODEL_REQUEST_PENDING")
        require(not any(t["status"] != "done" for t in run["tools"].values()), "MODEL_TOOLS_PENDING")

    def open_session(self, key, run_id, session_id, *, purpose, repository, environment, tools, prompt,
                     checkpoint):
        identifier(session_id)
        require(repository is not None and repository.repository_id == key.repository_id
                and repository.instance_id == key.instance_id, "MODEL_TASK_SCOPE")
        selected = self._selected(purpose, repository, environment)
        # Only these curated fields can cross a model/provider boundary. Raw
        # transcripts, response IDs, hidden reasoning and tool calls cannot.
        require(type(checkpoint) is dict and set(checkpoint) == {
            "requirements", "design", "diff", "verification_summary"}
            and all(type(value) is str for value in checkpoint.values()), "MODEL_CHECKPOINT")
        messages = (Message("system", prompt), Message("user", json_text(checkpoint)))
        ModelRequest("validate", session_id, messages, tools, 1, 1)
        binding = self._binding(selected, tools, environment)
        definition = {"binding": binding, "purpose": purpose,
                      "prompt_digest": canonical_digest(prompt), "checkpoint_digest": canonical_digest(checkpoint)}

        def change(runs):
            require(run_id in runs, "MODEL_RUN_MISSING")
            run = runs[run_id]
            if session_id in run["sessions"]:
                require(run["active_session"] == session_id
                        and run["sessions"][session_id]["definition"] == definition, "MODEL_SESSION_CHANGED")
                return
            self._idle(run)
            run["sessions"][session_id] = {"definition": definition, "messages": [asdict(m) for m in messages]}
            run["active_session"] = session_id

        return self._change(key, "model_session_opened", change)[run_id]["sessions"][session_id]

    def generate(self, key, run_id, session_id, request_id, *, repository, environment, tools,
                 max_output_tokens, timeout_seconds, cancellation=None):
        cancellation = cancellation or threading.Event()
        identifier(request_id)
        positive(max_output_tokens)
        positive(timeout_seconds)
        require(repository is not None and repository.repository_id == key.repository_id
                and repository.instance_id == key.instance_id, "MODEL_TASK_SCOPE")
        run = self.inspect(key, run_id)
        require(run["active_session"] == session_id, "MODEL_SESSION_CHANGED")
        session = run["sessions"][session_id]
        selected = self._selected(session["definition"]["purpose"], repository, environment)
        require(session["definition"]["binding"] == self._binding(selected, tools, environment),
                "MODEL_SESSION_CHANGED")
        require(not cancellation.is_set(), "MODEL_CANCELLED")
        identity = {"session_id": session_id, "max_output_tokens": max_output_tokens,
                    "timeout_seconds": timeout_seconds}
        existing = run["requests"].get(request_id)
        if existing:
            require(existing["identity"] == identity, "MODEL_REQUEST_COLLISION")
            if existing["status"] == "completed":
                return ModelResponse.from_dict(existing["response"])
            if existing["status"] == "pending":
                raise ModelError("MODEL_EFFECT_UNKNOWN", effect_state="unknown")
            if existing["status"] != "retry_wait":
                raise ModelError(existing["error"]["code"], effect_state=existing["error"]["effect_state"])
            require(self.wall_clock() >= existing["retry_at"], "MODEL_RETRY_WAIT")
        request = ModelRequest(request_id, session_id, tuple(_message(m) for m in session["messages"]),
                               tools, max_output_tokens, timeout_seconds)
        require(max_output_tokens <= selected.model.max_output_tokens, "MODEL_OUTPUT_LIMIT")
        # Conservative byte-based admission, NOT reported token usage. Provider
        # tokenizers differ; a 1024-token envelope also covers wire scaffolding.
        context_bound = len(json_text(asdict(request)).encode("utf-8")) + 1024
        require(context_bound + max_output_tokens <= selected.model.context_window_tokens, "MODEL_CONTEXT_LIMIT")
        with self.concurrency.slot(selected.provider):
            def reserve(runs):
                current = runs[run_id]
                require(current["active_session"] == session_id
                        and current["sessions"][session_id] == session, "MODEL_SESSION_CHANGED")
                pending = current["requests"].get(request_id)
                require(pending == existing, "MODEL_REQUEST_COLLISION")
                require(not any(t["status"] != "done" for t in current["tools"].values()), "MODEL_TOOLS_PENDING")
                require(not any(rid != request_id and r["status"] in {"pending", "retry_wait"}
                                for rid, r in current["requests"].items()), "MODEL_REQUEST_PENDING")
                limits = current["limits"]
                require(current["model_calls"] < limits["model_calls"]
                        and current["active_seconds"] + timeout_seconds <= limits["active_seconds"], "MODEL_BUDGET")
                attempt = (pending["attempt"] if pending else 0) + 1
                require(attempt <= limits["retry_attempts"], "MODEL_RETRY_EXHAUSTED")
                current["model_calls"] += 1
                current["active_seconds"] += timeout_seconds  # retained after an interrupted process
                current["requests"][request_id] = {"identity": identity, "status": "pending", "attempt": attempt,
                                                   "request_digest": canonical_digest(asdict(request))}

            self._change(key, "model_call_reserved", reserve)
            started = self.clock()
            try:
                response = self.adapters.get(selected.provider.adapter_key).generate(
                    request, selected, environment=environment, cancellation=cancellation)
                require(not cancellation.is_set(), "MODEL_CANCELLED")
                require(self.clock() - started <= timeout_seconds, "MODEL_TIMEOUT")
                response = validate_response(response, request)
            except Exception as error:
                if not isinstance(error, ModelError):
                    error = ModelError("MODEL_ADAPTER_ERROR", effect_state="unknown")
                if error.code in {"MODEL_CANCELLED", "MODEL_TIMEOUT"}:
                    error = ModelError(error.code, effect_state="unknown")
                elapsed = max(0, math.ceil(self.clock() - started))

                def failed(runs):
                    current = runs[run_id]
                    record = current["requests"][request_id]
                    require(record["status"] == "pending", "MODEL_REQUEST_COLLISION")
                    current["active_seconds"] += elapsed - timeout_seconds
                    delay = getattr(error, "retry_after", 1)
                    retry = (error.retryable and error.effect_state == "none" and type(delay) is int
                             and 0 <= delay <= 60 and record["attempt"] < current["limits"]["retry_attempts"])
                    record.update(status="retry_wait" if retry else "failed", error=error.as_dict(),
                                  elapsed_seconds=elapsed, retry_at=self.wall_clock() + (delay or 0) if retry else None)

                self._change(key, "model_call_failed", failed)
                raise error from None

            elapsed = max(0, math.ceil(self.clock() - started))

            def completed(runs):
                current = runs[run_id]
                record = current["requests"][request_id]
                require(record["status"] == "pending", "MODEL_REQUEST_COLLISION")
                require(not any(c.id in current["tools"] for c in response.tool_calls), "MODEL_TOOL_COLLISION")
                record.update(status="completed", response=response.as_dict(), elapsed_seconds=elapsed)
                current["active_seconds"] += elapsed - timeout_seconds
                message = Message("assistant", response.text, response.tool_calls,
                                  provider_items_json=response.provider_items_json)
                current["sessions"][session_id]["messages"].append(asdict(message))
                for call in response.tool_calls:
                    current["tools"][call.id] = {"session_id": session_id, "call": asdict(call), "status": "ready"}

            self._change(key, "model_call_completed", completed)
            return response

    def claim_tool(self, key, run_id, call_id):
        """Persist BEFORE actual dispatch; a claimed/done call is never claimed twice."""
        identifier(call_id)

        def change(runs):
            run = runs[run_id]
            require(call_id in run["tools"], "MODEL_TOOL_MISSING")
            tool = run["tools"][call_id]
            require(tool["status"] == "ready", "MODEL_TOOL_ALREADY_CLAIMED")
            require(run["tool_calls"] < run["limits"]["tool_calls"], "MODEL_BUDGET")
            require(not any(t["status"] == "claimed" for t in run["tools"].values()), "MODEL_TOOLS_PENDING")
            tool["status"] = "claimed"
            run["tool_calls"] += 1

        value = self._change(key, "model_tool_claimed", change)[run_id]["tools"][call_id]
        return ToolCall(**value["call"])

    def finish_tool(self, key, run_id, call_id, *, result, evidence_ref):
        """Trusted executor/reconciler supplies actual result and durable evidence."""
        require(type(result) is str and type(evidence_ref) is str and bool(evidence_ref), "MODEL_TOOL_RESULT")

        def change(runs):
            run = runs[run_id]
            require(call_id in run["tools"], "MODEL_TOOL_MISSING")
            tool = run["tools"][call_id]
            digest = canonical_digest({"result": result, "evidence_ref": evidence_ref})
            if tool["status"] == "done":
                require(tool["result_digest"] == digest, "MODEL_TOOL_COLLISION")
                return
            require(tool["status"] == "claimed", "MODEL_TOOL_NOT_CLAIMED")
            tool.update(status="done", result_digest=digest, evidence_ref=evidence_ref)
            run["sessions"][tool["session_id"]]["messages"].append(asdict(Message("tool", result, tool_call_id=call_id)))

        self._change(key, "model_tool_completed", change)

    def abandon_request(self, key, run_id, request_id, *, evidence_ref):
        """Explicit operator checkpoint only; never claims remote inference was cancelled."""
        require(type(evidence_ref) is str and bool(evidence_ref), "MODEL_CHECKPOINT")

        def change(runs):
            record = runs[run_id]["requests"][request_id]
            require(record["status"] in {"pending", "retry_wait"}, "MODEL_REQUEST_STATE")
            record.update(status="abandoned", evidence_ref=evidence_ref,
                          error=ModelError("MODEL_ABANDONED", effect_state="unknown").as_dict())

        self._change(key, "model_request_abandoned", change)
