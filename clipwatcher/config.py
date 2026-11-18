"""Where clip-watcher keeps its files, the settings in .env, and how it starts its own helpers.

Two kinds of files:
  * the program itself (this package, plus web/ and fonts/ inside it), and
  * your data: clips, recordings, downloaded models, logs, settings (.env, streamers.json).

The data folder ("home") is, in this order:
  1. CLIP_WATCHER_HOME, if set;
  2. the project folder, when running from a download or a git checkout (the folder that holds
     pyproject.toml or "Start clip-watcher.bat" next to this package);
  3. ~/clip-watcher, when installed with pip as a regular package.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
WEB_DIR = PKG_DIR / "web"
FONT_DIR = PKG_DIR / "fonts"


def _home() -> Path:
    if os.environ.get("CLIP_WATCHER_HOME"):
        return Path(os.environ["CLIP_WATCHER_HOME"]).expanduser().resolve()
    project = PKG_DIR.parent
    if (project / "pyproject.toml").exists() or (project / "Start clip-watcher.bat").exists():
        return project
    return Path.home() / "clip-watcher"


HOME = _home()
CLIP_DIR = HOME / "clips"               # clips/<channel>/<clip>.mp4 (+ .srt): the finished clips
SOURCE_DIR = CLIP_DIR / ".sources"      # source cut per clip, kept so clips can be re-edited
META_DIR = CLIP_DIR / ".meta"           # one job/metadata JSON per clip
WORK_DIR = CLIP_DIR / ".work"           # temporary audio / captions / renders
THUMB_DIR = CLIP_DIR / ".thumbs"        # gallery thumbnails
REC_DIR = HOME / "rec"                  # full stream recordings while a stream is live
RUN_DIR = HOME / "run"                  # locks, stop files, chat history between restarts
LOG_DIR = HOME / "logs"                 # one log per streamer (written by the dashboard)
MODEL_DIR = HOME / "models"             # downloaded speaker models
ENV_FILE = HOME / ".env"                # AI keys and global settings
STREAMERS_FILE = HOME / "streamers.json"

# <channel>_<YYYYmmdd-HHMMSS>_<N>msgs[_suffix].mp4; Twitch names may contain "_", the date anchors it
CLIP_NAME_RE = re.compile(r"(\w+?)_(\d{8}-\d{6})_(\d+)msgs(?:_\w+)?\.(?:mp4|srt)")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- clip files

def channel_of(name: str) -> str | None:
    """The streamer a clip file belongs to, from its name."""
    m = CLIP_NAME_RE.fullmatch(name)
    return m.group(1).lower() if m else None


def clip_file(name: str, channel: str | None = None) -> Path:
    """Where a finished clip lives: clips/<channel>/<name>. One folder per streamer, so the clips
    of one streamer are easy to find and copy."""
    ch = (channel or channel_of(name) or "other").lower()
    ch = re.sub(r"\W", "", ch) or "other"
    return CLIP_DIR / ch / name


def meta_path_for(clip_name: str) -> Path:
    return META_DIR / (Path(clip_name).stem + ".json")


def is_plain_name(name: str, suffixes: tuple[str, ...] = ("mp4",)) -> bool:
    """Only plain clip file names (no folders, no '..', no hidden files)."""
    return bool(re.fullmatch(r"[\w.-]+\.(?:" + "|".join(suffixes) + ")", name)) and not name.startswith(".")


# --------------------------------------------------------------------------- .env settings

def read_env_file() -> dict[str, str]:
    """KEY=VALUE pairs from .env (where the dashboard saves the AI and storage settings)."""
    out: dict[str, str] = {}
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def env_setting(key: str, default: str | None = None) -> str | None:
    """A setting from .env, else the environment, else the default (.env wins: the dashboard writes it)."""
    return read_env_file().get(key) or os.environ.get(key) or default


def env_int(key: str, default: int) -> int:
    try:
        return int(str(env_setting(key, str(default))).strip())
    except ValueError:
        return default


def write_env(updates: dict[str, str | None]) -> None:
    """Set (or with None remove) KEY=VALUE lines in .env, keeping every other line as it is."""
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    out, done = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            done.add(key)
            if updates[key] is not None:
                out.append(f"{key}={updates[key]}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in done and v is not None]
    HOME.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- helper processes

def self_cmd(*args: str) -> list[str]:
    """Command line that runs clip-watcher itself (e.g. self_cmd("render", job)) in a new process."""
    return [sys.executable, "-u", "-m", "clipwatcher", *args]


def child_env(**extra: str) -> dict[str, str]:
    """Environment for helper processes: they find this package and use the same data folder."""
    env = dict(os.environ, **extra)
    project = str(PKG_DIR.parent)
    paths = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if project not in paths:
        env["PYTHONPATH"] = os.pathsep.join([project, *paths])
    env["CLIP_WATCHER_HOME"] = str(HOME)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def low_priority() -> dict:
    """Popen arguments that run a helper below normal priority, so recording, Docker etc. stay smooth."""
    if os.name == "nt":
        return {"creationflags": 0x00004000}         # BELOW_NORMAL_PRIORITY_CLASS
    return {"preexec_fn": lambda: os.nice(10)}
