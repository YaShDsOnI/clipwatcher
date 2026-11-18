from clipwatcher.chat import BUCKET, ChatCounter, ClipLimiter, Moment, SpikeDetector, SpikeMerger


def test_counter_caps_each_chatter_per_bucket():
    c = ChatCounter(per_user=2)
    for _ in range(10):
        c.add(5, "spammer")
    c.add(5, "someone")
    assert c.pop(5) == 3                        # 2 from the spammer + 1


def test_counter_ignores_bots_and_case():
    c = ChatCounter(per_user=2)
    assert not c.add(1, "Nightbot")
    assert c.add(1, "Viewer")
    assert c.pop(1) == 1


def test_counter_without_limit_and_forgets_old_buckets():
    c = ChatCounter(per_user=0)
    for _ in range(5):
        c.add(1, "a")
    c.add(2, "a")
    assert c.pop(2) == 1
    assert c.pop(1) == 0                        # older buckets were dropped with bucket 2


def test_detector_needs_warm_up_then_flags_spikes():
    d = SpikeDetector(ratio=2.5, min_count=10, window_buckets=3)
    assert d.push(100) == (False, 0.0)          # not warm yet
    d.push(4)
    d.push(4)
    spike, med = d.push(40)
    assert spike and med == 4


def test_detector_respects_minimum():
    d = SpikeDetector(ratio=2.0, min_count=10, window_buckets=3)
    for n in (1, 1, 1):
        d.push(n)
    assert d.push(5) == (False, 1)              # 5x the usual, but under --min


def test_merger_makes_one_moment_at_the_peak():
    m = SpikeMerger(window=2 * BUCKET)
    assert m.spike(Moment(0, 20, 5))[0] is None
    assert m.spike(Moment(10, 50, 5))[0] is None        # bigger: the peak moves here
    assert m.spike(Moment(20, 30, 5))[0] is None        # same moment
    assert m.tick(30) is None                           # 20 s after the peak: still open
    final = m.tick(40)
    assert final == Moment(10, 50, 5)
    assert m.tick(50) is None


def test_merger_separates_distant_moments():
    m = SpikeMerger(window=20)
    m.spike(Moment(0, 20, 5))
    first, _ = m.spike(Moment(60, 25, 5))
    assert first == Moment(0, 20, 5)
    assert m.flush() == Moment(60, 25, 5)
    assert m.flush() is None


def test_limiter_gap_and_overlap():
    lim = ClipLimiter(gap=30, per_hour=0)
    assert lim.blocked(start=0, anchor=20) is None
    lim.record(end=30, anchor=20)
    assert "overlaps" in lim.blocked(start=25, anchor=45)
    assert "after the previous clip" in lim.blocked(start=50, anchor=70)
    assert lim.blocked(start=60, anchor=80) is None


def test_limiter_hourly_cap_ignores_manual_clips():
    lim = ClipLimiter(gap=0, per_hour=2)
    lim.record(end=30, anchor=20)
    lim.record(end=130, anchor=120, manual=True)       # "Clip now" doesn't count
    lim.record(end=230, anchor=220)
    assert "hourly limit" in lim.blocked(start=300, anchor=320)
    assert lim.blocked(start=3700, anchor=3625) is None  # the first one is over an hour old
