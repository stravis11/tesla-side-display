#!/usr/bin/python3
"""Privileged, bounded helper for a separate AP interface and its scoped firewall rules."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import string
import subprocess
import sys
import time

INTERFACE = "side-display0"
PROFILE = "Omarchy Side Display"
ADDRESS = "198.18.0.1"
SUBNET = "198.18.0.0/24"  # Benchmark network: outside RFC1918, without taking a public allocation.
DHCP_CONFIG = Path("/etc/NetworkManager/dnsmasq-shared.d/omarchy-side-display.conf")
DHCP_SETTINGS = (
    "# DHCP compatibility for the Side Display AP only.\n"
    f"dhcp-broadcast=tag:{INTERFACE}\n"
    f"dhcp-option=tag:{INTERFACE},option:netmask,255.255.255.0\n"
    f"dhcp-option=tag:{INTERFACE},option:router,{ADDRESS}\n"
    f"dhcp-option=tag:{INTERFACE},option:dns-server,{ADDRESS}\n"
)


def configure_dhcp(enabled):
    if DHCP_CONFIG.is_symlink():
        raise RuntimeError("Unsafe DHCP configuration file")
    if not enabled:
        if DHCP_CONFIG.exists() and DHCP_CONFIG.read_text() == DHCP_SETTINGS:
            DHCP_CONFIG.unlink()
        return
    DHCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(DHCP_CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as output:
        output.write(DHCP_SETTINGS)
    run("dnsmasq", "--test", f"--conf-file={DHCP_CONFIG}")


def run(*args, optional=False, timeout=15):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode and not optional:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"{args[0]} failed")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Run through pkexec")
    uid = int(os.environ["PKEXEC_UID"])
    if uid == 0 or not 1024 <= args.port <= 65535:
        raise RuntimeError("Invalid caller or port")
    user = pwd.getpwuid(uid)
    data = Path(user.pw_dir) / ".local/share/omarchy-side-display"
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if data.is_symlink() or data.stat().st_uid not in (uid, 0):
        raise RuntimeError("Unsafe hotspot data directory")
    path = data / "hotspot.json"
    if path.is_symlink():
        raise RuntimeError("Unsafe hotspot state file")
    state = json.loads(path.read_text()) if path.exists() else {}
    rules = state.get("rules", [])
    created = False

    def save(value):
        # O_NOFOLLOW protects the privileged write from a swapped symlink.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output)
        os.chown(path, uid, user.pw_gid)

    def stop():
        # Only stop the helper's named AP; the upstream station remains connected.
        run("nmcli", "connection", "down", PROFILE, optional=True)
        configure_dhcp(False)
        for rule in rules:
            # Do not execute arbitrary commands from the user-owned state file.
            if rule[:4] == ["allow", "in", "on", INTERFACE] or rule[:5] == ["route", "allow", "in", "on", INTERFACE]:
                run("ufw", "--force", "delete", *rule, optional=True)
        if state.get("ownedInterface"):
            run("iw", "dev", INTERFACE, "del", optional=True)
        state["active"] = False
        save(state)

    if args.action == "stop":
        stop()
        print(json.dumps({"ok": True, "hotspot": {"active": False}}))
        return
    if state.get("active") and Path(f"/sys/class/net/{INTERFACE}").exists():
        print(json.dumps({"ok": True, "hotspot": state}))
        return
    if Path(f"/sys/class/net/{INTERFACE}").exists():
        raise RuntimeError("The Side Display interface already exists; stop the hotspot first")
    iw = run("iw", "dev")
    matches = list(re.finditer(r"Interface (\S+)\n(?P<info>.*?)(?=\n\tInterface |\Z)", iw, re.S))
    station = next((m for m in matches if "type managed" in m["info"] and "ssid " in m["info"]), None)
    if not station:
        raise RuntimeError("Connect the laptop to your phone hotspot before starting the Tesla hotspot")
    upstream = station[1]
    channel_match = re.search(r"channel (\d+) \((\d+) MHz\)", station["info"])
    if not channel_match:
        raise RuntimeError("Could not determine the phone hotspot's Wi-Fi channel")
    channel, frequency = map(int, channel_match.groups())
    if frequency >= 5925:
        raise RuntimeError("Use a 2.4 or 5 GHz phone hotspot; this helper does not configure 6 GHz")
    band = "a" if frequency > 3000 else "bg"
    phy = Path(f"/sys/class/net/{upstream}/phy80211").resolve().name
    capabilities = run("iw", "phy", phy, "info")
    combinations = capabilities.split("valid interface combinations:", 1)[-1]
    if not any("managed" in line and "AP," in line for line in combinations.splitlines()):
        raise RuntimeError("This Wi-Fi adapter does not advertise simultaneous station and AP support")
    if not Path("/usr/bin/dnsmasq").exists():
        raise RuntimeError("Install dnsmasq first: omarchy pkg add dnsmasq")
    for route in json.loads(run("ip", "-j", "-4", "route")):
        if route.get("dst", "default") != "default" and ipaddress.ip_network(route["dst"], strict=False).overlaps(ipaddress.ip_network(SUBNET)):
            raise RuntimeError("The Tesla hotspot subnet is already in use")
    password = state.get("password") or "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    ssid = "Omarchy Side Display"
    low, high = Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()
    rules = [
        ["allow", "in", "on", INTERFACE, "to", "any", "port", "67", "proto", "udp", "comment", "side-display-dhcp"],
        ["allow", "in", "on", INTERFACE, "from", SUBNET, "to", ADDRESS, "port", "53", "comment", "side-display-dns"],
        ["allow", "in", "on", INTERFACE, "from", SUBNET, "to", ADDRESS, "port", str(args.port), "proto", "tcp", "comment", "side-display-http"],
        ["allow", "in", "on", INTERFACE, "from", SUBNET, "to", ADDRESS, "port", f"{low}:{high}", "proto", "udp", "comment", "side-display-webrtc"],
        ["route", "allow", "in", "on", INTERFACE, "out", "on", upstream, "from", SUBNET, "to", "any", "comment", "side-display-internet"]]
    state = {"active": False, "ssid": ssid, "password": password, "address": ADDRESS,
             "interface": INTERFACE, "upstream": upstream, "channel": channel,
             "rules": rules, "ownedInterface": True}
    save(state)
    try:
        profiles = run("nmcli", "-t", "-f", "NAME", "connection", "show").splitlines()
        if PROFILE not in profiles:
            run("nmcli", "connection", "add", "type", "wifi", "con-name", PROFILE,
                "ifname", INTERFACE, "ssid", ssid, "connection.autoconnect", "no")
        run("nmcli", "connection", "modify", PROFILE, "802-11-wireless.mode", "ap",
            "802-11-wireless.band", band, "802-11-wireless.channel", str(channel),
            "802-11-wireless-security.key-mgmt", "wpa-psk", "802-11-wireless-security.psk", password,
            "ipv4.method", "shared", "ipv4.addresses", f"{ADDRESS}/24", "ipv4.never-default", "yes",
            "ipv6.method", "disabled", "connection.autoconnect", "no")
        run("iw", "phy", phy, "interface", "add", INTERFACE, "type", "__ap")
        created = True
        for _ in range(30):
            if run("nmcli", "-g", "GENERAL.DEVICE", "device", "show", INTERFACE, optional=True):
                break
            time.sleep(.1)
        run("nmcli", "device", "set", INTERFACE, "managed", "yes")
        configure_dhcp(True)
        if run("systemctl", "is-active", "ufw", optional=True) == "active":
            for rule in rules:
                run("ufw", *rule)
        run("nmcli", "--wait", "20", "connection", "up", PROFILE, "ifname", INTERFACE, timeout=25)
        current = run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", upstream)
        if not current or current == "--":
            raise RuntimeError("The phone uplink disconnected; hotspot activation was rolled back")
        state["active"] = True
        save(state)
        print(json.dumps({"ok": True, "hotspot": state}))
    except Exception:
        if created:
            stop()
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
