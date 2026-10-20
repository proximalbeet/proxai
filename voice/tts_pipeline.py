#!/usr/bin/env python3
"""Hermes command TTS for ProxAI: Chatterbox voice cloning.

How it works:
  Chatterbox listens to a short reference clip of the target voice
  (reference/<voice>.wav, 10-20 s of clean speech) and speaks new text in that
  voice, copying both the sound and some of the delivery. No training needed.

Hermes calls this script from config.yaml (tts.providers.<name>.command) as:
  .venv-chatterbox/bin/python tts_pipeline.py {input_path} {output_path} <voice>

Usage: tts_pipeline.py <input_text_file> <output_wav> [voice]
voice = reference clip name under reference/ (default: kanye -> reference/kanye.wav)

RVC voices without a reference clip (like Gru) use tts_rvc.py instead.
"""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# Everything lives next to this script, so it works no matter which folder
# Hermes runs it from.
VOICE_DIR = Path(__file__).resolve().parent

# How dramatic the delivery is. 0.5 = neutral, higher = more intense and
# emotional (good for chaotic Ye), too high starts to slur. Override with the
# PROXAI_TTS_EXAGGERATION environment variable.
EXAGGERATION = float(os.getenv("PROXAI_TTS_EXAGGERATION", "0.7"))

# How tightly the model follows the reference's pacing. Lower = slower, looser,
# more expressive delivery. Override with PROXAI_TTS_CFG.
CFG_WEIGHT = float(os.getenv("PROXAI_TTS_CFG", "0.3"))

# Chatterbox gets unstable on long inputs, so long replies are spoken in chunks
# of roughly this many characters and stitched back together.
MAX_CHUNK_CHARS = 300

# Silence inserted between chunks so stitched sentences don't run together.
GAP_SECONDS = 0.25

# Ollama keeps qwen3.5:9b (~7.5 GB) on the GPU for 5 minutes after each reply,
# which leaves too little VRAM for Chatterbox (~3 GB) and makes it crash with
# CUDA out of memory. Override the address with OLLAMA_HOST.
OLLAMA_URL = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = f"http://{OLLAMA_URL}"


def ollama_request(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{OLLAMA_URL}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def free_ollama_vram(wait_seconds: float = 10) -> None:
    """Unload every model Ollama has on the GPU (keep_alive 0), then wait for it to let go.

    Best effort: if Ollama is down or slow, TTS still runs. The next qwen reply
    reloads the model, which costs a few seconds.
    """
    try:
        loaded = [m["name"] for m in ollama_request("/api/ps").get("models", [])]
        for name in loaded:
            ollama_request("/api/generate", {"model": name, "keep_alive": 0})
        deadline = time.monotonic() + wait_seconds
        while loaded and time.monotonic() < deadline:
            time.sleep(0.5)
            loaded = ollama_request("/api/ps").get("models", [])
        if loaded:
            print(f"warning: Ollama still has {len(loaded)} model(s) loaded", file=sys.stderr)
    except OSError as exc:
        print(f"warning: could not unload Ollama models: {exc}", file=sys.stderr)


def split_into_chunks(text: str) -> list[str]:
    """Split text at sentence ends, then pack sentences into chunks under MAX_CHUNK_CHARS."""
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


# Phase 10 server/personal mode (mode/proxai_mode.py writes this file on every switch).
MODE_FILE = Path(os.getenv("PROXAI_STATE_DIR", "~/.local/state/proxai")).expanduser() / "mode.json"
# The warm Kanye voice server (ProxMobile, systemd socket on this port) and its API key.
VOICE_SERVER_URL = os.getenv("PROXAI_VOICE_URL", "http://127.0.0.1:8653")
ENV_FILE = Path(os.getenv("PROXAI_ENV_FILE", "~/.hermes/profiles/proxai/.env")).expanduser()
VOICE_SERVER_MAX_CHARS = 1100   # the server takes up to 1200 per request


def mode_voice_settings() -> dict:
    """{'voice': bool, 'voice_warm': bool} for the current mode, {} when no mode is set."""
    try:
        return json.loads(MODE_FILE.read_text()).get("hermes") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def _api_key() -> str:
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_SERVER_KEY="):
                v = line.split("=", 1)[1].strip()
                if v[:1] in "\"'" and v[-1:] == v[:1]:
                    return v[1:-1]
                return re.split(r"\s+#", v, maxsplit=1)[0].strip()
    except OSError:
        pass
    return ""


def speak_via_voice_server(text: str, voice_name: str, out_path: str) -> bool:
    """Server mode: the voice model is already loaded in the voice server, so ask it (about 1 s a
    sentence) instead of loading Chatterbox here (~9 s) and unloading Ollama. False on any failure,
    and the caller falls back to speaking locally."""
    key = _api_key()
    if not key:
        return False
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        if current and len(current) + len(sentence) + 1 > VOICE_SERVER_MAX_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    if not pieces or any(len(p) > 1200 for p in pieces):
        return False
    try:
        import io
        import numpy as np
        import soundfile as sf
        audio, rate = [], None
        for piece in pieces:
            req = urllib.request.Request(
                f"{VOICE_SERVER_URL}/v1/tts", data=json.dumps({"text": piece, "voice": voice_name}).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data, rate = sf.read(io.BytesIO(resp.read()), dtype="float32")
            audio += [data, np.zeros(int(GAP_SECONDS * rate), dtype=np.float32)]
        sf.write(out_path, np.concatenate(audio[:-1]), rate)
        return True
    except Exception as exc:
        print(f"warning: voice server failed, speaking locally: {exc}", file=sys.stderr)
        return False


def main() -> None:
    # Hermes passes a text file containing what the bot wants to say, plus the
    # path where it expects the finished .wav.
    in_path, out_path = sys.argv[1], sys.argv[2]
    voice_name = sys.argv[3] if len(sys.argv) > 3 else "kanye"
    text = Path(in_path).read_text(encoding="utf-8").strip()
    if not text:
        sys.exit("empty input text")
    mode = mode_voice_settings()
    if mode.get("voice") is False:
        # Personal mode: text-only, keep the GPU free. Hermes still sends the text reply.
        sys.exit("voice replies are off in personal mode (text-only)")
    if mode.get("voice_warm") and speak_via_voice_server(text, voice_name, out_path):
        return
    reference = VOICE_DIR / "reference" / f"{voice_name}.wav"
    if not reference.exists():
        sys.exit(f"no reference clip at {reference}")

    # Heavy imports (PyTorch, Chatterbox) happen here so a bad argument fails fast.
    import numpy as np
    import soundfile as sf
    from chatterbox.tts import ChatterboxTTS

    # Make room on the GPU first, or Chatterbox runs out of memory while qwen is loaded.
    # Not in server mode: there the local model is sized to fit next to the voice model.
    if not mode.get("voice_warm"):
        free_ollama_vram()

    # Loads the model on the GPU. The first run downloads it (about 3 GB) into
    # ~/.cache/huggingface, later runs load it from there.
    model = ChatterboxTTS.from_pretrained(device="cuda")

    # The voice is learned from the reference clip once, then reused for every chunk.
    model.prepare_conditionals(str(reference), exaggeration=EXAGGERATION)

    gap = np.zeros(int(GAP_SECONDS * model.sr), dtype=np.float32)
    pieces = []
    for chunk in split_into_chunks(text):
        wav = model.generate(chunk, exaggeration=EXAGGERATION, cfg_weight=CFG_WEIGHT)
        pieces += [wav.squeeze().cpu().numpy(), gap]

    # Writes the finished .wav where Hermes asked for it (dropping the trailing gap).
    sf.write(out_path, np.concatenate(pieces[:-1]), model.sr)


if __name__ == "__main__":
    main()
