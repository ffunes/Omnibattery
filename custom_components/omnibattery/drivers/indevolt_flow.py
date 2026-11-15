"""Indevolt config flow: read-only probe, optional mode notice, request speed, two-step reconfigure.

Imports only ``drivers.setup``, ``const``, Indevolt's own client/settings modules and
Home Assistant. ``indevolt.py`` imports ``INDEVOLT_SETUP`` from here to set
``IndevoltDriver.SETUP``, so this module cannot import ``indevolt.py`` at the top
(cycle): everything that needs it imports it inside the function. It is the same
class object, so patching ``IndevoltDriver.probe`` works as before.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import (
    CONF_DC_PV_CONNECTED,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    CONF_INDEVOLT_REQUEST_SPEED,
)
from ..infra.mac_tracking import CONF_MAC
from .indevolt_client import DEFAULT_REQUEST_SPEED, REQUEST_SPEED_SLOT_S, IndevoltAuthenticationError
from .indevolt_settings import SYNCED_PREFIX as INDEVOLT_SYNCED_PREFIX
from .setup import DriverSetup, FlowKind, SetupContext, saved_dc_pv_connected

_LOGGER = logging.getLogger(__name__)

STEP_CONNECTION = "battery_connection_indevolt"
STEP_MODE_NOTICE = "indevolt_mode_notice"
STEP_REQUEST_SPEED = "indevolt_request_speed"
STEP_RECONFIGURE = "reconfigure_battery_indevolt"
STEP_RECONFIGURE_REQUEST_SPEED = "reconfigure_battery_indevolt_request_speed"

_INDEVOLT_DEFAULT_PORT = 8080
_INDEVOLT_DEFAULT_MAX_POWER_W = 2400


def _indevolt_connection_schema(defaults: dict, battery_num: int) -> vol.Schema:
    """Connection form shared by setup, options and reconfigure."""
    return vol.Schema({
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, f"Indevolt {battery_num}")): str,
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Optional(CONF_PORT, default=defaults.get(CONF_PORT, _INDEVOLT_DEFAULT_PORT)): int,
    })


def _indevolt_request_speed_schema(defaults: dict) -> vol.Schema:
    """Request speed form, the screen after the connection.

    It has a screen of its own so the explanation of the speeds fits above the
    field instead of disappearing below the fold.
    """
    return vol.Schema({
        vol.Required(CONF_INDEVOLT_REQUEST_SPEED, default=_indevolt_request_speed(defaults)): SelectSelector(
            SelectSelectorConfig(
                options=list(REQUEST_SPEED_SLOT_S),
                translation_key="indevolt_request_speed",
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    })


def _indevolt_request_speed(values: dict) -> str:
    """The battery's request speed; an older entry without one gets the default."""
    speed = values.get(CONF_INDEVOLT_REQUEST_SPEED)
    return speed if speed in REQUEST_SPEED_SLOT_S else DEFAULT_REQUEST_SPEED


async def _indevolt_probe(user_input: dict) -> tuple[dict, dict] | str:
    """Probe an Indevolt (read-only): its battery config and probe info, or an error key."""
    from .indevolt import IndevoltDriver  # local: indevolt.py imports this module

    host = (user_input.get(CONF_HOST) or "").strip()
    port = int(user_input.get(CONF_PORT, _INDEVOLT_DEFAULT_PORT))
    _LOGGER.info("Probing Indevolt device at %s:%s", host, port)
    try:
        info = await IndevoltDriver.probe(host, port)
    except IndevoltAuthenticationError:
        # The local API is set to HTTP Digest; Omnibattery uses plain HTTP.
        return "indevolt_http_digest"
    except ValueError:
        return "cannot_connect"
    if info is None:
        return "cannot_connect"
    data = {
        CONF_NAME: user_input[CONF_NAME],
        CONF_HOST: host,
        CONF_PORT: port,
        "brand": "indevolt",
    }
    for source, target in (("max_charge_power", "device_max_charge_power"),
                           ("max_discharge_power", "device_max_discharge_power")):
        if info.get(source):
            data[target] = int(info[source])
    return data, info


def _indevolt_prefill(battery_data: dict, info: dict, stored: dict | None = None) -> None:
    """Pre-fill the limits form with the reserve, power limits and MAC the battery reports.

    On its first start the driver adopts the battery's own settings and writes
    nothing (drivers/indevolt_settings.py), so a form value that differs would
    be replaced without notice. A setting that Omnibattery and the battery
    already agreed on (``stored``) keeps its configured value.
    """
    stored = stored or {}
    for key, value in (info.get("settings") or {}).items():
        if stored.get(INDEVOLT_SYNCED_PREFIX + key) is None:
            battery_data[key] = value
    if info.get("mac") and not battery_data.get(CONF_MAC):
        battery_data[CONF_MAC] = info["mac"]


def _indevolt_power_ceilings(battery_data: dict) -> tuple[int, int]:
    """Device-reported power ceilings, or the SF2000's 2400 W before a probe."""
    return (
        int(battery_data.get("device_max_charge_power") or _INDEVOLT_DEFAULT_MAX_POWER_W),
        int(battery_data.get("device_max_discharge_power") or _INDEVOLT_DEFAULT_MAX_POWER_W),
    )


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Configure an Indevolt battery through its local HTTP API (read-only probe)."""
    errors = {}
    options = ctx.kind is FlowKind.OPTIONS
    battery_num = flow.battery_index + 1
    # Options flow: the form starts from the stored battery and the pre-fill
    # leaves settings Omnibattery and the battery already agreed on.
    current_battery = (ctx.battery or {}) if options else {}
    if user_input is not None:
        probed = await _indevolt_probe(user_input)
        if isinstance(probed, str):
            errors["base"] = probed
        else:
            data, info = probed
            flow._current_battery_data.update(data)
            _indevolt_prefill(
                flow._current_battery_data, info, current_battery if options else None
            )
            if not info["realtime_mode"]:
                return await flow.async_step_indevolt_mode_notice()
            return await flow.async_step_indevolt_request_speed()
    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=_indevolt_connection_schema(user_input or current_battery, battery_num),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


async def _mode_notice(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Realtime mode is required; setup continues and control starts once it is set."""
    if user_input is not None:
        return await flow.async_step_indevolt_request_speed()
    return flow.async_show_form(
        step_id=STEP_MODE_NOTICE,
        data_schema=vol.Schema({}),
        description_placeholders={"battery_num": str(flow.battery_index + 1)},
    )


async def _request_speed(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Choose how fast Omnibattery may send requests to the Indevolt battery."""
    if user_input is not None:
        return {CONF_INDEVOLT_REQUEST_SPEED: _indevolt_request_speed(user_input)}
    # The form defaults come from the battery in progress when adding, from the
    # stored battery when editing in the options flow.
    if ctx.kind is FlowKind.OPTIONS:
        defaults = ctx.battery or {}
    else:
        defaults = flow._current_battery_data
    return flow.async_show_form(
        step_id=STEP_REQUEST_SPEED,
        data_schema=_indevolt_request_speed_schema(defaults),
        description_placeholders={"battery_num": str(flow.battery_index + 1)},
    )


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Update connection settings for an Indevolt battery during reconfiguration."""
    current = ctx.battery or {}
    battery_num = flow.battery_index + 1
    errors = {}

    if user_input is not None:
        probed = await _indevolt_probe(user_input)
        if isinstance(probed, str):
            errors["base"] = probed
        else:
            data, _info = probed
            ctx.work["updated"] = {**current, **data}
            return await flow.async_step_reconfigure_battery_indevolt_request_speed()

    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=_indevolt_connection_schema(user_input or current, battery_num),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


async def _reconfigure_request_speed(
    flow, user_input: dict[str, Any] | None, ctx: SetupContext
) -> FlowResult | dict:
    """Update the request speed of an Indevolt battery during reconfiguration."""
    current = ctx.battery
    updated = ctx.work["updated"]

    if user_input is not None:
        updated[CONF_INDEVOLT_REQUEST_SPEED] = _indevolt_request_speed(user_input)
        # Migrated only now, so leaving the flow on this screen changes nothing.
        old_host = current.get(CONF_HOST)
        old_port = current.get(CONF_PORT)
        if old_host and old_port and (old_host != updated[CONF_HOST] or old_port != updated[CONF_PORT]):
            flow._migrate_battery_registry_ids(
                ctx.entry, old_host, old_port, updated[CONF_HOST], updated[CONF_PORT]
            )
        return updated

    return flow.async_show_form(
        step_id=STEP_RECONFIGURE_REQUEST_SPEED,
        data_schema=_indevolt_request_speed_schema(updated),
        description_placeholders={"battery_num": str(flow.battery_index + 1)},
    )


class _IndevoltSetup(DriverSetup):
    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        return _indevolt_power_ceilings(battery_data)

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        from .indevolt import IndevoltDriver

        merged[CONF_DC_PV_CONNECTED] = saved_dc_pv_connected(IndevoltDriver, merged, user_input)
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = False


INDEVOLT_SETUP = _IndevoltSetup(
    steps={
        STEP_CONNECTION: _connection,
        STEP_MODE_NOTICE: _mode_notice,
        STEP_REQUEST_SPEED: _request_speed,
        STEP_RECONFIGURE: _reconfigure,
        STEP_RECONFIGURE_REQUEST_SPEED: _reconfigure_request_speed,
    },
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
