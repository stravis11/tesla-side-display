from unittest.mock import AsyncMock
import json
from types import SimpleNamespace
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from side_display.server import Server


def request(server, body=None, token=None, origin=None):
    headers = {"Host": "localhost:8876"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if origin:
        headers["Origin"] = origin
    req = make_mocked_request("POST", "/offer", headers=headers)
    req.json = AsyncMock(return_value=body)
    return req


async def test_missing_and_incorrect_codes_cannot_create_monitors(tmp_path):
    server = Server(tmp_path)
    server.create_output = AsyncMock()
    for code in (None, "wrong-code"):
        with pytest.raises(web.HTTPUnauthorized):
            await server.offer(request(server, token=code))
    server.create_output.assert_not_called()


async def test_foreign_origin_is_rejected(tmp_path):
    server = Server(tmp_path)
    with pytest.raises(web.HTTPForbidden):
        await server.offer(request(server, token=server.token, origin="http://foreign.test"))


@pytest.mark.parametrize("width,height", [(999999, 720), (641, 720), (640, 1), (True, 720), (640, "720")])
async def test_dimensions_are_bounded_before_monitor_creation(tmp_path, width, height):
    server = Server(tmp_path)
    server.create_output = AsyncMock()
    with pytest.raises(web.HTTPBadRequest):
        await server.offer(request(server, {"width": width, "height": height, "sdp": "", "type": "offer"}, server.token))
    server.create_output.assert_not_called()


async def test_capacity_is_enforced_before_monitor_creation(tmp_path):
    server = Server(tmp_path, max_displays=1)
    server.sessions["occupied"] = object()
    server.create_output = AsyncMock()
    with pytest.raises(web.HTTPConflict):
        await server.offer(request(server, {"width": 1280, "height": 720, "sdp": "", "type": "offer"}, server.token))
    server.create_output.assert_not_called()


async def test_failed_crash_recovery_preserves_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test-instance")
    journal = tmp_path / "owned.json"
    journal.write_text(json.dumps({"instance": "test-instance", "outputs": ["SIDE-owned-1"]}))
    calls = []

    async def compositor(*args):
        calls.append(args)
        if args[0] == "output":
            raise RuntimeError("Compositor is busy")
        return [{"name": "SIDE-owned-1"}, {"name": "eDP-1"}]

    monkeypatch.setattr("side_display.server.hypr", compositor)
    with pytest.raises(RuntimeError, match="ownership journal preserved"):
        await Server(tmp_path).recover()
    assert journal.exists()
    assert calls == [("output", "remove", "SIDE-owned-1"), ("-j", "monitors", "all")]


def test_dual_stack_urls_omit_vpn_and_tentative_addresses(tmp_path, monkeypatch):
    monkeypatch.setattr("side_display.server.hotspot_status", lambda: {"active": False})
    interfaces = [
        {"ifname": "wlan0", "operstate": "UP", "addr_info": [
            {"scope": "global", "local": "172.20.10.3"},
            {"scope": "global", "local": "2001:db8::3"},
            {"scope": "link", "local": "fe80::3"},
            {"scope": "global", "local": "2001:db8::4", "tentative": True}]},
        {"ifname": "tailscale0", "operstate": "UNKNOWN", "addr_info": [
            {"scope": "global", "local": "100.67.60.111"}]}]
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(interfaces)))
    server = Server(tmp_path)
    base_urls = [url.split("/#")[0] for url in server.urls()]
    assert base_urls == ["http://172.20.10.3:8876", "http://127.0.0.1:8876"]


def test_active_hotspot_has_a_short_ipv4_link(tmp_path, monkeypatch):
    monkeypatch.setattr("side_display.server.hotspot_status", lambda: {"active": True, "address": "198.18.0.1"})
    server = Server(tmp_path)
    assert server.urls()[0].startswith("http://198.18.0.1:8876/#")


def test_short_pairing_code_and_failed_attempt_limit(tmp_path):
    server = Server(tmp_path)
    assert len(server.pairing_code) == 8 and server.pairing_code.isdigit()
    server.authenticate(request(server, token=server.pairing_code))
    for _ in range(5):
        with pytest.raises(web.HTTPUnauthorized):
            server.authenticate(request(server, token="incorrect"))
    with pytest.raises(web.HTTPTooManyRequests):
        server.authenticate(request(server, token=server.pairing_code))
    # The strong full-link capability still works when short-code attempts are throttled.
    server.authenticate(request(server, token=server.token))


async def test_trickled_candidates_require_authentication_and_valid_media(tmp_path):
    server = Server(tmp_path)
    peer = SimpleNamespace(addIceCandidate=AsyncMock())
    server.sessions["test"] = SimpleNamespace(peer=peer, closing=False)

    def candidate_request(body, token=server.token):
        req = make_mocked_request("POST", "/sessions/test/candidate",
            headers={"Authorization": "Bearer " + token}, match_info={"id": "test"})
        req.json = AsyncMock(return_value=body)
        return req

    with pytest.raises(web.HTTPUnauthorized):
        await server.candidate(candidate_request({"candidate": None}, "wrong"))
    with pytest.raises(web.HTTPBadRequest):
        await server.candidate(candidate_request({"candidate": {"candidate": "candidate:invalid"}}))
    peer.addIceCandidate.assert_not_called()
    await server.candidate(candidate_request({"candidate": {
        "candidate": "candidate:1 1 udp 2130706431 198.18.0.117 45000 typ host",
        "sdpMid": "0", "sdpMLineIndex": 0}}))
    value = peer.addIceCandidate.call_args.args[0]
    assert (value.ip, value.port, value.sdpMid) == ("198.18.0.117", 45000, "0")
    await server.candidate(candidate_request({"candidate": None}))
    peer.addIceCandidate.assert_awaited_with(None)
