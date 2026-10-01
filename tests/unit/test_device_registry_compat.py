"""Unit tests for `device_registry_compat.py` (ON-032)."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ontology.device_registry_compat import get_device, iter_devices


def _add_device(hass, identifier: str) -> dr.DeviceEntry:
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", identifier)}
    )


def test_get_device_returns_entry_by_id(hass) -> None:
    device = _add_device(hass, "a")

    assert get_device(hass, device.id) == device


def test_get_device_returns_none_for_unknown_id(hass) -> None:
    assert get_device(hass, "missing") is None


def test_iter_devices_yields_every_device_entry(hass) -> None:
    first = _add_device(hass, "a")
    second = _add_device(hass, "b")

    devices = list(iter_devices(hass))

    assert {device.id for device in devices} == {first.id, second.id}
    assert all(isinstance(device, dr.DeviceEntry) for device in devices)


def test_iter_devices_is_empty_without_devices(hass) -> None:
    assert list(iter_devices(hass)) == []
