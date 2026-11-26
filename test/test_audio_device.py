"""Which device the audio is heard from, and the /control socket, against the real renderer.py
(no Chromium needed: CHROMIUM_BIN points at /bin/false). Run: python3 test/test_audio_device.py"""
import asyncio, atexit, json, os, subprocess, sys, tempfile, time
import aiohttp

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "milkdrop-renderer", "rootfs", "app", "renderer.py")
tmp = tempfile.mkdtemp()
env = {**os.environ, "MILKDROP_PORT": "18098", "MILKDROP_HOST": "127.0.0.1", "STATE_FILE": f"{tmp}/state.json",
       "OPTIONS_FILE": f"{tmp}/opts.json", "CHROMIUM_BIN": "/bin/false"}
open(f"{tmp}/opts.json", "w").write(json.dumps({"audio_device": "player-1"}))

def start():
    proc = subprocess.Popen([sys.executable, APP], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    atexit.register(proc.kill)                           # never leave one running after a failed test
    return proc

async def recv_until(ws, pred, secs=3):
    end = time.time() + secs
    while time.time() < end:
        try:
            m = await asyncio.wait_for(ws.receive(), 0.5)
        except asyncio.TimeoutError:
            continue
        if m.type == aiohttp.WSMsgType.TEXT:
            j = json.loads(m.data)
            if pred(j):
                return j
    raise AssertionError("never saw it")

async def main():
    p = start()
    await asyncio.sleep(1.5)
    base = "http://127.0.0.1:18098"
    async with aiohttp.ClientSession() as s:
        dev_a = await s.ws_connect(f"{base}/audio?name=player-1")
        dev_b = await s.ws_connect(f"{base}/audio")
        await dev_b.send_str(json.dumps({"type": "hello", "name": "player-2"}))
        ctl = await s.ws_connect(f"{base}/control")
        st = await recv_until(ctl, lambda m: m["type"] == "control" and len(m["sources"]) == 2)
        assert st["device"] == "player-1", st                      # from the option
        assert {x["name"] for x in st["sources"]} == {"player-1", "player-2"}, st
        assert st["settings"] == {"cycle": 30, "minFps": 0, "delayMs": 200}, st
        print("control state ok:", st["device"], [x["name"] for x in st["sources"]])
        # a viewer starts: only player-1 is asked to send
        view = await s.get(f"{base}/stream")
        w = await recv_until(dev_a, lambda m: m["type"] == "want" and m["on"])
        k = await recv_until(dev_b, lambda m: m["type"] == "want")
        assert w["on"] and not k["on"], (w, k)
        print("want: player-1 on, player-2 off")
        # switch the device from the controller
        await ctl.send_str(json.dumps({"type": "source", "device": "player-2"}))
        await recv_until(dev_b, lambda m: m["type"] == "want" and m["on"])
        await recv_until(dev_a, lambda m: m["type"] == "want" and not m["on"])
        st = await recv_until(ctl, lambda m: m["type"] == "control" and m["device"] == "player-2")
        print("switched to player-2")
        # tuning is kept
        await ctl.send_str(json.dumps({"type": "settings", "cycle": 45, "minFps": 12, "delayMs": 300}))
        await recv_until(ctl, lambda m: m["type"] == "control" and m["settings"]["cycle"] == 45)
        # a device that isn't listened to can't command it (no crash, nothing forwarded)
        await dev_a.send_str(json.dumps({"type": "next"}))
        # any device
        await ctl.send_str(json.dumps({"type": "source", "device": ""}))
        await recv_until(dev_a, lambda m: m["type"] == "want" and m["on"])
        print("any device: player-1 on again")
        view.close()
        for w in (dev_a, dev_b, ctl):
            await w.close()
        async with s.get(f"{base}/status") as r:
            j = await r.json()
            assert "sources" in j and j["device"] == "", j
    p.kill(); p.wait()
    state = json.load(open(f"{tmp}/state.json"))
    assert state["device"] == "" and state["settings"] == {"cycle": 45, "minFps": 12, "delayMs": 300}, state
    print("state saved:", state)
    # restart: the controller's choice (any device, '') beats the option ("player-1"); tuning is back
    p = start(); await asyncio.sleep(1.5)
    async with aiohttp.ClientSession() as s:
        ctl = await s.ws_connect(f"{base}/control")
        st = await recv_until(ctl, lambda m: m["type"] == "control")
        assert st["device"] == "" and st["settings"]["cycle"] == 45, st
        print("after restart:", st["device"] or "any device", st["settings"])
    p.kill(); p.wait()
    print("ALL OK")

asyncio.run(main())
