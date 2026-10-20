"""Tests for spoken_text.speakable. Run: python3 -m pytest voice/test_spoken_text.py
(or python3 voice/test_spoken_text.py without pytest)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spoken_text import int_to_words, ordinal_words, speakable, year_words  # noqa: E402

CASES = [
    # Ye and names
    ("Yo, it's Ye.", "Yo, it's Yay."),
    ("Ye's back", "Yay's back"),
    ("Yesterday Kanye said yes", "Yesterday Kanye said yes"),   # only the word "Ye"
    ("ProxAI on the GPU vs CPU", "Prox A.I. on the G.P.U. versus C.P.U."),
    # emojis and markdown
    ("Let's go 🔥🔥💯👍🏽", "Let's go"),
    ("**Bold** move: `code` [link](https://x.com)", "Bold move: code link"),
    ("❤️ love it ✅", "love it"),
    # numbers
    ("I got 3 tracks", "I got three tracks"),
    ("6.6 GB", "six point six gigabytes"),
    ("The 5070 Ti has 16GB", "The fifty seventy Ti has sixteen gigabytes"),
    ("1,000,000 records", "one million records"),
    ("1000 fans, 3500 records", "one thousand fans, thirty-five hundred records"),
    ("In 1999 and 2008", "In nineteen ninety-nine and two thousand eight"),
    ("21st, 2nd, 3rd, 11th, 102nd", "twenty-first, second, third, eleventh, one hundred second"),
    ("call 8005551234", "call eight zero zero five five five one two three four"),
    ("range 5-10", "range five to ten"),
    ("temp -5°C", "temp minus five degrees Celsius"),
    ("1920x1080 @ 60fps", "nineteen twenty by ten eighty at sixty F.P.S."),
    ("2x faster", "two times faster"),
    ("#1 hit", "number one hit"),
    # money, percent, time, dates
    ("$1,250.50", "one thousand two hundred fifty dollars and fifty cents"),
    ("$1", "one dollar"),
    ("$3.5k", "three point five thousand dollars"),
    ("12% more", "twelve percent more"),
    ("10:30 pm and 9am", "ten thirty P.M. and nine A.M."),
    ("at 14:00", "at fourteen o'clock"),
    ("at 9:05", "at nine oh five"),
    ("2026-10-19", "October nineteenth, twenty twenty-six"),
    ("21st Oct", "twenty-first October"),
    # identifiers
    ("qwen3.5:9b", "kwen three point five nine B"),
    ("version v2.1.3", "version two point one point three"),
    ("a → b & c", "a to b and c"),
]


def test_cases():
    bad = [(i, speakable(i), want) for i, want in CASES if speakable(i) != want]
    assert not bad, "\n".join(f"{i!r}\n  got  {g!r}\n  want {w!r}" for i, g, w in bad)


def test_helpers():
    assert int_to_words(0) == "zero"
    assert int_to_words(115) == "one hundred fifteen"
    assert int_to_words(-42) == "minus forty-two"
    assert ordinal_words(40) == "fortieth"
    assert ordinal_words(12) == "twelfth"
    assert year_words(1905) == "nineteen oh five"
    assert year_words(2026) == "twenty twenty-six"


def test_idempotent():
    for raw, _ in CASES:
        once = speakable(raw)
        assert speakable(once) == once, raw


def test_empty():
    assert speakable("") == ""
    assert speakable("🔥") == ""


if __name__ == "__main__":
    for t in (test_cases, test_helpers, test_idempotent, test_empty):
        t()
    print(f"OK: {len(CASES)} cases")
