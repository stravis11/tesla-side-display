import fcntl
import json
import os
from pathlib import Path
import socket
import struct


def hotspot_status():
    path = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "omarchy-side-display/hotspot.json"
    try:
        state = json.loads(path.read_text())
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            address = fcntl.ioctl(sock, 0x8915, struct.pack("256s", b"side-display0"))[20:24]
        active = state.get("active") is True and socket.inet_ntoa(address) == "198.18.0.1"
        return {key: state.get(key) for key in ("ssid", "password", "address")} | {"active": active}
    except (OSError, ValueError, KeyError):
        return {"active": False}
