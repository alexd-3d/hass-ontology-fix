"""ON-016: nightly Zigbee mesh snapshot via direct MQTT to Zigbee2MQTT.

Bypasses the optional spatial_context integration - its `get_map` response
carries no mesh/link data at all. Optional dependency: no-ops if the `mqtt`
integration isn't set up.

Z2M's `bridge/response/networkmap` "raw" payload shape here follows
Zigbee2MQTT's documented format (`data.value.{nodes,links}`) but isn't
verified against a live capture. Parsing is defensive - an unexpected shape
logs a warning and yields an empty snapshot rather than crashing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import (
    LABEL_DEVICE,
    LABEL_MESH_LINK,
    LABEL_MESH_SNAPSHOT,
    REL_FROM_DEVICE,
    REL_HAS_LINK,
    REL_TO_DEVICE,
    ZIGBEE2MQTT_NETWORKMAP_REQUEST_TOPIC_SUFFIX,
    ZIGBEE2MQTT_NETWORKMAP_RESPONSE_TOPIC_SUFFIX,
)
from .graph_builder import merge_node, merge_relationship
from .memgraph_client import MemgraphClient

_LOGGER = logging.getLogger(__name__)

MQTT_DOMAIN = "mqtt"

# Zigbee neighbor-table relationship codes (Zigbee spec, as passed through by
# Zigbee2MQTT's raw networkmap `relationship` field).
_RELATIONSHIP_NAMES = {0: "parent", 1: "child", 2: "sibling", 3: "none", 4: "previous_child"}
_Z2M_IDENTIFIER_PREFIXES = ("zigbee2mqtt_bridge_", "zigbee2mqtt_")


def _ieee_to_device_ids(hass: HomeAssistant) -> dict[str, str]:
    """Map Zigbee IEEE address -> HA device id via the MQTT discovery identifiers."""
    mapping: dict[str, str] = {}
    for device in dr.async_get(hass).devices.values():
        for domain, identifier in device.identifiers:
            if domain != MQTT_DOMAIN or not isinstance(identifier, str):
                continue
            for prefix in _Z2M_IDENTIFIER_PREFIXES:
                if identifier.startswith(prefix):
                    mapping[identifier[len(prefix) :].lower()] = device.id
                    break
    return mapping


def mqtt_available(hass: HomeAssistant) -> bool:
    """Whether the mqtt integration is set up."""
    return hass.services.has_service(MQTT_DOMAIN, "publish")


async def _async_request_networkmap(
    hass: HomeAssistant, base_topic: str, response_timeout_seconds: float
) -> dict[str, Any] | None:
    """Publish a networkmap request and wait for the one matching response."""
    request_topic = f"{base_topic}/{ZIGBEE2MQTT_NETWORKMAP_REQUEST_TOPIC_SUFFIX}"
    response_topic = f"{base_topic}/{ZIGBEE2MQTT_NETWORKMAP_RESPONSE_TOPIC_SUFFIX}"

    loop = asyncio.get_running_loop()
    response_future: asyncio.Future[dict[str, Any]] = loop.create_future()

    @callback
    def _on_message(msg: Any) -> None:
        if response_future.done():
            return
        try:
            response_future.set_result(json.loads(msg.payload))
        except (TypeError, ValueError) as err:
            response_future.set_exception(err)

    unsubscribe = await mqtt.async_subscribe(hass, response_topic, _on_message)
    try:
        await mqtt.async_publish(hass, request_topic, "raw")
        return await asyncio.wait_for(response_future, timeout=response_timeout_seconds)
    except (TimeoutError, ValueError) as err:
        _LOGGER.warning("Zigbee2MQTT networkmap request failed: %s", err)
        return None
    finally:
        unsubscribe()


def _extract_links(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Best-effort extraction of links + an ieeeAddr->friendlyName map."""
    value = ((payload or {}).get("data") or {}).get("value")
    if not isinstance(value, dict):
        _LOGGER.warning("Unexpected Zigbee2MQTT networkmap payload shape: %r", payload)
        return [], {}
    nodes = value.get("nodes") or []
    links = value.get("links") or []
    names = {
        node["ieeeAddr"]: node.get("friendlyName") or node["ieeeAddr"]
        for node in nodes
        if node.get("ieeeAddr")
    }
    return links, names


async def async_scan_zigbee_mesh(
    hass: HomeAssistant,
    client: MemgraphClient,
    *,
    base_topic: str,
    retention_days: int,
    response_timeout_seconds: float,
) -> dict[str, int]:
    """Scan the live Zigbee mesh and write one MeshSnapshot to the graph."""
    if not mqtt_available(hass):
        _LOGGER.debug("mqtt integration not found; skipping Zigbee mesh scan")
        return {"links": 0}

    payload = await _async_request_networkmap(hass, base_topic, response_timeout_seconds)
    if payload is None:
        return {"links": 0}

    links, names = _extract_links(payload)

    now = datetime.now(UTC)
    snapshot_id = now.strftime("mesh-%Y%m%dT%H%M%SZ")
    await merge_node(
        client,
        LABEL_MESH_SNAPSHOT,
        snapshot_id,
        # ON-018: human-readable name for the Explorer's search/display.
        {"name": f"Mesh scan {now.strftime('%Y-%m-%d %H:%M')}", "scanned_at": now.isoformat()},
    )

    ieee_to_device = _ieee_to_device_ids(hass)
    link_count = 0
    for link in links:
        source = (link.get("source") or {}).get("ieeeAddr")
        target = (link.get("target") or {}).get("ieeeAddr")
        if not source or not target:
            continue
        link_id = f"{snapshot_id}::{source}->{target}"
        relationship = link.get("relationship")
        if not isinstance(relationship, int) or isinstance(relationship, bool):
            relationship = None
        await merge_node(
            client,
            LABEL_MESH_LINK,
            link_id,
            {
                "name": f"{names.get(source, source)} → {names.get(target, target)}",
                "from_device": source,
                "from_name": names.get(source, source),
                "to_device": target,
                "to_name": names.get(target, target),
                "lqi": link.get("linkquality"),
                "depth": link.get("depth"),
                "relationship": relationship,
                "relationship_name": _RELATIONSHIP_NAMES.get(relationship),
            },
        )
        await merge_relationship(
            client, LABEL_MESH_SNAPSHOT, snapshot_id, REL_HAS_LINK, LABEL_MESH_LINK, link_id
        )
        for rel_type, ieee in ((REL_FROM_DEVICE, source), (REL_TO_DEVICE, target)):
            device_id = ieee_to_device.get(ieee.lower())
            if device_id:
                await merge_relationship(
                    client, LABEL_MESH_LINK, link_id, rel_type, LABEL_DEVICE, device_id
                )
        link_count += 1

    await _async_prune_old_snapshots(client, retention_days)
    return {"links": link_count}


async def _async_prune_old_snapshots(client: MemgraphClient, retention_days: int) -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    await client.run_query_with_retry(
        f"MATCH (s:{LABEL_MESH_SNAPSHOT}) WHERE s.scanned_at < $cutoff "
        f"OPTIONAL MATCH (s)-[:{REL_HAS_LINK}]->(l:{LABEL_MESH_LINK}) "
        "DETACH DELETE s, l",
        {"cutoff": cutoff},
    )
