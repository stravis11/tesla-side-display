"""Private Unix-socket RFB capture and input. Only WebRTC leaves the host."""
import asyncio
import contextlib
import struct

import numpy as np


class RFB:
    def __init__(self):
        self.reader = None
        self.writer = None
        self.pixels = None
        self.updated = asyncio.Event()
        self.task = None
        self.keys = set()
        self.position = (0, 0)
        self.buttons = 0

    async def connect(self, path):
        self.reader, self.writer = await asyncio.open_unix_connection(str(path))
        read = self.reader.readexactly
        version = await read(12)
        if version != b"RFB 003.008\n":
            raise RuntimeError(f"Unsupported RFB version: {version!r}")
        self.writer.write(version)
        count = (await read(1))[0]
        if count == 0 or 1 not in await read(count):
            raise RuntimeError("Local WayVNC must allow no-auth Unix connections")
        self.writer.write(b"\x01")
        if await read(4) != b"\0" * 4:
            raise RuntimeError("WayVNC handshake failed")
        self.writer.write(b"\x01")  # Shared client.
        header = await read(24)
        width, height = struct.unpack("!HH", header[:4])
        if not 0 < width <= 4096 or not 0 < height <= 2160:
            raise RuntimeError("Unexpected framebuffer size")
        await read(struct.unpack("!I", header[20:24])[0])
        self.pixels = np.zeros((height, width, 4), dtype=np.uint8)
        # 32-bit little-endian truecolor: B G R padding.
        fmt = struct.pack("!BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self.writer.write(b"\x00\x00\x00\x00" + fmt)
        self.writer.write(struct.pack("!BBHii", 2, 0, 2, 0, -223))
        self.request(False)
        self.task = asyncio.create_task(self.read_updates())
        ready = asyncio.create_task(self.updated.wait())
        try:
            done, _ = await asyncio.wait([ready, self.task], timeout=10,
                                         return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                await self.task
                raise RuntimeError("WayVNC closed before the first frame")
            if ready not in done:
                raise RuntimeError("WayVNC did not produce a frame")
        finally:
            ready.cancel()

    def request(self, incremental=True):
        height, width = self.pixels.shape[:2]
        self.writer.write(struct.pack("!BBHHHH", 3, incremental, 0, 0, width, height))

    async def read_updates(self):
        read = self.reader.readexactly
        while True:
            kind = (await read(1))[0]
            if kind == 0:
                _, count = struct.unpack("!BH", await read(3))
                for _ in range(count):
                    x, y, width, height, encoding = struct.unpack("!HHHHi", await read(12))
                    if encoding == -223:
                        if not 0 < width <= 4096 or not 0 < height <= 2160:
                            raise RuntimeError("Invalid desktop resize")
                        self.pixels = np.zeros((height, width, 4), dtype=np.uint8)
                    elif encoding == 0:
                        if x + width > self.pixels.shape[1] or y + height > self.pixels.shape[0]:
                            raise RuntimeError("Rectangle outside framebuffer")
                        raw = await read(width * height * 4)
                        self.pixels[y:y+height, x:x+width] = np.frombuffer(raw, np.uint8).reshape(height, width, 4)
                    else:
                        raise RuntimeError(f"Unexpected RFB encoding {encoding}")
                self.updated.set()
                self.request()
                await self.writer.drain()
            elif kind == 2:  # Bell.
                continue
            elif kind == 3:  # Ignore the server clipboard; never export it.
                header = await read(7)
                size = struct.unpack("!I", header[3:])[0]
                if size > 1024 * 1024:
                    raise RuntimeError("Clipboard message too large")
                await read(size)
            else:
                raise RuntimeError(f"Unexpected RFB message {kind}")

    def pointer(self, x, y, buttons=0):
        height, width = self.pixels.shape[:2]
        x, y = max(0, min(width - 1, int(x))), max(0, min(height - 1, int(y)))
        self.position, self.buttons = (x, y), buttons & 7
        self.writer.write(struct.pack("!BBHH", 5, buttons & 127, x, y))

    def key(self, keysym, down):
        if down:
            self.keys.add(keysym)
        else:
            self.keys.discard(keysym)
        self.writer.write(struct.pack("!BB2xI", 4, bool(down), keysym))

    def release(self):
        if self.writer and not self.writer.is_closing():
            for key in tuple(self.keys):
                self.key(key, False)
            self.pointer(*self.position, 0)

    async def close(self):
        with contextlib.suppress(ConnectionError, OSError):
            self.release()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
