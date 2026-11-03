"""Frame-boundary EOF is distinct from truncated wire data."""
import asyncio
from check_real_media import media
import endpoint

async def main():
    for data, expected in [(b'', endpoint.EndpointPeerEof),
                           (b'\x01', endpoint.EndpointEnded),
                           (endpoint.HEADER.pack(1, 4) + b'x', endpoint.EndpointEnded)]:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        obj = object.__new__(endpoint.EndpointClient)
        obj.reader = reader
        try:
            await obj._read_raw()
        except expected as err:
            if data:
                assert not isinstance(err, endpoint.EndpointPeerEof)
                assert str(err) == 'FRAME_TRUNCATED'
        else:
            raise AssertionError('EOF/truncation accepted')
    print('PASS: exact peer EOF only; partial header/body fail closed')

asyncio.run(main())
