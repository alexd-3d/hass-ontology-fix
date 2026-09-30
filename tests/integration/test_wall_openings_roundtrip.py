"""Integration test: wall openings survive a real Memgraph round trip (ON-027)."""

from __future__ import annotations

from homeassistant.core import SupportsResponse

from custom_components.ontology import spatial_sync
from custom_components.ontology.memgraph_client import MemgraphClient

_MAP = {
    "floors": [
        {
            "floor_id": "f1",
            "rooms": [],
            "walls": [
                {
                    "id": "wall-1",
                    "material": "ceramic_poroton_block",
                    "thickness_cm": 25,
                    "attenuation_db": 10.5,
                    "points_m": [[0.9, 1.4], [7.5, 1.4]],
                    "openings": [
                        {"type": "window", "x_m": 2.5, "y_m": 1.4, "width_m": 1.9},
                        {"type": "door", "x_m": 5.0, "y_m": 1.4, "width_m": 0.8},
                    ],
                }
            ],
        }
    ]
}


async def test_wall_openings_round_trip_and_clear_when_removed(
    hass, memgraph_client: MemgraphClient
) -> None:
    async def _get_map(call):
        return _MAP

    hass.services.async_register(
        "spatial_context", "get_map", _get_map, supports_response=SupportsResponse.ONLY
    )
    query = (
        "MATCH (w:Wall {ha_id: 'wall-1'}) RETURN w.opening_types AS t, "
        "w.opening_x AS x, w.opening_width AS width"
    )

    await spatial_sync.async_sync_spatial_layout(hass, memgraph_client)
    row = (await memgraph_client.run_query(query))[0]
    assert row["t"] == ["window", "door"]
    assert row["x"] == [2.5, 5.0]
    assert row["width"] == [1.9, 0.8]

    _MAP["floors"][0]["walls"][0]["openings"] = []
    try:
        await spatial_sync.async_sync_spatial_layout(hass, memgraph_client)
    finally:
        _MAP["floors"][0]["walls"][0]["openings"] = [
            {"type": "window", "x_m": 2.5, "y_m": 1.4, "width_m": 1.9},
            {"type": "door", "x_m": 5.0, "y_m": 1.4, "width_m": 0.8},
        ]
    row = (await memgraph_client.run_query(query))[0]
    assert row["t"] == []
    assert row["x"] == []
