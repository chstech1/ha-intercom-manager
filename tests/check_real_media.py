"""Behavioral async bridge checks with fake browser and endpoint transports."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import struct
import sys
from types import SimpleNamespace


HERE = Path(__file__).resolve().parent
COMPONENT = HERE.parent / "custom_components/intercom_m3_poc"
sys.path.insert(0, str(COMPONENT))
spec = importlib.util.spec_from_file_location("media", COMPONENT / "media.py")
media = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = media
spec.loader.exec_module(media)
from session import AUDIO, decode_frame, encode_frame  # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.incoming = asyncio.Queue()
        self.outgoing = []
        self.closed = False

    async def receive(self):
        return await self.incoming.get()

    async def send_bytes(self, value: bytes):
        self.outgoing.append(value)
        kind, payload = decode_frame(value)
        if kind == 5:
            nonce, t1 = media.PING.unpack(payload)
            now = int(asyncio.get_running_loop().time() * 1_000_000)
            await self.incoming.put(SimpleNamespace(
                type=media.WSMsgType.BINARY,
                data=encode_frame(6, media.PONG.pack(nonce, t1, now, now))))

    async def close(self, **kwargs):
        self.close_arguments = kwargs
        self.closed = True


class FakeEndpoint:
    def __init__(self) -> None:
        self.audio = deque(maxlen=3)
        self.capture = asyncio.Queue(maxsize=3)
        self.started = False
        self.mode = "speaker_only"
        self.mode_generation = 0
        self.room_mic_muted = False
        self.unmute_edges = 0
        self.upgrade_generation = None
        self.upgrade_started = 0
        self.upgrade_result = None
        self.last_sink = 0.0
        self.status_rx_sequence = 0
        self.status_tx_sequence = 0
        self.audio_prior_age_max_ms = 19
        self.audio_transit_age_max_ms = 23
        self.clock_uncertainty_ms = 3
        self.audio_stale_drops = 2
        self.tx_queue_hold_max_ms = 7
        self.clock = SimpleNamespace(rejected_samples=4,
                                     rejected_uncertainty_max_ms=123)
        self.forwarded = asyncio.Event()
        self.ready_value = False

    def send_audio(self, generated: int, prior: int, pcm: bytes):
        self.audio.append((generated, prior, pcm))
        self.forwarded.set()

    async def start(self):
        assert self.forwarded.is_set(), "START preceded first valid browser uplink"
        self.started = True

    def ready(self, now):
        return self.ready_value


@dataclass
class Principal:
    ingress_class: str = "direct_lan"


async def check_mode(mode: str) -> None:
    ws, endpoint = FakeWS(), FakeEndpoint()
    endpoint.mode = mode
    owner = SimpleNamespace(call_id=bytes(16), generation=1, mode_generation=0,
                            attached=True, caller_tx_enabled=True, principal=Principal())
    current = True
    states = []
    preflight_checked = asyncio.Event()

    async def fake_run(_config, _call, _owner, mode, _mode_generation, bridge,
                       renewal_authorize):
        assert await renewal_authorize()
        task = asyncio.create_task(bridge(endpoint))
        await asyncio.sleep(.1)
        assert not endpoint.started and not endpoint.forwarded.is_set()
        assert not any(decode_frame(frame)[0] == 3 for frame in ws.outgoing)
        preflight_checked.set()
        await asyncio.wait_for(endpoint.forwarded.wait(), 2)
        while not endpoint.started:
            await asyncio.sleep(.01)
        if mode == "speaker_only":
            await asyncio.sleep(1.1)  # No capture is healthy in speaker-only mode.
            assert not task.done(), "capture wait timed out a speaker-only call"
        if mode == "full_duplex":
            await endpoint.capture.put((int(asyncio.get_running_loop().time() * 1_000_000), 10_000,
                                        bytes(1024)))
            while not any(decode_frame(frame)[0] == 3 for frame in ws.outgoing):
                await asyncio.sleep(.01)
            await asyncio.sleep(.06)
        await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.CLOSE, data=b""))
        await task

    original = media.run_endpoint
    media.run_endpoint = fake_run
    try:
        task = asyncio.create_task(media._run_real_media(
            ws, owner, mode, lambda: asyncio.sleep(0, result=True),
            lambda state, reason, metrics: asyncio.sleep(0, result=states.append((state, reason, metrics))),
            lambda: current, object()))
        await asyncio.wait_for(preflight_checked.wait(), 1)
        assert not endpoint.started
        ready = media.READY.pack(15 if mode == "full_duplex" else 14, 48_000, 0, 0)
        await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,
                                              data=encode_frame(4, ready)))
        await asyncio.sleep(.05)
        assert not endpoint.started, "READY without uplink started endpoint"
        now = int(asyncio.get_running_loop().time() * 1_000_000)
        pcm = bytes(1024)
        await ws.incoming.put(SimpleNamespace(
            type=media.WSMsgType.BINARY,
            data=encode_frame(3, AUDIO.pack(0, now, 2_000_000, 512, bytes([0x44]) * 1024))))
        await asyncio.sleep(.05)
        assert not endpoint.started and not endpoint.forwarded.is_set()
        now = int(asyncio.get_running_loop().time() * 1_000_000)
        await ws.incoming.put(SimpleNamespace(
            type=media.WSMsgType.BINARY,
            data=encode_frame(3, AUDIO.pack(1, now, 350_000, 512, pcm))))
        await asyncio.wait_for(task, 3)
    finally:
        media.run_endpoint = original
    assert endpoint.started
    generated, prior, pcm = endpoint.audio[-1]
    assert pcm == bytes(1024) and prior >= 350_000
    assert abs(generated - int(asyncio.get_running_loop().time() * 1_000_000)) < 2_000_000
    assert states[-1][0] == "TERMINATED"
    final_metrics = states[-1][2]
    assert final_metrics["endpoint_audio_prior_age_max_ms"] == 19
    assert final_metrics["endpoint_audio_transit_age_max_ms"] == 23
    assert final_metrics["endpoint_clock_uncertainty_ms"] == 3
    assert final_metrics["endpoint_audio_stale_drops"] == 2
    assert final_metrics["endpoint_tx_queue_hold_max_ms"] == 7
    assert final_metrics["endpoint_clock_rejected_samples"] == 4
    assert final_metrics["endpoint_clock_rejected_uncertainty_max_ms"] == 123
    assert final_metrics["browser_uplink_age_max_ms"] >= 2_000
    assert final_metrics["stale_drops"] == 1
    if mode == "full_duplex":
        assert final_metrics["browser_downlink_age_max_ms"] >= 10
    assert not any(state.startswith("ACTIVE_") for state, _reason, _metrics in states)


async def check_soft_mute() -> None:
    ws, endpoint = FakeWS(), FakeEndpoint()
    states = []
    owner = SimpleNamespace(call_id=bytes(16), generation=1, mode_generation=0,
                            attached=True, caller_tx_enabled=True, principal=Principal())

    async def put_audio(sequence: int, marker: int) -> None:
        now = int(asyncio.get_running_loop().time() * 1_000_000)
        await ws.incoming.put(SimpleNamespace(
            type=media.WSMsgType.BINARY,
            data=encode_frame(3, AUDIO.pack(sequence, now, 0, 512, bytes([marker]) * 1024))))

    async def keep_ready(duration: float) -> None:
        deadline = asyncio.get_running_loop().time() + duration
        while asyncio.get_running_loop().time() < deadline:
            await ws.incoming.put(SimpleNamespace(
                type=media.WSMsgType.BINARY,
                data=encode_frame(4, media.READY.pack(14, 48_000, 0, 0))))
            await asyncio.sleep(.1)

    async def fake_run(_config, _call, _owner, _mode, _mode_generation, bridge,
                       _renewal_authorize):
        task = asyncio.create_task(bridge(endpoint))
        await asyncio.wait_for(endpoint.forwarded.wait(), 2)
        while not endpoint.started:
            await asyncio.sleep(.01)
        await put_audio(1, 0x42)
        await put_audio(2, 0x43)
        for _ in range(50):
            if len(endpoint.audio) == 3:
                break
            await asyncio.sleep(.01)
        assert len(endpoint.audio) == 3
        owner.caller_tx_enabled = False
        await keep_ready(2.1)
        assert endpoint.audio and all(pcm == bytes(1024) for _generated, _prior, pcm in endpoint.audio)
        owner.caller_tx_enabled = True
        await keep_ready(.1)
        assert not task.done(), "stale pre-mute uplink terminated unmute"
        await put_audio(3, 0x44)
        for _ in range(50):
            if any(pcm == bytes([0x44]) * 1024 for _generated, _prior, pcm in endpoint.audio):
                break
            await asyncio.sleep(.01)
        assert any(pcm == bytes([0x44]) * 1024 for _generated, _prior, pcm in endpoint.audio)
        assert not any(pcm in (bytes([0x41]) * 1024, bytes([0x42]) * 1024,
                               bytes([0x43]) * 1024) for _generated, _prior, pcm in endpoint.audio)
        owner.caller_tx_enabled = False
        await keep_ready(.1)
        owner.caller_tx_enabled = True
        await keep_ready(2.1)
        await task

    original = media.run_endpoint
    media.run_endpoint = fake_run
    try:
        task = asyncio.create_task(media._run_real_media(
            ws, owner, "speaker_only", lambda: asyncio.sleep(0, result=True),
            lambda state, reason, metrics: asyncio.sleep(
                0, result=states.append((state, reason, metrics))),
            lambda: True, object()))
        await asyncio.sleep(.05)
        await ws.incoming.put(SimpleNamespace(
            type=media.WSMsgType.BINARY,
            data=encode_frame(4, media.READY.pack(14, 48_000, 0, 0))))
        await asyncio.sleep(.05)
        await put_audio(0, 0x41)
        await asyncio.wait_for(task, 6)
    finally:
        media.run_endpoint = original
    assert states[-1][0:2] == ("TERMINATED", "MEDIA_TIMEOUT")


async def check_no_progress_watchdog() -> None:
    """Dropped endpoint audio cannot manufacture progress indefinitely."""
    ws, endpoint, states = FakeWS(), FakeEndpoint(), []
    owner = SimpleNamespace(call_id=bytes(16), generation=1, mode_generation=0,
                            attached=True, caller_tx_enabled=True, principal=Principal())

    async def fake_run(_config, _call, _owner, _mode, _generation, bridge,
                       renewal_authorize):
        assert await renewal_authorize()
        await bridge(endpoint)

    original = media.run_endpoint
    media.run_endpoint = fake_run
    try:
        task = asyncio.create_task(media._run_real_media(
            ws, owner, "speaker_only", lambda: asyncio.sleep(0, result=True),
            lambda state, reason, metrics: asyncio.sleep(
                0, result=states.append((state, reason, metrics))),
            lambda: True, object()))
        await asyncio.sleep(.05)
        await ws.incoming.put(SimpleNamespace(
            type=media.WSMsgType.BINARY,
            data=encode_frame(4, media.READY.pack(14, 48_000, 0, 0))))
        now = int(asyncio.get_running_loop().time() * 1_000_000)
        await ws.incoming.put(SimpleNamespace(
            type=media.WSMsgType.BINARY,
            data=encode_frame(3, AUDIO.pack(0, now, 0, 512, bytes(1024)))))
        await asyncio.wait_for(task, 3)
    finally:
        media.run_endpoint = original
    assert states[-1][0:2] == ("TERMINATED", "MEDIA_TIMEOUT")
    assert not any(state.startswith("ACTIVE_") for state, _reason, _metrics in states)


async def check_setup_deadline_is_initial_only() -> None:
    async def run_case(recover: bool) -> list[tuple[str, str, dict]]:
        ws, endpoint, states = FakeWS(), FakeEndpoint(), []
        owner = SimpleNamespace(call_id=bytes(16), generation=1, mode_generation=0,
                                attached=True, caller_tx_enabled=True, principal=Principal())
        endpoint.ready_value = recover

        async def fake_run(_config, _call, _owner, _mode, _generation, bridge,
                           renewal_authorize):
            assert await renewal_authorize()
            await bridge(endpoint)

        async def feed(sequence: int) -> None:
            now = asyncio.get_running_loop().time()
            endpoint.last_sink = now
            endpoint.status_rx_sequence += 1
            await ws.incoming.put(SimpleNamespace(
                type=media.WSMsgType.BINARY,
                data=encode_frame(4, media.READY.pack(14, 48_000, 0, 0))))
            await ws.incoming.put(SimpleNamespace(
                type=media.WSMsgType.BINARY,
                data=encode_frame(3, AUDIO.pack(
                    sequence, int(now * 1_000_000), 0, 512, bytes(1024)))))

        original_run, original_timeout = media.run_endpoint, media.REAL_SETUP_TIMEOUT_S
        media.run_endpoint, media.REAL_SETUP_TIMEOUT_S = fake_run, .2
        try:
            task = asyncio.create_task(media._run_real_media(
                ws, owner, "speaker_only", lambda: asyncio.sleep(0, result=True),
                lambda state, reason, metrics: asyncio.sleep(
                    0, result=states.append((state, reason, metrics))),
                lambda: True, object()))
            await asyncio.sleep(.05)
            await feed(0)
            if not recover:
                sequence = 1
                while not task.done():
                    await asyncio.sleep(.05)
                    await feed(sequence)
                    sequence += 1
                await asyncio.wait_for(task, 1)
                return states
            for _ in range(30):
                if any(state == "ACTIVE_SPEAKER_ONLY" for state, _reason, _metrics in states):
                    break
                await asyncio.sleep(.02)
            assert any(state == "ACTIVE_SPEAKER_ONLY" for state, _reason, _metrics in states)
            await asyncio.sleep(.22)  # Move past the initial-only setup deadline.
            endpoint.ready_value = False
            await feed(1)
            await asyncio.sleep(.1)
            assert not task.done(), "post-active health dip reused setup deadline"
            assert states[-1][0:2] == ("CONNECTING", "MEDIA_NO_PROGRESS")
            endpoint.ready_value = True
            await feed(2)
            for _ in range(30):
                if sum(state == "ACTIVE_SPEAKER_ONLY" for state, _reason, _metrics in states) >= 2:
                    break
                await asyncio.sleep(.02)
            assert sum(state == "ACTIVE_SPEAKER_ONLY" for state, _reason, _metrics in states) >= 2
            await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.CLOSE, data=b""))
            await asyncio.wait_for(task, 1)
            return states
        finally:
            media.run_endpoint, media.REAL_SETUP_TIMEOUT_S = original_run, original_timeout

    recovered = await run_case(True)
    assert recovered[-1][0] == "TERMINATED"
    never_active = await run_case(False)
    assert never_active[-1][0:2] == ("TERMINATED", "SETUP_TIMEOUT")


async def check_initial_clock_rejection_recovery() -> None:
    class StartupClockWS(FakeWS):
        def __init__(self, always_bad: bool = False) -> None:
            super().__init__()
            self.always_bad = always_bad
            self.pings = 0

        async def send_bytes(self, value: bytes):
            self.outgoing.append(value)
            kind, payload = decode_frame(value)
            if kind == 5:
                self.pings += 1
                nonce, t1 = media.PING.unpack(payload)
                if self.always_bad or self.pings == 1:
                    await asyncio.sleep(.15)
                now = int(asyncio.get_running_loop().time() * 1_000_000)
                await self.incoming.put(SimpleNamespace(
                    type=media.WSMsgType.BINARY,
                    data=encode_frame(6, media.PONG.pack(nonce, t1, now, now))))

    async def run_recovery() -> None:
        ws, endpoint, states = StartupClockWS(), FakeEndpoint(), []
        owner = SimpleNamespace(call_id=bytes(16), generation=1, mode_generation=0,
                                attached=True, caller_tx_enabled=True, principal=Principal())

        async def fake_run(_config, _call, _owner, _mode, _generation, bridge,
                           _renewal_authorize):
            await bridge(endpoint)

        original_run = media.run_endpoint
        original_uncertainty = media.BROWSER_MAX_CLOCK_UNCERTAINTY_US
        media.run_endpoint = fake_run
        media.BROWSER_MAX_CLOCK_UNCERTAINTY_US = 50_000
        try:
            task = asyncio.create_task(media._run_real_media(
                ws, owner, "speaker_only", lambda: asyncio.sleep(0, result=True),
                lambda state, reason, metrics: asyncio.sleep(
                    0, result=states.append((state, reason, metrics))),
                lambda: True, object()))
            await ws.incoming.put(SimpleNamespace(
                type=media.WSMsgType.BINARY,
                data=encode_frame(4, media.READY.pack(14, 48_000, 0, 0))))
            # Windows event-loop timer granularity can make 60 nominal 25 ms
            # sleeps total under the one-second ping interval.
            for _ in range(120):
                if ws.pings >= 2:
                    break
                await asyncio.sleep(.025)
            assert ws.pings >= 2 and not task.done(), (
                ws.pings, states, task.exception() if task.done() else None)
            await asyncio.sleep(.1)
            assert not task.done()
            now = int(asyncio.get_running_loop().time() * 1_000_000)
            await ws.incoming.put(SimpleNamespace(
                type=media.WSMsgType.BINARY,
                data=encode_frame(3, AUDIO.pack(0, now, 0, 512, bytes(1024)))))
            await asyncio.wait_for(endpoint.forwarded.wait(), 1)
            for _ in range(20):
                if endpoint.started:
                    break
                await asyncio.sleep(.01)
            assert endpoint.started
            await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.CLOSE, data=b""))
            await asyncio.wait_for(task, 1)
        finally:
            media.run_endpoint = original_run
            media.BROWSER_MAX_CLOCK_UNCERTAINTY_US = original_uncertainty
        assert states[-1][2]["browser_clock_rejected_samples"] == 1
        assert states[-1][2]["browser_clock_rejected_uncertainty_max_ms"] > 50

    async def run_deadline() -> None:
        ws, endpoint, states = StartupClockWS(always_bad=True), FakeEndpoint(), []
        owner = SimpleNamespace(call_id=bytes(16), generation=1, mode_generation=0,
                                attached=True, caller_tx_enabled=True, principal=Principal())

        async def fake_run(_config, _call, _owner, _mode, _generation, bridge,
                           _renewal_authorize):
            await bridge(endpoint)

        original_run, original_timeout = media.run_endpoint, media.BROWSER_PREFLIGHT_TIMEOUT_S
        original_uncertainty = media.BROWSER_MAX_CLOCK_UNCERTAINTY_US
        media.run_endpoint, media.BROWSER_PREFLIGHT_TIMEOUT_S = fake_run, .2
        media.BROWSER_MAX_CLOCK_UNCERTAINTY_US = 1
        try:
            task = asyncio.create_task(media._run_real_media(
                ws, owner, "speaker_only", lambda: asyncio.sleep(0, result=True),
                lambda state, reason, metrics: asyncio.sleep(
                    0, result=states.append((state, reason, metrics))),
                lambda: True, object()))
            await ws.incoming.put(SimpleNamespace(
                type=media.WSMsgType.BINARY,
                data=encode_frame(4, media.READY.pack(14, 48_000, 0, 0))))
            await asyncio.wait_for(task, 1)
        finally:
            media.run_endpoint, media.BROWSER_PREFLIGHT_TIMEOUT_S = original_run, original_timeout
            media.BROWSER_MAX_CLOCK_UNCERTAINTY_US = original_uncertainty
        assert states[-1][0:2] == ("TERMINATED", "BROWSER_READY_TIMEOUT")

    await run_recovery()
    await run_deadline()


async def check() -> None:
    internet_clock = media.ClockRelation()
    internet_clock.pending = {1: 1_000_000}
    internet_clock.pong(media.PONG.pack(1, 1_000_000, 1_100_000, 1_100_000),
                        1_600_000, 1.0)
    assert 300_000 <= internet_clock.uncertainty_us < 350_000
    assert internet_clock.fresh(1.1)
    accepted = (internet_clock.offset_us, internet_clock.uncertainty_us,
                internet_clock.updated, internet_clock.last_clock_peer_us)
    internet_clock.pending = {2: 2_000_000}
    internet_clock.pong(media.PONG.pack(2, 2_000_000, 2_100_000, 2_100_000),
                        3_010_000, 1.1)
    assert (internet_clock.offset_us, internet_clock.uncertainty_us,
            internet_clock.updated, internet_clock.last_clock_peer_us) == accepted
    assert internet_clock.rejected_samples == 1
    assert internet_clock.rejected_uncertainty_max_ms > 500
    internet_clock.pending = {3: 4_000_000}
    internet_clock.pong(media.PONG.pack(3, 4_000_000, 4_100_000, 4_100_000),
                        4_200_000, 1.2)
    assert internet_clock.updated == 1.2 and internet_clock.last_clock_peer_us == 4_100_000
    assert internet_clock.rejected_samples == 1
    internet_clock.pending = {4: 5_000_000}
    try:
        internet_clock.pong(media.PONG.pack(5, 5_000_000, 5_100_000, 5_100_000),
                            5_200_000, 1.3)
    except media.MediaEnded as err:
        assert err.args[0] == "CLOCK_INVALID"
    else:
        raise AssertionError("invalid sample was discarded despite fresh clock")
    bad_clock = media.ClockRelation()
    bad_clock.pending = {2: 1_000_000}
    bad_clock.pong(media.PONG.pack(2, 1_000_000, 1_100_000, 1_100_000),
                   2_010_000, 1.0)
    assert not bad_clock.fresh(1.0) and bad_clock.updated == 0
    assert bad_clock.rejected_samples == 1
    bad_clock.pending = {3: 3_000_000}
    bad_clock.pong(media.PONG.pack(3, 3_000_000, 3_100_000, 3_100_000),
                   3_200_000, 1.1)
    assert bad_clock.fresh(1.1) and bad_clock.rejected_samples == 1
    invalid_clock = media.ClockRelation()
    invalid_clock.pending = {3: 1_000_000}
    try:
        invalid_clock.pong(media.PONG.pack(4, 1_000_000, 1_100_000, 1_100_000),
                           1_200_000, 1.0)
    except media.MediaEnded as err:
        assert err.args[0] == "CLOCK_INVALID"
    else:
        raise AssertionError("invalid browser clock sample was discarded")
    future_clock = media.ClockRelation()
    future_clock.updated, future_clock.offset_us, future_clock.uncertainty_us = 1.0, 0, 1_000
    try:
        future_clock.age_upper(2_000_001, 0, 1_000_000, 1.1)
    except media.MediaEnded as err:
        assert err.args[0] == "CLOCK_INVALID"
    else:
        raise AssertionError("future browser sender accepted")
    await check_mode("speaker_only")
    await check_mode("full_duplex")
    await check_soft_mute()
    await check_no_progress_watchdog()
    await check_setup_deadline_is_initial_only()
    await check_initial_clock_rejection_recovery()
    original = media.run_endpoint
    try:
        for supplied, expected in (("ENDPOINT_END_5", "ENDPOINT_END_5"),
                                   ("private /config/key", "ENDPOINT_ERROR")):
            ws, events = FakeWS(), []
            async def failure(*args):
                raise media.EndpointEnded(supplied)
            async def record(state, reason, metrics):
                assert not ws.closed, "failure reason was published after socket close"
                events.append((state, reason))
            media.run_endpoint = failure
            owner = SimpleNamespace(call_id=bytes(16), generation=1, mode_generation=0,
                                    attached=True, caller_tx_enabled=True, principal=Principal())
            await media._run_real_media(ws, owner, "full_duplex",
                                        lambda: asyncio.sleep(0, result=True), record,
                                        lambda: True, object())
            assert events[-1] == ("TERMINATED", expected)
            assert ws.closed and ws.close_arguments == {"code": 1000, "message": expected.encode()}
    finally:
        media.run_endpoint = original
    print("PASS: terminal enum precedes media close; safe close reason survives socket race; private exception text excluded")
    print("PASS: full/speaker preflight and direction, soft-mute purge/silence/resume, no false ACTIVE, age and clock bounds")


if __name__ == "__main__":
    asyncio.run(check())



