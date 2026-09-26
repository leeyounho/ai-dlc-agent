"""Policy-enforcing HTTPS transport shared by GitHub and model adapters.

The transport ignores environment proxy variables, never follows redirects, pins
each request to a DNS result inside an explicitly configured address range, and
does not retry writes.  Git, JVM, and child-process traffic remains outside this
application boundary and requires OS egress enforcement.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from .config.network import authorize_host, normalize_host
from .config.types import NetworkConfig, TransportConfig
from .errors import AgentError


_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_READ_METHODS = frozenset({"GET", "HEAD"})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_FORBIDDEN_REQUEST_HEADERS = frozenset({"host", "connection", "content-length", "transfer-encoding"})


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes = b""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class ResolvedDestination:
    host: str
    port: int
    address: str


@dataclass(frozen=True)
class PreparedRequest:
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


class BackendFailure(Exception):
    """Sanitized backend signal; it intentionally carries no exception payload."""

    def __init__(self, kind: str, *, request_sent: bool):
        super().__init__(kind)
        self.kind = kind
        self.request_sent = request_sent


class SocketHttpBackend:
    """Small HTTP/1.1-over-TLS backend connected to an already-approved IP."""

    def exchange(self, destination: ResolvedDestination, request: PreparedRequest, *,
                 tls_context: ssl.SSLContext, connect_timeout: int, read_timeout: int,
                 max_response_bytes: int, deadline=None, cancellation=None) -> HttpResponse:
        raw_socket = None
        tls_socket = None
        request_sent = False
        watch_stop = threading.Event()
        watcher = None

        def interrupted():
            if cancellation is not None and cancellation.is_set():
                return "cancelled"
            if deadline is not None and time.monotonic() >= deadline:
                return "deadline"
            return None

        def watch_socket():
            while not watch_stop.wait(0.05):
                if interrupted():
                    try:
                        tls_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return

        try:
            if interrupted():
                raise BackendFailure(interrupted(), request_sent=False)
            raw_socket = socket.create_connection((destination.address, destination.port),
                                                  timeout=connect_timeout)
            tls_socket = tls_context.wrap_socket(raw_socket, server_hostname=destination.host,
                                                 do_handshake_on_connect=False)
            raw_socket = None  # ownership moved to the TLS socket
            if deadline is not None or cancellation is not None:
                watcher = threading.Thread(target=watch_socket, daemon=True, name="http-cancellation")
                watcher.start()
            tls_socket.do_handshake()
            tls_socket.settimeout(read_timeout)
            head = [f"{request.method} {request.target} HTTP/1.1\r\n"]
            head.extend(f"{name}: {value}\r\n" for name, value in request.headers)
            payload = "".join(head).encode("ascii") + b"\r\n" + request.body
            # sendall may partially write before raising, so mark the effect uncertain first.
            request_sent = True
            tls_socket.sendall(payload)
            response = http.client.HTTPResponse(tls_socket)
            response.begin()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except ValueError:
                    raise BackendFailure("protocol", request_sent=True) from None
                if declared < 0 or declared > max_response_bytes:
                    raise BackendFailure("response_limit", request_sent=True)
            body = response.read(max_response_bytes + 1)
            if interrupted():
                raise BackendFailure(interrupted(), request_sent=request_sent)
            if len(body) > max_response_bytes:
                raise BackendFailure("response_limit", request_sent=True)
            headers = MappingProxyType({name.lower(): value for name, value in response.getheaders()})
            return HttpResponse(response.status, headers, body)
        except BackendFailure:
            raise
        except (ssl.SSLCertVerificationError, ssl.CertificateError):
            raise BackendFailure("tls", request_sent=request_sent) from None
        except ssl.SSLError:
            raise BackendFailure(interrupted() or ("response" if request_sent else "tls"),
                                 request_sent=request_sent) from None
        except (TimeoutError, socket.timeout):
            raise BackendFailure(interrupted() or "timeout", request_sent=request_sent) from None
        except http.client.HTTPException:
            raise BackendFailure(interrupted() or "protocol", request_sent=request_sent) from None
        except OSError:
            raise BackendFailure(interrupted() or ("response" if request_sent else "network"),
                                 request_sent=request_sent) from None
        finally:
            watch_stop.set()
            if watcher is not None:
                watcher.join()
            if tls_socket is not None:
                try:
                    tls_socket.close()
                except OSError:
                    pass
            if raw_socket is not None:
                try:
                    raw_socket.close()
                except OSError:
                    pass


class HttpTransport:
    def __init__(self, network: NetworkConfig, config: TransportConfig, *, backend=None,
                 resolver: Callable[[str, int], tuple[str, ...]] | None = None,
                 sleeper: Callable[[float], None] = time.sleep):
        self.network = network
        self.config = config
        self.backend = backend or SocketHttpBackend()
        self.resolver = resolver or _system_resolver
        self.sleeper = sleeper

    def request(self, request: HttpRequest, *, boundary: str | None = None,
                timeout_seconds: int | None = None, cancellation=None) -> HttpResponse:
        if timeout_seconds is not None and (type(timeout_seconds) is not int or timeout_seconds <= 0):
            raise AgentError("HTTP_REQUEST", "HTTP deadline must be a positive duration.")
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None

        def check_deadline():
            if cancellation is not None and cancellation.is_set():
                raise AgentError("HTTP_CANCELLED", "HTTP operation was cancelled locally.")
            if deadline is not None and time.monotonic() >= deadline:
                raise AgentError("HTTP_TIMEOUT", "HTTP operation exceeded its deadline.")

        check_deadline()
        if self.config.proxy_mode != "none" or self.config.dns_mode != "system":
            raise AgentError("TRANSPORT_NOT_CONFIGURED", "Transport route mode is unsupported.")
        prepared, host, port = self._prepare(request, boundary)
        route = self.config.routes.get((host, port))
        if route is None or route.host != host or route.port != port:
            raise AgentError("NETWORK_DENIED", "No route is configured for the requested destination.")
        if not route.address_ranges:
            raise AgentError("TRANSPORT_NOT_CONFIGURED", "Destination address policy is not configured.")
        tls_context = self._tls_context()
        is_read = prepared.method in _READ_METHODS
        attempts = self.config.read_retry_attempts if is_read else 1
        last_failure = None
        for attempt in range(attempts):
            try:
                check_deadline()
                destination = self._resolve(host, port, route.address_ranges)
                check_deadline()
                options = {}
                if deadline is not None or cancellation is not None:
                    options = {"deadline": deadline, "cancellation": cancellation}
                remaining = deadline - time.monotonic() if deadline is not None else float("inf")
                response = self.backend.exchange(
                    destination, prepared, tls_context=tls_context,
                    connect_timeout=min(self.config.connect_timeout_seconds, max(0.001, remaining)),
                    read_timeout=min(self.config.read_timeout_seconds, max(0.001, remaining)),
                    max_response_bytes=self.config.max_response_bytes, **options,
                )
                check_deadline()
                if len(response.body) > self.config.max_response_bytes:
                    raise BackendFailure("response_limit", request_sent=True)
                if response.status in _REDIRECTS:
                    raise AgentError("NETWORK_REDIRECT", "Redirect responses are not followed.")
                if is_read and response.status in _TRANSIENT_STATUSES and attempt + 1 < attempts:
                    self.sleeper(min(2 ** attempt, 4))
                    continue
                return HttpResponse(response.status, MappingProxyType(dict(response.headers)), response.body)
            except AgentError:
                raise
            except BackendFailure as failure:
                last_failure = failure
                if is_read and failure.kind in {"network", "timeout", "response"} and attempt + 1 < attempts:
                    self.sleeper(min(2 ** attempt, 4))
                    continue
                break
        assert last_failure is not None
        if last_failure.kind in {"cancelled", "deadline"}:
            code = "HTTP_CANCELLED" if last_failure.kind == "cancelled" else "HTTP_TIMEOUT"
            raise AgentError(code, "HTTP operation stopped locally; remote completion is not confirmed.")
        if not is_read and last_failure.request_sent:
            raise AgentError("EFFECT_UNKNOWN",
                             "A write response was not confirmed; inspect the remote effect before retrying.")
        codes = {
            "tls": ("TLS_ERROR", "TLS verification failed."),
            "response_limit": ("RESPONSE_LIMIT", "HTTP response exceeded the configured size limit."),
            "protocol": ("HTTP_PROTOCOL", "Remote HTTP response was invalid."),
            "timeout": ("TRANSIENT_READ" if is_read else "NETWORK_UNAVAILABLE",
                        "HTTP operation timed out."),
        }
        code, message = codes.get(last_failure.kind,
                                  ("TRANSIENT_READ" if is_read else "NETWORK_UNAVAILABLE",
                                   "HTTP operation could not be completed."))
        raise AgentError(code, message)

    def _prepare(self, request: HttpRequest, boundary: str | None):
        if request.method not in _METHODS:
            raise AgentError("HTTP_REQUEST", "HTTP method is not permitted.")
        if type(request.url) is not str or not request.url or "\\" in request.url:
            raise AgentError("NETWORK_URL", "Invalid HTTPS request URL.")
        try:
            parsed = urlsplit(request.url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.fragment or "%" in parsed.netloc
                    or parsed.netloc.endswith(":")):
                raise ValueError
            port = parsed.port or 443
            host = normalize_host(parsed.hostname)
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            decoded = unquote(target)
            if (not target.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
                    or port < 1 or port > 65535):
                raise ValueError
        except (ValueError, UnicodeError):
            raise AgentError("NETWORK_URL", "Invalid HTTPS request URL.") from None
        authorize_host(self.network, host, boundary)
        if type(request.headers) is not dict and not isinstance(request.headers, Mapping):
            raise AgentError("HTTP_REQUEST", "HTTP headers must be a mapping.")
        headers = []
        seen = set()
        for name, value in request.headers.items():
            if (type(name) is not str or not _HEADER_NAME.fullmatch(name) or name.lower() in seen
                    or name.lower() in _FORBIDDEN_REQUEST_HEADERS or type(value) is not str
                    or not value or not value.isascii()
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise AgentError("HTTP_REQUEST", "HTTP headers are invalid.")
            seen.add(name.lower())
            headers.append((name, value))
        if type(request.body) is not bytes:
            raise AgentError("HTTP_REQUEST", "HTTP request body must be bytes.")
        host_header = f"[{host}]" if ":" in host else host
        if port != 443:
            host_header += f":{port}"
        headers.extend((("Host", host_header), ("Connection", "close"),
                        ("Content-Length", str(len(request.body)))))
        return PreparedRequest(request.method, target, tuple(headers), request.body), host, port

    def _tls_context(self) -> ssl.SSLContext:
        try:
            if self.config.tls.mode == "custom_ca":
                if self.config.tls.ca_bundle_file is None or not self.config.tls.ca_bundle_file.is_file():
                    raise AgentError("TRANSPORT_NOT_CONFIGURED", "TLS CA bundle is unavailable.")
                context = ssl.create_default_context(cafile=str(self.config.tls.ca_bundle_file))
            elif self.config.tls.mode == "system":
                context = ssl.create_default_context()
            else:
                raise AgentError("TRANSPORT_NOT_CONFIGURED", "TLS trust mode is unsupported.")
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            return context
        except AgentError:
            raise
        except (OSError, ssl.SSLError):
            raise AgentError("TRANSPORT_NOT_CONFIGURED", "TLS trust configuration could not be loaded.") from None

    def _resolve(self, host, port, ranges) -> ResolvedDestination:
        try:
            addresses = self.resolver(host, port)
        except Exception:
            raise BackendFailure("network", request_sent=False) from None
        if type(addresses) not in (tuple, list) or not addresses:
            raise BackendFailure("network", request_sent=False)
        parsed = []
        try:
            for value in addresses:
                address = ipaddress.ip_address(value)
                if not any(address.version == network.version and address in network for network in ranges):
                    raise AgentError("NETWORK_DENIED", "DNS result is outside the configured route.")
                parsed.append(address)
        except ValueError:
            raise AgentError("NETWORK_DENIED", "DNS returned an invalid destination address.") from None
        # Preserve resolver order but connect only once; endpoint fallback is never inferred.
        return ResolvedDestination(host, port, str(parsed[0]))


def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    values = []
    for _family, _kind, _protocol, _canonical, sockaddr in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM):
        if sockaddr[0] not in values:
            values.append(sockaddr[0])
    return tuple(values)
