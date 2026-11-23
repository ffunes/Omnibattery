"""Anker config flow: host/port/slave step, reuse of the live coordinator, hidden power ceilings.

Imports only ``drivers.setup``, ``const`` and Home Assistant. ``anker.py`` imports
``ANKER_SETUP`` from here to set ``AnkerModbusDriver.SETUP``, so this module cannot
import ``anker.py`` at the top (cycle): everything that needs it imports it inside
the function. It is the same class object, so patching ``AnkerModbusDriver.probe``
works as before.
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
    CONF_SLAVE_ID,
    DEFAULT_SLAVE_ID,
    DOMAIN,
)
from .setup import DriverSetup, FlowKind, SetupContext, saved_dc_pv_connected

_LOGGER = logging.getLogger(__name__)

STEP_CONNECTION = "battery_connection_anker"
STEP_RECONFIGURE = "reconfigure_battery_anker"

# Sanity bound on 10036/10038, not a rating: a combined system on one IP reports
# its total (two units 7000 W, three units up to 10500 W, #646). The default the
# stored max charge/discharge starts at stays one unit; the user raises it with
# the Max charge/discharge power number.
# ponytail: four units (Anker's Power Dock limit); raise if a bigger system reads more.
_ANKER_MAX_POWER_W = 4 * 3500
_ANKER_DEFAULT_POWER_W = 3500


def _anker_apply_probe_caps(battery_data: dict, caps: dict) -> None:
    """Store Anker hardware ceilings from probe for config seeding."""
    for src, dst in (
        ("device_max_charge_power", "device_max_charge_power"),
        ("device_max_discharge_power", "device_max_discharge_power"),
    ):
        if src in caps:
            battery_data[dst] = int(caps[src])


def _anker_power_ceilings(battery_data: dict) -> tuple[int, int]:
    """Hardware max charge/discharge from probe, falling back to the static envelope."""
    charge = int(battery_data.get("device_max_charge_power") or _ANKER_MAX_POWER_W)
    discharge = int(battery_data.get("device_max_discharge_power") or _ANKER_MAX_POWER_W)
    return (
        max(100, min(_ANKER_MAX_POWER_W, charge)),
        max(100, min(_ANKER_MAX_POWER_W, discharge)),
    )


async def _validate_anker_connection(
    hass: Any,
    entry_id: str,
    host: str,
    port: int,
    slave_id: int,
) -> tuple[bool, dict[str, int]]:
    """Validate Anker connection without probing an already-active endpoint.

    Reconfigure/options flows run while the entry coordinator still owns its
    persistent Modbus connection. When that exact Anker endpoint is healthy,
    its live telemetry is stronger evidence than opening a second connection.
    A changed or unavailable endpoint still receives a normal probe.
    """
    from .anker import AnkerModbusDriver  # local: anker.py imports this module

    entry_data = getattr(hass, "data", {}).get(DOMAIN, {}).get(entry_id, {})
    for coordinator in entry_data.get("coordinators", []):
        if (
            getattr(coordinator, "brand", None) == "anker"
            and getattr(coordinator, "host", None) == host
            and int(getattr(coordinator, "port", 502)) == port
            and int(getattr(coordinator, "slave_id", DEFAULT_SLAVE_ID)) == slave_id
            and bool(getattr(coordinator, "is_available", False))
        ):
            data = getattr(coordinator, "data", None) or {}
            caps: dict[str, int] = {}
            for src, dst in (
                ("max_charge_power", "device_max_charge_power"),
                ("max_discharge_power", "device_max_discharge_power"),
            ):
                value = data.get(src)
                if isinstance(value, (int, float)) and int(value) > 0:
                    caps[dst] = int(value)
            _LOGGER.info(
                "Reusing active Anker coordinator for connection validation at "
                "%s:%s slave %s",
                host,
                port,
                slave_id,
            )
            return True, caps

    return await AnkerModbusDriver.probe(host, port, slave_id)


def _slave_selector():
    return vol.All(
        NumberSelector(NumberSelectorConfig(min=1, max=247, step=1, mode=NumberSelectorMode.BOX)),
        vol.Coerce(int),
    )


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Add (config flow) or edit (options flow) an Anker Solarbank Max AC / 4 E5000 Pro."""
    from .anker import AnkerModbusDriver  # local: anker.py imports this module

    errors = {}
    options = ctx.kind is FlowKind.OPTIONS
    battery_num = flow.battery_index + 1

    # The options flow has always run this whole step inside a try: any error,
    # also one raised by the limits step it opens, aborts with unknown_error.
    # Its probe goes through the live-coordinator validation; the config flow
    # probes directly.
    try:
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input.get(CONF_PORT, 502))
            slave_id = int(user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID))
            if options:
                ok, caps = await _validate_anker_connection(
                    flow.hass, flow.config_entry.entry_id, host, port, slave_id
                )
            else:
                ok, caps = await AnkerModbusDriver.probe(host, port, slave_id)
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                data = {
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_SLAVE_ID: slave_id,
                    "brand": "anker",
                }
                _anker_apply_probe_caps(data, caps)
                if not options:
                    return data
                flow._current_battery_data.update(data)
                return await flow.async_step_battery_limits()

        if options:
            current = ctx.battery
            if current is not None:
                defaults = {
                    CONF_NAME: current.get(CONF_NAME, f"Anker Solarbank {battery_num}"),
                    CONF_HOST: current.get(CONF_HOST, ""),
                    CONF_PORT: current.get(CONF_PORT, 502),
                    CONF_SLAVE_ID: current.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID),
                }
            else:
                defaults = {
                    CONF_NAME: f"Anker Solarbank {battery_num}",
                    CONF_HOST: "",
                    CONF_PORT: 502,
                    CONF_SLAVE_ID: DEFAULT_SLAVE_ID,
                }
    except Exception as e:
        if not options:
            raise
        _LOGGER.error("Error in options flow battery_connection_anker step: %s", e, exc_info=True)
        return flow.async_abort(reason="unknown_error")

    if options:
        schema = {
            vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
            vol.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
            vol.Optional(CONF_PORT, default=defaults[CONF_PORT]): int,
            vol.Required(CONF_SLAVE_ID, default=defaults[CONF_SLAVE_ID]): _slave_selector(),
        }
    else:
        schema = {
            vol.Required(CONF_NAME, default=f"Anker Solarbank {battery_num}"): str,
            vol.Required(CONF_HOST): str,
            vol.Optional(CONF_PORT, default=502): int,
            vol.Required(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID): _slave_selector(),
        }
    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=vol.Schema(schema),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Update connection settings for an Anker battery during reconfiguration."""
    entry = ctx.entry
    current = ctx.battery or {}
    battery_num = flow.battery_index + 1
    errors = {}

    if user_input is not None:
        new_host = user_input[CONF_HOST]
        new_port = int(user_input.get(CONF_PORT, 502))
        slave_id = int(user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID))
        ok, _ = await _validate_anker_connection(
            flow.hass, entry.entry_id, new_host, new_port, slave_id
        )
        if not ok:
            errors["base"] = "cannot_connect"
        else:
            old_host = current.get(CONF_HOST)
            old_port = current.get(CONF_PORT)

            # Only a host/port change migrates the registry ids: a change of slave
            # alone does not, and host+slave moves them to the slave-less id.
            if old_host and old_port and (old_host != new_host or old_port != new_port):
                flow._migrate_battery_registry_ids(entry, old_host, old_port, new_host, new_port)

            return {
                CONF_NAME: user_input[CONF_NAME],
                CONF_HOST: new_host,
                CONF_PORT: new_port,
                CONF_SLAVE_ID: slave_id,
                "brand": "anker",
            }

    defaults = {
        CONF_NAME: current.get(CONF_NAME, f"Anker Solarbank {battery_num}"),
        CONF_HOST: current.get(CONF_HOST, ""),
        CONF_PORT: current.get(CONF_PORT, 502),
        CONF_SLAVE_ID: current.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID),
    }
    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=vol.Schema({
            vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
            vol.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
            vol.Required(CONF_PORT, default=defaults[CONF_PORT]): int,
            vol.Required(CONF_SLAVE_ID, default=defaults[CONF_SLAVE_ID]): _slave_selector(),
        }),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


class _AnkerSetup(DriverSetup):
    async def reprobe(self, hass: Any, battery_config: dict, host: str) -> bool:
        from .anker import AnkerModbusDriver  # local: anker.py imports this module

        port = battery_config.get(CONF_PORT)
        slave_id = battery_config.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
        _LOGGER.info("Probing Anker Solarbank at %s:%s slave %s", host, port, slave_id)
        result, _ = await AnkerModbusDriver.probe(host, port, slave_id)
        return result

    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        return _anker_power_ceilings(battery_data)

    def limits_schema(self, battery_data: dict, base_fields: dict, ctx: SetupContext) -> dict:
        """Base fields without the power ceilings: they are device sensors (#54)."""
        fields = dict(base_fields)
        for key in [k for k in fields if str(k.schema) in ("max_charge_power", "max_discharge_power")]:
            del fields[key]
        return fields

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        from .anker import AnkerModbusDriver

        # Power caps are device sensors (10036/10038), not setup inputs. The
        # options flow reads them from the stored battery laid over the new one.
        source = merged if ctx.kind is FlowKind.OPTIONS else battery_data
        charge_w, discharge_w = _anker_power_ceilings(source)
        merged["max_charge_power"] = min(charge_w, _ANKER_DEFAULT_POWER_W)
        merged["max_discharge_power"] = min(discharge_w, _ANKER_DEFAULT_POWER_W)
        merged[CONF_DC_PV_CONNECTED] = saved_dc_pv_connected(AnkerModbusDriver, merged, user_input)
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = False


ANKER_SETUP = _AnkerSetup(
    steps={STEP_CONNECTION: _connection, STEP_RECONFIGURE: _reconfigure},
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
