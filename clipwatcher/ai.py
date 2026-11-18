"""Hook titles and scores: from chat (free, no AI) or from an AI provider in the cloud (no local RAM)."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter

from .captions import Word, phrases_of
from .config import RUN_DIR, read_env_file

# --------------------------------------------------------------------------- titles from chat

LAUGH = {"KEKW", "LUL", "LULW", "OMEGALUL", "LMAO", "LMFAO", "LOL", "ICANT", "KEKL", "PEPELAUGH",
         "XD", "HAHA", "HAHAHA", "DEAD", "💀", "😂", "🤣", "KEK", "AHAHAHA"}
HYPE = {"POG", "POGGERS", "POGCHAMP", "POGU", "LETSGO", "LETS GO", "LET'S GO", "HOLY", "GIGACHAD", "EZ",
        "CLEAN", "INSANE", "GOAT", "W", "WW", "WWW", "🔥", "CRAZY", "SHEESH"}
SHOCK = {"WHAT", "WTF", "NO WAY", "NOWAY", "OMG", "BRO", "BRUH", "MONKAS", "MONKAW", "D:", "WAIT",
         "HUH", "😳", "😱", "WHAT THE", "AYO"}
SAD = {"F", "SADGE", "RIP", "L", "LL", "PEPEHANDS", "😭", "NOOO", "NO"}
CATEGORY_TITLES = {
    "laugh": "CHAT COULDN'T STOP LAUGHING",
    "hype": "CHAT WENT CRAZY",
    "shock": "NO WAY THIS JUST HAPPENED",
    "sad": "PRESS F IN CHAT",
}


def norm_message(text: str) -> str:
    words = re.sub(r"[^\w' 😂🤣💀🔥😭😳😱]+", " ", text.upper()).split()
    out: list[str] = []
    for w in words:                     # "KEKW KEKW KEKW" -> "KEKW"
        if not out or out[-1] != w:
            out.append(w)
    return " ".join(out[:8])


def chat_title(chat: list[dict]) -> str | None:
    """A hook title from what chat spammed during the spike (free, no AI)."""
    if not chat:
        return None
    phrase_users: dict[str, set] = {}
    category: Counter[str] = Counter()
    for m in chat:
        n = norm_message(m.get("text", ""))
        if not n or "HTTP" in n:
            continue
        phrase_users.setdefault(n, set()).add(m.get("user", ""))
        for token in set(n.split()) | {n}:
            for name, words in (("laugh", LAUGH), ("hype", HYPE), ("shock", SHOCK), ("sad", SAD)):
                if token in words:
                    category[name] += 1
    if category:
        name, count = category.most_common(1)[0]
        if count >= max(3, len(chat) // 6):
            if name == "hype" and sum(1 for m in chat if norm_message(m.get("text", "")) in ("W", "WW", "WWW")) > count / 2:
                return "HUGE W"
            return CATEGORY_TITLES[name]
    ranked = sorted(phrase_users.items(), key=lambda kv: len(kv[1]), reverse=True)
    for phrase, users in ranked:
        if len(users) >= 3 and 3 <= len(phrase) <= 28 and not phrase.startswith("@"):
            return f"CHAT: “{phrase}”"
    return None


def hype_score(msgs: int, median: float) -> int:
    ratio = msgs / median if median else msgs / 2 or 1
    return int(max(0, min(100, round(25 * math.log2(max(ratio, 1)) + 15 * math.log10(msgs + 1) + 10))))


# --------------------------------------------------------------------------- AI providers

class AIUnavailable(Exception):
    pass


# Any of these can write hook titles, scores and post captions. Gemini, Groq and OpenRouter have
# free tiers; Gemini/OpenAI/Groq/OpenRouter/"custom" all speak the OpenAI chat-completions format,
# so they share one plain-HTTP code path (no extra packages). Claude uses Anthropic's SDK.
PROVIDERS = {
    "gemini": {"label": "Gemini (Google AI Studio)", "key": "GEMINI_API_KEY", "model": "gemini-3.5-flash-lite",
               "url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "signup": "https://aistudio.google.com/apikey", "free": True},
    "groq": {"label": "Groq", "key": "GROQ_API_KEY", "model": "openai/gpt-oss-20b",
             "url": "https://api.groq.com/openai/v1", "signup": "https://console.groq.com/keys", "free": True},
    "openrouter": {"label": "OpenRouter", "key": "OPENROUTER_API_KEY", "model": "openrouter/free",
                   "url": "https://openrouter.ai/api/v1", "signup": "https://openrouter.ai/keys", "free": True},
    "claude": {"label": "Claude (Anthropic)", "key": "ANTHROPIC_API_KEY", "model": "claude-opus-5-5",
               "url": None, "signup": "https://console.anthropic.com/settings/keys", "free": False},
    "openai": {"label": "OpenAI (GPT)", "key": "OPENAI_API_KEY", "model": "gpt-6-luna",
               "url": "https://api.openai.com/v1", "signup": "https://platform.openai.com/api-keys", "free": False},
    "custom": {"label": "Other OpenAI-compatible API", "key": "CUSTOM_API_KEY", "model": "",
               "url": "", "signup": None, "free": False},
}
AUTO_ORDER = ["claude", "gemini", "openai", "groq", "openrouter", "custom"]

RATING_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "integer"}, "engagement": {"type": "integer"},
        "value": {"type": "integer"}, "shareability": {"type": "integer"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "hook", "engagement", "value", "shareability", "caption", "hashtags"],
    "additionalProperties": False,
}


def ai_config() -> dict | None:
    """Which AI writes titles/scores: AI_PROVIDER in .env (or "auto" = the first provider with a
    key). None = no AI set up, titles come from chat."""
    env = {**os.environ, **read_env_file()}           # the dashboard's saved settings win
    choice = env.get("AI_PROVIDER", "auto").strip().lower()
    if choice in ("chat", "off", "none"):
        return None
    order = AUTO_ORDER if choice == "auto" else [choice]
    for name in order:
        preset = PROVIDERS.get(name)
        if not preset or not env.get(preset["key"]):
            continue
        model = (env.get("AI_MODEL") if choice != "auto" else "") or preset["model"]
        url = (env.get("AI_BASE_URL") if name == "custom" else "") or preset["url"]
        if not model or (preset["url"] is not None and not url):
            continue
        return {"provider": name, "model": model, "url": url, "key": env[preset["key"]]}
    return None


def rating_prompt(meta: dict, words: list[Word]) -> str:
    start, end = meta["start"], meta["end"]
    said = []
    for p in phrases_of([w for w in words if start <= w.start < end], gap=0.6):
        said.append(f"[speaker {p[0].speaker + 1}] " + " ".join(w.text for w in p))
    chat = [m["text"] for m in meta.get("chat", [])][:150]
    return (
        f"A Twitch chat burst happened during {meta['channel']}'s live stream "
        f"({meta.get('msgs', 0)} messages in 10 seconds, usually {meta.get('median', 0)}). "
        "We cut a 30-second vertical clip around it for Instagram Reels and YouTube Shorts.\n\n"
        "What was said in the clip:\n" + ("\n".join(said) or "(nobody spoke)") + "\n\n"
        "Chat messages during the moment:\n" + ("\n".join(chat) or "(none captured)") + "\n\n"
        "Write a hook title that will be burned into the first 3 seconds (max 7 words, punchy, "
        "no hashtags, no emojis, no quotes around it), score the clip 0-10 for hook, engagement, "
        "value and shareability, and write a short post caption (max 150 characters) with 3-6 hashtags.\n"
        'Reply with JSON only: {"title": str, "hook": int, "engagement": int, "value": int, '
        '"shareability": int, "caption": str, "hashtags": [str]}'
    )


def parse_json_reply(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\s*|\s*```$", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("the AI didn't reply with JSON")
    return json.loads(text[a:b + 1])


# --------------------------------------------------------------------------- Claude

# Request shapes, richest first. Instead of a hard-coded list of which model supports what (which
# goes stale with every new model), the richest shape is tried and a 400 steps down to a plainer
# one. The shape that worked is remembered per model in run/, so later clips don't repeat the 400s.
CLAUDE_LEVELS = (
    "structured JSON + low effort + server-side fallback if the model declines",
    "structured JSON + low effort",
    "structured JSON",
    "JSON asked for in the prompt only",
)
CLAUDE_LEVELS_FILE = RUN_DIR / "claude-request-levels.json"
CLAUDE_LEVEL_TTL = 7 * 24 * 3600         # re-try richer shapes once a week (models gain features)


def _claude_level_cache() -> dict:
    try:
        return json.loads(CLAUDE_LEVELS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _claude_start_level(model: str) -> int:
    entry = _claude_level_cache().get(model) or {}
    try:
        if time.time() - float(entry["t"]) < CLAUDE_LEVEL_TTL:
            return min(max(int(entry["level"]), 0), len(CLAUDE_LEVELS) - 1)
    except (KeyError, TypeError, ValueError):
        pass
    return 0


def _remember_claude_level(model: str, level: int) -> None:
    cache = _claude_level_cache()
    if (cache.get(model) or {}).get("level") == level:
        return
    cache[model] = {"level": level, "t": time.time()}
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        CLAUDE_LEVELS_FILE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except OSError:
        pass


def _claude_request(client, model: str, prompt: str, level: int):
    kwargs: dict = {"model": model, "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}]}
    if level <= 2:
        output_config: dict = {"format": {"type": "json_schema", "schema": RATING_SCHEMA}}
        if level <= 1:
            output_config["effort"] = "low"      # a title and four scores: no deep thinking needed
        kwargs["output_config"] = output_config
    if level == 0:
        # if this model declines, Anthropic re-runs the request on a suitable fallback model
        return client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    return client.messages.create(**kwargs)


def claude_rate(cfg: dict, prompt: str) -> dict:
    if not importlib.util.find_spec("anthropic"):
        raise AIUnavailable("the anthropic package isn't installed (pip install anthropic)")
    import anthropic

    model = cfg["model"]
    client = anthropic.Anthropic(api_key=cfg["key"])
    level = _claude_start_level(model)
    while True:
        try:
            response = _claude_request(client, model, prompt, level)
            break
        except (anthropic.BadRequestError, TypeError) as e:
            # 400: this model doesn't take part of the request. TypeError: the installed SDK is too
            # old for a parameter. Either way, try the next plainer request shape.
            if level + 1 >= len(CLAUDE_LEVELS):
                raise AIUnavailable(f"Anthropic rejected the request for '{model}' ({e})") from e
            level += 1
        except anthropic.AuthenticationError as e:
            raise AIUnavailable("Anthropic rejected the API key") from e
        except anthropic.PermissionDeniedError as e:
            raise AIUnavailable(f"this API key can't use '{model}'") from e
        except anthropic.NotFoundError as e:
            raise AIUnavailable(f"Anthropic doesn't know the model '{model}'") from e
        except anthropic.RateLimitError as e:
            raise AIUnavailable("Anthropic rate limit reached; try again later") from e
        except anthropic.APIStatusError as e:
            raise AIUnavailable(f"Anthropic API error ({e.status_code}); try again later") from e
        except anthropic.APIConnectionError as e:
            raise AIUnavailable("couldn't reach the Anthropic API") from e
    _remember_claude_level(model, level)
    if response.stop_reason == "refusal":
        raise AIUnavailable("Claude declined this clip")
    if response.stop_reason == "max_tokens":
        raise AIUnavailable("Claude's reply was cut off")
    return parse_json_reply(next((b.text for b in response.content if b.type == "text"), ""))


# --------------------------------------------------------------------------- OpenAI-compatible

def openai_compatible_rate(cfg: dict, prompt: str) -> dict:
    """Gemini, OpenAI, Groq, OpenRouter or any OpenAI-compatible API, over plain HTTPS."""
    url = cfg["url"].rstrip("/") + "/chat/completions"
    formats = [
        {"type": "json_schema", "json_schema": {"name": "clip_rating", "schema": RATING_SCHEMA, "strict": True}},
        {"type": "json_object"},
        None,                                       # some models support neither: ask in the prompt only
    ]
    last_error = ""
    for fmt in formats:
        body = {"model": cfg["model"], "messages": [{"role": "user", "content": prompt}]}
        if fmt:
            body["response_format"] = fmt
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {cfg['key']}",
            "User-Agent": "clip-watcher", "X-Title": "clip-watcher"})
        try:
            with urllib.request.urlopen(req, timeout=90) as res:
                data = json.loads(res.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"] or ""
            return parse_json_reply(content)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403) or (e.code == 400 and re.search(r"api[ _-]?key", detail, re.I)):
                raise AIUnavailable(f"{cfg['provider']} rejected the API key ({e.code})") from e
            if e.code == 404:
                raise AIUnavailable(f"{cfg['provider']}: model '{cfg['model']}' or URL not found") from e
            if e.code == 429:
                raise AIUnavailable(f"{cfg['provider']}: rate limit / free quota reached; try later") from e
            last_error = f"HTTP {e.code}: {detail}"
            if e.code != 400:
                break                                # a 400 may just be the response_format: try plainer
        except (ValueError, KeyError, IndexError) as e:
            last_error = f"unexpected reply ({e})"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise AIUnavailable(f"couldn't reach {cfg['provider']} ({e})") from e
    raise AIUnavailable(f"{cfg['provider']} failed: {last_error}")


def ai_rate(meta: dict, words: list[Word], cfg: dict | None = None) -> dict:
    """Hook title, SupoClip-style scores and a post caption from the configured AI (in the cloud:
    no local RAM). Raises AIUnavailable when no AI is set up or the call fails."""
    cfg = cfg or ai_config()
    if cfg is None:
        raise AIUnavailable("no AI key set up")
    prompt = rating_prompt(meta, words)
    data = claude_rate(cfg, prompt) if cfg["provider"] == "claude" else openai_compatible_rate(cfg, prompt)

    def clamp(v):
        try:
            return max(0, min(10, int(v)))
        except (TypeError, ValueError):
            return 0
    scores = {k: clamp(data.get(k)) for k in ("hook", "engagement", "value", "shareability")}
    tags = data.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    title = re.sub(r"\s+", " ", str(data.get("title", ""))).strip().strip('"“”')
    if not title:
        raise AIUnavailable("the AI returned no title")
    return {
        "title": title[:60],
        "scores": scores,
        "virality": round(sum(scores.values()) * 2.5),
        "post": {"caption": str(data.get("caption", "")).strip()[:300],
                 "hashtags": [t if t.startswith("#") else f"#{t}" for t in map(str, tags) if t.strip()][:6]},
        "by": cfg["provider"], "model": cfg["model"],
    }
