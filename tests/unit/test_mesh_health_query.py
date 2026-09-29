"""Tests for the one-shot Zigbee mesh health summary (ON-021)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.ontology.const import OUTCOME_EMPTY, OUTCOME_OK
from custom_components.ontology.query_tools import mesh_health

_SNAPSHOT = [{"snapshot_id": "mesh-1", "scanned_at": "2026-09-30T00:00:00+00:00"}]


def _link(from_name, to_name, lqi, *, a="dev-a", b="dev-b", relationship=2):
    return {
        "from_device": f"0x{from_name}",
        "from_name": from_name,
        "to_device": f"0x{to_name}",
        "to_name": to_name,
        "lqi": lqi,
        "depth": 1,
        "relationship": relationship,
        "relationship_name": "sibling",
        "from_device_id": a,
        "to_device_id": b,
    }


def _client(*, parents, weak_devices, coordinator, links, positions=(), walls=()):
    client = AsyncMock()

    async def run_query(query, params):
        if "RETURN s.ha_id AS snapshot_id" in query:
            return _SNAPSHOT
        if "l.relationship = 0" in query:
            return parents
        if "UNWIND" in query:
            return weak_devices
        if "l.to_name = 'Coordinator'" in query:
            return coordinator
        if "PINNED_ON_FLOOR" in query:
            return list(positions)
        if ":Wall" in query:
            return list(walls)
        raise AssertionError(f"unexpected query: {query}")

    client.run_query.side_effect = run_query
    client.run_query_limited.return_value = (links, False)
    return client


async def test_empty_when_no_snapshot() -> None:
    client = AsyncMock()
    client.run_query.return_value = []

    result = await mesh_health(client)

    assert result["outcome"] == OUTCOME_EMPTY
    assert "no Zigbee mesh snapshot recorded yet" in result["warnings"]


async def test_flags_parents_whose_children_are_all_unmeasured() -> None:
    client = _client(
        parents=[
            {"device": "Coordinator", "children": 16, "unmeasured": 0},
            {"device": "Dead-Switch", "children": 9, "unmeasured": 9},
            {"device": "Half", "children": 3, "unmeasured": 1},
        ],
        weak_devices=[],
        coordinator=[],
        links=[],
    )

    result = await mesh_health(client)

    assert result["outcome"] == OUTCOME_OK
    payload = result["result"]
    assert payload["summary"]["parents"] == 3
    assert payload["suspect_parents"] == [
        {"device": "Dead-Switch", "children": 9, "unmeasured_child_links": 9}
    ]
    assert payload["coordinator"] is None


async def test_reports_coordinator_and_weak_devices() -> None:
    client = _client(
        parents=[],
        weak_devices=[{"device": "Lonely", "best_lqi": 12, "measured_links": 2}],
        coordinator=[{"links": 52, "avg_lqi": 84.46, "min_lqi": 1, "weak_links": 11}],
        links=[],
    )

    payload = (await mesh_health(client, weak_lqi=40.0))["result"]

    assert payload["weak_devices"] == [{"device": "Lonely", "best_lqi": 12, "measured_links": 2}]
    assert payload["coordinator"] == {
        "links": 52,
        "avg_lqi": 84.5,
        "min_lqi": 1,
        "weak_links": 11,
    }
    assert payload["summary"]["weak_devices"] == 1


async def test_separates_unexplained_weak_links_from_geometry_explained_ones() -> None:
    clear_path = _link("Near-A", "Near-B", 10, a="dev-a", b="dev-b")
    behind_slab = _link("Up-A", "Down-B", 8, a="dev-c", b="dev-d")
    client = _client(
        parents=[],
        weak_devices=[],
        coordinator=[],
        links=[clear_path, behind_slab],
        positions=[
            {"device_id": "dev-a", "floor_id": "f1", "level": 1, "x": 0.0, "y": 0.0, "z": 1.0, "pins": 1},
            {"device_id": "dev-b", "floor_id": "f1", "level": 1, "x": 3.0, "y": 0.0, "z": 1.0, "pins": 1},
            {"device_id": "dev-c", "floor_id": "f1", "level": 1, "x": 0.0, "y": 0.0, "z": 1.0, "pins": 1},
            {"device_id": "dev-d", "floor_id": "f2", "level": 2, "x": 8.0, "y": 0.0, "z": 1.0, "pins": 1},
        ],
    )

    payload = (await mesh_health(client, slab_attenuation_db=30.0))["result"]

    assert payload["summary"]["weak_links"] == 2
    assert payload["summary"]["unexplained_weak_links"] == 1
    assert payload["unexplained_weak_links"][0]["from_name"] == "Near-A"


async def test_weak_links_without_geometry_are_counted_in_a_warning() -> None:
    client = _client(
        parents=[],
        weak_devices=[],
        coordinator=[],
        links=[_link("X", "Y", 5, a=None, b=None)],
    )

    result = await mesh_health(client)

    assert result["result"]["summary"]["unexplained_weak_links"] == 0
    assert any("could not be checked against the floor plan" in w for w in result["warnings"])
