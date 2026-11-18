"""Build share/clip-watcher.zip: the download for people who just want to double-click and go.

    python tools/build_zip.py

The zip holds the program only (no clips, keys, models or logs). Attach it to a GitHub Release.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["Start clip-watcher.bat", "SETUP GUIDE.txt", "README.md", "LICENSE", "pyproject.toml",
         "dashboard.sh", "watch.sh", "streamers.example.json"]


def main() -> None:
    out = ROOT / "share" / "clip-watcher.zip"
    out.parent.mkdir(exist_ok=True)
    paths = [ROOT / f for f in FILES]
    paths += sorted(p for p in (ROOT / "clipwatcher").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    missing = [p for p in paths if not p.exists()]
    if missing:
        sys.exit("missing: " + ", ".join(str(p.relative_to(ROOT)) for p in missing))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, "clip-watcher/" + p.relative_to(ROOT).as_posix())
    print(f"wrote {out.relative_to(ROOT)} ({len(paths)} files, {out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
