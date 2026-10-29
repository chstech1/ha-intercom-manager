"""Bounded room relay/conference over the existing authenticated endpoint transport.

Reservations/policy records belong to the manager. This module owns only volatile
PCM, per-room detector state, and the endpoint tasks for one exact call.
"""
from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from typing import Awaitable, Callable

from .endpoint import CLOCK_MEDIA_EXPIRY_US, EndpointConfig, EndpointEnded, run_endpoint

PCM = struct.Struct("<512h")
SILENCE = bytes(PCM.size)
BLOCK_S = 512 / 16_000
MEDIA_STALL_S = 2.0
FRESH_S = .5
SETUP_S = 10.0


def mix_blocks(blocks: list[tuple[int, int, bytes]], now_us: int) -> tuple[int, int, bytes] | None:
    """Normalize fresh contributors, carrying their earliest conservative expiry.

    generated/prior already use HA monotonic time. Rebase exactly once here;
    EndpointClient adds subsequent queue residence and destination uncertainty.
    """
    fresh = []
    for generated, prior, pcm in blocks:
        if len(pcm) != PCM.size or generated > now_us or prior < 0:
            raise EndpointEnded("AUDIO_INVALID")
        age = prior + now_us - generated
        if age < CLOCK_MEDIA_EXPIRY_US:
            fresh.append((age, PCM.unpack(pcm)))
    if not fresh:
        return None
    # Python integers provide wider intermediate arithmetic without overflow.
    count = len(fresh)
    samples = (max(-32768, min(32767, int(sum(values) / count)))
               for values in zip(*(values for _, values in fresh)))
    return now_us, max(age for age, _ in fresh), PCM.pack(*samples)


class SpeechDetector:
    """The bundled microVAD, reframed to 10 ms with 300 ms qualification."""

    def __init__(self) -> None:
        from pymicro_vad import MicroVad
        self.vad = MicroVad()
        self.pending = bytearray()
        self.speech_chunks = 0

    def reset(self) -> None:
        self.vad.Reset()
        self.pending.clear()
        self.speech_chunks = 0

    def feed(self, pcm: bytes) -> bool:
        self.pending.extend(pcm)
        qualified = False
        while len(self.pending) >= 320:
            probability = self.vad.Process10ms(bytes(self.pending[:320]))
            del self.pending[:320]
            self.speech_chunks = self.speech_chunks + 1 if probability is not None and probability > .5 else 0
            qualified |= self.speech_chunks >= 30
        return qualified


class Inactivity:
    """Monotonic inactivity policy; a command hold pauses rather than resets it."""

    def __init__(self, now: float) -> None:
        self.last_activity = now
        self.last_check = now
        self.configuration: tuple[bool, int] | None = None

    def check(self, now: float, *, enabled: bool, timeout: int, speech: bool, held: bool) -> bool:
        if type(enabled) is not bool or type(timeout) is not int or not 5 <= timeout <= 3600:
            raise EndpointEnded("SETTINGS_INVALID")
        if self.configuration is not None and self.configuration != (enabled, timeout):
            self.last_activity = self.last_check = now
        self.configuration = (enabled, timeout)
        if held or not enabled:
            self.last_activity += max(0, now - self.last_check)
        if speech:
            self.last_activity = now
        self.last_check = now
        return enabled and not held and now - self.last_activity >= timeout


@dataclass
class _Peer:
    client: object
    joined: float
    last_capture: float = 0
    saw_capture: bool = False
    detector: SpeechDetector | None = None
    controls: tuple[bool, bool] = (True, False)
    held_since: float | None = None
    resumed: float = 0


async def run_room_call(call_id: bytes, owner_generation: int,
                        endpoints: dict[str, EndpointConfig], mode: str,
                        stop: asyncio.Event,
                        authorize: Callable[[], Awaitable[bool]],
                        on_state: Callable[[str, str, dict], Awaitable[None]],
                        settings: Callable[[], dict]) -> None:
    """Run 2–4 initial required rooms; active conferences isolate failed peers.

    Settings: silence_enabled (bool), silence_timeout_seconds (5–3600), and
    participants: {room_id: {tx_enabled: bool, held: bool, removed: bool}}.
    State callbacks: CONNECTING, CONNECTED, DEGRADED, TERMINATED. Metrics expose
    room IDs and numeric progress only. Caller must release its reservations.
    Speaker-only requires source_room_id: only that enrolled source captures;
    destinations use the existing speaker-only endpoint mode.
    """
    if len(call_id) != 16 or call_id == bytes(16) or not 0 < owner_generation < 2**64:
        raise ValueError("invalid call owner")
    if not 2 <= len(endpoints) <= 4 or mode not in ("full_duplex", "speaker_only"):
        raise EndpointEnded("UNSUPPORTED_ROOM_MODE")
    initial_settings = settings()
    source_room = initial_settings.get("source_room_id")
    pool = initial_settings.get("endpoint_pool")
    answer_policy = initial_settings.get("answer_policy", "drop_in")
    if answer_policy not in ("drop_in", "ring"):
        raise EndpointEnded("ANSWER_POLICY_INVALID")
    if mode == "speaker_only" and source_room not in endpoints:
        raise EndpointEnded("SOURCE_REQUIRED")
    loop = asyncio.get_running_loop()
    started = loop.time()
    peers: dict[str, _Peer] = {}
    tasks: dict[str, asyncio.Task] = {}
    failures: dict[str, str] = {}
    removed: set[str] = set()
    admitted = set(endpoints)
    pending: dict[str, float] = {}
    accepted: dict[str, float] = {}
    invitation_seen: set[str] = set()
    invitation_states: dict[str, dict[str, str]] = {}
    metrics = {"forwarded_blocks": 0, "stale_drops": 0, "capture_drops": 0,
               "age_max_us": 0, "queue_high_water": 0, "failed_rooms": failures,
               "active_rooms": [], "setup_ms": 0, "silence_detector": "pymicro_vad"}
    active = False
    previous_state = ""
    last_report = started
    reason = "STOPPED"
    inactivity = Inactivity(started)
    setup_started = started if answer_policy == "drop_in" else None

    def room_metrics() -> dict:
        return {room: {
                "frames_tx": getattr(peer.client, "tx_media_sequence", 0),
                "frames_rx": getattr(peer.client, "rx_media_sequence", 0),
                "clock_uncertainty_ms": getattr(peer.client, "clock_uncertainty_ms", 0),
                "stale_drops": getattr(peer.client, "audio_stale_drops", 0),
                "queue_depth": len(peer.client.audio),
            } for room, peer in peers.items()}

    async def report(state: str, why: str = "") -> None:
        nonlocal previous_state, last_report
        previous_state = state
        if peers:
            metrics["rooms"] = room_metrics()
        snapshot = dict(metrics, failed_rooms=dict(failures), active_rooms=sorted(admitted - removed),
                        invitations={room: dict(value) for room, value in invitation_states.items()})
        await asyncio.wait_for(on_state(state, why, snapshot), .25)
        last_report = loop.time()

    async def authorized() -> bool:
        if stop.is_set():
            return False
        return await asyncio.wait_for(authorize(), .25)

    async def bridge(room: str, client) -> None:
        if not await authorized():
            raise EndpointEnded("AUTH_REVOKED")
        await client.start()
        peers[room] = _Peer(client, loop.time())
        await asyncio.Future()  # run_endpoint owns transport/lease/reader failures.

    def flush(peer: _Peer) -> None:
        peer.client.audio.clear()
        while not peer.client.capture.empty():
            peer.client.capture.get_nowait()
        if peer.detector:
            peer.detector.reset()

    async def remove(room: str, why: str) -> None:
        removed.add(room)
        failures[room] = why
        if room in peers:
            flush(peers[room])
        if room in pending:
            pending.pop(room)
            invitation_states[room] = {"state": "FAILED", "reason": why}
        admitted.discard(room)
        if not tasks[room].done() and not tasks[room].cancelling():
            tasks[room].cancel()
        if mode == "speaker_only" and room == source_room:
            # Destinations are receive-only. Their count cannot replace the
            # explicitly authorized source or justify opening another mic.
            raise EndpointEnded("SOURCE_LOST")
        # Never await a slow peer's socket cleanup in the audio mixing loop.

    def start_room(room: str, config: EndpointConfig, policy: str) -> None:
        peer_mode = "speaker_only" if mode == "speaker_only" and room != source_room else "full_duplex"
        options = {"pool": pool, "answer_policy": policy} if pool is not None or policy != "drop_in" else {}
        volume_configs = settings().get("volume_configs", {})
        if not isinstance(volume_configs, dict):
            raise EndpointEnded("SETTINGS_INVALID")
        if volume_configs.get(room) is not None:
            if not isinstance(volume_configs[room], dict):
                raise EndpointEnded("SETTINGS_INVALID")
            # The transport prepares after authenticated bind, then start()
            # waits for APPLIED before the engine can report readiness.
            options["volume_config"] = dict(volume_configs[room])
        tasks[room] = asyncio.create_task(run_endpoint(
            config, call_id, owner_generation, peer_mode, 0,
            lambda client: bridge(room, client), authorized, **options), name="intercom-room-media")

    try:
        if not await authorized():
            raise EndpointEnded("AUTH_REVOKED")
        await report("CONNECTING")
        for room, config in endpoints.items():
            start_room(room, config, answer_policy)
        next_tick = loop.time()
        while not stop.is_set():
            if not await authorized():
                raise EndpointEnded("AUTH_REVOKED")
            now = loop.time()
            config = settings()
            enabled = config.get("silence_enabled", True)
            timeout = config.get("silence_timeout_seconds", 60)
            controls = config.get("participants", {})
            if not isinstance(controls, dict):
                raise EndpointEnded("SETTINGS_INVALID")
            if type(enabled) is not bool or type(timeout) is not int or not 5 <= timeout <= 3600:
                raise EndpointEnded("SETTINGS_INVALID")
            call_types = config.get("silence_call_types", ["room", "conference"])
            if not isinstance(call_types, list) or any(value not in ("room", "conference", "browser") for value in call_types):
                raise EndpointEnded("SETTINGS_INVALID")
            enabled = enabled and config.get("call_type", "conference" if len(admitted) > 2 else "room") in call_types
            invitations = config.get("invitations", {})
            if not isinstance(invitations, dict):
                raise EndpointEnded("SETTINGS_INVALID")
            invitation_seen.intersection_update(invitations)
            if active:
                for room, endpoint in invitations.items():
                    if room in invitation_seen or room in admitted or room in pending:
                        continue
                    if room in tasks and not tasks[room].done():
                        continue  # Previous attempt is still closing its transport.
                    invitation_seen.add(room)
                    if len(admitted) + len(pending) >= 4:
                        invitation_states[room] = {"state": "FAILED", "reason": "CAPACITY"}
                    else:
                        peers.pop(room, None)
                        removed.discard(room)
                        failures.pop(room, None)
                        accepted.pop(room, None)
                        pending[room] = now
                        invitation_states[room] = {"state": "RINGING", "reason": ""}
                        start_room(room, endpoint, "ring")
                    await report(previous_state, "INVITATION_UPDATED")
            for room, task in list(tasks.items()):
                if room not in removed and task.done():
                    if not task.cancelled():
                        task.exception()  # Consume it; expose a bounded reason only.
                    if not active:
                        failures[room] = "TRANSPORT_FAILED"
                        raise EndpointEnded("INITIAL_PEER_FAILED")
                    was_pending = room in pending
                    await remove(room, "TRANSPORT_FAILED")
                    await report(previous_state if was_pending else "DEGRADED",
                                 "INVITATION_UPDATED" if was_pending else "PARTICIPANT_REMOVED")
            for room, invited_at in list(pending.items()):
                client = peers[room].client if room in peers else None
                if client and client.answered and room not in accepted:
                    accepted[room] = now
                    invitation_states[room] = {"state": "CONNECTING", "reason": ""}
                    await report(previous_state, "INVITATION_UPDATED")
                if (room in accepted and now - accepted[room] >= SETUP_S
                        or room not in accepted and now - invited_at >= 30):
                    await remove(room, "SETUP_TIMEOUT" if room in accepted else "NO_ANSWER")
                    await report(previous_state, "INVITATION_UPDATED")
            frames = {}
            speech = held = False
            healthy = True
            for room, peer in list(peers.items()):
                # Status callbacks above (or for a prior peer) may await while
                # fresh capture arrives. Validate it against current HA time.
                now = loop.time()
                if room in removed:
                    continue
                control = controls.get(room, {})
                if not isinstance(control, dict):
                    raise EndpointEnded("SETTINGS_INVALID")
                tx, hold, leave = (control.get("tx_enabled", True), control.get("held", False), control.get("removed", False))
                if any(type(value) is not bool for value in (tx, hold, leave)):
                    raise EndpointEnded("SETTINGS_INVALID")
                # The authenticated transport can enter a local wake hold
                # before the manager's settings snapshot catches up.
                hold = hold or getattr(peer.client, "held", False)
                if leave:
                    if not active:
                        failures[room] = "PARTICIPANT_LEFT"
                        raise EndpointEnded("INITIAL_PEER_REMOVED")
                    was_pending = room in pending
                    await remove(room, "PARTICIPANT_LEFT")
                    await report(previous_state if was_pending else "DEGRADED",
                                 "INVITATION_UPDATED" if was_pending else "PARTICIPANT_REMOVED")
                    continue
                if peer.controls != (tx, hold):
                    # A queued mixed block may contain this room. Purge every
                    # destination on mute/hold edges, including the release edge.
                    for destination in peers.values():
                        destination.client.audio.clear()
                    flush(peer)
                    if peer.controls[1] and not hold:
                        peer.resumed = now
                        peer.saw_capture = False
                    peer.controls = (tx, hold)
                held |= hold and room in admitted
                if hold and peer.held_since is None:
                    peer.held_since = now
                if not hold:
                    peer.held_since = None
                client = peer.client
                newest = None
                dropped = False
                while client.mode == "full_duplex" and not client.capture.empty():
                    metrics["queue_high_water"] = max(metrics["queue_high_water"], client.capture.qsize())
                    if newest is not None:
                        metrics["capture_drops"] += 1
                        dropped = True
                    newest = client.capture.get_nowait()
                if newest is not None:
                    generated, prior, pcm = newest
                    age = prior + int(now * 1_000_000) - generated
                    if generated > int(now * 1_000_000) or prior < 0 or len(pcm) != PCM.size:
                        raise EndpointEnded("AUDIO_INVALID")
                    metrics["age_max_us"] = max(metrics["age_max_us"], age)
                    if age >= CLOCK_MEDIA_EXPIRY_US:
                        metrics["stale_drops"] += 1
                    else:
                        peer.last_capture, peer.saw_capture = now, True
                        if tx and not hold and room in admitted:
                            frames[room] = newest
                            if enabled:
                                if peer.detector is None:
                                    peer.detector = SpeechDetector()
                                elif dropped:
                                    peer.detector.reset()
                                speech |= peer.detector.feed(pcm)
                if not enabled and peer.detector is not None:
                    peer.detector.reset()
                # Only a transport-validated explicit hold may suspend the
                # conversational progress requirement. A settings bit cannot.
                hold_ready = getattr(client, "hold_ready", None)
                held_healthy = hold and callable(hold_ready) and hold_ready(now)
                capture_required = client.mode == "full_duplex" and not held_healthy
                if mode == "speaker_only" and room == source_room and client.mode != "full_duplex":
                    raise EndpointEnded("SOURCE_MUTED")
                capture_age = now - max(peer.last_capture or peer.joined, peer.resumed)
                ready = (held_healthy or client.ready(now)) and (not capture_required or
                         (peer.saw_capture and now - peer.last_capture <= FRESH_S))
                if room in pending:
                    if ready and client.answered:
                        pending.pop(room)
                        admitted.add(room)
                        invitation_states[room] = {"state": "ACTIVE", "reason": ""}
                        await report(previous_state, "INVITATION_UPDATED")
                else:
                    healthy &= ready
                if active and room in admitted and (capture_required and capture_age > MEDIA_STALL_S
                               or not held_healthy and now - max(client.last_sink, peer.resumed, peer.held_since or 0) > MEDIA_STALL_S
                               or peer.held_since is not None and now - peer.held_since > 15):
                    await remove(room, "MEDIA_STALLED")
                    await report("DEGRADED", "PARTICIPANT_REMOVED")
            now = loop.time()
            remaining = admitted - removed
            if len(remaining) < 2:
                raise EndpointEnded("INSUFFICIENT_PARTICIPANTS")
            all_joined = remaining <= peers.keys()
            # Initial setup requires actual capture from every full-duplex room.
            # Silence primes output progress while ANSWER/chime/health settle.
            relay = all_joined and all(peers[r].client.answered and
                                      (peers[r].saw_capture or peers[r].client.mode == "speaker_only")
                                      for r in remaining)
            for room in remaining & peers.keys():
                peer = peers[room]
                if not peer.client.answered or peer.controls[1]:
                    continue
                block = (mix_blocks([frame for source, frame in frames.items()
                                     if source != room and source not in removed], int(loop.time() * 1_000_000))
                         if relay and not peer.controls[1] else None)
                peer.client.send_audio(*(block or (int(loop.time() * 1_000_000), 0, SILENCE)))
                metrics["forwarded_blocks"] += block is not None
            for room in pending.keys() & peers.keys():
                peer = peers[room]
                if peer.client.answered and not peer.controls[1]:
                    peer.client.send_audio(int(loop.time() * 1_000_000), 0, SILENCE)
            if not active:
                if setup_started is None:
                    if all_joined and all(peers[room].client.answered for room in remaining):
                        setup_started = now
                    elif now - started >= 30:
                        raise EndpointEnded("NO_ANSWER")
                    elif previous_state != "RINGING" and any(getattr(peer.client, "ring_seen", False) for peer in peers.values()):
                        await report("RINGING")
                if setup_started is not None and now - setup_started >= SETUP_S:
                    raise EndpointEnded("SETUP_TIMEOUT")
                if all_joined and healthy and relay:
                    active = True
                    metrics["setup_ms"] = int((now - (setup_started if setup_started is not None else started)) * 1000)
                    inactivity = Inactivity(now)
                    await report("CONNECTED")
            elif healthy:
                if previous_state != "CONNECTED" or now - last_report >= .5:
                    await report("CONNECTED")
            elif previous_state != "DEGRADED" or now - last_report >= .5:
                await report("DEGRADED", "MEDIA_NOT_FRESH")
            # Missing fresh required media cannot be counted as quiet. The
            # independent stall/lease/clock gates still remove failed rooms.
            if active and inactivity.check(now, enabled=enabled, timeout=timeout, speech=speech,
                                           held=held or not healthy):
                raise EndpointEnded("SILENCE_TIMEOUT")
            next_tick = max(next_tick + BLOCK_S, loop.time())
            try:
                await asyncio.wait_for(stop.wait(), max(0, next_tick - loop.time()))
            except TimeoutError:
                pass
    except EndpointEnded as err:
        reason = err.args[0] if err.args and err.args[0] in {
            "AUTH_REVOKED", "INITIAL_PEER_FAILED", "INITIAL_PEER_REMOVED", "SETTINGS_INVALID",
            "AUDIO_INVALID", "SOURCE_MUTED", "SOURCE_LOST", "INSUFFICIENT_PARTICIPANTS", "NO_ANSWER", "SETUP_TIMEOUT", "SILENCE_TIMEOUT"} else "ENDPOINT_FAILED"
    except TimeoutError:
        reason = "CONTROL_TIMEOUT"
    except asyncio.CancelledError:
        reason = "CANCELLED"
        raise
    except Exception:
        reason = "MEDIA_ENGINE_FAILED"
    finally:
        cleanup_started = loop.time()
        for peer in peers.values():
            flush(peer)
        for task in tasks.values():
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        metrics["cleanup_ms"] = int((loop.time() - cleanup_started) * 1000)
        metrics["active_rooms"] = []
        metrics["rooms"] = room_metrics()
        peers.clear()
        admitted.clear()
        try:
            await report("TERMINATED", reason)
        except Exception:
            pass
