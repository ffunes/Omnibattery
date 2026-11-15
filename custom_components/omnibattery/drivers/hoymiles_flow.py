"""Hoymiles config flow: MQTT device id + optional model hint, probe caps, per-model ceilings.

Imports only ``drivers.setup``, ``const`` and Home Assistant. ``hoymiles.py`` imports
``HOYMILES_SETUP`` from here to set ``HoymilesMqttDriver.SETUP``, so this module cannot
import ``hoymiles.py`` at the top (cycle): everything that needs it imports it inside
the function. It is the same class object, so patching ``HoymilesMqttDriver.probe``
works as before.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import CONF_DC_PV_CONNECTED, CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED
from .setup import (
    DriverSetup,
    FlowKind,
    SetupContext,
    optional_capacity_field,
    saved_dc_pv_connected,
    saved_optional_capacity,
)

STEP_CONNECTION = "battery_connection_hoymiles"
STEP_RECONFIGURE = "reconfigure_battery_hoymiles"

_HOYMILES_MODEL_AUTO = "auto"


def _hoymiles_model_selector(default: str = _HOYMILES_MODEL_AUTO):
    """Build the MQTT model selector, retaining automatic discovery by default."""
    from .hoymiles import HOYMILES_MODEL_PROFILES  # local: hoymiles.py imports this module

    options = [_HOYMILES_MODEL_AUTO, *(profile.key for profile in HOYMILES_MODEL_PROFILES)]
    return vol.Required("hoymiles_model", default=default), SelectSelector(
        SelectSelectorConfig(
            options=options,
            translation_key="hoymiles_model",
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _hoymiles_model_hint(user_input: dict[str, Any]) -> str | None:
    """Return an explicit model override or None for retained-topic discovery."""
    selected = user_input.get("hoymiles_model", _HOYMILES_MODEL_AUTO)
    return None if selected == _HOYMILES_MODEL_AUTO else selected


def _hoymiles_apply_probe_caps(
    battery_data: dict, caps: dict, *, upgrade_legacy_defaults: bool = False
) -> None:
    from .hoymiles import hoymiles_capacity_kwh, hoymiles_model_profile

    previous_model = battery_data.get("hoymiles_model")
    detected_model = caps.get("hoymiles_model")
    if detected_model:
        battery_data["hoymiles_model"] = detected_model
    if caps.get("hoymiles_model_label"):
        battery_data["hoymiles_model_label"] = caps["hoymiles_model_label"]
    for source, target in (("device_max_charge_power", "device_max_charge_power"),
                           ("device_max_discharge_power", "device_max_discharge_power")):
        if isinstance(caps.get(source), (int, float)) and caps[source] > 0:
            battery_data[target] = int(caps[source])

    capacity = caps.get("battery_capacity_kwh")
    if (
        (not isinstance(capacity, (int, float)) or capacity <= 0)
        and (detected_model or previous_model)
    ):
        capacity = hoymiles_capacity_kwh(
            detected_model or previous_model,
            battery_data.get("device_max_charge_power"),
            battery_data.get("device_max_discharge_power"),
        )
    if not isinstance(capacity, (int, float)) or capacity <= 0:
        capacity = None

    # Entries created by the original MS-A2-only flow have no model marker and
    # persist its 1000 W / 2.24 kWh defaults. When reconfiguration identifies a
    # different product, replace only those indistinguishable legacy defaults.
    detected_profile = hoymiles_model_profile(detected_model)
    if upgrade_legacy_defaults and previous_model is None and detected_profile:
        for key, device_key, legacy_default, profile_default in (
            ("max_charge_power", "device_max_charge_power", 1000, detected_profile.max_charge_power_w),
            ("max_discharge_power", "device_max_discharge_power", 1000, detected_profile.max_discharge_power_w),
        ):
            if int(battery_data.get(key, legacy_default) or 0) == legacy_default:
                battery_data[key] = int(
                    battery_data.get(device_key) or profile_default
                )
        if float(battery_data.get("battery_capacity_kwh", 2.24) or 0) == 2.24:
            battery_data["battery_capacity_kwh"] = capacity
    elif "battery_capacity_kwh" not in battery_data and capacity:
        battery_data["battery_capacity_kwh"] = capacity


def _hoymiles_power_ceilings(battery_data: dict) -> tuple[int, int]:
    from .hoymiles import DEFAULT_HOYMILES_MODEL, hoymiles_model_profile

    profile = hoymiles_model_profile(
        battery_data.get("hoymiles_model") or DEFAULT_HOYMILES_MODEL
    )
    default_charge = profile.max_charge_power_w if profile else 1000
    default_discharge = profile.max_discharge_power_w if profile else 1000
    charge_maximum = profile.max_system_charge_power_w if profile else 10000
    discharge_maximum = profile.max_system_discharge_power_w if profile else 10000
    return (
        max(100, min(charge_maximum, int(battery_data.get("device_max_charge_power") or default_charge))),
        max(100, min(discharge_maximum, int(battery_data.get("device_max_discharge_power") or default_discharge))),
    )


def _hoymiles_capacity_default(battery_data: dict) -> float:
    from .hoymiles import hoymiles_capacity_kwh

    capacity = battery_data.get("battery_capacity_kwh")
    if isinstance(capacity, (int, float)) and capacity > 0:
        return round(float(capacity), 2)
    model = battery_data.get("hoymiles_model")
    if model:
        return hoymiles_capacity_kwh(
            model,
            battery_data.get("device_max_charge_power"),
            battery_data.get("device_max_discharge_power"),
        ) or 2.24
    return 2.24


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Add (config flow) or edit (options flow) a Hoymiles battery through HA's MQTT broker."""
    from .hoymiles import HoymilesMqttDriver  # local: hoymiles.py imports this module

    errors = {}
    options = ctx.kind is FlowKind.OPTIONS
    current = (ctx.battery or {}) if options else {}
    battery_num = flow.battery_index + 1

    if user_input is not None:
        device_id = user_input["device_id"].strip()
        ok, caps = await HoymilesMqttDriver.probe(
            flow.hass,
            device_id,
            model_hint=_hoymiles_model_hint(user_input),
        )
        if not ok:
            errors["base"] = caps.get("error", "cannot_connect")
        else:
            # The probe caps read what the battery in progress already holds
            # (the stored battery in the options flow), so apply them on a copy
            # of it; the shared flow lays the result back over the same dict.
            data = dict(flow._current_battery_data)
            data.update({CONF_NAME: user_input[CONF_NAME], CONF_HOST: device_id,
                CONF_PORT: 0, "device_id": device_id, "brand": "hoymiles"})
            # Only the options flow upgrades the legacy MS-A2 defaults, and only
            # when the slot already holds a battery.
            _hoymiles_apply_probe_caps(data, caps, upgrade_legacy_defaults=bool(current))
            return data

    if options:
        model_field, model_selector = _hoymiles_model_selector(
            current.get("hoymiles_model", _HOYMILES_MODEL_AUTO)
        )
        schema = {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Hoymiles {battery_num}")): str,
            vol.Required("device_id", default=current.get("device_id", current.get(CONF_HOST, ""))): str,
            model_field: model_selector,
        }
    else:
        model_field, model_selector = _hoymiles_model_selector()
        schema = {
            vol.Required(CONF_NAME, default=f"Hoymiles {battery_num}"): str,
            vol.Required("device_id"): str,
            model_field: model_selector,
        }
    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=vol.Schema(schema),
        errors=errors,
        description_placeholders={"battery_num": str(battery_num)},
    )


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Update the MQTT device id without asking for broker credentials (reconfigure source)."""
    from .hoymiles import HoymilesMqttDriver

    current = ctx.battery or {}
    errors = {}
    if user_input is not None:
        device_id = user_input["device_id"].strip()
        ok, caps = await HoymilesMqttDriver.probe(
            flow.hass,
            device_id,
            model_hint=_hoymiles_model_hint(user_input),
        )
        if not ok:
            errors["base"] = caps.get("error", "cannot_connect")
        else:
            old_host, old_port = current.get(CONF_HOST), current.get(CONF_PORT, 0)
            if old_host and old_host != device_id:
                flow._migrate_battery_registry_ids(ctx.entry, old_host, old_port, device_id, 0)
            updated = dict(current)
            updated.update({CONF_NAME: user_input[CONF_NAME], CONF_HOST: device_id, CONF_PORT: 0,
                            "device_id": device_id, "brand": "hoymiles"})
            _hoymiles_apply_probe_caps(updated, caps, upgrade_legacy_defaults=True)
            return updated
    model_field, model_selector = _hoymiles_model_selector(
        current.get("hoymiles_model", _HOYMILES_MODEL_AUTO)
    )
    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=vol.Schema({
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, "Hoymiles")): str,
            vol.Required("device_id", default=current.get("device_id", current.get(CONF_HOST, ""))): str,
            model_field: model_selector,
        }),
        errors=errors,
        description_placeholders={"battery_num": str(flow.battery_index + 1)},
    )


class _HoymilesSetup(DriverSetup):
    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        return _hoymiles_power_ceilings(battery_data)

    def limits_schema(self, battery_data: dict, base_fields: dict, ctx: SetupContext) -> dict:
        """Base fields plus an optional capacity, defaulted from the detected model."""
        return optional_capacity_field(
            dict(base_fields), battery_data, ctx, _hoymiles_capacity_default(battery_data)
        )

    def finalize_limits(
        self, merged: dict, user_input: dict, battery_data: dict, ctx: SetupContext
    ) -> None:
        from .hoymiles import HoymilesMqttDriver

        merged[CONF_DC_PV_CONNECTED] = saved_dc_pv_connected(HoymilesMqttDriver, merged, user_input)
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = False
        merged["battery_capacity_kwh"] = saved_optional_capacity(
            user_input, _hoymiles_capacity_default(battery_data)
        )


HOYMILES_SETUP = _HoymilesSetup(
    steps={STEP_CONNECTION: _connection, STEP_RECONFIGURE: _reconfigure},
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
