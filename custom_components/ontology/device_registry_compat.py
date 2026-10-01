"""Device registry access that avoids the deprecated ``devices`` mapping API.

Home Assistant deprecated using ``DeviceRegistry.devices`` as a mapping (``.get``,
``.values``) and will remove it in 2027.9.0. Lookups go through ``async_get``;
iteration goes through ``iter_devices``, which works both on older cores where
iterating ``devices`` yields device ids and on newer ones where it yields entries.
"""

from __future__ import annotations

from collections.abc import Iterator

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr


def get_device(hass: HomeAssistant, device_id: str) -> dr.DeviceEntry | None:
    """Return one device entry by id, or None when it no longer exists."""
    return dr.async_get(hass).async_get(device_id)


def iter_devices(hass: HomeAssistant) -> Iterator[dr.DeviceEntry]:
    """Yield every device entry in the registry."""
    registry = dr.async_get(hass)
    for item in list(registry.devices):
        if isinstance(item, dr.DeviceEntry):
            yield item
        elif (device := registry.async_get(item)) is not None:
            yield device
