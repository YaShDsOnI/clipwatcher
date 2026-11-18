"""clipper: turn one clip job into a finished vertical clip.

    clip-watcher render clips/.meta/<name>.json [more.json ...] [--retranscribe] [--retitle]

A job ("meta") file describes a short *source* cut of the stream (the clip plus a margin on
each side), the window inside it to publish, the settings, and the chat around the moment.
This process loads the AI models (Whisper, speaker detection) only for as long as it works and
then exits, so their memory goes back to the system. A machine-wide lock makes sure only one
clip is processed at a time, however many streamers are being recorded.
"""
from __future__ import annotations

import argparse
import contextlib
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .ai import AIUnavailable, ai_config, ai_rate, chat_title, hype_score
from .captions import FONT_FILE, OUT_H, OUT_W, Word, phrases_of, write_ass, write_srt
from .components import (SETUP_HINT, MissingComponent, ensure_model, has_module, whisper_downloaded,
                         whisper_model_name)
from .config import FONT_DIR, HOME, RUN_DIR, WORK_DIR, clip_file, log
from .storage import migrate_flat_clips

CPU_ONLY_MARK = RUN_DIR / "whisper-cpu-only"   # delete to make Whisper try the GPU again
SPLIT_TOP = 768                         # split layout: facecam band height (gameplay gets the rest)
AUDIO_FILTER = "dynaudnorm=f=150:g=15,loudnorm"   # level quiet voices up before transcription

LAYOUTS = ("blur", "crop", "split")
DEFAULT_SETTINGS = {
    "layout": "blur", "template": "karaoke", "captions": True, "speakers": True,
    "caption_words": 4, "speaker_threshold": 0.3, "title": "auto", "facecam": None,
    "title_in_video": False,          # the hook title is shown in the dashboard; burned in only if asked
    "model": "base", "language": None,
}


def default_threads() -> int:
    return max(2, min(4, (os.cpu_count() or 4) // 2))


def run_ffmpeg(args: list[str], threads: int | None = None, cwd: Path | None = None) -> tuple[bool, str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    if threads:
        cmd[5:5] = ["-threads", str(threads)]          # applies to the output encoder
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or HOME)
    return res.returncode == 0, res.stderr.strip()[-500:]


def media_duration(path: Path) -> float:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def save_meta(path: Path, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- one at a time

@contextlib.contextmanager
def render_lock():
    """Machine-wide lock: only one clip is processed at a time, across every streamer."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    f = open(RUN_DIR / "render.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


# --------------------------------------------------------------------------- video

def video_filter(layout: str, facecam, ass_file: str | None, size: tuple[int, int] = (OUT_W, OUT_H),
                 src: str = "[0:v]", out: str = "[v]") -> str:
    """ffmpeg filter graph that frames a landscape video into a WxH output (1080x1920 by default).
    ass_file is a plain file name in the work folder (ffmpeg runs there), so no Windows drive
    letters end up inside the filter string. src/out are the graph labels to read and write."""
    w_out, h_out = size
    tag = out.strip("[]")                  # keeps the inner labels unique when used inside a bigger graph
    if layout == "split" and facecam:
        x, y, w, h = (min(max(float(v), 0.0), 1.0) for v in facecam)
        top = SPLIT_TOP if size == (OUT_W, OUT_H) else round(h_out * 0.4 / 2) * 2
        bottom_h = h_out - top
        v = (f"{src}split[{tag}a][{tag}b];"
             # top: the streamer's facecam, filled to the band
             f"[{tag}a]crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},"
             f"scale={w_out}:{top}:force_original_aspect_ratio=increase,crop={w_out}:{top}[{tag}top];"
             # bottom: the gameplay, centre-cropped to the remaining shape
             f"[{tag}b]crop=trunc(ih*{w_out / bottom_h:.5f}/2)*2:ih,scale={w_out}:{bottom_h}[{tag}bot];"
             f"[{tag}top][{tag}bot]vstack,setsar=1")
    elif layout == "crop":
        ratio = "9/16" if size == (OUT_W, OUT_H) else f"{w_out / h_out:.5f}"
        v = f"{src}crop=trunc(ih*{ratio}/2)*2:ih,scale={w_out}:{h_out},setsar=1"
    else:
        bw, bh = w_out // 4 // 2 * 2, h_out // 4 // 2 * 2
        v = (f"{src}split[{tag}a][{tag}b];"
             f"[{tag}a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=8:2,"
             f"scale={w_out}:{h_out},eq=brightness=-0.28[{tag}bg];"
             f"[{tag}b]scale={w_out}:-2[{tag}fg];[{tag}bg][{tag}fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    if ass_file:
        v += f",ass={ass_file}:fontsdir=fonts"
    return v + out


def fonts_in_work_dir() -> None:
    """The caption font next to the .ass file, where ffmpeg (running in the work folder) finds it."""
    src = FONT_DIR / FONT_FILE
    if not src.exists():
        return
    dst = WORK_DIR / "fonts" / FONT_FILE
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


# --------------------------------------------------------------------------- speech + speakers

class Transcriber:
    """faster-whisper if installed, otherwise openai-whisper. Only uses models already on disk."""

    def __init__(self, model: str, language: str | None, threads: int):
        self.name, self.language, self.threads = model, language, threads
        if has_module("faster_whisper"):
            if not whisper_downloaded(model):
                raise MissingComponent(f"the Whisper '{model}' model isn't downloaded")
            self.kind = "faster-whisper"
            # once the GPU has failed (CUDA libraries missing), go straight to the CPU next time
            self._load_faster("cpu" if CPU_ONLY_MARK.exists() else "auto")
        elif has_module("whisper"):
            if not whisper_downloaded(model):
                raise MissingComponent(f"the Whisper '{model}' model isn't downloaded")
            import whisper
            self.kind = "openai-whisper"
            self.model = whisper.load_model(model)
        else:
            raise MissingComponent("faster-whisper isn't installed")

    def _load_faster(self, device: str) -> None:
        from faster_whisper import WhisperModel
        self.device = device
        self.model = WhisperModel(self.name, device=device, cpu_threads=self.threads,
                                  compute_type="int8" if device == "cpu" else "auto",
                                  local_files_only=True)        # never download on its own

    def transcribe(self, media: Path) -> tuple[list[tuple[float, float, str]], list[Word]]:
        def word(start, end, text):
            return Word(start, end, re.sub(r"[{}\\]", "", text).strip())

        if self.kind == "faster-whisper":
            try:
                # gentle speech detection: the default drops quieter second voices
                segs, _ = self.model.transcribe(
                    str(media), language=self.language, word_timestamps=True,
                    condition_on_previous_text=False, vad_filter=True,
                    vad_parameters={"threshold": 0.25, "min_silence_duration_ms": 300})
                segs = list(segs)
            except RuntimeError as e:
                # a GPU is present but the CUDA runtime isn't installed: only fails on first use
                if self.device == "cpu" or not re.search(r"cu(blas|dnn|da)|\.dll|\.so", str(e), re.I):
                    raise
                log("whisper: GPU unavailable (CUDA libraries missing); using the CPU from now on")
                RUN_DIR.mkdir(parents=True, exist_ok=True)
                CPU_ONLY_MARK.touch()
                self._load_faster("cpu")
                return self.transcribe(media)
            segments = [(s.start, s.end, s.text) for s in segs]
            words = [word(w.start, w.end, w.word) for s in segs for w in (s.words or [])]
        else:
            res = self.model.transcribe(str(media), language=self.language, word_timestamps=True,
                                        condition_on_previous_text=False)
            segments = [(s["start"], s["end"], s["text"]) for s in res["segments"]]
            words = [word(w["start"], w["end"], w["word"]) for s in res["segments"] for w in s.get("words", [])]
        return segments, [w for w in words if w.text]


class Diarizer:
    """Who spoke which phrase: sherpa-onnx diarization decides how many people talk, then each
    phrase's own voice embedding moves it to the speaker it sounds most like."""

    def __init__(self, threshold: float, threads: int):
        if not has_module("sherpa_onnx"):
            raise MissingComponent("sherpa-onnx isn't installed")
        import sherpa_onnx
        voice_model = str(ensure_model("wespeaker_en_voxceleb_resnet34_LM.onnx"))
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(ensure_model("pyannote-segmentation-3.0.onnx"))),
                num_threads=threads),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=voice_model, num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
            min_duration_on=0.3, min_duration_off=0.5,
        )
        self.sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
        self.voices = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=voice_model, num_threads=threads))

    def _voice(self, audio, start: float, end: float):
        import numpy as np
        mid, half = (start + end) / 2, max((end - start) / 2, 0.75)
        a, b = int(max(0.0, mid - half) * 16000), int(min(len(audio) / 16000, mid + half) * 16000)
        stream = self.voices.create_stream()
        stream.accept_waveform(16000, audio[a:b])
        stream.input_finished()
        v = np.array(self.voices.compute(stream))
        return v / (np.linalg.norm(v) or 1.0)

    def assign(self, wav_path: Path, words: list[Word]) -> int:
        import numpy as np
        if not words:
            return 0
        with wave.open(str(wav_path)) as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        turns = [(r.start, r.end, r.speaker) for r in self.sd.process(audio).sort_by_start_time()]
        phrases = phrases_of(words)
        if len({k for _, _, k in turns}) < 2:
            for w in words:
                w.speaker = 0
            return 1
        labels: list[int] = []
        for p in phrases:
            overlap: Counter[int] = Counter()
            for s, e, k in turns:
                o = min(e, p[-1].end) - max(s, p[0].start)
                if o > 0:
                    overlap[k] += o
            labels.append(overlap.most_common(1)[0][0] if overlap else (labels[-1] if labels else turns[0][2]))
        voices = np.array([self._voice(audio, p[0].start, p[-1].end) for p in phrases])
        length = np.array([max(p[-1].end - p[0].start, 0.3) for p in phrases])
        for _ in range(5):
            centroids = {}
            for k in set(labels):
                idx = [i for i, l in enumerate(labels) if l == k]
                c = (voices[idx] * length[idx, None]).sum(0)
                centroids[k] = c / (np.linalg.norm(c) or 1.0)
            new = [max(centroids, key=lambda k: float(voices[i] @ centroids[k])) for i in range(len(phrases))]
            if new == labels:
                break
            labels = new
        # fold "speakers" with only a sliver of talk time (game audio, a stray voice) into the
        # closest real one, so a caption colour always means a person who actually talks
        while True:
            talk: Counter[int] = Counter()
            for p, k in zip(phrases, labels, strict=True):
                talk[k] += max(p[-1].end - p[0].start, 0.3)
            total = sum(talk.values())
            k_min, t_min = min(talk.items(), key=lambda kv: kv[1])
            if len(talk) < 2 or (t_min >= max(2.0, 0.12 * total) and len(talk) <= 4):
                break
            keep = {}
            for k in talk:
                if k != k_min:
                    idx = [i for i, l in enumerate(labels) if l == k]
                    c = (voices[idx] * length[idx, None]).sum(0)
                    keep[k] = c / (np.linalg.norm(c) or 1.0)
            labels = [max(keep, key=lambda k: float(voices[i] @ keep[k])) if l == k_min else l
                      for i, l in enumerate(labels)]
        talk = Counter()
        for p, k in zip(phrases, labels, strict=True):
            talk[k] += p[-1].end - p[0].start
        rank = {k: i for i, (k, _) in enumerate(talk.most_common())}
        for p, k in zip(phrases, labels, strict=True):
            for w in p:
                w.speaker = rank[k]
        return len(talk)


# --------------------------------------------------------------------------- the job

def render(meta_path: Path, threads: int, retranscribe: bool = False, retitle: bool = False) -> bool:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    settings = {**DEFAULT_SETTINGS, **(meta.get("settings") or {})}
    name = meta["name"]
    source = HOME / meta["source"]
    out = clip_file(name, meta.get("channel"))
    if not source.exists():
        meta["status"], meta["error"] = "failed", "source video is missing"
        save_meta(meta_path, meta)
        log(f"clip failed: {name}: source video is missing")
        return False
    meta["status"] = "rendering"
    meta.pop("error", None)
    save_meta(meta_path, meta)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem
    wav, ass, tmp_video = WORK_DIR / f"{stem}.wav", WORK_DIR / f"{stem}.ass", WORK_DIR / name
    try:
        # 1. words + speakers for the whole source cut (cached in the meta for re-edits)
        if retranscribe or "words" not in meta or meta.get("captions_missing"):
            ok, err = run_ffmpeg(["-i", str(source), "-vn", "-af", AUDIO_FILTER,
                                  "-ac", "1", "-ar", "16000", str(wav)])
            words: list[Word] = []
            segments: list = []
            speakers = 0
            if not ok:
                log(f"audio extract failed for {name}: {err}")
            else:
                meta.pop("captions_missing", None)
                try:
                    tr = Transcriber(whisper_model_name(settings), settings["language"], threads)
                    segments, words = tr.transcribe(wav)
                    del tr
                    gc.collect()
                except MissingComponent as e:
                    meta["captions_missing"] = True     # re-rendering after Setup adds the captions
                    log(f"captions: skipped for {name}, {e}. Approve it in the dashboard's Setup tab "
                        f"or run: {SETUP_HINT}")
                except Exception as e:
                    log(f"whisper failed on {name}: {e}")
                if words and settings["speakers"]:
                    try:
                        d = Diarizer(float(settings["speaker_threshold"]), threads)
                        speakers = d.assign(wav, words)
                        del d
                        gc.collect()
                    except MissingComponent as e:
                        log(f"speakers: one caption colour for {name}, {e} (optional; see Setup)")
                    except Exception as e:
                        log(f"speakers: skipped for {name} ({e})")
                speakers = speakers or (1 if words else 0)
            meta["words"] = [asdict(w) for w in words]
            meta["segments"] = segments
            meta["speakers"] = speakers
        words = [Word(**w) for w in meta.get("words", [])]
        start, end = float(meta["start"]), float(meta["end"])

        # 2. title + scores
        meta["hype"] = hype_score(meta.get("msgs", 0), meta.get("median", 0))
        mode = settings.get("title", "auto")
        if mode == "off":
            meta["title"] = None
        elif retitle or "title" not in meta:
            meta.pop("ai", None)
            result = None
            cfg = ai_config() if mode == "auto" else None
            if cfg:
                try:
                    result = ai_rate(meta, words, cfg)
                    log(f"titles: written by {cfg['provider']} ({cfg['model']})")
                except AIUnavailable as e:
                    log(f"titles: AI not used ({e}); title from chat instead")
                except Exception as e:
                    log(f"titles: {cfg['provider']} failed ({e}); title from chat instead")
            if result:
                meta["title"], meta["ai"] = result["title"], result
            else:
                meta["title"] = chat_title(meta.get("chat", []))

        # 3. captions for the published window, then one render
        win = [Word(w.start - start, w.end - start, w.text, w.speaker) for w in words if start <= w.start < end]
        talk: Counter[int] = Counter()                  # colours ranked within what's published:
        for w in win:                                   # the main voice of the clip is white
            talk[w.speaker] += w.end - w.start
        rank = {k: i for i, (k, _) in enumerate(talk.most_common())}
        for w in win:
            w.speaker = rank[w.speaker]
        segs = [(a - start, b - start, t) for a, b, t in meta.get("segments", []) if b > start and a < end]
        write_srt(segs, out.with_suffix(".srt"))
        title = meta.get("title") if mode != "off" and settings.get("title_in_video") else None
        if (win and settings["captions"]) or title:
            write_ass(win, ass, settings, title)
            fonts_in_work_dir()
        layout = settings["layout"]
        if layout == "split" and not settings.get("facecam"):
            log(f"{name}: split layout needs a facecam box (set it in the dashboard); using blur")
            layout = "blur"
        vf = video_filter(layout, settings.get("facecam"), ass.name if ass.exists() else None)
        ok, err = run_ffmpeg(["-ss", f"{start:.3f}", "-i", str(source), "-t", f"{end - start:.3f}",
                              "-filter_complex", vf, "-map", "[v]", "-map", "0:a:0?",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                              "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp_video)],
                             threads=threads, cwd=WORK_DIR)
        if not ok or not tmp_video.exists() or tmp_video.stat().st_size < 10_000:
            raise RuntimeError(f"ffmpeg: {err or 'empty output'}")
        for _ in range(20):                # the dashboard may be streaming the old version
            try:
                tmp_video.replace(out)
                break
            except PermissionError:
                time.sleep(0.25)
        else:
            raise RuntimeError("could not replace the old clip file (still open?)")
        n = len({w.speaker for w in win})              # speakers heard in the published window
        meta["window_speakers"] = n
        meta["status"] = "done"
        meta["rendered_at"] = datetime.now().isoformat(timespec="seconds")
        save_meta(meta_path, meta)
        title_note = f", title: {meta['title']}" if meta.get("title") else ""
        log(f"clip saved: {out.relative_to(HOME).as_posix()} ({end - start:.0f}s, {len(win)} words captioned, "
            f"{n} speaker{'s' if n != 1 else ''}{title_note})")
        return True
    except Exception as e:
        meta["status"], meta["error"] = "failed", str(e)[:300]
        save_meta(meta_path, meta)
        log(f"clip failed: {name}: {e}")
        return False
    finally:
        for f in (wav, ass, tmp_video):
            f.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="clip-watcher render", description="Render one or more clip jobs.")
    p.add_argument("jobs", nargs="+", type=Path, help="job files, e.g. clips/.meta/<clip>.json")
    p.add_argument("--threads", type=int, default=default_threads(),
                   help="CPU threads for AI and encoding (fewer = lighter on the rest of the PC)")
    p.add_argument("--retranscribe", action="store_true", help="redo captions and speakers")
    p.add_argument("--retitle", action="store_true", help="write a new hook title")
    args = p.parse_args(argv)
    migrate_flat_clips()
    ok = True
    with render_lock():
        for job in args.jobs:
            ok &= render(job.resolve(), args.threads, args.retranscribe, args.retitle)
    sys.exit(0 if ok else 1)
