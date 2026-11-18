"""clip-watcher: record a Twitch stream, watch chat for activity spikes,
and cut a vertical (9:16) clip + .srt subtitles around every spike.

    clip-watcher watch <channel> [more channels...] [options]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import storage
from .captions import TEMPLATES
from .chat import BOTS, BUCKET, ChatCounter, ClipLimiter, Moment, SpikeDetector, SpikeMerger, chat_loop, hhmmss
from .clipper import LAYOUTS, default_threads, run_ffmpeg, save_meta
from .components import SETUP_HINT, WHISPER_SIZES, speaker_models_downloaded, whisper_downloaded, whisper_model_name
from .config import (CLIP_DIR, HOME, REC_DIR, RUN_DIR, SOURCE_DIR, child_env, clip_file, low_priority,
                     meta_path_for, self_cmd)

OUTPUT_LOST = threading.Event()   # whoever reads our output (the dashboard) has gone away
CLEANUP_EVERY = 3600              # seconds between retention cleanups while recording


def log(msg: str) -> None:
    if OUTPUT_LOST.is_set():
        return
    try:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)
    except (OSError, ValueError):
        # The dashboard that started us was closed. Don't crash (that would leave streamlink
        # recording with nobody to stop it): the main loop sees this and stops cleanly.
        OUTPUT_LOST.set()


# --------------------------------------------------------------------------- recording

def tool_cmd(name: str) -> list[str] | None:
    exe = shutil.which(name)
    if exe:
        return [exe]
    if name == "streamlink" and importlib.util.find_spec("streamlink"):
        return [sys.executable, "-m", "streamlink"]       # pip-installed but Scripts/ not on PATH
    return None


def _ffprobe_floats(args: list[str], data: bytes | None = None) -> list[float]:
    try:
        res = subprocess.run(["ffprobe", "-v", "error", *args], input=data,
                             capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return []
    vals = []
    for tok in res.stdout.decode(errors="replace").split():
        try:
            vals.append(float(tok.strip(",")))
        except ValueError:
            pass
    return vals


TAIL_BYTES = 4 * 1024 * 1024


def probe_duration(path: Path) -> float | None:
    """Seconds of media in a (growing) recording: last packet timestamp - first.

    Twitch timestamps count from the start of the broadcast (hours), and ffprobe's
    format=duration doesn't reliably subtract that, so measure both ends directly.
    Twitch serves either fragmented MP4 or MPEG-TS; streamlink writes whichever it gets."""
    start = _ffprobe_floats(["-show_entries", "format=start_time", "-of", "default=nw=1:nk=1", str(path)])
    try:
        with open(path, "rb") as f:
            head = f.read(1)
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES) // 188 * 188)     # align to a TS packet
            tail = f.read()
    except OSError:
        return None
    if head == b"\x47":    # MPEG-TS: resyncs anywhere, so probe just the tail bytes
        ends = _ffprobe_floats(["-f", "mpegts", "-show_entries", "packet=pts_time",
                                "-of", "csv=p=0", "-i", "pipe:0"], tail)
    else:                  # fragmented MP4: seek past the end -> packets of the last fragment
        ends = _ffprobe_floats(["-read_intervals", "99999999%", "-select_streams", "v:0",
                                "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)])
    if not start or not ends:
        return None
    dur = max(ends) - start[0]
    return dur if dur > 0 else None


def media_ext(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return path.suffix
    return ".mp4" if head[4:8] == b"ftyp" else ".ts"


class Recorder:
    """Runs streamlink into rec/<channel>_<stamp>.ts and tracks when the file grows."""

    def __init__(self, channel: str, quality: str):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        REC_DIR.mkdir(parents=True, exist_ok=True)
        self.path = REC_DIR / f"{channel}_{stamp}.ts"
        self.first_write: float | None = None   # wall clock when the first bytes landed
        self.last_write: float | None = None    # wall clock of the latest growth
        self._logf = open(self.path.with_suffix(".log"), "w", encoding="utf-8")
        cmd = tool_cmd("streamlink") + [
            # (streamlink >= 7 always filters out Twitch ad segments)
            "--retry-streams", "30",             # if offline, wait for the channel to go live
            "--output", str(self.path),
            f"https://www.twitch.tv/{channel}", self.quality_arg(quality),
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=self._logf, stderr=subprocess.STDOUT)
        threading.Thread(target=self._watch_file, daemon=True).start()

    @staticmethod
    def quality_arg(q: str) -> str:
        if "," in q or q in ("best", "worst"):
            return q
        if re.fullmatch(r"\d+p", q):                 # Twitch names streams 720p60 / 720p30
            return f"{q}60,{q}30,{q},best"
        return f"{q},best"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _watch_file(self) -> None:
        size = 0
        while True:
            running = self.alive()
            new = self._size()
            if new > size:
                now = time.time()
                if self.first_write is None:
                    self.first_write = now
                    log(f"recording started: {self.path.relative_to(HOME).as_posix()}")
                size, self.last_write = new, now
            if not running:
                return
            time.sleep(0.25)

    def stop(self) -> None:
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._logf.close()

    def finalize(self) -> Path | None:
        """After stop(): give the file the extension of its real container
        (Twitch often serves fragmented MP4 rather than MPEG-TS)."""
        if not self.path.exists():
            return None
        final = self.path.with_suffix(media_ext(self.path))
        if final != self.path:
            self.path.rename(final)
            self.path.with_suffix(".log").rename(final.with_suffix(".log"))
        return final

    def timeline(self) -> tuple[float, float] | None:
        """(seconds of media in the file, wall-clock time its newest bytes were written).

        Position in the file of any wall-clock moment w is then  duration - (end_wall - w).
        ffprobe's duration and ffmpeg's -ss both use stream timestamps, so skipped ad breaks
        don't shift the mapping."""
        if self.first_write is None:
            return None
        dur = end_wall = None
        for _ in range(3):                       # retry if the file grew while probing
            before, end_wall = self._size(), self.last_write
            dur = probe_duration(self.path)
            if dur is not None and self._size() == before:
                break
        wall_dur = end_wall - self.first_write
        if dur is None or dur > wall_dur + 120:  # unreadable or implausible timestamps
            dur = wall_dur                       # fall back to wall-clock bookkeeping
        return dur, end_wall


# --------------------------------------------------------------------------- clipping

@dataclass
class Clip:
    anchor: float     # wall-clock time of the spike
    start: float
    end: float
    count: int
    name: str
    median: float = 0.0


SOURCE_MARGIN = 15.0                    # seconds kept on each side of a clip, so it can be re-edited


def run_clipper(meta: Path, threads: int) -> None:
    """Hand the job to the clipper: a separate, short-lived process that loads the AI models only
    while it works and then exits, so the memory goes straight back to the system."""
    proc = subprocess.Popen(
        self_cmd("render", str(meta), "--threads", str(threads)),
        cwd=HOME, env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", **low_priority())
    for line in proc.stdout:
        if line.strip():
            print(line.rstrip(), flush=True)          # already time-stamped by the clipper
    proc.wait()


def clip_settings(args: argparse.Namespace) -> dict:
    return {
        "layout": args.layout, "template": args.template, "captions": args.captions,
        "speakers": args.speakers, "caption_words": args.caption_words,
        "speaker_threshold": args.speaker_threshold, "title": args.title,
        "title_in_video": args.title_in_video,
        "facecam": args.facecam, "model": args.model, "language": args.language,
    }


def make_clip(ch: "Channel", rec: Recorder, clip: Clip) -> None:
    span = rec.timeline()
    if span is None:
        log(f"skip {clip.name}: not in the recording")
        return
    dur, end_wall = span
    pos = lambda wall: dur - (end_wall - wall)       # noqa: E731  wall clock -> position in the file
    src_start = max(0.0, pos(clip.start - SOURCE_MARGIN))
    src_end = min(dur, pos(clip.end + SOURCE_MARGIN))
    win_start, win_end = max(src_start, pos(clip.start)), min(src_end, pos(clip.end))
    if win_end - win_start < 1:
        log(f"skip {clip.name}: not in the recording")
        return
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    # 1. a small source cut (stream copy: no re-encode, almost no CPU or RAM), kept for re-editing
    source = SOURCE_DIR / clip.name
    ok, err = run_ffmpeg(["-ss", f"{src_start:.3f}", "-i", str(rec.path),
                          "-t", f"{src_end - src_start:.3f}", "-c", "copy",
                          "-movflags", "+faststart", str(source)])
    if not ok:
        log(f"skip {clip.name}: could not cut the source ({err})")
        return
    # 2. the job: what to publish, the settings and what chat said around the moment
    chat = [{"t": round(pos(t) - src_start, 2), "user": user, "text": text[:200]}
            for t, user, text in ch.chat_between(clip.start - 2, clip.end + 3)][:400]
    meta = {
        "name": clip.name, "channel": ch.name,
        "created": datetime.fromtimestamp(clip.anchor).isoformat(timespec="seconds"),
        "msgs": clip.count, "median": clip.median,
        "source": source.relative_to(HOME).as_posix(),
        "start": round(win_start - src_start, 3), "end": round(win_end - src_start, 3),
        "settings": clip_settings(ch.args), "chat": chat, "status": "queued",
    }
    meta_path = meta_path_for(clip.name)
    save_meta(meta_path, meta)
    # 3. captions, speakers, title and the final render, one clip at a time machine-wide
    run_clipper(meta_path, ch.args.threads)


def cutter_loop(ch: "Channel", rec: Recorder, jobs: queue.Queue) -> None:
    while (clip := jobs.get()) is not None:
        # wait until the recording has caught up past the end of the clip window (+ margin)
        while rec.alive() and time.time() < clip.end + ch.args.lag + SOURCE_MARGIN:
            time.sleep(0.5)
        try:
            make_clip(ch, rec, clip)
        except Exception as e:  # one bad clip must not stop the channel
            log(f"clip failed {clip.name}: {e}")


# --------------------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="clip-watcher watch", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("channels", nargs="+", metavar="channel",
                   help="one or more Twitch channel names (or twitch.tv URLs)")
    p.add_argument("--min", type=int, default=10, help="minimum messages in a 10 s bucket to count as a spike")
    p.add_argument("--ratio", type=float, default=1.7, help="spike threshold as a multiple of the median")
    p.add_argument("--window", type=int, default=180, help="seconds of history for the median")
    p.add_argument("--before", type=float, default=20, help="seconds of video before the spike")
    p.add_argument("--after", type=float, default=10, help="seconds of video after the spike")
    p.add_argument("--offset", type=float, default=0,
                   help="shift every clip by this many seconds (negative = earlier) to correct chat/video sync")
    p.add_argument("--lag", type=float, default=10,
                   help="extra seconds to wait after a clip window before cutting it")
    p.add_argument("--gap", type=float, default=30,
                   help="minimum seconds between the end of one clip and the start of the next (0 = only no overlap)")
    p.add_argument("--max-per-hour", type=int, default=0,
                   help="at most this many automatic clips per streamer per hour (0 = no limit)")
    p.add_argument("--per-user", type=int, default=2,
                   help="each chatter counts at most this many messages per 10 s bucket, so one spammer "
                        "can't fake a spike (0 = no limit); known bots never count")
    p.add_argument("--quality", default="best", help="streamlink quality, e.g. best, 1080p60, 720p")
    p.add_argument("--model", default=None,
                   help="caption (Whisper) model: tiny, base, small ... (default: the one chosen in Setup, else base)")
    p.add_argument("--language", default=None, help="spoken language code, e.g. en (default: auto-detect)")
    p.add_argument("--layout", default="blur", choices=list(LAYOUTS),
                   help="blur: whole frame over a blurred background; crop: centre crop to 9:16; "
                        "split: facecam on top, gameplay below (needs --facecam)")
    p.add_argument("--facecam", default=None,
                   help="facecam box for --layout split, as x,y,w,h fractions of the frame, e.g. 0,0.55,0.25,0.3")
    p.add_argument("--template", default="karaoke", choices=list(TEMPLATES),
                   help="caption style")
    p.add_argument("--title", default="auto", choices=["auto", "chat", "off"],
                   help="hook title in the first seconds: auto = the AI provider set up in the dashboard AI tab or .env, else from chat")
    p.add_argument("--title-in-video", action="store_true",
                   help="also burn the hook title into the first 3 s of the clip (default: the video "
                        "only has captions; the title is shown in the dashboard)")
    p.add_argument("--keep-recordings", action="store_true",
                   help="keep full stream recordings (default: delete them once their clips are made; "
                        "every clip keeps its own short source cut for re-editing)")
    p.add_argument("--threads", type=int, default=default_threads(),
                   help="CPU threads the AI step may use (fewer = lighter on the rest of the PC)")
    p.add_argument("--captions", default=True, action=argparse.BooleanOptionalAction,
                   help="burn captions into the clip (the .srt file is written either way)")
    p.add_argument("--caption-words", type=int, default=4, help="maximum words shown at a time")
    p.add_argument("--speakers", default=True, action=argparse.BooleanOptionalAction,
                   help="give each speaker their own caption colour")
    p.add_argument("--speaker-threshold", type=float, default=0.3,
                   help="lower = split voices into more speakers, higher = merge more")
    p.add_argument("--once", action="store_true",
                   help="stop a channel for good when its stream ends, instead of waiting to re-record it when it comes back")
    p.add_argument("--stop-file", type=Path, default=None,
                   help="stop cleanly (like Ctrl+C) as soon as this file exists; used by the dashboard")
    p.add_argument("--dry-run", action="store_true",
                   help="only watch chat and report spikes; no recording, clips or subtitles")
    a = p.parse_intermixed_args(argv)          # allows: watch a --min 5 b
    channels = []
    for raw in a.channels:
        m = re.fullmatch(r"(?:https?://)?(?:www\.)?(?:twitch\.tv/)?#?(\w{1,25})/?", raw.strip(), re.I)
        if not m:
            p.error(f"not a Twitch channel name: {raw!r}")
        if (name := m.group(1).lower()) not in channels:
            channels.append(name)
    a.channels = channels
    if a.facecam:
        try:
            box = [float(v) for v in a.facecam.split(",")]
            assert len(box) == 4 and all(0 <= v <= 1 for v in box) and box[2] > 0 and box[3] > 0
            a.facecam = box
        except (ValueError, AssertionError):
            p.error("--facecam must be four fractions x,y,w,h between 0 and 1")
    if a.window < BUCKET:
        p.error("--window must be at least 10 seconds")
    if a.gap < 0 or a.max_per_hour < 0 or a.per_user < 0:
        p.error("--gap, --max-per-hour and --per-user can't be negative")
    return a


def preflight(args: argparse.Namespace) -> None:
    if args.dry_run:
        return
    missing = [n for n in ("streamlink", "ffmpeg", "ffprobe") if tool_cmd(n) is None]
    if missing:
        sys.exit("missing: " + ", ".join(missing) + "\nThese are needed to record and cut clips. "
                 f"See what's installed with:  {SETUP_HINT}")
    # optional parts: never fetched on their own. Missing ones just switch that feature off;
    # in a terminal, offer to get them (the dashboard has a Setup tab instead)
    model = whisper_model_name({"model": args.model})
    off = []
    if not (importlib.util.find_spec("faster_whisper") or importlib.util.find_spec("whisper")):
        off.append("captions (faster-whisper isn't installed)")
    elif not whisper_downloaded(model):
        off.append(f"captions (the Whisper '{model}' model isn't downloaded, {WHISPER_SIZES.get(model, ('?',))[0]})")
    if args.speakers and not importlib.util.find_spec("sherpa_onnx"):
        off.append("speaker colours (sherpa-onnx isn't installed)")
    elif args.speakers and not speaker_models_downloaded():
        off.append("speaker colours (the speaker models aren't downloaded, ~32 MB)")
    if not off:
        return
    print("These optional features are off until their parts are downloaded:", flush=True)
    for item in off:
        print(f"  - {item}", flush=True)
    if os.environ.get("CLIP_WATCHER_NONINTERACTIVE"):
        print("Get them in the dashboard's Setup tab.", flush=True)
        return
    try:
        answer = input("Review and download them now? You'll be asked about each one. [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        subprocess.run(self_cmd("setup"), cwd=HOME, env=child_env())
    else:
        print(f"OK, continuing without them. Later: {SETUP_HINT}", flush=True)


class Channel:
    """Everything that is per streamer: chat counts, spike detector and the current
    recording session (recorder + clip cutter). When a stream ends, the session is
    finished in the background and a new one waits for the streamer to come back."""

    def __init__(self, name: str, args: argparse.Namespace, clipping: bool, width: int):
        self.name, self.args, self.clipping, self.tag = name, args, clipping, name.ljust(width)
        self.chat: deque[tuple[float, str, str]] = deque(maxlen=4000)   # (time, user, text)
        self.counter = ChatCounter(args.per_user)
        self.lock = threading.Lock()
        self.det = SpikeDetector(args.ratio, args.min, args.window // BUCKET)
        self.merger = SpikeMerger()
        self.limiter = ClipLimiter(args.gap, args.max_per_hour)
        self.history_file = RUN_DIR / f"{name}.history.json"
        self.clip_now_file = RUN_DIR / f"{name}.clipnow"
        self.clip_now_file.unlink(missing_ok=True)           # a stale request from before doesn't count
        self._load_history()
        self.last_count = 0
        self.rec: Recorder | None = None
        self.jobs: queue.Queue | None = None
        self.cutter: threading.Thread | None = None
        self.finishers: list[threading.Thread] = []
        self.done = False                 # no more recording sessions will be started
        self.retry_at = 0.0
        self.backoff = 0
        if clipping:
            self.start_session()

    # ---- recording sessions

    def start_session(self) -> None:
        self.rec = Recorder(self.name, self.args.quality)
        self.jobs = queue.Queue()
        self.cutter = threading.Thread(target=cutter_loop, args=(self, self.rec, self.jobs))
        self.cutter.start()

    def end_session(self, stopped_by_us: bool) -> Recorder:
        """Stop the recorder, cut the clips still queued, fix the file extension (in background)."""
        rec, jobs, cutter = self.rec, self.jobs, self.cutter
        self.rec = self.jobs = self.cutter = None

        def run() -> None:
            rec.stop()
            jobs.put(None)
            cutter.join()
            if rec.path.exists() and not self.args.keep_recordings:
                rec.path.unlink(missing_ok=True)       # clips keep their own source cuts
                rec.path.with_suffix(".log").unlink(missing_ok=True)
                log(f"{self.name}: recording deleted (its clips keep their own source cuts)")
                return
            final = rec.finalize()
            if final is not None:
                log(f"{self.name}: recording saved: {final.relative_to(HOME).as_posix()}")
            elif stopped_by_us:              # it was only waiting for the stream: nothing to keep
                rec.path.with_suffix(".log").unlink(missing_ok=True)

        t = threading.Thread(target=run)
        t.start()
        self.finishers.append(t)
        return rec

    def poll(self) -> None:
        """Called from the main loop: notice a finished stream and (re)start recording."""
        if not self.clipping or self.done:
            return
        if self.rec is not None and not self.rec.alive():
            self.queue_moment(self.merger.flush())        # a moment right before the stream ended
            code = self.rec.proc.returncode
            rec = self.end_session(stopped_by_us=False)
            if rec.first_write is not None:
                log(f"{self.name}: stream ended")
                self.backoff = 0
            else:  # streamlink gave up without recording anything: don't restart in a tight loop
                self.backoff = min(max(30, self.backoff * 2), 300)
                log(f"{self.name}: streamlink exited without recording (code {code}); "
                    f"see {rec.path.with_suffix('.log').name}")
            if self.args.once:
                self.done = True
                return
            self.retry_at = time.time() + self.backoff
            log(f"{self.name}: waiting for the stream to come back"
                + (f" (retrying in {self.backoff}s)" if self.backoff else ""))
        if self.rec is None and time.time() >= self.retry_at:
            self.start_session()

    def shutdown(self) -> None:
        self.queue_moment(self.merger.flush())            # don't lose a moment that just happened
        self.done = True
        if self.rec is not None:
            self.end_session(stopped_by_us=True)

    def join(self) -> None:
        for t in self.finishers:
            t.join()

    @property
    def live(self) -> bool:
        return self.rec is not None and self.rec.first_write is not None

    # ---- chat history kept across restarts (no 3-minute warm-up after a settings change)

    def _load_history(self) -> None:
        try:
            data = json.loads(self.history_file.read_text(encoding="utf-8"))
            if time.time() - float(data["t"]) <= 120:           # only if it's fresh
                self.det.seed([int(n) for n in data["counts"]])
                log(f"{self.name}: kept the last {len(data['counts']) * BUCKET // 60} min of chat "
                    f"activity, so no warm-up needed")
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_history(self) -> None:
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            self.history_file.write_text(json.dumps({"t": time.time(), "counts": list(self.det.long)}),
                                         encoding="utf-8")
        except OSError:
            pass

    # ---- chat

    def hit(self, t: float, user: str, text: str) -> None:
        with self.lock:
            self.counter.add(int(t // BUCKET), user)
            if user.lower() not in BOTS:
                # kept even when over this chatter's quota: it's still what chat said (for titles),
                # it just doesn't count towards the spike
                self.chat.append((t, user, text))

    def chat_between(self, a: float, b: float) -> list[tuple[float, str, str]]:
        with self.lock:
            return [m for m in self.chat if a <= m[0] <= b]

    def close_bucket(self, idx: int) -> None:
        with self.lock:
            n = self.counter.pop(idx)
        start = idx * BUCKET
        spike, med = self.det.push(n)
        self.last_count = n
        self._save_history()
        ratio = f"x{n / med:.1f}" if med else "x-"
        line = f"{self.tag}  {hhmmss(start)}  {n:4d} msgs  median {med:5.1f}  {ratio:>6}"
        if not self.det.warm and not spike:
            line += f"  (warming up {len(self.det.history)}/{self.det.history.maxlen})"
        final = None
        if spike:
            final, note = self.merger.spike(Moment(start, n, med))
            line += f"  SPIKE ({note})"
        else:
            final = self.merger.tick(start)
        log(line)
        self.queue_moment(final)

    def queue_moment(self, m: Moment | None) -> None:
        """A chat moment is final (its peak is known): make it a clip, if the limits allow."""
        if m is None:
            return
        msg = self.queue_clip(m.bucket + self.args.offset, m.count, m.median)
        log(f"{self.name}: clip {hhmmss(m.bucket)} ({m.count} msgs) {msg}")

    def queue_clip(self, anchor: float, n: int, med: float, manual: bool = False) -> str:
        a = self.args
        clip_start, clip_end = anchor - a.before, anchor + a.after
        if not manual and (why := self.limiter.blocked(clip_start, anchor)):
            return f"skipped ({why})"
        if not self.clipping:
            self.limiter.record(clip_end, anchor, manual)
            return "(dry run)"
        if not self.live:
            return "skipped (not recording - stream offline)"
        if storage.low_disk():
            return f"skipped (disk almost full: {storage.free_gb():.1f} GB free)"
        name = f"{self.name}_{datetime.fromtimestamp(anchor):%Y%m%d-%H%M%S}_{n}msgs{'_manual' if manual else ''}.mp4"
        self.jobs.put(Clip(anchor, clip_start, clip_end, n, name, med))
        self.limiter.record(clip_end, anchor, manual)
        return f"-> {clip_file(name, self.name).relative_to(CLIP_DIR).as_posix()}"

    def check_clip_now(self) -> None:
        """The dashboard's "Clip now" button: clip the last 20 s and the next 10 s."""
        if not self.clip_now_file.exists():
            return
        self.clip_now_file.unlink(missing_ok=True)
        # a manual clip may overlap an automatic one, and ignores the gap and hourly limit:
        # the person asked for it
        msg = self.queue_clip(time.time(), self.last_count, self.det.baseline(), manual=True)
        log(f"{self.name}: CLIP NOW {msg}")


def _graceful_stop(signum, frame):
    raise KeyboardInterrupt            # same clean shutdown as Ctrl+C


def _cleanup_quietly() -> None:
    try:
        removed = storage.cleanup()
        if removed["sources"] or removed["clips"]:
            log(f"storage: removed {removed['clips']} old clip(s) and {removed['sources']} old source(s) "
                "(retention is set in the dashboard's Setup tab)")
    except Exception as e:  # never let housekeeping stop a recording
        log(f"storage: cleanup failed ({e})")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    preflight(args)
    for sig in ("SIGTERM", "SIGBREAK"):    # kill <pid> / Ctrl+Break on Windows
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _graceful_stop)

    clipping = not args.dry_run
    if clipping:
        REC_DIR.mkdir(parents=True, exist_ok=True)
        CLIP_DIR.mkdir(parents=True, exist_ok=True)
        if moved := storage.migrate_flat_clips():
            log(f"moved {moved} clip file(s) into one folder per streamer under clips/")

    width = max(map(len, args.channels))
    channels = {name: Channel(name, args, clipping, width) for name in args.channels}

    def on_message(name: str, t: float, user: str, text: str) -> None:
        ch = channels.get(name)
        if ch is not None and not ch.done:
            ch.hit(t, user, text)

    stop = threading.Event()
    threading.Thread(target=chat_loop, args=(list(channels), on_message, stop, log), daemon=True).start()

    next_idx = int(time.time() // BUCKET) + 1      # first *complete* bucket
    next_cleanup = time.time() + 60                # first one shortly after starting
    if args.dry_run:
        mode = "  [dry run]"
    elif args.once:
        mode = "  [--once: stops when a stream ends]"
    else:
        mode = "  [re-records when a stream comes back]"
    limits = f"; >= {args.gap:g}s between clips" + (f", max {args.max_per_hour}/hour" if args.max_per_hour else "")
    log(f"watching {', '.join('#' + c for c in channels)}: spike = >= {args.ratio}x median of last "
        f"{args.window}s and >= {args.min} msgs/{BUCKET}s{limits}{mode}")
    log("press Ctrl+C to stop")

    try:
        while True:
            time.sleep(0.2)
            if args.stop_file is not None and args.stop_file.exists():
                log("stopping (stop requested)...")
                break
            if OUTPUT_LOST.is_set():          # started by a dashboard that is gone: stop cleanly
                break
            for ch in channels.values():
                ch.poll()
                if not ch.done:
                    ch.check_clip_now()
            if all(ch.done for ch in channels.values()):
                log("all streams have ended")
                break
            # close every bucket that ended more than 0.5 s ago
            while time.time() >= (next_idx + 1) * BUCKET + 0.5:
                for ch in channels.values():
                    if not ch.done:
                        ch.close_bucket(next_idx)
                next_idx += 1
            if clipping and time.time() >= next_cleanup:
                next_cleanup = time.time() + CLEANUP_EVERY
                threading.Thread(target=_cleanup_quietly, daemon=True).start()
    except KeyboardInterrupt:
        log("stopping...")

    stop.set()
    try:
        if clipping:
            log("finishing queued clips (Ctrl+C again to abort)")
            for ch in channels.values():
                ch.shutdown()
            for ch in channels.values():
                ch.join()
    except KeyboardInterrupt:
        log("aborted")
        sys.exit(130)
    log("done")
