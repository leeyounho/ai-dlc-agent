"""Framework-neutral HTTP contract for the GitHub App webhook route."""

from collections.abc import Mapping
from dataclasses import dataclass
import json
from types import MappingProxyType

from ..errors import AgentError
from .webhook import GitHubWebhookReceiver


@dataclass(frozen=True)
class WebhookHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class GitHubWebhookEndpoint:
    path = "/hooks/github"

    def __init__(self, receiver: GitHubWebhookReceiver):
        self.receiver = receiver

    @staticmethod
    def _response(status: int, document: dict) -> WebhookHttpResponse:
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return WebhookHttpResponse(
            status,
            MappingProxyType({"Content-Type": "application/json", "Content-Length": str(len(body)),
                              "Cache-Control": "no-store"}),
            body,
        )

    def handle(self, method: str, path: str, headers, raw_body: bytes) -> WebhookHttpResponse:
        if path != self.path:
            return self._response(404, {"error": "NOT_FOUND"})
        if method != "POST":
            return self._response(405, {"error": "METHOD_NOT_ALLOWED"})
        try:
            receipt = self.receiver.receive(headers, raw_body)
        except AgentError as error:
            if error.code in {"INBOX_IO", "STATE_CLOSED", "STATE_UNHEALTHY"}:
                status = 503
            elif error.code == "WEBHOOK_SIZE":
                status = 413
            elif error.code in {"WEBHOOK_SECRET", "WEBHOOK_SIGNATURE"}:
                status = 401
            elif error.code == "WEBHOOK_UNSUPPORTED":
                status = 422
            else:
                status = 400
            return self._response(status, {"error": error.code})
        return self._response(202, {"accepted": True, "delivery_id": receipt.delivery_id,
                                    "duplicate": receipt.duplicate})
