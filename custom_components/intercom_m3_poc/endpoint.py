"""Authenticated ICM3/ICM4 endpoint transport; profiles never downgrade."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import ssl
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID


HEADER = struct.Struct("<BH")
ENVELOPE = struct.Struct("<16s16sQ16sQ")
HELLO = struct.Struct("<4sHH16s16s16sIIB")
HELLO_ACK = struct.Struct("<4sHH16s16s16sI")
START = struct.Struct("<BBIB")
AUDIO = struct.Struct("<IIQIH")
PING = struct.Struct("<QQ")
PONG = struct.Struct("<QQQQ")
STATUS = struct.Struct("<BBHIIIIIQI")
CALL_CONTROL = struct.Struct("<IBQ")
SOURCE_REQUEST = struct.Struct("<Q16s")
LOCAL_WAKE = struct.Struct("<IQQB")
COMMAND_AUDIO = struct.Struct("<IQIQIH")
VOLUME = struct.Struct("<IBBBQB")
RATE = 16_000
SAMPLES = 512
ZERO16 = bytes(16)
MAX_BODY = 2048
MAX_CLOCK_UNCERTAINTY_US = 100_000
CLOCK_MEDIA_EXPIRY_US = 2_000_000
CLOCK_FRESH_S = 3.0
UNKNOWN_PROGRESS_MS = 2**32 - 1


class EndpointEnded(Exception):
    """Fail-closed endpoint session termination."""


@dataclass(frozen=True)
class EndpointConfig:
    host: str
    port: int
    expected_uuid: uuid.UUID
    expected_uri: str
    expected_spki_sha256: str
    ca_path: str
    client_cert_path: str
    client_key_path: str
    endpoint_profile: int = 3

    def __post_init__(self) -> None:
        if type(self.endpoint_profile) is not int or self.endpoint_profile not in (3, 4):
            raise ValueError("unsupported endpoint profile")

    @classmethod
    def from_mapping(cls, data: dict) -> "EndpointConfig":
        try:
            host = str(data["endpoint_host"]).strip()
            port = int(data["endpoint_port"])
            endpoint_uuid = uuid.UUID(str(data["expected_endpoint_uuid"]))
            uri = str(data["expected_endpoint_uri"]).strip()
            pin = str(data["expected_endpoint_spki_sha256"]).lower()
            profile = data.get("endpoint_profile", 3)
            paths = [str(data[key]) for key in (
                "endpoint_ca_path", "endpoint_client_cert_path", "endpoint_client_key_path")]
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError("invalid endpoint configuration") from err
        if not host or not 1 <= port <= 65535 or uri != f"urn:intercom:endpoint:{endpoint_uuid}":
            raise ValueError("endpoint identity or address is invalid")
        if len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
            raise ValueError("endpoint SPKI pin must be 64 lowercase hex characters")
        if any(not Path(path).is_file() for path in paths):
            raise ValueError("endpoint trust file is missing")
        return cls(host, port, endpoint_uuid, uri, pin, *paths, profile)


class ClockRelation:
    def __init__(self) -> None:
        self.offset_us = 0.0
        self.uncertainty_us = float("inf")
        self.updated = 0.0
        self.pending: dict[int, int] = {}
        self.last_peer_us = -1
        self.rejected_samples = 0
        self.rejected_uncertainty_max_ms = 0

    def pong(self, nonce: int, t1: int, t2: int, t3: int, now_us: int, now: float) -> None:
        if self.pending.pop(nonce, None) != t1 or t3 < t2 or now_us < t1 or t2 < self.last_peer_us:
            raise EndpointEnded("CLOCK_INVALID")
        rtt = (now_us - t1) - (t3 - t2)
        if rtt < 0:
            raise EndpointEnded("CLOCK_INVALID")
        resolution = max(1, int(time.get_clock_info("monotonic").resolution * 1_000_000))
        offset = ((t2 - t1) + (t3 - now_us)) / 2
        uncertainty = max(rtt / 2, resolution) + int((rtt + 3_000_000) * .0002) + resolution
        if uncertainty > MAX_CLOCK_UNCERTAINTY_US:
            self.rejected_samples += 1
            self.rejected_uncertainty_max_ms = max(
                self.rejected_uncertainty_max_ms, int(uncertainty) // 1_000)
            if self.fresh(now):
                return
            raise EndpointEnded("CLOCK_UNTRUSTED")
        self.offset_us = offset
        self.uncertainty_us = uncertainty
        self.updated, self.last_peer_us = now, t3

    def fresh(self, now: float) -> bool:
        return self.updated > 0 and now - self.updated <= CLOCK_FRESH_S

    def prior_age(self, generated_local_us: int, now_us: int, now: float) -> int:
        if not self.fresh(now):
            raise EndpointEnded("CLOCK_STALE")
        if generated_local_us - now_us > self.uncertainty_us:
            raise EndpointEnded("CLOCK_INVALID")
        return max(0, now_us - generated_local_us) + int(self.uncertainty_us)

    def local_generated(self, endpoint_us: int, prior_us: int, now_us: int, now: float) -> tuple[int, int]:
        if not self.fresh(now):
            raise EndpointEnded("CLOCK_STALE")
        local_us = int(endpoint_us - self.offset_us)
        if local_us - now_us > self.uncertainty_us:
            raise EndpointEnded("CLOCK_INVALID")
        upper = max(0, now_us - local_us) + prior_us + int(self.uncertainty_us)
        if upper >= CLOCK_MEDIA_EXPIRY_US:
            raise EndpointEnded("AUDIO_EXPIRED")
        return local_us, upper

    def report_transit_us(self, endpoint_us: int, now_us: int, now: float) -> int:
        """Return a conservative age for an endpoint health snapshot."""
        if not self.fresh(now):
            raise EndpointEnded("CLOCK_STALE")
        local_us = int(endpoint_us - self.offset_us)
        if endpoint_us <= 0 or local_us - now_us > self.uncertainty_us:
            raise EndpointEnded("STATUS_TIME_INVALID")
        return max(0, now_us - local_us) + int(self.uncertainty_us)


class EndpointClient:
    """One authenticated stream, idle or bound to exactly one call."""

    def __init__(self, config: EndpointConfig, call_id: bytes, owner_generation: int,
                 mode: str, mode_generation: int = 0,
                 renewal_authorize: Callable[[], Awaitable[bool]] | None = None) -> None:
        if (len(call_id) != 16 or not 0 <= owner_generation < 2**64
                or (owner_generation == 0 and (call_id != ZERO16 or config.endpoint_profile != 4))):
            raise ValueError("invalid owner")
        if mode not in ("full_duplex", "speaker_only"):
            raise ValueError("invalid mode")
        self.config, self.call, self.owner = config, call_id, owner_generation
        self.mode, self.mode_generation = mode, mode_generation
        self.renewal_authorize = renewal_authorize
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connection = self.boot = ZERO16
        self.tx_sequence = self.rx_sequence = self.tx_media_sequence = self.rx_media_sequence = 0
        self.clock = ClockRelation()
        self.control: asyncio.Queue[tuple[int, bytes, bytes, int]] = asyncio.Queue(maxsize=16)
        self.audio: deque[tuple[int, int, bytes]] = deque(maxlen=3)
        self.audio_ready = asyncio.Event()
        self.capture: asyncio.Queue[tuple[int, int, bytes]] = asyncio.Queue(maxsize=3)
        self.status = 0
        self.status_health = 0
        self.status_rx_sequence = self.status_tx_sequence = -1
        self.last_status_report_us = -1
        self.answered = False
        self.started = False
        self.ring_seen = False
        self.room_mic_muted = False
        self.last_tx = self.last_audio_tx = self.last_rx = self.last_sink = 0.0
        self.audio_prior_age_max_ms = self.audio_transit_age_max_ms = 0
        self.clock_uncertainty_ms = self.audio_stale_drops = 0
        self.tx_queue_hold_max_ms = 0
        self.closed = False
        self.ending = False
        self.capabilities = 0
        self.answer_policy = "drop_in"
        self.accept_token = 0
        self.source_handler: Callable[..., Awaitable[None]] | None = None
        self.peer_room_id: str | None = None
        self.source_nonce = 0
        self.source_task: asyncio.Task | None = None
        self.wake_handler: Callable[..., Awaitable[None]] | None = None
        self.wake_task: asyncio.Task | None = None
        self.wake_sequence = -1
        self.wake_received_at = 0.0
        self.last_wake_token = 0
        self.held = False
        self.command_token = self.command_sequence = 0
        self.command_ready = asyncio.Event()
        self.command_capture: asyncio.Queue[tuple[int, int, bytes]] = asyncio.Queue(maxsize=3)
        self.last_command = self.hold_started = 0.0
        self.last_status_received = 0.0
        self.volume_lock = asyncio.Lock()
        self.volume_request: tuple[int, int, int, int, int] | None = None
        self.volume_applied: tuple[int, int, int, int, int] | None = None
        self.volume_waiter: asyncio.Future | None = None
        self.volume_prepared = False
        self.volume_applied_ready = asyncio.Event()

    def _context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.config.ca_path)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.check_hostname = False
        context.load_cert_chain(self.config.client_cert_path, self.config.client_key_path)
        return context

    async def _async_context(self) -> ssl.SSLContext:
        return await asyncio.to_thread(self._context)

    def _validate_peer(self) -> None:
        ssl_object = self.writer.get_extra_info("ssl_object") if self.writer else None
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        if not der:
            raise EndpointEnded("TLS_IDENTITY_MISSING")
        cert = x509.load_der_x509_certificate(der)
        try:
            basic = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value
            usage = cert.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE).value
            eku = cert.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value
            san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        except x509.ExtensionNotFound as err:
            raise EndpointEnded("TLS_IDENTITY_INVALID") from err
        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
        forbidden_usage = (usage.content_commitment or usage.key_encipherment
                           or usage.data_encipherment or usage.key_agreement
                           or usage.key_cert_sign or usage.crl_sign)
        if (basic.ca or basic.path_length is not None or not usage.digital_signature
                or forbidden_usage or list(eku) != [ExtendedKeyUsageOID.SERVER_AUTH]
                or uris != [self.config.expected_uri]):
            raise EndpointEnded("TLS_IDENTITY_INVALID")
        public = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if hashlib.sha256(public).hexdigest() != self.config.expected_spki_sha256:
            raise EndpointEnded("TLS_PIN_MISMATCH")

    async def connect(self) -> None:
        try:
            host = str(ipaddress.ip_address(self.config.host))
        except ValueError:
            host = self.config.host
        context = await self._async_context()
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(host, self.config.port, ssl=context, server_hostname=None), 5)
        self._validate_peer()
        kind, hello = await asyncio.wait_for(self._read_raw(), 2)
        if kind != 10 or len(hello) < HELLO.size:
            raise EndpointEnded("HELLO_INVALID")
        magic, profile, direction, device, boot, connection, caps, audio_profiles, name_len = HELLO.unpack_from(hello)
        try:
            hello[HELLO.size:].decode("utf-8", "strict")
        except UnicodeDecodeError as err:
            raise EndpointEnded("HELLO_INVALID") from err
        expected_profile = self.config.endpoint_profile
        required_caps = 31 if expected_profile == 3 else 63
        if ((magic, profile, direction, device, len(hello))
                != (f"ICM{expected_profile}".encode(), expected_profile, 2,
                    self.config.expected_uuid.bytes, HELLO.size + name_len)
                or boot == ZERO16 or connection == ZERO16 or boot == connection
                or caps & required_caps != required_caps or audio_profiles != 1 or name_len > 64):
            raise EndpointEnded("HELLO_INVALID")
        self.boot, self.connection = boot, connection
        self.capabilities = caps
        await self._write_raw(11, HELLO_ACK.pack(
            magic, expected_profile, 0, device, boot, connection, 1))

    async def _read_raw(self) -> tuple[int, bytes]:
        header = await self.reader.readexactly(HEADER.size)
        kind, size = HEADER.unpack(header)
        if not 0 < size <= MAX_BODY:
            raise EndpointEnded("FRAME_INVALID")
        return kind, await self.reader.readexactly(size)

    async def _write_raw(self, kind: int, body: bytes) -> None:
        self.writer.write(HEADER.pack(kind, len(body)) + body)
        await asyncio.wait_for(self.writer.drain(), .25)

    def _envelope(self, call: bytes | None = None, owner: int | None = None) -> bytes:
        if self.tx_sequence >= 2**64 - 1:
            raise EndpointEnded("SEQUENCE_EXHAUSTED")
        body = ENVELOPE.pack(self.connection, self.boot, self.tx_sequence,
                             self.call if call is None else call,
                             self.owner if owner is None else owner)
        self.tx_sequence += 1
        return body

    async def send_control(self, kind: int, tail: bytes, *, idle: bool = False) -> None:
        if self.closed or (self.ending and kind != 3):
            raise EndpointEnded("ENDPOINT_CLOSED")
        value = (kind, tail, ZERO16 if idle else self.call, 0 if idle else self.owner)
        try:
            await asyncio.wait_for(self.control.put(value), .25)
        except TimeoutError:
            raise EndpointEnded("CONTROL_BLOCKED") from None

    def send_audio(self, generated_us: int, prior_us: int, pcm: bytes) -> None:
        if len(pcm) != SAMPLES * 2:
            raise EndpointEnded("AUDIO_INVALID")
        if self.held:
            return
        if len(self.audio) == self.audio.maxlen:
            self.audio.popleft()
        self.audio.append((generated_us, prior_us, pcm))
        self.audio_ready.set()

    async def start(self) -> None:
        if self.started or self.call == ZERO16 or not self.owner:
            raise EndpointEnded("START_STATE_INVALID")
        if self.answer_policy == "ring" and self.config.endpoint_profile != 4:
            raise EndpointEnded("ANSWER_POLICY_UNSUPPORTED")
        label = b"HA PoC"
        mode = 1 if self.mode == "full_duplex" else 2
        await self.send_control(2, START.pack(
            mode, 2 if self.answer_policy == "ring" else 1, 0, len(label)) + label)
        self.started = True
        await asyncio.wait_for(self.control.join(), .25)
        if self.volume_prepared:
            try:
                await asyncio.wait_for(self.volume_applied_ready.wait(), .75)
            except TimeoutError:
                raise EndpointEnded("VOLUME_APPLY_TIMEOUT") from None

    async def answer(self) -> None:
        if (self.config.endpoint_profile != 4 or self.answer_policy != "ring"
                or not self.started or self.answered):
            raise EndpointEnded("ANSWER_STATE_INVALID")
        await self.send_control(16, CALL_CONTROL.pack(self.mode_generation, 1, 0))

    async def decline(self) -> None:
        if (self.config.endpoint_profile != 4 or self.answer_policy != "ring"
                or not self.started or self.answered):
            raise EndpointEnded("ANSWER_STATE_INVALID")
        await self.send_control(16, CALL_CONTROL.pack(self.mode_generation, 2, 0))

    async def hangup(self, reason: int = 13) -> None:
        if not self.closed and self.started:
            await self.send_control(3, struct.pack("<H", reason))

    async def writer_loop(self) -> None:
        while not self.closed:
            from_control = False
            try:
                kind, tail, call, owner = self.control.get_nowait()
                from_control = True
                body = self._envelope(call, owner) + tail
            except asyncio.QueueEmpty:
                if not self.audio:
                    self.audio_ready.clear()
                    try:
                        await asyncio.wait_for(self.audio_ready.wait(), .02)
                    except TimeoutError:
                        continue
                if not self.answered or self.held:
                    await asyncio.sleep(.01)
                    continue
                generated, prior, pcm = self.audio.popleft()
                now = asyncio.get_running_loop().time()
                now_us = int(now * 1_000_000)
                queued = self.clock.prior_age(generated, now_us, now)
                self.tx_queue_hold_max_ms = max(self.tx_queue_hold_max_ms, queued // 1_000)
                inherited = prior + queued
                if inherited >= CLOCK_MEDIA_EXPIRY_US:
                    continue
                tail = AUDIO.pack(self.mode_generation, self.tx_media_sequence, now_us, inherited, SAMPLES) + pcm
                self.tx_media_sequence += 1
                kind, body = 1, self._envelope() + tail
            await self._write_raw(kind, body)
            if from_control:
                self.control.task_done()
            self.last_tx = asyncio.get_running_loop().time()
            if kind == 1:
                self.last_audio_tx = self.last_tx

    async def reader_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self.closed:
            kind, body = await self._read_raw()
            if len(body) < ENVELOPE.size:
                raise EndpointEnded("FRAME_INVALID")
            connection, boot, sequence, call, owner = ENVELOPE.unpack_from(body)
            if connection != self.connection or boot != self.boot or sequence != self.rx_sequence:
                raise EndpointEnded("FRAME_REPLAY_OR_EPOCH")
            self.rx_sequence += 1
            tail = body[ENVELOPE.size:]
            idle = call == ZERO16 and owner == 0
            if kind in (4, 5) and idle:
                await self._clock_frame(kind, tail)
                continue
            if kind == 17 and idle:
                await self._source_request(tail)
                continue
            if kind == 19 and idle:
                await self._local_wake(tail, idle=True)
                continue
            if kind == 12 and idle and self.config.endpoint_profile == 4:
                if self.answered or self.held or len(tail) != STATUS.size:
                    raise EndpointEnded("STATUS_INVALID")
                state, mode, health, generation, _, _, _, _, report, _ = STATUS.unpack(tail)
                if state != 0 or mode != 0 or generation != 0 or report <= self.last_status_report_us:
                    raise EndpointEnded("STATUS_INVALID")
                if not self.clock.updated:
                    continue  # Idle snapshots grant no readiness before clock bootstrap.
                transit = self.clock.report_transit_us(report, int(loop.time() * 1_000_000), loop.time())
                self.last_status_report_us, self.status_health = report, health
                self.last_status_received = loop.time() - transit / 1_000_000
                continue
            if call != self.call or owner != self.owner:
                raise EndpointEnded("OWNER_MISMATCH")
            now = loop.time()
            if kind == 1:
                if len(tail) != AUDIO.size + SAMPLES * 2:
                    raise EndpointEnded("AUDIO_INVALID")
                mode_gen, sequence, generated, prior, samples = AUDIO.unpack_from(tail)
                if mode_gen != self.mode_generation or sequence != self.rx_media_sequence or samples != SAMPLES:
                    raise EndpointEnded("AUDIO_SEQUENCE_INVALID")
                if not self.answered or self.mode != "full_duplex" or self.held:
                    raise EndpointEnded("AUDIO_STATE_INVALID")
                self.rx_media_sequence += 1
                self.audio_prior_age_max_ms = max(self.audio_prior_age_max_ms, prior // 1_000)
                self.clock_uncertainty_ms = int(self.clock.uncertainty_us) // 1_000
                try:
                    local_generated, age = self.clock.local_generated(
                        generated, prior, int(now * 1_000_000), now)
                except EndpointEnded as err:
                    if err.args != ("AUDIO_EXPIRED",):
                        raise
                    local_generated = int(generated - self.clock.offset_us)
                    self.audio_transit_age_max_ms = max(
                        self.audio_transit_age_max_ms,
                        max(0, int(now * 1_000_000) - local_generated) // 1_000)
                    self.audio_stale_drops += 1
                    continue
                self.audio_transit_age_max_ms = max(
                    self.audio_transit_age_max_ms,
                    max(0, int(now * 1_000_000) - local_generated) // 1_000)
                if self.capture.full():
                    self.capture.get_nowait()
                self.capture.put_nowait((int(now * 1_000_000), age, tail[AUDIO.size:]))
                self.last_rx = now
            elif kind == 7:
                if len(tail) != 1:
                    raise EndpointEnded("RING_INVALID")
                self.ring_seen = True
            elif kind == 8:
                if len(tail) != 8:
                    raise EndpointEnded("ANSWER_INVALID")
                answer_mode, accepted, health, answer_generation = struct.unpack("<BBHI", tail)
                required_health = 31 if self.mode == "full_duplex" else 13
                if (not self.ring_seen or accepted != 1
                        or (self.answer_policy == "ring" and not self.accept_token)
                        or answer_mode != (1 if self.mode == "full_duplex" else 2)
                        or answer_generation != self.mode_generation
                        or health & required_health != required_health
                        or health & (0xe0 if self.mode == "full_duplex" else 0xc0)):
                    raise EndpointEnded("ANSWER_INVALID")
                self.status_health = health
                self.room_mic_muted = self.mode == "speaker_only" and bool(health & 32)
                self.answered = True
            elif kind == 16:
                await self._call_control(tail)
            elif kind == 19:
                await self._local_wake(tail)
            elif kind == 20:
                self._command_audio(tail, now)
            elif kind == 18:
                self._volume_control(tail)
            elif kind == 12:
                if len(tail) != STATUS.size:
                    raise EndpointEnded("STATUS_INVALID")
                (state, mode, health, mode_generation, rx_sequence, tx_sequence,
                 rx_age_ms, tx_age_ms, report_us, output_age_ms) = STATUS.unpack(tail)
                if report_us <= self.last_status_report_us:
                    raise EndpointEnded("STATUS_TIME_INVALID")
                transit_us = self.clock.report_transit_us(report_us, int(now * 1_000_000), now)
                self.last_status_report_us = report_us
                self.last_status_received = now - transit_us / 1_000_000
                if self.held:
                    if (state != 7 or mode != (1 if self.mode == "full_duplex" else 2)
                            or mode_generation != self.mode_generation):
                        raise EndpointEnded("STATUS_GENERATION_INVALID")
                    self.status, self.status_health, self.last_sink = state, health, 0.0
                    continue
                if (self.mode == "full_duplex" and state == 4 and mode == 2
                        and mode_generation == self.mode_generation + 1 and health & 32):
                    self.mode = "speaker_only"
                    self.mode_generation = mode_generation
                    self.room_mic_muted = True
                    while not self.capture.empty():
                        self.capture.get_nowait()
                elif mode_generation != self.mode_generation:
                    raise EndpointEnded("STATUS_GENERATION_INVALID")
                expected = 3 if self.mode == "full_duplex" else 4
                if state == expected and mode == (1 if self.mode == "full_duplex" else 2):
                    self.status = state
                    self.status_health = health
                    self.status_rx_sequence, self.status_tx_sequence = rx_sequence, tx_sequence
                    required_health = 31 if self.mode == "full_duplex" else 13
                    self.last_sink = 0.0
                    if output_age_ms == UNKNOWN_PROGRESS_MS:
                        continue
                    rx_age_us = rx_age_ms * 1_000 + transit_us
                    tx_age_us = tx_age_ms * 1_000 + transit_us
                    output_age_us = output_age_ms * 1_000 + transit_us
                    if ((health & required_health) == required_health
                            and rx_age_us <= 500_000 and output_age_us <= 500_000
                            and (self.mode != "full_duplex" or tx_age_us <= 500_000)):
                        self.last_sink = now - output_age_us / 1_000_000
                elif state in (1, 2, 6):
                    self.status = state
                    self.last_sink = 0.0
                else:
                    raise EndpointEnded("STATUS_INVALID")
            elif kind in (6, 9, 13):
                raise EndpointEnded(self._terminal_reason(kind, tail))
            elif kind == 15:
                pass
            elif kind in (4, 5):
                await self._clock_frame(kind, tail, idle=False)
            else:
                raise EndpointEnded("FRAME_INVALID")

    async def _call_control(self, tail: bytes) -> None:
        if self.config.endpoint_profile != 4 or len(tail) != CALL_CONTROL.size:
            raise EndpointEnded("CALL_CONTROL_INVALID")
        generation, operation, token = CALL_CONTROL.unpack(tail)
        if operation in (4, 5):
            if (not self.held or not token or token != self.command_token
                    or generation != self.mode_generation + (operation == 5)):
                raise EndpointEnded("COMMAND_CONTROL_INVALID")
            if operation == 4:
                if self.command_ready.is_set():
                    raise EndpointEnded("COMMAND_CONTROL_INVALID")
                self.command_ready.set()
            else:
                self.mode_generation = generation
                self._flush_media()
                self.held = False
                self.command_ready.clear()
                self.command_token = 0
                self.status, self.last_sink = 0, 0.0
            return
        if (generation != self.mode_generation or operation != 1 or not token
                or not self.started or not self.ring_seen or self.answer_policy != "ring"
                or (self.accept_token and self.accept_token != token)
                or not self.clock.fresh(asyncio.get_running_loop().time())):
            raise EndpointEnded("CALL_CONTROL_INVALID")
        if self.renewal_authorize is None:
            raise EndpointEnded("AUTHORIZATION_MISSING")
        try:
            authorized = await asyncio.wait_for(self.renewal_authorize(), .25)
        except TimeoutError:
            raise EndpointEnded("AUTHORIZATION_TIMEOUT") from None
        if not authorized:
            raise EndpointEnded("AUTH_REVOKED")
        if self.closed or self.ending or generation != self.mode_generation:
            raise EndpointEnded("STALE_CALL")
        self.accept_token = token
        await self.send_control(16, CALL_CONTROL.pack(generation, 3, token))

    async def _source_request(self, tail: bytes) -> None:
        if (self.config.endpoint_profile != 4 or not self.capabilities & 64
                or len(tail) != SOURCE_REQUEST.size
                or self.started or self.call != ZERO16 or self.owner != 0
                or self.source_handler is None or self.peer_room_id is None
                or not self.clock.fresh(asyncio.get_running_loop().time())):
            raise EndpointEnded("SOURCE_STATE_INVALID")
        nonce, target = SOURCE_REQUEST.unpack(tail)
        if not nonce or nonce <= self.source_nonce or target in (ZERO16, self.config.expected_uuid.bytes):
            raise EndpointEnded("SOURCE_REQUEST_INVALID")
        now = asyncio.get_running_loop().time()
        if self.clock.report_transit_us(nonce, int(now * 1_000_000), now) >= 15_000_000:
            raise EndpointEnded("SOURCE_REQUEST_EXPIRED")
        self.source_nonce = nonce
        if self.source_task is not None and not self.source_task.done():
            raise EndpointEnded("SOURCE_REQUEST_BUSY")
        async def deliver() -> None:
            try:
                await asyncio.wait_for(self.source_handler(
                    self.peer_room_id, nonce, str(uuid.UUID(bytes=target)),
                    self.connection, self.boot), max(.001, 15 - self.clock.report_transit_us(
                        nonce, int(asyncio.get_running_loop().time() * 1_000_000),
                        asyncio.get_running_loop().time()) / 1_000_000))
            except Exception:
                pass  # Denied/expired request never opens capture or replays later.
        self.source_task = asyncio.create_task(deliver())

    def _flush_media(self) -> None:
        self.audio.clear()
        for queue in (self.capture, self.command_capture):
            while not queue.empty():
                queue.get_nowait()

    async def _local_wake(self, tail: bytes, *, idle: bool = False) -> None:
        if (self.config.endpoint_profile != 4 or not self.capabilities & 128
                or len(tail) != LOCAL_WAKE.size):
            raise EndpointEnded("LOCAL_WAKE_INVALID")
        generation, token, created, local = LOCAL_WAKE.unpack(tail)
        now = asyncio.get_running_loop().time()
        age = self.clock.report_transit_us(created, int(now * 1_000_000), now)
        if (local != 1 or not token or token <= self.last_wake_token or age >= 2_000_000
                or self.held or self.room_mic_muted or self.status_health & 32
                or (idle and (self.started or self.call != ZERO16 or self.owner or generation))
                or (not idle and (not self.started or not (self.answered or self.ring_seen)
                    or self.mode_generation == 2**32 - 1
                    or generation != self.mode_generation + 1))):
            raise EndpointEnded("LOCAL_WAKE_INVALID")
        self.last_wake_token = token
        self.wake_sequence, self.wake_received_at = self.rx_sequence - 1, now
        self._flush_media()
        if not idle:
            if not self.answered:
                self.accept_token = 0
            self.mode_generation, self.command_token = generation, token
            self.held, self.hold_started, self.command_sequence = True, now - age / 1_000_000, 0
            self.command_ready.clear()
            self.last_command, self.last_sink = 0.0, 0.0
        if self.wake_handler:
            if self.wake_task is not None and not self.wake_task.done():
                raise EndpointEnded("LOCAL_WAKE_BUSY")
            event = {"request_id": hashlib.sha256(self.boot + self.connection
                       + struct.pack("<Q", token)).digest()[:16],
                     "sequence": self.wake_sequence, "received_at": now,
                     "call_id": None if idle else str(uuid.UUID(bytes=self.call)),
                     "generation": generation, "token": token}
            async def deliver() -> None:
                try:
                    await asyncio.wait_for(self.wake_handler(self, event), 15)
                except Exception:
                    if self.held and self.command_token == token and not self.closed:
                        await self.resume_command()
            self.wake_task = asyncio.create_task(deliver())

    def _command_audio(self, tail: bytes, now: float) -> None:
        if (self.config.endpoint_profile != 4 or not self.held
                or not self.command_ready.is_set() or len(tail) != COMMAND_AUDIO.size + SAMPLES * 2):
            raise EndpointEnded("COMMAND_AUDIO_INVALID")
        generation, token, sequence, generated, prior, samples = COMMAND_AUDIO.unpack_from(tail)
        if ((generation, token, sequence, samples) != (
                self.mode_generation, self.command_token, self.command_sequence, SAMPLES)):
            raise EndpointEnded("COMMAND_AUDIO_INVALID")
        self.command_sequence += 1
        if now - self.hold_started >= 15:
            self._flush_media()
            return
        _, age = self.clock.local_generated(generated, prior, int(now * 1_000_000), now)
        if self.command_capture.full():
            self.command_capture.get_nowait()
        self.command_capture.put_nowait((int(now * 1_000_000), age, tail[COMMAND_AUDIO.size:]))
        self.last_command = now

    async def resume_command(self) -> None:
        if not self.held or self.closed or self.ending:
            raise EndpointEnded("COMMAND_STATE_INVALID")
        await self.send_control(16, CALL_CONTROL.pack(self.mode_generation, 5, self.command_token))

    def hold_healthy(self, now: float) -> bool:
        required = 31 if self.mode == "full_duplex" else 13
        return (self.held and not self.closed and not self.ending
                and self.clock.fresh(now) and now - self.hold_started <= 15
                and self.last_status_received > 0 and now - self.last_status_received <= .5
                and self.status_health & required == required and not self.status_health & 0xe0
                and (now - self.hold_started <= .5 or now - self.last_command <= .5))

    def hold_ready(self, now: float) -> bool:
        """Only the authenticated hold ACK permits a media-health exception."""
        return self.command_ready.is_set() and self.hold_healthy(now)

    async def prepare_volume(self, level: int, maximum: int, chime: int, revision: int) -> dict:
        """Stage call-bound settings; PREPARED never means gain was applied."""
        if self.call == ZERO16 or not self.owner or not self.clock.fresh(asyncio.get_running_loop().time()):
            raise EndpointEnded("VOLUME_UNSUPPORTED_OR_INVALID")
        return await self.set_volume(level, maximum, chime, revision, prepare=True)

    async def set_volume(self, level: int, maximum: int, chime: int, revision: int, *,
                         prepare: bool = False) -> dict:
        if (self.config.endpoint_profile != 4 or not self.capabilities & 256
                or (prepare and (self.started or self.mode_generation != 0))
                or (not prepare and not self.started)
                or any(type(value) is not int for value in (level, maximum, chime, revision))
                or not 5 <= level <= maximum <= 100 or not 5 <= chime <= 100
                or not 0 < revision <= 2**53 - 1):
            raise EndpointEnded("VOLUME_UNSUPPORTED_OR_INVALID")
        async with self.volume_lock:
            request = (self.mode_generation, level, maximum, chime, revision)
            if self.volume_request and (revision < self.volume_request[-1]
                    or (revision == self.volume_request[-1] and request != self.volume_request)):
                raise EndpointEnded("VOLUME_REVISION_INVALID")
            self.volume_request = request
            self.volume_applied_ready.clear()
            self.volume_waiter = asyncio.get_running_loop().create_future()
            await self.send_control(18, VOLUME.pack(*request, 0))
            try:
                return await asyncio.wait_for(self.volume_waiter, .75)
            except TimeoutError:
                raise EndpointEnded("VOLUME_APPLY_TIMEOUT") from None

    def _volume_control(self, tail: bytes) -> None:
        if (self.config.endpoint_profile != 4 or not self.capabilities & 256
                or len(tail) != VOLUME.size):
            raise EndpointEnded("VOLUME_ACK_INVALID")
        generation, level, maximum, chime, revision, result = VOLUME.unpack(tail)
        if (not self.volume_request or generation != self.mode_generation
                or (maximum, chime, revision) != (self.volume_request[2],
                   self.volume_request[3], self.volume_request[4])
                or not 5 <= level <= maximum <= 100 or not 5 <= chime <= 100 or result not in (1, 2, 3)):
            raise EndpointEnded("VOLUME_ACK_INVALID")
        applied = (generation, level, maximum, chime, revision)
        if result == 3:
            if self.started or applied != self.volume_request:
                raise EndpointEnded("VOLUME_ACK_INVALID")
            self.volume_prepared = True
            if self.volume_waiter and not self.volume_waiter.done():
                self.volume_waiter.set_result({"state": "prepared", "level": level,
                    "maximum": maximum, "chime": chime, "revision": revision})
            return
        if result == 2:
            raise EndpointEnded("VOLUME_APPLY_FAILED")
        if not self.started:
            raise EndpointEnded("VOLUME_ACK_INVALID")
        if self.volume_applied and self.volume_applied[-1] == revision:
            if applied != self.volume_applied:
                raise EndpointEnded("VOLUME_ACK_INVALID")
        self.volume_applied = applied
        self.volume_applied_ready.set()
        if self.volume_waiter and not self.volume_waiter.done():
            self.volume_waiter.set_result({"level": level, "maximum": maximum,
                                          "chime": chime, "revision": revision})

    @staticmethod
    def _terminal_reason(kind: int, tail: bytes) -> str:
        label = {6: "ERROR", 9: "BUSY", 13: "END"}[kind]
        if kind in (9, 13):
            if len(tail) != 2:
                raise EndpointEnded("TERMINAL_INVALID")
            code = struct.unpack("<H", tail)[0]
        else:
            if len(tail) < 3:
                raise EndpointEnded("TERMINAL_INVALID")
            code, text_len = struct.unpack_from("<HB", tail)
            if text_len > 96 or len(tail) != 3 + text_len:
                raise EndpointEnded("TERMINAL_INVALID")
            try:
                tail[3:].decode("utf-8", "strict")
            except UnicodeDecodeError as err:
                raise EndpointEnded("TERMINAL_INVALID") from err
        if not 1 <= code <= 13:
            raise EndpointEnded("TERMINAL_INVALID")
        return f"ENDPOINT_{label}_{code}"

    async def _clock_frame(self, kind: int, tail: bytes, idle: bool = True) -> None:
        now = asyncio.get_running_loop().time()
        now_us = int(now * 1_000_000)
        if kind == 4:
            if len(tail) != PING.size:
                raise EndpointEnded("CLOCK_INVALID")
            nonce, t1 = PING.unpack(tail)
            await self.send_control(5, PONG.pack(nonce, t1, now_us, int(asyncio.get_running_loop().time() * 1_000_000)), idle=idle)
        else:
            if len(tail) != PONG.size:
                raise EndpointEnded("CLOCK_INVALID")
            self.clock.pong(*PONG.unpack(tail), now_us, now)

    async def pinger_loop(self) -> None:
        nonce = 0
        while not self.closed:
            now_us = int(asyncio.get_running_loop().time() * 1_000_000)
            if self.clock.updated > 0 and not self.clock.fresh(asyncio.get_running_loop().time()):
                raise EndpointEnded("CLOCK_STALE")
            if self.held and not self.hold_healthy(asyncio.get_running_loop().time()):
                raise EndpointEnded("COMMAND_HEALTH_FAILED")
            self.clock.pending = {nonce: now_us}
            idle = not self.started
            if not idle:
                if self.renewal_authorize is None:
                    raise EndpointEnded("AUTHORIZATION_MISSING")
                try:
                    authorized = await asyncio.wait_for(self.renewal_authorize(), .25)
                except TimeoutError:
                    raise EndpointEnded("AUTHORIZATION_TIMEOUT") from None
                if not authorized:
                    raise EndpointEnded("AUTH_REVOKED")
            await self.send_control(4, PING.pack(nonce, now_us), idle=idle)
            nonce = (nonce + 1) % 2**64
            await asyncio.sleep(1)

    def ready(self, now: float) -> bool:
        return (self.answered and not self.held and self.status in (3, 4) and self.clock.fresh(now)
                and self.last_sink > 0 and now - self.last_sink <= .5)

    def bind(self, call_id: bytes, owner_generation: int, mode: str,
             mode_generation: int, authorize: Callable[[], Awaitable[bool]],
             answer_policy: str) -> None:
        if (self.started or self.closed or len(call_id) != 16 or call_id == ZERO16
                or not 0 < owner_generation < 2**64 or not 0 <= mode_generation < 2**32
                or mode not in ("full_duplex", "speaker_only")
                or answer_policy not in ("ring", "drop_in")):
            raise EndpointEnded("CALL_BIND_INVALID")
        self.call, self.owner = call_id, owner_generation
        self.mode, self.mode_generation = mode, mode_generation
        self.renewal_authorize, self.answer_policy = authorize, answer_policy
        self.last_status_report_us = -1

    async def _wait_closed(self) -> None:
        try:
            await self.writer.wait_closed()
        except OSError:
            # A cancelled close caller leaves this shielded waiter running.
            # Consume expected peer-close errors here, before shield reports them.
            self.writer.transport.abort()

    async def close(self) -> None:
        self.closed = True
        self._flush_media()
        if self.volume_waiter and not self.volume_waiter.done():
            self.volume_waiter.set_exception(EndpointEnded("ENDPOINT_CLOSED"))
        callbacks = [task for task in (self.source_task, self.wake_task)
                     if task is not None and task is not asyncio.current_task()]
        for task in callbacks:
            task.cancel()
        await asyncio.gather(*callbacks, return_exceptions=True)
        if self.writer:
            self.writer.close()
            try:
                # Concurrent terminal watcher cancellation must not cancel the
                # StreamWriter's shared TLS close waiter for the call cleanup.
                await asyncio.wait_for(asyncio.shield(self._wait_closed()), .5)
            except TimeoutError:
                self.writer.transport.abort()


async def _bootstrap(client: EndpointClient, tasks: list[asyncio.Task]) -> None:
    await client.connect()
    tasks.extend(asyncio.create_task(method()) for method in (
        client.writer_loop, client.reader_loop, client.pinger_loop))
    deadline = asyncio.get_running_loop().time() + 2
    while not client.clock.fresh(asyncio.get_running_loop().time()):
        if asyncio.get_running_loop().time() >= deadline:
            raise EndpointEnded("CLOCK_BOOTSTRAP_TIMEOUT")
        for task in tasks:
            if task.done():
                if not task.cancelled() and task.exception():
                    raise task.exception()
                raise EndpointEnded("ENDPOINT_CLOSED")
        await asyncio.sleep(.01)


async def _stop_transport(client: EndpointClient, tasks: list[asyncio.Task]) -> None:
    client.ending = True
    client.answered = False
    client._flush_media()
    while not client.control.empty():
        client.control.get_nowait()
        client.control.task_done()
    try:
        await client.hangup()
        await asyncio.wait_for(client.control.join(), .25)
        if client.started and len(tasks) >= 2:
            # Let the peer retire this owner before the pool opens its next stream.
            await asyncio.wait([tasks[1]], timeout=.25)
    except (EndpointEnded, OSError, TimeoutError):
        pass
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await client.close()


class EndpointPool:
    """Manager-owned single authenticated stream per enrolled profile4 room."""

    def __init__(self, *, source_handler: Callable[..., Awaitable[None]],
                 wake_handler: Callable[..., Awaitable[None]] | None = None) -> None:
        self.source_handler = source_handler
        self.wake_handler = wake_handler
        self.entries: dict[str, tuple[EndpointClient, list[asyncio.Task]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.closed = False
        self.configurations: dict[str, EndpointConfig] = {}
        self.last_errors: dict[str, str] = {}

    async def _open(self, room_id: str, config: EndpointConfig) -> EndpointClient:
        if (self.closed or config.endpoint_profile != 4 or room_id != str(config.expected_uuid)
                or self.configurations.get(room_id) != config):
            raise EndpointEnded("POOL_CONFIGURATION_INVALID")
        entry = self.entries.get(room_id)
        if entry:
            client, tasks = entry
            if client.config == config and not client.closed and not any(task.done() for task in tasks):
                return client
            self.entries.pop(room_id, None)
            await _stop_transport(client, tasks)
        client = EndpointClient(config, ZERO16, 0, "speaker_only")
        client.peer_room_id, client.source_handler = room_id, self.source_handler
        client.wake_handler = self.wake_handler
        tasks: list[asyncio.Task] = []
        try:
            await _bootstrap(client, tasks)
            if self.closed or self.configurations.get(room_id) != config:
                raise EndpointEnded("POOL_CONFIGURATION_CHANGED")
        except BaseException:
            await _stop_transport(client, tasks)
            raise
        self.entries[room_id] = (client, tasks)
        self.last_errors.pop(room_id, None)
        watched = list(tasks)
        async def watch() -> None:
            try:
                await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                for task in watched:
                    task.cancel()
                await asyncio.gather(*watched, return_exceptions=True)
                await client.close()
            except asyncio.CancelledError:
                raise
        # The watcher observes only the three transport tasks, never itself.
        watcher = asyncio.create_task(watch())
        tasks.append(watcher)
        return client

    async def open(self, room_id: str, config: EndpointConfig) -> EndpointClient:
        self.configurations[room_id] = config
        async with self.locks.setdefault(room_id, asyncio.Lock()):
            return await self._open(room_id, config)

    def matches(self, room_id: str, connection: bytes, boot: bytes) -> bool:
        entry = self.entries.get(room_id)
        return bool(entry and not entry[0].closed and entry[0].connection == connection
                    and entry[0].boot == boot and not any(task.done() for task in entry[1]))

    async def answer(self, room_id: str, call_id: bytes, owner_generation: int,
                     mode_generation: int) -> None:
        await self._command(room_id, call_id, owner_generation, mode_generation, False)

    async def decline(self, room_id: str, call_id: bytes, owner_generation: int,
                      mode_generation: int) -> None:
        await self._command(room_id, call_id, owner_generation, mode_generation, True)

    async def set_volume(self, room_id: str, call_id: bytes, owner_generation: int,
                         mode_generation: int, level: int, maximum: int, chime: int,
                         revision: int) -> dict:
        entry = self.entries.get(room_id)
        if (not entry or entry[0].closed or (entry[0].call, entry[0].owner,
                entry[0].mode_generation) != (call_id, owner_generation, mode_generation)):
            raise EndpointEnded("STALE_CALL")
        return await entry[0].set_volume(level, maximum, chime, revision)

    async def _command(self, room_id: str, call: bytes, owner: int,
                       generation: int, decline: bool) -> None:
        entry = self.entries.get(room_id)
        if (not entry or entry[0].closed or (entry[0].call, entry[0].owner,
                entry[0].mode_generation) != (call, owner, generation)):
            raise EndpointEnded("STALE_CALL")
        await (entry[0].decline() if decline else entry[0].answer())

    async def remove(self, room_id: str) -> None:
        self.configurations.pop(room_id, None)
        entry = self.entries.get(room_id)
        if entry:
            await _stop_transport(*entry)
            if self.entries.get(room_id) is entry:
                self.entries.pop(room_id, None)

    async def close(self) -> None:
        self.closed = True
        await asyncio.gather(*(self.remove(room) for room in list(self.entries)))

    async def run(self, config: EndpointConfig, call: bytes, owner: int, mode: str,
                  generation: int, bridge: Callable[[EndpointClient], Awaitable[None]],
                  authorize: Callable[[], Awaitable[bool]], answer_policy: str) -> None:
        room = str(config.expected_uuid)
        async with self.locks.setdefault(room, asyncio.Lock()):
            client = await self._open(room, config)
            tasks = self.entries[room][1]
            client.bind(call, owner, mode, generation, authorize, answer_policy)
            worker = asyncio.create_task(bridge(client))
            try:
                done, _ = await asyncio.wait([*tasks, worker], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled() and task.exception():
                        raise task.exception()
                if worker not in done:
                    raise EndpointEnded("ENDPOINT_CLOSED")
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                if self.entries.get(room, (None,))[0] is client:
                    self.entries.pop(room, None)
                await _stop_transport(client, tasks)
                if not self.closed and self.configurations.get(room) == config:
                    try:
                        await self._open(room, config)
                    except (EndpointEnded, OSError, TimeoutError) as err:
                        self.last_errors[room] = (err.args[0] if isinstance(err, EndpointEnded)
                                                  else "IDLE_RECONNECT_FAILED")


async def run_endpoint(config: EndpointConfig, call_id: bytes, owner_generation: int,
                       mode: str, mode_generation: int,
                       bridge: Callable[[EndpointClient], Awaitable[None]],
                       renewal_authorize: Callable[[], Awaitable[bool]], *,
                       pool: EndpointPool | None = None, answer_policy: str = "drop_in",
                       volume_config: dict | None = None) -> None:
    """Connect, run one bound call, and guarantee terminal cleanup."""
    if answer_policy not in ("drop_in", "ring"):
        raise EndpointEnded("ANSWER_POLICY_UNSUPPORTED")
    if volume_config is not None:
        if (config.endpoint_profile != 4 or type(volume_config) is not dict
                or set(volume_config) != {"level", "maximum", "chime", "revision"}):
            raise EndpointEnded("VOLUME_UNSUPPORTED_OR_INVALID")
        requested_volume = dict(volume_config)
        original_bridge = bridge
        async def configured_bridge(client: EndpointClient) -> None:
            await client.prepare_volume(**requested_volume)
            await original_bridge(client)
        bridge = configured_bridge
    if pool is not None and config.endpoint_profile == 4:
        await pool.run(config, call_id, owner_generation, mode, mode_generation,
                       bridge, renewal_authorize, answer_policy)
        return
    client = EndpointClient(config, call_id, owner_generation, mode, mode_generation,
                            renewal_authorize)
    client.answer_policy = answer_policy
    tasks: list[asyncio.Task] = []
    try:
        await _bootstrap(client, tasks)
        tasks.append(asyncio.create_task(bridge(client)))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if task.exception():
                raise task.exception()
    finally:
        await _stop_transport(client, tasks)
