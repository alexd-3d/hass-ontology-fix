"""Registry and state_changed event listeners for the Ontology integration.

Registry changes (area/device/entity add/remove/update) are forwarded to the
coordinator immediately. `state_changed` events are filtered to primary
state changes only (FR-012a) and debounced across a single shared batch
window (research.md §5, FR-011; ON-002) before being forwarded, so rapid
successive changes - across any number of entities - collapse into a single
batched sync instead of one sync per entity.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_EXCLUDED_DOMAINS,
    CONF_EXCLUDED_ENTITIES,
    CONF_STATE_CHANGE_DEBOUNCE_SECONDS,
    DEFAULT_EXCLUDED_DOMAINS,
    DEFAULT_EXCLUDED_ENTITIES,
    STATE_CHANGE_DEBOUNCE_SECONDS,
)
from .coordinator import OntologyCoordinator
from .graph_builder import EntitySyncContext

_LOGGER = logging.getLogger(__name__)


def _parse_csv(value: str) -> set[str]:
    """Parse a comma-separated options string into a normalized set."""
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


class StateChangeDebouncer:
    """Collapses `state_changed` events across ALL entities into a single
    shared batch window instead of one independent timer per entity
    (ON-002).

    Previously every entity had its own debounce timer, so N entities
    changing state within the same window still produced up to N separate
    coordinator syncs - and each sync's `_refresh_counts()` runs two
    full-graph Cypher scans, so that fan-out was the dominant amplifier of
    the sync-overload bug, not just "many small writes" on their own.

    Now a single timer is armed only on the empty -> non-empty transition of
    the pending-entity queue, and is never rearmed by further events
    arriving inside the same window. On expiry, every entity accumulated
    during the window is flushed to the coordinator as one batch
    (`OntologyCoordinator.async_handle_entity_changes_batch`), which
    refreshes counts once for the whole batch. This puts a hard ceiling on
    sync frequency of one batch per debounce window, independent of how many
    entities changed or how often.
    """

    def __init__(self, hass: HomeAssistant, coordinator: OntologyCoordinator) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._timer: asyncio.TimerHandle | None = None
        self._pending: dict[str, EntitySyncContext] = {}

    def _debounce_seconds(self) -> float:
        options = self._coordinator.entry.options
        return options.get(
            CONF_STATE_CHANGE_DEBOUNCE_SECONDS, STATE_CHANGE_DEBOUNCE_SECONDS
        )

    def _excluded_domains(self) -> set[str]:
        options = self._coordinator.entry.options
        return _parse_csv(options.get(CONF_EXCLUDED_DOMAINS, DEFAULT_EXCLUDED_DOMAINS))

    def _excluded_entities(self) -> set[str]:
        options = self._coordinator.entry.options
        return _parse_csv(options.get(CONF_EXCLUDED_ENTITIES, DEFAULT_EXCLUDED_ENTITIES))

    @callback
    def async_handle_state_changed(self, event: Event) -> None:
        """Retain accepted state/attribute changes and ignore unrelated churn."""
        entity_id = event.data.get("entity_id")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if entity_id is None or new_state is None:
            return
        domain = entity_id.split(".", 1)[0]
        if entity_id in self._excluded_entities() or domain in self._excluded_domains():
            return
        measurement_last_updated = new_state.last_updated
        if old_state is not None and old_state.state == new_state.state:
            changed_attributes = {
                key
                for key in ("friendly_name", "device_class", "unit_of_measurement")
                if old_state.attributes.get(key) != new_state.attributes.get(key)
            }
            if not changed_attributes:
                return
            if changed_attributes == {"friendly_name"}:
                measurement_last_updated = old_state.last_updated

        # ON-004: one raw accepted state-change, counted before batching -
        # a single cheap int increment, see OntologyCoordinator.record_sync_event.
        self._coordinator.record_sync_event()

        was_empty = not self._pending
        self._pending[entity_id] = EntitySyncContext(
            state=new_state,
            measurement_last_updated=measurement_last_updated,
        )
        if was_empty:
            self._timer = self._hass.loop.call_later(
                self._debounce_seconds(), self._fire
            )

    def _fire(self) -> None:
        self._timer = None
        batch = self._pending
        self._pending = {}
        self._hass.async_create_task(
            self._coordinator.async_handle_entity_changes_batch(batch)
        )

    def async_cancel_all(self) -> None:
        """Cancel the pending batch timer (called on unload)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._pending.clear()


def async_register_listeners(
    hass: HomeAssistant, coordinator: OntologyCoordinator
) -> Callable[[], None]:
    """Register all registry/state listeners; returns an unsubscribe callable."""
    debouncer = StateChangeDebouncer(hass, coordinator)

    @callback
    def _on_area_registry_updated(event: Event) -> None:
        if hass.state is not CoreState.running:
            return
        area_id = event.data.get("area_id")
        if area_id:
            hass.async_create_task(coordinator.async_handle_area_change(area_id))

    @callback
    def _on_device_registry_updated(event: Event) -> None:
        if hass.state is not CoreState.running:
            return
        device_id = event.data.get("device_id")
        if device_id:
            hass.async_create_task(coordinator.async_handle_device_change(device_id))

    @callback
    def _on_entity_registry_updated(event: Event) -> None:
        if hass.state is not CoreState.running:
            return
        entity_id = event.data.get("entity_id")
        if entity_id:
            hass.async_create_task(coordinator.async_handle_entity_change(entity_id))

    @callback
    def _on_state_changed(event: Event) -> None:
        if hass.state is CoreState.running:
            debouncer.async_handle_state_changed(event)

    unsub_area = hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, _on_area_registry_updated)
    unsub_device = hass.bus.async_listen(
        dr.EVENT_DEVICE_REGISTRY_UPDATED, _on_device_registry_updated
    )
    unsub_entity = hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, _on_entity_registry_updated
    )
    unsub_state = hass.bus.async_listen(EVENT_STATE_CHANGED, _on_state_changed)

    def _unsubscribe() -> None:
        unsub_area()
        unsub_device()
        unsub_entity()
        unsub_state()
        debouncer.async_cancel_all()

    return _unsubscribe
