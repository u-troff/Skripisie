"""Hand-authored 2D room geometry — the node table VirtualRover drives on.

Deliberately *not* estimated from anything. Coordinates are typed in once per
test room off a sketch and a tape measure, so nothing here recovers camera pose,
depth, or structure from video. If an edit to this file starts needing a camera
pose, stop and re-read the scope note in CLAUDE.md.

Frame: metres; x to the right, y away from the start wall; theta in degrees
counter-clockwise, 0 = +x. Landmark coordinates are the *standing spot* a rover
can occupy to look at a thing, not the thing's centroid — a coordinate inside
the furniture is unreachable by construction.
"""

import difflib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from log_setup import get_logger

log = get_logger("room_map")


def _flag(name: str, default: bool = False) -> bool:
    raw = config.get(name, "1" if default else "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def footprint_clearance_m() -> float:
    """Half the rover's diagonal — safe anywhere the centre is, since a
    mecanum base turns on the spot — plus a safety margin. The one formula
    both this module's footprint check and VirtualRover's occupancy grid use,
    so a landmark that validates here can never turn out unreachable there.
    See Progress/spec-planner-profiles-and-virtual-sweep.md §1."""
    length = config.get_float("VIRTUAL_ROBOT_LENGTH_M", 0.187)
    width = config.get_float("VIRTUAL_ROBOT_WIDTH_M", 0.162)
    margin = config.get_float("VIRTUAL_SAFETY_MARGIN_M", 0.05)
    return math.hypot(length, width) / 2.0 + margin

BRAIN_DIR = Path(__file__).parent

Point = Tuple[float, float]
Polygon = List[Point]

# Below this, a fuzzy name match is a miss — a hallucinated target — rather than
# a sloppily worded real one. Loose enough for "the kitchen table" ->
# "kitchen table", tight enough that "the fridge" does not quietly become
# "the desk". A loose match that silently succeeds would hide the exact events
# RQ2 is trying to count, so this errs tight.
MATCH_CUTOFF = 0.72

_ARTICLES = ("the ", "a ", "an ", "my ", "this ", "that ", "some ")


def normalise(name) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())
    text = " ".join(text.split())
    while True:
        for article in _ARTICLES:
            if text.startswith(article):
                text = text[len(article):]
                break
        else:
            return text


def wrap_deg(degrees: float) -> float:
    return (float(degrees) + 180.0) % 360.0 - 180.0


@dataclass
class Pose:
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0  # degrees CCW from +x

    def point(self) -> Point:
        return (self.x, self.y)

    def distance_to(self, point: Point) -> float:
        return math.hypot(point[0] - self.x, point[1] - self.y)

    def bearing_to(self, point: Point) -> float:
        return wrap_deg(math.degrees(math.atan2(point[1] - self.y, point[0] - self.x)))

    def advance(self, distance: float) -> "Pose":
        rad = math.radians(self.theta)
        return Pose(self.x + distance * math.cos(rad),
                    self.y + distance * math.sin(rad), self.theta)

    def rotate(self, degrees: float) -> "Pose":
        return Pose(self.x, self.y, wrap_deg(self.theta + degrees))

    def advance_toward(self, point: Point, limit: float, stop_short: float = 0.0) -> "Pose":
        """Turn to face `point`, then close the gap, stopping `stop_short` away.

        Turn-then-drive even though a mecanum base can strafe: `approach` is a
        waypoint intent, and the conservative reading is the one a differential
        fallback could execute too. `limit <= 0` means "no cap, go all the way".
        """
        heading = self.bearing_to(point)
        gap = max(0.0, self.distance_to(point) - float(stop_short))
        travel = gap if limit <= 0.0 else min(gap, float(limit))
        rad = math.radians(heading)
        return Pose(self.x + travel * math.cos(rad),
                    self.y + travel * math.sin(rad), heading)

    def jittered(self, dx: float, dy: float, dtheta: float) -> "Pose":
        return Pose(self.x + dx, self.y + dy, wrap_deg(self.theta + dtheta))

    def to_dict(self) -> dict:
        return {"x": round(self.x, 4), "y": round(self.y, 4), "theta": round(self.theta, 2)}


@dataclass
class Landmark:
    name: str
    x: float
    y: float
    aliases: List[str] = field(default_factory=list)
    # Plain relative phrase ("by the window"), no coordinates. Only matters
    # for ambiguity ("the thing by the window"), never for collisions — the
    # grid and resolve() don't read it.
    where: Optional[str] = None

    def point(self) -> Point:
        return (self.x, self.y)

    def keys(self) -> List[str]:
        found = [normalise(self.name)]
        found.extend(normalise(alias) for alias in self.aliases)
        return [key for key in found if key]

    def to_dict(self) -> dict:
        return {"name": self.name, "x": self.x, "y": self.y,
                "aliases": list(self.aliases), "where": self.where}


@dataclass
class Obstacle:
    name: str
    polygon: Polygon
    where: Optional[str] = None

    def to_dict(self) -> dict:
        return {"name": self.name, "polygon": [list(p) for p in self.polygon],
                "where": self.where}


def _side(a: Point, b: Point, c: Point) -> float:
    """> 0 when c lies left of the directed line a -> b."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_cross(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Proper crossings only — touching endpoints and collinear overlap read as
    no crossing. The node table is hand-placed, so exact tangency is a
    coincidence to ignore, not a case to get right."""
    d1, d2 = _side(q1, q2, p1), _side(q1, q2, p2)
    d3, d4 = _side(p1, p2, q1), _side(p1, p2, q2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _point_in_polygon(point: Point, polygon: Polygon) -> bool:
    x, y = point
    inside = False
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


@dataclass
class RoomMap:
    name: str = "room"
    width_m: float = 5.0
    height_m: float = 4.0
    obstacles: List[Obstacle] = field(default_factory=list)
    landmarks: List[Landmark] = field(default_factory=list)
    start_x: float = 0.0
    start_y: float = 0.0
    start_theta: float = 0.0
    source: str = ""

    def __post_init__(self) -> None:
        self._index: Dict[str, Landmark] = {}
        for landmark in self.landmarks:
            for key in landmark.keys():
                existing = self._index.get(key)
                if existing is not None and existing is not landmark:
                    log.warning("[room %s] %r is claimed by both %r and %r — first wins",
                                self.name, key, existing.name, landmark.name)
                    continue
                self._index[key] = landmark

    def start_pose(self) -> Pose:
        return Pose(self.start_x, self.start_y, self.start_theta)

    def resolve(self, target) -> Tuple[Optional[Landmark], str]:
        """(landmark, how) — `how` is recorded in the trace so a run that only
        grounded by fuzzy match is distinguishable from one that grounded
        cleanly."""
        key = normalise(target)
        if not key:
            return None, "empty"
        if key in self._index:
            return self._index[key], "exact"
        contained = [k for k in self._index if k in key or key in k]
        if contained:
            return self._index[max(contained, key=len)], "substring"
        close = difflib.get_close_matches(key, list(self._index), n=1, cutoff=MATCH_CUTOFF)
        if close:
            return self._index[close[0]], "fuzzy"
        return None, "unresolved"

    def locate(self, target) -> Optional[Point]:
        landmark, _ = self.resolve(target)
        return landmark.point() if landmark is not None else None

    def obstruction(self, from_pose: Pose, to_pose: Pose) -> Optional[str]:
        """What the straight segment from -> to hits, or None if it is clear."""
        a, b = from_pose.point(), to_pose.point()
        if not (0.0 <= b[0] <= self.width_m and 0.0 <= b[1] <= self.height_m):
            return "the room boundary"
        for obstacle in self.obstacles:
            polygon = obstacle.polygon
            if len(polygon) < 3:
                continue
            if _point_in_polygon(b, polygon):
                return obstacle.name
            for index in range(len(polygon)):
                edge_a = polygon[index]
                edge_b = polygon[(index + 1) % len(polygon)]
                if _segments_cross(a, b, edge_a, edge_b):
                    return obstacle.name
        return None

    def collides(self, from_pose: Pose, to_pose: Pose) -> bool:
        return self.obstruction(from_pose, to_pose) is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "width_m": self.width_m,
            "height_m": self.height_m,
            "start": {"x": self.start_x, "y": self.start_y, "theta": self.start_theta},
            "obstacles": [o.to_dict() for o in self.obstacles],
            "landmarks": [l.to_dict() for l in self.landmarks],
            "source": self.source,
        }

    def digest_text(self) -> str:
        """A text digest shaped like Scene.digest() (scene.py), built straight
        from the hand-typed node table instead of a VLM reading video frames.

        Lets a run skip scene.py/vlm.inventory_frame entirely: the planner and
        the clarification loop then see exactly the same names
        VirtualRover.resolve() will later look up, so there is no video-vs-
        fixture mismatch left to cause a false "hallucination" reading.

        `where` strings are appended only when VIRTUAL_DIGEST_WHERE=1 and the
        landmark/obstacle actually has one — a fixture with no `where` fields
        produces byte-identical output regardless of the flag.
        """
        show_where = _flag("VIRTUAL_DIGEST_WHERE", False)
        parts = []
        for landmark in self.landmarks:
            label = landmark.name
            if landmark.aliases:
                label += " (also called: " + ", ".join(landmark.aliases) + ")"
            if show_where and landmark.where:
                label += " — " + landmark.where
            parts.append(label)
        if self.obstacles:
            def obstacle_label(o: Obstacle) -> str:
                return o.name + (" — " + o.where if show_where and o.where else "")
            parts.append("floor obstacles: " + ", ".join(obstacle_label(o) for o in self.obstacles))
        return f"View 1 ({self.name}): " + ("; ".join(parts) if parts else "nothing known")


def _polygon_of(entry: dict) -> Polygon:
    if "box" in entry:
        x0, y0, x1, y1 = (float(v) for v in entry["box"])
        left, right = min(x0, x1), max(x0, x1)
        bottom, top = min(y0, y1), max(y0, y1)
        return [(left, bottom), (right, bottom), (right, top), (left, top)]
    return [(float(p[0]), float(p[1])) for p in (entry.get("polygon") or [])]


def _validate(room: RoomMap) -> None:
    """Loud on a bad fixture. A landmark typed inside a table makes every plan
    come back "blocked", which reads as a planning failure when it is a typo."""
    def inside_room(x, y):
        return 0.0 <= x <= room.width_m and 0.0 <= y <= room.height_m

    if not inside_room(room.start_x, room.start_y):
        log.error("[room %s] start pose (%.2f, %.2f) is outside the room",
                  room.name, room.start_x, room.start_y)
    for obstacle in room.obstacles:
        if len(obstacle.polygon) < 3:
            log.error("[room %s] obstacle %r has %d point(s)",
                      room.name, obstacle.name, len(obstacle.polygon))
        if _point_in_polygon((room.start_x, room.start_y), obstacle.polygon):
            log.error("[room %s] start pose is inside %r", room.name, obstacle.name)
    for landmark in room.landmarks:
        if not inside_room(landmark.x, landmark.y):
            log.error("[room %s] landmark %r at (%.2f, %.2f) is outside the room",
                      room.name, landmark.name, landmark.x, landmark.y)
        for obstacle in room.obstacles:
            if _point_in_polygon(landmark.point(), obstacle.polygon):
                log.error("[room %s] landmark %r sits inside obstacle %r — it wants "
                          "the standing spot in front of the object, not its centre",
                          room.name, landmark.name, obstacle.name)

    _validate_footprint(room)


def _validate_footprint(room: RoomMap) -> None:
    """Every landmark's standing spot must be free on the inflated occupancy
    grid *and* reachable from the start pose — the same grid `approach` plans
    on, so a landmark that fails here would make every plan targeting it come
    back `blocked`/`no_path` at mission time. Logged, never auto-corrected:
    moving a hand-typed coordinate silently would hide exactly the kind of
    fixture mistake this check exists to catch."""
    # Local import: nav.py imports Point/Polygon/RoomMap from this module, so
    # importing nav at module load time here would be circular. By the time
    # this function actually runs (via load_room, after both modules have
    # finished importing) the cycle is moot.
    from nav import OccupancyGrid, plan_path

    clearance = footprint_clearance_m()
    grid = OccupancyGrid(room, clearance)
    start_point = room.start_pose().point()

    for landmark in room.landmarks:
        free = grid.free(landmark.x, landmark.y)
        if not free:
            blocker = grid.blocker(landmark.x, landmark.y)
            log.error(
                "[room %s] landmark %r's standing spot (%.2f, %.2f) is not free at "
                "%.2fm clearance (blocked by %s) — the rover footprint cannot stand there",
                room.name, landmark.name, landmark.x, landmark.y, clearance, blocker,
            )
            continue
        if plan_path(grid, start_point, landmark.point()) is None:
            log.error(
                "[room %s] landmark %r's standing spot (%.2f, %.2f) is free but not "
                "reachable from the start pose (%.2f, %.2f) at %.2fm clearance",
                room.name, landmark.name, landmark.x, landmark.y,
                start_point[0], start_point[1], clearance,
            )


def load_room(path) -> RoomMap:
    file = Path(path)
    if not file.is_absolute():
        file = BRAIN_DIR / file
    data = json.loads(file.read_text(encoding="utf-8"))
    start = data.get("start") or {}
    room = RoomMap(
        name=str(data.get("name") or file.stem),
        width_m=float(data.get("width_m", 5.0)),
        height_m=float(data.get("height_m", 4.0)),
        obstacles=[Obstacle(str(e.get("name") or "obstacle"), _polygon_of(e),
                            where=(str(e["where"]) if e.get("where") else None))
                   for e in (data.get("obstacles") or [])],
        landmarks=[Landmark(str(e["name"]), float(e["x"]), float(e["y"]),
                            [str(a) for a in (e.get("aliases") or [])],
                            where=(str(e["where"]) if e.get("where") else None))
                   for e in (data.get("landmarks") or [])],
        start_x=float(start.get("x", 0.0)),
        start_y=float(start.get("y", 0.0)),
        start_theta=float(start.get("theta", 0.0)),
        source=str(file),
    )
    _validate(room)
    log.info("[room %s] %.1f x %.1f m, %d landmark(s), %d obstacle(s) from %s",
             room.name, room.width_m, room.height_m,
             len(room.landmarks), len(room.obstacles), file.name)
    return room
