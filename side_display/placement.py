"""Saved display placement and logical desktop coordinates."""
import json
import os
from pathlib import Path


def settings_path():
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "omarchy-side-display/settings.json"


def load_position():
    try:
        value = json.loads(settings_path().read_text()).get("position", "left")
        return value if value in ("left", "right") else "left"
    except (OSError, ValueError, AttributeError):
        return "left"


def save_position(position):
    if position not in ("left", "right"):
        raise ValueError("Choose left or right")
    path = settings_path()
    settings = json.loads(path.read_text()) if path.exists() else {}
    settings["position"] = position
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings))
    temporary.chmod(0o600)
    temporary.replace(path)


def logical_width(monitor):
    dimension = monitor["height"] if monitor.get("transform", 0) % 2 else monitor["width"]
    return round(dimension / monitor.get("scale", 1))


def next_position(monitors, width, position):
    if not monitors:
        return f"{-width if position == 'left' else 0}x0"
    y = min(m["y"] for m in monitors)
    x = (min(m["x"] for m in monitors) - width if position == "left" else
         max(m["x"] + logical_width(m) for m in monitors))
    return f"{x}x{y}"
