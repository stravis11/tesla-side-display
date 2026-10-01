from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from side_display.placement import load_position, save_position, next_position
from side_display.server import Server


def test_saved_position_defaults_left_and_survives_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert load_position() == "left"
    save_position("right")
    assert load_position() == "right"
    with pytest.raises(ValueError):
        save_position("above")
    assert load_position() == "right"


def test_coordinates_use_scaled_and_rotated_desktop_bounds():
    monitors = [dict(x=0, y=0, width=1920, height=1200, scale=1.25),
                dict(x=1536, y=-100, width=1920, height=1080, scale=1, transform=1)]
    assert next_position(monitors, 1600, "left") == "-1600x-100"
    assert next_position(monitors, 1600, "right") == "2616x-100"


async def test_live_move_packs_displays_and_saves_only_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    server = Server(tmp_path)
    server.sessions = {
        "one": SimpleNamespace(name="SIDE-one", slot=1, closing=False),
        "two": SimpleNamespace(name="SIDE-two", slot=2, closing=False)}
    monitors = [dict(name="eDP-1", x=0, y=0, width=1920, height=1200, scale=1.25),
                dict(name="SIDE-one", x=-1600, y=0, width=1600, height=900, scale=1),
                dict(name="SIDE-two", x=-2880, y=0, width=1280, height=720, scale=1)]
    monkeypatch.setattr("side_display.server.hypr", AsyncMock(return_value=monitors))
    server.apply_monitor_rule = AsyncMock()
    server.wait_placement = AsyncMock()
    await server.set_position("right")
    rules = [call.args[0] for call in server.apply_monitor_rule.await_args_list]
    assert [(r["output"], r["position"]) for r in rules] == [
        ("eDP-1", "0x0"), ("SIDE-one", "1536x0"), ("SIDE-two", "3136x0")]
    assert rules[0]["scale"] == 1.25
    assert load_position() == "right"
    server.apply_monitor_rule = AsyncMock(side_effect=[None, None, RuntimeError("busy"), None])
    with pytest.raises(RuntimeError, match="busy"):
        await server.set_position("left")
    assert load_position() == "right"
    assert server.apply_monitor_rule.await_args_list[-1].args[0]["position"] == "-1600x0"
