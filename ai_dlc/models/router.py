"""Model selection and pre-dispatch contracts shared by evaluation and sessions."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from ..config.network import authorize_url, url_host
from ..config.types import CODING_PURPOSES, PURPOSES, ConnectionConfig, Model, Provider, RepositoryConfig
from ..errors import AgentError
from ..validation import canonical_digest


@dataclass(frozen=True)
class ResolvedModel:
    purpose: str
    model: Model
    provider: Provider
    route_source: str
    config_digest: str

    def summary(self) -> dict:
        return {"purpose": self.purpose, "model_id": self.model.id,
                "provider_id": self.provider.id, "adapter": self.provider.adapter,
                "boundary": self.provider.boundary, "route_source": self.route_source,
                "config_digest": self.config_digest}


@dataclass(frozen=True)
class Preflight:
    status: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons)}


class ModelRegistry:
    def __init__(self, config: ConnectionConfig):
        self.config = config

    def get_model(self, model_id: str) -> Model:
        try:
            return self.config.models[model_id]
        except KeyError:
            raise AgentError("MODEL_REFERENCE", "Model is not registered.") from None

    def get_provider(self, provider_id: str) -> Provider:
        try:
            return self.config.providers[provider_id]
        except KeyError:
            raise AgentError("MODEL_REFERENCE", "Provider is not registered.") from None


class ModelRouter:
    def __init__(self, registry: ModelRegistry):
        self.registry = registry

    def resolve(self, purpose: str, repository: RepositoryConfig | None = None) -> ResolvedModel:
        if type(purpose) is not str or purpose not in PURPOSES:
            raise AgentError("MODEL_PURPOSE", "Unknown model purpose.")
        config = self.registry.config
        if repository and purpose in repository.routing.by_purpose:
            model_id, source = repository.routing.by_purpose[purpose], "repository.purpose"
        elif repository and repository.routing.default_model is not None:
            model_id, source = repository.routing.default_model, "repository.default"
        elif purpose in config.routing.by_purpose:
            model_id, source = config.routing.by_purpose[purpose], "global.purpose"
        else:
            model_id, source = config.routing.default_model, "global.default"
        if repository and model_id not in repository.allowed_models:
            raise AgentError("MODEL_NOT_ALLOWED", "Selected model is not allowed for this repository.")
        model = self.registry.get_model(model_id)
        provider = self.registry.get_provider(model.provider)
        authorize_url(config.network, provider.base_url, provider.boundary)
        digest = canonical_digest({"connection": config.digest, "repository": repository.digest if repository else None})
        return ResolvedModel(purpose, model, provider, source, digest)

    def preflight(self, purpose: str, repository: RepositoryConfig | None = None, *,
                  environment: Mapping[str, str], available_adapters: frozenset[str]) -> Preflight:
        selected = self.resolve(purpose, repository)
        p, m = selected.provider, selected.model
        denied, pending = [], []
        if repository is not None and not repository.enabled:
            denied.append("REPOSITORY_DISABLED")
        if purpose in CODING_PURPOSES:
            if m.tool_calls == "unsupported":
                denied.append("TOOLS_UNSUPPORTED")
            elif m.tool_calls == "unknown":
                pending.append("TOOLS_UNCONFIRMED")
        if p.adapter == "unconfigured":
            pending.append("ADAPTER_UNCONFIGURED")
        elif p.adapter_key not in available_adapters:
            pending.append("ADAPTER_UNAVAILABLE")
        if p.auth.type == "unconfigured":
            pending.append("AUTH_UNCONFIGURED")
        secret_ref = p.auth.token_env or p.auth.value_env
        if secret_ref and not _safe_env_value(environment.get(secret_ref)):
            pending.append("AUTH_VALUE_MISSING_OR_INVALID")
        if not _safe_env_value(environment.get(m.model_env)):
            pending.append("MODEL_NAME_MISSING_OR_INVALID")
        if m.context_window_tokens is None or m.max_output_tokens is None:
            pending.append("MODEL_LIMITS_UNCONFIGURED")
        host = url_host(p.base_url)
        if host == "example" or host.endswith(".example"):
            pending.append("ENDPOINT_PLACEHOLDER")
        reasons = tuple(sorted(denied + pending))
        return Preflight("denied" if denied else "configuration_pending" if pending else "ready_for_registered_adapter", reasons)


def _safe_env_value(value) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 65536
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


T = TypeVar("T")


class DispatchGuard:
    """Call an injected trusted adapter only after resolving and checking policy.

    This is not the task approval engine or OS/network sandbox. Offline evaluation
    supplies a local recording adapter; real generation uses ModelSessions.
    """

    def __init__(self, router: ModelRouter, adapters: Mapping[str, Callable[[ResolvedModel], T]]):
        self.router = router
        self.adapters = dict(adapters)

    def dispatch(self, purpose: str, repository: RepositoryConfig | None = None, *,
                 environment: Mapping[str, str]) -> T:
        preflight = self.router.preflight(purpose, repository, environment=environment,
                                          available_adapters=frozenset(self.adapters))
        if preflight.status != "ready_for_registered_adapter":
            code = "MODEL_DENIED" if preflight.status == "denied" else "MODEL_NOT_CONFIGURED"
            raise AgentError(code, "Model dispatch prerequisites are not satisfied.")
        selected = self.router.resolve(purpose, repository)
        try:
            return self.adapters[selected.provider.adapter_key](selected)
        except AgentError:
            raise
        except Exception:
            # Do not leak credentials/prompts from exceptions raised by an adapter.
            raise AgentError("MODEL_ADAPTER_ERROR", "Registered adapter failed; no fallback was attempted.") from None
