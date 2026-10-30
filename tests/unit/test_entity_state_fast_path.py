"""ON-036: plain state changes are written in one bulk query per batch.

A `state_changed` event can only change an Entity node's own properties, so the
batch sync writes entities that already have a node with one `UNWIND` query
instead of ~8 queries each, and keeps the full per-entity path for everything
else (no node yet, removed, stateless, the integration's own entities, or a
failed bulk write).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import entity_registry as er

from custom_components.ontology import graph_builder
from custom_components.ontology.const import DOMAIN, HEALTH_OK
from custom_components.ontology.coordinator import OntologyCoordinator
from custom_components.ontology.graph_builder import (
    _ENTITY_BULK_QUERY,
    _ENTITY_MEASUREMENT_REMOVE,
    _ENTITY_STATE_BULK_QUERY,
    EntitySyncContext,
    update_entity_states_bulk,
)

_CO = "custom_components.ontology.coordinator"


def _coordinator(hass) -> OntologyCoordinator:
    entry = MagicMock()
    entry.options = {}
    coordinator = OntologyCoordinator(hass, entry, AsyncMock())
    coordinator._refresh_counts = AsyncMock()
    coordinator.change_buffer = MagicMock()
    return coordinator


def _patches(bulk, update=None, reconcile=None):
    return (
        patch(f"{_CO}.graph_builder.update_entity_states_bulk", bulk),
        patch(f"{_CO}.graph_builder.update_entity", update or AsyncMock()),
        patch(
            f"{_CO}.user_knowledge.async_reconcile_energy_role_for_entity",
            reconcile or AsyncMock(return_value=False),
        ),
        patch(f"{_CO}.user_knowledge.async_repair_energy_role_bindings", AsyncMock()),
    )


async def test_existing_entities_are_written_in_one_bulk_call(hass) -> None:
    for entity_id in ("sensor.a", "sensor.b", "sensor.c"):
        hass.states.async_set(entity_id, "1")
    coordinator = _coordinator(hass)
    bulk = AsyncMock(return_value={"sensor.a", "sensor.b", "sensor.c"})
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk)
    with bulk_p, update_p as update, reconcile_p as reconcile, repair_p:
        await coordinator.async_handle_entity_changes_batch(
            {"sensor.a": None, "sensor.b": None, "sensor.c": None}
        )

    bulk.assert_awaited_once()
    assert set(bulk.await_args.args[2]) == {"sensor.a", "sensor.b", "sensor.c"}
    update.assert_not_awaited()
    assert reconcile.await_count == 3
    assert coordinator.state.health == HEALTH_OK
    assert coordinator.state.failed_updates == []
    published = [call.args[0] for call in coordinator.change_buffer.publish_upsert.call_args_list]
    assert published == [["Entity:sensor.a"], ["Entity:sensor.b"], ["Entity:sensor.c"]]
    coordinator.change_buffer.publish_remove.assert_not_called()
    coordinator._refresh_counts.assert_not_awaited()


async def test_contexts_are_passed_through_to_the_bulk_write(hass) -> None:
    hass.states.async_set("sensor.a", "1")
    state = hass.states.get("sensor.a")
    context = EntitySyncContext(state=state, measurement_last_updated=state.last_updated)
    coordinator = _coordinator(hass)
    bulk = AsyncMock(return_value={"sensor.a"})
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk)
    with bulk_p, update_p, reconcile_p, repair_p:
        await coordinator.async_handle_entity_changes_batch({"sensor.a": context})

    assert bulk.await_args.args[2] == {"sensor.a": context}


async def test_entity_without_a_node_falls_back_to_the_full_write(hass) -> None:
    for entity_id in ("sensor.a", "sensor.b", "sensor.new"):
        hass.states.async_set(entity_id, "1")
    coordinator = _coordinator(hass)
    bulk = AsyncMock(return_value={"sensor.a", "sensor.b"})  # sensor.new had no node
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk)
    with bulk_p, update_p as update, reconcile_p as reconcile, repair_p:
        await coordinator.async_handle_entity_changes_batch(
            {"sensor.a": None, "sensor.b": None, "sensor.new": None}
        )

    assert [call.args[2] for call in update.await_args_list] == ["sensor.new"]
    assert reconcile.await_count == 3


async def test_removed_entity_never_reaches_the_bulk_write(hass) -> None:
    hass.states.async_set("sensor.kept", "1")
    coordinator = _coordinator(hass)
    bulk = AsyncMock(return_value={"sensor.kept"})
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk)
    with bulk_p, update_p as update, reconcile_p, repair_p as repair:
        await coordinator.async_handle_entity_changes_batch(
            {"sensor.kept": None, "sensor.gone": None}
        )

    assert set(bulk.await_args.args[2]) == {"sensor.kept"}
    assert [call.args[2] for call in update.await_args_list] == ["sensor.gone"]
    coordinator._refresh_counts.assert_awaited_once()
    repair.assert_awaited_once()
    coordinator.change_buffer.publish_remove.assert_called_once_with(["Entity:sensor.gone"], [])


async def test_batch_with_only_removed_entities_skips_the_bulk_write(hass) -> None:
    coordinator = _coordinator(hass)
    bulk = AsyncMock(return_value=set())
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk)
    with bulk_p, update_p as update, reconcile_p, repair_p:
        await coordinator.async_handle_entity_changes_batch({"sensor.gone": None})

    bulk.assert_not_awaited()
    update.assert_awaited_once()


async def test_the_integrations_own_entities_are_not_bulk_written(hass) -> None:
    registry = er.async_get(hass)
    own = registry.async_get_or_create("sensor", DOMAIN, "own_unique_id")
    hass.states.async_set(own.entity_id, "1")
    hass.states.async_set("sensor.other", "1")
    coordinator = _coordinator(hass)
    bulk = AsyncMock(return_value={"sensor.other"})
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk)
    with bulk_p, update_p as update, reconcile_p, repair_p:
        await coordinator.async_handle_entity_changes_batch(
            {own.entity_id: None, "sensor.other": None}
        )

    assert set(bulk.await_args.args[2]) == {"sensor.other"}
    assert [call.args[2] for call in update.await_args_list] == [own.entity_id]


async def test_bulk_failure_falls_back_to_per_entity_writes_without_failing(hass) -> None:
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")
    coordinator = _coordinator(hass)
    bulk = AsyncMock(side_effect=RuntimeError("bulk failed"))
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk)
    with bulk_p, update_p as update, reconcile_p, repair_p:
        await coordinator.async_handle_entity_changes_batch({"sensor.a": None, "sensor.b": None})

    assert update.await_count == 2
    assert coordinator.state.health == HEALTH_OK
    assert coordinator.state.failed_updates == []


async def test_unexpected_ids_in_the_bulk_result_are_ignored(hass) -> None:
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")
    coordinator = _coordinator(hass)
    bulk = AsyncMock(return_value={"sensor.a", "sensor.not_in_batch"})
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk)
    with bulk_p, update_p as update, reconcile_p, repair_p:
        await coordinator.async_handle_entity_changes_batch({"sensor.a": None, "sensor.b": None})

    assert [call.args[2] for call in update.await_args_list] == ["sensor.b"]


async def test_one_failing_fallback_entity_only_fails_itself(hass) -> None:
    for entity_id in ("sensor.good", "sensor.bad"):
        hass.states.async_set(entity_id, "1")
    coordinator = _coordinator(hass)
    bulk = AsyncMock(return_value={"sensor.good"})
    update = AsyncMock(side_effect=RuntimeError("write failed"))
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk, update=update)
    with bulk_p, update_p, reconcile_p, repair_p:
        await coordinator.async_handle_entity_changes_batch(
            {"sensor.good": None, "sensor.bad": None}
        )

    assert [item["id"] for item in coordinator.state.failed_updates] == ["sensor.bad"]
    coordinator.change_buffer.publish_upsert.assert_called_once_with(
        ["Entity:sensor.good"], [], ["state", "ha_id"]
    )


async def test_role_reconcile_still_runs_for_bulk_written_entities(hass) -> None:
    hass.states.async_set("sensor.power", "5")
    coordinator = _coordinator(hass)
    bulk = AsyncMock(return_value={"sensor.power"})
    reconcile = AsyncMock(return_value=True)
    bulk_p, update_p, reconcile_p, repair_p = _patches(bulk, reconcile=reconcile)
    with bulk_p, update_p as update, reconcile_p, repair_p as repair:
        await coordinator.async_handle_entity_changes_batch({"sensor.power": None})

    update.assert_not_awaited()
    reconcile.assert_awaited_once()
    repair.assert_awaited_once()


# -- graph_builder.update_entity_states_bulk ----------------------------------


def _sent(client: AsyncMock) -> list[list[dict]]:
    return [call.args[1]["rows"] for call in client.run_query_with_retry.await_args_list]


async def test_state_bulk_rows_match_the_per_entity_properties(hass) -> None:
    hass.states.async_set(
        "sensor.power", "42.5", {"device_class": "power", "unit_of_measurement": "W"}
    )
    hass.states.async_set("switch.plain", "on")
    client = AsyncMock()
    client.run_query_with_retry.return_value = [
        {"ha_id": "sensor.power"},
        {"ha_id": "switch.plain"},
    ]

    updated = await update_entity_states_bulk(
        hass, client, {"sensor.power": None, "switch.plain": None}
    )

    assert updated == {"sensor.power", "switch.plain"}
    rows = {row["ha_id"]: row for row in _sent(client)[0]}
    for entity_id in ("sensor.power", "switch.plain"):
        properties, measurement = graph_builder._entity_node_properties(hass, entity_id, None)
        assert rows[entity_id]["properties"]["name"] == properties["name"]
        assert rows[entity_id]["properties"]["state"] == properties["state"]
        assert rows[entity_id]["properties"]["source"] == properties["source"]
        assert rows[entity_id]["measurement_properties"] == measurement
    assert rows["sensor.power"]["measurement_properties"]  # a power sensor has measurement props


async def test_state_bulk_returns_only_matched_ids(hass) -> None:
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")
    client = AsyncMock()
    client.run_query_with_retry.return_value = [{"ha_id": "sensor.a"}]

    updated = await update_entity_states_bulk(hass, client, {"sensor.a": None, "sensor.b": None})

    assert updated == {"sensor.a"}


async def test_state_bulk_splits_large_batches(hass) -> None:
    ids = [f"sensor.s{i}" for i in range(251)]
    for entity_id in ids:
        hass.states.async_set(entity_id, "1")
    client = AsyncMock()
    client.run_query_with_retry.side_effect = lambda _query, params: [
        {"ha_id": row["ha_id"]} for row in params["rows"]
    ]

    updated = await update_entity_states_bulk(hass, client, dict.fromkeys(ids))

    assert [len(batch) for batch in _sent(client)] == [250, 1]
    assert updated == set(ids)


async def test_state_bulk_with_no_entities_sends_nothing(hass) -> None:
    client = AsyncMock()

    assert await update_entity_states_bulk(hass, client, {}) == set()
    client.run_query_with_retry.assert_not_awaited()


def test_state_bulk_query_matches_instead_of_merging_and_clears_measurements() -> None:
    assert "MATCH (n:Entity {ha_id: row.ha_id})" in _ENTITY_STATE_BULK_QUERY
    assert "MERGE" not in _ENTITY_STATE_BULK_QUERY
    assert "RETURN row.ha_id AS ha_id" in _ENTITY_STATE_BULK_QUERY
    # The REMOVE list is one shared fragment, so the full-sync and state-only
    # writes can never clear different measurement properties.
    assert _ENTITY_MEASUREMENT_REMOVE.startswith("REMOVE n.")
    assert _ENTITY_MEASUREMENT_REMOVE in _ENTITY_STATE_BULK_QUERY
    assert _ENTITY_MEASUREMENT_REMOVE in _ENTITY_BULK_QUERY
