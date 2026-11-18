"""The optional parts (captions, speaker colours, Claude) and the opt-in setup that installs them.

Nothing here downloads or installs anything on its own: every install is approved by the user,
either in the dashboard's Setup tab or by answering yes in `clip-watcher setup`.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

from .config import MODEL_DIR, env_setting, log, read_env_file

SETUP_HINT = "python -m clipwatcher setup"      # works from the project folder, installed or not

DIARIZATION_MODELS = {
    "pyannote-segmentation-3.0.onnx": (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
        "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"),
    "wespeaker_en_voxceleb_resnet34_LM.onnx": (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
        "wespeaker_en_voxceleb_resnet34_LM.onnx", None),
}

WHISPER_SIZES = {
    "tiny": ("~75 MB", "least RAM and fastest; rougher captions"),
    "base": ("~145 MB", "good balance (default)"),
    "small": ("~485 MB", "best captions; about twice the RAM while a clip is made"),
}

# Python versions faster-whisper's compiled parts (ctranslate2, onnxruntime, av) ship for.
# Newer Pythons come out before those packages catch up; pip then fails after downloading.
CAPTIONS_PYTHON = ((3, 12), (3, 14))


class MissingComponent(Exception):
    """A part that has to be downloaded or installed first. Nothing is ever fetched on its own:
    approve it in the dashboard's Setup tab or with `python -m clipwatcher setup`."""


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


# --------------------------------------------------------------------------- models on disk

def ensure_model(name: str, download: bool = False) -> Path:
    """Path to a speaker model in models/. Downloads only when asked to (from Setup)."""
    path = MODEL_DIR / name
    if path.exists():
        return path
    if not download:
        raise MissingComponent("the speaker models aren't downloaded")
    url, member = DIARIZATION_MODELS[name]
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    log(f"speakers: downloading {name}")
    tmp = path.with_name(path.name + ".part")
    urllib.request.urlretrieve(url, tmp)
    if member:
        with tarfile.open(tmp) as tar:
            path.write_bytes(tar.extractfile(member).read())
        tmp.unlink()
    else:
        tmp.replace(path)
    return path


def speaker_models_downloaded() -> bool:
    return all((MODEL_DIR / n).exists() for n in DIARIZATION_MODELS)


def whisper_model_name(settings: dict | None = None) -> str:
    """The caption model: WHISPER_MODEL in .env (chosen in Setup), else the job's, else base."""
    return env_setting("WHISPER_MODEL") or (settings or {}).get("model") or "base"


def _hf_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"


def whisper_downloaded(name: str) -> bool:
    """Is this Whisper model on disk already? (looks at files only: no heavy imports)"""
    if has_module("faster_whisper"):
        snaps = _hf_cache() / f"models--Systran--faster-whisper-{name}" / "snapshots"
        return any((s / "model.bin").exists() for s in snaps.glob("*")) if snaps.exists() else False
    if has_module("whisper"):
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "whisper"
        return any(root.glob(f"{name}.pt")) or any(root.glob(f"{name}*.pt"))
    return False


# --------------------------------------------------------------------------- Python checks

def python_label() -> str:
    bits = "64-bit" if sys.maxsize > 2**32 else "32-bit"
    arm = ", ARM" if platform.machine().upper() in ("ARM64", "AARCH64") else ""
    return f"Python {sys.version.split()[0]} ({bits}{arm})"


def python_fix() -> str:
    hi = CAPTIONS_PYTHON[1]
    return (f"Install Python {hi[0]}.{hi[1]} (64-bit) from python.org/downloads/windows and start "
            f"clip-watcher again; SETUP GUIDE.txt step 1 explains it.")


def captions_python_problem() -> str | None:
    """Why faster-whisper can't be installed on this Python, if we know (None = should work)."""
    lo, hi = CAPTIONS_PYTHON
    v = sys.version_info[:2]
    fix = python_fix()
    if sys.maxsize <= 2**32:
        return f"Captions need 64-bit Python; this is {python_label()}. {fix}"
    if v > hi:
        return (f"This is {python_label()}, which is newer than faster-whisper supports yet "
                f"(it needs Python {lo[0]}.{lo[1]} to {hi[0]}.{hi[1]}). {fix}")
    if v < lo:
        return f"This is {python_label()}, which is too old for faster-whisper. {fix}"
    if os.name == "nt" and platform.machine().upper() == "ARM64":
        return "faster-whisper has no Windows-on-ARM version (ctranslate2 is x64 only)."
    return None


def explain_pip_failure(output: str, package: str) -> str:
    """One readable line from pip's output saying why it failed."""
    lo, hi = CAPTIONS_PYTHON
    python_ok = sys.maxsize > 2**32 and lo <= sys.version_info[:2] <= hi
    fix = ("Check the internet connection and try again." if python_ok else python_fix())
    m = (re.search(r"No matching distribution found for ([\w.\-\[\]]+)", output)
         or re.search(r"Could not find a version that satisfies the requirement ([\w.\-\[\]]+)", output))
    if m:
        return f"{m.group(1)} has no version for {python_label()} yet. {fix}"
    if re.search(r"ResolutionImpossible|conflicting dependencies", output):
        dep = re.search(r"depends on ([A-Za-z0-9_.\-]+)", output)
        part = dep.group(1) if dep else f"A part {package} needs"
        return f"{part} has no version for {python_label()} yet. {fix}"
    if re.search(r"Microsoft Visual C\+\+|subprocess-exited-with-error|Failed to build", output):
        return f"pip had to build a part from source code and couldn't ({python_label()}). {fix}"
    if re.search(r"Errno 28|No space left", output):
        return "Not enough free disk space."
    if re.search(r"Errno 13|Permission denied|Access is denied", output):
        return "Windows denied access: close other clip-watcher / Python windows and try again."
    if re.search(r"ConnectionError|Read timed out|Max retries exceeded|getaddrinfo failed|SSLError", output):
        return "Couldn't reach pypi.org: check the internet connection (or a firewall / VPN) and try again."
    lines = [ln.strip() for ln in output.splitlines() if ln.strip().startswith(("ERROR", "error"))]
    return lines[-1][:300] if lines else f"pip failed; run  py -m pip install {package}  in a terminal to see why."


# --------------------------------------------------------------------------- the parts

def components() -> list[dict]:
    """Everything the tool can use, what it's for, and whether it's on this PC."""
    rows = [
        {"id": "ffmpeg", "label": "ffmpeg", "purpose": "Cuts, crops and renders the clips", "required": True,
         "size": "~200 MB", "installed": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
         "manual": "winget install Gyan.FFmpeg   (then open a new terminal)"},
        {"id": "streamlink", "label": "streamlink", "purpose": "Records the live stream", "required": True,
         "size": "~10 MB", "installed": has_module("streamlink") or bool(shutil.which("streamlink")),
         "package": "streamlink"},
        {"id": "faster-whisper", "label": "faster-whisper", "purpose": "Turns speech into captions",
         "required": False, "size": "~60 MB", "installed": has_module("faster_whisper"), "package": "faster-whisper",
         "warning": None if has_module("faster_whisper") else captions_python_problem()},
    ]
    current = whisper_model_name()
    for size, (mb, note) in WHISPER_SIZES.items():
        rows.append({"id": f"whisper-{size}", "label": f"Whisper {size} model", "group": "whisper",
                     "purpose": f"Caption model: {note}", "required": False, "size": mb,
                     "installed": whisper_downloaded(size), "needs": "faster-whisper", "selected": size == current})
    rows += [
        {"id": "sherpa-onnx", "label": "sherpa-onnx", "purpose": "Tells speakers apart (caption colour per person)",
         "required": False, "size": "~30 MB", "installed": has_module("sherpa_onnx"), "package": "sherpa-onnx"},
        {"id": "speaker-models", "label": "Speaker models", "purpose": "pyannote segmentation + WeSpeaker voices, for speaker colours",
         "required": False, "size": "~32 MB", "installed": speaker_models_downloaded(), "needs": "sherpa-onnx"},
        {"id": "anthropic", "label": "anthropic", "purpose": "Only for Claude hook titles (Gemini, Groq, OpenAI etc. need nothing)",
         "required": False, "size": "~5 MB", "installed": has_module("anthropic"), "package": "anthropic>=2.0"},
    ]
    return rows


def install_component(cid: str) -> bool:
    """Download / install one component. Only ever called after the user approved it."""
    comp = next((c for c in components() if c["id"] == cid), None)
    if comp is None:
        log(f"unknown component: {cid}")
        return False
    if comp.get("manual"):
        log(f"{comp['label']} has to be installed by you: {comp['manual']}")
        return False
    if comp.get("package"):
        log(f"installing {comp['package']} with pip into {python_label()}...")
        proc = subprocess.Popen([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                                 comp["package"]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        out = []
        for line in proc.stdout:                 # pass pip's progress through as it happens
            out.append(line)
            print(line, end="", flush=True)
        code = proc.wait()
        importlib.invalidate_caches()
        if code != 0:
            log("FAILED: " + explain_pip_failure("".join(out), comp["package"]))
            return False
        return True
    if cid.startswith("whisper-"):
        if not has_module("faster_whisper"):
            log("install faster-whisper first")
            return False
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper.utils import download_model
        size = cid.split("-", 1)[1]
        log(f"downloading the Whisper {size} model ({WHISPER_SIZES[size][0]}) from Hugging Face...")
        download_model(size)
        return whisper_downloaded(size)
    if cid == "speaker-models":
        for name in DIARIZATION_MODELS:
            ensure_model(name, download=True)
        return True
    return False


def setup_cli(ids: list[str], yes: bool) -> int:
    from .ai import ai_config

    rows = components()
    print("\nclip-watcher parts (nothing is downloaded or installed without your OK):\n")
    for c in rows:
        mark = "installed" if c["installed"] else ("REQUIRED, missing" if c["required"] else "not installed")
        sel = "  <- caption model in use" if c.get("selected") else ""
        print(f"  {c['id']:16} {mark:18} {c['size']:>8}  {c['purpose']}{sel}")
    uses_claude = ((ai_config() or {}).get("provider") == "claude"
                   or read_env_file().get("AI_PROVIDER", "").lower() == "claude")
    todo = [c for c in rows if (c["id"] in ids if ids else not c["installed"] and not c.get("manual")
                                and (c.get("group") != "whisper" or c.get("selected"))
                                and (c["id"] != "anthropic" or uses_claude))]
    for c in rows:
        if c.get("manual") and not c["installed"]:
            print(f"\n  {c['label']} is missing: install it yourself with:  {c['manual']}")
    if not todo:
        print("\nNothing to download.")
        return 0
    ok = True
    for c in todo:
        if c["installed"] and c["id"] not in ids:
            continue
        if not yes:
            try:
                answer = input(f"\nDownload/install {c['label']} ({c['size']}, {c['purpose'].lower()})? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print(f"  skipped {c['label']}")
                continue
        ok &= install_component(c["id"])
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="clip-watcher setup",
                                description="See which parts are installed; download/install them (asks first).")
    p.add_argument("ids", nargs="*", help="only these parts, e.g. whisper-tiny speaker-models anthropic")
    p.add_argument("--yes", action="store_true", help="don't ask (you already approved, e.g. in the dashboard)")
    args = p.parse_args(argv)
    sys.exit(setup_cli(args.ids, args.yes))
