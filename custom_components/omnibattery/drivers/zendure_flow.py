"""Zendure config flow: host/port step, model detected by the probe, per-model power ceilings.

Imports only ``drivers.setup``, ``const`` and Home Assistant. ``zendure.py`` imports
``ZENDURE_SETUP`` from here to set ``ZendureLocalDriver.SETUP``, so this module cannot
import ``zendure.py`` at the top (cycle): the handlers import it inside the function.
It is the same class object, so patching ``ZendureLocalDriver.probe`` works as before.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult

from ..const import (
    CONF_DC_PV_CONNECTED,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
)
from .setup import (
    DriverSetup,
    FlowKind,
    SetupContext,
    optional_capacity_field,
    saved_dc_pv_connected,
    saved_optional_capacity,
)

_LOGGER = logging.getLogger(__name__)

STEP_CONNECTION = "battery_connection_zendure"
STEP_RECONFIGURE = "reconfigure_battery_zendure"


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Add (config flow) or edit (options flow) a Zendure SolarFlow through its local HTTP API."""
    from .zendure import ZendureLocalDriver, detect_model  # local: zendure.py imports this module

    errors = {}
    battery_num = flow.battery_index + 1
    options = ctx.kind is FlowKind.OPTIONS
    current = (ctx.battery or {}) if options else {}

    try:
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input.get(CONF_PORT, 80))
            ok, product = await ZendureLocalDriver.probe(host, port)
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                data = {
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_HOST: host,
                    CONF_PORT: port,
                    "brand": "zendure",
                    "zendure_model": detect_model(product),
                }
                if not options:
                    return data
                # The options flow has always opened the limits step inside this
                # try, so an error there aborts with unknown_error.
                flow._current_battery_data.update(data)
                return await flow.async_step_battery_limits()
    except Exception as e:
        if not options:
            raise
        _LOGGER.error("Error in options flow battery_connection_zendure step: %s", e, exc_info=True)
        return flow.async_abort(reason="unknown_error")

    if options:
        schema = {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Zendure SolarFlow {battery_num}")): str,
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=current.get(CONF_PORT, 80)): int,
        }
    else:
        schema = {
            vol.Required(CONF_NAME, default=f"Zendure SolarFlow {battery_num}"): str,
            vol.Required(CONF_HOST): str,
            vol.Optional(CONF_PORT, default=80): int,
        }
    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=vol.Schema(schema),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Update a Zendure's connection settings (config flow, reconfigure source)."""
    from .zendure import ZendureLocalDriver, detect_model

    current = ctx.battery or {}
    battery_num = flow.battery_index + 1
    errors = {}

    if user_input is not None:
        new_host = user_input[CONF_HOST]
        new_port = user_input[CONF_PORT]
        ok, product = await ZendureLocalDriver.probe(new_host, new_port)
        if not ok:
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
                "zendure_model": detect_model(product),
            }

    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=vol.Schema({
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Zendure SolarFlow {battery_num}")): str,
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=current.get(CONF_PORT, 80)): int,
        }),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


class _ZendureSetup(DriverSetup):
    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        from .zendure import ZENDURE_MODEL_2400AC_PRO, zendure_power_limits

        return zendure_power_limits(battery_data.get("zendure_model", ZENDURE_MODEL_2400AC_PRO))

    def limits_schema(self, battery_data: dict, base_fields: dict, ctx: SetupContext) -> dict:
        """Base fields plus an optional capacity (0 = let the device report it).

        Options flow: the default is the capacity already stored for this slot.
        """
        return optional_capacity_field(dict(base_fields), battery_data, ctx, 0.0)

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        from .zendure import ZendureLocalDriver

        # Read-only chargeMaxLimit on the device: the software ceiling (#53).
        merged["user_max_charge_power"] = int(merged["max_charge_power"])
        merged[CONF_DC_PV_CONNECTED] = saved_dc_pv_connected(ZendureLocalDriver, merged, user_input)
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = False
        merged["battery_capacity_kwh"] = saved_optional_capacity(user_input, 0.0)


ZENDURE_SETUP = _ZendureSetup(
    steps={STEP_CONNECTION: _connection, STEP_RECONFIGURE: _reconfigure},
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
