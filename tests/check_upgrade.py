"""Offline authenticated-frame sequencing checks; no device or network access."""
import asyncio
import importlib.util
from pathlib import Path
import struct
import sys
import uuid

path = Path(__file__).parent.parent / "custom_components/intercom_m3_poc/endpoint.py"
spec = importlib.util.spec_from_file_location("upgrade_endpoint", path)
e = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = e
spec.loader.exec_module(e)


async def main():
    loop = asyncio.get_running_loop()
    config = e.EndpointConfig("localhost", 6054, uuid.UUID(int=1), "x", "x", "x", "x", "x", 4)
    async def authorize():
        return True
    client = e.EndpointClient(config, b"c" * 16, 1, "full_duplex", renewal_authorize=authorize)
    client.connection, client.boot, client.answered = b"a" * 16, b"b" * 16, True
    client.capabilities = 4
    client.clock.offset_us, client.clock.uncertainty_us, client.clock.updated = 123000000, 1000, loop.time()
    client.reader = asyncio.StreamReader()
    reader = asyncio.create_task(client.reader_loop())

    async def frame(kind, tail):
        sequence = client.rx_sequence
        body = e.ENVELOPE.pack(client.connection, client.boot, sequence, client.call, client.owner) + tail
        client.reader.feed_data(e.HEADER.pack(kind, len(body)) + body)
        for _ in range(100):
            await asyncio.sleep(.001)
            if reader.done():
                raise reader.exception()
            if client.rx_sequence > sequence:
                return
        raise AssertionError("frame not processed")

    async def status(state, mode, health, generation):
        report = max(client.last_status_report_us + 1, int(loop.time() * 1000000 + client.clock.offset_us))
        await frame(12, e.STATUS.pack(state, mode, health, generation, 0, 0, 10, 10, report, 10))

    try:
        client.capture.put_nowait((1, 0, bytes(1024)))
        await status(4, 2, 45, 1)
        assert client.room_mic_muted and client.capture.empty() and client.mode_generation == 1
        await status(4, 2, 13, 1)
        assert not client.room_mic_muted and client.unmute_edges == 1
        await status(4, 2, 13, 1)
        assert client.unmute_edges == 1
        await client.request_duplex(1)
        kind, payload, _, _ = client.control.get_nowait()
        assert (kind, payload) == (14, struct.pack("<I", 1))
        try:
            await client.request_duplex(1)
            raise AssertionError("duplicate accepted")
        except e.EndpointEnded as error:
            assert str(error) == "UPGRADE_BUSY"
        await status(5, 2, 13, 1)
        assert client.mode == "speaker_only"
        await frame(15, struct.pack("<IBH", 2, 0, 31))
        assert (client.mode, client.mode_generation, client.upgrade_result) == ("full_duplex", 2, "UPGRADE_ACCEPTED")
        await status(4, 2, 45, 3)
        await status(4, 2, 13, 3)
        await client.request_duplex(3)
        await status(4, 2, 45, 4)
        assert client.mode == "speaker_only" and client.upgrade_generation is None
        try:
            await frame(15, struct.pack("<IBH", 4, 0, 31))
            raise AssertionError("late result accepted")
        except e.EndpointEnded as error:
            assert str(error) == "UPGRADE_RESULT_INVALID"
        entered, release = asyncio.Event(), asyncio.Event()
        policy = {"allowed": True}
        async def delayed_authorize():
            entered.set()
            await release.wait()
            return policy["allowed"]
        queued = e.EndpointClient(config, b"d" * 16, 2, "speaker_only", 1, delayed_authorize)
        queued.connection, queued.boot, queued.answered = b"a" * 16, b"b" * 16, True
        queued.capabilities, queued.status, queued.status_health = 4, 4, 13
        queued.clock.updated = queued.last_sink = queued.last_status_received = loop.time()
        class Wire:
            def __init__(self):
                self.frames = []
            def write(self, data):
                self.frames.append(data)
            async def drain(self):
                pass
        queued.writer = Wire()
        await queued.request_duplex(1)
        writer = asyncio.create_task(queued.writer_loop())
        await asyncio.wait_for(entered.wait(), 1)
        policy["allowed"] = False  # DND, ACL or owner revoked while authorization awaited.
        release.set()
        result = (await asyncio.gather(writer, return_exceptions=True))[0]
        assert isinstance(result, e.EndpointEnded) and str(result) == "AUTH_REVOKED"
        assert not queued.writer.frames, "upgrade sent after authority was revoked"
        for change in ("mute", "health", "generation"):
            entered, release = asyncio.Event(), asyncio.Event()
            async def allow_after_wait():
                entered.set()
                await release.wait()
                return True
            queued = e.EndpointClient(config, b"d" * 16, 2, "speaker_only", 1, allow_after_wait)
            queued.connection, queued.boot, queued.answered = b"a" * 16, b"b" * 16, True
            queued.capabilities, queued.status, queued.status_health = 4, 4, 13
            queued.clock.updated = queued.last_sink = queued.last_status_received = loop.time()
            queued.writer = Wire()
            await queued.request_duplex(1)
            await queued.send_control(4, e.PING.pack(1, 2))
            writer = asyncio.create_task(queued.writer_loop())
            await asyncio.wait_for(entered.wait(), 1)
            if change == "mute":
                queued.room_mic_muted = True
            elif change == "health":
                queued.last_sink = 0
            else:
                queued.mode_generation = 2
            release.set()
            for _ in range(100):
                if queued.writer.frames:
                    break
                await asyncio.sleep(.001)
            assert queued.upgrade_generation is None and queued.upgrade_result == "UPGRADE_FAILED", change
            assert not writer.done() and len(queued.writer.frames) == 1, change
            raw = queued.writer.frames[0]
            assert e.HEADER.unpack_from(raw)[0] == 4, change
            assert e.ENVELOPE.unpack_from(raw, e.HEADER.size)[2] == 0, change
            await asyncio.wait_for(queued.control.join(), 1)
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        print("PASS: physical edge, duplicate request, indication, authenticated result and mute/late-result rejection")
        print("PASS: queued upgrade is not written after policy changes during authorization")
        print("PASS: queued remute, health loss and generation change preserve writer and wire sequence")
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


asyncio.run(main())


