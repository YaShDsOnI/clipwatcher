import importlib.machinery
import json
import sys
import types

import pytest

from clipwatcher import ai


def test_chat_title_categories_and_quotes():
    laughs = [{"user": f"u{i}", "text": "KEKW KEKW"} for i in range(8)]
    assert ai.chat_title(laughs) == "CHAT COULDN'T STOP LAUGHING"
    ws = [{"user": f"u{i}", "text": "W"} for i in range(8)]
    assert ai.chat_title(ws) == "HUGE W"
    quote = [{"user": f"u{i}", "text": "he did it again"} for i in range(4)]
    assert ai.chat_title(quote) == "CHAT: “HE DID IT AGAIN”"
    assert ai.chat_title([]) is None


def test_hype_score_range():
    assert 0 <= ai.hype_score(0, 0) <= 100
    assert ai.hype_score(200, 10) > ai.hype_score(20, 10)
    assert ai.hype_score(10_000, 1) == 100


def test_parse_json_reply_handles_fences():
    assert ai.parse_json_reply('```json\n{"title": "x"}\n```') == {"title": "x"}
    assert ai.parse_json_reply('Sure! {"a": 1} hope that helps') == {"a": 1}
    with pytest.raises(ValueError):
        ai.parse_json_reply("no json here")


# --------------------------------------------------------------------------- Claude request shapes

REPLY = {"title": "He Hit The Shot", "hook": 8, "engagement": 7, "value": 6, "shareability": 9,
         "caption": "insane", "hashtags": ["clip", "#twitch"]}


def fake_anthropic(monkeypatch, reject_levels):
    """A stand-in for the anthropic SDK that answers 400 to the request shapes in reject_levels."""
    mod = types.ModuleType("anthropic")
    mod.__spec__ = importlib.machinery.ModuleSpec("anthropic", None)
    for name in ("APIStatusError", "APIConnectionError"):
        setattr(mod, name, type(name, (Exception,), {}))
    for name in ("BadRequestError", "AuthenticationError", "PermissionDeniedError", "NotFoundError", "RateLimitError"):
        setattr(mod, name, type(name, (mod.APIStatusError,), {}))
    calls = []

    def level_of(kwargs):
        oc = kwargs.get("output_config")
        if "fallbacks" in kwargs:
            return 0
        return 1 if oc and "effort" in oc else 2 if oc else 3

    def create(**kwargs):
        level = level_of(kwargs)
        calls.append(level)
        if level in reject_levels:
            raise mod.BadRequestError(f"level {level} not supported")
        text = types.SimpleNamespace(type="text", text=json.dumps(REPLY))
        return types.SimpleNamespace(stop_reason="end_turn", content=[text])

    class Anthropic:
        def __init__(self, api_key):
            self.messages = types.SimpleNamespace(create=create)
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))

    mod.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return calls


CFG = {"provider": "claude", "model": "claude-test-model", "key": "k", "url": None}


def test_claude_steps_down_and_remembers(home, monkeypatch):
    calls = fake_anthropic(monkeypatch, reject_levels={0, 1})
    assert ai.claude_rate(CFG, "prompt")["title"] == REPLY["title"]
    assert calls == [0, 1, 2]
    calls.clear()
    ai.claude_rate(CFG, "prompt")                     # the next clip starts at the shape that worked
    assert calls == [2]


def test_claude_uses_richest_shape_when_supported(home, monkeypatch):
    calls = fake_anthropic(monkeypatch, reject_levels=set())
    ai.claude_rate(CFG, "prompt")
    assert calls == [0]


def test_claude_gives_up_after_plainest_shape(home, monkeypatch):
    fake_anthropic(monkeypatch, reject_levels={0, 1, 2, 3})
    with pytest.raises(ai.AIUnavailable, match="rejected the request"):
        ai.claude_rate(CFG, "prompt")


def test_ai_rate_normalises_reply(home, monkeypatch):
    fake_anthropic(monkeypatch, reject_levels=set())
    meta = {"channel": "x", "msgs": 40, "median": 5, "start": 0, "end": 30, "chat": []}
    r = ai.ai_rate(meta, [], CFG)
    assert r["virality"] == round((8 + 7 + 6 + 9) * 2.5)
    assert r["post"]["hashtags"] == ["#clip", "#twitch"]
