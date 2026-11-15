"""Sessy config flow: one credentials step, required capacity, fixed power ceilings.

Imports only ``drivers.setup``, ``const`` and Home Assistant. ``sessy.py`` imports
``SESSY_SETUP`` from here to set ``SessyLocalDriver.SETUP``, so this module cannot
import ``sessy.py`` at the top (cycle): the handlers import ``SessyLocalDriver``
inside the function. It is the same class object, so patching
``SessyLocalDriver.probe`` works as before.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from ..const import (
    CONF_DC_PV_CONNECTED,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
)
from .setup import DriverSetup, FlowKind, SetupContext, saved_dc_pv_connected

_LOGGER = logging.getLogger(__name__)

STEP_CONNECTION = "battery_connection_sessy"
STEP_RECONFIGURE = "reconfigure_battery_sessy"

_SESSY_MAX_CHARGE_POWER_W = 2200
_SESSY_MAX_DISCHARGE_POWER_W = 1700

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


async def _probe(host: str, port: int, username: str, password: str) -> bool:
    from .sessy import SessyLocalDriver  # local: sessy.py imports this module

    _LOGGER.info("Probing Sessy device at %s:%s", host, port)
    return await SessyLocalDriver.probe(host, port, username, password)


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Add (config flow) or edit (options flow) a Sessy through its dongle HTTP API."""
    errors = {}
    battery_num = flow.battery_index + 1
    options = ctx.kind is FlowKind.OPTIONS
    current = (ctx.battery or {}) if options else {}

    if user_input is not None:
        host = user_input[CONF_HOST]
        port = int(user_input.get(CONF_PORT, 80))
        username = user_input[CONF_USERNAME]
        password = user_input[CONF_PASSWORD]
        if await _probe(host, port, username, password):
            return {
                CONF_NAME: user_input[CONF_NAME],
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
                "brand": "sessy",
            }
        errors["base"] = "cannot_connect"

    if options:
        schema = {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Sessy {battery_num}")): str,
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=current.get(CONF_PORT, 80)): int,
            vol.Required(CONF_USERNAME, default=current.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD, default=current.get(CONF_PASSWORD, "")): _PASSWORD,
        }
    else:
        schema = {
            vol.Required(CONF_NAME, default=f"Sessy {battery_num}"): str,
            vol.Required(CONF_HOST): str,
            vol.Optional(CONF_PORT, default=80): int,
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): _PASSWORD,
        }
    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=vol.Schema(schema),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Update a Sessy's connection settings (config flow, reconfigure source)."""
    current = ctx.battery or {}
    battery_num = flow.battery_index + 1
    errors = {}

    if user_input is not None:
        new_host = user_input[CONF_HOST]
        new_port = int(user_input.get(CONF_PORT, 80))
        username = user_input[CONF_USERNAME]
        password = user_input[CONF_PASSWORD]
        if not await _probe(new_host, new_port, username, password):
            errors["base"] = "cannot_connect"
        else:
            old_host = current.get(CONF_HOST)
            old_port = current.get(CONF_PORT)
            if old_host and old_port and (old_host != new_host or old_port != new_port):
                flow._migrate_battery_registry_ids(
                    ctx.entry, old_host, old_port, new_host, new_port
                )
            return {
                CONF_NAME: user_input[CONF_NAME],
                CONF_HOST: new_host,
                CONF_PORT: new_port,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
                "brand": "sessy",
            }

    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=vol.Schema({
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Sessy {battery_num}")): str,
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=current.get(CONF_PORT, 80)): int,
            vol.Required(CONF_USERNAME, default=current.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD, default=current.get(CONF_PASSWORD, "")): _PASSWORD,
        }),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


class _SessySetup(DriverSetup):
    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        return _SESSY_MAX_CHARGE_POWER_W, _SESSY_MAX_DISCHARGE_POWER_W

    def limits_schema(self, battery_data: dict, base_fields: dict, ctx: SetupContext) -> dict:
        """Base fields plus a required capacity (Sessy reports neither capacity nor packs).

        Options flow: the default is the capacity already stored for this slot.
        """
        fields = dict(base_fields)
        saved = 0.0
        if ctx.kind is FlowKind.OPTIONS and ctx.battery is not None:
            saved = float((ctx.battery | battery_data).get("battery_capacity_kwh", 0.0))
        if ctx.kind is FlowKind.OPTIONS and saved > 0:
            capacity_field = vol.Required("battery_capacity_kwh", default=saved)
        else:
            capacity_field = vol.Required("battery_capacity_kwh")
        fields[capacity_field] = NumberSelector(
            NumberSelectorConfig(
                min=0.01, max=100, step=0.01, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX
            )
        )
        return fields

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        from .sessy import SessyLocalDriver

        merged[CONF_DC_PV_CONNECTED] = saved_dc_pv_connected(SessyLocalDriver, merged, user_input)
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = False
        merged["battery_capacity_kwh"] = round(float(user_input.get("battery_capacity_kwh", 0.0)), 2)


SESSY_SETUP = _SessySetup(
    steps={STEP_CONNECTION: _connection, STEP_RECONFIGURE: _reconfigure},
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
