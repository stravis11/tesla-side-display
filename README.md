# Tesla Side Display for Omarchy

An independent Omarchy shell plugin inspired by [Side Display](https://sidedisplay.co/).
Extend your Hyprland desktop into a Tesla, tablet, or another browser on the same
Wi-Fi/hotspot. Video travels over **WebRTC**, and touch/mouse/keyboard input travels
over its encrypted data channel. Up to three browsers each receive a separate
virtual monitor. No subscription, cloud account, or client installation.

## Install

Requirements: Omarchy with Quickshell plugins, Hyprland (tested with 0.56.2),
WayVNC 0.10 or later, `uv`, and Python 3.13 (uv can provision it).

```sh
git clone https://github.com/stravis11/tesla-side-display.git
cd tesla-side-display
omarchy pkg add wayvnc uv
./install.sh
```

The installer validates the manifest, creates a locked Python environment under
`~/.local/share/omarchy-side-display/venv`, copies the plugin under
`~/.config/omarchy/plugins/st87.side-display`, backs up `shell.json`, and enables
the widget before Omarchy's monitor widget. It never edits `/usr/share/omarchy`.
The installed copy is independent of this development checkout.

## Use

1. Connect the laptop and receiving device to the same Wi-Fi or personal hotspot.
   The network must permit devices to communicate with one another.
2. Click Tesla Side Display's monitor-and-T icon in the bar, then **Start sharing**.
3. Copy the link for the laptop's Wi-Fi/hotspot address and open it in the other browser.
   For a browser where copying is inconvenient, navigate to the address before `/#`
   and enter the eight-digit connection code shown in the laptop panel.
4. Select a resolution and connect. Drag windows past the selected edge of your desktop.
5. Use **Disconnect** to remove one monitor, or **Stop sharing** to remove all and
   invalidate the connection code. Use the car browser while parked.

The laptop's plugin panel has a **Screen location** control with **Left** and
**Right** buttons. Left is the default. Switching sides moves connected browser
displays immediately while streaming continues. The choice is saved in
`~/.local/share/omarchy-side-display/settings.json` and applies to future sessions. Multiple
browser displays are arranged next to one another on the selected side.

Tap and drag operate the mouse; two-finger vertical dragging scrolls. The toolbar
provides right click, full screen (when the browser permits it), and a text input
for the on-screen keyboard. Physical keyboards work while the video has focus.
Desktop monitor scale is 1; the selected resolution is fixed during a connection.
When a monitor is removed, Hyprland moves its workspaces back to remaining monitors.

## CLI

```sh
./side-display doctor
./side-display start
./side-display status
./side-display position left
./side-display position right
./side-display disconnect SESSION_ID
./side-display stop
# Alternative listen address, frame rate, and display limit:
./side-display start --host 192.168.1.10 --port 8876 --fps 24 --max-displays 2
```

Settings in the bar widget's `shell.json` entry: `host` (default `0.0.0.0`),
`port` (8876), `fps` (15/24/30/60; default 30), and `maxDisplays` (1–3).
Stop and restart sharing to apply changes. Sharing does not start automatically.
The daemon is independent of shell hot reload; disabling the widget alone does not
stop sharing. Run `./uninstall.sh` to stop sharing and disable the widget while
preserving files. Stop before logging out; normal SIGTERM/SIGINT removes monitors.

## Privacy and network behavior

Every server start generates a random 192-bit capability code. The link keeps the
code in its URL fragment; the page removes the fragment from browser history and
authenticates signaling with a bearer header. Anyone with it can create a monitor
and send input to your desktop. The private control socket and raw WayVNC sockets
live under `$XDG_RUNTIME_DIR/omarchy-side-display` in a mode-0700 directory.
WayVNC does not listen on a network port. No clipboard sharing or audio capture.

For manual entry, the panel also shows an eight-digit pairing code that expires
when sharing stops. Incorrect code attempts are limited to five per client and
twenty total per minute. The long capability in copied links remains available.

The default **HTTP** page/signaling is appropriate for a trusted personal network:
WebRTC media and input use DTLS/SRTP encryption, but an attacker on an untrusted
network could intercept the initial signaling or modify the page. For a network
requiring protected signaling, run with a certificate trusted by the receiving browser:

```sh
./side-display start --certificate /path/to/fullchain.pem --key /path/to/key.pem
```

There is no STUN/TURN dependency and no WAN traversal. Firewalls must allow TCP
8876 (or the chosen port) for signaling and WebRTC's ephemeral UDP ports on the
trusted interface. Hotspot client isolation can prevent a connection. Full screen,
browser availability while parked, and WebRTC support depend on device/firmware.
Tesla hardware/firmware compatibility and real Wi-Fi latency require a device test.

### Tesla browser and firewall troubleshooting

Tesla's browser is reported by [Side Display's developer](https://sidedisplay.co/faq)
to block RFC1918 IPv4 addresses, even on the same hotspot. The **Start Tesla hotspot
(IPv4)** button creates a separate laptop Wi-Fi network named **Omarchy Side Display**
with the short address **http://198.18.0.1:8876**. Join this network in the Tesla.
The panel displays the Wi-Fi password and connection code. Install the optional
hotspot dependency with `omarchy pkg add dnsmasq` before using this mode.

The helper uses the benchmark subnet `198.18.0.0/24`, outside RFC1918, and requires
administrator authentication. It keeps the phone Wi-Fi uplink on its existing
station interface and adds a second AP interface on the same channel. Your Wi-Fi
adapter must advertise concurrent station/AP support. The new connection does not
autostart at boot. **Stop Tesla hotspot** removes the AP and its firewall rules;
the laptop's phone connection remains active. The helper rolls back its AP if
activation fails. Some vehicle firmware may impose additional restrictions;
connecting the Tesla is the final compatibility check.

DHCP replies are broadcast for this AP, with explicit subnet mask, gateway, and
DNS settings. The helper installs an interface-scoped configuration in
`/etc/NetworkManager/dnsmasq-shared.d/omarchy-side-display.conf` and removes it
when the hotspot stops or activation rolls back.

```sh
./side-display hotspot start
./side-display hotspot status
./side-display hotspot stop
```

When the Tesla hotspot is active, its short IPv4 URL is shown first. Suggested
addresses omit VPN/tunnel addresses and IPv6 literals. Wildcard sharing still
listens on IPv4 and IPv6 for clients that already have an IPv6 connection link.

Omarchy's UFW firewall also needs inbound rules for the selected TCP signaling
port and WebRTC's UDP ports. Scope rules to your trusted Wi-Fi interface, local
subnet, and laptop destination address. On Linux, aiortc obtains UDP ports from
`/proc/sys/net/ipv4/ip_local_port_range`. The installer does not disable the firewall
or add unrestricted rules. Changing hotspots can change addresses and invalidate
rules scoped to the previous hotspot. A successful page load on the laptop itself
does not test inbound firewall access from another device.

Background status checks do not display “Working…” or disable Start/Stop. The
active icon uses the theme accent; red text indicates a reported error.
The panel displays the connection code separately for browsers where pasting a
full link is inconvenient. Local UI status can be inspected with
`omarchy-shell st87.side-display status`.

## Architecture and current limits

```text
Hyprland headless output → WayVNC (private Unix RFB) → Python framebuffer
                         → PyAV/aiortc WebRTC video → browser
browser touch/keyboard   → WebRTC data channel → local RFB input → Hyprland
Omarchy Quickshell widget → local control socket → display lifecycle
```

RFB is used only for local capture/input; it is never the browser transport.
CPU encoding and local raw-frame copies are the first implementation. Hardware
encoding, adaptive bitrate/resolution, audio, clipboard, and Internet relay are
not implemented. This project does not claim Side Display's advertised ~100 ms
latency. The negotiated codec is a browser/aiortc-supported codec (VP8 or H.264).
Failed connections expire after 30 seconds; disconnects get a short recovery
window. The local ownership journal permits cleanup after restarting a crashed
daemon, without touching monitors created by other tools.

## Development

```sh
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/omarchy-side-display/dev-venv"
uv sync --python 3.13
uv run pytest
omarchy plugin validate .
bash -n install.sh uninstall.sh side-display
node --check web/client.js
```

`tests/smoke_browser.py` is an opt-in live Hyprland test: it starts sharing on
loopback, creates real temporary monitors, verifies WebRTC playback and input,
simulates a crash to check ownership recovery, then stops sharing. It requires
Playwright and `/usr/bin/chromium`. Run it with the development environment:

```sh
SIDE_DISPLAY_PYTHON="$UV_PROJECT_ENVIRONMENT/bin/python" \
  "$UV_PROJECT_ENVIRONMENT/bin/python" tests/smoke_browser.py
```

Re-run `./install.sh` to update the installed copy after editing this checkout.

Verified on the development machine: three simultaneous 1280×720 WebRTC displays
in Chromium, fourth-client rejection, physical keyboard and emulated touch with
on-screen text entry, per-display removal, full stop cleanup, and daemon crash
recovery. The smoke result and screenshot are saved under `artifacts/`.

References: [Hyprland output control](https://wiki.hypr.land/Configuring/Using-hyprctl/),
[WayVNC](https://github.com/any1/wayvnc), and [aiortc](https://aiortc.readthedocs.io/).
