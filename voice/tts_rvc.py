#!/usr/bin/env python3
"""Hermes command TTS for ProxAI (RVC voices, e.g. Gru): Piper speech, then RVC voice conversion.

How it works:
  1. Piper (local text-to-speech) reads the text out loud in a generic voice (Ryan).
  2. RVC (voice conversion) takes that recording and re-voices it as the chosen
     character, keeping the timing and intonation but swapping the voice.

RVC cannot read text by itself, it only converts existing audio. That is why
Piper runs first.

Hermes calls this script from config.yaml (tts.providers.<name>.command) as:
  .venv/bin/python tts_rvc.py {input_path} {output_path} <voice>

Usage: tts_rvc.py <input_text_file> <output_wav> [voice]
voice = folder name under models/ (default: kanye). Each folder holds one RVC
model: a .pth file (the voice) and optionally a .index file (sharpens the accent).
"""
import os
import sys
import tempfile
import wave
from pathlib import Path

# Everything lives next to this script, so it works no matter which folder
# Hermes runs it from.
VOICE_DIR = Path(__file__).resolve().parent

# Base voice Piper speaks in before conversion. A male voice is used because RVC
# keeps the input's pitch, and the target voices (Kanye, Gru) are male.
PIPER_MODEL = VOICE_DIR / "piper" / "en_US-ryan-medium.onnx"

# Optional pitch shift in semitones, set with the PROXAI_RVC_PITCH environment
# variable. 0 = no change. Negative is deeper, positive is higher.
PITCH = int(os.getenv("PROXAI_RVC_PITCH", "0"))  # semitones; try -4 if using a female base voice


def find_rvc_model(name: str) -> tuple[Path, Path | None]:
    """Find the .pth voice model (required) and .index file (optional) in models/<name>/."""
    folder = VOICE_DIR / "models" / name
    pth = next(folder.glob("*.pth"), None)
    if pth is None:
        sys.exit(f"no .pth model in {folder}")
    return pth, next(folder.glob("*.index"), None)


def main() -> None:
    # Hermes passes a text file containing what the bot wants to say, plus the
    # path where it expects the finished .wav.
    in_path, out_path = sys.argv[1], sys.argv[2]
    voice_name = sys.argv[3] if len(sys.argv) > 3 else "kanye"
    text = Path(in_path).read_text(encoding="utf-8").strip()
    if not text:
        sys.exit("empty input text")

    # Heavy imports (PyTorch, RVC) happen here so a bad argument fails fast.
    import torch
    from fairseq.data.dictionary import Dictionary
    from piper import PiperVoice
    from rvc_python.infer import RVCInference

    # PyTorch 2.6+ loads checkpoints weights-only by default. RVC's hubert_base.pt
    # stores a fairseq Dictionary, so allow just that class.
    torch.serialization.add_safe_globals([Dictionary])

    # The intermediate Piper recording goes in a temp folder that is deleted
    # automatically when the block ends.
    with tempfile.TemporaryDirectory() as tmp:
        # Step 1: text -> speech in the generic Piper voice.
        base_wav = os.path.join(tmp, "base.wav")
        piper_voice = PiperVoice.load(str(PIPER_MODEL))
        with wave.open(base_wav, "wb") as wf:
            piper_voice.synthesize_wav(text, wf)

        # Step 2: generic speech -> character voice with RVC on the GPU.
        pth, index = find_rvc_model(voice_name)
        rvc = RVCInference(device="cuda:0")  # use "cpu" if the GPU is unavailable (slower)
        rvc.load_model(str(pth), index_path=str(index) if index else "")
        # f0method="rmvpe": the most accurate pitch tracker RVC has.
        # f0up_key: the pitch shift from PROXAI_RVC_PITCH.
        # index_rate: how strongly the .index pulls toward the target voice
        #   (0.5-0.9; higher = more like the character, but more artifacts).
        rvc.set_params(f0method="rmvpe", f0up_key=PITCH, index_rate=0.75)
        # Writes the finished .wav where Hermes asked for it.
        rvc.infer_file(base_wav, out_path)


if __name__ == "__main__":
    main()
