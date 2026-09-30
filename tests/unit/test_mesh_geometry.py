"""Tests for the wall-crossing geometry helpers (ON-019)."""

from __future__ import annotations

from custom_components.ontology.mesh_geometry import (
    MAX_OBLIQUE_FACTOR,
    OPENING_ATTENUATION_DB,
    distance_m,
    free_space_loss_db,
    segments_intersect,
    slab_factor,
    wall_crossing_details,
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


def _wall_with_openings(*openings: tuple[str, float, float, float]) -> dict:
    """The vertical 6 dB wall at x=5 with (type, centre_x, centre_y, width) openings."""
    return {
        **_VERTICAL_WALL,
        "opening_types": [o[0] for o in openings],
        "opening_x": [o[1] for o in openings],
        "opening_y": [o[2] for o in openings],
        "opening_width": [o[3] for o in openings],
    }


def test_opening_constants_are_the_agreed_door_and_window_values() -> None:
    assert OPENING_ATTENUATION_DB == {"door": 3.0, "window": 2.0}


def test_path_through_a_door_uses_door_loss_instead_of_wall_loss() -> None:
    wall = _wall_with_openings(("door", 5.0, 0.0, 1.0))
    (detail,) = wall_crossing_details((0, 0), (10, 0), [wall])
    assert detail == {"id": "w1", "attenuation_db": 3.0, "opening": "door"}
    assert wall_crossings((0, 0), (10, 0), [wall]) == (["w1"], 3.0)


def test_path_through_a_window_uses_window_loss() -> None:
    wall = _wall_with_openings(("window", 5.0, 2.0, 2.0))
    (detail,) = wall_crossing_details((0, 2), (10, 2), [wall])
    assert detail["attenuation_db"] == 2.0
    assert detail["opening"] == "window"


def test_path_beside_the_opening_still_pays_full_wall_loss() -> None:
    wall = _wall_with_openings(("door", 5.0, 0.0, 1.0))
    # Crosses the wall at y=3, well outside the 1 m door centred at y=0.
    (detail,) = wall_crossing_details((0, 3), (10, 3), [wall])
    assert detail == {"id": "w1", "attenuation_db": 6.0, "opening": None}


def test_opening_edge_is_inclusive_and_centre_based() -> None:
    wall = _wall_with_openings(("door", 5.0, 0.0, 2.0))
    assert wall_crossing_details((0, 1), (10, 1), [wall])[0]["opening"] == "door"
    assert wall_crossing_details((0, 1.01), (10, 1.01), [wall])[0]["opening"] is None


def test_opening_loss_is_scaled_by_slant_like_a_wall() -> None:
    wall = _wall_with_openings(("door", 5.0, 0.0, 4.0))
    (detail,) = wall_crossing_details((0, -5), (10, 5), [wall])  # 45 degrees
    assert detail["opening"] == "door"
    assert abs(detail["attenuation_db"] - 3.0 * (2**0.5)) < 1e-9


def test_unknown_opening_type_and_ragged_arrays_are_ignored() -> None:
    wall = {
        **_VERTICAL_WALL,
        "opening_types": ["gate", "door"],
        "opening_x": [5.0],
        "opening_y": [0.0],
        "opening_width": [2.0],
    }
    assert wall_crossing_details((0, 0), (10, 0), [wall])[0]["opening"] is None


def test_walls_from_older_syncs_without_openings_still_work() -> None:
    assert wall_crossing_details((0, 0), (10, 0), [_VERTICAL_WALL])[0]["opening"] is None
