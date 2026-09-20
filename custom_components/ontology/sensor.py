"""Diagnostic sensors for the Ontology integration (contracts/diagnostics.md)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import OntologyCoordinator, OntologyState


@dataclass(frozen=True, kw_only=True)
class OntologySensorEntityDescription(SensorEntityDescription):
    """Describes an Ontology diagnostic sensor."""

    value_fn: Callable[[OntologyState], object]
    attrs_fn: Callable[[OntologyState], dict[str, object]] | None = None


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


class OntologySensor(CoordinatorEntity[OntologyCoordinator], SensorEntity):
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
