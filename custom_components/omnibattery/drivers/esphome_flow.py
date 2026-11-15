"""ESPHome config flow: pick the LilyGo/ESPHome device bridging a Marstek battery.

Imports only ``drivers.setup``, ``const`` and Home Assistant. ``esphome.py`` imports
``ESPHOME_SETUP`` from here to set ``EsphomeEntityDriver.SETUP``, so this module cannot
import ``esphome.py`` at the top (cycle): the handlers import it inside the function.
It is the same class object, so patching ``EsphomeEntityDriver.resolve`` works as before.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import DeviceSelector, DeviceSelectorConfig

from ..const import (
    CONF_BATTERY_VERSION,
    CONF_DC_PV_CONNECTED,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    DEFAULT_VERSION,
)
from .marstek import max_power_for_battery_version
from .setup import DriverSetup, FlowKind, SetupContext, saved_dc_pv_connected

_LOGGER = logging.getLogger(__name__)

STEP_CONNECTION = "battery_connection_esphome"
STEP_RECONFIGURE = "reconfigure_battery_esphome"


def _device_selector() -> DeviceSelector:
    return DeviceSelector(DeviceSelectorConfig(integration="esphome"))


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Add (config flow) or edit (options flow) a battery behind a LilyGo/ESPHome bridge."""
    from .esphome import EsphomeEntityDriver  # local: esphome.py imports this module

    errors = {}
    battery_num = flow.battery_index + 1
    placeholders: dict[str, str] = {"battery_num": str(battery_num)}
    options = ctx.kind is FlowKind.OPTIONS
    current = (ctx.battery or {}) if options else {}

    try:
        if user_input is not None:
            device_id = user_input["esphome_device"]
            _, missing = EsphomeEntityDriver.resolve(flow.hass, device_id)
            if missing:
                errors["base"] = "esphome_entities_missing"
                placeholders["missing"] = ", ".join(missing)
            else:
                data = {
                    CONF_NAME: user_input[CONF_NAME],
                    # The registry device id doubles as the battery identity
                    # (device_key, persistence matching); there is no IP:port.
                    CONF_HOST: device_id,
                    CONF_PORT: 0,
                    "brand": "esphome",
                    "esphome_device_id": device_id,
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
        _LOGGER.error("Error in options flow battery_connection_esphome step: %s", e, exc_info=True)
        return flow.async_abort(reason="unknown_error")

    if options:
        schema: dict = {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Marstek Venus {battery_num}")): str,
        }
        default_device = current.get("esphome_device_id")
        if default_device:
            schema[vol.Required("esphome_device", default=default_device)] = _device_selector()
        else:
            schema[vol.Required("esphome_device")] = _device_selector()
    else:
        schema = {
            vol.Required(CONF_NAME, default=f"Marstek Venus {battery_num}"): str,
            vol.Required("esphome_device"): _device_selector(),
        }
    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=vol.Schema(schema),
        errors=errors,
        description_placeholders=placeholders,
    )


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Update the ESPHome bridge device of a battery (config flow, reconfigure source)."""
    from .esphome import EsphomeEntityDriver

    current = ctx.battery or {}
    battery_num = flow.battery_index + 1
    errors = {}
    placeholders: dict[str, str] = {"battery_num": str(battery_num)}

    if user_input is not None:
        device_id = user_input["esphome_device"]
        _, missing = EsphomeEntityDriver.resolve(flow.hass, device_id)
        if missing:
            errors["base"] = "esphome_entities_missing"
            placeholders["missing"] = ", ".join(missing)
        else:
            old_host = current.get(CONF_HOST)
            old_port = current.get(CONF_PORT)
            if old_host and old_host != device_id:
                flow._migrate_battery_registry_ids(
                    ctx.entry, old_host, old_port, device_id, 0
                )
            return {
                CONF_NAME: user_input[CONF_NAME],
                CONF_HOST: device_id,
                CONF_PORT: 0,
                "esphome_device_id": device_id,
            }

    schema: dict = {
        vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Marstek Venus {battery_num}")): str,
    }
    current_device = current.get("esphome_device_id")
    if current_device:
        schema[vol.Required("esphome_device", default=current_device)] = _device_selector()
    else:
        schema[vol.Required("esphome_device")] = _device_selector()

    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=vol.Schema(schema),
        errors=errors,
        description_placeholders=placeholders,
    )


class _EsphomeSetup(DriverSetup):
    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        # The bridge reaches a Marstek Venus: same table as a Marstek, read from
        # whatever version the battery data carries (default v2).
        ceiling = max_power_for_battery_version(
            battery_data.get(CONF_BATTERY_VERSION, DEFAULT_VERSION),
            battery_data.get("ems_version"),
        )
        return ceiling, ceiling

    def limits_schema(self, battery_data: dict, base_fields: dict, ctx: SetupContext) -> dict:
        """Base fields plus the voltage-taper switch (no capacity field).

        Options flow: the default is the value already stored for this slot.
        """
        fields = dict(base_fields)
        taper_default = DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED
        if ctx.kind is FlowKind.OPTIONS and ctx.battery is not None:
            taper_default = (ctx.battery | battery_data).get(
                CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED
            )
        fields[vol.Required(CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, default=taper_default)] = bool
        return fields

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        from .esphome import EsphomeEntityDriver

        merged[CONF_DC_PV_CONNECTED] = saved_dc_pv_connected(EsphomeEntityDriver, merged, user_input)
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = user_input.get(
            CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED
        )


ESPHOME_SETUP = _EsphomeSetup(
    steps={STEP_CONNECTION: _connection, STEP_RECONFIGURE: _reconfigure},
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
