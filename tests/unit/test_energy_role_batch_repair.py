"""ON-008 regression: the energy-role binding repair inside
`_execute_entity_sync_batch` must run at most once per batch - gated on
whether the batch could plausibly have left a binding stale - instead of
once per entity in the batch (the same class of unconditional full-graph
operation as ON-007's `_refresh_counts` bug, but worse: it ran ~52x per
batch instead of once).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ontology.coordinator import OntologyCoordinator


def _make_coordinator(hass) -> OntologyCoordinator:
    entry = MagicMock()
    entry.options = {}
    client = AsyncMock()
    return OntologyCoordinator(hass, entry, client)


async def test_batch_skips_repair_when_nothing_could_go_stale(hass) -> None:
    """No entity in the batch resolved to an inferred role and none were
    removed - the shared, full-graph binding repair should not run at all."""
    coordinator = _make_coordinator(hass)
    hass.states.async_set("light.kitchen", "on")

    with (
        patch("custom_components.ontology.coordinator.graph_builder") as mock_gb,
        patch("custom_components.ontology.coordinator.user_knowledge") as mock_uk,
    ):
        mock_gb.update_entity = AsyncMock()
        mock_uk.async_reconcile_energy_role_for_entity = AsyncMock(return_value=False)
        mock_uk.async_repair_energy_role_bindings = AsyncMock()

        results = await coordinator._execute_entity_sync_batch({"light.kitchen": None})

    assert results == {"light.kitchen": None}
    mock_uk.async_reconcile_energy_role_for_entity.assert_awaited_once_with(
        hass, coordinator.memgraph_client, "light.kitchen"
    )
    mock_uk.async_repair_energy_role_bindings.assert_not_awaited()


async def test_batch_runs_repair_once_when_a_role_was_assigned(hass) -> None:
    """Three entities sync in one batch; only one resolves to an inferred
    energy role. The shared repair must run exactly once for the whole
    batch, not once per entity (the ON-008 bug)."""
    coordinator = _make_coordinator(hass)
    for entity_id in ("sensor.a_power", "sensor.b", "sensor.c"):
        hass.states.async_set(entity_id, "1")

    with (
        patch("custom_components.ontology.coordinator.graph_builder") as mock_gb,
        patch("custom_components.ontology.coordinator.user_knowledge") as mock_uk,
    ):
        mock_gb.update_entity = AsyncMock()
        mock_uk.async_reconcile_energy_role_for_entity = AsyncMock(
            side_effect=[True, False, False]
        )
        mock_uk.async_repair_energy_role_bindings = AsyncMock()

        results = await coordinator._execute_entity_sync_batch(
            {"sensor.a_power": None, "sensor.b": None, "sensor.c": None}
        )

    assert all(err is None for err in results.values())
    assert mock_uk.async_reconcile_energy_role_for_entity.await_count == 3
    mock_uk.async_repair_energy_role_bindings.assert_awaited_once_with(
        coordinator.memgraph_client
    )


async def test_batch_runs_repair_once_when_an_entity_was_removed(hass) -> None:
    """An entity disappeared mid-flight (no registry entry, no live state).
    Even with zero inferred roles this batch, the repair must still run
    once, since the removed entity's own assignment binding (if any) may
    now be stale - mirroring the existing `removed`-gated counts refresh."""
    coordinator = _make_coordinator(hass)
    coordinator.memgraph_client.run_query = AsyncMock(return_value=[{"c": 0}])

    with (
        patch("custom_components.ontology.coordinator.graph_builder") as mock_gb,
        patch("custom_components.ontology.coordinator.user_knowledge") as mock_uk,
    ):
        mock_gb.update_entity = AsyncMock()
        mock_uk.async_reconcile_energy_role_for_entity = AsyncMock(return_value=False)
        mock_uk.async_repair_energy_role_bindings = AsyncMock()

        # sensor.gone has neither a registry entry nor a live state, so
        # _execute_entity_sync_batch's own "removed" check picks it up.
        results = await coordinator._execute_entity_sync_batch({"sensor.gone": None})

    assert results == {"sensor.gone": None}
    mock_uk.async_repair_energy_role_bindings.assert_awaited_once_with(
        coordinator.memgraph_client
    )
