"""Twitch chat: read it, count it, and decide which moments become clips.

    chat_loop       anonymous IRC reader (no token needed)
    ChatCounter     messages per 10 s bucket; one chatter (or a bot) can't fake a spike on their own
    SpikeDetector   is this bucket far above the usual level?
    SpikeMerger     back-to-back spike buckets become ONE moment, at their peak
    ClipLimiter     a gap between clips and an optional hourly cap, so a wild chat isn't 100 clips
"""
from __future__ import annotations

import math
import re
import socket
import statistics
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

BUCKET = 10                      # seconds per chat bucket
IRC_HOST, IRC_PORT = "irc.chat.twitch.tv", 6667
IRC_NICK = "justinfan12345"      # anonymous, read-only login: no token needed

# chat bots post on their own schedule (timers, alerts, giveaways); they're not chat reacting
BOTS = frozenset({
    "nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "soundalerts",
    "sery_bot", "botrixoficial", "kofistreambot", "pokemoncommunitygame", "blerp", "own3d",
    "streamstickers", "commanderroot", "lolrankbot", "tangiabot", "fourthwallhq",
})

PRIVMSG_RE = re.compile(r"^:(\w+)!\S* PRIVMSG #(\w+) :(.*)$")
JOIN_RE = re.compile(r" JOIN #(\w+)")


def hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def chat_loop(channels: list[str], on_message: Callable[[str, float, str, str], None],
              stop: threading.Event, log: Callable[[str], None] = print) -> None:
    """Read chat of all channels anonymously over one IRC connection.

    Calls on_message(channel, t, user, text) for every PRIVMSG. Reconnects forever."""
    backoff = 1
    join = "JOIN " + ",".join(f"#{c}" for c in channels)
    while not stop.is_set():
        try:
            with socket.create_connection((IRC_HOST, IRC_PORT), timeout=15) as sock:
                sock.sendall(f"NICK {IRC_NICK}\r\n{join}\r\n".encode())
                sock.settimeout(1.0)
                buf, last_rx, pinged = b"", time.time(), False
                while not stop.is_set():
                    try:
                        data = sock.recv(65536)
                    except socket.timeout:
                        idle = time.time() - last_rx
                        if idle > 240 and not pinged:           # quiet channel: check the link is alive
                            sock.sendall(b"PING :clip-watcher\r\n")
                            pinged = True
                        elif idle > 300:
                            raise ConnectionError("no data from chat for 5 min") from None
                        continue
                    if not data:
                        raise ConnectionError("server closed the connection")
                    now = time.time()
                    last_rx, pinged, backoff = now, False, 1
                    *lines, buf = (buf + data).split(b"\r\n")
                    for raw in lines:
                        line = raw.decode("utf-8", "replace")
                        if line.startswith("PING"):
                            sock.sendall(("PONG" + line[4:] + "\r\n").encode())
                        elif m := PRIVMSG_RE.search(line):
                            on_message(m.group(2), now, m.group(1), m.group(3))
                        elif m := JOIN_RE.search(line):
                            log(f"chat: joined #{m.group(1)}")
                        elif " RECONNECT" in line:
                            raise ConnectionError("server asked us to reconnect")
                        elif " NOTICE " in line:
                            log(f"chat notice: {line.split(' :', 1)[-1]}")
        except OSError as e:  # ConnectionError and socket errors
            if stop.is_set():
                break
            log(f"chat: {e}; reconnecting in {backoff}s")
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)


class ChatCounter:
    """Messages per bucket, where each chatter counts at most `per_user` times per bucket and known
    bots not at all. Without this, one person spamming (or a bot's timer) can look like a spike."""

    def __init__(self, per_user: int = 2):
        self.per_user = per_user          # 0 = no limit
        self.counts: Counter[int] = Counter()
        self.by_user: Counter[tuple[int, str]] = Counter()

    def add(self, idx: int, user: str) -> bool:
        """Count one message in bucket idx. False if it didn't count (a bot, or this chatter's
        quota for the bucket is used up)."""
        user = user.lower()
        if user in BOTS:
            return False
        if self.per_user:
            self.by_user[idx, user] += 1
            if self.by_user[idx, user] > self.per_user:
                return False
        self.counts[idx] += 1
        return True

    def pop(self, idx: int) -> int:
        """The count of bucket idx; forgets it and every older bucket."""
        n = self.counts.pop(idx, 0)
        for k in [k for k in self.counts if k < idx]:
            del self.counts[k]
        for k in [k for k in self.by_user if k[0] <= idx]:
            del self.by_user[k]
        return n


class SpikeDetector:
    """A bucket is a spike if count >= ratio * baseline and count >= min_count.

    The baseline is the *lower* of two medians: the last `window` buckets (3 min) and the
    last 10 minutes. With only the short one, chat that stays insane for a few minutes
    becomes the new normal and nothing clips; the 10-minute median remembers what the
    stream was like before the hype started."""

    LONG_BUCKETS = 60                  # 10 minutes of 10 s buckets

    def __init__(self, ratio: float, min_count: int, window_buckets: int):
        self.ratio, self.min_count = ratio, min_count
        self.history: deque[int] = deque(maxlen=window_buckets)
        self.long: deque[int] = deque(maxlen=max(self.LONG_BUCKETS, window_buckets))

    @property
    def warm(self) -> bool:
        return len(self.history) == self.history.maxlen

    def baseline(self) -> float:
        if not self.history:
            return 0.0
        med = statistics.median(self.history)
        if len(self.long) > len(self.history):        # only once it knows more than the short window
            med = min(med, statistics.median(self.long))
        return med

    def push(self, count: int) -> tuple[bool, float]:
        med = self.baseline()
        spike = self.warm and count >= self.min_count and count >= self.ratio * med
        self.history.append(count)
        self.long.append(count)
        return spike, med

    def seed(self, counts: list[int]) -> None:
        for n in counts[-self.long.maxlen:]:
            self.long.append(n)
        for n in counts[-self.history.maxlen:]:
            self.history.append(n)


@dataclass
class Moment:
    """A moment chat reacted to: the bucket it peaked in and how big the reaction was."""
    bucket: float          # wall-clock start of the peak bucket
    count: int
    median: float


class SpikeMerger:
    """Chat that stays wild for 30 s used to give a clip every 30 s, all of the same moment.
    Now spike buckets close together are one moment: the clip is centred on the biggest bucket.
    A moment is final once `window` seconds pass after its peak without a bigger bucket."""

    def __init__(self, window: float = 2 * BUCKET):
        self.window = window
        self.pending: Moment | None = None

    def spike(self, m: Moment) -> tuple[Moment | None, str]:
        """Add a spike bucket. Returns (a moment that is now final or None, a note for the log)."""
        p = self.pending
        if p is not None and m.bucket - p.bucket <= self.window:
            if m.count > p.count:
                self.pending = m
                return None, "peak so far"
            return None, f"same moment as {hhmmss(p.bucket)}"
        self.pending = m
        return p, "waiting for the peak"

    def tick(self, bucket: float) -> Moment | None:
        """A quiet bucket started at `bucket`: returns the pending moment if it is now final."""
        p = self.pending
        if p is not None and bucket - p.bucket > self.window:
            self.pending = None
            return p
        return None

    def flush(self) -> Moment | None:
        p, self.pending = self.pending, None
        return p


class ClipLimiter:
    """No overlapping clips, at least `gap` seconds between the end of one clip and the start of
    the next, and at most `per_hour` automatic clips in any hour (0 = no cap)."""

    def __init__(self, gap: float = 30.0, per_hour: int = 0):
        self.gap, self.per_hour = gap, per_hour
        self.last_end = -math.inf
        self.recent: deque[float] = deque()

    def blocked(self, start: float, anchor: float) -> str | None:
        """Why an automatic clip [start, ...] around `anchor` can't be made, or None if it can."""
        if start < self.last_end:
            return "overlaps the previous clip"
        if self.gap and start < self.last_end + self.gap:
            return f"less than {self.gap:g}s after the previous clip"
        while self.recent and self.recent[0] <= anchor - 3600:
            self.recent.popleft()
        if self.per_hour and len(self.recent) >= self.per_hour:
            return f"hourly limit of {self.per_hour} clips reached"
        return None

    def record(self, end: float, anchor: float, manual: bool = False) -> None:
        self.last_end = max(self.last_end, end)
        if not manual:                     # "Clip now" doesn't use up the hourly allowance
            self.recent.append(anchor)
