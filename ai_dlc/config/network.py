"""Pure URL policy checks. These functions never perform DNS or socket I/O.

They are not an OS egress firewall. Transport and OS enforcement are later layers.
"""

import ipaddress
import re
from urllib.parse import unquote, urlsplit

from ..errors import AgentError
from ..validation import string
from .types import NetworkConfig


def normalize_host(value: str) -> str:
    value = string(value, "network.host")
    if not value.isascii() or value.endswith("."):
        raise AgentError("CONFIG_HOST", "Hosts must be canonical ASCII names or IP addresses.")
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        pass
    if len(value) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", x)
                                   for x in value.split(".")):
        raise AgentError("CONFIG_HOST", "Invalid destination hostname.")
    return value.lower()


def url_host(url: str) -> str:
    url = string(url, "endpoint")
    if "\\" in url or "?" in url or "#" in url or any(c.isspace() for c in url):
        raise AgentError("NETWORK_URL", "Endpoint contains a forbidden URL component.")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or "%" in parsed.netloc or parsed.netloc.endswith(":")):
            raise ValueError
        decoded_path = unquote(parsed.path)
        if any(ord(c) < 32 or ord(c) == 127 for c in decoded_path):
            raise ValueError
        return normalize_host(parsed.hostname)
    except (ValueError, UnicodeError):
        raise AgentError("NETWORK_URL", "Only credential-free HTTPS endpoints on port 443 are supported.") from None


def authorize_url(network: NetworkConfig, url: str, boundary: str | None = None) -> None:
    host = url_host(url)
    authorize_host(network, host, boundary)


def authorize_host(network: NetworkConfig, host: str, boundary: str | None = None) -> None:
    host = normalize_host(host)
    internal = host in network.internal_hosts
    external = (network.profile == "test" and network.external_access
                and host in network.external_hosts)
    if boundary == "internal":
        permitted = internal
    elif boundary == "external":
        permitted = external
    elif boundary is None:
        permitted = internal or external
    else:
        permitted = False
    if not permitted:
        raise AgentError("NETWORK_DENIED", "Destination is outside the configured connection boundary.")
