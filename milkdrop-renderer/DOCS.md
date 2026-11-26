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
| `music_assistant` | *(empty)* | Your Music Assistant server, e.g. `192.0.2.5` or `192.0.2.5:8095`. Turns on the built-in players (below) and lets channels follow a speaker |
| `music_assistant_token` | *(empty)* | A long-lived token, created in Music Assistant under your user's profile/security settings. Needed to follow speakers. Music Assistant's long-lived tokens last a year, so renew it then |
| `channels` | *(none)* | One visualizer per channel (below). None = a single visualizer, as before |
| `squeezelite_server` | *(empty)* | Only to point the built-in players somewhere other than `music_assistant` (e.g. a Lyrion server, or `192.0.2.5:3483`) |
| `player_name` | `Visualizer` | The built-in player's name in Music Assistant; with channels, each one's is this plus the channel's name ("Visualizer Garage") |
| `audio_device` | *(empty)* | Without channels: which device the music is heard from, by name or address (e.g. `player-1` or `192.0.2.10`). Empty = any device. A choice made from a settings page is remembered by the app and wins over this |

If the GPU gives no WebGL 2, the app switches to software by itself. Software only manages a few frames a
second at 640×400, so lower the size if that happens. The log names what it draws with (`Drawing with: …`).

## Channels: a visualizer per speaker

Each channel is its own Milkdrop with its own picture, built-in player, tuning and settings pages, so two speakers
playing different music each get their own visualizer. Add them under `channels`:

```yaml
music_assistant: 192.0.2.5
music_assistant_token: "eyJ…"
channels:
  - name: Garage
    follow: Car-Radio
  - name: Kitchen
    follow: KitchenEcho
```

| Channel field | | |
|---|---|---|
| `name` | required | Its name; the URLs use it in lower case (`Garage` → `?channel=garage`) |
| `follow` | *(empty)* | A Music Assistant player, by name or id, to follow (below). Empty = group the built-in player yourself |
| `mode` | `auto` | `sync`, `mirror`, or `auto`: sync where the speaker allows it, mirror otherwise |
| `audio_device` | *(empty)* | Hear a device on `/audio` instead of the built-in player |

A kiosk picks its channel in the URL: `http://<home-assistant-ip>:8190/stream?channel=garage` (the same for
`/audio`, `/control` and `/status`; without `?channel=` it's the first channel). A channel only draws while somebody
watches it. Each one costs about as much GPU as the single visualizer did, so 2–3 at 640×400 is comfortable on an
Intel iGPU; with more, lower the size or `fps`.

## Following a speaker

With `follow` set, the channel's built-in player hears whatever that speaker plays, without grouping anything by hand.
The app only does it while the speaker is playing **and** somebody watches the channel (and for 30 seconds after the
last viewer leaves), and undoes it afterwards. Its state shows on settings pages and in `/status` (`follow`).

- **Sync** (Squeezelite speakers, like a Squeezelite-on-Bluetooth radio): the built-in player joins the speaker's sync
  group, so Music Assistant plays both in step. Joining a group can make the speaker pause for a moment while
  Music Assistant restarts the stream.
- **Mirror** (Echo, Cast and anything else Music Assistant can't sync with a Squeezelite player): the built-in player
  plays the same track on its own queue, started at the speaker's position, and the app seeks it back whenever it
  drifts more than 4 seconds. The speaker's own lag (Echos buffer a few seconds) is what the **audio delay** setting
  is for. The track is streamed twice, so a music service that allows only one stream at a time may stop one of
  them, and Music Assistant may count the play twice. Live radio can't be seeked; it's just started on both.

The app sets the built-in player's volume to 100% when it starts following, and evens out its level either way, so
the picture doesn't go flat when Music Assistant turns the group down.

## The built-in player (hear Music Assistant directly)

Set `music_assistant` to your Music Assistant server's address (the Home Assistant machine's IP, if Music Assistant
runs as an app there; the built-in players connect to it as Squeezelite players on port 3483) and restart the app.
In Music Assistant a new player named **Visualizer** appears (one per channel, turn on the Squeezelite/Slimproto
provider if it doesn't). It makes no sound.

With `follow` (above) the app does the rest. Without it:

1. In Music Assistant, **group** the Visualizer with the player that makes the sound: the player menu →
   *Group/sync with*. Music Assistant then sends both the same music, in step.
2. Choose it as the audio source: in `audio_device`, or on a settings page: **Visualizer** (shown as "built-in player").
   A channel that follows a speaker hears its built-in player already.
3. If the picture runs ahead of or behind the sound, use the audio delay setting.

Notes: it plays at 48 kHz, and Music Assistant converts to that. With the source set to "any device" the built-in
player is only heard while it carries real sound. Its identity (a made-up MAC address) is kept in the app's storage
so Music Assistant sees the same player after a restart. The app's image is larger for it (Squeezelite brings audio
libraries).

## Which device it hears

Any device can connect to `/audio` (`/audio?channel=garage` for a channel), but each channel listens to only one. Devices that aren't listened to are
told to stop sending, and their commands are ignored. A device is known by the name it gives (`/audio?name=player-1`,
or a `{"type":"hello","name":"player-1"}` message), or by its address if it gives none.

Choose the device (the built-in player included) from a settings page that uses `/control` (below; it can list the devices connected now and which
are sending), or set `audio_device` above. The choice is kept in the
app's own storage across restarts; "Any device" restores the old behaviour.

## Settings pages (`/control`)

`/control` (`/control?channel=garage`) is a WebSocket for a settings page (no audio). It sends the channel's state
(`control`: the channel, the chosen device, the devices it can see, the tuning, and `follow`: the speaker, the mode
and what it's doing; `status`: presets, what it draws with, frames a second) and takes the same
commands as `/audio` (`settings` with `cycle`, `minFps`, `delayMs`; `next`, `prev`, `preset`, `forget_slow`) plus
`{"type":"source","device":"player-1"}` (empty = any device). The tuning is kept across restarts too.

## Network

Port 8190 on the Home Assistant machine (8098 inside the app; Music Assistant uses 8097 and
another app may use 8098 on the machine): `/stream` (the picture), `/audio` (the devices' link), `/control` (settings pages), `/status` (JSON) and `/` (a page to watch
the stream in a browser). There's no login: anyone on your network can watch the visualizer or send it audio,
nothing more. To use another port, change it under the app's **Network** section and
point the audio device and the kiosk at the new port.
