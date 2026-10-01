"""Opt-in real desktop test: connect without an ICE gathering completion signal."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

from playwright.async_api import async_playwright
from side_display.server import Server


async def main():
    with tempfile.TemporaryDirectory(prefix="side-display-trickle-") as directory:
        os.environ["XDG_DATA_HOME"] = str(Path(directory) / "data")
        server = Server(directory, host="127.0.0.1", port=8877)
        task = asyncio.create_task(server.run())
        try:
            for _ in range(100):
                if Path(directory, "control.sock").exists():
                    break
                if task.done():
                    await task
                await asyncio.sleep(.1)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path="/usr/bin/chromium",
                    headless=True, args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
                try:
                    for delayed in (False, True):
                        page = await browser.new_page()
                        if delayed:
                            await page.add_init_script("""(() => {
                              const Native = window.RTCPeerConnection;
                              window.RTCPeerConnection = class extends Native {
                                constructor(config) {
                                  super(config);
                                  Object.defineProperty(this, 'iceGatheringState', {get: () => 'gathering'});
                                  this.addEventListener('icecandidate', event => {
                                    if (event.candidate) setTimeout(() => this.handler?.(event), 1000);
                                  });
                                }
                                set onicecandidate(handler) { this.handler = handler; }
                              };
                            })();""")
                        offers, candidates, errors = [], [], []
                        def requested(req):
                            if req.url.endswith('/offer'):
                                offers.append(json.loads(req.post_data)['sdp'])
                            if req.url.endswith('/candidate'):
                                candidates.append(req.post_data)
                        page.on('request', requested)
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.goto(server.urls()[0])
                        await page.select_option('#resolution', '1280x720')
                        await page.click('#connect')
                        await page.wait_for_function("document.getElementById('video').videoWidth > 0", timeout=25000)
                        await page.wait_for_function("document.getElementById('video').getVideoPlaybackQuality().totalVideoFrames > 5", timeout=10000)
                        assert len(offers) == 1 and 'a=candidate:' not in offers[0], offers
                        for _ in range(30):
                            if candidates:
                                break
                            await asyncio.sleep(.1)
                        assert candidates and not errors, (candidates, errors)
                        assert next(iter(server.sessions.values())).channel.readyState == 'open'
                        await server.set_position('right')
                        await server.set_position('left')
                        before = await page.evaluate("document.getElementById('video').getVideoPlaybackQuality().totalVideoFrames")
                        await page.wait_for_function("count => document.getElementById('video').getVideoPlaybackQuality().totalVideoFrames > count + 5", arg=before, timeout=10000)
                        assert next(iter(server.sessions.values())).peer.connectionState == 'connected'
                        print(json.dumps({'completionWithheld': delayed, 'offerWithoutCandidates': True,
                            'trickledRequests': len(candidates), 'video': '1280x720', 'inputChannel': 'open',
                            'livePlacementChanges': 'right then left, playback continued'}))
                        await page.click('#disconnect')
                        for _ in range(50):
                            if not server.sessions:
                                break
                            await asyncio.sleep(.1)
                        assert not server.sessions
                        await page.close()
                finally:
                    await browser.close()
        finally:
            server.stopping.set()
            await task


if __name__ == '__main__':
    asyncio.run(main())
