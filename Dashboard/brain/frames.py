"""Frame analysis: no models, no session state, no I/O beyond bytes in.

Everything here is cheap enough for every inbound frame. The expensive tiers
live in mission.py and only fire when these functions say something changed.
"""

import io
from dataclasses import dataclass
from typing import List, Optional

import av
import numpy as np

from log_setup import get_logger

log = get_logger("frames")

# Comparison thumbnail. Small enough that a full diff costs nothing, large
# enough to notice the rover turning to face a different wall.
THUMB = 32
# Aspect is deliberately distorted here; it is only a blur metric.
BLUR_SIZE = 256


@dataclass
class FrameStats:
    thumb: np.ndarray  # THUMB x THUMB grayscale, float32
    sharpness: float   # variance of Laplacian — low means motion blur
    width: int
    height: int


def _first_frame(raw: bytes):
    try:
        with av.open(io.BytesIO(raw)) as container:
            for frame in container.decode(video=0):
                return frame
    except Exception as exc:
        log.warning("undecodable frame (%d bytes): %s", len(raw), exc)
    return None


def _laplacian_var(grey: np.ndarray) -> float:
    if grey.shape[0] < 3 or grey.shape[1] < 3:
        return 0.0
    lap = (
        -4.0 * grey[1:-1, 1:-1]
        + grey[:-2, 1:-1]
        + grey[2:, 1:-1]
        + grey[1:-1, :-2]
        + grey[1:-1, 2:]
    )
    return float(lap.var())


def _stats(frame) -> FrameStats:
    thumb = frame.reformat(width=THUMB, height=THUMB, format="gray").to_ndarray()
    blur = frame.reformat(width=BLUR_SIZE, height=BLUR_SIZE, format="gray").to_ndarray()
    return FrameStats(
        thumb=thumb.astype(np.float32),
        sharpness=_laplacian_var(blur.astype(np.float32)),
        width=frame.width,
        height=frame.height,
    )


def analyse(raw: bytes) -> Optional[FrameStats]:
    frame = _first_frame(raw)
    return _stats(frame) if frame is not None else None


def distance(a: FrameStats, b: FrameStats) -> float:
    """0.0 identical, 1.0 maximally different."""
    return float(np.abs(a.thumb - b.thumb).mean() / 255.0)


def _encode_jpeg(frame, max_edge: int) -> bytes:
    scale = min(1.0, float(max_edge) / max(frame.width, frame.height))
    width = max(2, int(frame.width * scale) // 2 * 2)
    height = max(2, int(frame.height * scale) // 2 * 2)
    codec = av.CodecContext.create("mjpeg", "w")
    codec.width, codec.height, codec.pix_fmt = width, height, "yuvj420p"
    packets = codec.encode(frame.reformat(width=width, height=height, format="yuvj420p"))
    packets.extend(codec.encode(None))
    return b"".join(bytes(packet) for packet in packets)


def keyframes(
    video: bytes,
    max_frames: int = 5,
    min_gap_s: float = 1.0,
    min_distance: float = 0.06,
    max_edge: int = 512,
) -> List[bytes]:
    """Codec keyframes, deduplicated by visual distance, ranked by sharpness.

    skip_frame="NONKEY" avoids decoding the whole video — only I-frames are
    reconstructed, which is fast and a decent proxy for scene changes.
    """
    picked: List[bytes] = []
    try:
        with av.open(io.BytesIO(video)) as container:
            stream = container.streams.video[0]
            stream.codec_context.skip_frame = "NONKEY"

            candidates = []
            last: Optional[FrameStats] = None
            last_t = -1e9
            for frame in container.decode(stream):
                moment = float(frame.time or 0.0)
                if moment - last_t < min_gap_s:
                    continue
                stats = _stats(frame)
                if last is not None and distance(stats, last) < min_distance:
                    continue
                candidates.append((stats.sharpness, moment, frame))
                last, last_t = stats, moment
                if len(candidates) >= 40:
                    break

            # A blurred pan frame wastes a whole VLM call, so prefer the
            # sharpest survivors, then restore chronological order.
            candidates.sort(key=lambda item: item[0], reverse=True)
            chosen = sorted(candidates[:max_frames], key=lambda item: item[1])
            picked = [_encode_jpeg(item[2], max_edge) for item in chosen]
    except Exception as exc:
        log.error("keyframe extraction failed: %s", exc)

    log.info("[keyframes] %d frame(s) from %d bytes", len(picked), len(video))
    return picked
