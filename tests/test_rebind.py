"""Offline peer-EOF recovery against the real pool and media coordinator."""
import asyncio
from collections import deque
from types import SimpleNamespace
from check_real_media import FakeWS, FakeEndpoint, Principal, media, AUDIO, encode_frame
from session import decode_frame
import endpoint as native


async def main(scenario):
    loop = asyncio.get_running_loop()
    original_clock = media.ClockRelation
    clock_ok = [True]
    browser_clocks = []
    class StableClock(original_clock):
        def __init__(self):
            super().__init__()
            browser_clocks.append(self)
        def fresh(self, now):
            return clock_ok[0]
        def age_upper(self, sent, prior, now_us, now):
            return prior
    media.ClockRelation = StableClock
    ws, events, clients = FakeWS(), [], []
    owner = SimpleNamespace(call_id=b'x' * 16, generation=7, mode_generation=0,
                            initial_mode='full_duplex', mode='full_duplex', attached=True,
                            caller_tx_enabled=True, principal=Principal())
    config = SimpleNamespace(endpoint_profile=4, expected_uuid='room')
    pool = native.EndpointPool(source_handler=lambda *args: None)
    pool.configurations['room'] = config
    allow = True

    class Client(FakeEndpoint):
        def __init__(self):
            super().__init__()
            self.config = config
            self.mode = 'full_duplex'
            self.status = 3
            self.ready_value = True
            self.closed = False
            self.ending = False
            self.answered = False
            self.last_status_received = loop.time()
            self.status_health = 31
            if scenario == 'muted' and clients:
                self.status_health |= 32
            self.eof = asyncio.Event()
            self.native_owner = None
        def bind(self, call, token, mode, generation, authorize, policy):
            self.call, self.owner, self.mode, self.mode_generation = call, token, mode, generation
            self.native_owner = token
        async def start(self):
            if scenario == 'busy' and clients and clients[0] is not self:
                raise native.EndpointEnded('ENDPOINT_BUSY_1')
            self.started = True
            self.answered = True
            if scenario == 'second_eof' and clients and clients[0] is not self:
                self.eof.set()
        async def close(self):
            self.closed = True

    async def reader(client):
        await client.eof.wait()
        if scenario == 'clock_error':
            raise native.EndpointEnded('CLOCK_STALE')
        if scenario == 'truncated':
            raise native.EndpointFrameTruncated('BODY', 3)
        raise native.EndpointPeerEof()

    async def sleeper():
        await asyncio.Future()

    async def fake_open(room, cfg):
        existing = pool.entries.get(room)
        if existing and not existing[0].closed:
            return existing[0]
        client = Client()
        clients.append(client)
        tasks = [asyncio.create_task(sleeper()), asyncio.create_task(reader(client)),
                 asyncio.create_task(sleeper())]
        pool.entries[room] = client, tasks
        return client

    async def fake_stop(client, tasks):
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()

    async def on_state(state, reason, metrics):
        nonlocal allow
        events.append((state, reason))
        if reason.startswith('RECOVERY_PAUSE_'):
            if scenario == 'revoked':
                allow = False
            if scenario == 'hangup':
                owner.attached = False
            if scenario in ('legacy', 'revoked', 'hangup'):
                return
            nonce = int(reason.rsplit('_', 1)[-1], 16)
            if scenario == 'wrong_nonce':
                nonce ^= 1
            now = int(loop.time() * 1_000_000)
            await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,
                data=encode_frame(5, media.PING.pack(nonce, now))))

    async def feed():
        sequence = 0
        playback = 0
        while True:
            now = loop.time()
            await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,
                data=encode_frame(4, media.READY.pack(15, 48_000, 0, 0))))
            await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,
                data=encode_frame(3, AUDIO.pack(sequence, int(now * 1_000_000), 0, 512, bytes(1024)))))
            sequence += 1
            for client in clients:
                client.last_sink = now
                client.status_rx_sequence += 1
                if client.started and not client.closed:
                    if not client.capture.full():
                        client.capture.put_nowait((int(now * 1_000_000), 0, bytes(1024)))
            if getattr(owner, 'recovery_epoch', None) and scenario not in ('stale_playback',):
                playback += 1
                owner.recovery_playback = (1 if scenario == 'frozen_playback' else playback, now)
            await asyncio.sleep(.04)

    original_open, original_stop = pool._open, native._stop_transport
    pool._open, native._stop_transport = fake_open, fake_stop
    feeder = asyncio.create_task(feed())
    task = asyncio.create_task(media._run_real_media(ws, owner, 'full_duplex',
        lambda: asyncio.sleep(0, result=allow), on_state, lambda: owner.attached, config, pool))
    try:
        for _ in range(200):
            if clients and clients[0].started and clients[0].mode_changed:
                break
            await asyncio.sleep(.01)
        assert clients and clients[0].started
        first = clients[0]
        first.room_mic_muted = True
        first.mode = 'speaker_only'
        first.mode_generation = 1
        first.mode_changed()
        first.eof.set()
        for _ in range(500):
            if any(reason.startswith('RECOVERY_READY_') for _, reason in events):
                break
            if task.done():
                break
            await asyncio.sleep(.01)
        if scenario in ('success', 'two_incidents', 'two_incidents_softmute', 'stable_no_edge', 'stale_playback', 'frozen_playback', 'early_mute', 'interrupt_clock', 'interrupt_mute', 'interrupt_auth', 'interrupt_dnd', 'interrupt_hangup'):
            assert any(reason.startswith('RECOVERY_READY_') for _, reason in events), events
            assert len(clients) >= 2 and clients[1].native_owner not in (None, owner.generation)
            assert clients[1].mode_generation == 0 and owner.generation == 7 and owner.call_id == b'x' * 16
            assert not task.done() and not ws.closed
            if scenario == 'success':
                await asyncio.sleep(12.2)
                assert not task.done() and not ws.closed, 'setup deadline limited the active resumed call'
            else:
                if scenario == 'two_incidents_softmute':
                    owner.caller_tx_enabled = False
                if scenario.startswith('interrupt_'):
                    await asyncio.sleep(1.0)
                    if scenario in ('interrupt_auth', 'interrupt_dnd'):
                        allow = False
                    elif scenario == 'interrupt_hangup':
                        owner.attached = False
                    elif scenario == 'interrupt_clock':
                        clock_ok[0] = False
                    elif scenario == 'interrupt_mute':
                        clients[1].room_mic_muted = True
                        clients[1].mode = 'speaker_only'
                        clients[1].mode_generation = 1
                        clients[1].mode_changed()
                    await asyncio.sleep(.4)
                    if scenario in ('interrupt_auth', 'interrupt_dnd', 'interrupt_hangup'):
                        await asyncio.wait_for(task, 2)
                        assert sum(reason.startswith('RECOVERY_PAUSE_') for _, reason in events) == 1
                        print('PASS:', scenario)
                        return
                    clock_ok[0] = True
                    if scenario == 'interrupt_mute':
                        clients[1].room_mic_muted = False
                        clients[1].mode = 'full_duplex'
                        clients[1].mode_changed()
                    await asyncio.sleep(.6)
                else:
                    await asyncio.sleep(.6 if scenario == 'early_mute' else 2.6)
                if scenario in ('two_incidents', 'two_incidents_softmute', 'stable_no_edge'):
                    assert ('ACTIVE_FULL', 'RECOVERY_HEALTHY') in events, (scenario, events)
                else:
                    assert ('ACTIVE_FULL', 'RECOVERY_HEALTHY') not in events
                second = clients[1]
                if scenario != 'stable_no_edge':
                    second.room_mic_muted = True
                    second.mode = 'speaker_only'
                    second.mode_generation = 1
                    second.mode_changed()
                second.eof.set()
                if scenario in ('two_incidents', 'two_incidents_softmute'):
                    for _ in range(400):
                        if sum(reason.startswith('RECOVERY_READY_') for _, reason in events) >= 2 or task.done():
                            break
                        await asyncio.sleep(.01)
                    assert sum(reason.startswith('RECOVERY_READY_') for _, reason in events) == 2, events
                    assert len(clients) >= 3 and len({x.native_owner for x in clients[:3]}) == 3
                    assert owner.call_id == b'x'*16 and owner.generation == 7 and not ws.closed
                    assert len(browser_clocks) == 1, 'browser clock was replaced'
                    delivered = [AUDIO.unpack_from(payload)[0] for kind, payload in map(decode_frame, ws.outgoing) if kind == 3]
                    assert delivered and delivered == list(range(len(delivered))), 'browser output sequence reset or gap'
                else:
                    await asyncio.wait_for(task, 2)
                    assert ('CONNECTING', 'RECOVERY_REFUSED_BUDGET') in events
                    assert sum(reason.startswith('RECOVERY_PAUSE_') for _, reason in events) == 1
        else:
            if scenario == 'second_eof':
                await asyncio.wait_for(task, 2)
            assert task.done(), (scenario, events)
            assert ws.closed
            assert not any(reason.startswith('RECOVERY_READY_') for _, reason in events) or scenario == 'second_eof'
            assert len([x for x in events if x[1].startswith('RECOVERY_PAUSE_')]) <= 1
            if scenario == 'clock_error':
                assert not any(reason.startswith('RECOVERY_PAUSE_') for _, reason in events)
            if scenario == 'truncated':
                assert ('CONNECTING', 'NATIVE_EOF_BODY_3') in events
                assert ('CONNECTING', 'RECOVERY_REFUSED_TRUNCATED') in events
                assert not any(reason.startswith('RECOVERY_PAUSE_') for _, reason in events)
            if scenario == 'second_eof':
                assert ('CONNECTING', 'RECOVERY_REFUSED_BUDGET') in events
            if scenario in ('legacy', 'revoked', 'hangup', 'wrong_nonce'):
                assert not any(client.started for client in clients[1:])
            if scenario == 'muted':
                assert not clients[1].started
        print('PASS:', scenario)
    finally:
        owner.attached = False
        task.cancel()
        feeder.cancel()
        await asyncio.gather(task, feeder, return_exceptions=True)
        for client, tasks in list(pool.entries.values()):
            await fake_stop(client, tasks)
        pool._open, native._stop_transport = original_open, original_stop
        media.ClockRelation = original_clock


async def all_cases():
    for scenario in ('success', 'two_incidents', 'two_incidents_softmute', 'stable_no_edge',
                     'stale_playback', 'frozen_playback', 'early_mute', 'interrupt_clock', 'interrupt_mute', 'interrupt_auth', 'interrupt_dnd', 'interrupt_hangup', 'legacy', 'wrong_nonce', 'revoked', 'hangup', 'muted', 'busy',
                     'second_eof', 'clock_error', 'truncated'):
        await main(scenario)

if __name__ == '__main__':
    asyncio.run(all_cases())
