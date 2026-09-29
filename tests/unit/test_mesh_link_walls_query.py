"""Tests for the mesh-link-with-wall-attenuation query (ON-019)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.ontology.const import OUTCOME_EMPTY, OUTCOME_OK
from custom_components.ontology.query_tools import mesh_link_walls

_LINK = {
    "from_device": "0x01",
    "from_name": "Switch-Hall",
    "to_device": "0x00",
    "to_name": "Coordinator",
    "lqi": 20,
    "depth": 1,
    "relationship": 1,
    "relationship_name": "child",
    "from_device_id": "dev-a",
    "to_device_id": "dev-b",
}


def _client(links, positions, walls):
    client = AsyncMock()
    client.run_query.side_effect = [
        [{"snapshot_id": "mesh-1", "scanned_at": "2026-09-30T00:00:00+00:00"}],
        positions,
        walls,
    ]
    client.run_query_limited.return_value = (links, False)
    return client


async def test_empty_when_no_snapshot() -> None:
    client = AsyncMock()
    client.run_query.return_value = []

    result = await mesh_link_walls(client)

    assert result["outcome"] == OUTCOME_EMPTY
    assert result["result"]["snapshot_id"] is None
    client.run_query_limited.assert_not_called()


async def test_counts_walls_between_pinned_devices_on_same_floor() -> None:
    client = _client(
        [_LINK],
        [
            {"device_id": "dev-a", "floor_id": "f1", "level": 1, "x": 0.0, "y": 0.0, "z": 0.0, "pins": 2},
            {"device_id": "dev-b", "floor_id": "f1", "x": 10.0, "y": 0.0, "pins": 1},
        ],
        [
            {
                "id": "w1",
                "floor_id": "f1",
                "points_x": [5.0, 5.0],
                "points_y": [-5.0, 5.0],
                "attenuation_db": 6.0,
            },
            {
                "id": "far",
                "floor_id": "f1",
                "points_x": [50.0, 50.0],
                "points_y": [-5.0, 5.0],
                "attenuation_db": 9.0,
            },
        ],
    )

    result = await mesh_link_walls(client)

    assert result["outcome"] == OUTCOME_OK
    link = result["result"]["links"][0]
    assert link["walls_crossed"] == 1
    assert link["wall_attenuation_db"] == 6.0
    assert link["distance_m"] == 10.0
    assert link["free_space_loss_db"] == 60.2
    assert link["expected_loss_db"] == 66.2
    assert link["same_floor"] is True
    assert link["relationship_name"] == "child"


async def test_cross_floor_links_count_slabs_and_use_3d_distance() -> None:
    client = _client(
        [_LINK],
        [
            {"device_id": "dev-a", "floor_id": "f1", "level": 1, "x": 0.0, "y": 0.0, "z": 1.0, "pins": 1},
            {"device_id": "dev-b", "floor_id": "f2", "level": 2, "x": 0.0, "y": 4.0, "z": 2.0, "pins": 1},
        ],
        [],
    )

    result = await mesh_link_walls(client, floor_height_m=3.0, slab_attenuation_db=12.0)
    link = result["result"]["links"][0]

    assert link["same_floor"] is False
    assert link["slabs_crossed"] == 1
    # dz = 1 floor * 3.0 m + 2.0 - 1.0 = 4.0; horizontal 4.0 -> hypot(4, 4) = 5.66.
    # The 45-degree path is 1/sin(45) = 1.414x longer through the slab than a
    # vertical one: 12 dB * 1.414 = 17.0 dB.
    assert link["distance_m"] == 5.66
    assert link["slab_attenuation_db"] == 17.0
    assert link["free_space_loss_db"] == 55.3
    assert link["expected_loss_db"] == 72.3
    assert link["walls_crossed"] is None
    assert link["wall_attenuation_db"] is None


async def test_cross_floor_without_levels_skips_slab_calculation() -> None:
    client = _client(
        [_LINK],
        [
            {"device_id": "dev-a", "floor_id": "f1", "level": None, "x": 0.0, "y": 0.0, "z": 0.0, "pins": 1},
            {"device_id": "dev-b", "floor_id": "f2", "level": 2, "x": 1.0, "y": 0.0, "z": 0.0, "pins": 1},
        ],
        [],
    )

    link = (await mesh_link_walls(client))["result"]["links"][0]

    assert link["same_floor"] is False
    assert link["slabs_crossed"] is None
    assert link["distance_m"] is None


async def test_unpinned_or_unresolved_devices_leave_wall_fields_null() -> None:
    unresolved = {**_LINK, "to_device_id": None}
    client = _client(
        [unresolved],
        [{"device_id": "dev-a", "floor_id": "f1", "x": 0.0, "y": 0.0, "pins": 1}],
        [],
    )

    link = (await mesh_link_walls(client))["result"]["links"][0]

    assert link["same_floor"] is None
    assert link["walls_crossed"] is None


async def test_truncated_result_adds_warning() -> None:
    client = AsyncMock()
    client.run_query.return_value = [{"snapshot_id": "mesh-1", "scanned_at": "x"}]
    client.run_query_limited.return_value = ([], True)

    result = await mesh_link_walls(client, limit=5)

    assert any("truncated to 5" in w for w in result["warnings"])


async def test_siblings_hidden_by_default_and_lqi_zero_flagged_unmeasured() -> None:
    client = _client([{**_LINK, "lqi": 0}], [], [])

    result = await mesh_link_walls(client)

    query, params, _limit = client.run_query_limited.call_args.args
    assert "$include_siblings OR l.relationship IS NULL OR l.relationship IN [0, 1]" in query
    assert params["include_siblings"] is False
    link = result["result"]["links"][0]
    assert link["lqi"] == 0
    assert link["lqi_measured"] is False


async def test_include_siblings_flag_is_passed_to_the_query() -> None:
    client = _client([], [], [])

    await mesh_link_walls(client, include_siblings=True)

    assert client.run_query_limited.call_args.args[1]["include_siblings"] is True
