"""Room video -> keyframes -> per-frame object inventory -> a text digest.

Built once per room, before the conversation starts. The digest is what the
clarification loop and the planner reason over; one representative frame still
goes to the VLM so it can look as well as read.
"""

import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import config
import frames
import vlm
from log_setup import get_logger

log = get_logger("scene")

KEYFRAMES_MAX = config.get_int("KEYFRAMES_MAX", 10)
FRAME_MAX_EDGE = config.get_int("FRAME_MAX_EDGE", 512)

# The digest is what the planner and verifier read, so it is deliberately much
# terser than the stored inventory: the full inventory is kept on disk and in
# vocabulary(), but ten frames of five attributes per object ran to ~5k prompt
# tokens and made the verifier see "multiple matching boxes" everywhere.
DIGEST_MAX_OBJECTS = config.get_int("DIGEST_MAX_OBJECTS_PER_FRAME", 8)
DIGEST_MAX_ATTRS = config.get_int("DIGEST_MAX_ATTRS", 2)
DIGEST_WHERE_CHARS = config.get_int("DIGEST_WHERE_CHARS", 50)
# Surfaces a rover cannot go to or look "at"; dropped from the digest only.
_STRUCTURAL = ("wall", "floor", "tile", "ceiling", "grout", "stripe", "tape",
               "marking", "baseboard", "skirting", "line")

# Persisted scenes, sibling to rooms/ and logs/. A scene built once survives a
# backend restart here, so re-uploading the same room video — and paying the
# ~15s/frame VLM inventory cost again — is a choice, not a requirement.
SCENES_DIR = Path(__file__).parent / "scenes"


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
            name = str(obj.get("name") or "").strip()
            if not name or any(word in name.lower() for word in _STRUCTURAL):
                continue
            raw = obj.get("attributes") or []
            if isinstance(raw, dict):
                raw = list(raw.values())
            attributes = " ".join(str(a) for a in list(raw)[:DIGEST_MAX_ATTRS])
            label = (attributes + " " + name).strip()
            where = str(obj.get("where") or "").strip()[:DIGEST_WHERE_CHARS].strip()
            parts.append(label + (f" ({where})" if where else ""))
            if len(parts) >= DIGEST_MAX_OBJECTS:
                break
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
    name: str = ""
    frames: List[SceneFrame] = field(default_factory=list)
    elapsed_s: float = 0.0
    created_at: float = field(default_factory=time.time)

    def digest(self) -> str:
        return "\n".join(frame.text() for frame in self.frames)

    def vocabulary(self, limit: int = 20) -> List[str]:
        """Distinct catalogued names — objects first, then place labels.

        The grounding list handed to the drive checks (vlm._known_block) and
        the list report.py scores their answers against, so it lives here
        rather than in either caller: two definitions of "what is in this room"
        would drift, and the whole point is that both ends mean the same thing.

        Objects before places because a check can point at an object; "kitchen"
        is a weaker claim, so places only fill what room is left. Deduplicated
        case-insensitively, first-seen casing kept. Capped because a small VLM
        handed a long list starts reporting things because they are listed.
        """
        names: List[str] = []
        seen = set()

        def add(raw) -> None:
            name = str(raw or "").strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                names.append(name)

        for frame in self.frames:
            for obj in frame.objects:
                add(obj.get("name"))
        for frame in self.frames:
            add(frame.place)
        return names[:limit]

    def representative(self) -> Optional[bytes]:
        """One frame the VLM can actually look at during clarification."""
        for frame in self.frames:
            if frame.error is None:
                return frame.jpeg
        return self.frames[0].jpeg if self.frames else None

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "name": self.name,
            "frame_count": len(self.frames),
            "elapsed": self.elapsed_s,
            "digest": self.digest(),
            "frames": [frame.to_dict() for frame in self.frames],
        }

    def save_to_disk(self) -> None:
        """One folder per scene: meta.json (everything but the JPEGs) plus
        one frame_<i>.jpg each. Lets _load_scene_from_disk rebuild an
        identical Scene without re-running keyframe extraction or the VLM."""
        folder = SCENES_DIR / self.scene_id
        folder.mkdir(parents=True, exist_ok=True)
        meta = {
            "scene_id": self.scene_id,
            "name": self.name,
            "created_at": self.created_at,
            "elapsed_s": self.elapsed_s,
            "frames": [
                {"index": f.index, "place": f.place, "objects": f.objects,
                 "obstacles": f.obstacles, "error": f.error}
                for f in self.frames
            ],
        }
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        for f in self.frames:
            (folder / f"frame_{f.index}.jpg").write_bytes(f.jpeg)


def _load_scene_from_disk(scene_id: str) -> Optional[Scene]:
    folder = SCENES_DIR / scene_id
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    scene = Scene(scene_id=meta["scene_id"], name=meta.get("name", ""),
                  elapsed_s=meta.get("elapsed_s", 0.0),
                  created_at=meta.get("created_at", time.time()))
    for fmeta in meta.get("frames", []):
        jpeg_path = folder / f"frame_{fmeta['index']}.jpg"
        scene.frames.append(SceneFrame(
            index=fmeta["index"],
            jpeg=jpeg_path.read_bytes() if jpeg_path.exists() else b"",
            place=fmeta.get("place", ""), objects=fmeta.get("objects") or [],
            obstacles=fmeta.get("obstacles") or [], error=fmeta.get("error"),
        ))
    return scene


def list_saved_scenes() -> List[dict]:
    """Newest first, for the UI's room picker."""
    if not SCENES_DIR.exists():
        return []
    out = []
    for folder in sorted(SCENES_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        out.append({"scene_id": meta["scene_id"], "name": meta.get("name", ""),
                    "frame_count": len(meta.get("frames", [])),
                    "created_at": meta.get("created_at")})
    return out


class SceneStore:
    def __init__(self) -> None:
        self._scenes: Dict[str, Scene] = {}
        self._lock = threading.Lock()

    def put(self, scene: Scene) -> None:
        with self._lock:
            self._scenes[scene.scene_id] = scene

    def get(self, scene_id: str) -> Optional[Scene]:
        with self._lock:
            found = self._scenes.get(scene_id)
        if found is not None:
            return found
        loaded = _load_scene_from_disk(scene_id)
        if loaded is not None:
            self.put(loaded)
        return loaded


store = SceneStore()


def build_scene(video: bytes, name: str = "") -> Scene:
    """Blocking: keyframe extraction plus one VLM call per surviving frame.

    Call it from a threadpool — on local Ollama this is roughly 15s per frame.
    """
    started = time.perf_counter()
    jpegs = frames.keyframes(video, max_frames=KEYFRAMES_MAX, max_edge=FRAME_MAX_EDGE)
    scene = Scene(scene_id=uuid.uuid4().hex[:12], name=name)

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
    scene.save_to_disk()
    log.info("[scene %s] %d frame(s), %d unreadable, %.1fs",
             scene.scene_id, len(scene.frames),
             sum(1 for f in scene.frames if f.error), scene.elapsed_s)
    return scene
