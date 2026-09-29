"""ON-019: pure geometry helpers for wall crossings between two floor-plan points."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

_EPSILON = 1e-9
# A near-parallel path would give an unbounded 1/cos factor; the path through
# a real wall is never that long, so cap the slant multiplier.
MAX_OBLIQUE_FACTOR = 3.0
# Free-space loss at 1 m for 2.4 GHz (Zigbee): 20*log10(2440 MHz) - 27.55.
_FSPL_1M_2_4GHZ_DB = 40.2


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def segments_intersect(
    p: tuple[float, float],
    q: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> bool:
    """Whether segment p-q properly or touchingly crosses segment a-b."""
    rx, ry = q[0] - p[0], q[1] - p[1]
    sx, sy = b[0] - a[0], b[1] - a[1]
    denom = _cross(rx, ry, sx, sy)
    ap_x, ap_y = a[0] - p[0], a[1] - p[1]
    if abs(denom) < _EPSILON:
        # Parallel: only "crossing" if collinear and overlapping.
        if abs(_cross(ap_x, ap_y, rx, ry)) > _EPSILON:
            return False
        length_sq = rx * rx + ry * ry
        if length_sq < _EPSILON:
            return False
        t0 = (ap_x * rx + ap_y * ry) / length_sq
        t1 = t0 + (sx * rx + sy * ry) / length_sq
        low, high = sorted((t0, t1))
        return high >= 0 and low <= 1
    t = _cross(ap_x, ap_y, sx, sy) / denom
    u = _cross(ap_x, ap_y, rx, ry) / denom
    return 0 <= t <= 1 and 0 <= u <= 1


def oblique_factor(cos_incidence: float) -> float:
    """Path-length multiplier for a slab crossed at an angle: 1/cos, capped."""
    if cos_incidence < 1.0 / MAX_OBLIQUE_FACTOR:
        return MAX_OBLIQUE_FACTOR
    return 1.0 / cos_incidence


def _segment_factor(
    p: tuple[float, float],
    q: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """Slant multiplier of line p-q through the wall segment a-b (1.0 = square-on)."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    wx, wy = b[0] - a[0], b[1] - a[1]
    line_len = math.hypot(dx, dy)
    wall_len = math.hypot(wx, wy)
    if line_len < _EPSILON or wall_len < _EPSILON:
        return 1.0
    # |cos| of the angle to the wall normal == |sin| of the angle to the wall.
    cos_incidence = abs(_cross(dx, dy, wx, wy)) / (line_len * wall_len)
    return oblique_factor(cos_incidence)


def wall_crossings(
    p: tuple[float, float],
    q: tuple[float, float],
    walls: Iterable[dict[str, Any]],
) -> tuple[list[str], float]:
    """Return (crossed wall ids, summed attenuation in dB) for the line p-q.

    Each wall is ``{"id", "points_x", "points_y", "attenuation_db"}`` with a
    polyline geometry; a wall counts once however many of its segments the
    line touches. Its `attenuation_db` is the loss for a square-on pass and is
    scaled up by the slant of the line through the first segment it hits. A
    wall without an attenuation value counts as 0 dB.
    """
    crossed: list[str] = []
    total_db = 0.0
    for wall in walls:
        xs: Sequence[float] = wall.get("points_x") or []
        ys: Sequence[float] = wall.get("points_y") or []
        points = list(zip(xs, ys, strict=False))
        for i in range(len(points) - 1):
            if segments_intersect(p, q, points[i], points[i + 1]):
                crossed.append(wall["id"])
                factor = _segment_factor(p, q, points[i], points[i + 1])
                total_db += float(wall.get("attenuation_db") or 0.0) * factor
                break
    return crossed, total_db


def slab_factor(distance: float, vertical: float) -> float:
    """Slant multiplier for a horizontal slab crossed on a path of given 3D length."""
    if distance < _EPSILON:
        return 1.0
    return oblique_factor(abs(vertical) / distance)


def free_space_loss_db(distance: float) -> float:
    """Free-space path loss at 2.4 GHz; distances under 1 m are clamped to 1 m."""
    return _FSPL_1M_2_4GHZ_DB + 20.0 * math.log10(max(distance, 1.0))


def distance_m(p: Sequence[float], q: Sequence[float]) -> float:
    """Straight-line distance between two 2D or 3D points."""
    return math.dist(p, q)
