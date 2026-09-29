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
            {"device_id": "dev-a", "floor_id": "f1", "x": 0.0, "y": 0.0, "pins": 2},
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
    assert link["same_floor"] is True
    assert link["relationship_name"] == "child"


async def test_cross_floor_links_skip_wall_calculation() -> None:
    client = _client(
        [_LINK],
        [
            {"device_id": "dev-a", "floor_id": "f1", "x": 0.0, "y": 0.0, "pins": 1},
            {"device_id": "dev-b", "floor_id": "f2", "x": 10.0, "y": 0.0, "pins": 1},
        ],
        [],
    )

    link = (await mesh_link_walls(client))["result"]["links"][0]

    assert link["same_floor"] is False
    assert link["walls_crossed"] is None
    assert link["wall_attenuation_db"] is None


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
