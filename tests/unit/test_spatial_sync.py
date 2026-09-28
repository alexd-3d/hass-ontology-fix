"""Unit tests for `spatial_sync.py` (ON-015).

Covers: the optional-dependency guard (no crash/no-op when spatial_context
isn't installed), and that a real `get_map`-shaped payload gets merged into
Wall nodes + `PINNED_ON_FLOOR` relationships with no Room node anywhere.
"""

from __future__ import annotations

from homeassistant.core import SupportsResponse

from custom_components.ontology import spatial_sync
from custom_components.ontology.const import (
    LABEL_ENTITY,
    LABEL_FLOOR,
    LABEL_WALL,
    REL_ON_FLOOR,
    REL_PINNED_ON_FLOOR,
)

_SAMPLE_GET_MAP_RESPONSE = {
    "floors": [
        {
            "floor_id": "drugii",
            "name": "Другий",
            "rooms": [
                {
                    "id": "room-1",
                    "name": "Кабінет",
                    "devices": [
                        {
                            "entity_id": "light.office",
                            "x_m": 4.3,
                            "y_m": 12.9,
                            "height_m": 2.8,
                        },
                        # A device with no entity_id (pinned but not linked to
                        # anything HA knows about) must be skipped, not crash.
                        {"device_id": "orphan-pin", "x_m": 1.0, "y_m": 1.0},
                    ],
                }
            ],
            "walls": [
                {
                    "id": "wall-1",
                    "material": "ceramic_poroton_block",
                    "thickness_cm": 25,
                    "attenuation_db": 10.5,
                    "points_m": [[0.9, 1.4], [7.5, 1.4]],
                },
                # A wall with no points_m must be skipped, not crash.
                {"id": "wall-no-geometry", "material": "aerated_concrete_block"},
            ],
        },
        # A floor spatial_context can't identify (no floor_id) is skipped.
        {"name": "unlinked", "rooms": [], "walls": []},
    ]
}


def _register_fake_spatial_context_service(hass, response: dict) -> None:
    async def _handle_get_map(call):
        return response

    hass.services.async_register(
        "spatial_context", "get_map", _handle_get_map, supports_response=SupportsResponse.ONLY
    )


async def test_spatial_context_available_false_when_service_missing(hass) -> None:
    assert spatial_sync.spatial_context_available(hass) is False


async def test_spatial_context_available_true_when_service_registered(hass) -> None:
    _register_fake_spatial_context_service(hass, {"floors": []})

    assert spatial_sync.spatial_context_available(hass) is True


async def test_sync_spatial_layout_noops_when_spatial_context_missing(
    hass, mock_memgraph_client
) -> None:
    result = await spatial_sync.async_sync_spatial_layout(hass, mock_memgraph_client)

    assert result == {"walls": 0, "pinned_on_floor": 0}
    mock_memgraph_client.run_query_with_retry.assert_not_called()


async def test_sync_spatial_layout_merges_walls_and_pins_no_room(
    hass, mock_memgraph_client
) -> None:
    _register_fake_spatial_context_service(hass, _SAMPLE_GET_MAP_RESPONSE)

    result = await spatial_sync.async_sync_spatial_layout(hass, mock_memgraph_client)

    assert result == {"walls": 1, "pinned_on_floor": 1}

    calls = mock_memgraph_client.run_query_with_retry.call_args_list
    queries_and_params = [call.args for call in calls]

    # Wall node merged with flat parallel coordinate arrays, not nested lists.
    wall_call = next(q for q, _p in queries_and_params if f"MERGE (n:{LABEL_WALL}" in q)
    wall_params = next(p for q, p in queries_and_params if q == wall_call)
    assert wall_params["ha_id"] == "wall-1"
    assert wall_params["properties"]["points_x"] == [0.9, 7.5]
    assert wall_params["properties"]["points_y"] == [1.4, 1.4]

    # Wall -> Floor via ON_FLOOR (reusing the same relationship Area already uses).
    assert any(
        f"MERGE (a)-[r:{REL_ON_FLOOR}]->(b)" in q
        and p["from_ha_id"] == "wall-1"
        and p["to_ha_id"] == "drugii"
        for q, p in queries_and_params
    )

    # Entity -> Floor via PINNED_ON_FLOOR, carrying x/y/z, no Room anywhere.
    pin_call = next(
        (q, p)
        for q, p in queries_and_params
        if f"[r:{REL_PINNED_ON_FLOOR}]" in q and p.get("from_ha_id") == "light.office"
    )
    _pin_query, pin_params = pin_call
    assert pin_params["to_ha_id"] == "drugii"
    assert pin_params["properties"] == {"x": 4.3, "y": 12.9, "z": 2.8}

    assert not any("Room" in q for q, _p in queries_and_params)
    assert not any(p.get("from_ha_id") == "orphan-pin" for _q, p in queries_and_params)
