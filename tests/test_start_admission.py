"""Actual endpoint writer rechecks recovery admission after authorization waits."""
import asyncio
import uuid
import check_real_media  # Adds the exact candidate component to sys.path.
from endpoint import EndpointClient, EndpointConfig, EndpointEnded

class Wire:
    def __init__(self):
        self.writes = []
    def write(self, data):
        self.writes.append(data)
    async def drain(self):
        await asyncio.sleep(0)

async def case(kind):
    loop = asyncio.get_running_loop()
    entered, resume = asyncio.Event(), asyncio.Event()
    allowed = True
    async def authorize():
        entered.set()
        await resume.wait()
        return allowed
    client = EndpointClient(EndpointConfig('localhost', 6054, uuid.UUID(int=1),
                            'x', 'x', 'x', 'x', 'x', 4), b'c' * 16, 42,
                            'full_duplex', 0, authorize)
    client.connection, client.boot, client.writer = b'a' * 16, b'b' * 16, Wire()
    client.clock.updated = loop.time()
    client.last_status_received, client.status_health = loop.time(), 31
    client.recovery_admission = True
    await client.send_control(2, b'candidate')
    writer = asyncio.create_task(client.writer_loop())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if kind == 'revoked':
            allowed = False
        elif kind == 'remute':
            client.status_health |= 32
        elif kind == 'dnd':
            client.status_health |= 64
        elif kind == 'hangup':
            client.ending = True
        elif kind == 'clock':
            client.clock.updated = 0
        resume.set()
        try:
            await asyncio.wait_for(writer, 1)
        except EndpointEnded as err:
            assert str(err) in ('AUTH_REVOKED', 'RECOVERY_NOT_READY')
        else:
            raise AssertionError('recovery START was not refused')
        assert not client.writer.writes
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)

async def main():
    for kind in ('revoked', 'remute', 'dnd', 'hangup', 'clock'):
        await case(kind)
    print('PASS: recovery START rechecks revoked, remute, DND, stop and clock after await')

asyncio.run(main())
