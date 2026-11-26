# Changelog

Newest first.

## Unreleased

## 0.4.0

- **Music Assistant's own MilkDrop Visualizer plugin feeds the visualizer** (`music_assistant`,
  `music_assistant_token`, `player`). The app takes the waveform Music Assistant already decodes for a player,
  timed to when it is heard, so it works with every player (Squeezelite, Echo, Cast, Sendspin…) with no grouping,
  no extra player and no second stream. The built-in Squeezelite player is no longer needed (it still works).
- **Channels**: several visualizers at once, one per speaker (`channels`). Each has its own picture
  (`/stream?channel=garage`), tuning and settings pages, and only draws while watched.
- Presets change on a beat when Music Assistant knows the beats.
- Fixed: the app noticed a kiosk leaving only when the next frame was sent.

  After merging: update the app, turn on **MilkDrop Visualizer** in Music Assistant (Settings → Plugins), create a
  long-lived token there, and set `music_assistant`, `music_assistant_token` and `channels` (see DOCS.md). Point each
  kiosk at `/stream?channel=<name>`. Nothing changes until you do: the old single-visualizer setup and its
  settings carry over. If you used the built-in player, you can ungroup and remove "Visualizer" in Music Assistant.

- Claude Code permission rules (`.claude/settings.json`): branch pushes, new branches and pull requests are allowed; force-pushes, pushes to `main`/`master`, deletes and merges are blocked. Every PR now carries a changelog entry (see CLAUDE.md).
  After merging: nothing.

## 0.3.1

- Fixed: the drawing page never got its settings or audio on older aiohttp (the Home Assistant image's), so the visualizer stayed blank.

## 0.3.0

- Built-in Music Assistant player (`squeezelite_server`, `player_name`): the visualizer can follow what Music Assistant plays.
- Host port moved from 8097 to 8190 (Music Assistant uses 8097).

## 0.2.1

- Generic names and examples; the app slug is now `milkdrop_renderer` (uninstall an old `odroid_milkdrop` install).

## 0.2.0

- `audio_device` option and a `/control` socket: choose which device the music is heard from, and tune Milkdrop from a settings page.
