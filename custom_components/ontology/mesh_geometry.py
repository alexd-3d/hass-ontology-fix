"""ON-019: pure geometry helpers for wall crossings between two floor-plan points."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

_EPSILON = 1e-9


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


def wall_crossings(
    p: tuple[float, float],
    q: tuple[float, float],
    walls: Iterable[dict[str, Any]],
) -> tuple[list[str], float]:
    """Return (crossed wall ids, summed attenuation in dB) for the line p-q.

    Each wall is ``{"id", "points_x", "points_y", "attenuation_db"}`` with a
    polyline geometry; a wall counts once however many of its segments the
    line touches. A wall without an attenuation value counts as 0 dB.
    """
    crossed: list[str] = []
    total_db = 0.0
    for wall in walls:
        xs: Sequence[float] = wall.get("points_x") or []
        ys: Sequence[float] = wall.get("points_y") or []
        points = list(zip(xs, ys, strict=False))
        hit = any(
            segments_intersect(p, q, points[i], points[i + 1]) for i in range(len(points) - 1)
        )
        if hit:
            crossed.append(wall["id"])
            total_db += float(wall.get("attenuation_db") or 0.0)
    return crossed, total_db


def distance_m(p: Sequence[float], q: Sequence[float]) -> float:
    """Straight-line distance between two 2D or 3D points."""
    return math.dist(p, q)
