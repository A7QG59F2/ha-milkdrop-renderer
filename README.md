# Milkdrop renderer (Home Assistant app)

A Home Assistant app that draws the Milkdrop visualizer (butterchurn) on the Home Assistant machine's GPU and
streams it, as a small video, to a kiosk or screen that is too weak to draw it itself. A device that plays the
music sends its audio here.

Add it in **Settings → Apps → App store → ⋮ → Repositories** with `https://github.com/A7QG59F2/ha-milkdrop-renderer`,
then install **Milkdrop renderer**. Setup and options: [milkdrop-renderer/DOCS.md](milkdrop-renderer/DOCS.md).

## Tests

```bash
python3 test/test_audio_device.py    # needs python3-aiohttp; starts renderer.py without Chromium
python3 test/test_builtin_player.py  # the built-in player, with a stand-in for Squeezelite
```

## Testing it off Home Assistant

`renderer.py` runs anywhere with Python, aiohttp and Chromium/Chrome:

```bash
CHROMIUM_BIN=/path/to/chrome CHROMIUM_PROFILE=/tmp/md OPTIONS_FILE=opts.json \
  CHROMIUM_GL_FLAGS="--use-angle=d3d11 --enable-gpu" python milkdrop-renderer/rootfs/app/renderer.py
```

`OPTIONS_FILE` is a JSON file like the app's options; `CHROMIUM_GL_FLAGS` replaces the GPU flags
(`--use-angle=d3d11` on Windows). The butterchurn scripts need copying into `rootfs/app/www/` first
(see the Dockerfile for the pinned versions and hashes). On a Windows PC with an Intel iGPU it drew 30 fps
at 640×400; SwiftShader managed 2–7.
