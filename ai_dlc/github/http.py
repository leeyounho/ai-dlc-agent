"""Sanitized JSON calls over the shared policy-enforcing transport."""

import json
import re
from urllib.parse import urlsplit, urlunsplit

from ..errors import AgentError
from ..transport import HttpRequest, HttpTransport
from ..validation import decode_json


class GitHubHttp:
    def __init__(self, api_base_url: str, transport: HttpTransport, *, api_version: str | None = None):
        parsed = urlsplit(api_base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise AgentError("GITHUB_CONFIG", "GitHub API base URL is invalid.")
        self._base = parsed
        self.transport = transport
        self.api_version = api_version

    def request_json(self, method: str, path: str, *, authorization: str, body: dict | None = None,
                     not_found: bool = False) -> dict | None:
        if (type(path) is not str or not path.startswith("/") or not path.isascii()
                or "\\" in path or "?" in path or "#" in path or not re.fullmatch(r"/[A-Za-z0-9_./%~-]+", path)):
            raise AgentError("GITHUB_REQUEST", "GitHub API path is invalid.")
        base_path = self._base.path.rstrip("/")
        url = urlunsplit((self._base.scheme, self._base.netloc, base_path + path, "", ""))
        headers = {"Accept": "application/vnd.github+json", "Authorization": authorization,
                   "User-Agent": "ai-dlc-agent"}
        if self.api_version is not None:
            headers["X-GitHub-Api-Version"] = self.api_version
        payload = b""
        if body is not None:
            try:
                payload = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                raise AgentError("GITHUB_REQUEST", "GitHub request body is invalid.") from None
            headers["Content-Type"] = "application/json"
        response = self.transport.request(HttpRequest(method, url, headers, payload))
        if response.status == 404 and not_found:
            return None
        if response.status in {401, 403}:
            raise AgentError("GITHUB_AUTH", "GitHub rejected the App credential or repository scope.")
        if response.status == 404:
            raise AgentError("GITHUB_NOT_FOUND", "Requested GitHub resource is unavailable.")
        if response.status in {409, 415, 422}:
            raise AgentError("GITHUB_CAPABILITY_UNAVAILABLE",
                             "The configured GitHub API does not support this request contract.")
        if response.status == 429 or response.status >= 500:
            raise AgentError("GITHUB_UNAVAILABLE", "GitHub API is temporarily unavailable.")
        if response.status < 200 or response.status >= 300:
            raise AgentError("GITHUB_PROTOCOL", "GitHub API returned an unexpected status.")
        try:
            return decode_json(response.body)
        except AgentError:
            raise AgentError("GITHUB_PROTOCOL", "GitHub API returned an invalid JSON object.") from None
