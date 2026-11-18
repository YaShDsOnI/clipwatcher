"""Clip files on disk: one folder per streamer, retention (old sources/clips), disk-space checks.

Retention is set in the dashboard's Setup tab (saved in .env):
  KEEP_SOURCES_DAYS  delete a clip's 1-minute source after this many days (default 7; 0 = keep).
                     The clip itself stays, it just can't be trimmed or re-rendered any more.
  KEEP_CLIPS_DAYS    delete whole clips after this many days (default 0 = keep forever).
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

from .config import (CACHE_DIR, CLIP_DIR, HOME, META_DIR, SOURCE_DIR, THUMB_DIR, WORK_DIR, channel_of, clip_file,
                     env_int, meta_path_for, project_path_for)

DAY = 86400
DEFAULT_KEEP_SOURCES_DAYS = 7
DEFAULT_KEEP_CLIPS_DAYS = 0
LOW_DISK_GB = 3.0                 # below this, new clips are skipped so the disk never fills up completely


def retention() -> dict[str, int]:
    return {"keep_sources_days": max(0, env_int("KEEP_SOURCES_DAYS", DEFAULT_KEEP_SOURCES_DAYS)),
            "keep_clips_days": max(0, env_int("KEEP_CLIPS_DAYS", DEFAULT_KEEP_CLIPS_DAYS))}


def free_gb() -> float:
    try:
        return shutil.disk_usage(HOME if HOME.exists() else HOME.anchor).free / 1e9
    except OSError:
        return float("inf")


def low_disk() -> bool:
    return free_gb() < LOW_DISK_GB


def _read_meta(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clip_files(name: str, channel: str | None = None) -> tuple[Path, ...]:
    """Every file that belongs to one clip: the clip, its captions, thumbnail, source and job, plus
    its editor project, timeline cache and exported edits."""
    p = clip_file(name, channel)
    extra: list[Path] = [project_path_for(name)]
    if CACHE_DIR.is_dir():
        extra += CACHE_DIR.glob(f"{p.stem}.*")
    edits = p.parent / "edits"
    if edits.is_dir():
        extra += edits.glob(f"{p.stem}_edit-*.mp4")
    return (p, p.with_suffix(".srt"), THUMB_DIR / (p.stem + ".jpg"), SOURCE_DIR / name, meta_path_for(name), *extra)


# --------------------------------------------------------------------------- one folder per streamer

def migrate_flat_clips() -> int:
    """Clips made before streamer folders existed sit directly in clips/: move each into
    clips/<channel>/. Safe to call any time; a file that's in use is simply moved next time."""
    if not CLIP_DIR.is_dir():
        return 0
    moved = 0
    for p in [*CLIP_DIR.glob("*.mp4"), *CLIP_DIR.glob("*.srt")]:
        channel = channel_of(p.name) or (_read_meta(meta_path_for(p.name)) or {}).get("channel")
        target = clip_file(p.name, channel)
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            p.replace(target)
            moved += 1
        except OSError:
            pass                      # open in the player right now, or moved by another process
    return moved


def finished_clips() -> list[Path]:
    """Every finished clip in the streamer folders (not the hidden working folders)."""
    if not CLIP_DIR.is_dir():
        return []
    return [p for p in CLIP_DIR.glob("*/*.mp4") if not p.parent.name.startswith(".")]


# --------------------------------------------------------------------------- retention

def _created(meta: dict, fallback: Path) -> float:
    try:
        return datetime.fromisoformat(meta["created"]).timestamp()
    except (KeyError, TypeError, ValueError):
        try:
            return fallback.stat().st_mtime
        except OSError:
            return time.time()


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:                   # in use (Windows): try again at the next cleanup
        return False


def delete_clip(name: str, channel: str | None = None) -> bool:
    """Remove one clip and everything that belongs to it. False if the clip file is in use."""
    files = clip_files(name, channel)
    if files[0].exists() and not _unlink(files[0]):
        return False
    for f in files[1:]:
        _unlink(f)
    return True


def cleanup(now: float | None = None) -> dict[str, int]:
    """Apply the retention settings once. Clips that are still queued or rendering are never touched."""
    now = now if now is not None else time.time()
    keep = retention()
    src_cut = now - keep["keep_sources_days"] * DAY if keep["keep_sources_days"] else None
    clip_cut = now - keep["keep_clips_days"] * DAY if keep["keep_clips_days"] else None
    removed = {"sources": 0, "clips": 0}
    with_meta: set[str] = set()

    for mp in sorted(META_DIR.glob("*.json")) if META_DIR.is_dir() else []:
        meta = _read_meta(mp)
        if meta is None:
            continue
        name = meta.get("name") or mp.stem + ".mp4"
        with_meta.add(name)
        if meta.get("status") in ("queued", "rendering"):
            continue
        created = _created(meta, mp)
        if clip_cut is not None and created < clip_cut:
            if delete_clip(name, meta.get("channel")):
                removed["clips"] += 1
            continue
        if src_cut is not None and created < src_cut and _unlink(SOURCE_DIR / name):
            removed["sources"] += 1

    if clip_cut is not None:          # old clips that have no job file (made by very early versions)
        for p in finished_clips():
            try:
                old = p.name not in with_meta and p.stat().st_mtime < clip_cut
            except OSError:
                continue
            if old and delete_clip(p.name, p.parent.name):
                removed["clips"] += 1

    if THUMB_DIR.is_dir():            # thumbnails whose clip is gone (they're re-made on demand)
        existing = {p.stem for p in finished_clips()}
        for t in THUMB_DIR.glob("*.jpg"):
            if t.stem not in existing:
                _unlink(t)
    if WORK_DIR.is_dir():             # leftovers from a render that crashed
        for w in WORK_DIR.glob("*.*"):
            try:
                if w.is_file() and w.stat().st_mtime < now - DAY:
                    _unlink(w)
            except OSError:
                pass
    return removed


def usage() -> dict:
    """What the clips take on disk, per streamer, for the Setup tab."""
    per_channel: dict[str, dict[str, int]] = {}
    for p in finished_clips():
        try:
            size = p.stat().st_size
        except OSError:
            continue
        row = per_channel.setdefault(p.parent.name, {"clips": 0, "bytes": 0})
        row["clips"] += 1
        row["bytes"] += size
    for p in CLIP_DIR.glob("*/edits/*.mp4") if CLIP_DIR.is_dir() else []:   # exported edits count too
        try:
            per_channel.setdefault(p.parent.parent.name, {"clips": 0, "bytes": 0})["bytes"] += p.stat().st_size
        except OSError:
            pass
    sources = 0
    if SOURCE_DIR.is_dir():
        for s in SOURCE_DIR.glob("*.mp4"):
            try:
                sources += s.stat().st_size
            except OSError:
                pass
    return {"channels": dict(sorted(per_channel.items())), "sources_bytes": sources,
            "clips_bytes": sum(r["bytes"] for r in per_channel.values()),
            "free_gb": round(free_gb(), 1), "low_disk_gb": LOW_DISK_GB,
            "folder": str(CLIP_DIR), **retention()}
