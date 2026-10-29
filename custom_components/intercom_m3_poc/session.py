"""M3 in-memory admission seam. No HA adapter, listener, device or audio I/O.

The HA adapter must derive user/token/control identity and the cloud ContextVar
from HA itself, recheck permissions and revoke this session on control loss.
This module alone does not prove HA authentication or a working media path.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import urlsplit


HEADER = struct.Struct("<BBH")
ATTACH = struct.Struct("<16sQ16s32s")
AUDIO = struct.Struct("<IQIH1024s")
LENGTHS = {1: 72, 2: 26, 3: 1042, 4: 8, 5: 16, 6: 32, 7: 2, 8: 2}


class Refused(ValueError):
    """Safe machine reason only; never include credentials or payloads."""


def canonical_origin(value: str) -> str:
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.path or url.query or url.fragment
                or any(c.isspace() for c in value) or "\\" in value):
            raise ValueError
        host = url.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = url.port
        return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")
    except (ValueError, TypeError, AttributeError):
        raise Refused("SECURE_INGRESS_REQUIRED") from None


def admit_request(request, *, internal_origin: str | None, nabu_origin: str | None,
                  cloud_context: bool) -> tuple[str, str]:
    """cloud_context must come from Nabu's ContextVar, never an HTTP header.

    Proxy admission is intentionally unavailable until the actual proxy and
    forwarded middleware are verified. Direct TLS and Nabu use the same session.
    """
    origin = canonical_origin(request.headers.get("Origin", ""))
    if origin != canonical_origin("https://" + request.host):
        raise Refused("SECURE_INGRESS_REQUIRED")
    if cloud_context:
        if nabu_origin and origin == canonical_origin(nabu_origin):
            return "nabu_remote", origin
        raise Refused("SECURE_INGRESS_REQUIRED")
    transport = request.transport
    if (internal_origin and origin == canonical_origin(internal_origin)
            and request.scheme == "https" and request.secure and transport is not None
            and transport.get_extra_info("ssl_object") is not None):
        return "direct_tls", origin
    raise Refused("SECURE_INGRESS_REQUIRED")


def encode_frame(kind: int, payload: bytes) -> bytes:
    if kind not in LENGTHS or len(payload) != LENGTHS[kind]:
        raise Refused("INVALID_FRAME")
    return HEADER.pack(kind, 1, len(payload)) + payload


def decode_frame(data: bytes) -> tuple[int, bytes]:
    if not isinstance(data, bytes) or len(data) < HEADER.size:
        raise Refused("INVALID_FRAME")
    kind, version, length = HEADER.unpack_from(data)
    if version != 1 or kind not in LENGTHS or length != LENGTHS[kind] or len(data) != length + 4:
        raise Refused("INVALID_FRAME")
    return kind, data[4:]


def decode_audio(payload: bytes, expected_sequence: int) -> tuple[int, int, bytes]:
    if len(payload) != AUDIO.size or not 0 <= expected_sequence < 0xFFFFFFFF:
        raise Refused("INVALID_MEDIA")
    sequence, sent_us, prior_age_us, samples, pcm = AUDIO.unpack(payload)
    if sequence != expected_sequence or samples != 512:
        raise Refused("INVALID_MEDIA")
    return sent_us, prior_age_us, pcm


@dataclass(frozen=True)
class Principal:
    user_id: str
    refresh_token_id: str
    control_connection_id: str
    ingress_class: str
    origin: str


@dataclass
class Owner:
    principal: Principal
    call_id: bytes
    browser_id: bytes
    generation: int
    ticket_digest: bytes | None = None
    ticket_deadline: float = 0
    attached: bool = False


class Session:
    """One simulated room, one owner, one-use ticket; no network awaits here.

    The HA event loop serializes these short synchronous transitions. This scope
    deliberately covers admission only, not endpoint leases or CONNECTED state.
    """

    def __init__(self, clock=time.monotonic):
        self.owner: Owner | None = None
        self.generation = 0
        self.clock = clock

    def start(self, principal: Principal, browser_id: bytes) -> Owner:
        if self.owner is not None:
            raise Refused("BUSY")
        if len(browser_id) != 16 or self.generation == 0xFFFFFFFFFFFFFFFF:
            raise Refused("INVALID_IDENTITY")
        self.generation += 1
        self.owner = Owner(principal, secrets.token_bytes(16), browser_id, self.generation)
        return self.owner

    def require_owner(self, principal: Principal, call_id: bytes, generation: int) -> Owner:
        owner = self.owner
        if (owner is None or owner.principal != principal or owner.call_id != call_id
                or owner.generation != generation):
            raise Refused("NOT_OWNER")
        return owner

    def issue_ticket(self, principal: Principal, call_id: bytes, browser_id: bytes) -> str:
        owner = self.owner
        if owner is None:
            raise Refused("NOT_OWNER")
        owner = self.require_owner(principal, call_id, owner.generation)
        if owner.browser_id != browser_id or owner.attached:
            raise Refused("INVALID_ATTACHMENT")
        ticket = secrets.token_bytes(32)
        owner.ticket_digest = hashlib.sha256(ticket).digest()
        owner.ticket_deadline = self.clock() + 30
        return base64.urlsafe_b64encode(ticket).decode("ascii").rstrip("=")

    def attach(self, frame: bytes, ingress: tuple[str, str]) -> Owner:
        kind, payload = decode_frame(frame)
        if kind != 1:
            raise Refused("INVALID_ATTACHMENT")
        call_id, generation, browser_id, ticket = ATTACH.unpack(payload)
        owner = self.owner
        if (owner is None or owner.attached or owner.ticket_digest is None
                or owner.call_id != call_id or owner.generation != generation
                or owner.browser_id != browser_id or self.clock() >= owner.ticket_deadline
                or ingress != (owner.principal.ingress_class, owner.principal.origin)
                or not hmac.compare_digest(owner.ticket_digest, hashlib.sha256(ticket).digest())):
            raise Refused("INVALID_ATTACHMENT")
        owner.ticket_digest = None
        owner.ticket_deadline = 0
        owner.attached = True
        return owner

    def stop(self, principal: Principal, call_id: bytes, generation: int) -> None:
        if self.owner is not None:
            self.require_owner(principal, call_id, generation)
            self.revoke()

    def revoke(self) -> None:
        if self.owner is not None:
            self.owner.ticket_digest = None
            self.owner.attached = False
        self.owner = None
