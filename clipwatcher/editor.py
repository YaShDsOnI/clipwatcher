"""The clip editor's server side.

The browser edits a small *project* (which parts of the video to keep, their speed and zoom
punch-ins, text layers, framing, audio and music). This module checks that project, turns it into
one ffmpeg command and renders it into a NEW file in clips/<streamer>/edits/. The original clip is
only ever read, never written.

The edit is cut from one of two media files:
  * "source": the clip's 1-minute source (kept for a few days): any framing, and captions that are
    re-timed to the cuts;
  * "clip":   the finished 30-second clip (always there): its captions are already burned in.
"""
from __future__ import annotations

import array
import hashlib
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from .captions import TEMPLATES, TEXT_STYLES, Word, ass_document
from .clipper import default_threads, fonts_in_work_dir, render_lock, video_filter
from .config import (CACHE_DIR, EDIT_NAME_RE, HOME, MUSIC_DIR, PROJECT_DIR, WORK_DIR, clip_file, edits_dir,
                     log, low_priority, project_path_for)

ASPECTS = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080)}
SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
LAYOUTS = ("blur", "crop", "split")
FITS = ("crop", "blur")
ANIMS = ("none", "fade", "pop")
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
MUSIC_RE = re.compile(r"[a-f0-9]{16}\.(?:mp3|m4a|wav|ogg)")
MUSIC_EXTS = (".mp3", ".m4a", ".wav", ".ogg")
MAX_SEGMENTS, MAX_TEXTS, MAX_MUSIC = 200, 50, 10
MAX_OUTPUT = 180.0               # seconds; Shorts and Reels are shorter than this anyway
MAX_MUSIC_BYTES = 30_000_000
FPS = 30


# --------------------------------------------------------------------------- media

_probe_cache: dict[tuple[str, float], dict | None] = {}


def probe(path: Path) -> dict | None:
    """Duration, size and whether there's sound, for one media file (cached per file version)."""
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None
    if key in _probe_cache:
        return _probe_cache[key]
    info = None
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                              str(path)], capture_output=True, text=True, timeout=60)
        data = json.loads(res.stdout or "{}")
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        duration = float(data.get("format", {}).get("duration") or 0)
        if duration > 0:
            info = {"duration": round(duration, 3),
                    "width": int(video["width"]) if video else 0, "height": int(video["height"]) if video else 0,
                    "audio": any(s.get("codec_type") == "audio" for s in streams), "video": video is not None}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        info = None
    _probe_cache[key] = info
    return info


def media_for(name: str, meta: dict | None) -> dict[str, dict | None]:
    """The media an edit of this clip can be cut from, with their duration/size (path is server-only)."""
    channel = (meta or {}).get("channel")
    out: dict[str, dict | None] = {"source": None, "clip": None}
    source = HOME / meta["source"] if meta and meta.get("source") else None
    for kind, path in (("source", source), ("clip", clip_file(name, channel))):
        info = probe(path) if path is not None and path.is_file() else None
        if info and info["video"]:
            out[kind] = {**info, "path": path, "url": f"/media/{name}/{kind}"}
    return out


# --------------------------------------------------------------------------- the project

def default_project(kind: str, meta: dict | None, media: dict) -> dict:
    """What the editor starts with: the published 30 s, framed and captioned like the clip."""
    meta = meta or {}
    s = meta.get("settings") or {}
    if kind == "source":
        start = float(meta.get("start", 0))
        end = float(meta.get("end", media["duration"]))
        segments = [{"in": round(start, 3), "out": round(min(end, media["duration"]), 3), "speed": 1.0, "punch": None}]
    else:
        segments = [{"in": 0.0, "out": media["duration"], "speed": 1.0, "punch": None}]
    layout = s.get("layout") if s.get("layout") in LAYOUTS else "blur"
    if layout == "split" and not s.get("facecam"):
        layout = "blur"
    return {
        "v": 1, "media": kind, "aspect": "9:16", "segments": segments,
        "frame": {"layout": layout, "facecam": s.get("facecam"), "fit": "crop", "zoom": 1.0, "x": 0.5, "y": 0.5},
        "captions": {"on": kind == "source" and bool(meta.get("words")) and s.get("captions", True) is not False,
                     "template": s.get("template") if s.get("template") in TEMPLATES else "karaoke",
                     "words": int(s.get("caption_words") or 4)},
        "texts": [], "audio": {"volume": 1.0, "fade_in": 0.0, "fade_out": 0.0}, "music": [],
    }


def _num(v, lo: float, hi: float, default: float | None = None) -> float:
    try:
        x = float(v)
        if x != x:                                   # NaN
            raise ValueError
    except (TypeError, ValueError):
        if default is None:
            raise ValueError("The edit has a value that isn't a number.") from None
        x = default
    return min(max(x, lo), hi)


def _facecam(raw) -> list[float] | None:
    try:
        box = [round(float(v), 4) for v in raw]
        if len(box) == 4 and all(0 <= v <= 1 for v in box) and box[2] > 0.02 and box[3] > 0.02:
            return box
    except (TypeError, ValueError):
        pass
    return None


def segment_duration(s: dict) -> float:
    return (s["out"] - s["in"]) / s["speed"]


def total_duration(segments: list[dict]) -> float:
    return sum(segment_duration(s) for s in segments)


def clean_project(raw: dict, media: dict[str, dict | None], music: dict[str, dict]) -> dict:
    """Check everything the browser sent. Numbers are clamped into range; anything that can't be
    made sense of (unknown media or music, too many items, text too long) is refused."""
    if not isinstance(raw, dict):
        raise ValueError("That isn't an edit.")
    kind = raw.get("media")
    if kind not in ("source", "clip") or not media.get(kind):
        raise ValueError("The video this edit is cut from isn't available any more.")
    m = media[kind]
    dur = m["duration"]

    segments = []
    for s in (raw.get("segments") or [])[:MAX_SEGMENTS + 1]:
        if not isinstance(s, dict):
            continue
        a, b = _num(s.get("in"), 0, dur), _num(s.get("out"), 0, dur)
        if b - a < 0.1:
            continue
        speed = min(SPEEDS, key=lambda v: abs(v - _num(s.get("speed"), 0.5, 2.0, 1.0)))
        seg = {"in": round(a, 3), "out": round(b, 3), "speed": speed, "punch": None}
        p = s.get("punch")
        if isinstance(p, dict):
            seg_len = (b - a) / speed
            seg["punch"] = {"at": round(_num(p.get("at"), 0, max(0.0, seg_len - 0.05), 0), 3),
                            "zoom": round(_num(p.get("zoom"), 1.05, 2.0, 1.3), 2),
                            "dur": round(_num(p.get("dur"), 0.05, 3.0, 0.25), 2)}
        segments.append(seg)
    if not segments:
        raise ValueError("The edit has no video left: keep at least one part.")
    if len(segments) > MAX_SEGMENTS:
        raise ValueError(f"Too many cuts (max {MAX_SEGMENTS}).")
    total = total_duration(segments)
    if total > MAX_OUTPUT:
        raise ValueError(f"The edit is {total:.0f} s long; the maximum is {MAX_OUTPUT:.0f} s.")

    f = raw.get("frame") if isinstance(raw.get("frame"), dict) else {}
    facecam = _facecam(f.get("facecam"))
    layout = f.get("layout") if f.get("layout") in LAYOUTS else "blur"
    if layout == "split" and not facecam:
        layout = "blur"
    frame = {"layout": layout, "facecam": facecam, "fit": f.get("fit") if f.get("fit") in FITS else "crop",
             "zoom": round(_num(f.get("zoom"), 1.0, 3.0, 1.0), 3),
             "x": round(_num(f.get("x"), 0, 1, 0.5), 4), "y": round(_num(f.get("y"), 0, 1, 0.5), 4)}

    c = raw.get("captions") if isinstance(raw.get("captions"), dict) else {}
    captions = {"on": bool(c.get("on")) and kind == "source",
                "template": c.get("template") if c.get("template") in TEMPLATES else "karaoke",
                "words": int(_num(c.get("words"), 1, 8, 4))}

    texts = []
    raw_texts = raw.get("texts") or []
    if len(raw_texts) > MAX_TEXTS:
        raise ValueError(f"Too many text layers (max {MAX_TEXTS}).")
    for t in raw_texts:
        if not isinstance(t, dict):
            continue
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        if len(text) > 120:
            raise ValueError("A text layer is longer than 120 characters.")
        start = _num(t.get("start"), 0, total, 0)
        end = _num(t.get("end"), 0, total, total)
        if end - start < 0.1:
            continue
        texts.append({
            "id": re.sub(r"[^\w-]", "", str(t.get("id") or uuid.uuid4().hex[:8]))[:20] or uuid.uuid4().hex[:8],
            "text": text, "start": round(start, 3), "end": round(end, 3),
            "x": round(_num(t.get("x"), 0.02, 0.98, 0.5), 4), "y": round(_num(t.get("y"), 0.02, 0.98, 0.2), 4),
            "size": round(_num(t.get("size"), 16, 220, 64)),
            "color": t["color"] if COLOR_RE.fullmatch(str(t.get("color"))) else "#ffffff",
            "bg": t["bg"] if COLOR_RE.fullmatch(str(t.get("bg"))) else "#7c5cff",
            "style": t.get("style") if t.get("style") in TEXT_STYLES else "band",
            "anim": t.get("anim") if t.get("anim") in ANIMS else "fade",
        })

    a = raw.get("audio") if isinstance(raw.get("audio"), dict) else {}
    audio = {"volume": round(_num(a.get("volume"), 0, 2, 1), 2),
             "fade_in": round(_num(a.get("fade_in"), 0, 5, 0), 2), "fade_out": round(_num(a.get("fade_out"), 0, 5, 0), 2)}

    tracks = []
    raw_music = raw.get("music") or []
    if len(raw_music) > MAX_MUSIC:
        raise ValueError(f"Too many music items (max {MAX_MUSIC}).")
    for item in raw_music:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "")
        if not MUSIC_RE.fullmatch(mid) or mid not in music:
            raise ValueError("A music file in the edit isn't in the music library any more.")
        mdur = music[mid]["duration"]
        start = _num(item.get("start"), 0, max(0.0, total - 0.1), 0)
        offset = _num(item.get("offset"), 0, max(0.0, mdur - 0.1), 0)
        length = _num(item.get("dur"), 0.1, max(0.1, min(mdur - offset, total - start)), min(mdur - offset, total - start))
        tracks.append({"id": mid, "start": round(start, 3), "offset": round(offset, 3), "dur": round(length, 3),
                       "volume": round(_num(item.get("volume"), 0, 2, 0.4), 2),
                       "fade_in": round(_num(item.get("fade_in"), 0, 5, 0), 2),
                       "fade_out": round(_num(item.get("fade_out"), 0, 5, 1), 2)})

    return {"v": 1, "media": kind, "aspect": raw.get("aspect") if raw.get("aspect") in ASPECTS else "9:16",
            "segments": segments, "frame": frame, "captions": captions, "texts": texts, "audio": audio,
            "music": tracks}


def retime_words(words: list[Word], segments: list[dict]) -> list[Word]:
    """Word captions (in media time) moved onto the edit's timeline: words in parts that were cut
    out disappear, the rest follow the cuts and speed changes. Speaker colours are re-ranked so the
    main voice of the edit is white."""
    out: list[Word] = []
    t0 = 0.0
    for s in segments:
        a, b, sp = s["in"], s["out"], s["speed"]
        for w in words:
            if a <= w.start < b:
                out.append(Word(t0 + (w.start - a) / sp, t0 + (min(w.end, b) - a) / sp, w.text, w.speaker))
        t0 += (b - a) / sp
    talk: Counter[int] = Counter()
    for w in out:
        talk[w.speaker] += w.end - w.start
    rank = {k: i for i, (k, _) in enumerate(talk.most_common())}
    for w in out:
        w.speaker = rank[w.speaker]
    return out


# --------------------------------------------------------------------------- the ffmpeg recipe

def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def build_ffmpeg(project: dict, media: dict, music_paths: dict[str, Path], ass_name: str | None,
                 out_path: Path, threads: int | None = None) -> list[str]:
    """One ffmpeg command that renders the whole edit."""
    W, H = ASPECTS[project["aspect"]]
    segs = project["segments"]
    n = len(segs)
    total = total_duration(segs)
    fc: list[str] = []

    # 1. every kept part, at its speed, then joined
    fc.append(f"[0:v]split={n}" + "".join(f"[vs{i}]" for i in range(n)))
    if media["audio"]:
        fc.append(f"[0:a]asplit={n}" + "".join(f"[as{i}]" for i in range(n)))
    for i, s in enumerate(segs):
        sp = s["speed"]
        fc.append(f"[vs{i}]trim=start={s['in']:.3f}:end={s['out']:.3f},setpts=(PTS-STARTPTS)/{sp},fps={FPS}[v{i}]")
        if media["audio"]:
            tempo = f",atempo={sp}" if sp != 1 else ""
            fc.append(f"[as{i}]atrim=start={s['in']:.3f}:end={s['out']:.3f},asetpts=PTS-STARTPTS{tempo},"
                      f"aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
        else:
            fc.append(f"aevalsrc=0|0:s=48000:d={segment_duration(s):.3f}[a{i}]")
    fc.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")

    # 2. framing into the output size
    f = project["frame"]
    if project["media"] == "source":
        fc.append(video_filter(f["layout"], f["facecam"], None, (W, H), src="[vc]", out="[vf]"))
    elif f["fit"] == "blur":
        bw, bh = _even(W / 4), _even(H / 4)
        fc.append(f"[vc]split[cb][cf];[cb]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                  f"boxblur=8:2,scale={W}:{H},eq=brightness=-0.28[cbg];"
                  f"[cf]scale={W}:{H}:force_original_aspect_ratio=decrease[cfg];[cbg][cfg]overlay=(W-w)/2:(H-h)/2,setsar=1[vf]")
    else:
        fc.append(f"[vc]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[vf]")
    cur = "vf"

    # 3. zoom & pan
    if f["zoom"] > 1.001:
        zw, zh = _even(W * f["zoom"]), _even(H * f["zoom"])
        fc.append(f"[{cur}]scale={zw}:{zh},crop={W}:{H}:{round((zw - W) * f['x'])}:{round((zh - H) * f['y'])}[vz]")
        cur = "vz"

    # 4. zoom punch-ins: zoom in quickly at a moment and hold until that part ends
    terms, t0 = [], 0.0
    for s in segs:
        p = s.get("punch")
        if p:
            a, e = t0 + p["at"], t0 + segment_duration(s)
            terms.append(f"{p['zoom'] - 1:.3f}*min(1,max(0,(it-{a:.3f})/{p['dur']:.3f}))*between(it,{a:.3f},{e:.3f})")
        t0 += segment_duration(s)
    if terms:
        fc.append(f"[{cur}]zoompan=z='1+{'+'.join(terms)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                  f":d=1:s={W}x{H}:fps={FPS},setsar=1[vp]")
        cur = "vp"

    # 5. captions and text layers
    fc.append(f"[{cur}]ass={ass_name}:fontsdir=fonts[vout]" if ass_name else f"[{cur}]null[vout]")

    # 6. sound: the clip's own audio, then music mixed in
    a = project["audio"]
    af = []
    if a["volume"] != 1:
        af.append(f"volume={a['volume']}")
    if a["fade_in"] > 0:
        af.append(f"afade=t=in:st=0:d={a['fade_in']}")
    if a["fade_out"] > 0:
        af.append(f"afade=t=out:st={max(0.0, total - a['fade_out']):.3f}:d={a['fade_out']}")
    fc.append(f"[ac]{','.join(af) or 'anull'}[amain]")
    inputs = ["-i", str(media["path"])]
    mix = []
    for j, m in enumerate(project["music"]):
        inputs += ["-i", str(music_paths[m["id"]])]
        chain = [f"atrim=start={m['offset']:.3f}:duration={m['dur']:.3f}", "asetpts=PTS-STARTPTS",
                 f"volume={m['volume']}"]
        if m["fade_in"] > 0:
            chain.append(f"afade=t=in:st=0:d={m['fade_in']}")
        if m["fade_out"] > 0:
            chain.append(f"afade=t=out:st={max(0.0, m['dur'] - m['fade_out']):.3f}:d={m['fade_out']}")
        delay = round(m["start"] * 1000)
        chain += [f"adelay=delays={delay}:all=1", "aformat=sample_rates=48000:channel_layouts=stereo"]
        fc.append(f"[{j + 1}:a]{','.join(chain)}[m{j}]")
        mix.append(f"[m{j}]")
    if mix:
        fc.append(f"[amain]{''.join(mix)}amix=inputs={len(mix) + 1}:duration=first:normalize=0[aout]")
    else:
        fc.append("[amain]anull[aout]")

    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
            "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-threads", str(threads or default_threads()),
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", f"{total:.3f}",
            "-progress", "pipe:1", "-nostats", str(out_path)]


# --------------------------------------------------------------------------- exporting

def export(name: str, meta: dict | None, project: dict, progress=lambda f: None) -> Path:
    """Render the edit into a new file in clips/<streamer>/edits/ and return it."""
    channel = (meta or {}).get("channel")
    media = media_for(name, meta)[project["media"]]
    if media is None:
        raise RuntimeError("the video this edit is cut from is gone")
    W, H = ASPECTS[project["aspect"]]
    total = total_duration(project["segments"])
    lib = music_library()
    music_paths = {m["id"]: MUSIC_DIR / m["id"] for m in project["music"] if m["id"] in lib}
    if len(music_paths) < len({m["id"] for m in project["music"]}):
        raise RuntimeError("a music file in the edit was deleted")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    job = uuid.uuid4().hex[:10]
    ass_path, tmp, errlog = WORK_DIR / f"edit-{job}.ass", WORK_DIR / f"edit-{job}.mp4", WORK_DIR / f"edit-{job}.log"
    words = []
    if project["captions"]["on"] and project["media"] == "source":
        words = retime_words([Word(**w) for w in (meta or {}).get("words", [])], project["segments"])
    try:
        ass_name = None
        if words or project["texts"]:
            settings = {"template": project["captions"]["template"], "caption_words": project["captions"]["words"],
                        "captions": bool(words), "layout": project["frame"]["layout"] if project["media"] == "source" else "blur"}
            ass_path.write_text(ass_document(words, settings, None, project["texts"], (W, H)), encoding="utf-8")
            fonts_in_work_dir()
            ass_name = ass_path.name
        cmd = build_ffmpeg(project, media, music_paths, ass_name, tmp)
        with render_lock(), open(errlog, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(cmd, cwd=WORK_DIR, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace", **low_priority())
            for line in proc.stdout:
                if line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        progress(min(0.99, int(line.split("=", 1)[1]) / 1e6 / max(total, 0.1)))
                    except ValueError:
                        pass
            code = proc.wait()
        if code != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
            detail = errlog.read_text(encoding="utf-8", errors="replace").strip()[-400:]
            raise RuntimeError(f"ffmpeg failed: {detail or 'no output'}")
        out_dir = edits_dir(name, channel)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{Path(name).stem}_edit-{datetime.now():%Y%m%d-%H%M%S}.mp4"
        while out.exists():                          # two exports in the same second
            time.sleep(1)
            out = out_dir / f"{Path(name).stem}_edit-{datetime.now():%Y%m%d-%H%M%S}.mp4"
        tmp.replace(out)
        progress(1.0)
        return out
    finally:
        for p in (ass_path, tmp, errlog):
            p.unlink(missing_ok=True)


class ExportQueue:
    """Exports run one at a time in the background; the page polls a job for its progress."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, name: str, meta: dict | None, project: dict) -> dict:
        job = {"id": uuid.uuid4().hex[:12], "clip": name, "status": "queued", "progress": 0.0,
               "created": time.time(), "file": None, "error": None}
        with self._lock:
            self.jobs[job["id"]] = job
            for old in sorted(self.jobs.values(), key=lambda j: j["created"])[:-50]:
                self.jobs.pop(old["id"], None)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True)
                self._worker.start()
        self._q.put((job, name, meta, project))
        return job

    def _run(self) -> None:
        while True:
            job, name, meta, project = self._q.get()
            job["status"] = "running"
            log(f"editor: exporting an edit of {name}")
            try:
                out = export(name, meta, project, lambda f, job=job: job.__setitem__("progress", round(f, 3)))
                job.update(status="done", progress=1.0, file=export_info(out))
                log(f"editor: saved {out.relative_to(HOME).as_posix()}")
            except Exception as e:  # report it on the page, keep the queue running
                job.update(status="failed", error=str(e)[:400])
                log(f"editor: export of {name} failed: {e}")


def export_info(path: Path) -> dict:
    st = path.stat()
    return {"file": path.name, "channel": path.parent.parent.name, "url": f"/edits/{path.parent.parent.name}/{path.name}",
            "size": st.st_size, "time": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "path": str(path), "folder": path.parent.relative_to(HOME).as_posix()}


def exports_for(name: str, channel: str | None) -> list[dict]:
    folder = edits_dir(name, channel)
    if not folder.is_dir():
        return []
    files = [p for p in folder.glob(f"{Path(name).stem}_edit-*.mp4") if EDIT_NAME_RE.fullmatch(p.name)]
    return [export_info(p) for p in sorted(files, key=lambda p: p.name, reverse=True)]


def edit_path(channel: str, file: str) -> Path | None:
    """An exported edit by its folder and name (validated: no other file can be reached)."""
    if not re.fullmatch(r"\w{1,25}", channel) or not EDIT_NAME_RE.fullmatch(file):
        return None
    p = clip_file(file, channel).parent / "edits" / file
    return p if p.is_file() else None


# --------------------------------------------------------------------------- saved projects

def load_project(name: str) -> dict | None:
    try:
        data = json.loads(project_path_for(name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_project(name: str, project: dict) -> None:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    path = project_path_for(name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(project, indent=1), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- timeline pictures

FILMSTRIP_FRAMES = 24


def filmstrip(media: dict, stem: str, kind: str) -> Path | None:
    """One picture with FILMSTRIP_FRAMES small frames across the media, for the timeline (cached)."""
    out = CACHE_DIR / f"{stem}.{kind}.strip.jpg"
    path: Path = media["path"]
    if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
        return out
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    n = FILMSTRIP_FRAMES
    vf = f"fps={n}/{media['duration']:.3f},scale=-2:96,tile={n}x1"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vf", vf,
                    "-frames:v", "1", "-q:v", "5", str(out)], capture_output=True, timeout=180, **low_priority())
    return out if out.exists() else None


def waveform(media: dict, stem: str, kind: str, buckets: int = 1000) -> dict:
    """Loudness peaks (0..1) across the media, for the audio track of the timeline (cached)."""
    out = CACHE_DIR / f"{stem}.{kind}.wave.json"
    path: Path = media["path"]
    try:
        if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
            return json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    peaks: list[float] = []
    if media["audio"]:
        res = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "4000",
                              "-f", "s16le", "-"], capture_output=True, timeout=180, **low_priority())
        samples = array.array("h")
        samples.frombytes(res.stdout[: len(res.stdout) // 2 * 2])
        if samples:
            size = max(1, len(samples) // buckets)
            peaks = [round(max(abs(x) for x in samples[i:i + size]) / 32768, 3) for i in range(0, len(samples), size)]
    data = {"duration": media["duration"], "peaks": peaks}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data), encoding="utf-8")
    return data


# --------------------------------------------------------------------------- music library

def _index_path() -> Path:
    return MUSIC_DIR / "index.json"


def music_library() -> dict[str, dict]:
    """Uploaded music and sounds: id -> {"name", "duration"} (only files that still exist)."""
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if MUSIC_RE.fullmatch(k) and (MUSIC_DIR / k).is_file()}


def add_music(data: bytes, filename: str) -> dict:
    """Store an uploaded audio file in the music library (checked with ffprobe)."""
    ext = Path(filename).suffix.lower()
    if ext not in MUSIC_EXTS:
        raise ValueError("Use an MP3, M4A, WAV or OGG file.")
    if not data or len(data) > MAX_MUSIC_BYTES:
        raise ValueError(f"The file must be under {MAX_MUSIC_BYTES // 1_000_000} MB.")
    mid = hashlib.sha256(data).hexdigest()[:16] + ext
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    path = MUSIC_DIR / mid
    path.write_bytes(data)
    info = probe(path)
    if not info or not info["audio"] or info["duration"] > 1800:
        path.unlink(missing_ok=True)
        raise ValueError("That file isn't a playable audio file (or it's longer than 30 minutes).")
    lib = music_library()
    name = re.sub(r"[^\w .()&'-]", "", Path(filename).stem)[:60] or "sound"
    lib[mid] = {"name": name, "duration": info["duration"]}
    _index_path().write_text(json.dumps(lib, indent=1), encoding="utf-8")
    return {"id": mid, **lib[mid]}


def delete_music(mid: str) -> None:
    if not MUSIC_RE.fullmatch(mid):
        raise ValueError("Unknown sound.")
    lib = music_library()
    lib.pop(mid, None)
    (MUSIC_DIR / mid).unlink(missing_ok=True)
    if MUSIC_DIR.is_dir():
        _index_path().write_text(json.dumps(lib, indent=1), encoding="utf-8")


def music_path(mid: str) -> Path | None:
    return MUSIC_DIR / mid if MUSIC_RE.fullmatch(mid) and mid in music_library() else None
