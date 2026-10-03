"""Local text-to-speech via Piper, mirroring stt.py's cached-model pattern.

Voice models are not auto-downloaded the way faster-whisper's are — fetch
one once with:

    venv/bin/python -m piper.download_voices en_US-lessac-medium

then point PIPER_VOICE_MODEL at the resulting .onnx file. Its paired
<name>.onnx.json config file must sit alongside it in the same directory —
that's how Piper ships a voice, not a separate setting.

No Afrikaans voice exists in the rhasspy/piper-voices set (checked
2026-09-23) — not a blocker: every _speak() call site in pipeline.py speaks
a hardcoded English string regardless of session.language, so only STT
needs to branch on language. An English voice is correct for now; spoken
output in the session's own language would mean localizing those template
strings, a separate feature from picking a TTS engine.
"""

import io
import os
import wave
from typing import Optional

from piper import PiperVoice

_BRAIN_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.getenv("PIPER_VOICE_MODEL", "")
if MODEL_PATH and not os.path.isabs(MODEL_PATH):
    MODEL_PATH = os.path.join(_BRAIN_DIR, MODEL_PATH)

_voice: Optional[PiperVoice] = None


def get_voice() -> PiperVoice:
    global _voice
    if _voice is None:
        if not MODEL_PATH:
            raise RuntimeError(
                "PIPER_VOICE_MODEL is not set in .env — point it at a "
                "downloaded .onnx voice file, e.g. after running "
                "`venv/bin/python -m piper.download_voices en_US-lessac-medium`"
            )
        _voice = PiperVoice.load(MODEL_PATH)
    return _voice


def synthesize_speech(text: str) -> bytes:
    """Text in, WAV bytes out — the shape _speak() needs to base64-encode."""
    buffer = io.BytesIO()
    # synthesize_wav() calls setframerate()/writeframes() on its argument, so
    # it needs an actual wave.Wave_write, not a bare BytesIO. Wrapping an
    # already-open file-like object like this does not close `buffer` when
    # the `with` block exits — only close() on a wave-opened filename does
    # that — so getvalue() below still sees the finished WAV bytes.
    with wave.open(buffer, "wb") as wav_file:
        get_voice().synthesize_wav(text, wav_file)
    return buffer.getvalue()
