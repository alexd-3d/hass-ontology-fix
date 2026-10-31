"""ON-040: the `mesh_history_keep_siblings` option in the options flow."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import voluptuous_serialize
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ontology.config_flow import _options_schema
from custom_components.ontology.const import (
    CONF_MESH_HISTORY_KEEP_SIBLINGS,
    CONF_MESH_SNAPSHOT_RETENTION_DAYS,
    DEFAULT_MESH_HISTORY_KEEP_SIBLINGS,
    DOMAIN,
)

COMPONENT = Path(__file__).parents[2] / "custom_components" / "ontology"


def _connection(**overrides: object) -> dict[str, object]:
    return {
        "host": "localhost",
        "port": 7687,
        "username": "",
        "password": "",
        "database": "",
        "encrypted": False,
        **overrides,
    }


def test_default_keeps_sibling_history() -> None:
    assert DEFAULT_MESH_HISTORY_KEEP_SIBLINGS is True
    assert _options_schema()(_connection())[CONF_MESH_HISTORY_KEEP_SIBLINGS] is True


def test_saved_value_is_the_form_default() -> None:
    saved = _connection(**{CONF_MESH_HISTORY_KEEP_SIBLINGS: False})

    assert _options_schema(saved)(_connection())[CONF_MESH_HISTORY_KEEP_SIBLINGS] is False


def test_option_is_serializable_and_labelled() -> None:
    serialized = voluptuous_serialize.convert(
        _options_schema(), custom_serializer=cv.custom_serializer
    )
    field = next(f for f in serialized if f["name"] == CONF_MESH_HISTORY_KEEP_SIBLINGS)
    assert field["type"] == "boolean"

    for name in ("strings.json", "translations/en.json"):
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        assert CONF_MESH_HISTORY_KEEP_SIBLINGS in data["options"]["step"]["init"]["data"]


async def _submit(hass, entry, **options: object) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    with (
        patch("custom_components.ontology.config_flow._validate_connection", AsyncMock()),
        patch.object(hass.config_entries, "async_reload", AsyncMock()),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**_connection(), **options}
        )
    assert result["type"] == FlowResultType.ABORT


async def test_option_is_persisted_and_survives_editing_another_option(hass) -> None:
    """ON-007 regression: options are written from an explicit list of keys, so
    a new key must be in it, and must not be reset by a later edit."""
    entry = MockConfigEntry(domain=DOMAIN, data=_connection(), options={})
    entry.add_to_hass(hass)

    await _submit(hass, entry, **{CONF_MESH_HISTORY_KEEP_SIBLINGS: False})
    assert entry.options[CONF_MESH_HISTORY_KEEP_SIBLINGS] is False

    # A later submission that does not mention the option keeps the saved value.
    await _submit(hass, entry, **{CONF_MESH_SNAPSHOT_RETENTION_DAYS: 14})
    assert entry.options[CONF_MESH_SNAPSHOT_RETENTION_DAYS] == 14
    assert entry.options[CONF_MESH_HISTORY_KEEP_SIBLINGS] is False

    await _submit(hass, entry, **{CONF_MESH_HISTORY_KEEP_SIBLINGS: True})
    assert entry.options[CONF_MESH_HISTORY_KEEP_SIBLINGS] is True
