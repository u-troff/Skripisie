"""Room video -> keyframes -> per-frame object inventory -> a text digest.

Built once per room, before the conversation starts. The digest is what the
clarification loop and the planner reason over; one representative frame still
goes to the VLM so it can look as well as read.
"""

import base64
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config
import frames
import vlm
from log_setup import get_logger

log = get_logger("scene")

KEYFRAMES_MAX = config.get_int("KEYFRAMES_MAX", 10)
FRAME_MAX_EDGE = config.get_int("FRAME_MAX_EDGE", 512)


@dataclass
class SceneFrame:
    index: int
    jpeg: bytes
    place: str = ""
    objects: List[dict] = field(default_factory=list)
    obstacles: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def text(self) -> str:
        head = f"View {self.index + 1}"
        if self.place:
            head += f" ({self.place})"
        if self.error:
            return f"{head}: could not be read - {self.error}"

        parts = []
        for obj in self.objects:
            attributes = " ".join(str(a) for a in (obj.get("attributes") or []))
            label = (attributes + " " + str(obj.get("name") or "")).strip()
            where = str(obj.get("where") or "").strip()
            parts.append(label + (f" ({where})" if where else ""))
        if self.obstacles:
            parts.append("floor obstacles: " + ", ".join(str(o) for o in self.obstacles))
        return f"{head}: " + ("; ".join(parts) if parts else "nothing identifiable")

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "place": self.place,
            "objects": self.objects,
            "obstacles": self.obstacles,
            "error": self.error,
            "thumbnail": base64.b64encode(self.jpeg).decode("ascii"),
        }


@dataclass
class Scene:
    scene_id: str
    frames: List[SceneFrame] = field(default_factory=list)
    elapsed_s: float = 0.0
    created_at: float = field(default_factory=time.time)

    def digest(self) -> str:
        return "\n".join(frame.text() for frame in self.frames)

    def representative(self) -> Optional[bytes]:
        """One frame the VLM can actually look at during clarification."""
        for frame in self.frames:
            if frame.error is None:
                return frame.jpeg
        return self.frames[0].jpeg if self.frames else None

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "frame_count": len(self.frames),
            "elapsed": self.elapsed_s,
            "digest": self.digest(),
            "frames": [frame.to_dict() for frame in self.frames],
        }


class SceneStore:
    def __init__(self) -> None:
        self._scenes: Dict[str, Scene] = {}
        self._lock = threading.Lock()

    def put(self, scene: Scene) -> None:
        with self._lock:
            self._scenes[scene.scene_id] = scene

    def get(self, scene_id: str) -> Optional[Scene]:
        with self._lock:
            return self._scenes.get(scene_id)


store = SceneStore()


def build_scene(video: bytes) -> Scene:
    """Blocking: keyframe extraction plus one VLM call per surviving frame.

    Call it from a threadpool — on local Ollama this is roughly 15s per frame.
    """
    started = time.perf_counter()
    jpegs = frames.keyframes(video, max_frames=KEYFRAMES_MAX, max_edge=FRAME_MAX_EDGE)
    scene = Scene(scene_id=uuid.uuid4().hex[:12])

    for index, jpeg in enumerate(jpegs):
        parsed = vlm.inventory_frame(jpeg)
        if vlm.failed(parsed):
            # Keep the frame with its error rather than dropping it: a frame the
            # model could not read is different from a frame with nothing in it,
            # and the difference matters when a command fails to ground.
            scene.frames.append(
                SceneFrame(index=index, jpeg=jpeg, error=str(parsed.get(vlm.ERROR_KEY)))
            )
            continue
        scene.frames.append(
            SceneFrame(
                index=index,
                jpeg=jpeg,
                place=str(parsed.get("place") or ""),
                objects=[o for o in (parsed.get("objects") or []) if isinstance(o, dict)],
                obstacles=[str(o) for o in (parsed.get("obstacles") or [])],
            )
        )

    scene.elapsed_s = time.perf_counter() - started
    store.put(scene)
    log.info("[scene %s] %d frame(s), %d unreadable, %.1fs",
             scene.scene_id, len(scene.frames),
             sum(1 for f in scene.frames if f.error), scene.elapsed_s)
    return scene
