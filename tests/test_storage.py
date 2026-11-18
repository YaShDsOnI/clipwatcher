import json
import time
from datetime import datetime

from clipwatcher import config, storage


def make_clip(name, channel, days_old, status="done", flat=False):
    created = datetime.fromtimestamp(time.time() - days_old * 86400).isoformat(timespec="seconds")
    clip = config.CLIP_DIR / name if flat else config.clip_file(name, channel)
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"video")
    clip.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    config.SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    (config.SOURCE_DIR / name).write_bytes(b"source")
    config.META_DIR.mkdir(parents=True, exist_ok=True)
    config.meta_path_for(name).write_text(
        json.dumps({"name": name, "channel": channel, "created": created, "status": status}), encoding="utf-8")
    return clip


def test_clip_file_uses_one_folder_per_streamer():
    assert config.channel_of("stable_ronaldo_20261118-163704_77msgs_manual.mp4") == "stable_ronaldo"
    p = config.clip_file("lacy_20261118-164340_81msgs.mp4")
    assert p == config.CLIP_DIR / "lacy" / "lacy_20261118-164340_81msgs.mp4"
    assert config.clip_file("odd.mp4").parent.name == "other"
    assert config.clip_file("odd.mp4", "Some..Name").parent.name == "somename"


def test_plain_names_only():
    assert config.is_plain_name("lacy_20261118-164340_81msgs.mp4")
    assert not config.is_plain_name("../secret.mp4")
    assert not config.is_plain_name(".hidden.mp4")
    assert not config.is_plain_name("lacy/x.mp4")


def test_migrate_moves_flat_clips(home):
    name = "lacy_20261118-164340_81msgs.mp4"
    make_clip(name, "lacy", 0, flat=True)
    assert storage.migrate_flat_clips() == 2                    # the .mp4 and its .srt
    assert (config.CLIP_DIR / "lacy" / name).exists()
    assert (config.CLIP_DIR / "lacy" / name.replace(".mp4", ".srt")).exists()
    assert not (config.CLIP_DIR / name).exists()
    assert storage.migrate_flat_clips() == 0
    assert [p.name for p in storage.finished_clips()] == [name]


def test_cleanup_deletes_old_sources_but_keeps_the_clip(home):
    old, new = "a_20260101-000000_10msgs.mp4", "a_20261118-000000_10msgs.mp4"
    make_clip(old, "a", days_old=10)
    make_clip(new, "a", days_old=1)
    assert storage.cleanup() == {"sources": 1, "clips": 0}     # default: sources kept 7 days
    assert not (config.SOURCE_DIR / old).exists()
    assert config.clip_file(old).exists()
    assert (config.SOURCE_DIR / new).exists()


def test_cleanup_deletes_old_clips_when_asked(home):
    config.write_env({"KEEP_CLIPS_DAYS": "5", "KEEP_SOURCES_DAYS": "0"})
    old = "a_20260101-000000_10msgs.mp4"
    make_clip(old, "a", days_old=10)
    make_clip("a_20261118-000000_10msgs.mp4", "a", days_old=1)
    assert storage.cleanup() == {"sources": 0, "clips": 1}
    assert not any(f.exists() for f in storage.clip_files(old))
    assert len(storage.finished_clips()) == 1


def test_cleanup_never_touches_clips_being_made(home):
    config.write_env({"KEEP_CLIPS_DAYS": "1"})
    name = "a_20260101-000000_10msgs.mp4"
    make_clip(name, "a", days_old=30, status="rendering")
    assert storage.cleanup() == {"sources": 0, "clips": 0}
    assert (config.SOURCE_DIR / name).exists()


def test_usage_counts_per_streamer(home):
    make_clip("a_20261118-000000_10msgs.mp4", "a", 0)
    make_clip("b_20261118-000000_10msgs.mp4", "b", 0)
    make_clip("b_20261118-000100_10msgs.mp4", "b", 0)
    u = storage.usage()
    assert {k: v["clips"] for k, v in u["channels"].items()} == {"a": 1, "b": 2}
    assert u["keep_sources_days"] == 7 and u["keep_clips_days"] == 0
