"""Composition root for the persistent service's GitHub boundaries."""

from dataclasses import dataclass

from ..errors import AgentError
from ..execution import InstalledRunner, load_runtime_profiles
from ..github import (GitHubApiClient, GitHubAppAuthenticator, GitHubEventProcessor,
                      GitHubWebhookEndpoint, GitHubWebhookReceiver, RepositoryBinding,
                      WebhookInbox)
from ..github.http import GitHubHttp
from ..transport import HttpTransport


@dataclass(frozen=True)
class GitHubComponents:
    endpoint: GitHubWebhookEndpoint | None
    processor: GitHubEventProcessor | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RunnerComponents:
    runner: InstalledRunner | None
    installation_digest: str | None
    reasons: tuple[str, ...]


def build_runner_components(bundle) -> RunnerComponents:
    """Load only installer-owned profiles; unavailable isolation has no shell fallback."""
    execution = bundle.service.execution
    try:
        profiles = load_runtime_profiles(execution.runner_pool_profile_file,
                                         execution.toolchains_profile_file,
                                         execution.egress_profile_file)
        runner = InstalledRunner(profiles)
        digest = runner.check_installation()
        return RunnerComponents(runner, digest, ())
    except AgentError as error:
        return RunnerComponents(None, None, tuple(sorted({"RUNNER_ADAPTER_UNAVAILABLE", error.code})))


def build_github_components(bundle, store, *, environment) -> GitHubComponents:
    """Build independently available intake and processing capabilities.

    A missing App key must not prevent a correctly configured webhook from being
    durably accepted.  Conversely, neither capability is advertised as ready
    when its credential cannot be loaded.
    """
    reasons = set()
    inbox = WebhookInbox(store)
    endpoint = None
    try:
        endpoint = GitHubWebhookEndpoint(
            GitHubWebhookReceiver.from_environment(inbox, bundle.service.github,
                                                   environment=environment))
    except AgentError:
        reasons.add("GITHUB_WEBHOOK_INTAKE_UNAVAILABLE")

    processor = None
    try:
        transport = HttpTransport(bundle.connection.network, bundle.service.transport)
        http = GitHubHttp(bundle.service.github.api_base_url, transport,
                          api_version=bundle.service.github.api_version)
        authenticator = GitHubAppAuthenticator.from_environment(
            bundle.service.github, http, environment=environment)

        def gateway_factory(binding):
            return GitHubApiClient(binding, authenticator, http)

        def reconcile_scope(metadata, *, event_id):
            repository_ids = set(metadata.get("repository_ids") or ())
            repository_id = metadata.get("repository_id")
            if type(repository_id) is int:
                repository_ids.add(repository_id)
            configured = sorted(repository_ids & set(bundle.repositories))
            verified = []
            if (type(repository_id) is int and repository_id in bundle.repositories
                    and type(metadata.get("repository_full_name")) is str
                    and type(metadata.get("installation_id")) is int):
                binding = RepositoryBinding(bundle.service.github.instance_id, repository_id,
                                            metadata["repository_full_name"],
                                            metadata["installation_id"])
                gateway_factory(binding).verify_repository_scope()
                verified.append(repository_id)
            return {"event_id": event_id, "configured_repository_ids": configured,
                    "verified_repository_ids": verified,
                    "reobservation_required": sorted(set(configured) - set(verified))}

        processor = GitHubEventProcessor(
            inbox, store, bundle.repositories, gateway_factory=gateway_factory,
            scope_reconciler=reconcile_scope)
    except AgentError:
        reasons.add("GITHUB_PROCESSOR_UNAVAILABLE")
    return GitHubComponents(endpoint, processor, tuple(sorted(reasons)))
