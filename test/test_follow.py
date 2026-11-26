"""Channels and following Music Assistant speakers, against the real renderer.py and a stand-in Music Assistant
(its WebSocket API: auth, players/all, events and the few commands the app uses). Two channels:
  garage  follows "Car-Radio" (a Squeezelite speaker): sync, the built-in player joins its group
  kitchen follows "KitchenEcho" (an Echo, can't sync): mirror, the built-in player plays the same track
Needs python3-aiohttp. Run: python3 test/test_follow.py"""
import asyncio, atexit, json, os, subprocess, sys, tempfile, time
import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "milkdrop-renderer", "rootfs", "app", "renderer.py")
tmp = tempfile.mkdtemp()
MA_PORT, PORT = 18201, 18101
BASE = f"http://127.0.0.1:{PORT}"
MAC_G, MAC_K = "02:00:00:00:00:01", "02:00:00:00:00:02"
SQ = "squeezelite--abc"

players = {
    "car": {"player_id": "car", "name": "Car-Radio", "provider": SQ, "available": True,
            "playback_state": "playing", "can_group_with": [SQ], "group_members": [], "synced_to": None},
    "echo": {"player_id": "echo", "name": "KitchenEcho", "provider": "alexa--x", "available": True,
             "playback_state": "playing", "can_group_with": [], "group_members": [], "synced_to": None},
    MAC_G: {"player_id": MAC_G, "name": "Visualizer Garage", "provider": SQ, "available": True,
            "playback_state": "idle", "can_group_with": [SQ], "group_members": [], "synced_to": None},
    MAC_K: {"player_id": MAC_K, "name": "Visualizer Kitchen", "provider": SQ, "available": True,
            "playback_state": "idle", "can_group_with": [SQ], "group_members": [], "synced_to": None},
}
queues = {
    "echo": {"queue_id": "echo", "state": "playing", "elapsed_time": 50, "elapsed_time_last_updated": time.time(),
             "current_item": {"queue_item_id": "q1", "name": "Song", "duration": 200,
                              "media_item": {"uri": "library://track/7"}}},
    MAC_K: {"queue_id": MAC_K, "state": "idle", "elapsed_time": 0, "elapsed_time_last_updated": time.time(),
            "current_item": None},
}
calls, clients = [], set()


async def push(event, oid, data):
    for ws in list(clients):
        await ws.send_str(json.dumps({"event": event, "object_id": oid, "data": data}))


async def ma_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_str(json.dumps({"server_id": "x", "server_version": "2.99", "schema_version": 30,
                                  "min_supported_schema_version": 1}))
    authed = False
    async for msg in ws:
        m = json.loads(msg.data)
        cmd, a, mid = m["command"], m.get("args") or {}, m["message_id"]
        if cmd == "auth":
            authed = a.get("token") == "secret"
            clients.add(ws)
            await ws.send_str(json.dumps({"message_id": mid, "result": {"authenticated": authed}} if authed else
                                         {"message_id": mid, "error_code": 20, "details": "Invalid token"}))
            continue
        if not authed:
            await ws.send_str(json.dumps({"message_id": mid, "error_code": 20, "details": "auth first"}))
            continue
        calls.append((cmd, a))
        result = None
        if cmd == "players/all":
            vals = list(players.values())                       # in two pieces, like a long listing
            await ws.send_str(json.dumps({"message_id": mid, "result": vals[:2], "partial": True}))
            result = vals[2:]
        elif cmd == "players/cmd/set_members":
            for pid in a["player_ids_to_add"]:
                players[pid]["synced_to"] = a["target_player"]
                players[a["target_player"]]["group_members"] = [a["target_player"], pid]
                await push("player_updated", pid, players[pid])
        elif cmd == "players/cmd/ungroup":
            players[a["player_id"]]["synced_to"] = None
            await push("player_updated", a["player_id"], players[a["player_id"]])
        elif cmd == "player_queues/get_active_queue":
            result = queues.get(a["player_id"])
        elif cmd == "player_queues/get":
            result = queues.get(a["queue_id"])
        elif cmd == "player_queues/play_media":
            q = queues[a["queue_id"]]
            q.update(state="playing", elapsed_time=0, elapsed_time_last_updated=time.time(),
                     current_item={"queue_item_id": "v1", "duration": 200, "media_item": {"uri": a["media"]}})
            players[a["queue_id"]]["playback_state"] = "playing"
            await push("player_updated", a["queue_id"], players[a["queue_id"]])
        elif cmd == "player_queues/seek":
            queues[a["queue_id"]].update(elapsed_time=a["position"], elapsed_time_last_updated=time.time())
        elif cmd == "player_queues/stop":
            queues[a["queue_id"]]["state"] = "idle"
            players[a["queue_id"]]["playback_state"] = "idle"
        await ws.send_str(json.dumps({"message_id": mid, "result": result}))
    clients.discard(ws)
    return ws


def seen(cmd, **match):
    return [a for c, a in calls if c == cmd and all(a.get(k) == v for k, v in match.items())]


async def wait_for(pred, what, secs=8):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"never saw: {what}\ncalls: {calls}")


async def main():
    app = web.Application()
    app.router.add_get("/ws", ma_ws)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", MA_PORT).start()

    json.dump({"channels": {"garage": {"mac": MAC_G}, "kitchen": {"mac": MAC_K}}}, open(f"{tmp}/state.json", "w"))
    json.dump({"music_assistant": f"127.0.0.1:{MA_PORT}", "music_assistant_token": "secret",
               "channels": [{"name": "Garage", "follow": "Car-Radio"}, {"name": "Kitchen", "follow": "kitchenecho"}]},
              open(f"{tmp}/opts.json", "w"))
    env = {**os.environ, "MILKDROP_PORT": str(PORT), "MILKDROP_HOST": "127.0.0.1", "STATE_FILE": f"{tmp}/state.json",
           "OPTIONS_FILE": f"{tmp}/opts.json", "CHROMIUM_BIN": "/bin/false", "FOLLOW_EVERY": "0.5",
           "FOLLOW_GRACE": "0", "SQUEEZELITE_BIN": os.path.join(HERE, "fake_squeezelite.py"),
           "FAKE_ARGS": f"{tmp}/args.txt"}
    log = open(f"{tmp}/log.txt", "w")
    p = subprocess.Popen([sys.executable, APP], env=env, stdout=log, stderr=subprocess.STDOUT)
    atexit.register(p.kill)
    try:
        await asyncio.sleep(1.5)
        args = open(f"{tmp}/args.txt").read()
        assert f"-n Visualizer Garage -s 127.0.0.1 -m {MAC_G}" in args, args
        assert f"-n Visualizer Kitchen -s 127.0.0.1 -m {MAC_K}" in args, args
        print("a built-in player per channel, on Music Assistant's host")
        await wait_for(lambda: seen("players/all"), "players/all")
        await asyncio.sleep(1)
        assert not seen("players/cmd/set_members") and not seen("player_queues/play_media"), "followed with nobody watching"
        print("nobody watching: nothing followed")

        async with aiohttp.ClientSession() as s:
            page = await s.ws_connect(f"{BASE}/page")
            cfg = json.loads((await page.receive()).data)
            assert cfg["type"] == "config" and cfg["channels"] == 2, cfg

            # garage: sync with the Squeezelite speaker
            g = await s.get(f"{BASE}/stream?channel=garage")
            await wait_for(lambda: seen("players/cmd/set_members", target_player="car", player_ids_to_add=[MAC_G]), "sync")
            await wait_for(lambda: seen("players/cmd/volume_set", player_id=MAC_G, volume_level=100), "full volume")
            assert not seen("player_queues/play_media"), "kitchen followed while not watched"
            print("garage: joined Car-Radio's group at full volume")

            # kitchen: mirror the Echo
            k = await s.get(f"{BASE}/stream?channel=kitchen")
            await wait_for(lambda: seen("player_queues/play_media", queue_id=MAC_K, media="library://track/7",
                                        option="replace"), "mirror play_media")
            await wait_for(lambda: seen("player_queues/seek", queue_id=MAC_K), "mirror seek")
            pos = seen("player_queues/seek", queue_id=MAC_K)[-1]["position"]
            assert 49 <= pos <= 56, pos
            print(f"kitchen: mirroring the Echo, seeked to {pos} s")

            # each channel's audio goes to its own canvas
            got = set()
            end = time.time() + 2
            while time.time() < end and got != {0, 1}:
                try:
                    m = await asyncio.wait_for(page.receive(), 0.3)
                except asyncio.TimeoutError:
                    continue
                if m.type == aiohttp.WSMsgType.BINARY:
                    got.add(m.data[0])
            assert got == {0, 1}, got
            print("audio for both channels reaches the page, marked 0 and 1")

            # the page's frames come back to the right stream
            await page.send_bytes(b"\x01KITCHENJPEG")
            chunk = await asyncio.wait_for(k.content.read(200), 3)
            assert b"KITCHENJPEG" in chunk, chunk
            print("frames reach the right channel's stream")

            # the Echo stops: the mirror stops
            players["echo"]["playback_state"] = "idle"
            await push("player_updated", "echo", players["echo"])
            await wait_for(lambda: seen("player_queues/stop", queue_id=MAC_K), "mirror stop")
            print("Echo stopped: the kitchen player stopped")

            # nobody watches the garage: it leaves the group
            g.close()
            await wait_for(lambda: seen("players/cmd/ungroup", player_id=MAC_G), "ungroup")
            print("garage unwatched: left the group")

            async with s.get(f"{BASE}/status?channel=kitchen") as r:
                j = await r.json()
            assert j["channel"] == "kitchen" and j["music_assistant"]["connected"], j
            assert [c["id"] for c in j["channels"]] == ["garage", "kitchen"], j
            ctl = await s.ws_connect(f"{BASE}/control?channel=garage")
            st = json.loads((await ctl.receive()).data)
            assert st["channel"] == "garage" and st["follow"]["mode"] == "sync" and st["device"] == "Visualizer Garage", st
            async with s.get(f"{BASE}/stream?channel=nope") as r:
                assert r.status == 404
            print("status, control and unknown channels ok")
            k.close()
    finally:
        p.kill(); p.wait()
        await runner.cleanup()
    print("ALL OK")


try:
    asyncio.run(main())
except Exception:
    print(open(f"{tmp}/log.txt").read())
    raise
