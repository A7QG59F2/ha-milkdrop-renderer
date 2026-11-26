# Milkdrop renderer

Draws the Milkdrop visualizer (butterchurn) on this machine and streams it to a kiosk or screen, so a
device with a weak GPU only has to show a picture.

```
audio device ──ws /audio──▶ this app ──▶ headless Chromium (butterchurn, WebGL 2)
kiosk <img src=/stream> ◀── MJPEG ─────────────────────┘
```

It only draws while the kiosk is showing it. If the app is stopped or unreachable, the kiosk draws Milkdrop
itself (slowly) and switches back once the app is running again.

## Setup

1. Install and start this app.
2. Point the device that plays the music (and the kiosk that shows the picture) at
   `http://<home-assistant-ip>:8190`: audio goes to `/audio`, the picture comes from `/stream`.

## Options

| Option | Default | |
|---|---|---|
| `width`, `height` | 640, 400 | Size Milkdrop is drawn at; the kiosk scales it up |
| `fps` | 30 | Frames a second, at most |
| `jpeg_quality` | 80 | Picture quality of the stream (30–95) |
| `gpu` | true | Draw on the GPU (Intel/AMD through `/dev/dri`, Vulkan). Off = software (SwiftShader) only |
| `squeezelite_server` | *(empty)* | The address of your Music Assistant (or Lyrion) server, e.g. `192.0.2.5` or `192.0.2.5:3483`. Set it and the app also runs a built-in player (below). Empty = no built-in player |
| `player_name` | `Visualizer` | The built-in player's name in Music Assistant |
| `audio_device` | *(empty)* | Which device the music is heard from, by name or address (e.g. `player-1` or `192.0.2.10`). Empty = any device. A choice made from a settings page is remembered by the app and wins over this |

If the GPU gives no WebGL 2, the app switches to software by itself. Software only manages a few frames a
second at 640×400, so lower the size if that happens. The log names what it draws with (`Drawing with: …`).

## The built-in player (hear Music Assistant directly)

Set `squeezelite_server` to your Music Assistant server's address (the Home Assistant machine's IP, if Music Assistant
runs as an app there; Squeezelite players connect on port 3483) and restart the app. In Music Assistant a new player
named **Visualizer** appears (turn on its Squeezelite/Slimproto provider if it doesn't). It makes no sound.

1. In Music Assistant, **group** the Visualizer with the player that makes the sound (the radio, a speaker): the player
   menu → *Group/sync with*. Music Assistant then sends both the same music, in step.
2. Choose it as the audio source: in `audio_device`, or on a settings page: **Visualizer** (shown as "built-in player").
3. Leave the Visualizer's volume at 100%: the picture is drawn from the samples after the volume is applied, so a
   quiet Visualizer is a flat one.
4. If the picture runs ahead of or behind the sound, use the audio delay setting.

Notes: it plays at 48 kHz, and Music Assistant converts to that. With the source set to "any device" the built-in
player is only heard while it carries real sound. Its identity (a made-up MAC address) is kept in the app's storage
so Music Assistant sees the same player after a restart. The app's image is larger for it (Squeezelite brings audio
libraries).

## Which device it hears

Any device can connect to `/audio`, but the renderer listens to only one. Devices that aren't listened to are
told to stop sending, and their commands are ignored. A device is known by the name it gives (`/audio?name=player-1`,
or a `{"type":"hello","name":"player-1"}` message), or by its address if it gives none.

Choose the device (the built-in player included) from a settings page that uses `/control` (below; it can list the devices connected now and which
are sending), or set `audio_device` above. The choice is kept in the
app's own storage across restarts; "Any device" restores the old behaviour.

## Settings pages (`/control`)

`/control` is a WebSocket for a settings page (no audio). It sends the renderer's state (`control`: the chosen device,
the devices it can see, and the tuning; `status`: presets, what it draws with, frames a second) and takes the same
commands as `/audio` (`settings` with `cycle`, `minFps`, `delayMs`; `next`, `prev`, `preset`, `forget_slow`) plus
`{"type":"source","device":"player-1"}` (empty = any device). The tuning is kept across restarts too.

## Network

Port 8190 on the Home Assistant machine (8098 inside the app; Music Assistant uses 8097 and
another app may use 8098 on the machine): `/stream` (the picture), `/audio` (the devices' link), `/control` (settings pages), `/status` (JSON) and `/` (a page to watch
the stream in a browser). There's no login: anyone on your network can watch the visualizer or send it audio,
nothing more. To use another port, change it under the app's **Network** section and
point the audio device and the kiosk at the new port.
