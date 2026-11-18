"""Word-timed captions: .srt files and the burned-in .ass subtitles (styles, speaker colours)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import FONT_DIR

OUT_W, OUT_H = 1080, 1920
YELLOW = "&H00E5FF&"                    # the word being spoken; ASS colours are &HBBGGRR&
# one text colour per speaker, the most talkative first: white, cyan, pink, violet, orange
SPEAKER_COLOURS = ["&HFFFFFF&", "&HFFD84D&", "&HD86FFF&", "&HFF88B3&", "&H439FFF&"]

# caption styles: name -> (label, font size, uppercase, max words per caption or None = setting)
TEMPLATES = {
    "karaoke": ("Bold karaoke (yellow word)", 64, True, None),
    "boxed": ("Boxed (dark band)", 58, True, None),
    "clean": ("Clean (sentence case, no highlight)", 54, False, 6),
    "pop": ("Pop (one big word at a time)", 96, True, 1),
}
FONT_FILE = "Montserrat-ExtraBold.ttf"


@dataclass
class Word:
    start: float
    end: float
    text: str
    speaker: int = 0      # 0 = whoever talks most in the clip


def srt_time(t: float) -> str:
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def ass_time(t: float) -> str:
    cs = int(round(max(t, 0) * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def ass_text(s: str) -> str:
    return re.sub(r"[{}\\]", "", s).replace("\n", " ").strip()


def write_srt(segments: list[tuple[float, float, str]], out: Path) -> None:
    blocks = [f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{t.strip()}\n"
              for i, (a, b, t) in enumerate(((a, b, t) for a, b, t in segments if t.strip()), 1)]
    out.write_text("\n".join(blocks), encoding="utf-8")


def phrases_of(words: list[Word], gap: float = 0.35) -> list[list[Word]]:
    """Words grouped into phrases: a new phrase starts after a pause longer than `gap` seconds."""
    out: list[list[Word]] = []
    for w in words:
        if out and w.start - out[-1][-1].end <= gap:
            out[-1].append(w)
        else:
            out.append([w])
    return out


def caption_groups(words: list[Word], max_words: int, max_chars: int) -> list[list[Word]]:
    """A few words at a time: break on word count, line length, a pause, or a new speaker."""
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words
                    or sum(len(x.text) + 1 for x in cur) + len(w.text) > max_chars
                    or w.start - cur[-1].end > 1.0
                    or w.speaker != cur[-1].speaker):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


# vertical positions as fractions of the frame height (designed on 1080x1920, scaled to any size)
def caption_y(layout: str, height: int = OUT_H) -> int:
    return round({"split": 0.4375, "crop": 0.7708}.get(layout, 0.7708) * height)


def title_y(layout: str, height: int = OUT_H) -> int:
    return round({"split": 0.0573, "crop": 0.1563}.get(layout, 0.1719) * height)


def ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    """#rrggbb -> ASS &HAABBGGRR (alpha 0 = opaque)."""
    h = hex_rgb.lstrip("#").upper()
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}&"


# editor text layers: style name -> ASS style line (font size and colours are set per layer)
TEXT_STYLES = ("band", "plain", "outline")


def ass_document(words: list[Word], settings: dict, title: str | None = None, texts: list[dict] = (),
                 size: tuple[int, int] = (OUT_W, OUT_H)) -> str:
    """One subtitle file with the word captions, an optional hook title and the editor's text layers."""
    width, height = size
    k = width / OUT_W                                    # font sizes are designed for 1080 wide
    template = settings.get("template", "karaoke")
    label, fsize, upper, fixed_words = TEMPLATES.get(template, TEMPLATES["karaoke"])
    fsize = round(fsize * k)
    max_words = fixed_words or int(settings.get("caption_words", 4))
    font = "Montserrat ExtraBold" if (FONT_DIR / FONT_FILE).exists() else "Arial Black"
    layout = settings.get("layout", "blur")
    margin = round(80 * k)
    if template == "boxed":   # BorderStyle 3 = opaque box drawn in OutlineColour
        cap_style = f"Style: Cap,{font},{fsize},&H00FFFFFF,&H00FFFFFF,&H64000000,&H00000000,0,0,0,0,100,100,0,0,3,{14 * k:.1f},0,5,{margin},{margin},0,1"
    elif template == "clean":
        cap_style = f"Style: Cap,{font},{fsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{2.5 * k:.1f},{1.5 * k:.1f},5,{margin},{margin},0,1"
    else:
        cap_style = f"Style: Cap,{font},{fsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{3 * k:.1f},{2 * k:.1f},5,{margin},{margin},0,1"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        cap_style,
        # hook title: white words on a violet band
        f"Style: Title,{font},{round(58 * k)},&H00FFFFFF,&H00FFFFFF,&H00FF5C7C,&H00000000,0,0,0,0,100,100,0,0,3,{18 * k:.1f},0,5,{round(90 * k)},{round(90 * k)},0,1",
        # editor text layers
        f"Style: TxtBand,{font},60,&H00FFFFFF,&H00FFFFFF,&H00FF5C7C,&H00000000,0,0,0,0,100,100,0,0,3,{16 * k:.1f},0,5,{margin},{margin},0,1",
        f"Style: TxtPlain,{font},60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,0,{3 * k:.1f},5,{margin},{margin},0,1",
        f"Style: TxtOutline,{font},60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{5 * k:.1f},{2 * k:.1f},5,{margin},{margin},0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(3.5)},Title,,0,0,0,,"
                     f"{{\\an5\\pos({width // 2},{title_y(layout, height)})\\fad(120,250)}}{ass_text(title).upper()}")
    y = caption_y(layout, height)
    groups = caption_groups(words, max_words, 20 if fsize >= 60 * k else 28) if settings.get("captions", True) else []
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0].start if gi + 1 < len(groups) else None
        # hold the caption through short pauses until the next one starts (no blinking mid-sentence)
        g_end = nxt if nxt is not None and nxt - g[-1].end < 1.5 else g[-1].end + 0.4
        colour = SPEAKER_COLOURS[g[0].speaker % len(SPEAKER_COLOURS)]
        words_text = [ass_text(x.text.upper() if upper else x.text) for x in g]
        for wi, w in enumerate(g):
            end = g[wi + 1].start if wi + 1 < len(g) else g_end
            if template == "clean":
                body = f"{{\\c{colour}}}" + " ".join(words_text)
            elif template == "pop":
                body = (f"{{\\c{colour}\\fscx70\\fscy70\\t(0,90,\\fscx108\\fscy108)\\t(90,150,\\fscx100\\fscy100)}}"
                        + words_text[wi])
            else:
                body = f"{{\\c{colour}}}" + " ".join(
                    f"{{\\c{YELLOW}}}{t}{{\\c{colour}}}" if j == wi else t for j, t in enumerate(words_text))
            if template == "clean" and wi:          # one event per caption, not per word
                break
            ev_start = w.start
            ev_end = g_end if template == "clean" else max(end, w.start + 0.05)
            lines.append(f"Dialogue: 0,{ass_time(ev_start)},{ass_time(ev_end)},Cap,,0,0,0,,"
                         f"{{\\an5\\pos({width // 2},{y})}}{body}")
    for t in texts:
        lines.append(text_event(t, width, height))
    return "\n".join(lines) + "\n"


def text_event(t: dict, width: int, height: int) -> str:
    """One editor text layer as an ASS event: position, size, colours and a fade or pop-in."""
    style = {"band": "TxtBand", "plain": "TxtPlain", "outline": "TxtOutline"}.get(t.get("style"), "TxtBand")
    k = width / OUT_W
    tags = [f"\\an5\\pos({round(t['x'] * width)},{round(t['y'] * height)})", f"\\fs{round(t['size'] * k)}",
            f"\\1c{ass_colour(t['color'])}"]
    if style in ("TxtBand", "TxtOutline"):                  # box colour / outline colour
        tags.append(f"\\3c{ass_colour(t['bg'])}")
    if t.get("anim") == "fade":
        tags.append("\\fad(180,180)")
    elif t.get("anim") == "pop":
        tags.append("\\fscx70\\fscy70\\t(0,120,\\fscx106\\fscy106)\\t(120,200,\\fscx100\\fscy100)")
    text = "\\N".join(ass_text(line) for line in str(t["text"]).splitlines() if line.strip()) or " "
    return f"Dialogue: 2,{ass_time(t['start'])},{ass_time(t['end'])},{style},,0,0,0,,{{{''.join(tags)}}}{text}"


def write_ass(words: list[Word], out: Path, settings: dict, title: str | None) -> None:
    out.write_text(ass_document(words, settings, title), encoding="utf-8")
