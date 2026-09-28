"""ON-015: sync Wall floor-plan structure and device pin coordinates from the
optional ``spatial_context`` (ha-spatial-context) integration into the graph.

Optional dependency: most installs won't have ha-spatial-context, so every
entry point checks the service first (:func:`spatial_context_available`)
and no-ops when it isn't there.

No ``Room`` node: spatial_context's "room" grouping duplicates Area by name
at the container level, while individual pins inside it don't reliably
share that Area - so it's a visual floor-plan bucket, not a trustworthy
area/room-membership signal. Pin coordinates attach directly to Floor
instead (`REL_PINNED_ON_FLOOR`).

No ``ADJACENT_TO`` either: the plugin exposes no room boundary polygon, so
room-to-room adjacency can't be derived. A straight-line/segment-crossing
query between two pinned entities and nearby walls is still possible from
what's stored here (`Wall.points_x/points_y` + each pin's `x/y`).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    LABEL_ENTITY,
    LABEL_FLOOR,
    LABEL_WALL,
    REL_ON_FLOOR,
    REL_PINNED_ON_FLOOR,
    SPATIAL_CONTEXT_DOMAIN,
    SPATIAL_CONTEXT_GET_MAP_SERVICE,
)
from .graph_builder import merge_node, merge_relationship
from .memgraph_client import MemgraphClient

_LOGGER = logging.getLogger(__name__)


def spatial_context_available(hass: HomeAssistant) -> bool:
    """Whether the optional spatial_context integration is installed and loaded."""
    return hass.services.has_service(SPATIAL_CONTEXT_DOMAIN, SPATIAL_CONTEXT_GET_MAP_SERVICE)


async def async_sync_spatial_layout(
    hass: HomeAssistant, client: MemgraphClient
) -> dict[str, int]:
    """Pull the current floor plan from spatial_context and MERGE it into the graph.

    Returns a count of walls written and device pins placed, all zero if
    spatial_context isn't installed.
    """
    if not spatial_context_available(hass):
        _LOGGER.debug(
            "spatial_context integration not found; skipping spatial layout sync"
        )
        return {"walls": 0, "pinned_on_floor": 0}

    response = await hass.services.async_call(
        SPATIAL_CONTEXT_DOMAIN,
        SPATIAL_CONTEXT_GET_MAP_SERVICE,
        {},
        blocking=True,
        return_response=True,
    )
    floors: list[dict[str, Any]] = (response or {}).get("floors", [])

    wall_count = 0
    pinned_count = 0
    for floor in floors:
        floor_id = floor.get("floor_id")
        if not floor_id:
            # spatial_context's floor_id matches HA's Floor registry id -
            # skip a floor it can't identify rather than guessing.
            continue

        for room in floor.get("rooms", []):
            # Only the pins are used - the room grouping itself isn't
            # modeled, see this module's docstring.
            for device in room.get("devices", []):
                entity_id = device.get("entity_id")
                if not entity_id:
                    continue
                await merge_relationship(
                    client,
                    LABEL_ENTITY,
                    entity_id,
                    REL_PINNED_ON_FLOOR,
                    LABEL_FLOOR,
                    floor_id,
                    # z: height above this floor's own floor level, already
                    # set per-device in the plugin.
                    properties={
                        "x": device.get("x_m"),
                        "y": device.get("y_m"),
                        "z": device.get("height_m"),
                    },
                )
                pinned_count += 1

        for wall in floor.get("walls", []):
            wall_id = wall.get("id")
            points_m = wall.get("points_m")
            if not wall_id or not points_m:
                continue
            # points_x/points_y: parallel flat float arrays, since Memgraph/
            # Neo4j properties can't be a nested list of [x, y] pairs.
            await merge_node(
                client,
                LABEL_WALL,
                wall_id,
                {
                    "material": wall.get("material"),
                    "thickness_cm": wall.get("thickness_cm"),
                    "attenuation_db": wall.get("attenuation_db"),
                    "points_x": [point[0] for point in points_m],
                    "points_y": [point[1] for point in points_m],
                },
            )
            await merge_relationship(
                client, LABEL_WALL, wall_id, REL_ON_FLOOR, LABEL_FLOOR, floor_id
            )
            wall_count += 1

    return {"walls": wall_count, "pinned_on_floor": pinned_count}
