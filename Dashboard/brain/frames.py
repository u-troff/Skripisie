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
    """0.0 identical structure, 1.0 maximally different.

    Pearson correlation between the two thumbnails, so a frame that is just
    brighter/darker than the last one (exposure drift, a light flickering)
    still reads as "the same place", while a frame whose spatial pattern has
    actually decorrelated -- the rover turned to face a different wall --
    reads as changed. Plain mean-abs-diff conflated the two: a uniform
    brightness shift could cross the threshold as easily as a real scene
    change.
    """
    x = a.thumb.ravel() - a.thumb.mean()
    y = b.thumb.ravel() - b.thumb.mean()
    denom = float(np.sqrt((x * x).sum() * (y * y).sum()))
    if denom < 1e-6:
        # A flat/blank thumbnail (e.g. a blown-out wall) has no structure to
        # correlate against -- fall back to plain intensity difference so two
        # different flat frames still count as changed.
        return float(np.abs(a.thumb - b.thumb).mean() / 255.0)
    correlation = float((x * y).sum() / denom)
    return min(1.0, max(0.0, 1.0 - correlation))


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
    """Codec keyframes, deduplicated by visual distance, spread across the
    whole video, sharpest-in-each-slice.

    skip_frame="NONKEY" avoids decoding the whole video — only I-frames are
    reconstructed, which is fast and a decent proxy for scene changes.

    The video is read to the end (no early cap on candidates), then split into
    max_frames equal time slices and the sharpest candidate in each slice is
    taken, so the picks cover the whole clip instead of clustering wherever
    the camera happened to be still. Ranking purely by sharpness, or stopping
    after N candidates, would leave parts of the room unrepresented — and an
    object that is not in any keyframe can never be asked about later.
    """
    picked: List[bytes] = []
    try:
        with av.open(io.BytesIO(video)) as container:
            stream = container.streams.video[0]
            stream.codec_context.skip_frame = "NONKEY"

            # (sharpness, moment, jpeg) — encode now so decoded frames are not
            # all held in memory while the rest of the video is read.
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
                candidates.append((stats.sharpness, moment, _encode_jpeg(frame, max_edge)))
                last, last_t = stats, moment

            if len(candidates) <= max_frames:
                chosen = candidates
            else:
                start = candidates[0][1]
                span = max(1e-6, candidates[-1][1] - start)
                slices = [[] for _ in range(max_frames)]
                for item in candidates:
                    index = min(max_frames - 1, int((item[1] - start) / span * max_frames))
                    slices[index].append(item)

                # Sharpest candidate per time slice; a blurred pan frame wastes
                # a whole VLM call, so sharpness still breaks ties inside a slice.
                chosen = [max(group, key=lambda item: item[0]) for group in slices if group]

                # Empty slices (a long still stretch) leave spare slots: fill
                # them with the sharpest candidates not already taken.
                if len(chosen) < max_frames:
                    taken = {id(item) for item in chosen}
                    spare = sorted((c for c in candidates if id(c) not in taken),
                                   key=lambda item: item[0], reverse=True)
                    chosen += spare[:max_frames - len(chosen)]

            picked = [item[2] for item in sorted(chosen, key=lambda item: item[1])]
    except Exception as exc:
        log.error("keyframe extraction failed: %s", exc)

    log.info("[keyframes] %d frame(s) from %d bytes", len(picked), len(video))
    return picked
