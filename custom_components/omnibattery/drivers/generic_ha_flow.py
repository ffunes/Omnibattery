"""Observed-battery config flow: pick the Home Assistant entities to read.

Imports only ``drivers.setup``, ``const`` and Home Assistant. ``generic_ha.py``
imports ``GENERIC_HA_SETUP`` from here to set ``GenericHaEntityDriver.SETUP``, so
this module cannot import ``generic_ha.py`` at the top (cycle): the handlers
import it inside the function.

Unlike every other brand there is no host, port or credential to ask for. The
whole form is entity pickers, and the battery's identity is a generated id --
see ``_store`` for why it cannot be derived from the entities themselves.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..const import CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED
from .setup import DriverSetup, FlowKind, SetupContext

_LOGGER = logging.getLogger(__name__)

STEP_CONNECTION = "battery_connection_generic_ha"
STEP_RECONFIGURE = "reconfigure_battery_generic_ha"

DEFAULT_STALE_AFTER_S = 600


def _schema(defaults: dict) -> dict:
    """Build the observed-battery form; every field is a Home Assistant entity.

    ``defaults`` carries the saved values when editing.

    Optional fields pre-fill through ``description={"suggested_value": ...}``,
    never through ``default``. The difference is not cosmetic: a ``default`` on
    an optional selector renders a value the user cannot clear, while no
    pre-fill at all means a user who clicks straight through the options flow
    silently erases every optional setting -- which is exactly what happened on
    the first installation to re-run it, taking the energy counters with it.
    """
    sensor = EntitySelector(EntitySelectorConfig(domain="sensor"))
    sensors = EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True))
    schema: dict = {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "Battery")): str,
    }
    soc = defaults.get("ha_soc_entity")
    power = defaults.get("ha_power_entity")
    schema[vol.Required("ha_soc_entity", **({"default": soc} if soc else {}))] = sensor
    schema[vol.Required("ha_power_entity", **({"default": power} if power else {}))] = sensor
    schema[vol.Required(
        "ha_power_positive_is",
        default=defaults.get("ha_power_positive_is", "charge"),
    )] = SelectSelector(SelectSelectorConfig(
        options=[
            {"value": "charge", "label": "Positive values mean charging"},
            {"value": "discharge", "label": "Positive values mean discharging"},
        ],
        mode=SelectSelectorMode.LIST,
    ))

    def _optional(key: str):
        """An optional marker that shows the saved value without pinning it."""
        saved = defaults.get(key)
        if saved in (None, "", []):
            return vol.Optional(key)
        return vol.Optional(key, description={"suggested_value": saved})

    schema[_optional("ha_charge_energy_entities")] = sensors
    schema[_optional("ha_discharge_energy_entity")] = sensor
    schema[_optional("ha_target_soc_entity")] = EntitySelector(
        EntitySelectorConfig(domain=["number", "input_number"])
    )
    schema[vol.Required(
        "ha_stale_after_s", default=defaults.get("ha_stale_after_s", DEFAULT_STALE_AFTER_S),
    )] = NumberSelector(NumberSelectorConfig(
        min=60, max=3600, step=30, unit_of_measurement="s", mode=NumberSelectorMode.BOX
    ))
    return schema


def _store(user_input: dict, previous: dict | None = None) -> dict:
    """Turn the submitted form into the stored per-battery config."""
    previous = previous or {}
    # The identity has to survive the user renaming or replacing the source
    # entities, because device_key is built from it and every entity_id and
    # stored value hangs off that. A generated id is the only stable thing here:
    # unlike ESPHome there is no single upstream device to point at.
    host = previous.get(CONF_HOST) or uuid.uuid4().hex
    charge_entities = user_input.get("ha_charge_energy_entities") or []
    if isinstance(charge_entities, str):
        charge_entities = [charge_entities]
    return {
        CONF_NAME: user_input[CONF_NAME],
        CONF_HOST: host,
        CONF_PORT: 0,
        "brand": "generic_ha",
        "ha_soc_entity": user_input["ha_soc_entity"],
        "ha_power_entity": user_input["ha_power_entity"],
        "ha_power_positive_is": user_input.get("ha_power_positive_is", "charge"),
        "ha_charge_energy_entities": list(charge_entities),
        "ha_discharge_energy_entity": user_input.get("ha_discharge_energy_entity"),
        "ha_target_soc_entity": user_input.get("ha_target_soc_entity"),
        "ha_stale_after_s": int(user_input.get("ha_stale_after_s", DEFAULT_STALE_AFTER_S)),
    }


async def _connection(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Add (config flow) or edit (options flow) a battery read from HA entities."""
    from .generic_ha import GenericHaEntityDriver  # local: generic_ha imports this module

    errors: dict[str, str] = {}
    battery_num = flow.battery_index + 1
    placeholders: dict[str, str] = {"battery_num": str(battery_num)}
    options = ctx.kind is FlowKind.OPTIONS
    current = (ctx.battery or {}) if options else {}

    try:
        if user_input is not None:
            missing = GenericHaEntityDriver.resolve(
                flow.hass,
                {
                    "battery_soc": user_input.get("ha_soc_entity"),
                    "battery_power": user_input.get("ha_power_entity"),
                },
            )
            if missing:
                errors["base"] = "generic_ha_entities_missing"
                placeholders["missing"] = ", ".join(missing)
            else:
                data = _store(user_input, current)
                if not options:
                    return data
                # The options flow opens the limits step inside this try, so an
                # error there aborts with unknown_error, as the other brands do.
                flow._current_battery_data.update(data)
                return await flow.async_step_battery_limits()
    except Exception as e:  # noqa: BLE001
        if not options:
            raise
        _LOGGER.error(
            "Error in options flow battery_connection_generic_ha step: %s", e, exc_info=True
        )
        return flow.async_abort(reason="unknown_error")

    defaults = dict(current)
    defaults.setdefault(CONF_NAME, f"Battery {battery_num}")
    return flow.async_show_form(
        step_id=STEP_CONNECTION,
        data_schema=vol.Schema(_schema(defaults)),
        errors=errors,
        description_placeholders=placeholders,
    )


async def _reconfigure(flow, user_input: dict[str, Any] | None, ctx: SetupContext) -> FlowResult | dict:
    """Repoint an observed battery at different entities (reconfigure source).

    The generated host id is deliberately kept: the battery is the same battery,
    and changing it would orphan every entity and stored value hanging off
    ``device_key``.
    """
    from .generic_ha import GenericHaEntityDriver

    current = ctx.battery or {}
    battery_num = flow.battery_index + 1
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {"battery_num": str(battery_num)}

    if user_input is not None:
        missing = GenericHaEntityDriver.resolve(
            flow.hass,
            {
                "battery_soc": user_input.get("ha_soc_entity"),
                "battery_power": user_input.get("ha_power_entity"),
            },
        )
        if missing:
            errors["base"] = "generic_ha_entities_missing"
            placeholders["missing"] = ", ".join(missing)
        else:
            return _store(user_input, current)

    defaults = dict(current)
    defaults.setdefault(CONF_NAME, f"Battery {battery_num}")
    return flow.async_show_form(
        step_id=STEP_RECONFIGURE,
        data_schema=vol.Schema(_schema(defaults)),
        errors=errors,
        description_placeholders=placeholders,
    )


class _GenericHaSetup(DriverSetup):
    def power_ceilings(self, battery_data: dict) -> tuple[int, int]:
        """Nothing is ever commanded, so the ceilings are a formality.

        Kept at the shared default rather than 0 so the limits form renders a
        usable widget on the brands' common path; the driver reports a 0 W
        envelope in its capabilities, which is what control actually reads.
        """
        return super().power_ceilings(battery_data)

    def limits_schema(self, battery_data: dict, base_fields: dict, ctx: SetupContext) -> dict:
        """Base fields plus a required capacity (an observed battery reports none).

        The capacity is what the system aggregates need: this battery publishes
        no nominal-capacity reading of its own, and without a figure here its
        kilowatt-hours cannot enter the system SOC or the stored-energy total it
        is now expected to count towards.

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
        merged["battery_capacity_kwh"] = round(
            float(user_input.get("battery_capacity_kwh", 0.0)), 2
        )
        # No voltage taper on a battery whose charge this integration never
        # shapes; the trait says so too, this keeps the stored value honest.
        merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = False


GENERIC_HA_SETUP = _GenericHaSetup(
    steps={STEP_CONNECTION: _connection, STEP_RECONFIGURE: _reconfigure},
    entry_step=STEP_CONNECTION,
    reconfigure_entry_step=STEP_RECONFIGURE,
)
