#!/usr/bin/env python3
"""Milkdrop renderer for the Odroid C2 Winamp remote.

The Odroid's GPU can't run Milkdrop, so it runs here instead:

  Odroid winamp-server ──ws /audio──▶ this server ──ws /page──▶ headless Chromium
      (audio samples, commands)                               (butterchurn, WebGL 2
                                                               on the GPU, or on
                                                               SwiftShader)
  kiosk <img src=/stream> ◀── MJPEG ── this server ◀── JPEG frames ──┘

Chromium only draws while somebody watches /stream. Everything runs on one
port (8098). There's no login: it's meant for the home network, and all it
offers is a picture of the music.
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from aiohttp import WSMsgType, web

HERE = Path(__file__).parent
WWW = HERE / "www"
PORT = int(os.environ.get("MILKDROP_PORT", "8098"))
BOUNDARY = "milkdropframe"


def log(msg):
    print(msg, flush=True)


def options():
    """App options from Home Assistant (/data/options.json), or defaults."""
    opts = {"width": 640, "height": 400, "fps": 30, "jpeg_quality": 80, "gpu": True}
    try:
        opts.update(json.loads(Path(os.environ.get("OPTIONS_FILE", "/data/options.json")).read_text()))
    except (OSError, ValueError):
        pass
    return opts


class Renderer:
    def __init__(self, opts):
        self.opts = opts
        self.page = None            # the Chromium page's websocket
        self.sources = set()        # Odroid winamp-servers (audio + commands)
        self.viewers = set()        # /stream responses
        self.frame = None           # latest JPEG
        self.frame_event = asyncio.Event()
        self.status = {"type": "status", "presets": [], "preset": "", "fps": 0}
        self.settings = {}          # last settings from the Odroid (cycle, delay, …)
        self.chromium = None
        # GPU first (Intel/AMD through /dev/dri, Vulkan); software (SwiftShader)
        # when the GPU gives no WebGL 2. Software is far slower.
        self.gpu = bool(opts.get("gpu", True))
        self.gl = ""

    # ── the Chromium page ───────────────────────────────────────────────────
    async def page_ws(self, request):
        ws = web.WebSocketResponse(max_msg_size=8 << 20)
        await ws.prepare(request)
        if self.page and not self.page.closed:
            await self.page.close()
        self.page = ws
        log("Render page connected")
        await self.to_page({"type": "config", **self.opts})
        if self.settings:
            await self.to_page({"type": "settings", **self.settings})
        await self.to_page({"type": "run", "on": bool(self.viewers)})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self.frame = msg.data
                    self.frame_event.set()
                    self.frame_event = asyncio.Event()
                elif msg.type == WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    if m.get("type") == "status":
                        if m.get("gl") and m["gl"] != self.gl:
                            self.gl = m["gl"]
                            log(f"Drawing with: {self.gl}")
                        self.status = m
                        await self.to_sources(m)
                    elif m.get("type") == "nogl":
                        log(f"No WebGL 2 ({m.get('error', '')})")
                        if self.gpu:
                            log("The GPU didn't work: switching to software rendering (slow)")
                            self.gpu = False
                            if self.chromium and self.chromium.returncode is None:
                                self.chromium.kill()
        finally:
            if self.page is ws:
                self.page = None
            log("Render page disconnected")
        return ws

    async def to_page(self, m):
        if self.page and not self.page.closed:
            await self.page.send_str(json.dumps(m))

    async def to_page_bytes(self, b):
        if self.page and not self.page.closed:
            await self.page.send_bytes(b)

    # ── the Odroid: audio in, commands in, status out ───────────────────────
    async def audio_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
        await ws.prepare(request)
        self.sources.add(ws)
        log(f"Odroid connected from {request.remote}")
        await ws.send_str(json.dumps(self.status))
        await ws.send_str(json.dumps({"type": "want", "on": bool(self.viewers)}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if self.viewers:
                        await self.to_page_bytes(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    if m.get("type") == "settings":
                        self.settings = {k: v for k, v in m.items() if k != "type"}
                    await self.to_page(m)          # next / prev / preset / title / settings
        finally:
            self.sources.discard(ws)
            log("Odroid disconnected")
        return ws

    async def to_sources(self, m):
        text = json.dumps(m)
        for ws in list(self.sources):
            if not ws.closed:
                await ws.send_str(text)

    # ── the picture: MJPEG for the kiosk's <img> ────────────────────────────
    async def stream(self, request):
        resp = web.StreamResponse(headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            "Cache-Control": "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
        })
        await resp.prepare(request)
        self.viewers.add(resp)
        if len(self.viewers) == 1:
            await self.viewers_changed()
        try:
            while True:
                try:
                    await asyncio.wait_for(self.frame_event.wait(), 5)
                except asyncio.TimeoutError:
                    continue
                if self.frame:
                    await resp.write(b"--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                     % (BOUNDARY.encode(), len(self.frame)) + self.frame + b"\r\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.viewers.discard(resp)
            if not self.viewers:
                await self.viewers_changed()
        return resp

    async def viewers_changed(self):
        on = bool(self.viewers)
        log("Streaming" if on else "No viewers: paused")
        await self.to_page({"type": "run", "on": on})
        await self.to_sources({"type": "want", "on": on})

    async def index(self, _request):
        return web.Response(content_type="text/html", text=(
            "<!doctype html><title>Milkdrop renderer</title>"
            "<body style='margin:0;background:#000;color:#ccc;font:16px sans-serif'>"
            "<img src='stream' style='width:100vw;height:100vh;object-fit:contain'>"))

    async def status_json(self, _request):
        return web.json_response({**self.status, "gl": self.gl, "viewers": len(self.viewers),
                                  "odroids": len(self.sources), "page": bool(self.page)})

    # ── headless Chromium, kept running ─────────────────────────────────────
    async def run_chromium(self):
        exe = os.environ.get("CHROMIUM_BIN") or shutil.which("chromium") or shutil.which("chromium-browser")
        if not exe:
            log("No Chromium found")
            return
        w, h = int(self.opts["width"]), int(self.opts["height"])
        while True:
            if os.environ.get("CHROMIUM_GL_FLAGS"):        # for testing elsewhere
                gl = os.environ["CHROMIUM_GL_FLAGS"].split()
            elif self.gpu:
                gl = ["--enable-gpu", "--use-angle=vulkan", "--enable-features=Vulkan",
                      "--disable-vulkan-surface", "--ignore-gpu-blocklist"]
            else:
                gl = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
            args = [
                exe, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", *gl,
                "--mute-audio", "--no-first-run", "--disable-extensions",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                f"--window-size={w},{h}",
                "--user-data-dir=" + os.environ.get("CHROMIUM_PROFILE", "/tmp/milkdrop-chromium"),
                f"http://127.0.0.1:{PORT}/render.html",
            ]
            log(f"Starting Chromium ({'GPU' if self.gpu else 'software'})")
            self.chromium = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rc = await self.chromium.wait()
            log(f"Chromium exited ({rc}), restarting in 5 s")
            await asyncio.sleep(5)

    async def start(self, app):
        app["chromium"] = asyncio.create_task(self.run_chromium())

    async def stop(self, app):
        app["chromium"].cancel()
        if self.chromium and self.chromium.returncode is None:
            self.chromium.kill()


def main():
    opts = options()
    r = Renderer(opts)
    app = web.Application()
    app.router.add_get("/", r.index)
    app.router.add_get("/stream", r.stream)
    app.router.add_get("/status", r.status_json)
    app.router.add_get("/audio", r.audio_ws)
    app.router.add_get("/page", r.page_ws)
    app.router.add_static("/", WWW)
    app.on_startup.append(r.start)
    app.on_cleanup.append(r.stop)
    log(f"Milkdrop renderer on port {PORT}: {opts['width']}×{opts['height']} at up to {opts['fps']} fps")
    web.run_app(app, host=os.environ.get("MILKDROP_HOST", "0.0.0.0"), port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    sys.exit(main())
