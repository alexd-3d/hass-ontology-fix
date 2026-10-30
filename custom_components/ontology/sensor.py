"""Diagnostic sensors for the Ontology integration (contracts/diagnostics.md)."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import OntologyCoordinator, OntologyState


@dataclass(frozen=True, kw_only=True)
class OntologySensorEntityDescription(SensorEntityDescription):
    """Describes an Ontology diagnostic sensor."""

    value_fn: Callable[[OntologyState], object]
    attrs_fn: Callable[[OntologyState], dict[str, object]] | None = None
    # Set for sensors whose value is only produced by an occasional operation:
    # the coordinator state is in-memory, so after a restart the last known
    # duration (in ms) and attributes are put back from the previous HA state.
    restore_fn: Callable[[OntologyState, float, Mapping[str, object]], None] | None = None


def _ms_to_seconds(duration_ms: float | None) -> float | None:
    return None if duration_ms is None else round(duration_ms / 1000, 3)


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _restore_spatial_sync(
    state: OntologyState, duration_ms: float, attrs: Mapping[str, object]
) -> None:
    state.spatial_sync_last_duration_ms = duration_ms
    state.spatial_sync_last_walls = _as_int(attrs.get("walls"))
    state.spatial_sync_last_pinned_on_floor = _as_int(attrs.get("pinned_on_floor"))


def _restore_mesh_scan(
    state: OntologyState, duration_ms: float, attrs: Mapping[str, object]
) -> None:
    state.mesh_scan_last_duration_ms = duration_ms
    state.mesh_scan_last_links = _as_int(attrs.get("links"))


def _last_sync_datetime(state: OntologyState) -> datetime | None:
    """Parse ``state.last_sync`` (an ISO string, see coordinator._record_success)
    into an aware ``datetime``.

    A ``SensorDeviceClass.TIMESTAMP`` sensor's ``native_value`` must be a
    ``datetime`` object, not a string — returning the raw ISO string here
    causes Home Assistant to silently reject it and show the sensor as
    unavailable/"not provided", even after a successful sync.
    """
    if state.last_sync is None:
        return None
    return datetime.fromisoformat(state.last_sync)


SENSOR_DESCRIPTIONS: tuple[OntologySensorEntityDescription, ...] = (
    OntologySensorEntityDescription(
        key="health",
        translation_key="ontology_health",
        value_fn=lambda state: state.health,
    ),
    OntologySensorEntityDescription(
        key="nodes",
        translation_key="ontology_nodes",
        state_class="measurement",
        value_fn=lambda state: state.node_count,
    ),
    OntologySensorEntityDescription(
        key="relationships",
        translation_key="ontology_relationships",
        state_class="measurement",
        value_fn=lambda state: state.relationship_count,
    ),
    OntologySensorEntityDescription(
        key="last_sync",
        translation_key="ontology_last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_sync_datetime,
    ),
    OntologySensorEntityDescription(
        key="last_error",
        translation_key="ontology_last_error",
        value_fn=lambda state: state.last_error or "none",
    ),
    OntologySensorEntityDescription(
        key="schema_version",
        translation_key="ontology_schema_version",
        value_fn=lambda state: state.schema_version,
    ),
    # ON-004: state is the batch count (real graph writes) over the rolling
    # window; the raw event/pressure and coalescing-efficiency numbers live
    # in attributes rather than as separate entities, so this is the only
    # new sensor added for the whole feature (contracts/dashboard-guide.md).
    OntologySensorEntityDescription(
        key="sync_activity",
        translation_key="ontology_sync_activity",
        state_class="measurement",
        value_fn=lambda state: state.sync_activity_batches,
        attrs_fn=lambda state: {
            "events_debounced": state.sync_activity_events,
            "avg_batch_size": state.sync_activity_avg_batch_size,
            "last_batch_duration_ms": state.sync_activity_last_batch_ms,
        },
    ),
    # ON-015: unknown until the spatial-layout button/service has run (the last
    # value is restored across restarts). Reported in seconds; the coordinator
    # keeps milliseconds.
    OntologySensorEntityDescription(
        key="spatial_sync_duration",
        translation_key="ontology_spatial_sync_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=1,
        state_class="measurement",
        value_fn=lambda state: _ms_to_seconds(state.spatial_sync_last_duration_ms),
        attrs_fn=lambda state: {
            "walls": state.spatial_sync_last_walls,
            "pinned_on_floor": state.spatial_sync_last_pinned_on_floor,
        },
        restore_fn=_restore_spatial_sync,
    ),
    # ON-016: unknown until the first nightly or manual mesh scan (the last
    # value is restored across restarts).
    OntologySensorEntityDescription(
        key="mesh_scan_duration",
        translation_key="ontology_mesh_scan_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=1,
        state_class="measurement",
        value_fn=lambda state: _ms_to_seconds(state.mesh_scan_last_duration_ms),
        attrs_fn=lambda state: {"links": state.mesh_scan_last_links},
        restore_fn=_restore_mesh_scan,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Ontology diagnostic sensors."""
    coordinator: OntologyCoordinator = entry.runtime_data
    async_add_entities(
        OntologySensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class OntologySensor(CoordinatorEntity[OntologyCoordinator], RestoreEntity, SensorEntity):
    """A single ontology diagnostic sensor backed by the coordinator's state."""

    entity_description: OntologySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OntologyCoordinator,
        entry: ConfigEntry,
        description: OntologySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    async def async_added_to_hass(self) -> None:
        """Put back the last known duration after a restart (see ``restore_fn``)."""
        await super().async_added_to_hass()
        description = self.entity_description
        if description.restore_fn is None:
            return
        state = self.coordinator.state
        if description.value_fn(state) is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        try:
            value = float(last_state.state)
        except ValueError:
            return  # "unknown" / "unavailable"
        if not math.isfinite(value):
            return
        unit = last_state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        # Older releases reported milliseconds.
        if unit == UnitOfTime.MILLISECONDS:
            duration_ms = value
        elif unit == UnitOfTime.SECONDS:
            duration_ms = value * 1000
        else:
            return
        description.restore_fn(state, round(duration_ms, 1), last_state.attributes)

    @property
    def native_value(self) -> object:
        """Return the sensor's current value, read directly from coordinator state
        so it reflects the latest health/error even outside a refresh cycle."""
        return self.entity_description.value_fn(self.coordinator.state)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Optional supplementary detail (ON-004's sync_activity sensor uses
        this instead of adding further sensor entities per metric)."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.state)
