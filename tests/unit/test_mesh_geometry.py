"""Tests for the wall-crossing geometry helpers (ON-019)."""

from __future__ import annotations

from custom_components.ontology.mesh_geometry import (
    distance_m,
    segments_intersect,
    wall_crossings,
)

_VERTICAL_WALL = {
    "id": "w1",
    "points_x": [5.0, 5.0],
    "points_y": [-10.0, 10.0],
    "attenuation_db": 6.0,
}


def test_segments_intersect_crossing_and_disjoint() -> None:
    assert segments_intersect((0, 0), (10, 0), (5, -1), (5, 1))
    assert not segments_intersect((0, 0), (4, 0), (5, -1), (5, 1))


def test_parallel_and_collinear_segments() -> None:
    assert not segments_intersect((0, 0), (10, 0), (0, 1), (10, 1))
    assert segments_intersect((0, 0), (10, 0), (5, 0), (15, 0))
    assert not segments_intersect((0, 0), (4, 0), (5, 0), (15, 0))


def test_wall_crossings_sums_attenuation_once_per_wall() -> None:
    # A polyline wall that the line crosses twice still counts once.
    zigzag = {
        "id": "w2",
        "points_x": [2.0, 2.0, 3.0, 3.0],
        "points_y": [-5.0, 5.0, 5.0, -5.0],
        "attenuation_db": 4.0,
    }
    crossed, total = wall_crossings((0, 0), (10, 0), [_VERTICAL_WALL, zigzag])

    assert crossed == ["w1", "w2"]
    assert total == 10.0


def test_wall_crossings_none_and_missing_attenuation() -> None:
    assert wall_crossings((0, 0), (4, 0), [_VERTICAL_WALL]) == ([], 0.0)
    bare = {"id": "w3", "points_x": [1.0, 1.0], "points_y": [-1.0, 1.0], "attenuation_db": None}
    assert wall_crossings((0, 0), (2, 0), [bare]) == (["w3"], 0.0)


def test_distance() -> None:
    assert distance_m((0, 0), (3, 4)) == 5.0
