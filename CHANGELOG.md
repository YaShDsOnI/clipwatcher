# Changelog

All notable changes to clipwatcher. Versions follow [semantic versioning](https://semver.org/).

## [0.3.0] - 2026-11-19

The first public release.

### Added
- **Video editor** in the dashboard (Clipchamp-style). The original clip is never changed: every export is
  a new MP4 in `clips/<streamer>/edits/`.
  - Timeline with thumbnails and waveform; trim, split (S), cut out parts (Del).
  - Move parts along the timeline to leave blank space, or drag them past each other to reorder.
  - Speed per part (0.5×–2×) and zoom punch-ins.
  - Text layers you drag on the video (4 presets), captions re-timed to your cuts.
  - 9:16 / 4:5 / 1:1, layouts, zoom & pan.
  - Music and sounds: drag & drop or upload, with volume and fades; the video's own volume from its Sound track.
  - Undo/redo, keyboard shortcuts, autosave, export with progress and download.
  - Works from the 1-minute source while it exists, otherwise from the finished 30-second clip.
- Streamers' Twitch profile pictures in the dashboard, with a live ring while recording.
- Redesigned dashboard: status pills, light/dark/auto theme, clips grouped by day, "Best first" sort,
  storage bars per streamer, phone layout.
- Clips saved in one folder per streamer (`clips/<streamer>/`).
- Spike merging, a gap between clips and an optional hourly cap; per-chatter cap and bot filter.
- Retention settings for sources and clips; new clips pause when the disk is almost full.
- `clip-watcher` command (`dashboard`, `watch`, `render`, `setup`), installable with pip.

### Changed
- Default sensitivity (`--ratio`) is 1.7 instead of 2.5.
- "New title" only changes the title shown in the dashboard; it never re-renders the video.
- Claude requests step down to simpler request shapes instead of relying on a list of model names.

### Security
- The dashboard refuses requests from other websites and DNS-rebinding attempts (Host, Origin and
  Sec-Fetch-Site checks; JSON-only bodies).

## [0.2.0] - 2026-11-18

- Restructured into the `clipwatcher` package with `pyproject.toml`, tests and CI.

[0.3.0]: https://github.com/YaShDsOnI/clipwatcher/releases/tag/v0.3.0
