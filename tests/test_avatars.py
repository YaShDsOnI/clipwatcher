from clipwatcher import avatars

PIC = "https://static-cdn.jtvnw.net/jtv_user_pictures/abc-profile_image-300x300.png"


def test_parse_finds_og_image_in_any_attribute_order():
    assert avatars.parse_avatar_url(f'<meta property="og:image" content="{PIC}"/>') == PIC
    assert avatars.parse_avatar_url(f'<head><meta content="{PIC}" name="twitter:image"></head>') == PIC


def test_parse_rejects_images_not_on_twitch():
    assert avatars.parse_avatar_url('<meta property="og:image" content="https://evil.example/x.png">') is None
    assert avatars.parse_avatar_url('<meta property="og:image" content="https://static-cdn.jtvnw.net/ttv-static/logo.png">') is None
    assert avatars.parse_avatar_url("<html>no tags</html>") is None


def test_avatar_fetches_once_then_uses_the_cache(home, monkeypatch):
    calls = []

    def fake_get(url, limit):
        calls.append(url)
        if url.startswith("https://www.twitch.tv/"):
            return f'<meta property="og:image" content="{PIC}">'.encode(), "text/html"
        return b"\x89PNG fake", "image/png"

    monkeypatch.setattr(avatars, "_get", fake_get)
    first = avatars.avatar("Lacy")
    assert first is not None and first.name == "lacy.png" and first.read_bytes() == b"\x89PNG fake"
    assert avatars.avatar("lacy") == first
    assert calls == ["https://www.twitch.tv/lacy", PIC]


def test_failures_are_remembered_and_bad_names_refused(home, monkeypatch):
    calls = []

    def offline(url, limit):
        calls.append(url)
        raise OSError("offline")

    monkeypatch.setattr(avatars, "_get", offline)
    assert avatars.avatar("someone") is None
    assert avatars.avatar("someone") is None          # not retried for an hour
    assert len(calls) == 1
    assert avatars.avatar("../etc/passwd") is None
    assert avatars.avatar("x" * 30) is None
