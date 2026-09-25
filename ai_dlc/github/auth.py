"""GitHub App RS256 JWT creation and repository-scoped installation tokens."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

from ..config.types import GitHubServiceConfig
from ..errors import AgentError
from ..validation import integer
from .http import GitHubHttp


_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_MAX_KEY_BYTES = 64 * 1024


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _read_tlv(data: bytes, offset: int, expected: int):
    if offset >= len(data) or data[offset] != expected:
        raise ValueError
    offset += 1
    if offset >= len(data):
        raise ValueError
    length = data[offset]
    offset += 1
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or offset + count > len(data):
            raise ValueError
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise ValueError
    return data[offset:end], end


def _integer(data: bytes, offset: int):
    encoded, end = _read_tlv(data, offset, 0x02)
    if not encoded or encoded[0] & 0x80:
        raise ValueError
    if len(encoded) > 1 and encoded[0] == 0 and not encoded[1] & 0x80:
        raise ValueError
    return int.from_bytes(encoded, "big"), end


def _rsa_components(der: bytes):
    outer, end = _read_tlv(der, 0, 0x30)
    if end != len(der):
        raise ValueError
    version, position = _integer(outer, 0)
    if version not in {0, 1}:
        raise ValueError
    # PKCS#8 wraps the PKCS#1 key after an AlgorithmIdentifier sequence.
    if position < len(outer) and outer[position] == 0x30:
        _algorithm, position = _read_tlv(outer, position, 0x30)
        inner, position = _read_tlv(outer, position, 0x04)
        if position != len(outer):
            raise ValueError
        outer, end = _read_tlv(inner, 0, 0x30)
        if end != len(inner):
            raise ValueError
        version, position = _integer(outer, 0)
        if version not in {0, 1}:
            raise ValueError
    modulus, position = _integer(outer, position)
    public_exponent, position = _integer(outer, position)
    private_exponent, _position = _integer(outer, position)
    if modulus.bit_length() < 2048 or public_exponent < 3 or public_exponent % 2 == 0 or private_exponent < 2:
        raise ValueError
    return modulus, public_exponent, private_exponent


class RsaSha256Signer:
    def __init__(self, modulus: int, public_exponent: int, private_exponent: int):
        self.modulus = modulus
        self.public_exponent = public_exponent
        self._private_exponent = private_exponent

    @classmethod
    def from_file(cls, path: Path):
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError
            with path.open("rb") as stream:
                pem = stream.read(_MAX_KEY_BYTES + 1)
            if len(pem) > _MAX_KEY_BYTES:
                raise ValueError
            match = re.fullmatch(
                rb"-----BEGIN (?P<label>RSA PRIVATE KEY|PRIVATE KEY)-----\s+"
                rb"(?P<body>[A-Za-z0-9+/=\r\n]+)\s+"
                rb"-----END (?P=label)-----\s*", pem)
            if not match:
                raise ValueError
            der = base64.b64decode(re.sub(rb"\s+", b"", match.group("body")), validate=True)
            return cls(*_rsa_components(der))
        except (OSError, ValueError, TypeError):
            raise AgentError("GITHUB_KEY", "GitHub App private key is unavailable or unsupported.") from None

    def sign(self, message: bytes) -> bytes:
        digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
        size = (self.modulus.bit_length() + 7) // 8
        padding = size - len(digest_info) - 3
        if padding < 8:
            raise AgentError("GITHUB_KEY", "GitHub App private key cannot create an RS256 signature.")
        encoded = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
        signature = pow(int.from_bytes(encoded, "big"), self._private_exponent, self.modulus)
        if pow(signature, self.public_exponent, self.modulus) != int.from_bytes(encoded, "big"):
            raise AgentError("GITHUB_KEY", "GitHub App private key cannot create an RS256 signature.")
        return signature.to_bytes(size, "big")


@dataclass(frozen=True, repr=False)
class InstallationToken:
    token: str = field(repr=False)
    expires_at: datetime
    installation_id: int
    repository_id: int


class GitHubAppAuthenticator:
    def __init__(self, github: GitHubServiceConfig, http: GitHubHttp, *, app_id: int,
                 signer: RsaSha256Signer, clock=time.time):
        self.github = github
        self.http = http
        self.app_id = integer(app_id, "github.app_id")
        self.signer = signer
        self.clock = clock
        self._cache = {}

    @classmethod
    def from_environment(cls, github: GitHubServiceConfig, http: GitHubHttp, *, environment,
                         clock=time.time):
        value = environment.get(github.app_id_env)
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            raise AgentError("GITHUB_AUTH", "GitHub App ID is unavailable or invalid.")
        return cls(github, http, app_id=int(value), signer=RsaSha256Signer.from_file(github.private_key_file),
                   clock=clock)

    def app_jwt(self) -> str:
        now = int(self.clock())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, sort_keys=True,
                                    separators=(",", ":")).encode("ascii"))
        payload = _b64url(json.dumps({"exp": now + 540, "iat": now - 60, "iss": self.app_id},
                                     sort_keys=True, separators=(",", ":")).encode("ascii"))
        signing_input = (header + "." + payload).encode("ascii")
        return header + "." + payload + "." + _b64url(self.signer.sign(signing_input))

    def installation_token(self, installation_id: int, repository_id: int) -> InstallationToken:
        integer(installation_id, "installation_id")
        integer(repository_id, "repository_id")
        key = (installation_id, repository_id)
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        cached = self._cache.get(key)
        if cached is not None and (cached.expires_at - now).total_seconds() > 60:
            return cached
        document = self.http.request_json(
            "POST", f"/app/installations/{installation_id}/access_tokens",
            authorization="Bearer " + self.app_jwt(), body={"repository_ids": [repository_id]},
        )
        try:
            token = document["token"]
            expires_text = document["expires_at"]
            if (type(token) is not str or not token or not token.isascii() or len(token) > 4096
                    or any(ord(char) < 32 or ord(char) == 127 for char in token)
                    or type(expires_text) is not str):
                raise ValueError
            expires = datetime.fromisoformat(expires_text.replace("Z", "+00:00"))
            if expires.tzinfo is None or expires <= now:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AgentError("GITHUB_PROTOCOL", "GitHub returned an invalid installation token response.") from None
        result = InstallationToken(token, expires.astimezone(timezone.utc), installation_id, repository_id)
        self._cache[key] = result
        return result
