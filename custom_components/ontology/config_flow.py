"""Config flow for the Ontology integration."""

from __future__ import annotations

import logging
import math
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import (
    CONF_ACTIVE_POWER_THRESHOLD,
    CONF_AUTO_CLASSIFY,
    CONF_DATABASE,
    CONF_ENCRYPTED,
    CONF_EXCLUDED_DOMAINS,
    CONF_COORDINATOR_DEVICE_ID,
    CONF_EXCLUDED_ENTITIES,
    CONF_FLOOR_HEIGHT_M,
    CONF_FLOOR_SLAB_ATTENUATION_DB,
    CONF_GRAPHQL_TOKEN,
    CONF_GRAPHQL_URL,
    CONF_HOST,
    CONF_LOW_BATTERY_THRESHOLD,
    CONF_MAX_MEASUREMENT_AGE_HOURS,
    CONF_MCP_ALLOWED_NETWORKS,
    CONF_MCP_ENABLED,
    CONF_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
    CONF_MESH_SNAPSHOT_RETENTION_DAYS,
    CONF_MESH_SNAPSHOT_SCAN_HOUR,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RELATIONSHIP_RESULT_LIMIT,
    CONF_STATE_CHANGE_DEBOUNCE_SECONDS,
    CONF_USERNAME,
    CONF_ZIGBEE2MQTT_BASE_TOPIC,
    DEFAULT_ACTIVE_POWER_THRESHOLD,
    DEFAULT_AUTO_CLASSIFY,
    DEFAULT_DATABASE,
    DEFAULT_ENCRYPTED,
    DEFAULT_EXCLUDED_DOMAINS,
    DEFAULT_EXCLUDED_ENTITIES,
    DEFAULT_FLOOR_HEIGHT_M,
    DEFAULT_FLOOR_SLAB_ATTENUATION_DB,
    DEFAULT_GRAPHQL_TOKEN,
    DEFAULT_GRAPHQL_URL,
    DEFAULT_LOW_BATTERY_THRESHOLD,
    DEFAULT_MAX_MEASUREMENT_AGE_HOURS,
    DEFAULT_MCP_ALLOWED_NETWORKS,
    DEFAULT_MCP_ENABLED,
    DEFAULT_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
    DEFAULT_MESH_SNAPSHOT_RETENTION_DAYS,
    DEFAULT_MESH_SNAPSHOT_SCAN_HOUR,
    DEFAULT_PORT,
    DEFAULT_RELATIONSHIP_RESULT_LIMIT,
    DEFAULT_STATE_CHANGE_DEBOUNCE_SECONDS,
    DEFAULT_ZIGBEE2MQTT_BASE_TOPIC,
    DOMAIN,
    MAX_FLOOR_HEIGHT_M,
    MAX_FLOOR_SLAB_ATTENUATION_DB,
    MAX_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
    MAX_MESH_SNAPSHOT_RETENTION_DAYS,
    MAX_RELATIONSHIP_RESULT_LIMIT,
    MAX_STATE_CHANGE_DEBOUNCE_SECONDS,
    MIN_FLOOR_HEIGHT_M,
    MIN_FLOOR_SLAB_ATTENUATION_DB,
    MIN_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
    MIN_MESH_SNAPSHOT_RETENTION_DAYS,
    MIN_STATE_CHANGE_DEBOUNCE_SECONDS,
)
from .memgraph_client import CannotConnect, InvalidAuth, MemgraphClient

_LOGGER = logging.getLogger(__name__)


class _FiniteFloat(vol.Coerce):
    """Coerce finite floats while remaining schema-serializer compatible."""

    def __init__(self) -> None:
        super().__init__(float)

    def __call__(self, value: object) -> float:
        number = super().__call__(value)
        if not math.isfinite(number):
            raise vol.Invalid("value must be finite")
        return number


_FINITE_FLOAT = _FiniteFloat()


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the connection-details schema, optionally pre-filled."""
    defaults = defaults or {}
    graphql_url = (
        vol.Optional(CONF_GRAPHQL_URL, default=defaults[CONF_GRAPHQL_URL])
        if CONF_GRAPHQL_URL in defaults
        else vol.Optional(CONF_GRAPHQL_URL)
    )
    graphql_token = (
        vol.Optional(CONF_GRAPHQL_TOKEN, default=defaults[CONF_GRAPHQL_TOKEN])
        if CONF_GRAPHQL_TOKEN in defaults
        else vol.Optional(CONF_GRAPHQL_TOKEN)
    )
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): int,
            vol.Optional(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")
            ): str,
            vol.Optional(
                CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")
            ): str,
            vol.Optional(
                CONF_DATABASE, default=defaults.get(CONF_DATABASE, DEFAULT_DATABASE)
            ): str,
            vol.Optional(
                CONF_ENCRYPTED, default=defaults.get(CONF_ENCRYPTED, DEFAULT_ENCRYPTED)
            ): bool,
            graphql_url: str,
            graphql_token: str,
        }
    )


async def _validate_connection(data: dict[str, Any]) -> None:
    """Attempt a single bounded-timeout connection check.

    Raises ``CannotConnect`` or ``InvalidAuth`` on failure (contracts/config-flow.md).
    """
    client = MemgraphClient(
        host=data[CONF_HOST],
        port=data[CONF_PORT],
        username=data.get(CONF_USERNAME) or None,
        password=data.get(CONF_PASSWORD) or None,
        database=data.get(CONF_DATABASE) or None,
        encrypted=data.get(CONF_ENCRYPTED, DEFAULT_ENCRYPTED),
    )
    try:
        await client.test_connection()
    finally:
        await client.close()


class OntologyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for the Ontology integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial `user` setup step (contracts/config-flow.md)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()
            errors = await self._async_try_connect(user_input)
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_HOST], data=user_input
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle updating an existing connection without a restart (FR-003)."""
        reconfigure_entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_try_connect(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data=user_input,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or dict(reconfigure_entry.data)),
            errors=errors,
        )

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle discovery of a Memgraph add-on via the Supervisor.

        The Memgraph add-on announces itself to the Supervisor Discovery API
        with ``service: "ontology"``, which Home Assistant routes here since
        it matches this integration's domain (see homeassistant.components.
        hassio.discovery). We never auto-create the entry from this step —
        the user must confirm via `async_step_hassio_confirm` first.
        """
        config = discovery_info.config
        host = config.get(CONF_HOST)
        port = config.get(CONF_PORT, DEFAULT_PORT)
        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured()
        self._discovery_data = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_GRAPHQL_URL: config.get(CONF_GRAPHQL_URL, DEFAULT_GRAPHQL_URL),
            CONF_GRAPHQL_TOKEN: config.get(CONF_GRAPHQL_TOKEN, DEFAULT_GRAPHQL_TOKEN),
        }
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup discovered from the Memgraph add-on."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self._discovery_data, **user_input}
            errors = await self._async_try_connect(data)
            if not errors:
                return self.async_create_entry(title=data[CONF_HOST], data=data)
            return self.async_show_form(
                step_id="hassio_confirm", data_schema=_schema(data), errors=errors
            )
        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=_schema(self._discovery_data),
            errors=errors,
        )

    async def _async_try_connect(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Validate connectivity, returning a `{field: error_key}` errors dict."""
        try:
            await _validate_connection(user_input)
        except InvalidAuth:
            return {"base": "invalid_auth"}
        except CannotConnect:
            return {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001 - normalize any unexpected driver error
            _LOGGER.exception("Unexpected error validating Memgraph connection")
            return {"base": "unknown"}
        return {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OntologyOptionsFlow()


def _options_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the connection-details schema plus the v2 auto-classify toggle
    (FR-004) and the v3 MCP-enabled toggle (FR-023, default off), pre-filled
    from `defaults` (entry data merged with options)."""
    defaults = defaults or {}
    schema_dict = dict(_schema(defaults).schema)
    schema_dict[
        vol.Optional(
            CONF_AUTO_CLASSIFY, default=defaults.get(CONF_AUTO_CLASSIFY, DEFAULT_AUTO_CLASSIFY)
        )
    ] = bool
    schema_dict[
        vol.Optional(
            CONF_MCP_ALLOWED_NETWORKS,
            default=defaults.get(CONF_MCP_ALLOWED_NETWORKS, DEFAULT_MCP_ALLOWED_NETWORKS),
        )
    ] = str
    schema_dict[
        vol.Optional(
            CONF_MCP_ENABLED, default=defaults.get(CONF_MCP_ENABLED, DEFAULT_MCP_ENABLED)
        )
    ] = bool
    schema_dict[
        vol.Optional(
            CONF_LOW_BATTERY_THRESHOLD,
            default=defaults.get(
                CONF_LOW_BATTERY_THRESHOLD, DEFAULT_LOW_BATTERY_THRESHOLD
            ),
        )
    ] = vol.All(_FINITE_FLOAT, vol.Range(min=1, max=100))
    schema_dict[
        vol.Optional(
            CONF_ACTIVE_POWER_THRESHOLD,
            default=defaults.get(
                CONF_ACTIVE_POWER_THRESHOLD, DEFAULT_ACTIVE_POWER_THRESHOLD
            ),
        )
    ] = vol.All(_FINITE_FLOAT, vol.Range(min=0))
    schema_dict[
        vol.Optional(
            CONF_MAX_MEASUREMENT_AGE_HOURS,
            default=defaults.get(
                CONF_MAX_MEASUREMENT_AGE_HOURS, DEFAULT_MAX_MEASUREMENT_AGE_HOURS
            ),
        )
    ] = vol.All(_FINITE_FLOAT, vol.Range(min=0, min_included=False))
    schema_dict[
        vol.Optional(
            CONF_RELATIONSHIP_RESULT_LIMIT,
            default=defaults.get(
                CONF_RELATIONSHIP_RESULT_LIMIT, DEFAULT_RELATIONSHIP_RESULT_LIMIT
            ),
        )
    ] = vol.All(int, vol.Range(min=1, max=MAX_RELATIONSHIP_RESULT_LIMIT))
    # ON-002: state_changed batch-debounce window and domain/entity excludes.
    schema_dict[
        vol.Optional(
            CONF_STATE_CHANGE_DEBOUNCE_SECONDS,
            default=defaults.get(
                CONF_STATE_CHANGE_DEBOUNCE_SECONDS, DEFAULT_STATE_CHANGE_DEBOUNCE_SECONDS
            ),
        )
    ] = vol.All(
        _FINITE_FLOAT,
        vol.Range(min=MIN_STATE_CHANGE_DEBOUNCE_SECONDS, max=MAX_STATE_CHANGE_DEBOUNCE_SECONDS),
    )
    schema_dict[
        vol.Optional(
            CONF_EXCLUDED_DOMAINS,
            default=defaults.get(CONF_EXCLUDED_DOMAINS, DEFAULT_EXCLUDED_DOMAINS),
        )
    ] = str
    schema_dict[
        vol.Optional(
            CONF_EXCLUDED_ENTITIES,
            default=defaults.get(CONF_EXCLUDED_ENTITIES, DEFAULT_EXCLUDED_ENTITIES),
        )
    ] = str
    # ON-016: Zigbee mesh snapshot options.
    schema_dict[
        vol.Optional(
            CONF_ZIGBEE2MQTT_BASE_TOPIC,
            default=defaults.get(CONF_ZIGBEE2MQTT_BASE_TOPIC, DEFAULT_ZIGBEE2MQTT_BASE_TOPIC),
        )
    ] = str
    schema_dict[
        vol.Optional(
            CONF_MESH_SNAPSHOT_RETENTION_DAYS,
            default=defaults.get(
                CONF_MESH_SNAPSHOT_RETENTION_DAYS, DEFAULT_MESH_SNAPSHOT_RETENTION_DAYS
            ),
        )
    ] = vol.All(
        vol.Coerce(int),
        vol.Range(min=MIN_MESH_SNAPSHOT_RETENTION_DAYS, max=MAX_MESH_SNAPSHOT_RETENTION_DAYS),
    )
    schema_dict[
        vol.Optional(
            CONF_MESH_SNAPSHOT_SCAN_HOUR,
            default=defaults.get(CONF_MESH_SNAPSHOT_SCAN_HOUR, DEFAULT_MESH_SNAPSHOT_SCAN_HOUR),
        )
    ] = str
    schema_dict[
        vol.Optional(
            CONF_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
            default=defaults.get(
                CONF_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
                DEFAULT_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
            ),
        )
    ] = vol.All(
        _FINITE_FLOAT,
        vol.Range(
            min=MIN_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
            max=MAX_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
        ),
    )
    # ON-020: optional - which HA device physically holds the coordinator radio.
    schema_dict[
        vol.Optional(
            CONF_COORDINATOR_DEVICE_ID,
            description={"suggested_value": defaults.get(CONF_COORDINATOR_DEVICE_ID)},
        )
    ] = selector.DeviceSelector()
    # ON-019: geometry used to explain weak Zigbee links between floors.
    schema_dict[
        vol.Optional(
            CONF_FLOOR_HEIGHT_M,
            default=defaults.get(CONF_FLOOR_HEIGHT_M, DEFAULT_FLOOR_HEIGHT_M),
        )
    ] = vol.All(_FINITE_FLOAT, vol.Range(min=MIN_FLOOR_HEIGHT_M, max=MAX_FLOOR_HEIGHT_M))
    schema_dict[
        vol.Optional(
            CONF_FLOOR_SLAB_ATTENUATION_DB,
            default=defaults.get(
                CONF_FLOOR_SLAB_ATTENUATION_DB, DEFAULT_FLOOR_SLAB_ATTENUATION_DB
            ),
        )
    ] = vol.All(
        _FINITE_FLOAT,
        vol.Range(min=MIN_FLOOR_SLAB_ATTENUATION_DB, max=MAX_FLOOR_SLAB_ATTENUATION_DB),
    )
    return vol.Schema(schema_dict)


class OntologyOptionsFlow(OptionsFlow):
    """Options flow mirroring the reconfigure step (FR-003), plus the v2
    automatic semantic classification on/off toggle (FR-004)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle updating the connection and auto-classify option."""
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            connection_data = {
                key: value
                for key, value in user_input.items()
                if key not in (
                    CONF_AUTO_CLASSIFY,
                    CONF_MCP_ENABLED,
                    CONF_MCP_ALLOWED_NETWORKS,
                    CONF_LOW_BATTERY_THRESHOLD,
                    CONF_ACTIVE_POWER_THRESHOLD,
                    CONF_MAX_MEASUREMENT_AGE_HOURS,
                    CONF_RELATIONSHIP_RESULT_LIMIT,
                    CONF_STATE_CHANGE_DEBOUNCE_SECONDS,
                    CONF_EXCLUDED_DOMAINS,
                    CONF_EXCLUDED_ENTITIES,
                    CONF_ZIGBEE2MQTT_BASE_TOPIC,
                    CONF_MESH_SNAPSHOT_RETENTION_DAYS,
                    CONF_MESH_SNAPSHOT_SCAN_HOUR,
                    CONF_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
                    CONF_FLOOR_HEIGHT_M,
                    CONF_FLOOR_SLAB_ATTENUATION_DB,
                    CONF_COORDINATOR_DEVICE_ID,
                )
            }
            try:
                await _validate_connection(connection_data)
            except InvalidAuth:
                errors = {"base": "invalid_auth"}
            except CannotConnect:
                errors = {"base": "cannot_connect"}
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating Memgraph connection")
                errors = {"base": "unknown"}
            if not errors:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=connection_data,
                    options={
                        CONF_AUTO_CLASSIFY: user_input.get(
                            CONF_AUTO_CLASSIFY, DEFAULT_AUTO_CLASSIFY
                        ),
                        CONF_MCP_ENABLED: user_input.get(
                            CONF_MCP_ENABLED, DEFAULT_MCP_ENABLED
                        ),
                        CONF_MCP_ALLOWED_NETWORKS: user_input.get(
                            CONF_MCP_ALLOWED_NETWORKS, DEFAULT_MCP_ALLOWED_NETWORKS
                        ),
                        CONF_LOW_BATTERY_THRESHOLD: user_input.get(
                            CONF_LOW_BATTERY_THRESHOLD, DEFAULT_LOW_BATTERY_THRESHOLD
                        ),
                        CONF_ACTIVE_POWER_THRESHOLD: user_input.get(
                            CONF_ACTIVE_POWER_THRESHOLD, DEFAULT_ACTIVE_POWER_THRESHOLD
                        ),
                        CONF_MAX_MEASUREMENT_AGE_HOURS: user_input.get(
                            CONF_MAX_MEASUREMENT_AGE_HOURS,
                            DEFAULT_MAX_MEASUREMENT_AGE_HOURS,
                        ),
                        CONF_RELATIONSHIP_RESULT_LIMIT: user_input.get(
                            CONF_RELATIONSHIP_RESULT_LIMIT,
                            DEFAULT_RELATIONSHIP_RESULT_LIMIT,
                        ),
                        CONF_STATE_CHANGE_DEBOUNCE_SECONDS: user_input.get(
                            CONF_STATE_CHANGE_DEBOUNCE_SECONDS,
                            DEFAULT_STATE_CHANGE_DEBOUNCE_SECONDS,
                        ),
                        CONF_EXCLUDED_DOMAINS: user_input.get(
                            CONF_EXCLUDED_DOMAINS, DEFAULT_EXCLUDED_DOMAINS
                        ),
                        CONF_EXCLUDED_ENTITIES: user_input.get(
                            CONF_EXCLUDED_ENTITIES, DEFAULT_EXCLUDED_ENTITIES
                        ),
                        CONF_ZIGBEE2MQTT_BASE_TOPIC: user_input.get(
                            CONF_ZIGBEE2MQTT_BASE_TOPIC, DEFAULT_ZIGBEE2MQTT_BASE_TOPIC
                        ),
                        CONF_MESH_SNAPSHOT_RETENTION_DAYS: user_input.get(
                            CONF_MESH_SNAPSHOT_RETENTION_DAYS,
                            DEFAULT_MESH_SNAPSHOT_RETENTION_DAYS,
                        ),
                        CONF_MESH_SNAPSHOT_SCAN_HOUR: user_input.get(
                            CONF_MESH_SNAPSHOT_SCAN_HOUR, DEFAULT_MESH_SNAPSHOT_SCAN_HOUR
                        ),
                        CONF_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS: user_input.get(
                            CONF_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
                            DEFAULT_MESH_SNAPSHOT_RESPONSE_TIMEOUT_SECONDS,
                        ),
                        CONF_COORDINATOR_DEVICE_ID: user_input.get(CONF_COORDINATOR_DEVICE_ID),
                        CONF_FLOOR_HEIGHT_M: user_input.get(
                            CONF_FLOOR_HEIGHT_M, DEFAULT_FLOOR_HEIGHT_M
                        ),
                        CONF_FLOOR_SLAB_ATTENUATION_DB: user_input.get(
                            CONF_FLOOR_SLAB_ATTENUATION_DB, DEFAULT_FLOOR_SLAB_ATTENUATION_DB
                        ),
                    },
                )
                await self.hass.config_entries.async_reload(
                    self.config_entry.entry_id
                )
                # ON-007: must NOT return async_create_entry() here. When an
                # OptionsFlow step returns CREATE_ENTRY, HA's own
                # OptionsFlowManager unconditionally does its own
                # `async_update_entry(entry, options=result["data"])`
                # afterwards - with `data={}` that immediately clobbered the
                # real options we just persisted above back to an empty
                # dict, and (since an update listener is registered in
                # __init__.py) fired a *second*, redundant reload racing
                # the one above. That race is the most likely source of the
                # intermittent "Cannot resolve conflicting transactions"
                # errors seen in the field. async_abort() completes the
                # flow (HA shows "Successfully reconfigured") without
                # triggering that automatic post-processing, since we
                # already persisted data+options and reloaded ourselves.
                return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(user_input or current),
            errors=errors,
        )
