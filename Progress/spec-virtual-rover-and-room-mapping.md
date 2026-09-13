# Architecture Spec: Virtual Rover Test Harness + Room Mapping from Walkthrough Video

**Project:** PE448 Skripsie — Voice-Commanded Indoor Rover (Utroff, supervised by Theart RP)
**Drafted:** 2026-09-11, via conversation with Claude, for implementation by Claude Code
**Scope:** Two additive, independent changes:

1. A `VirtualRover` that actually integrates plan steps into a 2D pose, so a plan can be judged geometrically without hardware — replacing what `SimulatedRover` does today (sleep for a fixed duration, always report `"ok"`).
2. A one-time, offline room-scan step that turns the existing walkthrough video into a **floor-plane coordinate map** (not full 3D SLAM), giving `scene.py`'s object digest real positions instead of just names.

Both are additive: nothing about the real hardware path (once `ROVER=rover` exists) or the current `sim` mode changes unless you opt in.

---

## 0. Design principles this doesn't override

- **This does not test perception.** `VirtualRover` proves a plan is geometrically executable (stays on the floor, reaches its targets, doesn't drive through a wall). It has no opinion on whether the VLM correctly identified "the kitchen table" in the first place — that's RQ2's territory and needs its own test track (canned frames/transcripts), not this harness. Don't let "we have E2E sim coverage now" quietly come to mean the VLM-grounding tests stopped happening.
- **This is not SLAM.** The room-scan step runs once, offline, before a mission starts. The SLAM-based patrol robot design was already dropped for good reasons — indoor rover, no depth sensor, laptop is the brain not the Pi. Nothing here runs continuously during a mission or does live relocalization. If live localization turns out to be needed later, that's a separate, much bigger scope conversation with Theart, not a quiet extension of this spec.

---

## 1. Component A — Virtual Rover Test Harness

### 1.1 Where it plugs in

`rover.py` already has the right shape for this: `RoverController` ABC, `execute_step(step: dict) -> dict`, and a `get_rover()` factory keyed off `ROVER` in `.env`. Add a third implementation next to `SimulatedRover`:

```
ROVER=virtual
```

### 1.2 `VirtualRover`

```python
class VirtualRover(RoverController):
    """Integrates each step into a 2D pose instead of just sleeping.
    Same execute_step(step) -> {"status", "detail"} contract as SimulatedRover,
    so mission.py's blocked/halted handling needs no changes."""

    name = "virtual"

    def __init__(self, room: "RoomMap", step_distance_m: float = 0.5, step_turn_deg: float = 90.0):
        self.room = room
        self.pose = Pose(x=room.start_x, y=room.start_y, theta=room.start_theta)
        self.step_distance_m = step_distance_m
        self.step_turn_deg = step_turn_deg
        self.trace: list[dict] = []   # appended every step, for the report plot
        self._halted = threading.Event()

    def execute_step(self, step: dict) -> dict:
        self._halted.clear()
        action, target = step.get("action"), step.get("target")

        if action == "move":
            new_pose = self.pose.advance(self.step_distance_m)
        elif action == "turn":
            new_pose = self.pose.rotate(self.step_turn_deg)   # direction from target text, default right
        elif action == "approach":
            new_pose = self.pose.advance_toward(self.room.locate(target), self.step_distance_m)
        elif action in ("scan", "observe", "stop", "report"):
            new_pose = self.pose   # no motion
        else:
            return {"status": "blocked", "detail": f"unknown action {action!r}"}

        if self.room.collides(self.pose, new_pose):
            self.trace.append({"t": time.time(), **new_pose.__dict__, "step": step, "status": "blocked"})
            return {"status": "blocked", "detail": "path crosses a wall/obstacle"}

        self.pose = new_pose
        self.trace.append({"t": time.time(), **self.pose.__dict__, "step": step, "status": "ok"})
        return {"status": "ok", "detail": None}

    def halt(self) -> None:
        self._halted.set()
```

Key point: this reuses the exact `"blocked"` contract `mission.py` already understands (~line 158 — halts the mission, speaks "I am blocked and have stopped"). A plan that would drive through a wall fails the same way a real collision would, with zero changes to `mission.py`.

### 1.3 `RoomMap` — the piece both components need

`VirtualRover.locate(target)` needs somewhere to look up "the kitchen table" as a coordinate. Nothing in the codebase has that today — `scene.py`'s digest is names only, no positions. Define it once, shared by Component A and B:

```python
@dataclass
class RoomMap:
    width_m: float
    height_m: float
    obstacles: list[Polygon]                    # walls + furniture footprints
    landmarks: dict[str, tuple[float, float]]    # "kitchen table" -> (x, y)
    start_x: float = 0.0
    start_y: float = 0.0
    start_theta: float = 0.0

    def locate(self, target: str) -> tuple[float, float]: ...
    def collides(self, from_pose: "Pose", to_pose: "Pose") -> bool: ...
```

Until Component B exists, hand-author one `RoomMap` as a small JSON fixture (a rectangle, a couple of obstacle boxes, 3-4 landmark coordinates matching the room in `VLM test/room_tour1.MOV`) — enough to start exercising `VirtualRover` and the mission loop this week. Component B later replaces the hand-authored JSON with a generated one; `VirtualRover` doesn't change.

### 1.4 Evaluation output

Skip a live canvas — not worth the maintenance for what this needs to prove. `VirtualRover.trace` is a list of `{t, x, y, theta, step, status}`; dump it to `brain/logs/virtual_run_<id>.jsonl` at mission end. A ~15-line matplotlib script (room outline + obstacle boxes + the traced path, colour-coded by status) turns any run into a figure straight into `evaluation.tex`. This is also the sequential-instruction-limit test harness flagged as unresolved — it falls out of this for free.

---

## 2. Component B — Camera Calibration + Floor-Plane Room Map

### 2.1 Calibration (do this regardless of what follows)

Standard OpenCV chessboard calibration against the actual Pi camera module:

- Print a checkerboard (e.g. 9x6 internal corners, known square size).
- Capture 20-30 stills at varied distance/angle/tilt.
- `cv2.calibrateCamera` -> intrinsic matrix `K` + distortion coefficients.
- Save to `brain/camera_calibration.json`, load once at startup, undistort frames before they reach `frames.py` / `vlm.py`. Improves VLM grounding accuracy independent of anything below.

### 2.2 Why not full SLAM

ORB-SLAM3 / RTAB-Map-style mapping is exactly the complexity class already dropped in the SLAM-based-patrol-robot pivot, and it's normally paired with RGB-D/stereo input — the TurboPi kit has camera + ultrasonic + line sensors only, no depth sensor. Monocular SLAM's scale ambiguity would need the ultrasonic sensor or wheel odometry to resolve anyway, and it would need to run continuously during a mission, which this doesn't.

### 2.3 Floor-plane homography instead

The rover's camera looks at a flat floor from a fixed, known height and tilt. That means a *single* homography — computed once, using the calibration above plus a handful of known floor reference points (tile-grid corners, or a printed marker at a measured position) — maps any pixel in the frame straight to floor-plane coordinates. No 3D reconstruction, no per-frame pose estimation, no depth sensor needed.

Pipeline (offline, run once per room, against `VLM test/room_tour1.MOV` or a fresh walkthrough):

1. Extract the same keyframes `scene.py` already extracts.
2. For each keyframe, project the pixel location of each object `vlm.py` already identifies (bounding-box centroid, or a rough estimate from the "where" text) through the homography -> floor (x, y).
3. Reconcile across keyframes — the same "kitchen table" seen from two angles should land near the same (x, y); average, or keep the higher-confidence read.
4. Emit a `RoomMap` (Section 1.3): `landmarks` from the reconciled object positions, `obstacles` as rough boxes around anything `SceneFrame.obstacles` already flags, `width_m`/`height_m` from the homography's known floor extent.

### 2.4 Where this lands in the existing code

- New module: `brain/room_map.py` — the homography math + the `RoomMap` dataclass (shared with Component A).
- `scene.py`'s `Scene`/`SceneFrame` gain an optional `room_map: Optional[RoomMap]` alongside the existing text `digest()` — the digest keeps driving the planner/clarification prompts unchanged; the map is net-new, consumed by `VirtualRover` and, later, by `approach`'s real-hardware implementation.
- Nothing about the current text-digest pipeline is removed or changed — additive, same pattern as the swappable-backend spec.

### 2.5 Open items to decide before implementation

- Confirm the camera's mount height/tilt on the TurboPi is fixed (the homography assumes this) — if it can articulate, the homography needs recomputing per known tilt angle, which is more work.
- Floor reference points: cheapest is a printed marker (or the room's own tile/floorboard grid, if regular) placed at a few measured positions during the walkthrough recording.
- Decide the reconciliation tolerance for "same landmark, different frame" before writing the matching code — a judgment call, not a math problem, worth settling on paper first.

---

Framing for the report: floor-plane homography over full SLAM is a citable methodological choice, justified directly by the sensor budget (no depth sensor) and the earlier SLAM-scope decision — not a shortcut taken for lack of time.
