"""Foreground browser PCM to independent speaker-only announcement destinations.

Admission, tickets, reservations and history belong to the manager. PCM remains
transient; the shared browser protocol and endpoint transport own wire validation.
"""
from __future__ import annotations

import asyncio
from collections import deque
import re

from .endpoint import CLOCK_MEDIA_EXPIRY_US, EndpointConfig, EndpointEnded, run_endpoint
from .media import (BROWSER_PREFLIGHT_TIMEOUT_S, REAL_SETUP_TIMEOUT_S, ClockRelation,
                    MediaEnded, PING, PONG, READY, WSMsgType)
from .session import Refused, decode_audio, decode_frame, encode_frame


def _reason(error, fallback="MEDIA_ERROR"):
    value = error.args[0] if error.args else fallback
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else fallback


async def run_live_announcement(ws, owner, endpoints: dict[str, EndpointConfig],
                                authorize, on_state, is_current, stop: asyncio.Event,
                                endpoint_pool=None, *, volume_configs=None) -> None:
    """Run an already attached owner; at most seven rooms plus this browser.

    Metrics are metadata snapshots, never PCM or raw exception text. Accepted is
    admission only; active requires fresh browser media and real sink health.
    """
    loop = asyncio.get_running_loop()
    clock = ClockRelation()
    source = deque(maxlen=3)
    source_ready = asyncio.Event()
    control = asyncio.Queue(maxsize=16)
    peers, destination_tasks = {}, {}
    tasks = []
    results = {room: {"status": "accepted", "reason": "ACCEPTED", "ever_active": False,
                      "frames_forwarded": 0, "queue_drops": 0, "queue_high_water": 0,
                      "age_max_us": 0} for room in endpoints}
    metrics = {"uplink_frames": 0, "forwarded_frames": 0, "stale_drops": 0,
               "source_queue_drops": 0, "source_queue_high_water": 0,
               "browser_uplink_age_max_us": 0, "browser_clock_rejected_samples": 0,
               "browser_clock_rejected_uncertainty_max_ms": 0,
               "destination_capture_frames": 0, "cleanup_ms": 0}
    shared = {"ready": 0.0, "uplink": 0.0}
    freshness = 1.0 if owner.principal.ingress_class == "nabu_remote" else .5
    reason = "ENDED"

    def valid():
        return not stop.is_set() and is_current() and bool(getattr(owner, "attached", False))

    async def authorized():
        if not valid():
            return False
        try:
            return bool(await asyncio.wait_for(authorize(), .25)) and valid()
        except TimeoutError:
            raise MediaEnded("AUTHORIZATION_TIMEOUT") from None

    def source_fresh(now):
        return (clock.fresh(now) and all(shared[key] > 0 and now - shared[key] <= freshness
                                        for key in ("ready", "uplink")))

    async def emit(state, why):
        snapshot = dict(metrics)
        snapshot["destination_results"] = {room: dict(result) for room, result in results.items()}
        snapshot["active_room_ids"] = [room for room, result in results.items() if result["status"] == "active"]
        await on_state(state, why, snapshot)

    async def put_control(frame):
        try:
            await asyncio.wait_for(control.put(frame), .25)
        except TimeoutError:
            raise MediaEnded("CONTROL_BLOCKED") from None

    async def writer():
        while valid():
            frame = await control.get()
            try:
                await asyncio.wait_for(ws.send_bytes(frame), .25)
            except TimeoutError:
                raise MediaEnded("WRITE_TIMEOUT") from None

    async def reader():
        expected = 0
        while valid():
            message = await ws.receive()
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                raise MediaEnded("SOCKET_CLOSED")
            if message.type != WSMsgType.BINARY:
                raise MediaEnded("INVALID_FRAME")
            kind, payload = decode_frame(message.data)
            now = loop.time()
            now_us = int(now * 1_000_000)
            if kind == 3:
                sent, prior, pcm = decode_audio(payload, expected)
                expected += 1
                if expected >= 0xFFFFFFFF:
                    raise MediaEnded("SEQUENCE_EXHAUSTED")
                age = clock.age_upper(sent, prior, now_us, now)
                metrics["browser_uplink_age_max_us"] = max(metrics["browser_uplink_age_max_us"], age)
                if age >= CLOCK_MEDIA_EXPIRY_US:
                    metrics["stale_drops"] += 1
                    continue
                if not bool(getattr(owner, "caller_tx_enabled", True)):
                    raise MediaEnded("SOURCE_MUTED")
                # Rebase to HA receipt time and carry the ENTIRE measured age.
                # Dispatcher and EndpointClient retain this tuple unchanged.
                if len(source) == source.maxlen:
                    metrics["source_queue_drops"] += 1
                source.append((now_us, age, pcm))
                metrics["source_queue_high_water"] = max(metrics["source_queue_high_water"], len(source))
                source_ready.set()
                shared["uplink"] = now
                metrics["uplink_frames"] += 1
            elif kind == 4:
                bits, rate, route, reserved = READY.unpack(payload)
                # A live source needs capture as well as the existing browser
                # worklet/route readiness bits. Destination mode stays speaker-only.
                if reserved or route > 1 or not 8_000 <= rate <= 192_000 or bits != 15:
                    raise MediaEnded("MEDIA_NOT_READY")
                shared["ready"] = now
            elif kind == 5:
                nonce, t1 = PING.unpack(payload)
                t2 = int(loop.time() * 1_000_000)
                await put_control(encode_frame(6, PONG.pack(nonce, t1, t2, int(loop.time() * 1_000_000))))
            elif kind == 6:
                clock.pong(payload, now_us, now)
                metrics["browser_clock_rejected_samples"] = clock.rejected_samples
                metrics["browser_clock_rejected_uncertainty_max_ms"] = clock.rejected_uncertainty_max_ms
            else:
                raise MediaEnded("INVALID_FRAME")
        raise MediaEnded("STOPPED" if stop.is_set() else "OWNER_REVOKED")

    async def pinger():
        while valid():
            await put_control(clock.ping(int(loop.time() * 1_000_000)))
            await asyncio.sleep(1)

    async def distribute():
        while valid():
            await source_ready.wait()
            source_ready.clear()
            while source and valid():
                frame = source.popleft()
                generated, prior, _ = frame
                age = prior + max(0, int(loop.time() * 1_000_000) - generated)
                if age >= CLOCK_MEDIA_EXPIRY_US:
                    metrics["stale_drops"] += 1
                    continue
                for room, peer in list(peers.items()):
                    result = results[room]
                    try:
                        full = len(peer.audio) == peer.audio.maxlen
                        peer.send_audio(*frame)
                    except Exception:
                        # A broken destination must never interrupt fanout.
                        result.update(status="failed", reason="ENDPOINT_ERROR")
                        peers.pop(room, None)
                        peer.audio.clear()
                        destination_tasks[room].cancel()
                        continue
                    result["queue_drops"] += full
                    result["queue_high_water"] = max(result["queue_high_water"], len(peer.audio))
                    result["frames_forwarded"] += 1
                    result["age_max_us"] = max(result["age_max_us"], age)
                    metrics["forwarded_frames"] += 1
            await asyncio.sleep(0)

    async def destination(room, config):
        result = results[room]

        async def bridge(client):
            try:
                if not await authorized():
                    raise MediaEnded("AUTH_REVOKED")
                await client.start()
                peers[room] = client
                started = loop.time()
                last_healthy = started
                while valid():
                    now = loop.time()
                    active = (source_fresh(now) and result["frames_forwarded"] > 0 and client.ready(now))
                    if active:
                        last_healthy = now
                        result.update(status="active", reason="OUTPUT_HEALTHY", ever_active=True)
                    elif result["status"] != "failed":
                        result.update(status="accepted", reason="MEDIA_NO_PROGRESS")
                    if result["ever_active"] and now - last_healthy >= 2:
                        raise EndpointEnded("MEDIA_TIMEOUT")
                    if not result["ever_active"] and now - started >= REAL_SETUP_TIMEOUT_S:
                        raise EndpointEnded("SETUP_TIMEOUT")
                    await asyncio.sleep(.05)
            finally:
                peers.pop(room, None)
                client.audio.clear()

        try:
            options = {"pool": endpoint_pool}
            if volume_configs is not None and room in volume_configs:
                options["volume_config"] = volume_configs[room]
            await run_endpoint(config, owner.call_id, owner.generation, "speaker_only",
                               getattr(owner, "mode_generation", 0), bridge, authorized,
                               **options)
            if valid():
                result.update(status="failed", reason="ENDPOINT_CLOSED")
        except asyncio.CancelledError:
            raise
        except (EndpointEnded, MediaEnded, Refused) as error:
            result.update(status="failed", reason=_reason(error, "ENDPOINT_ERROR"))
        except Exception:
            result.update(status="failed", reason="ENDPOINT_ERROR")

    async def watchdog():
        started = loop.time()
        previous = None
        last_emit = 0
        while valid():
            if not await authorized():
                raise MediaEnded("AUTH_REVOKED")
            now = loop.time()
            if not bool(getattr(owner, "caller_tx_enabled", True)):
                raise MediaEnded("SOURCE_MUTED")
            for key in ("ready", "uplink"):
                if shared[key] and now - shared[key] >= 2:
                    raise MediaEnded("MEDIA_TIMEOUT")
            if not clock.fresh(now):
                raise MediaEnded("CLOCK_STALE")
            for room, task in destination_tasks.items():
                result = results[room]
                if not result["ever_active"] and result["status"] != "failed" and now - started >= REAL_SETUP_TIMEOUT_S:
                    result.update(status="failed", reason="SETUP_TIMEOUT")
                    peer = peers.pop(room, None)
                    if peer:
                        peer.audio.clear()
                    task.cancel()
            active = source_fresh(now) and any(result["status"] == "active" for result in results.values())
            state = "ACTIVE_SPEAKER_ONLY" if active else "CONNECTING"
            change = (state, tuple((room, result["status"], result["reason"]) for room, result in results.items()))
            if change != previous or now - last_emit >= 1:
                await emit(state, "LIVE_ANNOUNCEMENT" if active else "MEDIA_NO_PROGRESS")
                previous, last_emit = change, now
            if all(result["status"] == "failed" for result in results.values()):
                raise MediaEnded("ALL_DESTINATIONS_FAILED")
            await asyncio.sleep(.05)
        raise MediaEnded("STOPPED" if stop.is_set() else "OWNER_REVOKED")

    try:
        if not endpoints or len(endpoints) > 7:
            raise MediaEnded("INVALID_PARTICIPANTS")
        if getattr(owner, "mode", "speaker_only") != "speaker_only":
            raise MediaEnded("UNSUPPORTED_MODE")
        if not await authorized():
            raise MediaEnded("AUTH_REVOKED")
        await emit("CONNECTING", "LIVE_ANNOUNCEMENT")
        tasks = [asyncio.create_task(method()) for method in (writer, reader, pinger, distribute)]
        deadline = loop.time() + BROWSER_PREFLIGHT_TIMEOUT_S
        while not source_fresh(loop.time()):
            if not valid():
                raise MediaEnded("STOPPED" if stop.is_set() else "OWNER_REVOKED")
            if not await authorized():
                raise MediaEnded("AUTH_REVOKED")
            for task in tasks:
                if task.done():
                    task.result()
                    raise MediaEnded("MEDIA_ENDED")
            if loop.time() >= deadline:
                raise MediaEnded("BROWSER_READY_TIMEOUT")
            await asyncio.sleep(.01)
        # Do not replay preflight audio to endpoints that attach later.
        source.clear()
        destination_tasks = {room: asyncio.create_task(destination(room, config)) for room, config in endpoints.items()}
        tasks.append(asyncio.create_task(watchdog()))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise MediaEnded("STOPPED" if stop.is_set() else "OWNER_REVOKED")
    except (MediaEnded, Refused) as error:
        reason = _reason(error)
    except asyncio.CancelledError:
        reason = "CANCELLED"
        raise
    except Exception:
        reason = "MEDIA_ERROR"
    finally:
        cleanup = loop.time()
        stop.set()
        source.clear()
        while not control.empty():
            control.get_nowait()
        for peer in peers.values():
            peer.audio.clear()
        for task in [*tasks, *destination_tasks.values()]:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, *destination_tasks.values(), return_exceptions=True)
        peers.clear()
        metrics["cleanup_ms"] = int((loop.time() - cleanup) * 1000)
        for result in results.values():
            if result["status"] != "failed":
                result.update(status="completed" if reason == "STOPPED" and result["ever_active"] else "failed", reason=reason)
        try:
            await emit("TERMINATED", reason)
        finally:
            try:
                await asyncio.wait_for(ws.close(code=1000, message=reason.encode("ascii")), .5)
            except (TimeoutError, OSError):
                force_close = getattr(ws, "force_close", None)
                if force_close:
                    force_close()
