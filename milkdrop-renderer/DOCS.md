# Milkdrop renderer

Draws the Milkdrop visualizer (butterchurn) for the Winamp remote of
[ODroid-C2](https://github.com/A7QG59F2/ODroid-C2) on this machine and streams it to the kiosk, so the
Odroid's weak GPU only has to show a picture.

```
Odroid winamp-server ──ws /audio──▶ this app ──▶ headless Chromium (butterchurn, WebGL 2)
kiosk <img src=/stream> ◀── MJPEG ─────────────────────┘
```

It only draws while the kiosk is showing it. If the app is stopped or unreachable, the kiosk draws Milkdrop
itself (slowly) and switches back once the app is running again.

## Setup

1. Install and start this app.
2. On the Odroid, set `MILKDROP_RENDERER="http://<home-assistant-ip>:8098"` in `/etc/odroid-ha/odroid-ha.conf`
   (or answer the `odroid-ha-install --configure` question) and run `odroid-ha-install`.

## Options

| Option | Default | |
|---|---|---|
| `width`, `height` | 640, 400 | Size Milkdrop is drawn at; the kiosk scales it up |
| `fps` | 30 | Frames a second, at most |
| `jpeg_quality` | 80 | Picture quality of the stream (30–95) |
| `gpu` | true | Draw on the GPU (Intel/AMD through `/dev/dri`, Vulkan). Off = software (SwiftShader) only |

If the GPU gives no WebGL 2, the app switches to software by itself. Software only manages a few frames a
second at 640×400, so lower the size if that happens. The log names what it draws with (`Drawing with: …`).

## Network

Port 8098: `/stream` (the picture), `/audio` (the Odroid's link), `/status` (JSON) and `/` (a page to watch
the stream in a browser). There's no login: anyone on your network can watch the visualizer or send it audio,
nothing more.
