"""Occupancy grid + A* path planning for VirtualRover's `approach`.

Stdlib only. One grid per room (walls + every obstacle polygon, inflated by
the rover's clearance radius) is used for both planning and collision
checking, so the two can never disagree — see
Progress/spec-planner-profiles-and-virtual-sweep.md §1 ("Rover footprint").

Frame and units match room_map.py: metres, x right, y away from the start
wall. A grid cell's "value" is either None (free) or the name of whatever
blocks it — an obstacle's name, or the literal string "the room boundary" —
so both `approach`'s planner and `move`/`turn`'s literal sweep can report the
same blocker name for the same patch of floor.
"""

import heapq
import math
from typing import List, Optional, Tuple

from room_map import Point, Polygon, RoomMap

Cell = Tuple[int, int]

# 8-connected step costs, and the corner-cutting guard below, are what keep a
# diagonal move from grazing an inflated obstacle's corner at grid resolution.
_NEIGHBOURS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


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


def _polygon_distance(point: Point, polygon: Polygon) -> float:
    if len(polygon) < 3:
        return float("inf")
    if _point_in_polygon(point, polygon):
        return 0.0
    count = len(polygon)
    return min(
        _point_segment_distance(point, polygon[i], polygon[(i + 1) % count])
        for i in range(count)
    )


class OccupancyGrid:
    """Built once per VirtualRover (the room is static). Cell centres are
    sampled at (col + 0.5) * cell_m, (row + 0.5) * cell_m so free(x, y) and
    blocker(x, y) agree with plan_path's cell lookups exactly."""

    def __init__(self, room: RoomMap, clearance_m: float, cell_m: float = 0.02):
        self.room = room
        self.clearance_m = clearance_m
        self.cell_m = cell_m
        self.cols = max(1, int(math.ceil(room.width_m / cell_m)))
        self.rows = max(1, int(math.ceil(room.height_m / cell_m)))
        self._blocker: List[List[Optional[str]]] = self._build()

    def _blocker_at(self, x: float, y: float) -> Optional[str]:
        w, h, clearance = self.room.width_m, self.room.height_m, self.clearance_m
        if x < clearance or x > w - clearance or y < clearance or y > h - clearance:
            return "the room boundary"
        for obstacle in self.room.obstacles:
            if _polygon_distance((x, y), obstacle.polygon) <= clearance:
                return obstacle.name
        return None

    def _build(self) -> List[List[Optional[str]]]:
        grid: List[List[Optional[str]]] = []
        for row in range(self.rows):
            y = (row + 0.5) * self.cell_m
            grid.append([self._blocker_at((col + 0.5) * self.cell_m, y)
                        for col in range(self.cols)])
        return grid

    def cell_of(self, x: float, y: float) -> Cell:
        col = min(self.cols - 1, max(0, int(x / self.cell_m)))
        row = min(self.rows - 1, max(0, int(y / self.cell_m)))
        return row, col

    def point_of(self, cell: Cell) -> Point:
        row, col = cell
        return ((col + 0.5) * self.cell_m, (row + 0.5) * self.cell_m)

    def _in_room(self, x: float, y: float) -> bool:
        return 0.0 <= x <= self.room.width_m and 0.0 <= y <= self.room.height_m

    def free(self, x: float, y: float) -> bool:
        return self.blocker(x, y) is None

    def blocker(self, x: float, y: float) -> Optional[str]:
        if not self._in_room(x, y):
            return "the room boundary"
        row, col = self.cell_of(x, y)
        return self._blocker[row][col]

    def cell_blocked(self, cell: Cell) -> bool:
        row, col = cell
        return self._blocker[row][col] is not None


def _octile(a: Cell, b: Cell, cell_m: float) -> float:
    dr, dc = abs(a[0] - b[0]), abs(a[1] - b[1])
    return (max(dr, dc) + (math.sqrt(2.0) - 1.0) * min(dr, dc)) * cell_m


def _line_of_sight(grid: OccupancyGrid, a: Point, b: Point) -> bool:
    """Sampled at the grid's own resolution — the same resolution collision
    checks use, so a shortcut waypoint can't sneak past what sweep_segment
    would have caught."""
    dist = math.hypot(b[0] - a[0], b[1] - a[1])
    if dist <= 0.0:
        return grid.free(*a)
    steps = max(1, int(math.ceil(dist / grid.cell_m)))
    for i in range(steps + 1):
        t = i / steps
        if not grid.free(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t):
            return False
    return True


def _shortcut(grid: OccupancyGrid, points: List[Point]) -> List[Point]:
    """Greedy line-of-sight shortcutting: from each kept point, jump to the
    farthest later point still reachable in a straight line."""
    if len(points) <= 2:
        return points
    result = [points[0]]
    i, last = 0, len(points) - 1
    while i < last:
        j = last
        while j > i + 1 and not _line_of_sight(grid, points[i], points[j]):
            j -= 1
        result.append(points[j])
        i = j
    return result


def plan_path(grid: OccupancyGrid, start_xy: Point, goal_xy: Point) -> Optional[List[Point]]:
    """A*, 8-connected, octile heuristic, then line-of-sight shortcutting.

    Returns None (no_path) if the goal cell is blocked or unreachable. The
    start cell is walked from regardless of its own occupancy, so a start
    pose sitting exactly on a clearance boundary (a rounding artefact, not a
    real collision) doesn't make every plan fail immediately.
    """
    start_cell = grid.cell_of(*start_xy)
    goal_cell = grid.cell_of(*goal_xy)
    if grid.cell_blocked(goal_cell):
        return None
    if start_cell == goal_cell:
        return [start_xy, goal_xy]

    came_from = {}
    g_score = {start_cell: 0.0}
    open_heap: List[Tuple[float, Cell]] = [(_octile(start_cell, goal_cell, grid.cell_m), start_cell)]
    closed = set()

    while open_heap:
        _, current = heapq.heappop(open_heap)
        if current in closed:
            continue
        if current == goal_cell:
            cells = [current]
            while current in came_from:
                current = came_from[current]
                cells.append(current)
            cells.reverse()
            points = [grid.point_of(c) for c in cells]
            points[0] = start_xy
            points[-1] = goal_xy
            return _shortcut(grid, points)
        closed.add(current)

        for dr, dc in _NEIGHBOURS:
            nr, nc = current[0] + dr, current[1] + dc
            if not (0 <= nr < grid.rows and 0 <= nc < grid.cols):
                continue
            neighbour = (nr, nc)
            if neighbour in closed or grid.cell_blocked(neighbour):
                continue
            if dr != 0 and dc != 0:
                # No cutting the corner between two blocked orthogonal cells —
                # at 2cm resolution that would graze an inflated obstacle.
                if grid.cell_blocked((current[0] + dr, current[1])) or \
                   grid.cell_blocked((current[0], current[1] + dc)):
                    continue
            step_cost = grid.cell_m * (math.sqrt(2.0) if dr and dc else 1.0)
            tentative = g_score[current] + step_cost
            if tentative < g_score.get(neighbour, float("inf")):
                came_from[neighbour] = current
                g_score[neighbour] = tentative
                priority = tentative + _octile(neighbour, goal_cell, grid.cell_m)
                heapq.heappush(open_heap, (priority, neighbour))

    return None


def path_length(points: List[Point]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def sweep_segment(grid: OccupancyGrid, a: Point, b: Point,
                  step_m: float = 0.02) -> Tuple[Point, Optional[str]]:
    """Walk the straight segment a -> b at step_m spacing. Returns
    (last_free_point, blocker_or_None): the blocker is the obstacle name or
    "the room boundary", matching room_map.obstruction's naming."""
    dist = math.hypot(b[0] - a[0], b[1] - a[1])
    if dist <= 0.0:
        blocker = grid.blocker(*a)
        return (a, blocker) if blocker is not None else (a, None)

    steps = max(1, int(math.ceil(dist / step_m)))
    last_free = a
    for i in range(1, steps + 1):
        t = min(1.0, i / steps)
        point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        blocker = grid.blocker(*point)
        if blocker is not None:
            return last_free, blocker
        last_free = point
    return last_free, None
