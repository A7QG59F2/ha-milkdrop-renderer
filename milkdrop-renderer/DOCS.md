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
2. On the Odroid, set `MILKDROP_RENDERER="http://<home-assistant-ip>:8097"` in `/etc/odroid-ha/odroid-ha.conf`
   (or answer the `odroid-ha-install --configure` question) and run `odroid-ha-install`.

## Options

| Option | Default | |
|---|---|---|
| `width`, `height` | 640, 400 | Size Milkdrop is drawn at; the kiosk scales it up |
| `fps` | 30 | Frames a second, at most |
| `jpeg_quality` | 80 | Picture quality of the stream (30–95) |
| `gpu` | true | Draw on the GPU (Intel/AMD through `/dev/dri`, Vulkan). Off = software (SwiftShader) only |
| `audio_device` | *(empty)* | Which device the music is heard from, by name or address (e.g. `garage` or `192.168.1.40`). Empty = any device. A choice made in Brassworks Studio's Winamp settings sheet is remembered by the app and wins over this |

If the GPU gives no WebGL 2, the app switches to software by itself. Software only manages a few frames a
second at 640×400, so lower the size if that happens. The log names what it draws with (`Drawing with: …`).

## Which device it hears

Any device can connect to `/audio`, but the renderer listens to only one. Devices that aren't listened to are
told to stop sending, and their commands are ignored. A device is known by the name it gives (`/audio?name=garage`,
or a `{"type":"hello","name":"garage"}` message), or by its address if it gives none.

Choose the device in **Brassworks Studio**: tap the Winamp logo on any Winamp element, then **Visualizer → Audio from**
(it lists the devices connected now and which are sending). Or set `audio_device` above. The choice is kept in the
app's own storage across restarts; "Any device" restores the old behaviour.

## Settings pages (`/control`)

`/control` is a WebSocket for a settings page (no audio). It sends the renderer's state (`control`: the chosen device,
the devices it can see, and the tuning; `status`: presets, what it draws with, frames a second) and takes the same
commands as `/audio` (`settings` with `cycle`, `minFps`, `delayMs`; `next`, `prev`, `preset`, `forget_slow`) plus
`{"type":"source","device":"garage"}` (empty = any device). The tuning is kept across restarts too.

## Network

Port 8097 on the Home Assistant machine (8098 inside the app; Brassworks Studio
uses 8098 on the machine): `/stream` (the picture), `/audio` (the devices' link), `/control` (settings pages), `/status` (JSON) and `/` (a page to watch
the stream in a browser). There's no login: anyone on your network can watch the visualizer or send it audio,
nothing more. To use another port, change it under the app's **Network** section and
change `MILKDROP_RENDERER` on the Odroid to match.
