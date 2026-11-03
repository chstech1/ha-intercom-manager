"""Actual reader/writer: mode change can clear a just-awakened queue."""
import asyncio
import uuid
from check_real_media import media
from endpoint import EndpointClient, EndpointConfig, HEADER, ENVELOPE, STATUS

class Wire:
    def __init__(self):
        self.writes = []
    def write(self, data):
        self.writes.append(data)
    async def drain(self):
        await asyncio.sleep(0)

async def main():
    loop = asyncio.get_running_loop()
    client = EndpointClient(EndpointConfig('localhost', 6054, uuid.UUID(int=1),
                            'x', 'x', 'x', 'x', 'x', 4), b'c' * 16, 1, 'full_duplex')
    client.connection, client.boot = b'a' * 16, b'b' * 16
    client.answered = client.started = True
    client.clock.offset_us, client.clock.uncertainty_us, client.clock.updated = 0, 22000, loop.time()
    client.reader, client.writer = asyncio.StreamReader(), Wire()
    reader, writer = asyncio.create_task(client.reader_loop()), asyncio.create_task(client.writer_loop())
    try:
        await asyncio.sleep(.03)
        now = int(loop.time() * 1e6)
        tail = STATUS.pack(4, 2, 45, 1, 0, 0, 10, 10, now, 10)
        body = ENVELOPE.pack(client.connection, client.boot, 0, client.call, client.owner) + tail
        client.reader.feed_data(HEADER.pack(12, len(body)) + body)
        client.send_audio(now, 0, bytes(1024))
        await asyncio.sleep(.05)
        assert not writer.done() and not client.writer.writes
        assert client.mode == 'speaker_only' and client.room_mic_muted and not client.audio
        client.send_audio(int(loop.time() * 1e6), 0, bytes(1024))
        for _ in range(40):
            if client.writer.writes:
                break
            await asyncio.sleep(.01)
        assert client.writer.writes and not writer.done()
        print('PASS: queue cleared after wake; fresh subsequent audio still flows')
    finally:
        reader.cancel(); writer.cancel()
        await asyncio.gather(reader, writer, return_exceptions=True)

asyncio.run(main())
