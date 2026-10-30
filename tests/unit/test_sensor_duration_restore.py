"""Test: the spatial-sync and mesh-scan duration sensors report seconds and put
back their last known value after a restart, because the coordinator state they
read from is in-memory only."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ontology.const import DOMAIN
from custom_components.ontology.coordinator import OntologyCoordinator
from custom_components.ontology.sensor import SENSOR_DESCRIPTIONS, OntologySensor


COMPONENT_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "ontology"


def _build_sensors(hass, mock_memgraph_client):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = OntologyCoordinator(hass, entry, mock_memgraph_client)
    sensors = {
        description.key: OntologySensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    }
    return coordinator, sensors


async def _added_with_last_state(sensor: OntologySensor, last_state: State | None) -> None:
    with patch.object(sensor, "async_get_last_state", AsyncMock(return_value=last_state)):
        await sensor.async_added_to_hass()


def test_duration_sensors_are_duration_class_in_seconds(hass, mock_memgraph_client) -> None:
    _coordinator, sensors = _build_sensors(hass, mock_memgraph_client)

    for key in ("spatial_sync_duration", "mesh_scan_duration"):
        description = sensors[key].entity_description
        assert description.device_class == SensorDeviceClass.DURATION
        assert description.native_unit_of_measurement == "s"


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json"])
def test_duration_sensor_names_stay_short(path) -> None:
    translations = json.loads((COMPONENT_DIR / path).read_text(encoding="utf-8"))
    names = translations["entity"]["sensor"]

    assert names["ontology_spatial_sync_duration"]["name"] == "Spatial sync duration"
    assert names["ontology_mesh_scan_duration"]["name"] == "Mesh scan duration"


async def test_duration_sensors_report_seconds(hass, mock_memgraph_client) -> None:
    coordinator, sensors = _build_sensors(hass, mock_memgraph_client)
    coordinator.state.mesh_scan_last_duration_ms = 133670.8
    coordinator.state.spatial_sync_last_duration_ms = 250.0

    assert sensors["mesh_scan_duration"].native_value == 133.671
    assert sensors["spatial_sync_duration"].native_value == 0.25


async def test_duration_sensors_are_unknown_and_attributes_empty_before_first_run(
    hass, mock_memgraph_client
) -> None:
    _coordinator, sensors = _build_sensors(hass, mock_memgraph_client)

    assert sensors["mesh_scan_duration"].native_value is None
    assert sensors["mesh_scan_duration"].extra_state_attributes == {"links": None}
    assert sensors["spatial_sync_duration"].native_value is None
    assert sensors["spatial_sync_duration"].extra_state_attributes == {
        "walls": None,
        "pinned_on_floor": None,
    }


async def test_mesh_scan_restores_previous_release_milliseconds_state(
    hass, mock_memgraph_client
) -> None:
    coordinator, sensors = _build_sensors(hass, mock_memgraph_client)

    await _added_with_last_state(
        sensors["mesh_scan_duration"],
        State(
            "sensor.mesh_scan_duration",
            "133670.8",
            {"unit_of_measurement": "ms", "links": 1376},
        ),
    )

    assert coordinator.state.mesh_scan_last_duration_ms == 133670.8
    assert coordinator.state.mesh_scan_last_links == 1376
    assert sensors["mesh_scan_duration"].native_value == 133.671
    assert sensors["mesh_scan_duration"].extra_state_attributes == {"links": 1376}


async def test_spatial_sync_restores_seconds_state(hass, mock_memgraph_client) -> None:
    coordinator, sensors = _build_sensors(hass, mock_memgraph_client)

    await _added_with_last_state(
        sensors["spatial_sync_duration"],
        State(
            "sensor.spatial_sync_duration",
            "0.25",
            {"unit_of_measurement": "s", "walls": 14, "pinned_on_floor": 57},
        ),
    )

    assert coordinator.state.spatial_sync_last_duration_ms == 250.0
    assert coordinator.state.spatial_sync_last_walls == 14
    assert coordinator.state.spatial_sync_last_pinned_on_floor == 57
    assert sensors["spatial_sync_duration"].native_value == 0.25


@pytest.mark.parametrize(
    "last_state",
    [
        None,
        State("sensor.mesh_scan_duration", "unknown", {"unit_of_measurement": "ms"}),
        State("sensor.mesh_scan_duration", "unavailable", {}),
        State("sensor.mesh_scan_duration", "nan", {"unit_of_measurement": "s"}),
        State("sensor.mesh_scan_duration", "5", {"unit_of_measurement": "min"}),
        State("sensor.mesh_scan_duration", "5", {}),
    ],
)
async def test_unusable_last_state_leaves_sensor_unknown(
    hass, mock_memgraph_client, last_state
) -> None:
    coordinator, sensors = _build_sensors(hass, mock_memgraph_client)

    await _added_with_last_state(sensors["mesh_scan_duration"], last_state)

    assert coordinator.state.mesh_scan_last_duration_ms is None
    assert sensors["mesh_scan_duration"].native_value is None


async def test_restore_does_not_override_a_fresh_measurement(hass, mock_memgraph_client) -> None:
    coordinator, sensors = _build_sensors(hass, mock_memgraph_client)
    coordinator.state.mesh_scan_last_duration_ms = 1000.0
    coordinator.state.mesh_scan_last_links = 5

    await _added_with_last_state(
        sensors["mesh_scan_duration"],
        State("sensor.mesh_scan_duration", "133.7", {"unit_of_measurement": "s", "links": 1376}),
    )

    assert coordinator.state.mesh_scan_last_duration_ms == 1000.0
    assert coordinator.state.mesh_scan_last_links == 5


async def test_sensors_without_restore_fn_ignore_last_state(hass, mock_memgraph_client) -> None:
    coordinator, sensors = _build_sensors(hass, mock_memgraph_client)

    await _added_with_last_state(sensors["nodes"], State("sensor.nodes", "99", {}))

    assert coordinator.state.node_count != 99
