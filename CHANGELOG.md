# Changelog

Newest first.

## Unreleased

## 0.5.0

- **Music Assistant's own MilkDrop Visualizer plugin feeds the visualizer** (`music_assistant`,
  `music_assistant_token`, and `player` on a channel). The app takes the waveform Music Assistant already decodes
  for a player, timed to when it is heard, so it works with every player (Squeezelite, Echo, Cast, Sendspin…) with
  no grouping, no extra player and no second stream. This replaces 0.4.0's following (sync/mirror with a built-in
  player per channel).
- Presets change on a beat when Music Assistant knows the beats.
- A channel fed by Music Assistant starts with an audio delay of 0 (its frames are already timed to the sound).

  After merging: update the app, turn on **MilkDrop Visualizer** in Music Assistant (Settings → Plugins). In the
  options, rename each channel's `follow` to `player` (`follow` still works) and remove `mode` if you set it. The
  per-channel built-in players ("Visualizer Garage", …) are gone: remove them in Music Assistant. The single
  built-in "Visualizer" player (`squeezelite_server`, no channels) still works but isn't needed any more.

## 0.4.0

- **Channels**: several visualizers at once, one per speaker (`channels` option). Each has its own picture
  (`/stream?channel=garage`), built-in Music Assistant player, tuning and settings pages, so two speakers playing
  different music each get their own. All channels draw in the same Chromium, and only while somebody watches.
- **Follow a speaker** (`music_assistant`, `music_assistant_token`, and `follow` on a channel): no more grouping by
  hand. While the speaker plays and the channel is watched, the app joins its built-in player to the speaker's sync
  group (Squeezelite speakers) or has it play the same track at the same position (Echo, Cast and others it can't
  sync with), and undoes that afterwards.
- The built-in player's level is evened out, so a lowered group volume no longer flattens the picture.
- `music_assistant` replaces `squeezelite_server` for most setups (`squeezelite_server` still works and wins).
- Fixed: the app noticed a kiosk leaving only when the next frame was sent.

  After merging: update the app. Nothing changes until you add `channels`; a single visualizer works as before and
  keeps its settings. To follow speakers, set `music_assistant` and a token, then add channels with `follow`
  (see DOCS.md). Kiosks for a channel use `/stream?channel=<name>`. Old built-in "Visualizer" player: with channels,
  new ones named "Visualizer <channel>" appear in Music Assistant; the old one can be removed there.

- CLAUDE.md spells out the coworker workflow (branch + PR, Adam merges, Home Assistant changes arrive as files). Permission rules also block `gh pr merge` and merging through the GitHub API.
  After merging: nothing.
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
