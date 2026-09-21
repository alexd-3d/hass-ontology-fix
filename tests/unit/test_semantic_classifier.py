"""Unit tests for semantic classification (User Story 1) and on-demand
refresh (User Story 6).

Covers: T006 (rule matching for all 8 semantic types, including an entity
matching more than one rule), T007 (classification never overwrites an
existing user override), and T042 (`refresh_semantics` recalculates all
entities or a single `entity_id`, preserving user overrides)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ontology import semantic_classifier
from custom_components.ontology.const import (
    LABEL_BATTERY_POWERED_DEVICE,
    LABEL_CAMERA,
    LABEL_CLIMATE_DEVICE,
    LABEL_ENERGY_ASSET,
    LABEL_GAS_CYLINDER,
    LABEL_NETWORK_DEVICE,
    LABEL_OCCUPANCY_SENSOR,
    LABEL_SECURITY_DEVICE,
    LABEL_VEHICLE,
    REL_CLASSIFIED_AS,
)


def _matched_labels(hass, entity_id: str) -> set[str]:
    return {rule.label for rule in semantic_classifier.matching_rules(hass, entity_id)}


def _add_reolink_camera_entity(hass, entity_id: str, *, device_class: str) -> None:
    """ON-011 helper: registers `entity_id` on a device with
    manufacturer="Reolink", the signal LABEL_CAMERA's rule matches on."""
    domain, object_id = entity_id.split(".", 1)
    config_entry = MockConfigEntry(domain="reolink")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("reolink", f"{object_id}-device")},
        manufacturer="Reolink",
        name="Front",
    )
    er.async_get(hass).async_get_or_create(
        domain,
        "reolink",
        f"{object_id}-unique-id",
        suggested_object_id=object_id,
        device_id=device.id,
    )
    hass.states.async_set(entity_id, "off", {"device_class": device_class})


async def test_gas_cylinder_classification(hass) -> None:
    hass.states.async_set("sensor.gas_meter", "42", {"device_class": "gas"})
    assert LABEL_GAS_CYLINDER in _matched_labels(hass, "sensor.gas_meter")


async def test_vehicle_classification(hass) -> None:
    hass.states.async_set("device_tracker.my_car", "home", {"friendly_name": "My Car"})
    assert LABEL_VEHICLE in _matched_labels(hass, "device_tracker.my_car")


async def test_energy_asset_classification(hass) -> None:
    hass.states.async_set("sensor.inverter_output", "1200", {"device_class": "power"})
    assert LABEL_ENERGY_ASSET in _matched_labels(hass, "sensor.inverter_output")


async def test_security_device_classification(hass) -> None:
    hass.states.async_set("lock.front_door", "locked", {"device_class": "lock"})
    assert LABEL_SECURITY_DEVICE in _matched_labels(hass, "lock.front_door")


async def test_occupancy_sensor_classification(hass) -> None:
    hass.states.async_set("binary_sensor.hallway_motion", "off", {"device_class": "motion"})
    assert LABEL_OCCUPANCY_SENSOR in _matched_labels(hass, "binary_sensor.hallway_motion")


async def test_climate_device_classification(hass) -> None:
    hass.states.async_set("climate.living_room", "heat", {})
    assert LABEL_CLIMATE_DEVICE in _matched_labels(hass, "climate.living_room")


async def test_network_device_classification(hass) -> None:
    hass.states.async_set("device_tracker.router", "home", {})
    assert LABEL_NETWORK_DEVICE in _matched_labels(hass, "device_tracker.router")


async def test_battery_powered_device_classification(hass) -> None:
    hass.states.async_set("sensor.node_battery", "80", {"device_class": "battery"})
    assert LABEL_BATTERY_POWERED_DEVICE in _matched_labels(hass, "sensor.node_battery")


async def test_entity_can_match_more_than_one_rule(hass) -> None:
    """FR-005: an entity is not limited to a single semantic type."""
    hass.states.async_set(
        "sensor.solar_battery",
        "50",
        {"device_class": "battery", "friendly_name": "Solar Battery Sensor"},
    )
    labels = _matched_labels(hass, "sensor.solar_battery")
    assert LABEL_ENERGY_ASSET in labels
    assert LABEL_BATTERY_POWERED_DEVICE in labels


async def test_camera_classification_by_manufacturer(hass) -> None:
    """ON-011: an entity on a Reolink-manufactured device is tagged Camera
    even though nothing in its domain/device_class/name says "camera"."""
    _add_reolink_camera_entity(hass, "sensor.front_firmware", device_class=None)
    assert LABEL_CAMERA in _matched_labels(hass, "sensor.front_firmware")


async def test_reolink_ai_sensor_not_tagged_vehicle(hass) -> None:
    """ON-011: Reolink's AI object-detection binary_sensor is literally named
    "*_vehicle" (front_vehicle) but is a camera-detection event, not an
    actual vehicle - it should get Camera (and still OccupancySensor, since
    device_class=motion is genuinely true), never the old Vehicle tag."""
    _add_reolink_camera_entity(hass, "binary_sensor.front_vehicle", device_class="motion")
    labels = _matched_labels(hass, "binary_sensor.front_vehicle")
    assert LABEL_VEHICLE not in labels
    assert LABEL_OCCUPANCY_SENSOR in labels
    assert LABEL_CAMERA in labels


async def test_vehicle_classification_unaffected_for_non_reolink_devices(hass) -> None:
    """ON-011: the manufacturer exclusion is scoped to Reolink only - a
    genuine device_tracker "car" entity on unrelated hardware still gets
    Vehicle, same as test_vehicle_classification above."""
    hass.states.async_set("device_tracker.my_car", "home", {"friendly_name": "My Car"})
    assert LABEL_VEHICLE in _matched_labels(hass, "device_tracker.my_car")


def _classified_as_calls(mock_client: AsyncMock, from_ha_id: str) -> list:
    return [
        call
        for call in mock_client.run_query_with_retry.call_args_list
        if REL_CLASSIFIED_AS in call.args[0] and call.args[1].get("from_ha_id") == from_ha_id
    ]


async def test_classification_never_overwrites_existing_user_override(
    hass, mock_memgraph_client
) -> None:
    """FR-006: an entity/type pair with an existing `source = "user"`
    override is skipped entirely (no MERGE calls for that pair), while an
    unrelated entity with no override is still classified normally."""
    er.async_get(hass).async_get_or_create(
        "sensor", "demo", "gas-meter-1", suggested_object_id="gas_meter"
    )
    er.async_get(hass).async_get_or_create(
        "device_tracker", "demo", "my-car-1", suggested_object_id="my_car"
    )
    hass.states.async_set("sensor.gas_meter", "42", {"device_class": "gas"})
    hass.states.async_set("device_tracker.my_car", "home", {"friendly_name": "My Car"})

    async def run_query_side_effect(query, params=None):
        params = params or {}
        if params.get("entity_id") == "sensor.gas_meter" and params.get(
            "type_label"
        ) == LABEL_GAS_CYLINDER:
            return [{"c": 1}]
        return [{"c": 0}]

    mock_memgraph_client.run_query = AsyncMock(side_effect=run_query_side_effect)

    await semantic_classifier.async_classify_entities(hass, mock_memgraph_client)

    assert _classified_as_calls(mock_memgraph_client, "sensor.gas_meter") == []
    assert _classified_as_calls(mock_memgraph_client, "device_tracker.my_car") != []


def _asset_node_merge_calls(mock_client: AsyncMock, asset_id: str) -> list:
    """MERGE calls that created/updated the per-entity asset node itself
    (keyed by its `<entity_id>::<Label>` ha_id) - distinct from the shared
    SemanticType catalog node and from the CLASSIFIED_AS/back-relationship
    MERGE calls, which use different ha_id/query shapes."""
    return [
        call
        for call in mock_client.run_query_with_retry.call_args_list
        if call.args[1].get("ha_id") == asset_id
    ]


async def test_asset_node_gets_entity_friendly_name(hass, mock_memgraph_client) -> None:
    """ON-010: the per-entity asset node (`sensor.gas_meter::GasCylinder`)
    used to be merged with no properties at all, so the Explorer had
    nothing to show but the raw ha_id. It should now carry the entity's
    friendly_name, the same fallback a real Entity node uses."""
    hass.states.async_set(
        "sensor.gas_meter", "42", {"device_class": "gas", "friendly_name": "Kitchen Gas Meter"}
    )

    await semantic_classifier.async_refresh_semantics(
        hass, mock_memgraph_client, entity_id="sensor.gas_meter"
    )

    asset_id = semantic_classifier.semantic_ha_id("sensor.gas_meter", LABEL_GAS_CYLINDER)
    calls = _asset_node_merge_calls(mock_memgraph_client, asset_id)
    assert calls, "expected a MERGE call for the asset node"
    assert calls[0].args[1]["properties"]["name"] == "Kitchen Gas Meter"


async def test_asset_node_falls_back_to_entity_id_without_friendly_name(
    hass, mock_memgraph_client
) -> None:
    """Same fallback order as a real Entity node: no friendly_name means the
    bare entity_id, never an empty/missing name."""
    hass.states.async_set("sensor.gas_meter", "42", {"device_class": "gas"})

    await semantic_classifier.async_refresh_semantics(
        hass, mock_memgraph_client, entity_id="sensor.gas_meter"
    )

    asset_id = semantic_classifier.semantic_ha_id("sensor.gas_meter", LABEL_GAS_CYLINDER)
    calls = _asset_node_merge_calls(mock_memgraph_client, asset_id)
    assert calls[0].args[1]["properties"]["name"] == "sensor.gas_meter"


async def test_refresh_semantics_scoped_to_single_entity(hass, mock_memgraph_client) -> None:
    """T042: `refresh_semantics(entity_id=...)` only recalculates that one
    entity, leaving other entities' classification untouched by this call."""
    hass.states.async_set("sensor.gas_meter", "42", {"device_class": "gas"})
    hass.states.async_set("device_tracker.my_car", "home", {"friendly_name": "My Car"})

    matched = await semantic_classifier.async_refresh_semantics(
        hass, mock_memgraph_client, entity_id="sensor.gas_meter"
    )

    assert matched == 1
    assert _classified_as_calls(mock_memgraph_client, "sensor.gas_meter") != []
    assert _classified_as_calls(mock_memgraph_client, "device_tracker.my_car") == []


async def test_refresh_semantics_without_entity_id_classifies_everything(
    hass, mock_memgraph_client
) -> None:
    """T042: `refresh_semantics(entity_id=None)` re-runs the full-graph pass."""
    er.async_get(hass).async_get_or_create(
        "sensor", "demo", "gas-meter-1", suggested_object_id="gas_meter"
    )
    er.async_get(hass).async_get_or_create(
        "device_tracker", "demo", "my-car-1", suggested_object_id="my_car"
    )
    hass.states.async_set("sensor.gas_meter", "42", {"device_class": "gas"})
    hass.states.async_set("device_tracker.my_car", "home", {"friendly_name": "My Car"})

    matched = await semantic_classifier.async_refresh_semantics(hass, mock_memgraph_client)

    assert matched == 2
    assert _classified_as_calls(mock_memgraph_client, "sensor.gas_meter") != []
    assert _classified_as_calls(mock_memgraph_client, "device_tracker.my_car") != []
