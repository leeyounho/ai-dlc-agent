"""Durable raw webhook inbox committed before an HTTP success response."""

from copy import deepcopy
from dataclasses import dataclass
import base64
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import threading

from .. import validation as v
from ..errors import AgentError
from ..storage.journal import FileJournal, _mkdir, _native, _no_links, _publish


_DELIVERY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}\Z")


@dataclass(frozen=True)
class InboxReceipt:
    delivery_id: str
    delivery_digest: str
    body_digest: str
    metadata: dict
    raw_body: bytes
    duplicate: bool


class WebhookInbox:
    def __init__(self, store: FileJournal):
        self.store = store
        self._lock = threading.RLock()

    def _key(self, delivery_id: str):
        if type(delivery_id) is not str or not _DELIVERY_ID.fullmatch(delivery_id):
            raise AgentError("WEBHOOK_DELIVERY", "Webhook delivery identifier is invalid.")
        return hashlib.sha256(delivery_id.encode("ascii")).hexdigest()

    def _path(self, delivery_id):
        return self.store.root / "inbox" / (self._key(delivery_id) + ".json")

    def _processed_path(self, delivery_id):
        return self.store.root / "inbox-processed" / (self._key(delivery_id) + ".json")

    def _decode(self, record, *, duplicate):
        try:
            required = {"schema_version", "delivery_id", "delivery_digest", "body_digest",
                        "received_at", "metadata", "raw_body_base64"}
            if type(record) is not dict or set(record) != required or record["schema_version"] != 1:
                raise ValueError
            delivery_id = record["delivery_id"]
            self._key(delivery_id)
            raw = base64.b64decode(record["raw_body_base64"], validate=True)
            body_digest = hashlib.sha256(raw).hexdigest()
            unsigned = {key: value for key, value in record.items() if key != "delivery_digest"}
            if (body_digest != record["body_digest"]
                    or v.canonical_digest(unsigned) != record["delivery_digest"]
                    or type(record["metadata"]) is not dict):
                raise ValueError
            v.string(record["received_at"], "received_at")
            return InboxReceipt(delivery_id, record["delivery_digest"], body_digest,
                                deepcopy(record["metadata"]), raw, duplicate)
        except (AgentError, KeyError, TypeError, ValueError):
            raise AgentError("INBOX_CORRUPT", "Webhook inbox record is invalid or damaged.") from None

    def accept(self, delivery_id: str, raw_body: bytes, metadata: dict) -> InboxReceipt:
        self._key(delivery_id)
        if type(raw_body) is not bytes or type(metadata) is not dict:
            raise AgentError("WEBHOOK_VALUE", "Webhook delivery data is invalid.")
        record = {"schema_version": 1, "delivery_id": delivery_id,
                  "body_digest": hashlib.sha256(raw_body).hexdigest(),
                  "received_at": datetime.now(timezone.utc).isoformat(),
                  "metadata": deepcopy(metadata),
                  "raw_body_base64": base64.b64encode(raw_body).decode("ascii")}
        record["delivery_digest"] = v.canonical_digest(record)
        path = self._path(delivery_id)
        with self._lock:
            self.store.assert_healthy()
            try:
                _mkdir(path.parent)
                if _native(path).exists():
                    existing = self._decode(self.store._read(path), duplicate=True)
                    if (existing.body_digest != record["body_digest"]
                            or existing.metadata != metadata):
                        raise AgentError("WEBHOOK_COLLISION",
                                         "A delivery identifier was reused with different content.")
                    return existing
                _publish(path, record)
                return self._decode(record, duplicate=False)
            except AgentError:
                raise
            except OSError:
                self.store._unhealthy = True
                raise AgentError("INBOX_IO", "Webhook delivery could not be committed safely.") from None

    def pending(self) -> tuple[InboxReceipt, ...]:
        directory = self.store.root / "inbox"
        _no_links(directory)
        with self._lock:
            self.store.assert_healthy()
            if not _native(directory).exists():
                return ()
            try:
                receipts = []
                for path in sorted(_native(directory).iterdir()):
                    if path.name.startswith(".pending-"):
                        continue
                    if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                        raise AgentError("INBOX_CORRUPT", "Webhook inbox contains an invalid entry.")
                    receipt = self._decode(self.store._read(path), duplicate=False)
                    if not _native(self._processed_path(receipt.delivery_id)).exists():
                        receipts.append(receipt)
                return tuple(receipts)
            except AgentError:
                raise
            except OSError:
                raise AgentError("INBOX_CORRUPT", "Webhook inbox could not be enumerated.") from None

    def mark_processed(self, receipt: InboxReceipt, result: dict) -> bool:
        if type(receipt) is not InboxReceipt or type(result) is not dict:
            raise AgentError("INBOX_VALUE", "Webhook processing result is invalid.")
        path = self._processed_path(receipt.delivery_id)
        record = {"schema_version": 1, "delivery_id": receipt.delivery_id,
                  "delivery_digest": receipt.delivery_digest, "result": deepcopy(result)}
        record["digest"] = v.canonical_digest(record)
        with self._lock:
            self.store.assert_healthy()
            try:
                _mkdir(path.parent)
                if _native(path).exists():
                    existing = self.store._read(path)
                    if existing != record:
                        raise AgentError("WEBHOOK_COLLISION",
                                         "A delivery processing result changed after commit.")
                    return False
                _publish(path, record)
                return True
            except AgentError:
                raise
            except OSError:
                self.store._unhealthy = True
                raise AgentError("INBOX_IO", "Webhook processing result could not be committed safely.") from None
