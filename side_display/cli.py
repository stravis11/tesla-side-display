"""Dependency-light control client; the WebRTC dependencies load only in serve."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from .placement import load_position, save_position


def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise RuntimeError("XDG_RUNTIME_DIR is required; run inside your desktop session")
    path = Path(base) / "omarchy-side-display"
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError("Unsafe Tesla Side Display runtime directory")
    path.chmod(0o700)
    return path


def request(runtime, command="status", sid=None):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(str(runtime / "control.sock"))
        sock.sendall(json.dumps({"command": command, "id": sid}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            part = sock.recv(4096)
            if not part or len(data) > 65536:
                raise RuntimeError("Invalid control response")
            data += part
        result = json.loads(data)
        if "running" not in result and result.get("error"):
            raise RuntimeError(result["error"])
        return result


def options(parser):
    parser.add_argument("--host", default="0.0.0.0", help="Signaling listen address")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--fps", type=int, choices=(15, 24, 30, 60), default=30)
    parser.add_argument("--max-displays", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--certificate", help="Optional trusted HTTPS certificate PEM")
    parser.add_argument("--key", help="HTTPS private key PEM")


def main():
    parser = argparse.ArgumentParser(description="Tesla Side Display for Omarchy over WebRTC")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "serve"):
        options(sub.add_parser(name))
    for name in ("status", "stop", "doctor"):
        sub.add_parser(name)
    sub.add_parser("disconnect").add_argument("id")
    sub.add_parser("position").add_argument("id", choices=("left", "right"))
    hotspot = sub.add_parser("hotspot")
    hotspot.add_argument("action", choices=("start", "stop", "status"))
    hotspot.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()
    try:
        runtime = runtime_dir()
        if args.command == "hotspot":
            if args.action == "status":
                from .network import hotspot_status
                print(json.dumps({"hotspot": hotspot_status()}))
                return 0
            helper = Path(__file__).resolve().parent.parent / "hotspot-helper.py"
            return subprocess.call(["pkexec", "/usr/bin/python3", str(helper), args.action, "--port", str(args.port)])
        if args.command == "doctor":
            import importlib.util
            result = {"wayvnc": bool(shutil.which("wayvnc")),
                      "hyprctl": bool(shutil.which("hyprctl")),
                      "wayland": bool(os.environ.get("WAYLAND_DISPLAY")),
                      "hyprland": bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))}
            for module in ("aiohttp", "aiortc", "av", "numpy"):
                result[module] = bool(importlib.util.find_spec(module))
            print(json.dumps(result))
            return 0 if all(result.values()) else 1
        if args.command in ("status", "stop", "disconnect", "position"):
            try:
                result = request(runtime, args.command, getattr(args, "id", None))
                if args.command == "stop":
                    # The daemon's lifetime lock is held until monitor cleanup finishes.
                    for _ in range(150):
                        with (runtime / "server.lock").open("a") as lock:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                pass
                        time.sleep(.1)
                    else:
                        raise RuntimeError("Server is still stopping; inspect server.log")
            except (ConnectionRefusedError, FileNotFoundError):
                if args.command == "position":
                    save_position(args.id)
                result = {"running": False, "urls": [], "displays": [], "position": load_position()}
            print(json.dumps(result))
            return 0
        if not 1024 <= args.port <= 65535:
            raise RuntimeError("Port must be between 1024 and 65535")
        if bool(args.certificate) != bool(args.key):
            raise RuntimeError("Supply both --certificate and --key")
        if args.command == "start":
            # Serialize two bar instances/CLI invocations starting at once.
            with (runtime / "start.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    print(json.dumps(request(runtime)))
                    return 0
                except (ConnectionRefusedError, FileNotFoundError):
                    pass
                if not shutil.which("wayvnc"):
                    raise RuntimeError("WayVNC is missing. Run: omarchy pkg add wayvnc")
                flags = ["--host", args.host, "--port", str(args.port), "--fps", str(args.fps),
                         "--max-displays", str(args.max_displays)]
                if args.certificate:
                    flags += ["--certificate", args.certificate, "--key", args.key]
                log_path = runtime / "server.log"
                with log_path.open("w") as log:
                    log_path.chmod(0o600)
                    child = subprocess.Popen([sys.executable, "-m", "side_display.cli", "serve", *flags],
                        cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=log, start_new_session=True)
                for _ in range(150):
                    if child.poll() is not None:
                        raise RuntimeError(log_path.read_text()[-1800:] or "Server failed to start")
                    try:
                        print(json.dumps(request(runtime)))
                        return 0
                    except (ConnectionRefusedError, FileNotFoundError):
                        time.sleep(.1)
                raise RuntimeError("Server startup timed out; inspect server.log")
        if args.command == "serve":
            import logging
            from .server import serve
            logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
            with (runtime / "server.lock").open("w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("A Tesla Side Display server is already running") from None
                asyncio.run(serve(args, runtime))
        return 0
    except (RuntimeError, OSError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
