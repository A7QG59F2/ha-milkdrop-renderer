#!/usr/bin/env python3
"""Milkdrop renderer.

A weak device's GPU can't run Milkdrop, so it runs here instead:

  audio device ──ws /audio──▶ this server ──ws /page──▶ headless Chromium
      (audio samples, commands)                               (butterchurn, WebGL 2
                                                               on the GPU, or on
                                                               SwiftShader)
  kiosk <img src=/stream> ◀── MJPEG ── this server ◀── JPEG frames ──┘

Channels: the app can draw several visualizers at once, one per channel, so two
speakers playing different music each get their own. A channel has its own
Milkdrop (one canvas in the same Chromium page), its own stream
(/stream?channel=garage), its own devices, settings pages and tuning, and its
own built-in player. Without `channels` in the options there is one channel,
"main", that behaves exactly as before. Every URL without ?channel= means the
first channel.

The built-in player: with a Music Assistant (or Lyrion) server known, each
channel runs Squeezelite as a player that outputs no sound; its audio feeds that
channel as one more device ("built-in player").

Following a speaker: with `music_assistant` (and its token) set, a channel's
`follow` names a Music Assistant player. While that speaker plays and somebody
watches the channel, the app makes its built-in player hear the same music:

  sync    the built-in player joins the speaker's sync group (Squeezelite
          speakers such as the JVC radio): sample-accurate, MA does the work.
  mirror  for speakers it can't sync with (Echo, Cast, …): the built-in player
          plays the same track at the same position on its own queue, and the
          app keeps correcting drift. The delay setting covers the speaker's lag.

`mode: auto` (the default) tries sync and falls back to mirror. When the speaker
stops, or nobody has watched for a while, the app ungroups / stops its player.

Which device a channel hears: any number of devices may connect to
/audio?channel=…, but each channel listens to only one, chosen by name, address
or id (`audio_device`, or from a settings page, which is remembered in
/data/state.json and wins over the option; empty = any device). Devices that
aren't listened to are told to stop sending ({"type":"want","on":false}) and
their commands are ignored. A channel that follows a speaker listens to its
built-in player unless a settings page chose otherwise.

/control?channel=… is for settings pages (no audio): it takes the same commands
as /audio's text messages plus {"type":"source","device":"<name>"} and sends back
that channel's state.

The page link (/page) carries every channel: text messages have "ch" (the
channel's index); binary messages start with one byte, the channel's index.
"""
import asyncio
import itertools
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
FOLLOW_GRACE = float(os.environ.get("FOLLOW_GRACE", "30"))  # keep following this long after the last viewer
FOLLOW_EVERY = float(os.environ.get("FOLLOW_EVERY", "5"))   # check the followed speaker at least this often
MIRROR_DRIFT = 4                                       # seconds off before the mirror seeks


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


def host_of(addr):
    """'http://192.0.2.5:8095/' → '192.0.2.5'."""
    a = re.sub(r"^[a-z]+://", "", addr.strip()).split("/")[0]
    if a.startswith("["):
        return a.split("]")[0] + "]"
    return a.rsplit(":", 1)[0] if a.count(":") == 1 else a


def ma_ws_url(addr):
    """'192.0.2.5', '192.0.2.5:8095' or 'http://192.0.2.5:8095' → 'ws://192.0.2.5:8095/ws'."""
    a = addr.strip().rstrip("/")
    a = re.sub(r"^http", "ws", a) if re.match(r"^https?://", a) else "ws://" + a
    hostpart = re.sub(r"^wss?://", "", a).split("/")[0]
    if not re.search(r":\d+$", hostpart):
        a = a.replace(hostpart, hostpart + ":8095", 1)
    return a if a.endswith("/ws") else a + "/ws"


def channel_specs(opts):
    """The channels from the options: [{id, name, follow, mode, device}]; one 'main' channel when none are given."""
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
        mode = str(c.get("mode") or "auto").strip().lower()
        specs.append({"id": cid, "name": name, "follow": str(c.get("follow") or "").strip(),
                      "mode": mode if mode in ("auto", "sync", "mirror") else "auto",
                      "device": str(c.get("audio_device") or "")})
        if len(specs) == MAX_CHANNELS:
            break
    if not specs:
        specs = [{"id": "main", "name": "", "follow": "", "mode": "auto", "device": str(opts.get("audio_device") or "")}]
    return specs


class MAError(Exception):
    pass


class MusicAssistant:
    """A small client for Music Assistant's WebSocket API: players, their events, and commands."""

    def __init__(self, addr, token, on_change):
        self.url = ma_ws_url(addr)
        self.token = token
        self.on_change = on_change      # called with a player_id / queue_id when something changes
        self.ws = None
        self.ready = False
        self.players = {}
        self.ids = itertools.count(1)
        self.waiting = {}
        self.partial = {}
        self.error = ""

    async def call(self, command, **args):
        if self.ws is None or self.ws.closed:
            raise MAError("not connected to Music Assistant")
        mid = str(next(self.ids))
        fut = asyncio.get_running_loop().create_future()
        self.waiting[mid] = fut
        try:
            await self.ws.send_str(json.dumps({"message_id": mid, "command": command, "args": args}))
            return await asyncio.wait_for(fut, 15)
        except asyncio.TimeoutError:
            raise MAError(f"{command}: no answer") from None
        finally:
            self.waiting.pop(mid, None)

    def find(self, key):
        """A player by id or name (case-insensitive)."""
        k = key.strip().lower()
        if not k:
            return None
        for p in self.players.values():
            if p.get("player_id", "").lower() == k:
                return p
        for p in self.players.values():
            if (p.get("name") or "").strip().lower() == k or (p.get("display_name") or "").strip().lower() == k:
                return p
        return None

    def find_own(self, name, mac):
        """The built-in player: Squeezelite's player_id is its MAC; failing that, its name."""
        m = mac.lower()
        for p in self.players.values():
            if p.get("player_id", "").lower() == m:
                return p
        return self.find(name)

    async def run(self):
        while True:
            try:
                async with aiohttp.ClientSession() as s, s.ws_connect(self.url, heartbeat=30,
                                                                       max_msg_size=32 << 20) as ws:
                    self.ws = ws
                    reader = asyncio.create_task(self.read(ws))
                    try:
                        await self.hello()
                        await reader
                    finally:
                        reader.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as err:      # noqa: BLE001 - keep retrying whatever went wrong
                msg = str(err) or type(err).__name__
                if msg != self.error:
                    log(f"Music Assistant ({self.url}): {msg}")
                self.error = msg
            self.ws, self.ready = None, False
            for fut in self.waiting.values():
                if not fut.done():
                    fut.set_exception(MAError("disconnected"))
            self.on_change(None)
            await asyncio.sleep(10)

    async def hello(self):
        if self.token:
            await self.call("auth", token=self.token)
        players = await self.call("players/all")
        self.players = {p["player_id"]: p for p in players or [] if isinstance(p, dict) and p.get("player_id")}
        self.ready, self.error = True, ""
        log(f"Music Assistant connected: {len(self.players)} players")
        self.on_change(None)

    async def read(self, ws):
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            if "message_id" in m and m["message_id"] in self.waiting:
                fut = self.waiting[m["message_id"]]
                if fut.done():
                    continue
                if "error_code" in m:
                    self.partial.pop(m["message_id"], None)
                    fut.set_exception(MAError(f"{m.get('error_code')}: {m.get('details') or 'failed'}"))
                elif m.get("partial"):                          # a long listing comes in pieces
                    self.partial.setdefault(m["message_id"], []).extend(m.get("result") or [])
                else:
                    head = self.partial.pop(m["message_id"], None)
                    fut.set_result(head + list(m.get("result") or []) if head is not None else m.get("result"))
                continue
            if "event" in m:
                ev, oid, data = m.get("event"), m.get("object_id"), m.get("data")
                if ev in ("player_added", "player_updated") and isinstance(data, dict) and data.get("player_id"):
                    self.players[data["player_id"]] = data
                elif ev == "player_removed" and oid:
                    self.players.pop(oid, None)
                if ev in ("player_added", "player_updated", "player_removed", "queue_updated"):
                    self.on_change(oid)
            elif m.get("error_code") and m.get("message_id") == "connection":
                raise MAError(m.get("details") or "refused")


def media_uri(item):
    if not isinstance(item, dict):
        return ""
    mi = item.get("media_item") if isinstance(item.get("media_item"), dict) else {}
    return mi.get("uri") or item.get("uri") or ""


def queue_pos(q):
    """Where a queue is in its current item now (seconds)."""
    pos = float(q.get("elapsed_time") or 0)
    if q.get("state") == "playing" and q.get("elapsed_time_last_updated"):
        pos += max(0.0, time.time() - float(q["elapsed_time_last_updated"]))
    return pos


class Channel:
    def __init__(self, r, idx, spec, state):
        self.r, self.idx, self.id, self.name = r, idx, spec["id"], spec["name"]
        self.follow, self.mode_opt = spec["follow"], spec["mode"]
        self.sources = {}           # /audio websocket → {id, name, ip, last}: devices that send audio
        self.controllers = set()    # /control websockets: settings pages
        self.viewers = set()        # /stream responses
        self.unwatched_at = -1e9    # when the last viewer left
        self.frame = None           # latest JPEG
        self.frame_event = asyncio.Event()
        self.status = {"type": "status", "presets": [], "preset": "", "fps": 0}
        self.settings = {}
        self.mac = state["mac"] if isinstance(state.get("mac"), str) else ""
        server = r.squeezelite_server
        base = (str(r.opts.get("player_name") or "").strip() or "Visualizer")
        pname = (base if spec["id"] == "main" else f"{base} {self.name}")[:60]
        self.builtin = {"id": "builtin", "name": pname, "ip": "built-in player", "last": 0.0, "server": server} if server else None
        self.player = None          # the Squeezelite process
        if isinstance(state.get("device"), str):
            self.device = state["device"]
        elif spec["device"]:
            self.device = spec["device"]
        elif self.follow and self.builtin:
            self.device = self.builtin["name"]     # a follower hears its own player
        else:
            self.device = ""
        if isinstance(state.get("settings"), dict):
            self.settings = {k: v for k, v in state["settings"].items() if k in TUNING}
        self.peak = 0.0             # auto-gain for the built-in player
        self.mode = None            # follow: "sync" / "mirror", decided once the speaker is seen
        self.follow_state = "off" if not self.follow else "waiting for Music Assistant"
        self.mirrored = ""          # the uri the mirror put on the built-in player
        self.mirror_at = 0.0
        self.kick_event = asyncio.Event()

    # ── devices ──────────────────────────────────────────────────────────────
    def wanted(self, info):
        d = self.device.strip().lower()
        return not d or d in (info["name"].lower(), info["ip"], info["id"])

    def source_list(self):
        now = time.monotonic()
        infos = list(self.sources.values()) + ([self.builtin] if self.builtin else [])
        return [{"id": i["id"], "name": i["name"], "ip": i["ip"], "live": now - i["last"] < LIVE_SECONDS}
                for i in infos]

    def control_state(self):
        return {"type": "control", "channel": self.id, "device": self.device, "sources": self.source_list(),
                "settings": {**TUNING, **self.settings},
                "follow": {"player": self.follow, "mode": self.mode or self.mode_opt, "state": self.follow_state}}

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

    async def to_page_bytes(self, b):
        await self.r.to_page_bytes(bytes([self.idx]) + b)

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
        if not on:
            self.unwatched_at = time.monotonic()
        log(f"{self.label()}: " + ("streaming" if on else "no viewers, paused"))
        await self.to_page({"type": "run", "on": on})
        await self.send_want()
        self.kick()

    def label(self):
        return f"Channel {self.name}" if self.id != "main" else "Visualizer"

    # ── the built-in player ──────────────────────────────────────────────────
    def player_mac(self):
        if not self.mac:
            self.mac = "02:" + ":".join(f"{random.randrange(256):02x}" for _ in range(5))
            self.r.save_state()
        return self.mac

    async def feed(self, stereo):
        """Stereo 16-bit samples from the player → the page, as the /audio devices send them (u32 rate + mono int16).

        Auto-gain: Music Assistant may turn the player down (group volume), which would flatten the picture,
        so its audio is brought up to a steady level."""
        a = array("h")
        a.frombytes(stereo)
        if sys.byteorder == "big":
            a.byteswap()
        mono = array("h", [(left + right) >> 1 for left, right in zip(a[0::2], a[1::2])])
        peak = max(max(mono), -min(mono)) if mono else 0
        loud = peak > 8                                       # real sound, not silence
        if loud:
            self.builtin["last"] = time.monotonic()
        self.peak = max(float(peak), self.peak * 0.98)
        if self.peak > 200:
            gain = min(8.0, 16000.0 / self.peak)
            if gain > 1.05:
                mono = array("h", [max(-32768, min(32767, int(v * gain))) for v in mono])
        if self.viewers and self.wanted(self.builtin) and (loud or self.device.strip()):
            await self.to_page_bytes(struct.pack("<I", PLAYER_RATE) + mono.tobytes())

    async def run_player(self):
        if not self.builtin:
            return
        exe = os.environ.get("SQUEEZELITE_BIN") or shutil.which("squeezelite")
        if not exe:
            log("A Music Assistant server is set but Squeezelite isn't installed")
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
                    whole = len(pending) - len(pending) % 4
                    if whole:
                        await self.feed(pending[:whole])
                        pending = pending[whole:]
            finally:
                logs.cancel()
            rc = await self.player.wait()
            log(f"Built-in player \"{self.builtin['name']}\" exited ({rc}), restarting in 5 s")
            await asyncio.sleep(5)

    async def player_log(self, stream):
        n = 0
        async for line in stream:
            text = line.decode(errors="replace").strip()
            if text and n < 200:
                n += 1
                log(f"squeezelite ({self.builtin['name']}): {text}")

    # ── following a Music Assistant speaker ──────────────────────────────────
    def kick(self):
        self.kick_event.set()

    async def set_follow_state(self, s):
        if s != self.follow_state:
            self.follow_state = s
            log(f"{self.label()} following {self.follow}: {s}")
            await self.control_changed()

    async def run_follow(self):
        if not self.follow:
            return
        if not self.builtin:
            log(f"{self.label()}: follow is set, but there's no built-in player (set music_assistant)")
            await self.set_follow_state("needs the built-in player")
            return
        while True:
            try:
                await asyncio.wait_for(self.kick_event.wait(), FOLLOW_EVERY)
            except asyncio.TimeoutError:
                pass
            self.kick_event.clear()
            await asyncio.sleep(0.3)                  # let a burst of events settle
            self.kick_event.clear()
            try:
                await self.follow_step(self.r.ma)
            except MAError as err:
                await self.set_follow_state(f"error: {err}")
            except Exception as err:                  # noqa: BLE001 - never let the loop die
                await self.set_follow_state(f"error: {err!r}")

    async def follow_step(self, ma):
        if ma is None:
            return await self.set_follow_state("needs music_assistant in the options")
        if not ma.ready:
            return await self.set_follow_state("waiting for Music Assistant" + (f" ({ma.error})" if ma.error else ""))
        target = ma.find(self.follow)
        own = ma.find_own(self.builtin["name"], self.mac)
        if not target:
            return await self.set_follow_state(f"no player called {self.follow} in Music Assistant")
        if not own:
            return await self.set_follow_state("waiting for the built-in player to show up in Music Assistant")
        tid, oid = target["player_id"], own["player_id"]
        watched = bool(self.viewers) or time.monotonic() - self.unwatched_at < FOLLOW_GRACE
        playing = target.get("playback_state") == "playing" and target.get("available", True)
        want = watched and playing
        if self.mode is None:
            if self.mode_opt != "auto":
                self.mode = self.mode_opt
            else:
                cgw = set(target.get("can_group_with") or [])
                self.mode = "sync" if (oid in cgw or own.get("provider") in cgw) else "mirror"
            log(f"{self.label()} follows {target.get('name') or tid} by {self.mode}")
        if self.mode == "sync":
            leader = target.get("synced_to") or tid
            joined = own.get("synced_to") == leader or oid in (target.get("group_members") or [])
            if want and not joined:
                try:
                    await ma.call("players/cmd/set_members", target_player=leader, player_ids_to_add=[oid])
                except MAError as err:
                    if self.mode_opt == "sync":
                        raise
                    log(f"{self.label()}: can't sync with {self.follow} ({err}); mirroring instead")
                    self.mode = "mirror"
                    return await self.follow_step(ma)
                await self.full_volume(ma, oid)
                return await self.set_follow_state("in sync")
            if not want and own.get("synced_to"):
                await ma.call("players/cmd/ungroup", player_id=oid)
            return await self.set_follow_state("in sync" if want else ("speaker idle" if watched else "nobody watching"))
        # mirror
        if not want:
            if self.mirrored:
                self.mirrored = ""
                try:
                    await ma.call("player_queues/stop", queue_id=oid)
                except MAError:
                    pass                                # already stopped
            return await self.set_follow_state("speaker idle" if watched else "nobody watching")
        tq = await ma.call("player_queues/get_active_queue", player_id=tid)
        uri = media_uri((tq or {}).get("current_item"))
        if not uri:
            return await self.set_follow_state("the speaker plays something Music Assistant can't share")
        oq = await ma.call("player_queues/get", queue_id=oid) or {}
        pos = queue_pos(tq)
        if media_uri(oq.get("current_item")) != uri or oq.get("state") != "playing":
            if self.mirrored == uri and oq.get("state") != "playing" and self.mirror_at > time.monotonic() - 8:
                return                                  # just started it: give it a moment
            log(f"{self.label()}: mirroring {uri}")
            await ma.call("player_queues/play_media", queue_id=oid, media=uri, option="replace")
            self.mirrored, self.mirror_at = uri, time.monotonic()
            await self.full_volume(ma, oid)
            return await self.set_follow_state("mirroring")
        duration = (tq.get("current_item") or {}).get("duration") or 0
        if duration and abs(queue_pos(oq) - pos) > MIRROR_DRIFT and pos < duration - 5:
            await ma.call("player_queues/seek", queue_id=oid, position=int(pos))
        await self.set_follow_state("mirroring")

    async def full_volume(self, ma, oid):
        try:
            await ma.call("players/cmd/volume_set", player_id=oid, volume_level=100)
        except MAError:
            pass                                        # auto-gain covers it


class Renderer:
    def __init__(self, opts):
        self.opts = opts
        self.page = None
        self.gl = ""
        self.gpu = bool(opts.get("gpu", True))
        self.chromium = None
        ma = str(opts.get("music_assistant") or "").strip()
        self.squeezelite_server = str(opts.get("squeezelite_server") or "").strip() or (host_of(ma) if ma else "")
        state = load_state()
        chstate = state.get("channels") if isinstance(state.get("channels"), dict) else {}
        specs = channel_specs(opts)
        if not chstate and specs[0]["id"] == "main":        # the state file of 0.3.x: one channel
            chstate = {"main": {k: state[k] for k in ("device", "settings", "mac") if k in state}}
        self.channels = [Channel(self, i, s, chstate.get(s["id"]) if isinstance(chstate.get(s["id"]), dict) else {})
                         for i, s in enumerate(specs)]
        self.by_id = {c.id: c for c in self.channels}
        token = str(opts.get("music_assistant_token") or "").strip()
        self.ma = MusicAssistant(ma, token, self.ma_changed) if ma else None

    def save_state(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({"channels": {c.id: c.state() for c in self.channels}}))
        except OSError as err:
            log(f"Could not save {STATE_FILE}: {err}")

    def ma_changed(self, _object_id):
        for c in self.channels:
            if c.follow:
                c.kick()

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
            if c.settings:
                await c.to_page({"type": "settings", **c.settings})
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
                        await c.to_page_bytes(msg.data)
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
                                  "channel": c.id, "follow": c.control_state()["follow"],
                                  "music_assistant": None if self.ma is None else
                                  {"connected": self.ma.ready, "error": self.ma.error},
                                  "channels": [{"id": x.id, "name": x.name, "viewers": len(x.viewers),
                                                "player": x.builtin["name"] if x.builtin else None,
                                                "follow": x.follow, "state": x.follow_state}
                                               for x in self.channels]})

    # ── headless Chromium, kept running ─────────────────────────────────────
    async def run_chromium(self):
        exe = os.environ.get("CHROMIUM_BIN") or shutil.which("chromium") or shutil.which("chromium-browser")
        if not exe:
            log("No Chromium found")
            return
        w, h = int(self.opts["width"]), int(self.opts["height"])
        while True:
            if os.environ.get("CHROMIUM_GL_FLAGS"):
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
            tasks += [c.run_player(), c.run_follow()]
        if self.ma:
            tasks.append(self.ma.run())
        app["tasks"] = [asyncio.create_task(t) for t in tasks]

    async def stop(self, app):
        for t in app["tasks"]:
            t.cancel()
        for c in self.channels:
            if c.player and c.player.returncode is None:
                c.player.kill()
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
    names = ", ".join(c.name or c.id for c in r.channels)
    log(f"Milkdrop renderer on port {PORT}: {opts['width']}×{opts['height']} at up to {opts['fps']} fps; channels: {names}")
    web.run_app(app, host=os.environ.get("MILKDROP_HOST", "0.0.0.0"), port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    sys.exit(main())
