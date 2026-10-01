"""Opt-in real Hyprland/WebRTC smoke test. Run when no sharing session is active."""
import asyncio
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"


def cli(*args):
    return json.loads(subprocess.check_output([str(ROOT / "side-display"), *args], text=True))


async def main():
    if cli("status")["running"]:
        raise RuntimeError("Stop the existing sharing session before running this smoke test")
    ARTIFACTS.mkdir(exist_ok=True)
    before = json.loads(subprocess.check_output(["hyprctl", "-j", "monitors"], text=True))
    surface = None
    result = {}
    start = cli("start", "--host", "127.0.0.1")
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path="/usr/bin/chromium", headless=True,
                args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
            context = await browser.new_context(viewport={"width": 1280, "height": 900}, has_touch=True)
            pages = []
            errors = []
            for index in range(3):
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(start["urls"][0])
                await page.select_option("#resolution", "1280x720")
                await page.click("#connect")
                await page.wait_for_function("document.getElementById('video').videoWidth > 0", timeout=45000)
                await page.wait_for_function("document.getElementById('connection-state').textContent.includes('connected')", timeout=20000)
                pages.append(page)
            state = cli("status")
            assert len(state["displays"]) == 3, state
            result["threeDisplays"] = [s["name"] for s in state["displays"]]
            fourth = await context.new_page()
            await fourth.goto(start["urls"][0])
            await fourth.click("#connect")
            await fourth.wait_for_function("document.getElementById('status').textContent.includes('All display slots')", timeout=20000)
            result["fourthRejected"] = True
            env = dict(os.environ, SIDE_DISPLAY_TEST_MONITOR=state["displays"][0]["name"])
            with (ARTIFACTS / "input-surface.log").open("w") as output:
                surface = subprocess.Popen(["qs", "-p", str(ROOT / "tests/input-surface.qml"), "--no-color"],
                                           env=env, stdout=output, stderr=output)
            await asyncio.sleep(2)
            page = pages[0]
            await page.click("#video", position={"x": 600, "y": 350})
            await page.keyboard.type("webrtc", delay=100)
            for _ in range(30):
                if any("webrtc" in line for line in (ARTIFACTS / "input-surface.log").read_text().splitlines() if "TEST_TEXT=" in line):
                    break
                await asyncio.sleep(.2)
            else:
                raise AssertionError("Remote keyboard input did not reach the virtual monitor")
            result["keyboardInput"] = True
            await page.touchscreen.tap(600, 350)
            await page.click("#keyboard-toggle")
            await page.fill("#text-input", "-touch")
            await page.locator("#typing button[type=submit]").click()
            for _ in range(30):
                if any("-touch" in line for line in (ARTIFACTS / "input-surface.log").read_text().splitlines() if "TEST_TEXT=" in line):
                    break
                await asyncio.sleep(.2)
            else:
                raise AssertionError("Touch/onscreen keyboard input did not reach the desktop")
            result["touchInput"] = True
            await page.screenshot(path=str(ARTIFACTS / "webrtc-display.png"))
            stats = await page.evaluate("""async () => {
              const v = document.getElementById('video');
              return {width: v.videoWidth, height: v.videoHeight, frames: v.getVideoPlaybackQuality().totalVideoFrames};
            }""")
            assert stats["frames"] > 5, stats
            result["playback"] = stats
            surface.terminate()
            surface.wait(timeout=5)
            surface = None
            await pages[2].click("#disconnect")
            await asyncio.sleep(1)
            assert len(cli("status")["displays"]) == 2
            result["disconnectCleanup"] = True
            assert not errors, errors
            result["browserErrors"] = errors
            # Crash only this test-owned server and check its private ownership journal.
            os.kill(cli("status")["pid"], signal.SIGKILL)
            await asyncio.sleep(.3)
            assert not cli("status")["running"]
            cli("start", "--host", "127.0.0.1")
            recovered = json.loads(subprocess.check_output(["hyprctl", "-j", "monitors"], text=True))
            assert {m["name"] for m in recovered} == {m["name"] for m in before}
            result["crashRecovery"] = True
            await browser.close()
    finally:
        if surface:
            surface.terminate()
            surface.wait(timeout=5)
        cli("stop")
        after = json.loads(subprocess.check_output(["hyprctl", "-j", "monitors"], text=True))
        assert {m["name"] for m in after} == {m["name"] for m in before}, after
        result["stopCleanup"] = True
        (ARTIFACTS / "smoke-results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
