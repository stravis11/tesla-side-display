import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import ssl
import time

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import candidate_from_sdp
from av import VideoFrame

from .rfb import RFB
from .network import hotspot_status
from .placement import load_position, save_position, next_position

LOG = logging.getLogger("side-display")
ROOT = Path(__file__).resolve().parent.parent


async def hypr(*args):
    process = await asyncio.create_subprocess_exec(
        "hyprctl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
    text = stdout.decode().strip()
    if process.returncode or (args[0] != "-j" and text != "ok"):
        raise RuntimeError(stderr.decode().strip() or text or "Hyprland command failed")
    return json.loads(text) if args[0] == "-j" else text


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


class DisplayTrack(VideoStreamTrack):
    def __init__(self, rfb, fps):
        super().__init__()
        self.rfb, self.fps = rfb, fps
        self.started = time.monotonic()
        self.index = 0

    async def recv(self):
        if self.readyState != "live" or self.rfb.task.done():
            raise MediaStreamError
        target = self.started + self.index / self.fps
        await asyncio.sleep(max(0, target - time.monotonic()))
        self.index = max(self.index + 1, int((time.monotonic() - self.started) * self.fps))
        frame = VideoFrame.from_ndarray(self.rfb.pixels[:, :, :3].copy(), format="bgr24")
        frame.pts = int((time.monotonic() - self.started) * 90000)
        frame.time_base = Fraction(1, 90000)
        return frame


@dataclass
class Session:
    id: str
    slot: int
    name: str
    peer: RTCPeerConnection
    rfb: RFB
    process: object = None
    log_file: object = None
    timer: object = None
    closing: bool = False
    channel: object = None


class Server:
    def __init__(self, runtime, host="0.0.0.0", port=8876, fps=30, max_displays=3,
                 certificate=None, key=None):
        self.runtime = Path(runtime)
        self.host, self.port, self.fps = host, port, fps
        self.max_displays = max_displays
        self.certificate, self.key = certificate, key
        self.token = secrets.token_urlsafe(24)
        self.pairing_code = f"{secrets.randbelow(100000000):08d}"
        self.failed_pairing = deque()
        self.prefix = "SIDE-" + secrets.token_hex(3)
        self.sessions = {}
        self.creation_lock = asyncio.Lock()
        self.stopping = asyncio.Event()
        self.error = ""
        self.runner = None
        self.control = None
        self.watchdog = None

    def record_owned(self):
        atomic_json(self.runtime / "owned.json", {
            "instance": os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""),
            "outputs": [{"name": s.name, "slot": s.slot,
                         "pid": s.process.pid if s.process else None}
                        for s in self.sessions.values()]})

    async def recover(self):
        path = self.runtime / "owned.json"
        if path.exists():
            old = json.loads(path.read_text())
            if old.get("instance") == os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
                for entry in old.get("outputs", []):
                    name = entry if isinstance(entry, str) else entry.get("name", "")
                    # The private journal is the ownership boundary; never scan/remove other outputs.
                    if name.startswith("SIDE-"):
                        if isinstance(entry, dict) and entry.get("pid"):
                            # Pin the process identity before checking its exact private capture arguments.
                            with contextlib.suppress(OSError, ValueError):
                                fd = os.pidfd_open(entry["pid"])
                                try:
                                    argv = Path(f"/proc/{entry['pid']}/cmdline").read_bytes().split(b"\0")
                                    sock = f"unix:{self.runtime}/rfb-{entry['slot']}.sock".encode()
                                    if name.encode() in argv and sock in argv and Path(os.fsdecode(argv[0])).name == "wayvnc":
                                        signal.pidfd_send_signal(fd, signal.SIGTERM)
                                finally:
                                    os.close(fd)
                        try:
                            await hypr("output", "remove", name)
                        except Exception:
                            if any(m["name"] == name for m in await hypr("-j", "monitors", "all")):
                                raise RuntimeError(f"Could not recover {name}; ownership journal preserved")
            path.unlink()

    def urls(self):
        addresses = []
        hotspot = hotspot_status()
        if self.host in ("0.0.0.0", "::") and hotspot.get("active"):
            addresses = [hotspot["address"]]
        elif self.host not in ("0.0.0.0", "::"):
            addresses = [self.host]
        else:
            try:
                result = __import__("subprocess").run(["ip", "-j", "addr"],
                    capture_output=True, text=True, timeout=2, check=True)
                for interface in json.loads(result.stdout):
                    if interface.get("ifname", "").startswith(("tailscale", "tun", "wg", "docker", "br-", "veth")):
                        continue
                    if interface.get("operstate") not in ("UP", "UNKNOWN"):
                        continue
                    for addr in interface.get("addr_info", []):
                        if addr.get("scope") == "global" and ":" not in addr.get("local", "") and not addr.get("tentative") and not addr.get("deprecated"):
                            addresses.append(addr["local"])
            except Exception:
                pass
        addresses = list(dict.fromkeys(addresses + ["127.0.0.1"]))
        scheme = "https" if self.certificate else "http"
        return [f"{scheme}://{('[' + a + ']') if ':' in a else a}:{self.port}/#{self.token}"
                for a in addresses]

    def status(self):
        return {"running": True, "pid": os.getpid(), "transport": "WebRTC", "urls": self.urls(),
                "pairingCode": self.pairing_code,
                "displays": [{"id": s.id, "name": s.name,
                              "state": s.peer.connectionState} for s in self.sessions.values()],
                "maxDisplays": self.max_displays, "fps": self.fps, "error": self.error,
                "position": load_position(),
                "hotspot": hotspot_status()}

    def authenticate(self, request):
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(supplied, self.token):
            now = time.monotonic()
            while self.failed_pairing and self.failed_pairing[0][0] < now - 60:
                self.failed_pairing.popleft()
            remote = request.remote or "unknown"
            if len(self.failed_pairing) >= 20 or sum(ip == remote for _, ip in self.failed_pairing) >= 5:
                raise web.HTTPTooManyRequests(text="Too many incorrect codes. Wait one minute and retry.")
            if not secrets.compare_digest(supplied, self.pairing_code):
                self.failed_pairing.append((now, remote))
                raise web.HTTPUnauthorized(text="Connection code is missing or expired")
        # Do not let another web origin drive signaling with a known token.
        origin = request.headers.get("Origin")
        if origin and origin != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="Cross-origin signaling is disabled")

    async def offer(self, request):
        self.authenticate(request)
        try:
            params = await request.json()
            width, height = params.get("width", 1600), params.get("height", 900)
            if type(width) is not int or type(height) is not int:
                raise ValueError("Display dimensions must be integers")
            if not 640 <= width <= 2560 or not 360 <= height <= 1600 or width % 2 or height % 2:
                raise ValueError("Choose even dimensions from 640×360 to 2560×1600")
            if params.get("type") != "offer" or not isinstance(params.get("sdp"), str):
                raise ValueError("Invalid WebRTC offer")
        except (ValueError, TypeError, AttributeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        async with self.creation_lock:
            if self.stopping.is_set():
                raise web.HTTPServiceUnavailable()
            if len(self.sessions) >= self.max_displays:
                raise web.HTTPConflict(text="All display slots are in use")
            slot = next(n for n in range(1, self.max_displays + 1)
                        if all(s.slot != n for s in self.sessions.values()))
            sid = secrets.token_hex(8)
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            session = Session(sid, slot, f"{self.prefix}-{slot}", peer, RFB())
            self.sessions[sid] = session
            self.record_owned()
            try:
                await self.create_output(session, width, height)
                await peer.setRemoteDescription(RTCSessionDescription(sdp=params["sdp"], type="offer"))
                peer.addTrack(DisplayTrack(session.rfb, self.fps))
                self.setup_events(session)
                await peer.setLocalDescription(await peer.createAnswer())
                session.timer = asyncio.create_task(self.expire(session, 30))
                return web.json_response({"id": sid, "sdp": peer.localDescription.sdp, "type": "answer"})
            except Exception as exc:
                LOG.exception("Could not create display")
                self.error = str(exc)
                await self.close_session(session)
                raise web.HTTPServiceUnavailable(text=str(exc)) from exc

    async def candidate(self, request):
        self.authenticate(request)
        session = self.sessions.get(request.match_info["id"])
        if not session or session.closing:
            raise web.HTTPNotFound(text="Display session has ended")
        try:
            data = await request.json()
            value = data["candidate"]
            candidate = None
            if value is not None:
                text = value["candidate"]
                mid, index = value.get("sdpMid"), value.get("sdpMLineIndex")
                if not isinstance(text, str) or len(text) > 4096 or not text.startswith("candidate:"):
                    raise ValueError("Invalid network candidate")
                if mid is not None and not isinstance(mid, str):
                    raise ValueError("Invalid media identifier")
                if index is not None and (type(index) is not int or not 0 <= index <= 16):
                    raise ValueError("Invalid media index")
                if mid is None and index is None:
                    raise ValueError("Network candidate needs a media identifier")
                candidate = candidate_from_sdp(text.removeprefix("candidate:"))
                candidate.sdpMid, candidate.sdpMLineIndex = mid, index
        except (ValueError, TypeError, KeyError, IndexError, AssertionError, AttributeError) as exc:
            raise web.HTTPBadRequest(text="Invalid network candidate") from exc
        await session.peer.addIceCandidate(candidate)
        return web.json_response({"ok": True})

    async def create_output(self, session, width, height):
        monitors = await hypr("-j", "monitors")
        focused = next((m["name"] for m in monitors if m.get("focused")), None)
        # Automatic physical outputs otherwise move around an explicitly placed virtual output.
        owned = {s.name for s in self.sessions.values()}
        await self.anchor_monitors([m for m in monitors if m["name"] not in owned])
        # Lua configuration is supported by Omarchy's current Hyprland. Older releases use keywords.
        version = await hypr("-j", "version")
        minor = int(version.get("tag", "v0.56").lstrip("v").split(".")[1])
        rule = {"output": session.name, "mode": f"{width}x{height}@{self.fps}",
                "position": next_position(monitors, width, load_position()), "scale": 1}
        if minor >= 55:
            lua = "hl.monitor({" + ",".join(f"{k}={json.dumps(v)}" for k, v in rule.items()) + "})"
            await hypr("eval", lua)
            await hypr("eval", "hl.workspace_rule({workspace=" + json.dumps("name:" + session.name)
                       + ",monitor=" + json.dumps(session.name) + ",default=true})")
        else:
            await hypr("keyword", "monitor", f"{session.name},{width}x{height}@{self.fps},auto,1")
            await hypr("keyword", "workspace", f"name:{session.name},monitor:{session.name},default:true")
        await hypr("output", "create", "headless", session.name)
        for _ in range(30):
            if any(m["name"] == session.name for m in await hypr("-j", "monitors")):
                break
            await asyncio.sleep(.1)
        else:
            raise RuntimeError("Hyprland did not create the virtual monitor")
        if focused:
            if minor >= 55:
                await hypr("dispatch", "hl.dsp.focus({monitor=" + json.dumps(focused) + "})")
            else:
                await hypr("dispatch", "focusmonitor", focused)
        rfb_socket = self.runtime / f"rfb-{session.slot}.sock"
        control_socket = self.runtime / f"wayvnc-{session.slot}.sock"
        rfb_socket.unlink(missing_ok=True)
        control_socket.unlink(missing_ok=True)
        config = self.runtime / "wayvnc.conf"
        config.write_text("enable_auth=false\n")
        config.chmod(0o600)
        session.log_file = (self.runtime / f"wayvnc-{session.slot}.log").open("w")
        # WayVNC has no network listener; raw frames and input remain in a 0700 directory.
        session.process = await asyncio.create_subprocess_exec(
            "wayvnc", "-C", str(config), "-o", session.name, "-f", str(self.fps),
            "-r", "-R", "-S", str(control_socket), f"unix:{rfb_socket}",
            stdout=session.log_file, stderr=session.log_file)
        self.record_owned()
        for _ in range(50):
            if session.process.returncode is not None:
                raise RuntimeError((self.runtime / f"wayvnc-{session.slot}.log").read_text()[-1500:])
            if rfb_socket.exists():
                break
            await asyncio.sleep(.1)
        else:
            raise RuntimeError("WayVNC did not open its private socket")
        await asyncio.wait_for(session.rfb.connect(rfb_socket), 15)

    def setup_events(self, session):
        @session.peer.on("datachannel")
        def channel_open(channel):
            if channel.label != "input" or session.channel is not None:
                channel.close()
                return
            session.channel = channel

            @channel.on("message")
            def input_message(raw):
                if session.closing or not isinstance(raw, str) or len(raw) > 4096:
                    return
                try:
                    data = json.loads(raw)
                    kind = data.get("type")
                    rfb = session.rfb
                    if kind == "pointer":
                        x, y = float(data["x"]), float(data["y"])
                        if not 0 <= x <= 1 or not 0 <= y <= 1:
                            return
                        h, w = rfb.pixels.shape[:2]
                        rfb.pointer(x * (w - 1), y * (h - 1), int(data.get("buttons", 0)) & 7)
                    elif kind == "scroll":
                        delta = max(-10, min(10, int(data.get("delta", 0))))
                        for _ in range(abs(delta)):
                            rfb.pointer(*rfb.position, rfb.buttons | (8 if delta < 0 else 16))
                            rfb.pointer(*rfb.position, rfb.buttons)
                    elif kind == "key":
                        keysym = int(data["keysym"])
                        if 0 < keysym <= 0x0110FFFF:
                            rfb.key(keysym, data.get("down") is True)
                    elif kind == "release":
                        rfb.release()
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    pass

            @channel.on("close")
            def released():
                session.rfb.release()

        @session.peer.on("connectionstatechange")
        async def changed():
            state = session.peer.connectionState
            if state == "connected":
                if session.timer:
                    session.timer.cancel()
                    session.timer = None
            elif state in ("failed", "closed"):
                await self.close_session(session)
            elif state == "disconnected" and session.timer is None:
                session.timer = asyncio.create_task(self.expire(session, 15))

    async def expire(self, session, seconds):
        await asyncio.sleep(seconds)
        await self.close_session(session)

    async def close_session(self, session):
        if session.closing:
            return
        session.closing = True
        if session.timer and session.timer is not asyncio.current_task():
            session.timer.cancel()
        await session.peer.close()
        await session.rfb.close()
        if session.process and session.process.returncode is None:
            session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), 3)
            except asyncio.TimeoutError:
                session.process.kill()
                await session.process.wait()
        if session.log_file:
            session.log_file.close()
        try:
            await hypr("output", "remove", session.name)
        except Exception:
            # Failed creation has nothing to remove; retain the journal if an output still exists.
            with contextlib.suppress(Exception):
                if any(m["name"] == session.name for m in await hypr("-j", "monitors", "all")):
                    self.error = f"Could not remove {session.name}; stop and restart to retry"
                    return
        self.sessions.pop(session.id, None)
        self.record_owned()

    async def remove(self, request):
        self.authenticate(request)
        session = self.sessions.get(request.match_info["id"])
        if session:
            await self.close_session(session)
        return web.json_response({"ok": True})

    async def set_position(self, position):
        if position not in ("left", "right"):
            raise ValueError("Choose left or right")
        async with self.creation_lock:
            monitors = await hypr("-j", "monitors")
            owned = {s.name for s in self.sessions.values()}
            placed = [m for m in monitors if m["name"] not in owned]
            moved = []
            try:
                await self.anchor_monitors(placed)
                expected = {m["name"]: (m["x"], m["y"]) for m in placed}
                for session in sorted(self.sessions.values(), key=lambda s: s.slot):
                    monitor = next((m for m in monitors if m["name"] == session.name), None)
                    if not monitor or session.closing:
                        continue
                    coordinate = next_position(placed, monitor["width"], position)
                    rule = {"output": session.name,
                            "mode": f"{monitor['width']}x{monitor['height']}@{self.fps}",
                            "position": coordinate, "scale": 1}
                    await self.apply_monitor_rule(rule)
                    moved.append(monitor)
                    x, y = map(int, coordinate.split("x"))
                    expected[session.name] = (x, y)
                    placed.append(dict(monitor, x=x, y=y, scale=1))
                await self.wait_placement(expected)
                save_position(position)
            except Exception:
                for monitor in moved:
                    with contextlib.suppress(Exception):
                        await self.apply_monitor_rule({"output": monitor["name"],
                            "mode": f"{monitor['width']}x{monitor['height']}@{self.fps}",
                            "position": f"{monitor['x']}x{monitor['y']}", "scale": 1})
                raise

    async def anchor_monitors(self, monitors):
        for monitor in monitors:
            await self.apply_monitor_rule({"output": monitor["name"],
                "mode": f"{monitor['width']}x{monitor['height']}@{monitor.get('refreshRate', 60)}",
                "position": f"{monitor['x']}x{monitor['y']}",
                "scale": monitor.get("scale", 1), "transform": monitor.get("transform", 0)})

    async def wait_placement(self, expected):
        for _ in range(30):
            actual = {m["name"]: (m["x"], m["y"]) for m in await hypr("-j", "monitors")}
            if all(actual.get(name) == coordinate for name, coordinate in expected.items()):
                return
            await asyncio.sleep(.05)
        raise RuntimeError("The compositor did not apply the display location")

    async def apply_monitor_rule(self, rule):
        version = await hypr("-j", "version")
        minor = int(version.get("tag", "v0.56").lstrip("v").split(".")[1])
        if minor >= 55:
            await hypr("eval", "hl.monitor({" + ",".join(f"{k}={json.dumps(v)}" for k, v in rule.items()) + "})")
        else:
            await hypr("keyword", "monitor", f"{rule['output']},{rule['mode']},{rule['position']},{rule['scale']}")

    async def control_request(self, reader, writer):
        try:
            command = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if command.get("command") == "stop":
                self.stopping.set()
                answer = {"ok": True}
            elif command.get("command") == "disconnect":
                if session := self.sessions.get(command.get("id")):
                    await self.close_session(session)
                answer = self.status()
            elif command.get("command") == "position":
                await self.set_position(command.get("id"))
                answer = self.status()
            else:
                answer = self.status()
            writer.write(json.dumps(answer).encode() + b"\n")
            await writer.drain()
        except Exception as exc:
            LOG.exception("Control request failed")
            writer.write(json.dumps({"error": str(exc)}).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def watch_sessions(self):
        while True:
            await asyncio.sleep(2)
            for session in tuple(self.sessions.values()):
                if session.rfb.task and session.rfb.task.done() and not session.closing:
                    self.error = f"Capture stopped for {session.name}"
                    await self.close_session(session)

    async def run(self):
        await self.recover()
        app = web.Application(client_max_size=128 * 1024)

        async def index(request):
            return web.FileResponse(ROOT / "web" / "index.html", headers={
                "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})

        app.router.add_get("/", index)
        for filename in ("client.js", "style.css"):
            async def asset(request, name=filename):
                return web.FileResponse(ROOT / "web" / name, headers={"Cache-Control": "no-store"})
            app.router.add_get("/" + filename, asset)
        app.router.add_post("/offer", self.offer)
        app.router.add_post("/sessions/{id}/candidate", self.candidate)
        app.router.add_delete("/sessions/{id}", self.remove)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
        await self.runner.setup()
        tls = None
        if self.certificate:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(self.certificate, self.key)
        try:
            await web.TCPSite(self.runner, self.host, self.port, ssl_context=tls).start()
            if self.host == "0.0.0.0":
                # asyncio's IPv6 listener is v6-only, so it coexists with the IPv4 socket.
                # A globally routed IPv6 address avoids RFC1918 IPv4 URLs on Tesla browsers.
                try:
                    await web.TCPSite(self.runner, "::", self.port, ssl_context=tls).start()
                except OSError as exc:
                    LOG.warning("IPv6 listener unavailable: %s", exc)
            control_path = self.runtime / "control.sock"
            control_path.unlink(missing_ok=True)
            self.control = await asyncio.start_unix_server(self.control_request, path=control_path, limit=8192)
            control_path.chmod(0o600)
            self.watchdog = asyncio.create_task(self.watch_sessions())
            LOG.info("WebRTC display server ready on port %s", self.port)
            await self.stopping.wait()
        finally:
            self.stopping.set()
            if self.control:
                self.control.close()
                await self.control.wait_closed()
            if self.watchdog:
                self.watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.watchdog
            # Finish in-flight offers before removing their monitors.
            async with self.creation_lock:
                for session in tuple(self.sessions.values()):
                    await self.close_session(session)
            await self.runner.cleanup()
            (self.runtime / "control.sock").unlink(missing_ok=True)


async def serve(args, runtime):
    server = Server(runtime, args.host, args.port, args.fps, args.max_displays, args.certificate, args.key)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, server.stopping.set)
    await server.run()
