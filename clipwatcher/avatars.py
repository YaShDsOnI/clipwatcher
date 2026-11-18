"""Streamers' Twitch profile pictures, for the dashboard.

Taken from the public channel page's link-preview image (the same picture Twitch shows when a
channel link is shared), so no API key is needed. Each picture is cached in run/avatars/ and
refreshed in the background once a week. Only images on Twitch's own image server are accepted.
If a picture can't be fetched (offline, unknown channel), the dashboard keeps showing the letter.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from .config import RUN_DIR

AVATAR_DIR = RUN_DIR / "avatars"
MAX_AGE = 7 * 86400              # refresh a cached picture after a week
RETRY_AFTER = 3600               # after a failed fetch, wait an hour before trying that channel again
MAX_BYTES = 2_000_000
IMAGE_RE = re.compile(r"https://static-cdn\.jtvnw\.net/jtv_user_pictures/[\w./-]+?\.(png|jpe?g|webp|gif)")
META_RE = re.compile(r"<meta\b[^>]*>", re.I)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"

_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


def parse_avatar_url(html: str) -> str | None:
    """The profile picture URL from a channel page's og:image / twitter:image tag."""
    for tag in META_RE.findall(html):
        if re.search(r"""(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["']""", tag, re.I):
            m = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, re.I)
            if m and IMAGE_RE.fullmatch(m.group(1)):
                return m.group(1)
    return None


def _get(url: str, limit: int) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return res.read(limit + 1), res.headers.get("Content-Type", "")


def _cached(channel: str) -> Path | None:
    return next((p for p in AVATAR_DIR.glob(f"{channel}.*") if p.suffix != ".missing"), None) \
        if AVATAR_DIR.is_dir() else None


def _fetch(channel: str) -> Path | None:
    """Download the picture now. Returns the cached file, or None (and remembers the failure)."""
    missing = AVATAR_DIR / f"{channel}.missing"
    try:
        html, _ = _get(f"https://www.twitch.tv/{channel}", MAX_BYTES)
        url = parse_avatar_url(html.decode("utf-8", "replace"))
        if url is None:
            raise ValueError("no profile picture on the channel page")
        data, ctype = _get(url, MAX_BYTES)
        if len(data) > MAX_BYTES or not ctype.startswith("image/"):
            raise ValueError("not a usable image")
        ext = IMAGE_RE.fullmatch(url).group(1).replace("jpeg", "jpg")
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        old = _cached(channel)
        target = AVATAR_DIR / f"{channel}.{ext}"
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        if old is not None and old != target:
            old.unlink(missing_ok=True)
        missing.unlink(missing_ok=True)
        return target
    except (OSError, ValueError, urllib.error.URLError):
        try:
            AVATAR_DIR.mkdir(parents=True, exist_ok=True)
            missing.touch()
        except OSError:
            pass
        return None


def avatar(channel: str) -> Path | None:
    """The cached profile picture of a channel, fetching it the first time. A week-old picture is
    returned right away and refreshed in the background."""
    channel = channel.lower()
    if not re.fullmatch(r"\w{1,25}", channel):
        return None
    with _locks[channel]:
        cached = _cached(channel)
        if cached is not None:
            if time.time() - cached.stat().st_mtime > MAX_AGE:
                cached.touch()                       # one refresh at a time
                threading.Thread(target=_fetch, args=(channel,), daemon=True).start()
            return cached
        missing = AVATAR_DIR / f"{channel}.missing"
        if missing.exists() and time.time() - missing.stat().st_mtime < RETRY_AFTER:
            return None
        return _fetch(channel)
