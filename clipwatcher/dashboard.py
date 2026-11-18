"""clip-watcher dashboard: a local web page to manage streamers and watch the clips.

    clip-watcher dashboard            then open http://localhost:8765

Every streamer you start runs its own `clip-watcher watch` process; stopping one asks it to
finish its queued clips and save its recording first. Only reachable from this computer.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# only paths, constants and the job format here: recording and the AI run in their own processes
from . import ai, components, storage
from .captions import TEMPLATES as CAPTION_TEMPLATES
from .captions import Word
from .clipper import media_duration, save_meta
from .config import (CLIP_DIR, CLIP_NAME_RE, HOME, LOG_DIR, META_DIR, RUN_DIR, SOURCE_DIR, STREAMERS_FILE,
                     THUMB_DIR, WEB_DIR, channel_of, child_env, clip_file, is_plain_name, log, meta_path_for,
                     read_env_file, self_cmd, write_env)

CHANNEL_RE = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:twitch\.tv/)?#?(\w{1,25})/?", re.I)
BUCKET_RE = re.compile(r"^(\w+)\s+(\d\d:\d\d:\d\d)\s+(\d+) msgs\s+median\s+([\d.]+)")
CLIP_LINE_RE = re.compile(r"^\w+: clip (\d\d:\d\d:\d\d) \(\d+ msgs\) -> ")
LINE_TIME_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (.*)$")
CLEANUP_EVERY = 3600              # seconds between retention cleanups

DEFAULTS = {"min": 10, "ratio": 2.5, "quality": "best", "layout": "blur", "template": "karaoke",
            "title": "auto", "facecam": None, "captions": True, "speakers": True, "caption_words": 4,
            "keep_recordings": False, "title_in_video": False, "gap": 30, "max_per_hour": 0}
QUALITIES = ["best", "1080p60", "720p", "480p"]
LAYOUTS = {"blur": "Full frame on blurred background", "crop": "Center crop (fills the screen)",
           "split": "Streamer split: facecam on top, gameplay below"}
TEMPLATES = {k: v[0] for k, v in CAPTION_TEMPLATES.items()}
TITLE_MODES = {"auto": "AI (from the AI tab) if set up, else from chat", "chat": "From chat (free, no AI)", "off": "Off"}


def clean_facecam(raw) -> list[float] | None:
    try:
        box = [round(float(v), 4) for v in raw]
        if len(box) == 4 and all(0 <= v <= 1 for v in box) and box[2] > 0.02 and box[3] > 0.02:
            return box
    except (TypeError, ValueError):
        pass
    return None


def clean_settings(raw: dict | None) -> dict:
    """Validate settings from the page; anything missing or odd falls back to the default."""
    raw = raw or {}
    s = dict(DEFAULTS)

    def num(key, cast, lo, hi):
        try:
            v = cast(raw[key])
            if lo <= v <= hi:
                s[key] = v
        except (KeyError, TypeError, ValueError):
            pass

    num("min", int, 1, 10_000)
    num("ratio", float, 1.0, 20.0)
    num("caption_words", int, 1, 8)
    num("gap", int, 0, 3600)
    num("max_per_hour", int, 0, 1000)
    if raw.get("quality") in QUALITIES:
        s["quality"] = raw["quality"]
    if raw.get("layout") in LAYOUTS:
        s["layout"] = raw["layout"]
    if raw.get("template") in TEMPLATES:
        s["template"] = raw["template"]
    if raw.get("title") in TITLE_MODES:
        s["title"] = raw["title"]
    s["facecam"] = clean_facecam(raw.get("facecam"))
    for key in ("captions", "speakers", "keep_recordings", "title_in_video"):
        if isinstance(raw.get(key), bool):
            s[key] = raw[key]
    return s


# --------------------------------------------------------------------------- watchers

class Watcher:
    """One `clip-watcher watch` process for one streamer, plus what we learn from its output."""

    def __init__(self, channel: str, settings: dict):
        self.channel = channel
        self.settings = settings
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=200)
        self.buckets: deque[dict] = deque(maxlen=36)      # last 6 minutes of chat activity
        self.status = "stopped"      # stopped | starting | waiting | recording | stopping | error
        self.started_at: float | None = None
        self.spikes = 0
        self.clips_made = 0
        self.last_clip: str | None = None
        self.stop_requested = False
        self.warming: list[int] | None = None      # [done, needed] while the detector warms up
        self.lock = threading.Lock()

    @property
    def stop_file(self) -> Path:
        return RUN_DIR / f"{self.channel}.stop"

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def command(self) -> list[str]:
        s = self.settings
        layout = s["layout"] if s["layout"] != "split" or s.get("facecam") else "blur"
        cmd = self_cmd("watch", self.channel,
                       "--min", str(s["min"]), "--ratio", str(s["ratio"]), "--quality", s["quality"],
                       "--layout", layout, "--template", s["template"], "--title", s["title"],
                       "--caption-words", str(s["caption_words"]), "--gap", str(s.get("gap", 30)),
                       "--max-per-hour", str(s.get("max_per_hour", 0)), "--stop-file", str(self.stop_file))
        if s.get("facecam"):
            cmd += ["--facecam", ",".join(str(v) for v in s["facecam"])]
        if not s["captions"]:
            cmd.append("--no-captions")
        if not s["speakers"]:
            cmd.append("--no-speakers")
        if s.get("keep_recordings"):
            cmd.append("--keep-recordings")
        if s.get("title_in_video"):
            cmd.append("--title-in-video")
        return cmd

    def recording_path(self) -> Path | None:
        """The recording this watcher is writing right now, if any (from its log)."""
        for line in reversed(self.lines):
            if "recording started: " in line:
                p = HOME / line.split("recording started: ", 1)[1].strip().replace("\\", "/")
                return p if p.exists() else None
            if "stream ended" in line or "recording deleted" in line:
                return None
        return None

    def start(self, keep_stats: bool = False) -> None:
        with self.lock:
            if self.running:
                return
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self.stop_file.unlink(missing_ok=True)
            if not keep_stats:
                self.lines.clear()
                self.buckets.clear()
                self.spikes = self.clips_made = 0
            self.stop_requested = False
            env = child_env(HF_HUB_DISABLE_SYMLINKS_WARNING="1", CLIP_WATCHER_NONINTERACTIVE="1")
            kw = {}
            if os.name == "nt":
                # own process group (Ctrl+C in the dashboard's terminal doesn't hit it) and its own
                # hidden console, so it starts even when the dashboard has no console window
                kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                kw["start_new_session"] = True
            self.proc = subprocess.Popen(
                self.command(), cwd=HOME, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
            self.status = "starting"
            self.started_at = time.time()
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
            log(f"{self.channel}: started (pid {self.proc.pid})")

    def restart(self) -> None:
        """Apply new settings to a running watcher: stop it (its queued clips are finished
        first), then start it again. It keeps its chat history, so there is no warm-up."""
        self.stop()
        proc = self.proc
        if proc is not None:
            proc.wait()
        self.start(keep_stats=True)
        log(f"{self.channel}: restarted with the new settings")

    def clip_now(self) -> None:
        """Ask the running watcher to clip the last 20 s (and the next 10 s) right now."""
        if not self.running or self.stop_requested:
            raise ValueError("Start recording this streamer first.")
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / f"{self.channel}.clipnow").write_text(str(time.time()))

    def stop(self) -> None:
        """Ask the watcher to finish its queued clips, save the recording and exit."""
        with self.lock:
            if not self.running:
                return
            self.stop_requested = True
            self.status = "stopping"
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            self.stop_file.touch()
            log(f"{self.channel}: stop requested")

    def kill(self) -> None:
        if self.running:
            self.proc.kill()

    def _read(self, proc: subprocess.Popen) -> None:
        with open(LOG_DIR / f"{self.channel}.log", "a", encoding="utf-8") as logf:
            logf.write(f"\n=== started {datetime.now():%Y-%m-%d %H:%M:%S}: {' '.join(self.command())}\n")
            for raw in proc.stdout:
                line = raw.rstrip()
                if not line:
                    continue
                logf.write(line + "\n")
                logf.flush()
                self._parse(line)
        code = proc.wait()
        self.stop_file.unlink(missing_ok=True)
        if self.stop_requested or code == 0:
            self.status = "stopped"
        else:
            self.status = "error"
            self.lines.append(f"[{datetime.now():%H:%M:%S}] watcher exited unexpectedly (code {code})")
        log(f"{self.channel}: exited (code {code})")

    def _parse(self, line: str) -> None:
        self.lines.append(line)
        m = LINE_TIME_RE.match(line)
        msg = m.group(2) if m else line
        if b := BUCKET_RE.match(msg):
            self.buckets.append({"t": b.group(2), "msgs": int(b.group(3)), "median": float(b.group(4)),
                                 "spike": "SPIKE" in msg, "clip": False})
            w = re.search(r"\(warming up (\d+)/(\d+)\)", msg)
            self.warming = [int(w.group(1)), int(w.group(2))] if w else None
        elif c := CLIP_LINE_RE.match(msg):
            # a moment became a clip once its peak was known: mark the peak's bucket in the chart
            self.spikes += 1
            for bucket in reversed(self.buckets):
                if bucket["t"] == c.group(1):
                    bucket["clip"] = True
                    break
        if msg.startswith("clip saved: "):
            self.clips_made += 1
            self.last_clip = Path(msg.split("clip saved: ", 1)[1].split(" (", 1)[0]).name
        if self.status == "stopping":
            return
        if "recording started" in msg:
            self.status = "recording"
        elif ("stream ended" in msg or "waiting for the stream" in msg
              or "exited without recording" in msg):
            self.status = "waiting"
        elif msg.startswith("watching #") and self.status == "starting":
            self.status = "waiting"
        elif msg.startswith("stopping"):
            self.status = "stopping"

    def info(self) -> dict:
        return {
            "channel": self.channel,
            "settings": self.settings,
            "status": self.status if self.running or self.status in ("error",) else "stopped",
            "running": self.running,
            "started_at": self.started_at if self.running else None,
            "buckets": list(self.buckets),
            "warming": self.warming if self.running else None,
            "spikes": self.spikes,
            "clips_made": self.clips_made,
            "last_clip": self.last_clip,
            "log": list(self.lines)[-60:],
        }


class Manager:
    def __init__(self):
        self.watchers: dict[str, Watcher] = {}
        self.lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(STREAMERS_FILE.read_text(encoding="utf-8"))
            for item in data.get("streamers", []):
                ch = item.get("channel", "")
                if CHANNEL_RE.fullmatch(ch):
                    self.watchers[ch] = Watcher(ch, clean_settings(item.get("settings")))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        data = {"streamers": [{"channel": w.channel, "settings": w.settings} for w in self.watchers.values()]}
        STREAMERS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add(self, raw_channel: str, settings: dict, start: bool) -> Watcher:
        m = CHANNEL_RE.fullmatch((raw_channel or "").strip())
        if not m:
            raise ValueError("That doesn't look like a Twitch channel name or link.")
        ch = m.group(1).lower()
        with self.lock:
            w = self.watchers.get(ch)
            if w is None:
                w = self.watchers[ch] = Watcher(ch, clean_settings(settings))
            elif not w.running:
                w.settings = clean_settings(settings)
            self.save()
        if start:
            w.start()
        return w

    def get(self, ch: str) -> Watcher:
        w = self.watchers.get(ch)
        if w is None:
            raise KeyError(ch)
        return w

    def update(self, ch: str, settings: dict) -> Watcher:
        w = self.get(ch)
        if w.stop_requested and w.running:
            raise ValueError("This streamer is stopping; change the settings once it has stopped.")
        w.settings = clean_settings(settings)
        self.save()
        if w.running:                     # apply now: restart in the background, history is kept
            threading.Thread(target=w.restart, daemon=True).start()
        return w

    def remove(self, ch: str) -> None:
        w = self.get(ch)
        if w.running:
            raise ValueError("Stop the streamer before removing it.")
        with self.lock:
            del self.watchers[ch]
            self.save()

    def shutdown(self, timeout: float = 180) -> None:
        running = [w for w in self.watchers.values() if w.running]
        if not running:
            return
        log(f"stopping {len(running)} watcher(s): finishing their queued clips...")
        for w in running:
            w.stop()
        deadline = time.time() + timeout
        while any(w.running for w in running) and time.time() < deadline:
            time.sleep(0.5)
        for w in running:
            if w.running:
                log(f"{w.channel}: did not stop in time, killing it")
                w.kill()


# --------------------------------------------------------------------------- clips

def read_meta(name: str) -> dict | None:
    try:
        return json.loads(meta_path_for(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def channel_for(name: str, meta: dict | None = None) -> str | None:
    """The streamer folder a clip lives in: from its name, else from its job file."""
    return channel_of(name) or (meta if meta is not None else read_meta(Path(name).stem + ".mp4") or {}).get("channel")


def clip_info(name: str, meta: dict | None) -> dict:
    p = clip_file(name, channel_for(name, meta))
    m = CLIP_NAME_RE.fullmatch(name)
    st = p.stat() if p.exists() else None
    if m:
        channel, stamp, msgs = m.group(1), m.group(2), int(m.group(3))
        when = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    else:
        channel, msgs = (meta or {}).get("channel", "other"), (meta or {}).get("msgs", 0)
        when = datetime.fromtimestamp(st.st_mtime if st else time.time())
    srt = p.with_suffix(".srt")
    info = {
        "name": name, "channel": channel, "msgs": msgs, "folder": p.parent.relative_to(HOME).as_posix(), "path": str(p),
        "time": when.isoformat(timespec="seconds"),
        "size": st.st_size if st else 0, "version": int(st.st_mtime) if st else 0,
        "ready": st is not None, "has_srt": srt.exists(), "text": srt_text(srt),
        "status": "done" if st else "queued", "editable": False,
    }
    if meta:
        source = HOME / meta.get("source", "")
        ai = meta.get("ai") or {}
        info.update({
            "status": meta.get("status", info["status"]), "error": meta.get("error"),
            "title": meta.get("title"), "hype": meta.get("hype"), "median": meta.get("median"),
            "speakers": meta.get("window_speakers", meta.get("speakers")),
            "scores": ai.get("scores"), "virality": ai.get("virality"), "post": ai.get("post"),
            "title_by": ai.get("by") or ("chat" if meta.get("title") else None),
            "settings": meta.get("settings"), "start": meta.get("start"), "end": meta.get("end"),
            "editable": source.is_file(),
        })
    return info


def list_clips() -> list[dict]:
    names = {p.name for p in storage.finished_clips()}
    metas = {}
    if META_DIR.exists():
        for mp in META_DIR.glob("*.json"):
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            metas[meta.get("name", mp.stem + ".mp4")] = meta
    for name, meta in metas.items():      # clips still being made show up too
        if meta.get("status") in ("queued", "rendering", "failed"):
            names.add(name)
    out = [clip_info(n, metas.get(n)) for n in names]
    out.sort(key=lambda c: c["time"], reverse=True)
    return out


def source_path(name: str) -> Path | None:
    if not is_plain_name(name):
        return None
    p = SOURCE_DIR / name
    return p if p.is_file() else None


_rendering: set[str] = set()


def is_rendering() -> bool:
    """Is any clip being processed right now (by a recorder or a re-render)?"""
    if _rendering:
        return True
    if META_DIR.exists():
        for mp in META_DIR.glob("*.json"):
            try:
                if '"status": "rendering"' in mp.read_text(encoding="utf-8"):
                    return True
            except OSError:
                pass
    return False


def start_rerender(name: str, changes: dict) -> dict:
    """Apply edits to a clip's job and re-render it in a background, low-priority process."""
    meta = read_meta(name)
    source = source_path(name)
    if meta is None or source is None:
        raise ValueError("This clip has no saved source (it was made before editing existed).")
    if name in _rendering or meta.get("status") in ("queued", "rendering"):
        raise ValueError("This clip is already being rendered.")
    dur = media_duration(source)
    s = meta.setdefault("settings", {})
    try:
        start = float(changes.get("start", meta["start"]))
        end = float(changes.get("end", meta["end"]))
    except (TypeError, ValueError):
        raise ValueError("Start and end must be numbers of seconds.") from None
    start, end = max(0.0, start), min(dur, end)
    if end - start < 3:
        raise ValueError("A clip must be at least 3 seconds long.")
    if end - start > 120:
        raise ValueError("A clip can be at most 2 minutes long.")
    meta["start"], meta["end"] = round(start, 2), round(end, 2)
    if changes.get("layout") in LAYOUTS:
        s["layout"] = changes["layout"]
    if changes.get("template") in TEMPLATES:
        s["template"] = changes["template"]
    if isinstance(changes.get("captions"), bool):
        s["captions"] = changes["captions"]
    if isinstance(changes.get("title_in_video"), bool):
        s["title_in_video"] = changes["title_in_video"]
    if "facecam" in changes:
        s["facecam"] = clean_facecam(changes["facecam"])
    if s.get("layout") == "split" and not s.get("facecam"):
        raise ValueError("Draw the facecam box first (split layout).")
    if "title" in changes:                # the text as typed: shown as is, empty = no title
        meta["title"] = (str(changes["title"]).strip()[:80] or None)
        s["title"] = "custom"
    retitle = bool(changes.get("retitle"))
    if retitle:
        s["title"] = "auto"
    meta["status"] = "queued"
    save_meta(meta_path_for(name), meta)
    _rendering.add(name)

    def run():
        cmd = self_cmd("render", str(meta_path_for(name)))
        if retitle:
            cmd.append("--retitle")
        kw = {"creationflags": 0x00004000 | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            res = subprocess.run(cmd, cwd=HOME, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", env=child_env(), **kw)
            for line in res.stdout.strip().splitlines()[-3:]:
                log(f"re-render: {line}")
        finally:
            _rendering.discard(name)

    threading.Thread(target=run, daemon=True).start()
    log(f"re-rendering {name}")
    return clip_info(name, meta)


# --------------------------------------------------------------------------- AI provider (.env)

def ai_state() -> dict:
    env = {**os.environ, **read_env_file()}
    cfg = ai.ai_config()
    providers = []
    for pid, p in ai.PROVIDERS.items():
        key = env.get(p["key"], "")
        providers.append({
            "id": pid, "label": p["label"], "model": p["model"], "url": p["url"], "signup": p["signup"],
            "free": p["free"], "has_key": bool(key), "key_hint": f"…{key[-4:]}" if len(key) > 8 else ("set" if key else ""),
            "needs_package": pid == "claude" and importlib.util.find_spec("anthropic") is None,
        })
    return {
        "choice": env.get("AI_PROVIDER", "auto").lower(),
        "model": env.get("AI_MODEL", ""), "base_url": env.get("AI_BASE_URL", ""),
        "providers": providers,
        "active": {"provider": cfg["provider"], "label": ai.PROVIDERS[cfg["provider"]]["label"],
                   "model": cfg["model"]} if cfg else None,
    }


def save_ai(body: dict) -> None:
    choice = str(body.get("provider", "auto")).lower()
    if choice not in ("auto", "chat", *ai.PROVIDERS):
        raise ValueError("Unknown AI provider.")

    def clean(v, limit=300):
        v = str(v or "").strip()
        if any(c in v for c in "\r\n\0") or len(v) > limit:
            raise ValueError("That value can't be saved (line breaks or too long).")
        return v

    updates: dict[str, str | None] = {"AI_PROVIDER": choice}
    if choice in ai.PROVIDERS:
        preset = ai.PROVIDERS[choice]
        model = clean(body.get("model"), 120)
        updates["AI_MODEL"] = model if model and model != preset["model"] else None
        if choice == "custom":
            url = clean(body.get("base_url"))
            if url and not re.match(r"https?://", url):
                raise ValueError("The base URL must start with http:// or https://")
            updates["AI_BASE_URL"] = url or None
        key = clean(body.get("key"), 400)
        if key:
            updates[preset["key"]] = key
        if body.get("remove_key"):
            updates[preset["key"]] = None
    write_env(updates)
    log(f"AI provider set to {choice}")


def test_ai() -> dict:
    """One small real request with the current settings, so a wrong key or model shows up now."""
    cfg = ai.ai_config()
    if cfg is None:
        return {"ok": False, "error": "No AI is set up: titles will come from chat (that's fine and free)."}
    meta = {"channel": "teststreamer", "msgs": 48, "median": 9, "start": 0, "end": 30,
            "chat": [{"text": t} for t in ["NO WAY", "CLIP IT", "he actually hit that", "NO WAY", "W", "INSANE"]]}
    words = [Word(1.0 + i * 0.4, 1.3 + i * 0.4, w) for i, w in
             enumerate("no way I just hit a one in a million shot chat did you see that".split())]
    t0 = time.time()
    try:
        r = ai.ai_rate(meta, words, cfg)
    except ai.AIUnavailable as e:
        return {"ok": False, "error": str(e), "provider": cfg["provider"], "model": cfg["model"]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "provider": cfg["provider"], "model": cfg["model"]}
    return {"ok": True, "provider": cfg["provider"], "model": cfg["model"], "seconds": round(time.time() - t0, 1),
            "title": r["title"], "virality": r["virality"], "scores": r["scores"], "post": r["post"]}


# --------------------------------------------------------------------------- setup (opt-in installs)

_setup_jobs: dict[str, dict] = {}       # component id -> {"status": running|done|failed, "log": [...]}


def setup_state() -> dict:
    comps = components.components()
    for c in comps:
        job = _setup_jobs.get(c["id"])
        if job:
            c["job"] = {"status": job["status"], "log": job["log"][-6:]}
    return {"components": comps, "whisper_model": components.whisper_model_name(),
            "python": components.python_label(),
            "sizes": {k: v[0] for k, v in components.WHISPER_SIZES.items()}}


def start_install(cid: str) -> None:
    """Runs only when the user clicked (and confirmed) Download/Install for this part."""
    comp = next((c for c in components.components() if c["id"] == cid), None)
    if comp is None:
        raise ValueError("Unknown part.")
    if comp.get("manual"):
        raise ValueError(f"Install {comp['label']} yourself: {comp['manual']}")
    if _setup_jobs.get(cid, {}).get("status") == "running":
        raise ValueError("Already in progress.")
    need = comp.get("needs")
    if need and not next(c for c in components.components() if c["id"] == need)["installed"]:
        raise ValueError(f"Install {need} first.")
    job = _setup_jobs[cid] = {"status": "running", "log": []}

    def run():
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        proc = subprocess.Popen(self_cmd("setup", cid, "--yes"),
                                cwd=HOME, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                env=child_env(HF_HUB_DISABLE_SYMLINKS_WARNING="1"), **kw)
        for line in proc.stdout:
            line = line.rstrip()
            if line and "nothing is downloaded" not in line and not line.lstrip().startswith(
                    ("Downloading", "Using cached", "Collecting", "Requirement already", "━", "-")):
                job["log"].append(line.strip()[-300:])
        code = proc.wait()
        importlib.invalidate_caches()
        installed = next(c for c in components.components() if c["id"] == cid)["installed"]
        job["status"] = "done" if code == 0 and installed else "failed"
        if job["status"] == "failed":
            why = next((ln.split("FAILED: ", 1)[1] for ln in reversed(job["log"]) if "FAILED: " in ln), None)
            if code == 0 and not installed:
                why = ("it installed, but this dashboard can't see it yet: close the black window "
                       "and start clip-watcher again.")
                job["log"].append("FAILED: " + why)
            log(f"setup: {cid} failed: {why or (job['log'][-1] if job['log'] else 'no output')}")
        else:
            log(f"setup: {cid} done")

    threading.Thread(target=run, daemon=True).start()
    log(f"setup: {cid} approved by the user, installing")


def features_off() -> list[str]:
    """What's switched off because a part isn't downloaded yet (shown on the page)."""
    off = []
    model = components.whisper_model_name()
    if not components.has_module("faster_whisper"):
        off.append("Captions are off: faster-whisper isn't installed.")
    elif not components.whisper_downloaded(model):
        off.append(f"Captions are off: the Whisper '{model}' model isn't downloaded.")
    if not components.has_module("sherpa_onnx"):
        off.append("Speaker colours are off: sherpa-onnx isn't installed.")
    elif not components.speaker_models_downloaded():
        off.append("Speaker colours are off: the speaker models aren't downloaded.")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        off.append("Clips can't be made: ffmpeg isn't installed (winget install Gyan.FFmpeg).")
    if storage.low_disk():
        off.append(f"New clips are paused: only {storage.free_gb():.1f} GB of disk space is free "
                   f"(needs {storage.LOW_DISK_GB:g} GB). Delete clips or shorten how long they're kept.")
    return off


_frame_lock = threading.Lock()


def grab_frame(channel: str, watcher: "Watcher | None") -> Path | None:
    """A recent full frame of a streamer, for drawing the facecam box: from the live recording,
    else from their newest clip source, else a quick grab from the live stream."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    out = RUN_DIR / f"frame_{channel}.jpg"
    with _frame_lock:
        rec = watcher.recording_path() if watcher else None
        sources = sorted(SOURCE_DIR.glob(f"{channel}_*.mp4"), key=lambda p: p.stat().st_mtime) \
            if SOURCE_DIR.exists() else []
        if rec is not None:
            args = ["-sseof", "-4", "-i", str(rec)]
        elif sources:
            args = ["-ss", "15", "-i", str(sources[-1])]
        else:
            # not recording and no clips yet: one frame straight from the live stream
            sl = shutil.which("streamlink")
            cmd = [sl] if sl else [sys.executable, "-m", "streamlink"]
            try:
                stream = subprocess.Popen(cmd + ["--stdout", f"https://www.twitch.tv/{channel}", "720p30,720p60,480p30,best"],
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                res = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0",
                                      "-frames:v", "1", "-q:v", "3", str(out)],
                                     stdin=stream.stdout, capture_output=True, timeout=40)
            except (OSError, subprocess.TimeoutExpired):
                res = None
            finally:
                try:
                    stream.kill()
                except Exception:
                    pass
            return out if res is not None and res.returncode == 0 and out.exists() else None
        res = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
                              "-frames:v", "1", "-q:v", "3", str(out)], capture_output=True)
        return out if res.returncode == 0 and out.exists() else None


def srt_cues(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    cues = []
    for block in re.split(r"\r?\n\r?\n", raw.strip()):
        lines = block.splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            start = lines[1].split("-->")[0].strip()
            h, m, s = start.replace(",", ".").split(":")
            cues.append({"start": int(h) * 3600 + int(m) * 60 + float(s), "text": " ".join(lines[2:])})
    return cues


def srt_text(path: Path) -> str:
    return " ".join(c["text"] for c in srt_cues(path))


def clip_path(name: str) -> Path | None:
    """A clip (or its .srt) by plain file name, in its streamer's folder (no folders, no '..')."""
    if not is_plain_name(name, ("mp4", "srt")):
        return None
    p = clip_file(name, channel_for(name))
    return p if p.is_file() else None


def unlink_retry(path: Path, seconds: float = 5.0) -> None:
    """Delete a file, waiting while it's still open: on Windows a video the page was just
    streaming stays locked for a moment after the player closes."""
    deadline = time.time() + seconds
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if time.time() > deadline:
                raise
            time.sleep(0.25)


_thumb_lock = threading.Lock()


def thumbnail(clip: Path) -> Path | None:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumb = THUMB_DIR / (clip.stem + ".jpg")
    if thumb.exists() and thumb.stat().st_mtime >= clip.stat().st_mtime:
        return thumb
    with _thumb_lock:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "5", "-i", str(clip),
                        "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(thumb)],
                       capture_output=True)
    return thumb if thumb.exists() else None


# --------------------------------------------------------------------------- storage (retention)

def save_storage(body: dict) -> dict:
    """Retention settings from the Setup tab; applied right away in the background."""
    updates = {}
    for key, env_key in (("keep_sources_days", "KEEP_SOURCES_DAYS"), ("keep_clips_days", "KEEP_CLIPS_DAYS")):
        if key in body:
            try:
                days = int(body[key])
            except (TypeError, ValueError):
                raise ValueError("Days must be a whole number (0 = keep forever).") from None
            if not 0 <= days <= 3650:
                raise ValueError("Days must be between 0 (keep forever) and 3650.")
            updates[env_key] = str(days)
    if updates:
        write_env(updates)
        log(f"storage: retention set to {updates}")
        threading.Thread(target=run_cleanup, daemon=True).start()
    return storage.usage()


_cleanup_lock = threading.Lock()


def run_cleanup() -> None:
    if not _cleanup_lock.acquire(blocking=False):
        return                                   # one is already running
    try:
        removed = storage.cleanup()
        if removed["sources"] or removed["clips"]:
            log(f"storage: removed {removed['clips']} old clip(s) and {removed['sources']} old source(s)")
    except Exception as e:  # housekeeping must never take the dashboard down
        log(f"storage: cleanup failed ({e})")
    finally:
        _cleanup_lock.release()


def cleanup_loop() -> None:
    while True:
        run_cleanup()
        time.sleep(CLEANUP_EVERY)


# --------------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    manager: Manager = None  # set in main()
    server_version = "clip-watcher"

    def log_message(self, fmt, *args):   # keep the terminal quiet; errors still print
        pass

    # ---- helpers
    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status)

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(min(n, 100_000)))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def send_file(self, path: Path, ctype: str | None = None, download: bool = False):
        ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        # the file is opened per chunk and closed before writing to the network, so a video
        # the browser is still buffering never blocks deleting it (Windows locks open files)
        pos, left = start, end - start + 1
        try:
            while left > 0:
                try:
                    with open(path, "rb") as f:
                        f.seek(pos)
                        chunk = f.read(min(256 * 1024, left))
                except OSError:
                    break                   # deleted mid-stream
                if not chunk:
                    break
                self.wfile.write(chunk)
                pos += len(chunk)
                left -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    # ---- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        if path in ("/", "/index.html"):
            return self.send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            p = (WEB_DIR / path[len("/static/"):]).resolve()
            if p.parent == WEB_DIR.resolve() and p.is_file():
                return self.send_file(p)
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        if path == "/api/state":
            cfg = ai.ai_config()
            return self.send_json({
                "streamers": [w.info() for w in self.manager.watchers.values()],
                "qualities": QUALITIES, "defaults": DEFAULTS, "layouts": LAYOUTS,
                "templates": TEMPLATES, "title_modes": TITLE_MODES,
                "ai": {"provider": cfg["provider"], "label": ai.PROVIDERS[cfg["provider"]]["label"],
                       "model": cfg["model"]} if cfg else None,
                "rendering": (RUN_DIR / "render.lock").exists() and is_rendering(),
                "features_off": features_off(),
                "disk_free_gb": round(storage.free_gb(), 1),
            })
        if path == "/api/ai":
            return self.send_json(ai_state())
        if path == "/api/setup":
            return self.send_json(setup_state())
        if path == "/api/storage":
            return self.send_json(storage.usage())
        if path == "/api/frame":
            ch = (parse_qs(url.query).get("channel") or [""])[0]
            m = CHANNEL_RE.fullmatch(ch.strip())
            if not m:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, "Type the streamer's name first.")
            name = m.group(1).lower()
            frame = grab_frame(name, self.manager.watchers.get(name))
            if frame is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND,
                                            f"Couldn't get a picture of {name}: are they live?")
            return self.send_file(frame, "image/jpeg")
        if path.startswith("/sources/"):
            p = source_path(path[len("/sources/"):])
            if p is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "no source for this clip")
            return self.send_file(p, "video/mp4")
        if path == "/api/clips":
            return self.send_json({"clips": list_clips()})
        if path.startswith("/api/clips/") and path.endswith("/transcript"):
            p = clip_path(path[len("/api/clips/"):-len("/transcript")])
            if p is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "no such clip")
            return self.send_json({"cues": srt_cues(p.with_suffix(".srt"))})
        if path.startswith("/clips/"):
            p = clip_path(path[len("/clips/"):])
            if p is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "no such clip")
            return self.send_file(p, "video/mp4" if p.suffix == ".mp4" else "text/plain; charset=utf-8",
                                  download="download" in (url.query or ""))
        if path.startswith("/thumbs/"):
            p = clip_path(path[len("/thumbs/"):].removesuffix(".jpg") + ".mp4")
            t = thumbnail(p) if p else None
            if t is None:
                return self.send_error_json(HTTPStatus.NOT_FOUND, "no thumbnail")
            return self.send_file(t, "image/jpeg")
        self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        body = self.read_json()
        try:
            if path == "/api/streamers":
                w = self.manager.add(body.get("channel", ""), body.get("settings"), bool(body.get("start")))
                return self.send_json(w.info())
            if path == "/api/ai":
                save_ai(body)
                return self.send_json(ai_state())
            if path == "/api/ai/test":
                return self.send_json(test_ai())
            if path == "/api/setup/install":
                start_install(str(body.get("id", "")))
                return self.send_json(setup_state())
            if path == "/api/storage":
                return self.send_json(save_storage(body))
            if path == "/api/setup/whisper":
                size = body.get("model")
                if size not in components.WHISPER_SIZES:
                    raise ValueError("Unknown caption model.")
                write_env({"WHISPER_MODEL": size})
                log(f"caption model set to {size}")
                return self.send_json(setup_state())
            if m := re.fullmatch(r"/api/clips/([\w.-]+\.mp4)/render", path):
                if clip_path(m.group(1)) is None and read_meta(m.group(1)) is None:
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "no such clip")
                return self.send_json(start_rerender(m.group(1), body))
            m = re.fullmatch(r"/api/streamers/(\w+)/(start|stop|settings|clip)", path)
            if m:
                w = self.manager.get(m.group(1))
                if m.group(2) == "clip":
                    w.clip_now()
                elif m.group(2) == "start":
                    w.start()
                elif m.group(2) == "stop":
                    w.stop()
                else:
                    w = self.manager.update(m.group(1), body.get("settings"))
                return self.send_json(w.info())
        except KeyError:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown streamer")
        except ValueError as e:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(e))
        self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_DELETE(self):
        path = unquote(urlparse(self.path).path)
        try:
            if m := re.fullmatch(r"/api/streamers/(\w+)", path):
                self.manager.remove(m.group(1))
                return self.send_json({"ok": True})
            if path.startswith("/api/clips/"):
                name = path[len("/api/clips/"):]
                if not is_plain_name(name):
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "no such clip")
                if name in _rendering:
                    raise ValueError("Wait until this clip has finished rendering.")
                files = storage.clip_files(name, channel_for(name))
                if not any(f.exists() for f in files):
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "no such clip")
                for f in files:
                    unlink_retry(f)
                log(f"deleted clip {name}")
                return self.send_json({"ok": True})
        except KeyError:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown streamer")
        except ValueError as e:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(e))
        except OSError as e:
            return self.send_error_json(HTTPStatus.CONFLICT, f"could not delete: {e}")
        self.send_error_json(HTTPStatus.NOT_FOUND, "not found")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="clip-watcher dashboard", description="clip-watcher dashboard")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    args = p.parse_args(argv)

    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        log(f"warning: {', '.join(missing)} not found on PATH; clips and thumbnails will fail "
            "(open a new terminal after installing ffmpeg)")
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    if moved := storage.migrate_flat_clips():
        log(f"moved {moved} clip file(s) into one folder per streamer under {CLIP_DIR}")
    log(f"clips are saved in {CLIP_DIR} (one folder per streamer)")
    threading.Thread(target=cleanup_loop, daemon=True).start()
    Handler.manager = Manager()

    class Server(ThreadingHTTPServer):
        # on Windows, address reuse lets a second dashboard silently share the port: refuse instead
        allow_reuse_address = os.name != "nt"

    try:
        server = Server(("127.0.0.1", args.port), Handler)
    except OSError:
        sys.exit(f"port {args.port} is already in use: is the dashboard already running? "
                 f"Open http://localhost:{args.port}, or start this one with --port {args.port + 1}")
    server.daemon_threads = True
    url = f"http://localhost:{args.port}"
    log(f"dashboard running at {url}  (Ctrl+C to stop; running streamers finish their clips first)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("shutting down...")
    finally:
        server.server_close()
        try:
            Handler.manager.shutdown()
        except KeyboardInterrupt:
            log("aborted; killing watchers")
            for w in Handler.manager.watchers.values():
                w.kill()
        log("done")


if __name__ == "__main__":
    main()
