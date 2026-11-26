#!/usr/bin/env python3
"""Milkdrop renderer.

A weak device's GPU can't run Milkdrop, so it runs here instead:

  audio device ──ws /audio──▶ this server ──ws /page──▶ headless Chromium
      (audio samples, commands)                               (butterchurn, WebGL 2
                                                               on the GPU, or on
                                                               SwiftShader)
  kiosk <img src=/stream> ◀── MJPEG ── this server ◀── JPEG frames ──┘

Chromium only draws while somebody watches /stream. Everything runs on one
port (8098). There's no login: it's meant for the home network, and all it
offers is a picture of the music.

The built-in player: with `squeezelite_server` set (the address of a Music
Assistant / Lyrion server), the app also runs Squeezelite as a player named
`player_name` that outputs no sound; its audio feeds the visualizer as one more
device ("built-in player"). Group it with the real player in Music Assistant and
the visualizer follows what that player plays.

Which device the music is heard from: any number of devices may connect to
/audio, but only one is listened to. It is chosen by name, address or id
(`audio_device` in the app's options, or from a controller such as a settings
page, which is remembered in /data/state.json and
wins over the option; an empty choice means "any device", as before). A device
may say who it is with {"type":"hello","name":"player-1"} (or /audio?name=player-1);
otherwise it is known by its address. Devices that aren't listened to are told
to stop sending ({"type":"want","on":false}) and their commands are ignored.

/control is for controllers (no audio): it takes the same commands as /audio's
text messages plus {"type":"source","device":"<name>"} and sends back the
renderer's state, so a settings page can show presets, the devices it can hear
and the tuning.
"""
import asyncio
import json
import os
import random
import shutil
import struct
import sys
import time
from array import array
from pathlib import Path

from aiohttp import WSMsgType, web

HERE = Path(__file__).parent
WWW = HERE / "www"
PORT = int(os.environ.get("MILKDROP_PORT", "8098"))
BOUNDARY = "milkdropframe"
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
TUNING = {"cycle": 30, "minFps": 0, "delayMs": 200}   # what render.js starts with
LIVE_SECONDS = 3                                       # audio this recently = "sending audio"
PLAYER_RATE = 48000                                    # the built-in player always plays at this rate


def log(msg):
    print(msg, flush=True)


def options():
    """App options from Home Assistant (/data/options.json), or defaults."""
    opts = {"width": 640, "height": 400, "fps": 30, "jpeg_quality": 80, "gpu": True, "audio_device": "",
            "squeezelite_server": "", "player_name": "Visualizer"}
    try:
        opts.update(json.loads(Path(os.environ.get("OPTIONS_FILE", "/data/options.json")).read_text()))
    except (OSError, ValueError):
        pass
    return opts


def load_state():
    try:
        x = json.loads(STATE_FILE.read_text())
        return x if isinstance(x, dict) else {}
    except (OSError, ValueError):
        return {}


class Renderer:
    def __init__(self, opts):
        self.opts = opts
        self.page = None            # the Chromium page's websocket
        self.sources = {}           # /audio websocket → {id, name, ip, last}: devices that send audio
        self.controllers = set()    # /control websockets: settings pages
        self.viewers = set()        # /stream responses
        self.frame = None           # latest JPEG
        self.frame_event = asyncio.Event()
        self.status = {"type": "status", "presets": [], "preset": "", "fps": 0}
        self.settings = {}          # tuning: cycle, minFps, delayMs (kept in STATE_FILE)
        state = load_state()
        # a choice made from a controller wins over the app's option ("" = any device)
        self.mac = state["mac"] if isinstance(state.get("mac"), str) else ""
        server = str(opts.get("squeezelite_server") or "").strip()
        name = (str(opts.get("player_name") or "").strip() or "Visualizer")[:60]
        # the built-in player, one more device that can be listened to (None: switched off)
        self.builtin = {"id": "builtin", "name": name, "ip": "built-in player", "last": 0.0, "server": server} if server else None
        self.player = None          # the Squeezelite process
        self.device = str(state["device"]) if isinstance(state.get("device"), str) else str(opts.get("audio_device") or "")
        if isinstance(state.get("settings"), dict):
            self.settings = {k: v for k, v in state["settings"].items() if k in TUNING}
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

    # ── the devices: audio in, commands in, status out ──────────────────────
    def wanted(self, info):
        """Is this the device that is listened to?"""
        d = self.device.strip().lower()
        return not d or d in (info["name"].lower(), info["ip"], info["id"])

    def source_list(self):
        now = time.monotonic()
        infos = list(self.sources.values()) + ([self.builtin] if self.builtin else [])
        return [{"id": i["id"], "name": i["name"], "ip": i["ip"], "live": now - i["last"] < LIVE_SECONDS}
                for i in infos]

    def control_state(self):
        return {"type": "control", "device": self.device, "sources": self.source_list(),
                "settings": {**TUNING, **self.settings}}

    async def send_want(self):
        """Tell each device whether to send: only the one listened to, and only while somebody watches."""
        for ws, info in list(self.sources.items()):
            if not ws.closed:
                await ws.send_str(json.dumps({"type": "want", "on": bool(self.viewers) and self.wanted(info)}))

    async def to_controllers(self, m):
        text = json.dumps(m)
        for ws in list(self.controllers):
            if not ws.closed:
                await ws.send_str(text)

    async def control_changed(self):
        await self.to_controllers(self.control_state())

    def save_state(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({"device": self.device, "settings": self.settings, "mac": self.mac}))
        except OSError as err:
            log(f"Could not save {STATE_FILE}: {err}")

    async def command(self, m):
        """A command from the listened-to device or a controller, for the page."""
        t = m.get("type")
        if t == "settings":
            self.settings = {k: m[k] for k in TUNING if m.get(k) is not None}
            self.save_state()
            await self.control_changed()
        await self.to_page(m)          # next / prev / preset / title / settings / forget_slow

    async def audio_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
        await ws.prepare(request)
        ip = request.remote or ""
        info = {"id": f"{ip}#{len(self.sources) + 1}", "ip": ip, "last": 0.0,
                "name": (request.query.get("name") or "").strip()[:60] or ip}
        self.sources[ws] = info
        log(f"{info['name']} connected to /audio" + ("" if self.wanted(info) else " (not the device listened to)"))
        await ws.send_str(json.dumps(self.status))
        await ws.send_str(json.dumps({"type": "want", "on": bool(self.viewers) and self.wanted(info)}))
        await self.control_changed()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    info["last"] = time.monotonic()
                    if self.viewers and self.wanted(info):
                        await self.to_page_bytes(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    if m.get("type") == "hello":
                        info["name"] = str(m.get("name") or "").strip()[:60] or info["ip"]
                        await self.send_want()
                        await self.control_changed()
                    elif self.wanted(info):
                        await self.command(m)
        finally:
            self.sources.pop(ws, None)
            log(f"{info['name']} disconnected from /audio")
            await self.control_changed()
        return ws

    # ── controllers (a settings page): commands in, state out ───────────────
    async def control_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 16)
        await ws.prepare(request)
        self.controllers.add(ws)
        await ws.send_str(json.dumps(self.control_state()))
        await ws.send_str(json.dumps(self.status))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                if m.get("type") == "source":
                    self.device = str(m.get("device") or "").strip()[:60]
                    log(f"Audio is now heard from: {self.device or 'any device'}")
                    self.save_state()
                    await self.send_want()
                    await self.control_changed()
                elif m.get("type") in ("settings", "next", "prev", "preset", "forget_slow", "title"):
                    await self.command(m)
        finally:
            self.controllers.discard(ws)
        return ws

    async def to_sources(self, m):
        """The renderer's status, to every device and every controller."""
        text = json.dumps(m)
        for ws in list(self.sources):
            if not ws.closed:
                await ws.send_str(text)
        await self.to_controllers(m)

    async def ticker(self):
        """Controllers see which devices are sending audio right now."""
        while True:
            await asyncio.sleep(2)
            if self.controllers:
                await self.control_changed()

    # ── the built-in player: Squeezelite as a Music Assistant player ────────
    def player_mac(self):
        """A fixed made-up MAC, so Music Assistant sees the same player after every restart."""
        if not self.mac:
            self.mac = "02:" + ":".join(f"{random.randrange(256):02x}" for _ in range(5))
            self.save_state()
        return self.mac

    async def feed(self, stereo):
        """Stereo 16-bit samples from the player → the page, as the /audio devices send them (u32 rate + mono int16)."""
        a = array("h")
        a.frombytes(stereo)
        if sys.byteorder == "big":
            a.byteswap()
        mono = array("h", [(left + right) >> 1 for left, right in zip(a[0::2], a[1::2])])
        loud = bool(mono) and max(max(mono), -min(mono)) > 8      # real sound, not silence
        if loud:
            self.builtin["last"] = time.monotonic()
        # with "any device" the player's silence must not break up another device's audio
        if self.viewers and self.wanted(self.builtin) and (loud or self.device.strip()):
            await self.to_page_bytes(struct.pack("<I", PLAYER_RATE) + mono.tobytes())

    async def run_player(self):
        if not self.builtin:
            return
        exe = os.environ.get("SQUEEZELITE_BIN") or shutil.which("squeezelite")
        if not exe:
            log("squeezelite_server is set but Squeezelite isn't installed")
            return
        while True:
            args = [exe, "-n", self.builtin["name"], "-s", self.builtin["server"], "-m", self.player_mac(),
                    "-o", "-", "-a", "16", "-r", f"{PLAYER_RATE}-{PLAYER_RATE}"]
            log(f"Built-in player \"{self.builtin['name']}\" connecting to {self.builtin['server']}")
            self.player = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            logs = asyncio.create_task(self.player_log(self.player.stderr))
            try:
                pending = b""
                while True:
                    chunk = await self.player.stdout.read(8192)
                    if not chunk:
                        break
                    pending += chunk
                    whole = len(pending) - len(pending) % 4         # whole stereo frames only
                    if whole:
                        await self.feed(pending[:whole])
                        pending = pending[whole:]
            finally:
                logs.cancel()
            rc = await self.player.wait()
            log(f"Built-in player exited ({rc}), restarting in 5 s")
            await asyncio.sleep(5)

    async def player_log(self, stream):
        n = 0
        async for line in stream:
            text = line.decode(errors="replace").strip()
            if text and n < 200:                                    # Squeezelite is chatty when it misbehaves
                n += 1
                log(f"squeezelite: {text}")

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
        await self.send_want()

    async def index(self, _request):
        return web.Response(content_type="text/html", text=(
            "<!doctype html><title>Milkdrop renderer</title>"
            "<body style='margin:0;background:#000;color:#ccc;font:16px sans-serif'>"
            "<img src='stream' style='width:100vw;height:100vh;object-fit:contain'>"))

    async def status_json(self, _request):
        return web.json_response({**self.status, "gl": self.gl, "viewers": len(self.viewers),
                                  "devices": len(self.sources), "page": bool(self.page),
                                  "device": self.device, "sources": self.source_list()})

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
        app["ticker"] = asyncio.create_task(self.ticker())
        app["player"] = asyncio.create_task(self.run_player())

    async def stop(self, app):
        app["chromium"].cancel()
        app["ticker"].cancel()
        app["player"].cancel()
        if self.player and self.player.returncode is None:
            self.player.kill()
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
    app.router.add_get("/control", r.control_ws)
    app.router.add_get("/page", r.page_ws)
    app.router.add_static("/", WWW)
    app.on_startup.append(r.start)
    app.on_cleanup.append(r.stop)
    log(f"Milkdrop renderer on port {PORT}: {opts['width']}×{opts['height']} at up to {opts['fps']} fps")
    web.run_app(app, host=os.environ.get("MILKDROP_HOST", "0.0.0.0"), port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    sys.exit(main())
