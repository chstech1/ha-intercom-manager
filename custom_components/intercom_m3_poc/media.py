"""Bounded M3 browser media proof-of-concept; no audio is retained or logged."""

from __future__ import annotations

import asyncio
from collections import deque
import math
import logging
import re
import secrets
import ssl
import struct
import time

try:
    from aiohttp import WSMsgType
except ImportError:  # HA supplies aiohttp; keep the network-free check dependency-free.
    from enum import IntEnum

    class WSMsgType(IntEnum):
        TEXT = 1
        BINARY = 2
        CLOSE = 8
        CLOSING = 256
        CLOSED = 257
        ERROR = 258

try:
    from .session import AUDIO, Refused, decode_audio, decode_frame, encode_frame
except ImportError:  # Standalone check imports this file directly.
    from session import AUDIO, Refused, decode_audio, decode_frame, encode_frame

try:
    from .endpoint import (CLOCK_MEDIA_EXPIRY_US, EndpointClient, EndpointConfig,
                           EndpointEnded, run_endpoint)
except ImportError:
    from endpoint import (CLOCK_MEDIA_EXPIRY_US, EndpointClient, EndpointConfig,
                          EndpointEnded, run_endpoint)


READY = struct.Struct("<BIBH")
PING = struct.Struct("<QQ")
PONG = struct.Struct("<QQQQ")
RATE = 16_000
SAMPLES = 512
PERIOD = SAMPLES / RATE
BROWSER_MAX_CLOCK_UNCERTAINTY_US = 500_000
BROWSER_PREFLIGHT_TIMEOUT_S = 5
REAL_SETUP_TIMEOUT_S = 10
_LOGGER = logging.getLogger(__name__)


class MediaEnded(Exception):
    pass


class ClockRelation:
    def __init__(self) -> None:
        self.offset_us = 0.0       # peer monotonic - local monotonic
        self.uncertainty_us = float("inf")
        self.updated = 0.0
        self.pending: dict[int, int] = {}
        self.last_clock_peer_us = -1
        self.last_media_sent_us = -1
        self.rejected_samples = 0
        self.rejected_uncertainty_max_ms = 0

    def ping(self, now_us: int) -> bytes:
        nonce = secrets.randbits(64)
        self.pending = {nonce: now_us}
        return encode_frame(5, PING.pack(nonce, now_us))

    def pong(self, payload: bytes, now_us: int, now: float) -> None:
        nonce, t1, t2, t3 = PONG.unpack(payload)
        if (self.pending.pop(nonce, None) != t1 or t3 < t2 or now_us < t1
                or t2 < self.last_clock_peer_us):
            raise MediaEnded("CLOCK_INVALID")
        rtt = (now_us - t1) - (t3 - t2)
        if rtt < 0:
            raise MediaEnded("CLOCK_INVALID")
        offset = ((t2 - t1) + (t3 - now_us)) / 2
        resolution = max(1, int(time.get_clock_info("monotonic").resolution * 1_000_000))
        # Peer is browser performance.now(); include its conservative 1 ms quantum.
        resolution = max(resolution, 1000)
        drift = int((rtt + 3_000_000) * 0.0002) + resolution
        uncertainty = max(rtt / 2, resolution) + drift
        if uncertainty > BROWSER_MAX_CLOCK_UNCERTAINTY_US:
            self.rejected_samples += 1
            self.rejected_uncertainty_max_ms = max(
                self.rejected_uncertainty_max_ms, int(uncertainty) // 1_000)
            if self.fresh(now):
                return
            return
        self.offset_us = offset
        self.uncertainty_us = uncertainty
        self.updated = now
        self.last_clock_peer_us = t3

    def fresh(self, now: float) -> bool:
        return self.updated > 0 and now - self.updated <= 3

    def age_upper(self, sent_us: int, prior_us: int, now_us: int, now: float) -> int:
        if not self.fresh(now):
            raise MediaEnded("CLOCK_STALE")
        local_sent = sent_us - self.offset_us
        if sent_us < self.last_media_sent_us or local_sent - now_us > self.uncertainty_us:
            raise MediaEnded("CLOCK_INVALID")
        self.last_media_sent_us = sent_us
        return max(0, int(now_us - local_sent)) + prior_us + int(self.uncertainty_us)


async def _run_simulated_media(ws, owner, mode, authorize, on_state, is_current) -> None:
    """Run one already-attached owner media socket until terminal cleanup."""
    if mode not in ("full_duplex", "speaker_only"):
        raise Refused("UNSUPPORTED_MODE")
    loop = asyncio.get_running_loop()
    clock = ClockRelation()
    control: asyncio.Queue[bytes] = asyncio.Queue(maxsize=16)
    audio: deque[tuple[int, bytes]] = deque(maxlen=3)
    audio_ready = asyncio.Event()
    stop = asyncio.Event()
    metrics = {"uplink_frames": 0, "downlink_frames": 0, "stale_drops": 0,
               "queue_drops": 0, "simulation": True,
               "browser_clock_rejected_samples": 0,
               "browser_clock_rejected_uncertainty_max_ms": 0,
               "endpoint_clock_rejected_samples": 0,
               "endpoint_clock_rejected_uncertainty_max_ms": 0}
    shared = {"ready": 0.0, "uplink": 0.0, "downlink": 0.0, "active": False}
    ceiling = CLOCK_MEDIA_EXPIRY_US
    no_progress = 1.0 if owner.principal.ingress_class == "nabu_remote" else 0.5

    async def emit(state: str, reason: str) -> None:
        await on_state(state, reason, dict(metrics))

    def valid() -> bool:
        return is_current() and bool(getattr(owner, "attached", True))

    async def put_control(frame: bytes) -> None:
        try:
            await asyncio.wait_for(control.put(frame), 0.25)
        except TimeoutError:
            raise MediaEnded("CONTROL_BLOCKED") from None

    async def writer() -> None:
        sequence = 0
        while not stop.is_set():
            is_audio = False
            try:
                frame = control.get_nowait()
            except asyncio.QueueEmpty:
                if not audio:
                    audio_ready.clear()
                    try:
                        await asyncio.wait_for(audio_ready.wait(), 0.02)
                    except TimeoutError:
                        pass
                    continue  # Recheck control before taking queued audio.
                generated_us, pcm = audio.popleft()
                frame = encode_frame(3, AUDIO.pack(sequence, generated_us, 0, SAMPLES, pcm))
                is_audio = True
            try:
                await asyncio.wait_for(ws.send_bytes(frame), 0.25)
            except TimeoutError:
                raise MediaEnded("WRITE_TIMEOUT") from None
            if is_audio:
                sequence += 1
                if sequence >= 0xFFFFFFFF:
                    raise MediaEnded("SEQUENCE_EXHAUSTED")
                metrics["downlink_frames"] += 1
                shared["downlink"] = loop.time()

    async def receiver() -> None:
        expected = 0
        while valid():
            msg = await ws.receive()
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING,
                            WSMsgType.ERROR):
                raise MediaEnded("SOCKET_CLOSED")
            if msg.type != WSMsgType.BINARY:
                raise MediaEnded("INVALID_FRAME")
            kind, payload = decode_frame(msg.data)
            now = loop.time()
            now_us = int(now * 1_000_000)
            if kind == 3:
                sent, prior, _pcm = decode_audio(payload, expected)
                expected += 1
                if expected >= 0xFFFFFFFF:
                    raise MediaEnded("SEQUENCE_EXHAUSTED")
                if clock.age_upper(sent, prior, now_us, now) > ceiling:
                    metrics["stale_drops"] += 1
                    continue
                metrics["uplink_frames"] += 1
                shared["uplink"] = now
            elif kind == 4:
                bits, rate, route, reserved = READY.unpack(payload)
                required = 15 if mode == "full_duplex" else 14
                if (reserved or route > 1 or not 8_000 <= rate <= 192_000
                        or bits & ~15 or bits & required != required):
                    raise MediaEnded("MEDIA_NOT_READY")
                shared["ready"] = now
            elif kind == 5:
                nonce, t1 = PING.unpack(payload)
                t2 = int(loop.time() * 1_000_000)
                t3 = int(loop.time() * 1_000_000)
                await put_control(encode_frame(6, PONG.pack(nonce, t1, t2, t3)))
            elif kind == 6:
                clock.pong(payload, now_us, now)
                metrics["browser_clock_rejected_samples"] = clock.rejected_samples
                metrics["browser_clock_rejected_uncertainty_max_ms"] = (
                    clock.rejected_uncertainty_max_ms)
            else:
                raise MediaEnded("INVALID_FRAME")
        raise MediaEnded("OWNER_REVOKED")

    async def pinger() -> None:
        while valid():
            await put_control(clock.ping(int(loop.time() * 1_000_000)))
            await asyncio.sleep(1)
        raise MediaEnded("OWNER_REVOKED")

    async def tone() -> None:
        phase = 0
        next_frame = loop.time()
        while valid():
            next_frame += PERIOD
            await asyncio.sleep(max(0, next_frame - loop.time()))
            if loop.time() - next_frame > PERIOD:
                next_frame = loop.time()
            pcm = bytearray(1024)
            for i in range(SAMPLES):
                sample = int(900 * math.sin(2 * math.pi * 440 * phase / RATE))
                struct.pack_into("<h", pcm, i * 2, sample)
                phase = (phase + 1) % RATE
            now_us = int(loop.time() * 1_000_000)
            if len(audio) == audio.maxlen:
                metrics["queue_drops"] += 1
            audio.append((now_us, bytes(pcm)))
            audio_ready.set()

    async def watchdog() -> None:
        started = loop.time()
        last_emit = started
        await emit("CONNECTING", "SIMULATED_ENDPOINT")
        while valid():
            await asyncio.sleep(0.05)
            now = loop.time()
            if not await authorize():
                raise MediaEnded("AUTH_REVOKED")
            ready = shared["ready"] > 0 and now - shared["ready"] <= no_progress
            uplink_required = mode == "full_duplex" and bool(getattr(owner, "caller_tx_enabled", True))
            progressing = (now - shared["downlink"] <= no_progress and
                           (not uplink_required or now - shared["uplink"] <= no_progress))
            active = ready and clock.fresh(now) and progressing
            if active and not shared["active"]:
                shared["active"] = True
                await emit("ACTIVE_FULL" if mode == "full_duplex" else "ACTIVE_SPEAKER_ONLY",
                           "SIMULATED_ENDPOINT")
                last_emit = now
            elif not active and shared["active"]:
                shared["active"] = False
                await emit("CONNECTING", "MEDIA_NO_PROGRESS")
                last_emit = now
            elif active and now - last_emit >= 1:
                await emit("ACTIVE_FULL" if mode == "full_duplex" else "ACTIVE_SPEAKER_ONLY",
                           "SIMULATED_ENDPOINT")
                last_emit = now
            required_progress = [shared["downlink"], shared["ready"]]
            if uplink_required:
                required_progress.append(shared["uplink"])
            observed_progress = [stamp for stamp in required_progress if stamp > 0]
            last_required = min(observed_progress) if observed_progress else 0
            if started + 10 < now and not shared["active"]:
                raise MediaEnded("SETUP_TIMEOUT")
            if last_required and now - last_required >= 2:
                raise MediaEnded("MEDIA_TIMEOUT")
        raise MediaEnded("OWNER_REVOKED")

    tasks = [asyncio.create_task(coro()) for coro in (writer, receiver, pinger, tone, watchdog)]
    reason = "ENDED"
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc:
                reason = exc.args[0] if isinstance(exc, (MediaEnded, Refused)) else "MEDIA_ERROR"
                break
    finally:
        stop.set()
        audio.clear()
        while not control.empty():
            control.get_nowait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(ws.close(), 0.5)
        except TimeoutError:
            # Backend also bounds close and owns the dedicated request transport.
            force_close = getattr(ws, "force_close", None)
            if force_close is not None:
                force_close()
        await emit("TERMINATED", reason)


async def _run_real_media(ws, owner, mode, authorize, on_state, is_current,
                          config: EndpointConfig, endpoint_pool=None, volume_config=None) -> None:
    """Bridge browser PCM to one authenticated endpoint without rejuvenating age."""
    loop = asyncio.get_running_loop()
    browser_clock = ClockRelation()
    browser_control: asyncio.Queue[bytes] = asyncio.Queue(maxsize=16)
    browser_audio: deque[tuple[int, int, bytes]] = deque(maxlen=3)
    browser_audio_ready = asyncio.Event()
    stop = asyncio.Event()
    metrics = {"uplink_frames": 0, "downlink_frames": 0, "stale_drops": 0,
               "queue_drops": 0, "simulation": False, "endpoint_sink_frames": 0,
               "endpoint_capture_frames": 0, "room_mic_muted": False,
               "endpoint_audio_prior_age_max_ms": 0, "endpoint_audio_transit_age_max_ms": 0,
               "endpoint_clock_uncertainty_ms": 0, "endpoint_audio_stale_drops": 0,
               "browser_uplink_age_max_ms": 0, "endpoint_tx_queue_hold_max_ms": 0,
               "browser_downlink_queue_hold_max_ms": 0, "browser_downlink_age_max_ms": 0,
               "browser_clock_rejected_samples": 0,
               "browser_clock_rejected_uncertainty_max_ms": 0}
    shared = {"ready": 0.0, "browser_uplink": 0.0, "browser_downlink": 0.0,
              "endpoint_sink": 0.0, "endpoint_capture": 0.0, "active": False}
    ceiling = CLOCK_MEDIA_EXPIRY_US
    no_progress = 1.0 if owner.principal.ingress_class == "nabu_remote" else .5

    def valid() -> bool:
        return is_current() and bool(getattr(owner, "attached", True))

    async def emit(state: str, reason: str) -> None:
        await on_state(state, reason, dict(metrics))

    async def browser_put(frame: bytes) -> None:
        try:
            await asyncio.wait_for(browser_control.put(frame), .25)
        except TimeoutError:
            raise MediaEnded("CONTROL_BLOCKED") from None

    async def bridge(endpoint: EndpointClient) -> None:
        async def browser_writer() -> None:
            sequence = 0
            while valid() and not stop.is_set():
                try:
                    frame = browser_control.get_nowait()
                except asyncio.QueueEmpty:
                    if not browser_audio:
                        browser_audio_ready.clear()
                        try:
                            await asyncio.wait_for(browser_audio_ready.wait(), .02)
                        except TimeoutError:
                            continue
                    generated, prior, pcm = browser_audio.popleft()
                    hold_us = max(0, int(loop.time() * 1_000_000) - generated)
                    metrics["browser_downlink_queue_hold_max_ms"] = max(
                        metrics["browser_downlink_queue_hold_max_ms"], hold_us // 1_000)
                    metrics["browser_downlink_age_max_ms"] = max(
                        metrics["browser_downlink_age_max_ms"], (prior + hold_us) // 1_000)
                    frame = encode_frame(3, AUDIO.pack(sequence, generated, prior, SAMPLES, pcm))
                    sequence += 1
                await asyncio.wait_for(ws.send_bytes(frame), .25)
                if len(frame) > 100:
                    metrics["downlink_frames"] += 1
                    shared["browser_downlink"] = loop.time()

        async def browser_reader() -> None:
            expected = 0
            while valid():
                msg = await ws.receive()
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                    raise MediaEnded("SOCKET_CLOSED")
                if msg.type != WSMsgType.BINARY:
                    raise MediaEnded("INVALID_FRAME")
                kind, payload = decode_frame(msg.data)
                now = loop.time()
                now_us = int(now * 1_000_000)
                if kind == 3:
                    sent, prior, pcm = decode_audio(payload, expected)
                    expected += 1
                    age = browser_clock.age_upper(sent, prior, now_us, now)
                    metrics["browser_uplink_age_max_ms"] = max(
                        metrics["browser_uplink_age_max_ms"], age // 1_000)
                    if age > ceiling:
                        metrics["stale_drops"] += 1
                        continue
                    if bool(getattr(owner, "caller_tx_enabled", True)):
                        before = len(endpoint.audio)
                        endpoint.send_audio(now_us, age, pcm)
                        if before == endpoint.audio.maxlen:
                            metrics["queue_drops"] += 1
                        metrics["uplink_frames"] += 1
                        shared["browser_uplink"] = now
                elif kind == 4:
                    bits, rate, route, reserved = READY.unpack(payload)
                    required = 15 if mode == "full_duplex" else 14
                    if reserved or route > 1 or not 8_000 <= rate <= 192_000 or bits & ~15 or bits & required != required:
                        raise MediaEnded("MEDIA_NOT_READY")
                    shared["ready"] = now
                elif kind == 5:
                    nonce, t1 = PING.unpack(payload)
                    t2 = int(loop.time() * 1_000_000)
                    await browser_put(encode_frame(6, PONG.pack(nonce, t1, t2, int(loop.time() * 1_000_000))))
                elif kind == 6:
                    browser_clock.pong(payload, now_us, now)
                    metrics["browser_clock_rejected_samples"] = browser_clock.rejected_samples
                    metrics["browser_clock_rejected_uncertainty_max_ms"] = (
                        browser_clock.rejected_uncertainty_max_ms)
                else:
                    raise MediaEnded("INVALID_FRAME")
            raise MediaEnded("OWNER_REVOKED")

        async def browser_pinger() -> None:
            while valid():
                await browser_put(browser_clock.ping(int(loop.time() * 1_000_000)))
                await asyncio.sleep(1)

        async def endpoint_capture() -> None:
            while valid():
                generated, prior, pcm = await endpoint.capture.get()
                if prior > ceiling:
                    metrics["stale_drops"] += 1
                    continue
                if len(browser_audio) == browser_audio.maxlen:
                    browser_audio.popleft()
                    metrics["queue_drops"] += 1
                browser_audio.append((generated, prior, pcm))
                browser_audio_ready.set()
                metrics["endpoint_capture_frames"] += 1
                shared["endpoint_capture"] = loop.time()

        async def muted_silence() -> None:
            next_frame = loop.time()
            silence = bytes(SAMPLES * 2)
            while valid():
                next_frame += PERIOD
                await asyncio.sleep(max(0, next_frame - loop.time()))
                if not bool(getattr(owner, "caller_tx_enabled", True)):
                    endpoint.audio.clear()
                    now_us = int(loop.time() * 1_000_000)
                    endpoint.send_audio(now_us, 0, silence)

        async def watchdog() -> None:
            started, last_emit, last_status_sequence = loop.time(), 0.0, -1
            ever_active = False
            tx_enabled = bool(getattr(owner, "caller_tx_enabled", True))
            uplink_floor = 0.0
            uplink_wait_started = 0.0
            await emit("CONNECTING", "REAL_ENDPOINT")
            while valid():
                await asyncio.sleep(.05)
                now = loop.time()
                if not await authorize():
                    raise MediaEnded("AUTH_REVOKED")
                if endpoint.last_sink and endpoint.status_rx_sequence != last_status_sequence:
                    last_status_sequence = endpoint.status_rx_sequence
                    shared["endpoint_sink"] = endpoint.last_sink
                    metrics["endpoint_sink_frames"] = max(metrics["endpoint_sink_frames"], endpoint.status_rx_sequence + 1)
                metrics["room_mic_muted"] = endpoint.room_mic_muted
                metrics["endpoint_audio_prior_age_max_ms"] = endpoint.audio_prior_age_max_ms
                metrics["endpoint_audio_transit_age_max_ms"] = endpoint.audio_transit_age_max_ms
                metrics["endpoint_clock_uncertainty_ms"] = endpoint.clock_uncertainty_ms
                metrics["endpoint_clock_rejected_samples"] = endpoint.clock.rejected_samples
                metrics["endpoint_clock_rejected_uncertainty_max_ms"] = (
                    endpoint.clock.rejected_uncertainty_max_ms)
                metrics["endpoint_audio_stale_drops"] = endpoint.audio_stale_drops
                metrics["endpoint_tx_queue_hold_max_ms"] = endpoint.tx_queue_hold_max_ms
                if endpoint.room_mic_muted:
                    owner.mode = "speaker_only"
                    owner.mode_generation = endpoint.mode_generation
                required = [shared["ready"], shared["endpoint_sink"]]
                if endpoint.mode == "full_duplex":
                    required.extend((shared["browser_downlink"], shared["endpoint_capture"]))
                current_tx_enabled = bool(getattr(owner, "caller_tx_enabled", True))
                if not current_tx_enabled:
                    uplink_floor = max(uplink_floor, shared["browser_uplink"])
                    uplink_wait_started = 0.0
                elif not tx_enabled:
                    uplink_wait_started = now
                tx_enabled = current_tx_enabled
                if current_tx_enabled:
                    uplink = shared["browser_uplink"]
                    if uplink_wait_started:
                        if uplink > uplink_floor:
                            uplink_wait_started = 0.0
                            uplink_floor = 0.0
                        else:
                            uplink = 0.0
                    required.append(uplink)
                active = (endpoint.ready(now) and browser_clock.fresh(now)
                          and all(stamp > 0 and now - stamp <= no_progress for stamp in required))
                ever_active = ever_active or active
                if active != shared["active"] or active and now - last_emit >= 1:
                    shared["active"] = active
                    await emit("ACTIVE_FULL" if active and endpoint.mode == "full_duplex" else
                               "ACTIVE_SPEAKER_ONLY" if active else "CONNECTING",
                               "REAL_ENDPOINT" if active else "MEDIA_NO_PROGRESS")
                    last_emit = now
                if not ever_active and now - started > REAL_SETUP_TIMEOUT_S:
                    raise MediaEnded("SETUP_TIMEOUT")
                if (current_tx_enabled and uplink_wait_started
                        and now - uplink_wait_started >= 2):
                    raise MediaEnded("MEDIA_TIMEOUT")
                observed = [stamp for stamp in required if stamp > 0]
                if observed and now - min(observed) >= 2:
                    raise MediaEnded("MEDIA_TIMEOUT")
            raise MediaEnded("OWNER_REVOKED")

        tasks = [asyncio.create_task(coro()) for coro in
                 (browser_writer, browser_reader, browser_pinger, endpoint_capture,
                  muted_silence)]
        try:
            deadline = loop.time() + BROWSER_PREFLIGHT_TIMEOUT_S
            while not (shared["ready"] and shared["browser_uplink"] and browser_clock.fresh(loop.time())):
                if not await authorize():
                    raise MediaEnded("AUTH_REVOKED")
                if loop.time() >= deadline:
                    raise MediaEnded("BROWSER_READY_TIMEOUT")
                for task in tasks:
                    if task.done():
                        if task.exception():
                            raise task.exception()
                        raise MediaEnded("MEDIA_ENDED")
                await asyncio.sleep(.01)
            if not await authorize():
                raise MediaEnded("AUTH_REVOKED")
            await endpoint.start()
            tasks.append(asyncio.create_task(watchdog()))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.exception():
                    raise task.exception()
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    reason = "ENDED"
    try:
        async def renewal_authorize() -> bool:
            return valid() and await authorize()

        await run_endpoint(config, owner.call_id, owner.generation, mode,
                           getattr(owner, "mode_generation", 0), bridge,
                           renewal_authorize, **({"pool": endpoint_pool} if endpoint_pool is not None else {}),
                           **({"volume_config": volume_config} if volume_config is not None else {}))
    except (EndpointEnded, MediaEnded) as err:
        candidate = err.args[0] if err.args else "ENDPOINT_ERROR"
        reason = candidate if isinstance(candidate, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", candidate) else "ENDPOINT_ERROR"
    except asyncio.IncompleteReadError:
        reason = "ENDPOINT_CLOSED"
    except ssl.SSLError:
        reason = "ENDPOINT_TLS_ERROR"
    except OSError:
        reason = "ENDPOINT_IO_ERROR"
    except Exception:
        reason = "ENDPOINT_INTERNAL_ERROR"
    finally:
        stop.set()
        browser_audio.clear()
        if reason not in ("ENDED", "SOCKET_CLOSED", "OWNER_REVOKED"):
            _LOGGER.warning("Real endpoint call failed: %s", reason)
        # Publish the bounded reason before closing. The same safe enum in the
        # close frame survives races between the control and media sockets.
        try:
            await emit("TERMINATED", reason)
        finally:
            try:
                await asyncio.wait_for(ws.close(code=1000, message=reason.encode("ascii")), .5)
            except (TimeoutError, OSError):
                force_close = getattr(ws, "force_close", None)
                if force_close:
                    force_close()


async def run_media(ws, owner, mode, authorize, on_state, is_current,
                    endpoint_config: EndpointConfig | None = None, endpoint_pool=None, volume_config=None) -> None:
    if endpoint_config is None:
        await _run_simulated_media(ws, owner, mode, authorize, on_state, is_current)
    else:
        await _run_real_media(ws, owner, mode, authorize, on_state, is_current, endpoint_config, endpoint_pool, volume_config)
