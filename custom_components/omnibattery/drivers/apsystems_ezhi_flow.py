"""APsystems EZHI config flow: one connection step, capacity read from the device.

Modelled on sessy_flow.py. Imports only ``drivers.setup``, ``const`` and Home
Assistant. ``apsystems_ezhi.py`` imports ``EZHI_SETUP`` from here to set
``ApsystemsEzhiDriver.SETUP``, so this module must not import the driver at the
top (cycle): handlers import it inside the function.

Unlike Sessy there are no credentials (the EZHI local API is open once "Local
Mode" is enabled in the AP EasyPower app), and the battery capacity is read from
/getDeviceInfo, so the user is only asked for a capacity if the device reports none.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from ..const import (
    CONF_DC_PV_CONNECTED,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
)
from .setup import DriverSetup, FlowKind, SetupContext, saved_dc_pv_connected

_LOGGER = logging.getLogger(__name__)

BRAND = "apsystems_ezhi"
# Step ids are "battery_connection_<brand>" / "reconfigure_battery_<brand>"; they
# must match the keys added to strings.json and every translations/*.json.
STEP_CONNECTION = "battery_connection_apsystems_ezhi"
STEP_RECONFIGURE = "reconfigure_battery_apsystems_ezhi"

CAPACITY_KEY = "battery_capacity_kwh"
_EZHI_MAX_CHARGE_POWER_W = 1200
_EZHI_MAX_DISCHARGE_POWER_W = 1200


async def _probe(host: str, port: int) -> bool:
    from .apsystems_ezhi import ApsystemsEzhiDriver  # local: driver imports this module

    _LOGGER.info("Probing APsystems EZHI at %s:%s", host, port)
    return await ApsystemsEzhiDriver.probe(host, port)


async def _device_capacity(host: str, port: int) -> float | None:
    """Best-effort capacity (kWh) from /getDeviceInfo; None if unavailable."""
    from .apsystems_ezhi import ApsystemsEzhiDriver

    info = await ApsystemsEzhiDriver.fetch_info(host, port)
    cap = (info or {}).get("capacity_kwh")
    return float(cap) if cap else None


async def _battery_config(user_input: dict[str, Any], current: dict) -> dict[str, Any]:
    host = user_input[CONF_HOST]
    port = int(user_input.get(CONF_PORT, 80))
    config: dict[str, Any] = {
        CONF_NAME: user_input[CONF_NAME],
        CONF_HOST: host,
        CONF_PORT: port,
        "brand": BRAND,
    }
    capacity = await _device_capacity(host, port) or current.get(CAPACITY_KEY)
    if capacity:
        config[CAPACITY_KEY] = round(float(capacity), 2)
    return config


def _schema(current: dict, default_name: str, port_required: bool) -> vol.Schema:
    port_marker = vol.Required if port_required else vol.Optional
    if current:
        return vol.Schema({
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, default_name)): str,
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
            port_marker(CONF_PORT, default=current.get(CONF_PORT, 80)): int,
        })
    return vol.Schema({
        vol.Required(CONF_NAME, default=default_name): str,
        vol.Required(CONF_HOST): str,
        port_marker(CONF_PORT, default=80): int,
    })


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Add (config flow) or edit (options flow) an EZHI through its local HTTP API."""
    errors = {}
    battery_num = flow.battery_index + 1
    options = ctx.kind is FlowKind.OPTIONS
    current = (ctx.battery or {}) if options else {}

    if user_input is not None:
        host = user_input[CONF_HOST]
        port = int(user_input.get(CONF_PORT, 80))
        if await _probe(host, port):
            return await _battery_config(user_input, current)
        errors["base"] = "cannot_connect"

    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=_schema(current, f"EZHI {battery_num}", port_required=False),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Update an EZHI's connection settings (config flow, reconfigure source)."""
    current = ctx.battery or {}
    battery_num = flow.battery_index + 1
    errors = {}

    if user_input is not None:
        new_host = user_input[CONF_HOST]
        new_port = int(user_input.get(CONF_PORT, 80))
        if not await _probe(new_host, new_port):
            errors["base"] = "cannot_connect"
        else:
            old_host = current.get(CONF_HOST)
            old_port = current.get(CONF_PORT)
            if old_host and old_port and (old_host != new_host or old_port != new_port):
                flow._migrate_battery_registry_ids(
                    ctx.entry, old_host, old_port, new_host, new_port
                )
            return await _battery_config(user_input, current)

    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=_schema(current or {CONF_HOST: ""}, f"EZHI {battery_num}", port_required=True),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


class _EzhiSetup(DriverSetup):
    async def reprobe(self, hass: Any, battery_config: dict, host: str) -> bool:
        return await _probe(host, battery_config.get(CONF_PORT) or 80)

    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        return _EZHI_MAX_CHARGE_POWER_W, _EZHI_MAX_DISCHARGE_POWER_W

    def limits_schema(self, battery_data: dict, base_fields: dict, ctx: SetupContext) -> dict:
        """Base fields only when the device reported its capacity.

        If /getDeviceInfo returned no capacity (and none is stored), fall back to
        asking for it, as Sessy does.
        """
        fields = dict(base_fields)
        known = float(
            (ctx.battery or {}).get(CAPACITY_KEY) or battery_data.get(CAPACITY_KEY) or 0.0
        )
        if known > 0:
            return fields
        fields[vol.Required(CAPACITY_KEY)] = NumberSelector(
            NumberSelectorConfig(
                min=0.01, max=100, step=0.01, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX
            )
        )
        return fields

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        from .apsystems_ezhi import ApsystemsEzhiDriver

        merged[CONF_DC_PV_CONNECTED] = saved_dc_pv_connected(ApsystemsEzhiDriver, merged, user_input)
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = False
        capacity = (
            user_input.get(CAPACITY_KEY)
            or merged.get(CAPACITY_KEY)
            or battery_data.get(CAPACITY_KEY)
            or (ctx.battery or {}).get(CAPACITY_KEY)
        )
        if capacity:
            merged[CAPACITY_KEY] = round(float(capacity), 2)


EZHI_SETUP = _EzhiSetup(
    steps={STEP_CONNECTION: _connection, STEP_RECONFIGURE: _reconfigure},
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
