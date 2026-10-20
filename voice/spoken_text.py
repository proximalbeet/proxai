"""Turn a chat reply into text the Kanye voice (Chatterbox) can say cleanly.

Chatterbox reads raw text literally: "Ye" comes out as "yee", digits and symbols are
guessed at, and emojis turn into noise. Both voice paths call this before speaking:
  * Discord: Hermes -> tts_pipeline.py (split_into_chunks)
  * Phone:   ProxMobile's kanye_voice_server.py, which imports tts_pipeline

Standard library only (the Chatterbox venv has no extra packages). Safe to run twice on
the same text. Add your own words to pronunciations.json next to this file:
  {"Ye": "Yay", "Obsidian": "Ob-sid-ian"}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_DIR = Path(__file__).resolve().parent

# Words the voice gets wrong, as written -> as said. Case-sensitive, whole words only.
# pronunciations.json (optional) adds to or overrides these.
PRONUNCIATIONS: dict[str, str] = {
    "Ye": "Yay",
    "YE": "Yay",
    "Ye's": "Yay's",
    "Yeezy": "Yee-zee",
    "ProxAI": "Prox A.I.",
    "ProxMobile": "Prox Mobile",
    "ProxVault": "Prox Vault",
    "AI": "A.I.",
    "API": "A.P.I.",
    "APIs": "A.P.I.s",
    "GPU": "G.P.U.",
    "CPU": "C.P.U.",
    "VRAM": "V-RAM",
    "RGB": "R.G.B.",
    "URL": "U.R.L.",
    "VR": "V.R.",
    "LLM": "L.L.M.",
    "TTS": "T.T.S.",
    "MCP": "M.C.P.",
    "SSH": "S.S.H.",
    "Ollama": "Oh-llama",
    "qwen": "kwen",
    "Qwen": "Kwen",
    "vs": "versus",
    "vs.": "versus",
    "e.g.": "for example",
    "i.e.": "that is",
    "etc.": "et cetera",
    "w/": "with",
    "w/o": "without",
    "Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "Jun": "June",
    "Jul": "July", "Aug": "August", "Sep": "September", "Sept": "September",
    "Oct": "October", "Nov": "November", "Dec": "December",
    "Mon": "Monday", "Tue": "Tuesday", "Tues": "Tuesday", "Thu": "Thursday",
    "Thurs": "Thursday", "Fri": "Friday",
}

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand")]
_ORDINAL_EXCEPTIONS = {"one": "first", "two": "second", "three": "third", "five": "fifth",
                       "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]

# After a number: unit as written -> spoken (singular, plural). Case-sensitive where it matters.
_UNITS = {
    "k": ("thousand", "thousand"), "K": ("thousand", "thousand"),
    "M": ("million", "million"), "B": ("B", "B"), "b": ("B", "B"),
    "KB": ("kilobyte", "kilobytes"), "MB": ("megabyte", "megabytes"),
    "GB": ("gigabyte", "gigabytes"), "TB": ("terabyte", "terabytes"),
    "GiB": ("gigabyte", "gigabytes"), "MiB": ("megabyte", "megabytes"),
    "ms": ("millisecond", "milliseconds"), "sec": ("second", "seconds"),
    "s": ("second", "seconds"), "min": ("minute", "minutes"), "mins": ("minute", "minutes"),
    "h": ("hour", "hours"), "hr": ("hour", "hours"), "hrs": ("hour", "hours"),
    "W": ("watt", "watts"), "kW": ("kilowatt", "kilowatts"), "V": ("volt", "volts"),
    "Hz": ("hertz", "hertz"), "GHz": ("gigahertz", "gigahertz"), "MHz": ("megahertz", "megahertz"),
    "fps": ("F.P.S.", "F.P.S."), "FPS": ("F.P.S.", "F.P.S."),
    "x": ("times", "times"), "X": ("times", "times"),
    "km": ("kilometer", "kilometers"), "mi": ("mile", "miles"), "lbs": ("pound", "pounds"),
    "lb": ("pound", "pounds"), "kg": ("kilogram", "kilograms"), "ft": ("foot", "feet"),
}

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F\U0000200D\U000020E3\U0001F3FB-\U0001F3FF\U000E0020-\U000E007F"
    "\U00002190-\U000021FF\U00002300-\U000023FF\U000025A0-\U000025FF]+")
_ARROWS = {"→": " to ", "⇒": " to ", "->": " to ", "=>": " to ", "←": " from ", "↔": " and "}


def int_to_words(n: int) -> str:
    if n < 0:
        return "minus " + int_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {int_to_words(rest)}" if rest else "")
    for value, name in _SCALES:
        if n >= value:
            big, rest = divmod(n, value)
            return f"{int_to_words(big)} {name}" + (f" {int_to_words(rest)}" if rest else "")
    return str(n)


def ordinal_words(n: int) -> str:
    head, last = re.match(r"^(.*?)([a-z]+)$", int_to_words(n)).groups()
    if last in _ORDINAL_EXCEPTIONS:
        last = _ORDINAL_EXCEPTIONS[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head + last


def year_words(n: int) -> str:
    """1999 -> nineteen ninety-nine, 5070 -> fifty seventy, 2008 -> two thousand eight."""
    if 2000 <= n <= 2009 or n % 1000 == 0:
        return int_to_words(n)
    hi, lo = divmod(n, 100)
    if lo == 0:
        return f"{int_to_words(hi)} hundred"
    return f"{int_to_words(hi)} {'oh ' + _ONES[lo] if lo < 10 else int_to_words(lo)}"


def digits_words(s: str) -> str:
    return " ".join(_ONES[int(c)] for c in s if c.isdigit())


def _number(s: str) -> str:
    """'1,234.5' -> words. Long digit runs (IDs, phone numbers) are read digit by digit."""
    s_raw, s = s, s.replace(",", "")
    whole, _, frac = s.partition(".")
    if (len(whole) >= 7 and "," not in s_raw) or (len(whole) > 1 and whole.startswith("0")):
        out = digits_words(whole)
    else:
        out = int_to_words(int(whole or "0"))
    if frac:
        out += " point " + digits_words(frac)
    return out


def _load_pronunciations() -> dict[str, str]:
    words = dict(PRONUNCIATIONS)
    try:
        extra = json.loads((_DIR / "pronunciations.json").read_text(encoding="utf-8"))
        if isinstance(extra, dict):
            words.update({str(k): str(v) for k, v in extra.items()})
    except (OSError, ValueError):
        pass
    return words


def _apply_pronunciations(text: str) -> str:
    words = _load_pronunciations()
    if not words:
        return text
    # Longest first so "Ye's" wins over "Ye"; a word must not touch letters/digits on either side.
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.sub(rf"(?<![\w']){'(?:' + alts + ')'}(?![A-Za-z'])",
                  lambda m: words[m.group(0)], text)


def _strip_markdown(text: str) -> str:
    text = re.sub(r"```[\s\S]*?(```|$)", " ", text)            # code blocks: not speech
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)       # links -> their text
    text = re.sub(r"<(https?://[^>]+)>", " ", text)
    text = re.sub(r"https?://\S+", " a link ", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.M)        # headings ("#1" isn't one)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)            # quotes
    text = re.sub(r"^\s*[-*+•]\s+", "", text, flags=re.M)        # bullets
    text = re.sub(r"(\*\*|__|~~|\*|(?<!\w)_(?!\w))", "", text)  # emphasis marks
    text = text.replace("|", ", ")
    return text


def _numbers(text: str) -> str:
    # ISO dates: 2026-10-19 -> October nineteenth, twenty twenty-six
    def iso(m):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{_MONTHS[mo - 1]} {ordinal_words(d)}, {year_words(y)}"
        return m.group(0)
    text = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", iso, text)

    # Times: 10:30, 9:05 pm, 14:00
    def clock(m):
        h, mi, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "")
        if h > 23 or mi > 59:
            return m.group(0)
        spoken = int_to_words(h) + (" o'clock" if mi == 0 and not ampm else
                                    "" if mi == 0 else
                                    f" oh {_ONES[mi]}" if mi < 10 else f" {int_to_words(mi)}")
        if ampm:
            spoken += " " + ("A.M." if ampm.lower().startswith("a") else "P.M.")
        return spoken
    text = re.sub(r"\b(\d{1,2}):(\d{2})(?:\s*([ap]\.?m\.?))?(?![\w:])", clock, text, flags=re.I)
    text = re.sub(r"\b(\d{1,2})\s*([ap])\.?m\.?(?!\w)",
                  lambda m: f"{int_to_words(int(m.group(1)))} {m.group(2).upper()}.M.", text, flags=re.I)

    # Money and percentages
    for sym, unit in (("$", "dollar"), ("£", "pound"), ("€", "euro")):
        def money(m, unit=unit):
            amount, scale = m.group(1), m.group(2) or ""
            whole, _, cents = amount.replace(",", "").partition(".")
            words = _number(whole)
            scale_word = {"k": " thousand", "K": " thousand", "m": " million", "M": " million",
                          "b": " billion", "B": " billion"}.get(scale, "")
            if scale_word:
                return f"{_number(amount)}{scale_word} {unit}s"
            out = f"{words} {unit}{'' if whole == '1' else 's'}"
            if cents and int(cents.ljust(2, '0')[:2]):
                c = int(cents.ljust(2, "0")[:2])
                out += f" and {int_to_words(c)} cent{'' if c == 1 else 's'}"
            return out
        text = re.sub(re.escape(sym) + r"\s?(\d[\d,]*(?:\.\d+)?)([kKmMbB](?!\w))?", money, text)
    text = re.sub(r"(\d)\s?%", r"\1 percent", text)

    # Ordinals: 1st, 22nd, 3rd, 4th
    text = re.sub(r"\b(\d+)(st|nd|rd|th)\b", lambda m: ordinal_words(int(m.group(1))), text)

    # Dimensions / multipliers: 1920x1080 -> 1920 by 1080
    text = re.sub(r"\b(\d+)\s?[x×]\s?(\d+)\b", r"\1 by \2", text)
    # Ranges: 5-10 -> 5 to 10 (not dates or negative numbers)
    text = re.sub(r"(?<![\w.-])(\d+(?:\.\d+)?)\s?[-–—]\s?(\d+(?:\.\d+)?)(?![\w.-])", r"\1 to \2", text)
    # Version numbers: v2.1.3 / 3.5.1 -> two point one point three
    text = re.sub(r"\bv(?=\d)", "version ", text)
    text = re.sub(r"\b\d+(?:\.\d+){2,}\b",
                  lambda m: " point ".join(_number(p) for p in m.group(0).split(".")), text)
    # Negative numbers at a word start: -5 -> minus five
    text = re.sub(r"(?<![\w.])-(?=\d)", "minus ", text)

    # Number + unit: 16GB, 3.4 GB, 9b, 5k, 300W, 2x
    unit_alts = "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))
    def with_unit(m):
        num, unit = m.group(1), m.group(2)
        one, many = _UNITS[unit]
        return f"{_number(num)} {one if num in ('1', '1.0') else many}"
    text = re.sub(rf"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?({unit_alts})(?![\w])", with_unit, text)

    # Years (1900-2099 written without a comma, not followed by more digits)
    text = re.sub(r"(?<![\w.,$])([1-9]\d{3})(?![\w.,]\d|\d)",
                  lambda m: year_words(int(m.group(1))), text)
    # Digits glued to letters (qwen3.5 -> qwen three point five)
    text = re.sub(r"(?<=[A-Za-z])(\d+(?:\.\d+)?)", lambda m: " " + _number(m.group(1)), text)
    # Everything else: 1,234.56 / 42 / 3.5
    text = re.sub(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\d])",
                  lambda m: _number(m.group(0).rstrip(",")) + ("," if m.group(0).endswith(",") else ""),
                  text)
    return text


def _symbols(text: str) -> str:
    for a, b in _ARROWS.items():
        text = text.replace(a, b)
    text = text.replace("&", " and ").replace("@", " at ")
    text = re.sub(r"(?<=\d)\s?\+(?=\s|$)", " plus", text)       # 10+
    text = re.sub(r"\s\+\s", " plus ", text)
    text = re.sub(r"\s=\s", " equals ", text)
    text = re.sub(r"#(?=\d)", "number ", text)
    text = re.sub(r"(?<=\d)\s?°\s?([CF])\b",
                  lambda m: " degrees " + ("Celsius" if m.group(1) == "C" else "Fahrenheit"), text)
    text = text.replace("°", " degrees ").replace("~", " about ").replace("≈", " about ")
    text = text.replace("…", "...").replace("—", ", ").replace("–", ", ")
    text = re.sub(r"[()\[\]{}]", ", ", text)
    text = re.sub(r"[*_^<>\\|#]+", " ", text)
    return text


def speakable(text: str) -> str:
    """Full cleanup: markdown, emojis, pronunciations, numbers, symbols, spacing."""
    if not text:
        return ""
    text = _strip_markdown(str(text))
    for a, b in _ARROWS.items():              # before emojis (arrow glyphs) and numbers ("5->6")
        text = text.replace(a, b)
    text = _EMOJI_RE.sub(" ", text)
    text = _apply_pronunciations(text)
    text = re.sub(r"#(?=\d)", "number ", text)
    text = re.sub(r"\s?°\s?([CF])\b",
                  lambda m: " degrees " + ("Celsius" if m.group(1) == "C" else "Fahrenheit"), text)
    text = re.sub(r"(?<=[A-Za-z0-9]):(?=[A-Za-z0-9])(?!\d{2}\b)", " ", text)  # qwen3.5:9b, not 10:30
    text = _numbers(text)
    text = re.sub(r"\b(version)( version)+\b", r"\1", text)
    text = _symbols(text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)
    text = re.sub(r"^[\s,;:]+", "", text)
    return " ".join(text.split())


if __name__ == "__main__":  # quick check: python3 spoken_text.py "Ye made 3.5k on 10/19 🔥"
    import sys
    print(speakable(" ".join(sys.argv[1:]) or sys.stdin.read()))
