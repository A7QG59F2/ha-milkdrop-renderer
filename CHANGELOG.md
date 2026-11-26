# Changelog

Newest first.

## Unreleased

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
