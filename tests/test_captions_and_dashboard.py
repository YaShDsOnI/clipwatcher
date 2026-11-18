import pytest

from clipwatcher import __main__ as cli
from clipwatcher.captions import Word, ass_time, caption_groups, srt_time
from clipwatcher.dashboard import DEFAULTS, Watcher, clean_settings


def test_times():
    assert srt_time(3661.5) == "01:01:01,500"
    assert ass_time(61.234) == "0:01:01.23"
    assert srt_time(-1) == "00:00:00,000"


def test_caption_groups_break_on_speaker_and_pause():
    words = [Word(0, 0.2, "a"), Word(0.3, 0.5, "b"), Word(0.6, 0.8, "c", speaker=1), Word(3.0, 3.2, "d", speaker=1)]
    assert [[w.text for w in g] for g in caption_groups(words, max_words=4, max_chars=40)] == [["a", "b"], ["c"], ["d"]]


def test_clean_settings_new_limits():
    s = clean_settings({"gap": "45", "max_per_hour": 12})
    assert s["gap"] == 45 and s["max_per_hour"] == 12
    s = clean_settings({"gap": -5, "max_per_hour": "lots"})
    assert s["gap"] == DEFAULTS["gap"] and s["max_per_hour"] == DEFAULTS["max_per_hour"]


def test_watcher_command_passes_limits():
    cmd = Watcher("lacy", clean_settings({"gap": 60, "max_per_hour": 10})).command()
    assert cmd[cmd.index("--gap") + 1] == "60"
    assert cmd[cmd.index("--max-per-hour") + 1] == "10"
    assert cmd[3:6] == ["clipwatcher", "watch", "lacy"]


def test_dashboard_marks_the_peak_bucket_when_a_clip_is_queued():
    w = Watcher("lacy", clean_settings({}))
    w._parse("[16:43:40] lacy  16:43:40    81 msgs  median  30.0    x2.7  SPIKE (waiting for the peak)")
    w._parse("[16:43:50] lacy  16:43:50    95 msgs  median  30.0    x3.2  SPIKE (peak so far)")
    w._parse("[16:44:00] lacy  16:44:00    20 msgs  median  30.0    x0.7")
    w._parse("[16:44:10] lacy  16:44:10    20 msgs  median  30.0    x0.7")
    w._parse("[16:44:10] lacy: clip 16:43:50 (95 msgs) -> lacy/lacy_20261118-164350_95msgs.mp4")
    assert [b["clip"] for b in w.buckets] == [False, True, False, False]
    assert [b["spike"] for b in w.buckets] == [True, True, False, False]
    assert w.spikes == 1


def test_cli_rejects_unknown_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["nonsense"])
    cli.main(["--version"])
    assert "clip-watcher" in capsys.readouterr().out
