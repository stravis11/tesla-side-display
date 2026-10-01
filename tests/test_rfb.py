import asyncio
import struct
import pytest

from side_display.rfb import RFB


async def test_rfb_handshake_frame_and_input(tmp_path):
    received = asyncio.Queue()
    finished = asyncio.Event()

    async def fake(reader, writer):
        try:
            writer.write(b"RFB 003.008\n")
            assert await reader.readexactly(12) == b"RFB 003.008\n"
            writer.write(b"\x01\x01")
            assert await reader.readexactly(1) == b"\x01"
            writer.write(bytes(4))
            assert await reader.readexactly(1) == b"\x01"
            writer.write(struct.pack("!HH", 2, 1) + bytes(16) + struct.pack("!I", 0))
            assert (await reader.readexactly(20))[4:8] == bytes([32, 24, 0, 1])
            assert struct.unpack("!BBHii", await reader.readexactly(12)) == (2, 0, 2, 0, -223)
            assert await reader.readexactly(10) == struct.pack("!BBHHHH", 3, 0, 0, 0, 2, 1)
            writer.write(b"\x00\x00\x00\x01" + struct.pack("!HHHHi", 0, 0, 2, 1, 0)
                         + bytes([3, 2, 1, 0, 6, 5, 4, 0]))
            await reader.readexactly(10)
            await received.put(await reader.readexactly(6))
            await received.put(await reader.readexactly(8))
            await received.put(await reader.readexactly(8))
            await received.put(await reader.readexactly(6))
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    path = tmp_path / "rfb.sock"
    server = await asyncio.start_unix_server(fake, path=path)
    rfb = RFB()
    try:
        await rfb.connect(path)
        assert rfb.pixels.tolist() == [[[3, 2, 1, 0], [6, 5, 4, 0]]]
        rfb.pointer(999, -1, 1)
        rfb.key(0x61, True)
        rfb.release()
        assert await received.get() == struct.pack("!BBHH", 5, 1, 1, 0)
        assert await received.get() == struct.pack("!BB2xI", 4, 1, 0x61)
        assert await received.get() == struct.pack("!BB2xI", 4, 0, 0x61)
        assert await received.get() == struct.pack("!BBHH", 5, 0, 1, 0)
        await asyncio.wait_for(finished.wait(), 2)
    finally:
        await rfb.close()
        server.close()
        await server.wait_closed()
