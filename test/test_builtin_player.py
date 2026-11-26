"""The built-in Squeezelite player: it becomes a device that can be listened to, its audio reaches the
page in the same shape as a device's, and it isn't heard when another device is chosen.
Runs the real renderer.py; Squeezelite is a stand-in (test/fake_squeezelite.py). Needs python3-aiohttp.
Run: python3 test/test_builtin_player.py
Also run it as FALSY_WS=1 python3 test/test_builtin_player.py: a WebSocket object then counts as "false",
as it does in older aiohttp (the Home Assistant image's), which must change nothing."""
import asyncio, atexit, json, os, struct, subprocess, sys, tempfile, time
import aiohttp

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "milkdrop-renderer", "rootfs", "app", "renderer.py")
tmp = tempfile.mkdtemp()
env = {**os.environ, "MILKDROP_PORT": "18099", "MILKDROP_HOST": "127.0.0.1", "STATE_FILE": f"{tmp}/state.json",
       "OPTIONS_FILE": f"{tmp}/opts.json", "CHROMIUM_BIN": "/bin/false",
       "SQUEEZELITE_BIN": os.path.join(HERE, "fake_squeezelite.py"), "FAKE_ARGS": f"{tmp}/args.txt"}
if os.environ.get("FALSY_WS"):
    env["PYTHONPATH"] = os.path.join(HERE, "falsy_ws")
BASE = "http://127.0.0.1:18099"

def start(opts):
    open(f"{tmp}/opts.json", "w").write(json.dumps(opts))
    proc = subprocess.Popen([sys.executable, APP], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(proc.kill)                           # never leave one running after a failed test
    return proc

async def next_json(ws, pred, secs=4):
    end = time.time() + secs
    while time.time() < end:
        try:
            m = await asyncio.wait_for(ws.receive(), 0.5)
        except asyncio.TimeoutError:
            continue
        if m.type == aiohttp.WSMsgType.TEXT and pred(json.loads(m.data)):
            return json.loads(m.data)
    raise AssertionError("never saw it")

async def audio_for(ws, secs):
    """Binary frames the page got in `secs` seconds."""
    got, end = [], time.time() + secs
    while time.time() < end:
        try:
            m = await asyncio.wait_for(ws.receive(), 0.3)
        except asyncio.TimeoutError:
            continue
        if m.type == aiohttp.WSMsgType.BINARY:
            got.append(m.data)
    return got

async def main():
    p = start({"squeezelite_server": "192.0.2.5", "player_name": "Visualizer", "audio_device": "Visualizer"})
    await asyncio.sleep(1.5)
    async with aiohttp.ClientSession() as s:
        page = await s.ws_connect(f"{BASE}/page")           # stands in for the Chromium page
        ctl = await s.ws_connect(f"{BASE}/control")
        view = await s.get(f"{BASE}/stream")                # somebody watching: audio is wanted
        st = await next_json(ctl, lambda m: m["type"] == "control" and any(x["id"] == "builtin" and x["live"] for x in m["sources"]))
        b = next(x for x in st["sources"] if x["id"] == "builtin")
        assert b["name"] == "Visualizer" and st["device"] == "Visualizer", st
        print("listed:", b)
        # its audio reaches the page as channel, kind (1 = PCM), u32 rate + mono int16
        frames = await audio_for(page, 1.0)
        assert frames, "no audio reached the page"
        assert frames[0][:2] == b"\x00\x01", "not marked as channel 0, PCM"
        rate = struct.unpack("<I", frames[0][2:6])[0]
        samples = struct.unpack(f"<{(len(frames[0]) - 6) // 2}h", frames[0][6:])
        assert rate == 48000 and max(samples) > 5000, (rate, max(samples))
        print(f"audio ok: {len(frames)} chunks, rate {rate}, peak {max(samples)}")
        # another device chosen: the player isn't heard
        await ctl.send_str(json.dumps({"type": "source", "device": "somebody-else"}))
        await asyncio.sleep(0.5)
        await audio_for(page, 0.2)                           # drain
        assert not await audio_for(page, 1.0), "heard a device that was not chosen"
        print("not chosen: silent")
        # any device: heard while it carries real sound
        await ctl.send_str(json.dumps({"type": "source", "device": ""}))
        await asyncio.sleep(0.5)
        assert await audio_for(page, 1.0), "not heard on any device"
        print("any device: heard")
        view.close()
    p.kill(); p.wait()
    first = open(f"{tmp}/args.txt").read().splitlines()[0]
    assert first.startswith("-n Visualizer -s 192.0.2.5 -m 02:"), first
    assert "-o - -a 16 -r 48000-48000" in first, first
    mac = first.split("-m ")[1].split()[0]
    print("squeezelite args ok:", first)
    # a restart keeps the same made-up MAC, so Music Assistant sees the same player
    os.remove(f"{tmp}/args.txt")
    p = start({"squeezelite_server": "192.0.2.5"})
    await asyncio.sleep(1.5)
    p.kill(); p.wait()
    again = open(f"{tmp}/args.txt").read().splitlines()[0]
    assert f"-m {mac} " in again, (mac, again)
    print("same MAC after restart:", mac)
    # no server set: no built-in player
    os.remove(f"{tmp}/args.txt")
    p = start({})
    await asyncio.sleep(1.5)
    async with aiohttp.ClientSession() as s:
        ctl = await s.ws_connect(f"{BASE}/control")
        st = await next_json(ctl, lambda m: m["type"] == "control")
        assert st["sources"] == [], st
    p.kill(); p.wait()
    assert not os.path.exists(f"{tmp}/args.txt")
    print("switched off by default")
    print("ALL OK")

asyncio.run(main())
