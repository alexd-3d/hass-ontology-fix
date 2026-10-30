"""ON-036: state-only batches write the same graph as the full per-entity path.

Runs the real coordinator batch against a real Memgraph: entities that already
have a node are updated by one bulk query and must keep every relationship
untouched; entities without a node, and removed entities, keep the full path.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ontology import graph_builder
from custom_components.ontology.const import DOMAIN
from custom_components.ontology.coordinator import OntologyCoordinator
from custom_components.ontology.memgraph_client import MemgraphClient


def _register(hass, entry: MockConfigEntry, unique_id: str, **kwargs: Any) -> str:
    area = ar.async_get(hass).async_get_or_create("Test Area")
    labels = lr.async_get(hass)
    label = labels.async_get_label_by_name("Test Label") or labels.async_create("Test Label")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"device-{unique_id}")},
        name=f"Test Device {unique_id}",
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    entity = er.async_get(hass).async_get_or_create(
        "sensor", "test_platform", unique_id, device_id=device.id, **kwargs
    )
    er.async_get(hass).async_update_entity(entity.entity_id, labels={label.label_id})
    return entity.entity_id


async def _relationships(client: MemgraphClient, entity_id: str) -> list[dict[str, Any]]:
    return await client.run_query(
        "MATCH (e:Entity {ha_id: $id})-[r]-(o) "
        "RETURN type(r) AS type, o.ha_id AS other, r.source AS source, "
        "r.updated_at AS updated_at ORDER BY type, other",
        {"id": entity_id},
    )


async def _node(client: MemgraphClient, entity_id: str) -> dict[str, Any]:
    rows = await client.run_query(
        "MATCH (e:Entity {ha_id: $id}) RETURN properties(e) AS props", {"id": entity_id}
    )
    return rows[0]["props"]


def _coordinator(hass, entry, client: MemgraphClient) -> OntologyCoordinator:
    return OntologyCoordinator(hass, entry, client)


async def test_state_change_updates_the_node_and_leaves_relationships_untouched(
    hass, memgraph_client: MemgraphClient
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entity_id = _register(hass, entry, "state-1")
    hass.states.async_set(entity_id, "off")
    await graph_builder.build_full_graph(hass, memgraph_client)
    before = await _relationships(memgraph_client, entity_id)
    assert {row["type"] for row in before} >= {"IN_DOMAIN", "HAS_ENTITY", "PROVIDED_BY"}

    hass.states.async_set(entity_id, "on")
    await _coordinator(hass, entry, memgraph_client).async_handle_entity_changes_batch(
        {entity_id: None}
    )

    node = await _node(memgraph_client, entity_id)
    assert node["state"] == "on"
    # Same edges, same owners, and not rewritten (updated_at is unchanged).
    assert await _relationships(memgraph_client, entity_id) == before


async def test_bulk_written_node_matches_the_full_path_node(
    hass, memgraph_client: MemgraphClient
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entity_id = _register(hass, entry, "power-1", original_device_class="power")
    attributes = {"device_class": "power", "unit_of_measurement": "W"}
    hass.states.async_set(entity_id, "42.5", attributes)
    await graph_builder.build_full_graph(hass, memgraph_client)

    for state in ("120", "unavailable", "7.5"):
        hass.states.async_set(entity_id, state, attributes)
        await _coordinator(hass, entry, memgraph_client).async_handle_entity_changes_batch(
            {entity_id: None}
        )
        via_bulk = await _node(memgraph_client, entity_id)

        await graph_builder.update_entity(hass, memgraph_client, entity_id)
        via_full = await _node(memgraph_client, entity_id)

        # Only the write timestamp may differ between the two paths.
        via_bulk.pop("updated_at", None)
        via_full.pop("updated_at", None)
        assert via_bulk == via_full, state


async def test_entity_without_a_node_is_created_with_its_relationships(
    hass, memgraph_client: MemgraphClient
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    await graph_builder.build_full_graph(hass, memgraph_client)  # nothing registered yet
    entity_id = _register(hass, entry, "late-1")
    hass.states.async_set(entity_id, "on")

    await _coordinator(hass, entry, memgraph_client).async_handle_entity_changes_batch(
        {entity_id: None}
    )

    assert (await _node(memgraph_client, entity_id))["state"] == "on"
    types = {row["type"] for row in await _relationships(memgraph_client, entity_id)}
    assert {"IN_DOMAIN", "PROVIDED_BY"} <= types


async def test_removed_entity_is_deleted_while_the_rest_of_the_batch_is_bulk_written(
    hass, memgraph_client: MemgraphClient
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    kept = _register(hass, entry, "kept-1")
    gone = _register(hass, entry, "gone-1")
    hass.states.async_set(kept, "1")
    hass.states.async_set(gone, "1")
    await graph_builder.build_full_graph(hass, memgraph_client)

    hass.states.async_set(kept, "2")
    er.async_get(hass).async_remove(gone)
    hass.states.async_remove(gone)
    await hass.async_block_till_done()
    await _coordinator(hass, entry, memgraph_client).async_handle_entity_changes_batch(
        {kept: None, gone: None}
    )

    assert (await _node(memgraph_client, kept))["state"] == "2"
    rows = await memgraph_client.run_query(
        "MATCH (e:Entity {ha_id: $id}) RETURN count(e) AS c", {"id": gone}
    )
    assert rows == [{"c": 0}]
