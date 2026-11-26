# Milkdrop renderer

Draws the Milkdrop visualizer (butterchurn) on this machine and streams it to a kiosk or screen, so a
device with a weak GPU only has to show a picture.

```
Music Assistant (MilkDrop Visualizer plugin) ─┐
audio device ──ws /audio──────────────────────┼─▶ this app ──▶ headless Chromium (butterchurn, WebGL 2)
kiosk <img src=/stream?channel=…> ◀── MJPEG ──────────────────────────┘
```

It only draws while the kiosk is showing it. If the app is stopped or unreachable, the kiosk draws Milkdrop
itself (slowly) and switches back once the app is running again.

## Setup with Music Assistant (recommended)

1. In Music Assistant: **Settings → Plugins → Add a plugin → MilkDrop Visualizer**. It reads the audio Music Assistant
   already decodes for each player, so it works with every player (Squeezelite, Echo, Cast, Sendspin, …) with
   no grouping and no extra player.
2. In Music Assistant, create a long-lived token in your user's profile. (Long-lived tokens last a year; renew it then.)
3. In this app's options set `music_assistant` (e.g. `192.0.2.5`, port 8095 is assumed), `music_assistant_token`, and
   a channel per speaker you want to watch:

   ```yaml
   music_assistant: 192.0.2.5
   music_assistant_token: "eyJ…"
   channels:
     - name: Garage
       player: Car-Radio
     - name: Kitchen
       player: KitchenEcho
   ```
4. Point each kiosk at its channel: `http://<home-assistant-ip>:8190/stream?channel=garage`.

That's all: while a kiosk watches a channel, the app takes that player's waveform from Music Assistant, timed to
when it is heard. When nobody has watched for 30 seconds it lets go. Music a provider streams straight to the device
(not decoded by Music Assistant) gives no waveform, so the picture stays still while that plays.

## Setup with a device that sends its own audio

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
| `music_assistant` | *(empty)* | Your Music Assistant server: `192.0.2.5`, `192.0.2.5:8095` or `http://…` |
| `music_assistant_token` | *(empty)* | A long-lived Music Assistant token |
| `channels` | *(none)* | A visualizer per speaker: `name`, `player` (a Music Assistant player, by name or id), and optionally `audio_device`. None = one visualizer, as before |
| `player` | *(empty)* | Without channels: the Music Assistant player to visualize |
| `audio_device` | *(empty)* | Without channels: which source is heard, by name or address (e.g. `player-1` or `192.0.2.10`). Empty = any. A choice made from a settings page is remembered by the app and wins over this |
| `squeezelite_server` | *(empty)* | Old way, without channels only: the address of a Music Assistant (or Lyrion) server for a built-in Squeezelite player (below). `player` replaces it |
| `player_name` | `Visualizer` | The built-in player's name |

If the GPU gives no WebGL 2, the app switches to software by itself. Software only manages a few frames a
second at 640×400, so lower the size if that happens. The log names what it draws with (`Drawing with: …`).

## Channels

Each channel is its own Milkdrop with its own picture, tuning and settings pages, so two speakers playing different
music each get their own visualizer. A channel's name in lower case is its URL: `?channel=garage` on `/stream`,
`/audio`, `/control` and `/status` (without it, the first channel). A channel only draws while somebody watches it.
Each costs about as much GPU as the single visualizer did, so 2–3 at 640×400 is comfortable on an Intel iGPU;
with more, lower the size or `fps`.

Presets change on a beat when Music Assistant knows the track's beats (it works them out a few seconds into a track);
otherwise by the clock, as before. A channel fed by Music Assistant starts with an audio delay of 0, because its
frames already come timed to the sound; if a speaker reports its position late (some do), raise it.

## The built-in player (old way)

Before Music Assistant had its plugin, the app ran its own silent Squeezelite player that you grouped with the
speaker. It still works for the single-visualizer setup, but `player` (above) is simpler and works with every
speaker.

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

Any device can connect to `/audio` (`/audio?channel=garage` for a channel), but each channel listens to only one
source: a device, Music Assistant's waveform of its `player` (shown as "Music Assistant"), or the built-in player. Devices that aren't listened to are
told to stop sending, and their commands are ignored. A device is known by the name it gives (`/audio?name=player-1`,
or a `{"type":"hello","name":"player-1"}` message), or by its address if it gives none.

Choose the device (the built-in player included) from a settings page that uses `/control` (below; it can list the devices connected now and which
are sending), or set `audio_device` above. The choice is kept in the
app's own storage across restarts; "Any device" restores the old behaviour.

## Settings pages (`/control`)

`/control` (`/control?channel=garage`) is a WebSocket for a settings page (no audio). It sends the channel's state
(`control`: the channel, the chosen device, the sources it can see, the tuning, and `player`: the Music Assistant
player and what its feed is doing; `status`: presets, what it draws with, frames a second) and takes the same
commands as `/audio` (`settings` with `cycle`, `minFps`, `delayMs`; `next`, `prev`, `preset`, `forget_slow`) plus
`{"type":"source","device":"player-1"}` (empty = any device). The tuning is kept across restarts too.

## Network

Port 8190 on the Home Assistant machine (8098 inside the app; Music Assistant uses 8097 and
another app may use 8098 on the machine): `/stream` (the picture), `/audio` (the devices' link), `/control` (settings pages), `/status` (JSON) and `/` (a page to watch
the stream in a browser). There's no login: anyone on your network can watch the visualizer or send it audio,
nothing more. To use another port, change it under the app's **Network** section and
point the audio device and the kiosk at the new port.
