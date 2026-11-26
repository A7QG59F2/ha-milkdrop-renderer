"""Channels fed by Music Assistant's MilkDrop Visualizer plugin, against the real renderer.py and a stand-in
Music Assistant: its API (/ws: auth, players/all) and the plugin's relay (/milkdrop_visualizer: auth, time sync,
timestamped waveform and beat frames, in the wire format of music_assistant/providers/milkdrop_visualizer).
The stand-in's clock runs 5 s ahead of ours, so frames are only on time if the app synced to it.
Needs python3-aiohttp. Run: python3 test/test_plugin_feed.py"""
import asyncio, atexit, json, os, struct, subprocess, sys, tempfile, time
import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "milkdrop-renderer", "rootfs", "app", "renderer.py")
tmp = tempfile.mkdtemp()
MA_PORT, PORT = 18211, 18111
BASE = f"http://127.0.0.1:{PORT}"
SKEW_US = 5_000_000
PLAYERS = [{"player_id": "car", "name": "Car-Radio"}, {"player_id": "echo-1", "name": "KitchenEcho"},
           {"player_id": "tv", "name": "Garage TV"}]
viewers = {}          # player_id → list of relay websockets
api_calls = []


def ma_now():
    return int(time.monotonic() * 1_000_000) + SKEW_US


async def api(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_str(json.dumps({"server_id": "x", "server_version": "2.10.13", "schema_version": 30,
                                  "min_supported_schema_version": 1}))
    authed = False
    async for msg in ws:
        m = json.loads(msg.data)
        api_calls.append(m["command"])
        if m["command"] == "auth":
            authed = m["args"]["token"] == "secret"
            await ws.send_str(json.dumps({"message_id": m["message_id"], "result": {}} if authed else
                                         {"message_id": m["message_id"], "error_code": 20, "details": "Invalid token"}))
        elif not authed:
            await ws.send_str(json.dumps({"message_id": m["message_id"], "error_code": 20, "details": "auth first"}))
        elif m["command"] == "players/all":
            await ws.send_str(json.dumps({"message_id": m["message_id"], "result": PLAYERS[:1], "partial": True}))
            await ws.send_str(json.dumps({"message_id": m["message_id"], "result": PLAYERS[1:]}))
    return ws


async def relay(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    msg = await ws.receive()
    if json.loads(msg.data).get("token") != "secret":
        await ws.close(code=4001, message=b"Invalid or expired token")
        return ws
    await ws.send_str('{"type": "auth_ok"}')
    pid = request.query["player"]
    if pid not in {p["player_id"] for p in PLAYERS}:
        await ws.send_str(json.dumps({"type": "error", "message": "no such player"}))
        await ws.close()
        return ws
    await ws.send_str(json.dumps({"type": "stream/start", "payload": {"visualizer": {"types": ["waveform", "beat"]}}}))
    viewers.setdefault(pid, []).append(ws)
    try:
        async for msg in ws:
            m = json.loads(msg.data)
            if m["type"] == "client/time":
                now = ma_now()
                await ws.send_str(json.dumps({"type": "server/time", "payload": {
                    "client_transmitted": m["payload"]["client_transmitted"],
                    "server_received": now, "server_transmitted": now}}))
            elif m["type"] == "client/goodbye":
                break
    finally:
        viewers[pid].remove(ws)
    return ws


async def send_frames(pid, level, at_us, beat=False):
    """A waveform of the given level (and a downbeat), due at at_us on Music Assistant's clock."""
    for ws in viewers.get(pid, []):
        await ws.send_bytes(struct.pack(">Bq", 22, at_us) + bytes([level]) * 1024)
        if beat:
            await ws.send_bytes(struct.pack(">BqB", 17, at_us, 1))


async def page_frames(page, secs):
    got, end = [], time.time() + secs
    while time.time() < end:
        try:
            m = await asyncio.wait_for(page.receive(), 0.1)
        except asyncio.TimeoutError:
            continue
        if m.type == aiohttp.WSMsgType.BINARY:
            got.append((time.monotonic(), m.data))
    return got


async def wait_for(pred, what, secs=8):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never saw: {what}")


def start(opts):
    json.dump(opts, open(f"{tmp}/opts.json", "w"))
    env = {**os.environ, "MILKDROP_PORT": str(PORT), "MILKDROP_HOST": "127.0.0.1", "STATE_FILE": f"{tmp}/state.json",
           "OPTIONS_FILE": f"{tmp}/opts.json", "CHROMIUM_BIN": "/bin/false", "FEED_GRACE": "0"}
    p = subprocess.Popen([sys.executable, APP], env=env, stdout=open(f"{tmp}/log.txt", "a"), stderr=subprocess.STDOUT)
    atexit.register(p.kill)
    return p


async def main():
    app = web.Application()
    app.router.add_get("/ws", api)
    app.router.add_get("/milkdrop_visualizer", relay)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", MA_PORT).start()

    p = start({"music_assistant": f"http://127.0.0.1:{MA_PORT}", "music_assistant_token": "secret",
               "channels": [{"name": "Garage", "player": "Car-Radio"}, {"name": "Kitchen", "player": "echo-1"},
                            {"name": "Nowhere", "player": "Nope"}]})
    try:
        await asyncio.sleep(1.5)
        assert not viewers.get("car") and not viewers.get("echo-1"), "subscribed with nobody watching"
        print("nobody watching: no feed")
        async with aiohttp.ClientSession() as s:
            page = await s.ws_connect(f"{BASE}/page")
            cfg = json.loads((await page.receive()).data)
            assert cfg["type"] == "config" and cfg["channels"] == 3, cfg

            g = await s.get(f"{BASE}/stream?channel=garage")
            k = await s.get(f"{BASE}/stream?channel=kitchen")
            await wait_for(lambda: viewers.get("car") and viewers.get("echo-1"), "both feeds")
            print("watched: subscribed to Car-Radio (by name) and echo-1 (by id)")
            await asyncio.sleep(1.5)                            # time sync burst
            await page_frames(page, 0.2)

            # frames due 1 s from now (on MA's clock) reach the page after ~1 s, not before
            due_local = time.monotonic() + 1.0
            await send_frames("car", 200, ma_now() + 1_000_000, beat=True)
            await send_frames("echo-1", 50, ma_now() + 1_000_000)
            got = await page_frames(page, 2.0)
            waves = [(t, d) for t, d in got if d[1] == 2]
            beats = [(t, d) for t, d in got if d[1] == 3]
            assert {d[0] for _, d in waves} == {0, 1}, [d[:2] for _, d in got]
            for t, d in waves:
                assert abs(t - due_local) < 0.15, f"channel {d[0]} frame {t - due_local:+.3f} s off"
                assert len(d) == 2 + 1024 and d[2] == (200 if d[0] == 0 else 50), d[:4]
            assert len(beats) == 1 and beats[0][1][0] == 0, beats
            print("waveforms reach each channel on time ({}), beat too".format(
                ", ".join(f"{d[0]}: {t - due_local:+.3f} s" for t, d in waves)))

            # a frame already late is shown at once; the delay setting holds frames back
            ctl = await s.ws_connect(f"{BASE}/control?channel=kitchen")
            st = json.loads((await ctl.receive()).data)
            assert st["settings"]["delayMs"] == 0 and st["player"]["state"] == "streaming", st
            assert any(x["id"] == "music-assistant" and x["name"] == "echo-1" for x in st["sources"]), st
            await ctl.send_str(json.dumps({"type": "settings", "cycle": 30, "minFps": 0, "delayMs": 500}))
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            await send_frames("echo-1", 90, ma_now())
            got = [(t, d) for t, d in await page_frames(page, 1.2) if d[:2] == b"\x01\x02"]
            assert got and 0.4 < got[0][0] - t0 < 0.7, [t - t0 for t, _ in got]
            print(f"delay setting: shown {got[0][0] - t0:.2f} s later")

            # the channel without a real player says so
            n = await s.get(f"{BASE}/stream?channel=nowhere")
            await asyncio.sleep(1)
            async with s.get(f"{BASE}/status?channel=nowhere") as r:
                j = await r.json()
            n.close()
            assert j["channel"] == "nowhere" and "no player called Nope" in j["player"]["state"], j
            print("unknown player reported:", j["player"]["state"])

            # nobody watches the garage: its feed is dropped
            g.close()
            await send_frames("car", 100, ma_now())             # the app notices on the next message
            await wait_for(lambda: not viewers.get("car"), "garage feed dropped")
            assert viewers.get("echo-1"), "kitchen dropped too"
            print("garage unwatched: feed dropped, kitchen kept")
            k.close()
    finally:
        p.kill(); p.wait()

    # a wrong token is reported, not retried silently
    p = start({"music_assistant": f"127.0.0.1:{MA_PORT}", "music_assistant_token": "wrong", "player": "echo-1"})
    try:
        await asyncio.sleep(1.5)
        async with aiohttp.ClientSession() as s:
            v = await s.get(f"{BASE}/stream")
            await asyncio.sleep(1.5)
            async with s.get(f"{BASE}/status") as r:
                j = await r.json()
            assert j["channel"] == "main" and j["player"]["state"].startswith("error"), j
            print("single channel with a wrong token:", j["player"]["state"])
            v.close()
    finally:
        p.kill(); p.wait()
        await runner.cleanup()
    print("ALL OK")


try:
    asyncio.run(main())
except Exception:
    print(open(f"{tmp}/log.txt").read())
    raise
