#!/usr/bin/env python3
"""Milkdrop renderer.

A weak device's GPU can't run Milkdrop, so it runs here instead:

  Music Assistant's MilkDrop Visualizer plugin ─ws─┐
  audio device ──ws /audio──────────────────────────┼─▶ this server ──ws /page──▶ headless Chromium
                                                    │                            (butterchurn, WebGL 2)
  kiosk <img src=/stream?channel=…> ◀── MJPEG ── this server ◀── JPEG frames ──┘

Channels: several visualizers at once, one per channel, so two speakers playing
different music each get their own. A channel has its own Milkdrop (a canvas in
the same Chromium page), its own stream (/stream?channel=garage), devices,
settings pages and tuning. Without `channels` in the options there is one
channel, "main", that behaves as before. A URL without ?channel= means the first
channel. A channel only draws while somebody watches it.

Following a Music Assistant player: a channel's `player` names one (by name or
id). While the channel is watched, the app subscribes to that player's waveform
from Music Assistant's own MilkDrop Visualizer plugin
(ws://<ma>:8095/milkdrop_visualizer?player=<id>). The plugin reads the audio
Music Assistant is already decoding for the player, so it works for any player
(Squeezelite, Echo, Cast, Sendspin, …) without grouping, extra players or a
second stream. Each frame carries the time (on Music Assistant's clock) it is
heard at; the app syncs to that clock and hands Chromium each frame when it is
due, plus the delay setting. Beats from the plugin make preset changes land on a
beat.

Other sources, as before: devices that send their own audio to /audio, and the
built-in Squeezelite player (`squeezelite_server`, the single-channel setup only).
A channel listens to one of them, chosen by name, address or id (`audio_device`,
or from a settings page, which is remembered in /data/state.json and wins over the
option; empty = any device). A channel with a `player` hears it unless a device is
chosen.

/control?channel=… is for settings pages (no audio): it takes the same commands
as /audio's text messages plus {"type":"source","device":"<name>"} and sends back
that channel's state.

The page link (/page): text messages carry "ch" (the channel's index); binary
messages are [ch][kind][data], kind 1 = PCM (u32 rate + int16 mono), 2 = a
waveform (1024 × uint8, 128 = silence), 3 = a beat (flags, bit 0 = downbeat).
"""
import asyncio
import collections
import json
import os
import random
import re
import shutil
import struct
import sys
import time
from array import array
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

HERE = Path(__file__).parent
WWW = HERE / "www"
PORT = int(os.environ.get("MILKDROP_PORT", "8098"))
BOUNDARY = "milkdropframe"
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state.json"))
TUNING = {"cycle": 30, "minFps": 0, "delayMs": 200}   # what render.js starts with
LIVE_SECONDS = 3                                       # audio this recently = "sending audio"
PLAYER_RATE = 48000                                    # the built-in player always plays at this rate
MAX_CHANNELS = 8
GRACE = float(os.environ.get("FEED_GRACE", "30"))      # keep the plugin feed this long after the last viewer
PCM, WAVE, BEAT = 1, 2, 3                              # kinds of binary message to the page
WAVE_TAG, BEAT_TAG = 22, 17                            # the plugin's binary frames


def log(msg):
    print(msg, flush=True)


def options():
    """App options from Home Assistant (/data/options.json), or defaults."""
    opts = {"width": 640, "height": 400, "fps": 30, "jpeg_quality": 80, "gpu": True, "audio_device": "",
            "squeezelite_server": "", "player_name": "Visualizer", "music_assistant": "",
            "music_assistant_token": "", "channels": []}
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


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def ma_base(addr):
    """'192.0.2.5', '192.0.2.5:8095' or 'http://192.0.2.5:8095/' → 'ws://192.0.2.5:8095'."""
    a = addr.strip().rstrip("/")
    a = re.sub(r"^http", "ws", a) if re.match(r"^(https?|wss?)://", a) else "ws://" + a
    hostpart = re.sub(r"^wss?://", "", a).split("/")[0]
    if not re.search(r":\d+$", hostpart):
        a = a.replace(hostpart, hostpart + ":8095", 1)
    return re.sub(r"/ws$", "", a)


def channel_specs(opts):
    """The channels from the options: [{id, name, player, device}]; one 'main' channel when none are given."""
    raw = opts.get("channels") if isinstance(opts.get("channels"), list) else []
    specs, seen = [], set()
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()[:40]
        cid = slug(name)
        if not cid or cid in seen:
            continue
        seen.add(cid)
        player = c.get("player") or c.get("follow")         # `follow` is 0.4.0's name for it
        specs.append({"id": cid, "name": name, "player": str(player or "").strip(),
                      "device": str(c.get("audio_device") or "")})
        if len(specs) == MAX_CHANNELS:
            break
    if not specs:
        specs = [{"id": "main", "name": "", "player": str(opts.get("player") or "").strip(),
                  "device": str(opts.get("audio_device") or "")}]
    return specs


class MusicAssistant:
    """What the app needs from Music Assistant: its address, the token, and player ids by name."""

    def __init__(self, addr, token):
        self.base = ma_base(addr)
        self.token = token
        self.ids = {}                   # name (lower case) or id → player_id

    async def player_id(self, key, refresh=False):
        """A player's id, from its name or id (asks Music Assistant's API once, then remembers)."""
        k = key.strip().lower()
        if refresh or k not in self.ids:
            async with aiohttp.ClientSession() as s, s.ws_connect(f"{self.base}/ws", heartbeat=30,
                                                                   max_msg_size=32 << 20) as ws:
                await ws.send_str(json.dumps({"message_id": "auth", "command": "auth", "args": {"token": self.token}}))
                await ws.send_str(json.dumps({"message_id": "players", "command": "players/all"}))
                players = []
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        continue
                    m = json.loads(msg.data)
                    if m.get("message_id") in ("auth", "players", "connection") and "error_code" in m:
                        raise RuntimeError(f"Music Assistant: {m.get('details') or m['error_code']}")
                    if m.get("message_id") == "players":
                        players += list(m.get("result") or [])
                        if not m.get("partial"):
                            break
            self.ids = {}
            for p in players:
                pid = p.get("player_id")
                if pid:
                    for n in (pid, p.get("name"), p.get("display_name")):
                        if n:
                            self.ids.setdefault(str(n).strip().lower(), pid)
        return self.ids.get(k)


class PluginFeed:
    """One channel's subscription to the MilkDrop Visualizer plugin: frames in, scheduled out to the page."""

    def __init__(self, channel):
        self.c = channel
        self.state = "off"
        self.error = ""
        self.offset_us = 0              # Music Assistant's clock minus ours
        self.best_rtt = None
        self.waves = collections.deque()
        self.beats = collections.deque()

    @staticmethod
    def now_us():
        return int(time.monotonic() * 1_000_000)

    async def set_state(self, s):
        if s != self.state:
            self.state = s
            log(f"{self.c.label()} ← {self.c.player}: {s}")
            await self.c.control_changed()

    def due(self, ts):
        """When a frame stamped with Music Assistant's time is heard, on our clock, plus the delay setting."""
        return ts - self.offset_us + int(float(self.c.tuning()["delayMs"]) * 1000)

    async def run(self):
        """Keep the feed while the channel is watched (and a little longer), retrying when it drops."""
        ma = self.c.r.ma
        if ma is None:
            return await self.set_state("needs music_assistant and music_assistant_token in the options")
        while True:
            if not self.c.watched():
                await self.set_state("idle (nobody watching)")
                await self.c.watch_event.wait()
                continue
            try:
                pid = await ma.player_id(self.c.player) or await ma.player_id(self.c.player, refresh=True)
                if not pid:
                    await self.set_state(f"no player called {self.c.player} in Music Assistant")
                else:
                    await self.session(ma, pid)
            except asyncio.CancelledError:
                raise
            except Exception as err:          # noqa: BLE001 - report and retry whatever went wrong
                await self.set_state(f"error: {str(err) or type(err).__name__}")
            await asyncio.sleep(5)

    async def session(self, ma, pid):
        url = f"{ma.base}/milkdrop_visualizer?player={pid}"
        async with aiohttp.ClientSession() as s, s.ws_connect(url, heartbeat=25, max_msg_size=1 << 20) as ws:
            await ws.send_str(json.dumps({"type": "auth", "token": ma.token}))
            self.best_rtt = None
            self.waves.clear()
            self.beats.clear()
            tasks = [asyncio.create_task(self.pinger(ws)), asyncio.create_task(self.pump())]
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.BINARY:
                        self.frame(msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        await self.text(json.loads(msg.data))
                    if not self.c.watched():
                        await ws.send_str(json.dumps({"type": "client/goodbye"}))
                        break
                if ws.close_code == 4001:
                    raise RuntimeError(f"Music Assistant refused the token ({ws.exception() or 'invalid'})")
                if ws.close_code and ws.close_code >= 4000:
                    raise RuntimeError(f"closed by Music Assistant ({ws.close_code})")
            finally:
                for t in tasks:
                    t.cancel()
        if not self.c.watched():
            return
        raise RuntimeError("Music Assistant closed the visualizer feed (is the MilkDrop Visualizer plugin on?)")

    def frame(self, b):
        if len(b) >= 9 + 1024 and b[0] == WAVE_TAG:
            ts = struct.unpack_from(">q", b, 1)[0]
            if self.waves and ts < self.waves[-1][0]:
                self.waves.clear()                     # the timeline jumped back (a seek)
            self.waves.append((ts, b[9:9 + 1024]))
        elif len(b) >= 10 and b[0] == BEAT_TAG:
            ts = struct.unpack_from(">q", b, 1)[0]
            if not self.beats or ts > self.beats[-1][0]:
                self.beats.append((ts, b[9:10]))

    async def text(self, m):
        t = m.get("type")
        if t == "auth_ok":
            await self.set_state("connected")
        elif t == "server/time":
            p = m.get("payload") or {}
            t1 = self.now_us()
            t0 = int(p.get("client_transmitted", t1))
            rtt = t1 - t0
            if self.best_rtt is None or rtt <= self.best_rtt * 1.5:
                self.best_rtt = rtt if self.best_rtt is None else min(self.best_rtt, rtt)
                self.offset_us = int(p.get("server_received", t0)) - (t0 + t1) // 2
        elif t in ("stream/start", "stream/clear", "stream/end"):
            self.waves.clear()
            self.beats.clear()
            if t == "stream/start":
                await self.set_state("streaming")
        elif t == "error":
            raise RuntimeError(m.get("message") or "error")

    async def pinger(self, ws):
        """Time sync with Music Assistant's clock: a burst at the start, then now and then."""
        n = 0
        while not ws.closed:
            await ws.send_str(json.dumps({"type": "client/time", "payload": {"client_transmitted": self.now_us()}}))
            n += 1
            await asyncio.sleep(0.2 if n < 8 else 10)

    async def pump(self):
        """Hand the page the newest waveform that is due, and each beat as it comes due (~60 times a second)."""
        while True:
            await asyncio.sleep(1 / 60)
            if not self.c.viewers or not self.c.wanted(self.c.plugin_info):
                continue
            now = self.now_us()
            wave = None
            while self.waves and self.due(self.waves[0][0]) <= now:
                ts, wave = self.waves.popleft()
            if wave is not None:
                self.c.plugin_info["last"] = time.monotonic()
                await self.c.to_page_bytes(WAVE, wave)
            while self.beats and self.due(self.beats[0][0]) <= now:
                _, flags = self.beats.popleft()
                await self.c.to_page_bytes(BEAT, flags)


class Channel:
    def __init__(self, r, idx, spec, state):
        self.r, self.idx, self.id, self.name = r, idx, spec["id"], spec["name"]
        self.player = spec["player"]
        self.sources = {}           # /audio websocket → {id, name, ip, last}: devices that send audio
        self.controllers = set()    # /control websockets: settings pages
        self.viewers = set()        # /stream responses
        self.unwatched_at = -1e9    # when the last viewer left
        self.watch_event = asyncio.Event()
        self.frame = None           # latest JPEG
        self.frame_event = asyncio.Event()
        self.status = {"type": "status", "presets": [], "preset": "", "fps": 0}
        self.mac = state["mac"] if isinstance(state.get("mac"), str) else ""
        # the built-in Squeezelite player: the single-channel setup only
        server = r.squeezelite_server if self.id == "main" else ""
        pname = (str(r.opts.get("player_name") or "").strip() or "Visualizer")[:60]
        self.builtin = {"id": "builtin", "name": pname, "ip": "built-in player", "last": 0.0, "server": server} if server else None
        self.player_proc = None
        # Music Assistant's waveform of `player`: one more source to listen to
        self.plugin_info = {"id": "music-assistant", "name": self.player, "ip": "Music Assistant", "last": 0.0} if self.player else None
        self.feed = PluginFeed(self) if self.player else None
        if isinstance(state.get("device"), str):
            self.device = state["device"]
        elif spec["device"]:
            self.device = spec["device"]
        else:
            self.device = ""
        self.settings = {k: v for k, v in state["settings"].items() if k in TUNING} \
            if isinstance(state.get("settings"), dict) else {}
        if self.player and "delayMs" not in self.settings:
            self.settings["delayMs"] = 0           # the plugin's frames are already timed to the sound

    def tuning(self):
        return {**TUNING, **self.settings}

    def watched(self):
        return bool(self.viewers) or time.monotonic() - self.unwatched_at < GRACE

    def label(self):
        return f"Channel {self.name}" if self.id != "main" else "Visualizer"

    # ── sources ──────────────────────────────────────────────────────────────
    def wanted(self, info):
        d = self.device.strip().lower()
        if not d:
            return True
        return d in (info["name"].lower(), info["ip"].lower(), info["id"])

    def source_list(self):
        now = time.monotonic()
        infos = list(self.sources.values()) + [i for i in (self.plugin_info, self.builtin) if i]
        return [{"id": i["id"], "name": i["name"], "ip": i["ip"], "live": now - i["last"] < LIVE_SECONDS}
                for i in infos]

    def control_state(self):
        return {"type": "control", "channel": self.id, "device": self.device, "sources": self.source_list(),
                "settings": self.tuning(),
                "player": None if not self.feed else {"name": self.player, "state": self.feed.state}}

    def state(self):
        return {"device": self.device, "settings": self.settings, "mac": self.mac}

    async def send_want(self):
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

    async def to_page(self, m):
        await self.r.to_page({**m, "ch": self.idx})

    async def to_page_bytes(self, kind, b):
        await self.r.to_page_bytes(bytes([self.idx, kind]) + b)

    async def command(self, m):
        """A command from the listened-to device or a controller, for the page."""
        if m.get("type") == "settings":
            self.settings = {k: m[k] for k in TUNING if m.get(k) is not None}
            self.r.save_state()
            await self.control_changed()
        await self.to_page(m)          # next / prev / preset / title / settings / forget_slow

    async def to_sources(self, m):
        text = json.dumps(m)
        for ws in list(self.sources):
            if not ws.closed:
                await ws.send_str(text)
        await self.to_controllers(m)

    async def viewers_changed(self):
        on = bool(self.viewers)
        if on:
            self.watch_event.set()
            self.watch_event = asyncio.Event()
        else:
            self.unwatched_at = time.monotonic()
        log(f"{self.label()}: " + ("streaming" if on else "no viewers, paused"))
        await self.to_page({"type": "run", "on": on})
        await self.send_want()

    # ── the built-in player (single channel) ─────────────────────────────────
    def player_mac(self):
        if not self.mac:
            self.mac = "02:" + ":".join(f"{random.randrange(256):02x}" for _ in range(5))
            self.r.save_state()
        return self.mac

    async def feed_pcm(self, stereo):
        """Stereo 16-bit samples from the player → the page as u32 rate + mono int16."""
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
            await self.to_page_bytes(PCM, struct.pack("<I", PLAYER_RATE) + mono.tobytes())

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
            self.player_proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            logs = asyncio.create_task(self.player_log(self.player_proc.stderr))
            try:
                pending = b""
                while True:
                    chunk = await self.player_proc.stdout.read(8192)
                    if not chunk:
                        break
                    pending += chunk
                    whole = len(pending) - len(pending) % 4         # whole stereo frames only
                    if whole:
                        await self.feed_pcm(pending[:whole])
                        pending = pending[whole:]
            finally:
                logs.cancel()
            rc = await self.player_proc.wait()
            log(f"Built-in player exited ({rc}), restarting in 5 s")
            await asyncio.sleep(5)

    async def player_log(self, stream):
        n = 0
        async for line in stream:
            text = line.decode(errors="replace").strip()
            if text and n < 200:                                    # Squeezelite is chatty when it misbehaves
                n += 1
                log(f"squeezelite: {text}")


class Renderer:
    def __init__(self, opts):
        self.opts = opts
        self.page = None
        self.gl = ""
        self.gpu = bool(opts.get("gpu", True))
        self.chromium = None
        self.squeezelite_server = str(opts.get("squeezelite_server") or "").strip()
        ma = str(opts.get("music_assistant") or "").strip()
        token = str(opts.get("music_assistant_token") or "").strip()
        self.ma = MusicAssistant(ma, token) if ma and token else None
        state = load_state()
        chstate = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        specs = channel_specs(opts)
        if not chstate and specs[0]["id"] == "main":        # the state file of 0.3.x: one channel
            chstate = {"main": {k: state[k] for k in ("device", "settings", "mac") if k in state}}
        self.channels = [Channel(self, i, s, chstate.get(s["id"]) if isinstance(chstate.get(s["id"]), dict) else {})
                         for i, s in enumerate(specs)]
        self.by_id = {c.id: c for c in self.channels}

    def save_state(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({"channels": {c.id: c.state() for c in self.channels}}))
        except OSError as err:
            log(f"Could not save {STATE_FILE}: {err}")

    def channel(self, request):
        """The channel a request is for (?channel=…), or None."""
        cid = (request.query.get("channel") or "").strip().lower()
        return self.channels[0] if not cid else self.by_id.get(cid) or self.by_id.get(slug(cid))

    # ── the Chromium page ───────────────────────────────────────────────────
    async def page_ws(self, request):
        ws = web.WebSocketResponse(max_msg_size=8 << 20)
        await ws.prepare(request)
        if self.page is not None and not self.page.closed:
            await self.page.close()
        self.page = ws
        log("Render page connected")
        o = self.opts
        await self.to_page({"type": "config", "width": o["width"], "height": o["height"], "fps": o["fps"],
                            "jpeg_quality": o["jpeg_quality"], "channels": len(self.channels)})
        for c in self.channels:
            await c.to_page({"type": "settings", **c.tuning()})
            await c.to_page({"type": "run", "on": bool(c.viewers)})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if len(msg.data) > 1 and msg.data[0] < len(self.channels):
                        c = self.channels[msg.data[0]]
                        c.frame = msg.data[1:]
                        c.frame_event.set()
                        c.frame_event = asyncio.Event()
                elif msg.type == WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    if m.get("type") == "status":
                        if m.get("gl") and m["gl"] != self.gl:
                            self.gl = m["gl"]
                            log(f"Drawing with: {self.gl}")
                        i = m.pop("ch", 0)
                        if isinstance(i, int) and 0 <= i < len(self.channels):
                            self.channels[i].status = m
                            await self.channels[i].to_sources(m)
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
        if self.page is not None and not self.page.closed:
            await self.page.send_str(json.dumps(m))

    async def to_page_bytes(self, b):
        if self.page is not None and not self.page.closed:
            await self.page.send_bytes(b)

    # ── devices ─────────────────────────────────────────────────────────────
    async def audio_ws(self, request):
        c = self.channel(request)
        if c is None:
            raise web.HTTPNotFound(text="no such channel")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
        await ws.prepare(request)
        ip = request.remote or ""
        info = {"id": f"{ip}#{len(c.sources) + 1}", "ip": ip, "last": 0.0,
                "name": (request.query.get("name") or "").strip()[:60] or ip}
        c.sources[ws] = info
        log(f"{info['name']} connected to /audio ({c.label()})" + ("" if c.wanted(info) else ", not the device listened to"))
        await ws.send_str(json.dumps(c.status))
        await ws.send_str(json.dumps({"type": "want", "on": bool(c.viewers) and c.wanted(info)}))
        await c.control_changed()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    info["last"] = time.monotonic()
                    if c.viewers and c.wanted(info):
                        await c.to_page_bytes(PCM, msg.data)
                elif msg.type == WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    if m.get("type") == "hello":
                        info["name"] = str(m.get("name") or "").strip()[:60] or info["ip"]
                        await c.send_want()
                        await c.control_changed()
                    elif c.wanted(info):
                        await c.command(m)
        finally:
            c.sources.pop(ws, None)
            log(f"{info['name']} disconnected from /audio")
            await c.control_changed()
        return ws

    async def control_ws(self, request):
        c = self.channel(request)
        if c is None:
            raise web.HTTPNotFound(text="no such channel")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 16)
        await ws.prepare(request)
        c.controllers.add(ws)
        await ws.send_str(json.dumps(c.control_state()))
        await ws.send_str(json.dumps(c.status))
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
                    c.device = str(m.get("device") or "").strip()[:60]
                    log(f"{c.label()}: audio is now heard from {c.device or 'any device'}")
                    self.save_state()
                    await c.send_want()
                    await c.control_changed()
                elif m.get("type") in ("settings", "next", "prev", "preset", "forget_slow", "title"):
                    await c.command(m)
        finally:
            c.controllers.discard(ws)
        return ws

    async def ticker(self):
        """Settings pages see which sources are live right now."""
        while True:
            await asyncio.sleep(2)
            for c in self.channels:
                if c.controllers:
                    await c.control_changed()

    # ── the picture ─────────────────────────────────────────────────────────
    async def stream(self, request):
        c = self.channel(request)
        if c is None:
            raise web.HTTPNotFound(text="no such channel")
        resp = web.StreamResponse(headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            "Cache-Control": "no-cache, no-store",
            "Access-Control-Allow-Origin": "*",
        })
        await resp.prepare(request)
        c.viewers.add(resp)
        if len(c.viewers) == 1:
            await c.viewers_changed()
        try:
            while True:
                try:
                    await asyncio.wait_for(c.frame_event.wait(), 1)
                except asyncio.TimeoutError:
                    if request.transport is None or request.transport.is_closing():
                        break                           # the viewer left while no frames came
                    continue
                if c.frame:
                    await resp.write(b"--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                     % (BOUNDARY.encode(), len(c.frame)) + c.frame + b"\r\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            c.viewers.discard(resp)
            if not c.viewers:
                await c.viewers_changed()
        return resp

    async def index(self, _request):
        links = "".join(f"<a href='?channel={c.id}' style='color:#ccc;margin:0 .5em'>{c.name}</a>"
                        for c in self.channels) if len(self.channels) > 1 else ""
        return web.Response(content_type="text/html", text=(
            "<!doctype html><title>Milkdrop renderer</title>"
            "<body style='margin:0;background:#000;color:#ccc;font:16px sans-serif'>"
            f"<div style='position:fixed;top:4px;left:4px'>{links}</div>"
            "<img id=i style='width:100vw;height:100vh;object-fit:contain'>"
            "<script>document.getElementById('i').src='stream'+location.search</script>"))

    async def status_json(self, request):
        c = self.channel(request) or self.channels[0]
        return web.json_response({**c.status, "gl": self.gl, "viewers": len(c.viewers), "devices": len(c.sources),
                                  "page": self.page is not None, "device": c.device, "sources": c.source_list(),
                                  "channel": c.id, "player": c.control_state()["player"],
                                  "channels": [{"id": x.id, "name": x.name, "viewers": len(x.viewers),
                                                "player": x.player or None,
                                                "feed": x.feed.state if x.feed else None}
                                               for x in self.channels]})

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
                "--disable-backgrounding-occluded-windows", "--max-active-webgl-contexts=32",
                f"--window-size={w},{h}",
                "--user-data-dir=" + os.environ.get("CHROMIUM_PROFILE", "/tmp/milkdrop-chromium"),
                f"http://127.0.0.1:{PORT}/render.html",
            ]
            log(f"Starting Chromium ({'GPU' if self.gpu else 'software'}, {len(self.channels)} channel(s))")
            self.chromium = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rc = await self.chromium.wait()
            log(f"Chromium exited ({rc}), restarting in 5 s")
            await asyncio.sleep(5)

    async def start(self, app):
        tasks = [self.run_chromium(), self.ticker()]
        for c in self.channels:
            tasks.append(c.run_player())
            if c.feed:
                tasks.append(c.feed.run())
        app["tasks"] = [asyncio.create_task(t) for t in tasks]

    async def stop(self, app):
        for t in app["tasks"]:
            t.cancel()
        for c in self.channels:
            if c.player_proc and c.player_proc.returncode is None:
                c.player_proc.kill()
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
    names = ", ".join((c.name or c.id) + (f" ← {c.player}" if c.player else "") for c in r.channels)
    log(f"Milkdrop renderer on port {PORT}: {opts['width']}×{opts['height']} at up to {opts['fps']} fps; channels: {names}")
    web.run_app(app, host=os.environ.get("MILKDROP_HOST", "0.0.0.0"), port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    sys.exit(main())
