import io
import os
from typing import Optional, Union

import numpy as np
from faster_whisper import WhisperModel

MODEL_NAME = os.getenv("WHISPER_MODEL", "digiphyte/fluister-turbo")
LANGUAGE = os.getenv("WHISPER_LANGUAGE", "en")

_model = None


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(
            MODEL_NAME,
            device="cpu",
            compute_type="int8",
            cpu_threads=8,
        )
    return _model


def transcribe_audio(
    audio: Union[bytes, np.ndarray],
    language: Optional[str] = None,
) -> str:
    # Encoded containers (webm/mp4/wav) go through FFmpeg; raw float32 samples
    # from sounddevice have no header and must be passed as an array instead.
    source = audio if isinstance(audio, np.ndarray) else io.BytesIO(audio)
    segments, info = get_model().transcribe(
        source,
        language=language or LANGUAGE,
        beam_size=5,
    )
    return " ".join(segment.text for segment in segments).strip()
