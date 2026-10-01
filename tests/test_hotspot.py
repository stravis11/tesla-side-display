import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import pytest


@pytest.fixture
def helper(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("hotspot_helper", Path(__file__).resolve().parent.parent / "hotspot-helper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.INTERFACE = "test-side-ap0"
    module.DHCP_CONFIG = tmp_path / "dnsmasq-shared.d/omarchy-side-display.conf"
    monkeypatch.setenv("PKEXEC_UID", str(os.getuid()))
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path), pw_gid=os.getgid()))
    monkeypatch.setattr(module.sys, "argv", ["hotspot-helper.py", "start"])
    return module


def test_failed_ap_activation_rolls_back_only_the_new_interface(helper, monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args == ("iw", "dev"):
            return "Interface wlan-test\n\ttype managed\n\tssid Phone\n\tchannel 149 (5745 MHz)"
        if args[:2] == ("iw", "phy") and args[-1] == "info":
            return "valid interface combinations:\n * #{ managed } <= 2, #{ AP, P2P-client } <= 1"
        if args[:4] == ("ip", "-j", "-4", "route"):
            return "[]"
        if args[:2] == ("systemctl", "is-active"):
            return "active"
        if args[:3] == ("nmcli", "-g", "GENERAL.DEVICE"):
            return helper.INTERFACE
        if args[:3] == ("nmcli", "--wait", "20"):
            raise RuntimeError("AP activation failed")
        return ""

    monkeypatch.setattr(helper, "run", run)
    with pytest.raises(RuntimeError, match="AP activation failed"):
        helper.main()
    assert ("iw", "dev", helper.INTERFACE, "del") in calls
    assert ("nmcli", "connection", "down", helper.PROFILE) in calls
    assert not helper.DHCP_CONFIG.exists()
    assert not any(c[:3] == ("nmcli", "device", "disconnect") for c in calls)
    assert not any("wlan-test" in c for c in calls if c[:3] == ("nmcli", "connection", "down"))


def test_preexisting_ap_interface_is_never_removed(helper, monkeypatch):
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: True if str(p) == f"/sys/class/net/{helper.INTERFACE}" else exists(p))
    calls = []
    monkeypatch.setattr(helper, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match="already exists"):
        helper.main()
    assert calls == []
