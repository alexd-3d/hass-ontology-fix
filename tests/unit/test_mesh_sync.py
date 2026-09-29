"""Unit tests for `mesh_sync.py` (ON-016).

Mocks `homeassistant.components.mqtt` directly (an async-function
`side_effect` on a patched attribute simulates the async subscribe/publish
calls) rather than a real broker - this module only ever calls
`mqtt.async_subscribe`/`mqtt.async_publish`, both patched here.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from custom_components.ontology import mesh_sync
from custom_components.ontology.const import LABEL_MESH_LINK, LABEL_MESH_SNAPSHOT

_SAMPLE_NETWORKMAP_RESPONSE = {
    "data": {
        "value": {
            "nodes": [
                {"ieeeAddr": "0x00", "friendlyName": "Coordinator"},
                {"ieeeAddr": "0x01", "friendlyName": "Switch-Hall"},
            ],
            "links": [
                {
                    "source": {"ieeeAddr": "0x00"},
                    "target": {"ieeeAddr": "0x01"},
                    "linkquality": 120,
                    "depth": 1,
                    "relationship": 1,
                }
            ],
        }
    },
    "status": "ok",
}


def _register_fake_mqtt_publish_service(hass) -> None:
    async def _handle_publish(call):
        return None

    hass.services.async_register("mqtt", "publish", _handle_publish)


class _FakeMessage:
    def __init__(self, payload: str) -> None:
        self.payload = payload


def _device_edges(client) -> set[tuple[str, str]]:
    """(relationship, device id) pairs written by the batched device-edge queries."""
    edges = set()
    for call in client.run_query_with_retry.call_args_list:
        query, params = call.args
        for rel, key in (("FROM_DEVICE", "from_device_id"), ("TO_DEVICE", "to_device_id")):
            if f"MERGE (n)-[r:{rel}]->(d)" in query:
                edges.update((rel, row[key]) for row in params["rows"])
    return edges


async def test_mqtt_available_false_when_service_missing(hass) -> None:
    assert mesh_sync.mqtt_available(hass) is False


async def test_mqtt_available_true_when_service_registered(hass) -> None:
    _register_fake_mqtt_publish_service(hass)

    assert mesh_sync.mqtt_available(hass) is True


async def test_scan_zigbee_mesh_noops_when_mqtt_missing(hass, mock_memgraph_client) -> None:
    result = await mesh_sync.async_scan_zigbee_mesh(
        hass,
        mock_memgraph_client,
        base_topic="zigbee2mqtt",
        retention_days=30,
        response_timeout_seconds=150.0,
    )

    assert result == {"links": 0}
    mock_memgraph_client.run_query_with_retry.assert_not_called()


async def test_scan_zigbee_mesh_writes_snapshot_and_links(hass, mock_memgraph_client) -> None:
    _register_fake_mqtt_publish_service(hass)

    async def _fake_subscribe(hass_arg, topic, msg_callback, qos=0):
        msg_callback(_FakeMessage(json.dumps(_SAMPLE_NETWORKMAP_RESPONSE)))
        return lambda: None

    with (
        patch.object(mesh_sync.mqtt, "async_subscribe", side_effect=_fake_subscribe),
        patch.object(mesh_sync.mqtt, "async_publish", new=AsyncMock()),
    ):
        result = await mesh_sync.async_scan_zigbee_mesh(
            hass,
            mock_memgraph_client,
            base_topic="zigbee2mqtt",
            retention_days=30,
            response_timeout_seconds=150.0,
        )

    assert result == {"links": 1}

    calls = mock_memgraph_client.run_query_with_retry.call_args_list
    queries_and_params = [call.args for call in calls]

    snapshot_call = next(
        (q, p) for q, p in queries_and_params if f"MERGE (n:{LABEL_MESH_SNAPSHOT}" in q
    )
    assert "scanned_at" in snapshot_call[1]["properties"]

    batch_call = next((q, p) for q, p in queries_and_params if "UNWIND $rows" in q and "HAS_LINK" in q)
    assert batch_call[1]["snapshot_id"]
    (row,) = batch_call[1]["rows"]
    link_props = row["properties"]
    assert link_props["from_device"] == "0x00"
    assert link_props["from_name"] == "Coordinator"
    assert link_props["to_device"] == "0x01"
    assert link_props["to_name"] == "Switch-Hall"
    assert link_props["lqi"] == 120
    assert link_props["relationship"] == 1
    assert link_props["relationship_name"] == "child"

    assert any("DETACH DELETE" in q for q, _p in queries_and_params)


async def test_scan_zigbee_mesh_times_out_gracefully(hass, mock_memgraph_client) -> None:
    """No response within the timeout returns an empty result, not an error."""
    _register_fake_mqtt_publish_service(hass)

    async def _fake_subscribe(hass_arg, topic, msg_callback, qos=0):
        return lambda: None  # never calls msg_callback

    with (
        patch.object(mesh_sync.mqtt, "async_subscribe", side_effect=_fake_subscribe),
        patch.object(mesh_sync.mqtt, "async_publish", new=AsyncMock()),
    ):
        result = await mesh_sync.async_scan_zigbee_mesh(
            hass,
            mock_memgraph_client,
            base_topic="zigbee2mqtt",
            retention_days=30,
            response_timeout_seconds=0.01,
        )

    assert result == {"links": 0}


async def test_scan_links_mesh_link_to_device_nodes(hass, mock_memgraph_client) -> None:
    _register_fake_mqtt_publish_service(hass)

    async def _fake_subscribe(hass_arg, topic, msg_callback, qos=0):
        msg_callback(_FakeMessage(json.dumps(_SAMPLE_NETWORKMAP_RESPONSE)))
        return lambda: None

    with (
        patch.object(mesh_sync.mqtt, "async_subscribe", side_effect=_fake_subscribe),
        patch.object(mesh_sync.mqtt, "async_publish", new=AsyncMock()),
        patch.object(
            mesh_sync, "_ieee_to_device_ids", return_value={"0x00": "dev-c", "0x01": "dev-h"}
        ),
    ):
        await mesh_sync.async_scan_zigbee_mesh(
            hass,
            mock_memgraph_client,
            base_topic="zigbee2mqtt",
            retention_days=30,
            response_timeout_seconds=150.0,
        )

    edges = _device_edges(mock_memgraph_client)
    assert ("FROM_DEVICE", "dev-c") in edges
    assert ("TO_DEVICE", "dev-h") in edges


def test_ieee_to_device_ids_maps_z2m_and_bridge_identifiers(hass) -> None:
    from types import SimpleNamespace

    devices = {
        "a": SimpleNamespace(id="dev-a", identifiers={("mqtt", "zigbee2mqtt_0xABC")}),
        "b": SimpleNamespace(id="dev-b", identifiers={("mqtt", "zigbee2mqtt_bridge_0xDEF")}),
        "c": SimpleNamespace(id="dev-c", identifiers={("hue", "zigbee2mqtt_0x123")}),
    }
    with patch.object(mesh_sync.dr, "async_get", return_value=SimpleNamespace(devices=devices)):
        mapping = mesh_sync._ieee_to_device_ids(hass)

    assert mapping == {"0xabc": "dev-a", "0xdef": "dev-b"}


def test_extract_links_handles_unexpected_shape() -> None:
    links, names = mesh_sync._extract_links({"unexpected": "shape"})

    assert links == []
    assert names == {}


def test_coordinator_ieee_reads_the_coordinator_node() -> None:
    payload = {
        "data": {
            "value": {
                "nodes": [
                    {"ieeeAddr": "0x01", "type": "Router"},
                    {"ieeeAddr": "0x00AB", "type": "Coordinator"},
                ]
            }
        }
    }

    assert mesh_sync._coordinator_ieee(payload) == "0x00ab"
    assert mesh_sync._coordinator_ieee({"unexpected": "shape"}) is None


async def test_scan_points_coordinator_links_at_configured_device(hass, mock_memgraph_client) -> None:
    _register_fake_mqtt_publish_service(hass)
    response = json.loads(json.dumps(_SAMPLE_NETWORKMAP_RESPONSE))
    response["data"]["value"]["nodes"][0]["type"] = "Coordinator"

    async def _fake_subscribe(hass_arg, topic, msg_callback, qos=0):
        msg_callback(_FakeMessage(json.dumps(response)))
        return lambda: None

    with (
        patch.object(mesh_sync.mqtt, "async_subscribe", side_effect=_fake_subscribe),
        patch.object(mesh_sync.mqtt, "async_publish", new=AsyncMock()),
        patch.object(
            mesh_sync, "_ieee_to_device_ids", return_value={"0x00": "bridge", "0x01": "dev-h"}
        ),
    ):
        await mesh_sync.async_scan_zigbee_mesh(
            hass,
            mock_memgraph_client,
            base_topic="zigbee2mqtt",
            retention_days=30,
            response_timeout_seconds=150.0,
            coordinator_device_id="slzb-adapter",
        )

    targets = {dev for rel, dev in _device_edges(mock_memgraph_client) if rel == "FROM_DEVICE"}
    assert targets == {"slzb-adapter"}


async def test_links_are_written_in_batches_not_one_query_per_link(hass, mock_memgraph_client) -> None:
    _register_fake_mqtt_publish_service(hass)
    response = json.loads(json.dumps(_SAMPLE_NETWORKMAP_RESPONSE))
    template = response["data"]["value"]["links"][0]
    response["data"]["value"]["links"] = [
        {**template, "source": {"ieeeAddr": f"0x{i:02x}"}, "target": {"ieeeAddr": "0x00"}}
        for i in range(1, 601)
    ]

    async def _fake_subscribe(hass_arg, topic, msg_callback, qos=0):
        msg_callback(_FakeMessage(json.dumps(response)))
        return lambda: None

    with (
        patch.object(mesh_sync.mqtt, "async_subscribe", side_effect=_fake_subscribe),
        patch.object(mesh_sync.mqtt, "async_publish", new=AsyncMock()),
    ):
        result = await mesh_sync.async_scan_zigbee_mesh(
            hass,
            mock_memgraph_client,
            base_topic="zigbee2mqtt",
            retention_days=30,
            response_timeout_seconds=150.0,
        )

    assert result == {"links": 600}
    batch_queries = [
        call.args
        for call in mock_memgraph_client.run_query_with_retry.call_args_list
        if "UNWIND $rows" in call.args[0] and "MERGE (n:MeshLink" in call.args[0]
    ]
    assert [len(params["rows"]) for _q, params in batch_queries] == [250, 250, 100]
