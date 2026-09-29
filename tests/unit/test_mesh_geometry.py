"""Tests for the wall-crossing geometry helpers (ON-019)."""

from __future__ import annotations

from custom_components.ontology.mesh_geometry import (
    MAX_OBLIQUE_FACTOR,
    distance_m,
    free_space_loss_db,
    segments_intersect,
    slab_factor,
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


def test_slanted_crossing_scales_wall_attenuation() -> None:
    # 45 degrees through a vertical wall: path through it is 1/cos(45) = 1.414x.
    _crossed, total = wall_crossings((0, 0), (10, 10), [_VERTICAL_WALL])
    assert round(total, 2) == 8.49


def test_slant_factor_is_capped_for_near_parallel_paths() -> None:
    _crossed, total = wall_crossings((5.0, -9.0), (5.1, 9.0), [_VERTICAL_WALL])
    assert total == 6.0 * MAX_OBLIQUE_FACTOR


def test_slab_factor_vertical_and_diagonal() -> None:
    assert slab_factor(4.0, 4.0) == 1.0
    assert round(slab_factor(5.657, 4.0), 2) == 1.41
    assert slab_factor(10.0, 0.5) == MAX_OBLIQUE_FACTOR


def test_free_space_loss_grows_with_distance_and_clamps_below_1m() -> None:
    assert round(free_space_loss_db(1.0), 1) == 40.2
    assert round(free_space_loss_db(10.0), 1) == 60.2
    assert free_space_loss_db(0.3) == free_space_loss_db(1.0)
