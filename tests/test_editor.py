import hashlib
import json
import shutil
import subprocess

import pytest

from clipwatcher import config, editor
from clipwatcher.captions import Word, ass_document

NAME = "tester_20261118-120000_50msgs.mp4"
SRC = {"duration": 60.0, "width": 1280, "height": 720, "audio": True, "video": True}
CLIP = {"duration": 30.0, "width": 1080, "height": 1920, "audio": True, "video": True}


def project(**over):
    p = {"media": "source", "aspect": "9:16",
         "segments": [{"in": 10, "out": 20, "speed": 1}, {"in": 25, "out": 35, "speed": 2}],
         "frame": {"layout": "blur"}, "captions": {"on": True, "template": "karaoke", "words": 4},
         "texts": [{"text": "HOOK", "start": 0, "end": 3, "x": 0.5, "y": 0.2, "size": 64, "color": "#ffffff",
                    "bg": "#7c5cff", "style": "band", "anim": "pop"}],
         "audio": {"volume": 1, "fade_in": 0, "fade_out": 0.5}, "music": []}
    p.update(over)
    return p


def test_clean_project_clamps_and_refuses():
    media = {"source": SRC, "clip": CLIP}
    p = editor.clean_project(project(segments=[{"in": -5, "out": 999, "speed": 3}]), media, {})
    assert p["segments"] == [{"in": 0.0, "out": 60.0, "speed": 2.0, "punch": None, "gap": 0.0}]
    p = editor.clean_project(project(segments=[{"in": 0, "out": 5, "gap": 2.5}, {"in": 9, "out": 10, "gap": -3}]), media, {})
    assert [s["gap"] for s in p["segments"]] == [2.5, 0.0]
    assert editor.total_duration(p["segments"]) == 8.5 and editor.segment_starts(p["segments"]) == [2.5, 7.5]
    with pytest.raises(ValueError, match="isn't available"):
        editor.clean_project(project(media="source"), {"source": None, "clip": CLIP}, {})
    with pytest.raises(ValueError, match="120 characters"):
        editor.clean_project(project(texts=[{"text": "x" * 121, "start": 0, "end": 1}]), media, {})
    with pytest.raises(ValueError, match="music library"):
        editor.clean_project(project(music=[{"id": "0123456789abcdef.mp3"}]), media, {})
    with pytest.raises(ValueError, match="no video left"):
        editor.clean_project(project(segments=[{"in": 5, "out": 5.05}]), media, {})
    p = editor.clean_project(project(frame={"layout": "split", "facecam": None}), media, {})
    assert p["frame"]["layout"] == "blur"                       # split needs a facecam box
    p = editor.clean_project(project(media="clip", texts=[{"text": "a", "start": 0, "end": 2, "color": "red;evil"}]), media, {})
    assert p["captions"]["on"] is False and p["texts"][0]["color"] == "#ffffff"


def test_retime_words_follows_cuts_and_speed():
    words = [Word(11, 11.5, "kept"), Word(22, 22.5, "cut"), Word(27, 28, "fast", speaker=1)]
    segs = [{"in": 10, "out": 20, "speed": 1}, {"in": 25, "out": 35, "speed": 2}]
    out = editor.retime_words(words, segs)
    assert [(w.text, w.start, w.end) for w in out] == [("kept", 1.0, 1.5), ("fast", 11.0, 11.5)]
    assert out[0].speaker == 0 or out[1].speaker == 0                # main voice re-ranked to white


def test_retime_words_skips_blank_space():
    words = [Word(11, 11.5, "a"), Word(26, 26.5, "b")]
    segs = [{"in": 10, "out": 20, "speed": 1, "gap": 1.5}, {"in": 25, "out": 30, "speed": 1, "gap": 2}]
    assert [(w.text, w.start) for w in editor.retime_words(words, segs)] == [("a", 2.5), ("b", 14.5)]


def test_build_ffmpeg_blank_space(tmp_path):
    media = {**SRC, "path": tmp_path / "src.mp4"}
    segs = [{"in": 10, "out": 12}, {"in": 20, "out": 22, "gap": 1.5}]
    p = editor.clean_project(project(segments=segs), {"source": media, "clip": None}, {})
    cmd = editor.build_ffmpeg(p, media, {}, None, tmp_path / "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "color=c=black:s=1280x720:r=30:d=1.500" in graph and "concat=n=3:v=1:a=1" in graph
    assert cmd[cmd.index("-t") + 1] == "5.500"


def test_build_ffmpeg_graph(tmp_path):
    media = {**SRC, "path": tmp_path / "src.mp4"}
    p = editor.clean_project(project(aspect="4:5", segments=[{"in": 10, "out": 20, "speed": 1, "punch": {"at": 1, "zoom": 1.4, "dur": 0.3}},
                                                           {"in": 25, "out": 35, "speed": 2}]),
                             {"source": media, "clip": None}, {})
    p["music"] = [{"id": "a" * 16 + ".mp3", "start": 1, "offset": 0, "dur": 5, "volume": 0.4, "fade_in": 0, "fade_out": 1}]
    cmd = editor.build_ffmpeg(p, media, {"a" * 16 + ".mp3": tmp_path / "m.mp3"}, "edit.ass", tmp_path / "out.mp4")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1" in graph and "atempo=2.0" in graph
    assert "zoompan=" in graph and "s=1080x1350" in graph and "scale=1080:-2" in graph
    assert "amix=inputs=2" in graph and "adelay=delays=1000" in graph
    assert "ass=edit.ass:fontsdir=fonts" in graph
    assert cmd[cmd.index("-t") + 1] == "15.000"                     # 10 s + 10 s at 2x


def test_ass_document_has_text_layers():
    doc = ass_document([], {"captions": False}, None, [{"text": "Hi\nthere", "start": 0, "end": 2, "x": 0.5, "y": 0.25,
                                                      "size": 64, "color": "#ff0000", "bg": "#000000",
                                                      "style": "band", "anim": "fade"}], (1080, 1350))
    assert "PlayResY: 1350" in doc and "TxtBand" in doc
    assert "\\pos(540,338)" in doc and "\\1c&H000000FF&" in doc and "Hi\\Nthere" in doc


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_export_makes_a_new_file_and_never_touches_the_original(home):
    def make(path, size, seconds):
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=330", "-t", str(seconds), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)

    clip = config.clip_file(NAME)
    make(clip, "540x960", 6)
    make(config.SOURCE_DIR / NAME, "640x360", 10)
    music = home / "beep.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880", "-t", "3", str(music)], check=True)
    meta = {"name": NAME, "channel": "tester", "source": f"clips/.sources/{NAME}", "start": 2, "end": 8,
            "words": [{"start": 3, "end": 3.5, "text": "hello", "speaker": 0}], "settings": {}}
    config.META_DIR.mkdir(parents=True, exist_ok=True)
    config.meta_path_for(NAME).write_text(json.dumps(meta), encoding="utf-8")
    before = hashlib.sha256(clip.read_bytes()).hexdigest()

    sound = editor.add_music(music.read_bytes(), "beep.wav")
    media = editor.media_for(NAME, meta)
    raw = project(aspect="4:5", segments=[{"in": 2, "out": 4, "speed": 1, "punch": {"at": 0.5, "zoom": 1.3, "dur": 0.2}},
                                          {"in": 6, "out": 8, "speed": 2, "gap": 0.5}],
                  music=[{"id": sound["id"], "start": 0.5, "offset": 0, "dur": 2, "volume": 0.5}])
    p = editor.clean_project(raw, media, editor.music_library())
    progress = []
    out = editor.export(NAME, meta, p, progress.append)

    assert out.parent == clip.parent / "edits" and out.name.startswith(clip.stem + "_edit-")
    info = editor.probe(out)
    assert (info["width"], info["height"]) == (1080, 1350)
    assert abs(info["duration"] - 3.5) < 0.3                          # 2 s + 0.5 s blank + 2 s at 2x
    assert progress[-1] == 1.0
    assert hashlib.sha256(clip.read_bytes()).hexdigest() == before     # the original is untouched

    # the 30-s clip itself can be edited too (no source needed)
    p2 = editor.clean_project(project(media="clip", segments=[{"in": 1, "out": 4}], frame={"fit": "blur", "zoom": 1.2}),
                              media, {})
    out2 = editor.export(NAME, meta, p2)
    assert abs(editor.probe(out2)["duration"] - 3.0) < 0.3
    assert [e["file"] for e in editor.exports_for(NAME, "tester")] == sorted([out.name, out2.name], reverse=True)
    assert hashlib.sha256(clip.read_bytes()).hexdigest() == before
