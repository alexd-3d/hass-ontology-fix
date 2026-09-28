"""Tests for the shared weak-Zigbee-mesh-link query (ON-018)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.ontology.const import OUTCOME_EMPTY, OUTCOME_OK
from custom_components.ontology.query_tools import weak_mesh_links


async def test_returns_empty_outcome_when_no_snapshot_recorded() -> None:
    client = AsyncMock()
    client.run_query.return_value = []

    result = await weak_mesh_links(client)

    assert result["outcome"] == OUTCOME_EMPTY
    assert result["result"]["snapshot_id"] is None
    assert result["result"]["links"] == []
    assert "no Zigbee mesh snapshot recorded yet" in result["warnings"]
    client.run_query_limited.assert_not_called()


async def test_returns_weakest_links_from_latest_snapshot_sorted_ascending() -> None:
    client = AsyncMock()
    client.run_query.return_value = [
        {"snapshot_id": "mesh-20260928T214733Z", "scanned_at": "2026-09-28T21:47:33+00:00"}
    ]
    client.run_query_limited.return_value = (
        [
            {
                "from_device": "0xa4c138e9d75b117e",
                "from_name": "Coordinator",
                "to_device": "0x00124b00258c6ca3",
                "to_name": "Switch-Hall",
                "lqi": 1,
                "depth": 2,
            },
        ],
        False,
    )

    result = await weak_mesh_links(client, max_lqi=50.0)

    assert result["outcome"] == OUTCOME_OK
    payload = result["result"]
    assert payload["snapshot_id"] == "mesh-20260928T214733Z"
    assert payload["max_lqi"] == 50.0
    assert payload["links"] == [
        {
            "from_device": "0xa4c138e9d75b117e",
            "from_name": "Coordinator",
            "to_device": "0x00124b00258c6ca3",
            "to_name": "Switch-Hall",
            "lqi": 1,
            "depth": 2,
        }
    ]
    query, params, limit = client.run_query_limited.call_args.args
    assert params["snapshot_id"] == "mesh-20260928T214733Z"
    assert params["max_lqi"] == 50.0
    assert "ORDER BY l.lqi ASC" in query


async def test_truncated_result_adds_warning() -> None:
    client = AsyncMock()
    client.run_query.return_value = [
        {"snapshot_id": "mesh-1", "scanned_at": "2026-09-28T21:47:33+00:00"}
    ]
    client.run_query_limited.return_value = ([], True)

    result = await weak_mesh_links(client, limit=5)

    assert any("truncated to 5" in warning for warning in result["warnings"])
