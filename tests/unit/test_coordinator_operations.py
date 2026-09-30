"""Coordinator orchestration that earlier tickets merged without unit tests:
spatial sync / Zigbee mesh scan operations (ON-015/016/020), batched entity
sync (ON-002/ON-008) and retry of tracked failures (FR-020)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ontology.const import (
    CONF_COORDINATOR_DEVICE_ID,
    CONF_MESH_SNAPSHOT_RETENTION_DAYS,
    CONF_ZIGBEE2MQTT_BASE_TOPIC,
    HEALTH_ERROR,
    HEALTH_OK,
)
from custom_components.ontology.coordinator import OntologyCoordinator

_CO = "custom_components.ontology.coordinator"


def _coordinator(hass, options: dict | None = None) -> OntologyCoordinator:
    entry = MagicMock()
    entry.options = options or {}
    coordinator = OntologyCoordinator(hass, entry, AsyncMock())
    coordinator._refresh_counts = AsyncMock()
    return coordinator


# -- spatial sync (ON-015) ----------------------------------------------------


async def test_spatial_sync_records_counts_duration_and_refreshes(hass) -> None:
    coordinator = _coordinator(hass)
    with patch(
        f"{_CO}.spatial_sync.async_sync_spatial_layout",
        AsyncMock(return_value={"walls": 14, "pinned_on_floor": 57}),
    ) as sync:
        result = await coordinator.async_sync_spatial_layout()

    assert result == {"walls": 14, "pinned_on_floor": 57}
    sync.assert_awaited_once_with(hass, coordinator.memgraph_client)
    assert coordinator.state.spatial_sync_last_walls == 14
    assert coordinator.state.spatial_sync_last_pinned_on_floor == 57
    assert coordinator.state.spatial_sync_last_duration_ms is not None
    assert coordinator.state.health == HEALTH_OK
    coordinator._refresh_counts.assert_awaited_once()


async def test_spatial_sync_failure_is_recorded_and_reraised(hass) -> None:
    coordinator = _coordinator(hass)
    with (
        patch(
            f"{_CO}.spatial_sync.async_sync_spatial_layout",
            AsyncMock(side_effect=RuntimeError("spatial_context exploded")),
        ),
        pytest.raises(RuntimeError),
    ):
        await coordinator.async_sync_spatial_layout()

    assert coordinator.state.health == HEALTH_ERROR
    assert "spatial_context exploded" in coordinator.state.last_error
    # The duration sensor still reports how long the failed attempt took.
    assert coordinator.state.spatial_sync_last_duration_ms is not None
    coordinator._refresh_counts.assert_not_awaited()


# -- Zigbee mesh scan (ON-016 / ON-020) ---------------------------------------


async def test_mesh_scan_passes_options_and_records_result(hass) -> None:
    coordinator = _coordinator(
        hass,
        {
            CONF_ZIGBEE2MQTT_BASE_TOPIC: "z2m_custom",
            CONF_MESH_SNAPSHOT_RETENTION_DAYS: 7,
            CONF_COORDINATOR_DEVICE_ID: "device-abc",
        },
    )
    with patch(
        f"{_CO}.mesh_sync.async_scan_zigbee_mesh", AsyncMock(return_value={"links": 120})
    ) as scan:
        result = await coordinator.async_scan_zigbee_mesh()

    assert result == {"links": 120}
    kwargs = scan.await_args.kwargs
    assert kwargs["base_topic"] == "z2m_custom"
    assert kwargs["retention_days"] == 7
    assert kwargs["coordinator_device_id"] == "device-abc"
    assert coordinator.state.mesh_scan_last_links == 120
    assert coordinator.state.mesh_scan_last_duration_ms is not None
    coordinator._refresh_counts.assert_awaited_once()


async def test_mesh_scan_empty_coordinator_device_option_means_unset(hass) -> None:
    """A cleared options field arrives as "" and must not pin a bogus device."""
    coordinator = _coordinator(hass, {CONF_COORDINATOR_DEVICE_ID: ""})
    with patch(
        f"{_CO}.mesh_sync.async_scan_zigbee_mesh", AsyncMock(return_value={"links": 0})
    ) as scan:
        await coordinator.async_scan_zigbee_mesh()

    assert scan.await_args.kwargs["coordinator_device_id"] is None


async def test_mesh_scan_failure_is_recorded_and_reraised(hass) -> None:
    coordinator = _coordinator(hass)
    with (
        patch(
            f"{_CO}.mesh_sync.async_scan_zigbee_mesh",
            AsyncMock(side_effect=TimeoutError("no networkmap response")),
        ),
        pytest.raises(TimeoutError),
    ):
        await coordinator.async_scan_zigbee_mesh()

    assert coordinator.state.health == HEALTH_ERROR
    assert coordinator.state.mesh_scan_last_duration_ms is not None
    coordinator._refresh_counts.assert_not_awaited()


# -- batched entity sync (ON-002 / ON-008) ------------------------------------


def _batch_patches(update=None, reconcile=None):
    return (
        patch(f"{_CO}.graph_builder.update_entity", update or AsyncMock()),
        patch(
            f"{_CO}.user_knowledge.async_reconcile_energy_role_for_entity",
            reconcile or AsyncMock(return_value=False),
        ),
        patch(f"{_CO}.user_knowledge.async_repair_energy_role_bindings", AsyncMock()),
    )


async def test_empty_batch_is_a_noop(hass) -> None:
    coordinator = _coordinator(hass)
    update, reconcile, repair = _batch_patches()
    with update as u, reconcile, repair:
        await coordinator.async_handle_entity_changes_batch({})

    u.assert_not_awaited()
    coordinator._refresh_counts.assert_not_awaited()


async def test_plain_batch_skips_counts_refresh_and_binding_repair(hass) -> None:
    """ON-008: with no removals and no role assigned, nothing graph-wide runs."""
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")
    coordinator = _coordinator(hass)
    update, reconcile, repair = _batch_patches()
    with update as u, reconcile, repair as r:
        await coordinator.async_handle_entity_changes_batch(
            {"sensor.a": None, "sensor.b": None}
        )

    assert u.await_count == 2
    r.assert_not_awaited()
    coordinator._refresh_counts.assert_not_awaited()
    assert coordinator.state.health == HEALTH_OK
    assert coordinator.state.failed_updates == []


async def test_batch_with_assigned_role_repairs_bindings_once(hass) -> None:
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")
    coordinator = _coordinator(hass)
    update, reconcile, repair = _batch_patches(reconcile=AsyncMock(return_value=True))
    with update, reconcile, repair as r:
        await coordinator.async_handle_entity_changes_batch(
            {"sensor.a": None, "sensor.b": None}
        )

    r.assert_awaited_once()


async def test_batch_with_removed_entity_refreshes_counts_once(hass) -> None:
    """An entity gone from both registry and state machine counts as removed."""
    hass.states.async_set("sensor.kept", "1")
    coordinator = _coordinator(hass)
    update, reconcile, repair = _batch_patches()
    with update, reconcile, repair as r:
        await coordinator.async_handle_entity_changes_batch(
            {"sensor.kept": None, "sensor.gone": None}
        )

    coordinator._refresh_counts.assert_awaited_once()
    r.assert_awaited_once()


async def test_one_failing_entity_does_not_abort_the_batch(hass) -> None:
    hass.states.async_set("sensor.good", "1")
    hass.states.async_set("sensor.bad", "2")

    async def flaky_update(_hass, _client, entity_id, _context):
        if entity_id == "sensor.bad":
            raise RuntimeError("write failed")

    coordinator = _coordinator(hass)
    update, reconcile, repair = _batch_patches(update=AsyncMock(side_effect=flaky_update))
    with update as u, reconcile, repair:
        await coordinator.async_handle_entity_changes_batch(
            {"sensor.bad": None, "sensor.good": None}
        )

    assert u.await_count == 2
    assert [item["id"] for item in coordinator.state.failed_updates] == ["sensor.bad"]
    assert coordinator.state.failed_updates[0]["attempts"] == 1


async def test_failed_entity_is_cleared_once_a_later_batch_succeeds(hass) -> None:
    hass.states.async_set("sensor.flaky", "1")
    coordinator = _coordinator(hass)
    update, reconcile, repair = _batch_patches(update=AsyncMock(side_effect=RuntimeError("x")))
    with update, reconcile, repair:
        await coordinator.async_handle_entity_changes_batch({"sensor.flaky": None})
        await coordinator.async_handle_entity_changes_batch({"sensor.flaky": None})
    assert coordinator.state.failed_updates[0]["attempts"] == 2

    update, reconcile, repair = _batch_patches()
    with update, reconcile, repair:
        await coordinator.async_handle_entity_changes_batch({"sensor.flaky": None})
    assert coordinator.state.failed_updates == []


# -- retry of tracked failures (FR-020) ---------------------------------------


async def test_retry_failed_updates_dispatches_by_kind(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.state.failed_updates = [
        {"kind": "entity", "id": "sensor.a", "attempts": 1, "error": "e"},
        {"kind": "device", "id": "dev1", "attempts": 1, "error": "e"},
        {"kind": "area", "id": "area1", "attempts": 1, "error": "e"},
    ]
    coordinator.async_handle_entity_change = AsyncMock()
    coordinator.async_handle_device_change = AsyncMock()
    coordinator.async_handle_area_change = AsyncMock()

    await coordinator.async_retry_failed_updates()

    coordinator.async_handle_entity_change.assert_awaited_once_with("sensor.a")
    coordinator.async_handle_device_change.assert_awaited_once_with("dev1")
    coordinator.async_handle_area_change.assert_awaited_once_with("area1")
