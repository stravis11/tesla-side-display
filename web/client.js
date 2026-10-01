const $ = id => document.getElementById(id);
let peer, channel, session, connectionTimer, connecting = false, buttons = 0;
let token = decodeURIComponent(location.hash.slice(1));
$("token").value = token;
// The code stays out of requests, referrers, and browser history after the page loads.
history.replaceState(null, "", location.pathname);
const video = $("video");
const namedKeys = {
  Backspace: 0xff08, Tab: 0xff09, Enter: 0xff0d, Escape: 0xff1b,
  Delete: 0xffff, Insert: 0xff63, Home: 0xff50, End: 0xff57,
  PageUp: 0xff55, PageDown: 0xff56, ArrowLeft: 0xff51, ArrowUp: 0xff52,
  ArrowRight: 0xff53, ArrowDown: 0xff54, Shift: 0xffe1, Control: 0xffe3,
  Alt: 0xffe9, Meta: 0xffeb, CapsLock: 0xffe5,
};
for (let i = 1; i <= 12; i++) namedKeys[`F${i}`] = 0xffbd + i;
const heldKeys = new Map();
function keysym(key) {
  if (namedKeys[key]) return namedKeys[key];
  if ([...key].length !== 1) return null;
  const code = key.codePointAt(0);
  return code <= 255 ? code : 0x01000000 + code;
}
function send(data) {
  if (channel?.readyState === "open" && channel.bufferedAmount < 65536)
    channel.send(JSON.stringify(data));
}
function tapKey(key) {
  send({ type: "key", keysym: key, down: true });
  send({ type: "key", keysym: key, down: false });
}
function release() {
  buttons = 0; heldKeys.clear(); send({ type: "release" });
}
async function api(path, method, body) {
  const response = await fetch(path, {
    method, headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}
async function disconnect(message = "Display disconnected.") {
  clearTimeout(connectionTimer);
  release();
  const old = session;
  session = null;
  if (peer) { peer.onconnectionstatechange = null; peer.onicecandidate = null; peer.close(); peer = null; }
  channel = null; video.srcObject = null;
  $("display").hidden = true; $("setup").hidden = false;
  $("status").textContent = message;
  if (old) { try { await api(`/sessions/${old}`, "DELETE"); } catch (_) {} }
}
$("connect-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (connecting) return;
  if (typeof RTCPeerConnection === "undefined") {
    $("status").textContent = "This browser does not support WebRTC. Try an updated browser.";
    return;
  }
  connecting = true; $("connect").disabled = true;
  $("status").textContent = "Creating your display…";
  token = $("token").value.trim();
  const [width, height] = $("resolution").value.split("x").map(Number);
  try {
    const pc = peer = new RTCPeerConnection({ iceServers: [] });
    let candidateSession, sending = Promise.resolve();
    const candidates = [];
    function flushCandidates() {
      if (!candidateSession) return;
      while (candidates.length) {
        const candidate = candidates.shift();
        sending = sending.then(() => {
          if (peer === pc) return api(`/sessions/${candidateSession}/candidate`, "POST", { candidate });
        });
      }
      sending = sending.catch(error => {
        if (peer === pc) return disconnect(error.message || "Could not exchange network addresses.");
      });
    }
    pc.onicecandidate = event => {
      candidates.push(event.candidate ? event.candidate.toJSON() : null);
      flushCandidates();
    };
    pc.addTransceiver("video", { direction: "recvonly" });
    channel = pc.createDataChannel("input", { ordered: true });
    pc.ontrack = event => {
      video.srcObject = new MediaStream([event.track]);
      video.play().catch(() => { $("connection-state").textContent = "Tap screen to play"; });
    };
    pc.onconnectionstatechange = () => {
      $("connection-state").textContent = `WebRTC · ${pc.connectionState}`;
      if (pc.connectionState === "connected") clearTimeout(connectionTimer);
      if (pc.connectionState === "failed") disconnect("Connection failed. Check Wi-Fi, hotspot isolation, and firewall settings.");
    };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    // Send the offer immediately; candidates follow through authenticated signaling.
    const answer = await api("/offer", "POST", { sdp: offer.sdp, type: "offer", width, height });
    session = answer.id;
    candidateSession = session;
    await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
    flushCandidates();
    connectionTimer = setTimeout(() => {
      if (peer === pc && pc.connectionState !== "connected")
        disconnect(`Video connection timed out (network: ${pc.iceConnectionState}, discovery: ${pc.iceGatheringState}).`);
    }, 30000);
    $("setup").hidden = true; $("display").hidden = false;
    video.focus();
  } catch (error) {
    await disconnect(error.message || "Could not connect.");
  } finally {
    connecting = false; $("connect").disabled = false;
  }
});

// Map the displayed video rectangle, excluding object-fit letterboxing.
function point(event) {
  const rect = video.getBoundingClientRect();
  const scale = Math.min(rect.width / video.videoWidth, rect.height / video.videoHeight);
  const width = video.videoWidth * scale, height = video.videoHeight * scale;
  const x = (event.clientX - rect.left - (rect.width - width) / 2) / width;
  const y = (event.clientY - rect.top - (rect.height - height) / 2) / height;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  return { x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y)) };
}
function pointer(event, mask = buttons) {
  const p = point(event);
  if (p) send({ type: "pointer", ...p, buttons: mask });
}
const touchPoints = new Map();
let touchY = null;
video.addEventListener("pointerdown", event => {
  event.preventDefault(); video.focus(); video.play().catch(() => {});
  video.setPointerCapture(event.pointerId);
  if (event.pointerType === "touch") {
    touchPoints.set(event.pointerId, event.clientY);
    if (touchPoints.size >= 2) { buttons = 0; pointer(event, 0); touchY = event.clientY; return; }
  }
  buttons = event.pointerType === "touch" ? 1 : ((event.buttons & 1) | ((event.buttons & 2) << 1) | ((event.buttons & 4) >> 1));
  pointer(event);
});
video.addEventListener("pointermove", event => {
  if (event.pointerType === "touch" && touchPoints.size >= 2) {
    const dy = event.clientY - touchY;
    if (Math.abs(dy) >= 12) { send({ type: "scroll", delta: dy > 0 ? -1 : 1 }); touchY = event.clientY; }
  } else pointer(event);
});
video.addEventListener("pointerup", event => {
  buttons = 0; pointer(event, 0); touchPoints.delete(event.pointerId); touchY = null;
});
video.addEventListener("pointercancel", () => { touchPoints.clear(); release(); });
video.addEventListener("lostpointercapture", () => { if (buttons) release(); });
video.addEventListener("contextmenu", event => event.preventDefault());
video.addEventListener("wheel", event => {
  event.preventDefault(); pointer(event);
  if (event.deltaY) send({ type: "scroll", delta: event.deltaY > 0 ? 1 : -1 });
}, { passive: false });
video.addEventListener("keydown", event => {
  const sym = keysym(event.key);
  if (!sym) return;
  event.preventDefault();
  heldKeys.set(event.code, sym);
  send({ type: "key", keysym: sym, down: true });
});
video.addEventListener("keyup", event => {
  const sym = heldKeys.get(event.code) || keysym(event.key);
  if (!sym) return;
  event.preventDefault(); heldKeys.delete(event.code);
  send({ type: "key", keysym: sym, down: false });
});
video.addEventListener("blur", release);
window.addEventListener("blur", release);
document.addEventListener("visibilitychange", () => { if (document.hidden) release(); });
$("disconnect").onclick = () => disconnect();
$("fullscreen").onclick = async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await $("display").requestFullscreen();
  } catch (_) { $("connection-state").textContent = "Full screen unavailable in this browser"; }
};
$("right-click").onclick = () => {
  // Right click at the most recent pointer position.
  const pos = lastPoint || { x: .5, y: .5 };
  send({ type: "pointer", ...pos, buttons: 4 });
  send({ type: "pointer", ...pos, buttons: 0 });
};
let lastPoint;
video.addEventListener("pointermove", event => { lastPoint = point(event); });
video.addEventListener("pointerdown", event => { lastPoint = point(event); });
$("keyboard-toggle").onclick = () => {
  $("typing").hidden = !$("typing").hidden;
  if (!$("typing").hidden) $("text-input").focus(); else video.focus();
};
$("typing").addEventListener("submit", event => {
  event.preventDefault();
  for (const character of $("text-input").value.slice(0, 1000)) tapKey(keysym(character));
  $("text-input").value = "";
});
$("enter-key").onclick = () => tapKey(namedKeys.Enter);
$("backspace-key").onclick = () => tapKey(namedKeys.Backspace);
$("escape-key").onclick = () => tapKey(namedKeys.Escape);
window.addEventListener("pagehide", () => {
  release();
  if (session) fetch(`/sessions/${session}`, { method: "DELETE", keepalive: true,
    headers: { Authorization: `Bearer ${token}` } }).catch(() => {});
  peer?.close();
});
