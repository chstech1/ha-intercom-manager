"""Check deferred physical-unmute admission with the actual media bridge."""
import asyncio
from types import SimpleNamespace
from check_real_media import FakeWS, FakeEndpoint, Principal, media, AUDIO, encode_frame


async def main():
    ws, endpoint, states = FakeWS(), FakeEndpoint(), []
    owner = SimpleNamespace(call_id=bytes(16), generation=1, mode_generation=1,
                            initial_mode="full_duplex", attached=True,
                            caller_tx_enabled=True, principal=Principal())
    endpoint.mode_generation, endpoint.status, endpoint.ready_value = 1, 4, True
    requests = []

    async def upgrade(generation):
        requests.append(generation)
        endpoint.upgrade_generation = generation
        endpoint.upgrade_started = asyncio.get_running_loop().time()

    endpoint.request_duplex = upgrade

    async def fake_run(_config, _call, _owner, _mode, _generation, bridge, _authorize):
        await bridge(endpoint)

    async def feed():
        sequence = 0
        while True:
            now = asyncio.get_running_loop().time()
            endpoint.last_sink = now
            endpoint.status_rx_sequence += 1
            await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,
                data=encode_frame(4, media.READY.pack(15, 48_000, 0, 0))))
            await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,
                data=encode_frame(3, AUDIO.pack(sequence, int(now * 1_000_000), 0, 512, bytes(1024)))))
            sequence += 1
            await asyncio.sleep(.05)

    original = media.run_endpoint
    original_clock = media.ClockRelation
    class StableClock(original_clock):
        def fresh(self, now):
            return True
        def pong(self, payload, now_us, now):
            pass
        def age_upper(self, sent, prior, now_us, now):
            return prior
    media.ClockRelation = StableClock
    media.run_endpoint = fake_run
    task = asyncio.create_task(media._run_real_media(ws, owner, "full_duplex",
        lambda: asyncio.sleep(0, result=True),
        lambda state, reason, metrics: asyncio.sleep(0, result=states.append((state, reason, metrics))),
        lambda: True, object()))
    feeder = asyncio.create_task(feed())
    try:
        for _ in range(100):
            if any(state == "ACTIVE_SPEAKER_ONLY" for state, _, _ in states):
                break
            await asyncio.sleep(.02)
        else:
            raise AssertionError("speaker-only call never became active")
        endpoint.ready_value = False
        endpoint.unmute_edges += 1
        await asyncio.sleep(.2)
        assert not requests, "unmute dispatched before endpoint readiness"
        endpoint.ready_value = True
        for _ in range(50):
            if requests:
                break
            await asyncio.sleep(.02)
        assert requests == [1], requests
        assert any(reason == "UPGRADE_PENDING" for _, reason, _ in states)
        endpoint.mode, endpoint.mode_generation, endpoint.upgrade_generation = "full_duplex", 2, None
        endpoint.mode_changed()
        await endpoint.capture.put((int(asyncio.get_running_loop().time() * 1_000_000), 0, bytes(1024)))
        for _ in range(50):
            if states and states[-1][0] == "ACTIVE_FULL":
                break
            await endpoint.capture.put((int(asyncio.get_running_loop().time() * 1_000_000), 0, bytes(1024)))
            await asyncio.sleep(.02)
        assert states[-1][0] == "ACTIVE_FULL", ("initial full media was not observed", states[-3:])
        endpoint.mode, endpoint.mode_generation = "speaker_only", 3
        endpoint.mode_changed()
        for _ in range(50):
            if states[-1][0] == "ACTIVE_SPEAKER_ONLY":
                break
            await asyncio.sleep(.02)
        assert states[-1][0] == "ACTIVE_SPEAKER_ONLY"
        endpoint.mode, endpoint.mode_generation = "full_duplex", 4
        endpoint.mode_changed()
        await asyncio.sleep(.15)
        assert states[-1][0] != "ACTIVE_FULL", "old capture or downlink proved new duplex"
        await endpoint.capture.put((int(asyncio.get_running_loop().time() * 1_000_000), 0, bytes(1024)))
        for _ in range(50):
            if states[-1][0] == "ACTIVE_FULL":
                break
            await endpoint.capture.put((int(asyncio.get_running_loop().time() * 1_000_000), 0, bytes(1024)))
            await asyncio.sleep(.02)
        assert states[-1][0] == "ACTIVE_FULL", "fresh upgraded capture and delivery were not observed"
        print("PASS: early unmute waits for health and dispatches exactly once")
        print("PASS: upgraded duplex requires fresh capture and browser delivery")
    finally:
        feeder.cancel()
        task.cancel()
        await asyncio.gather(feeder, task, return_exceptions=True)
        media.run_endpoint = original
        media.ClockRelation = original_clock


asyncio.run(main())

