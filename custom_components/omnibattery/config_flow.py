"""Config flow for Omnibattery integration."""
from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import logging
import math
from typing import Any

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow, OptionsFlow, ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
)
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    DeviceSelector,
    EntitySelector,
    EntitySelectorConfig,
    TimeSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    BooleanSelector,
    Selector,
)

from .infra.mac_tracking import (
    CANDIDATE_SILENT,
    CONF_MAC,
    CONF_TRACK_MAC,
    detect_mac,
    device_key_for,
    evaluate_lease,
    is_ip_based,
    normalise_mac,
)
from .migration_flow import (
    LegacyDomainMigrationMixin,
    async_has_legacy_entries,
)
from .config_backup import (
    async_has_config_backup,
    async_load_config_backup,
    async_restore_config_backup,
)
from .infra.lifecycle import clear_reload_pending, mark_reload_pending
from .infra.entity_naming import is_omnibattery_solar_entity
from .infra.device_lookup import get_entry_device
from .solar_forecast import normalize_solar_forecast_config
from .const import (
    DOMAIN,
    CONF_ENABLE_PREDICTIVE_CHARGING,
    CONF_CHARGING_TIME_SLOT,
    CONF_SOLAR_FORECAST_SENSOR,
    CONF_SOLAR_FORECAST_REMAINING_SENSOR,
    CONF_SOLAR_PRODUCTION_SENSOR,
    CONF_MAX_CONTRACTED_POWER,
    CONF_OFFGRID_POWER_SENSOR,
    CONF_OFFGRID_METER_INVERTED,
    CONF_OFFGRID_MODE_ENABLED,
    CONF_THREE_PHASE_ENABLED,
    CONF_PHASE_1_CURRENT_SENSOR,
    CONF_PHASE_2_CURRENT_SENSOR,
    CONF_PHASE_3_CURRENT_SENSOR,
    CONF_PHASE_1_FUSE_SIZE,
    CONF_PHASE_2_FUSE_SIZE,
    CONF_PHASE_3_FUSE_SIZE,
    CONF_BATTERY_PHASE,
    PHASE_L1,
    PHASE_L2,
    PHASE_L3,
    PHASE_VALUES,
    PHASE_UNASSIGNED,
    normalize_battery_phase,
    DEFAULT_THREE_PHASE_ENABLED,
    CONF_ENABLE_WEEKLY_FULL_CHARGE,
    CONF_WEEKLY_FULL_CHARGE_DAY,
    CONF_ENABLE_BALANCE_MONITOR,
    CONF_ENABLE_CHARGE_DELAY,
    CONF_ENABLE_TEMP_CHARGE_LIMIT,
    CONF_DELAY_SOC_SETPOINT_ENABLED,
    CONF_BATTERY_VERSION,
    CONF_PRIMARY_BATTERY,
    CONF_SLAVE_ID,
    DEFAULT_SLAVE_ID,
    DEFAULT_VERSION,
    MAX_POWER_BY_VERSION,
    MAX_BATTERIES,
    CONF_ENABLE_SYSTEM_POWER_LIMITS,
    CONF_CAPACITY_PROTECTION_ENABLED,
    CONF_CAPACITY_PROTECTION_EXCLUDED_DEVICES,
    CONF_PREDICTIVE_CHARGING_MODE,
    CONF_PRICE_SENSOR,
    CONF_PRICE_INTEGRATION_TYPE,
    CONF_EXPORT_PRICE_SENSOR,
    CONF_EXPORT_PRICE_INTEGRATION_TYPE,
    CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
    DEFAULT_ZONNEPLAN_EXPORT_BONUS_ENABLED,
    CONF_AVERAGE_PRICE_SENSOR,
    PREDICTIVE_MODE_TIME_SLOT,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
    PRICE_INTEGRATION_NORDPOOL,
    PRICE_INTEGRATION_PVPC,
    PRICE_INTEGRATION_CKW,
    PRICE_INTEGRATION_EPEX,
    PRICE_INTEGRATION_ENTSOE,
    PRICE_INTEGRATION_ENERGYZERO,
    PRICE_INTEGRATION_BE_ELECTRICITY,
    PRICE_INTEGRATION_TIBBER,
    PRICE_INTEGRATION_ZONNEPLAN,
    PRICE_INTEGRATION_FRANK_ENERGIE,
    CONF_METER_INVERTED,
    MIN_CHARGE_HYSTERESIS_PERCENT,
    DEFAULT_CHARGE_HYSTERESIS_PERCENT,
    MAX_CHARGE_HYSTERESIS_PERCENT,
    SLOT_BATTERY_SCOPE_ALL,
    SLOT_MODE_PD,
    SLOT_MODE_MANUAL,
    DEFAULT_SLOT_ALLOW_CHARGE,
    DEFAULT_SLOT_ALLOW_DISCHARGE,
    DEFAULT_SLOT_SOC_OVERRIDE_ENABLED,
    DEFAULT_SLOT_POWER_OVERRIDE_ENABLED,
    DEFAULT_SLOT_MODE,
    DEFAULT_SLOT_SOC_MIN_FLOOR,
    DEFAULT_SLOT_SOC_MAX_CEILING,
    MAX_TIME_SLOTS,
)
from .drivers import registry
from .drivers.registry import DEFAULT_BRAND
from .drivers.setup import (
    FlowKind,
    ReplacesBattery,
    SetupContext,
    is_flow_result,
    live_coordinator,
    work_dict,
)
from .pricing.nordpool import is_official_nordpool_sensor
from .pricing.calculations import (
    parse_be_electricity_prices,
    parse_frank_energie_prices,
    parse_zonneplan_prices,
)
from .pricing.energyzero import (
    ENERGYZERO_DOMAIN,
    ENERGYZERO_GET_PRICES_SERVICE,
    is_energyzero_sensor,
)

def _parse_optional_float(value: Any) -> float | None:
    """Parse a localized optional number while preserving an explicit zero."""
    if value is None or value == "":
        return None
    return float(str(value).replace(",", "."))


def _price_integration_options() -> list[str]:
    """Return the supported price providers, in display order."""
    return [
        PRICE_INTEGRATION_NORDPOOL,
        PRICE_INTEGRATION_PVPC,
        PRICE_INTEGRATION_CKW,
        PRICE_INTEGRATION_EPEX,
        PRICE_INTEGRATION_ENTSOE,
        PRICE_INTEGRATION_ENERGYZERO,
        PRICE_INTEGRATION_TIBBER,
        PRICE_INTEGRATION_ZONNEPLAN,
        PRICE_INTEGRATION_FRANK_ENERGIE,
        PRICE_INTEGRATION_BE_ELECTRICITY,
    ]


def _price_integration_export_options() -> list[str]:
    """Return the providers usable for the export curve.

    Tibber is absent: it is service-based and its cache is keyed on the import
    sensor, so it cannot supply a second, independent curve.
    """
    return [
        option
        for option in _price_integration_options()
        if option != PRICE_INTEGRATION_TIBBER
    ]


def _price_integration_type_selector(options: list[str] | None = None) -> SelectSelector:
    """Return the provider dropdown shared by the import and export curves."""
    return SelectSelector(
        SelectSelectorConfig(
            options=options if options is not None else _price_integration_options(),
            translation_key="price_integration_type",
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _validate_price_sensor(
    hass: HomeAssistant,
    price_sensor: str,
    integration_type: str,
    *,
    allow_service_cache: bool = True,
) -> str | None:
    """Return an error key when the sensor lacks the provider's price data.

    Shared by the import and export curves, which use the same parsers.
    ``allow_service_cache`` accepts an official Nord Pool sensor that carries no
    ``raw_today`` because the import path reads it through the Nord Pool
    service. Only the import curve has that cache, so the export curve requires
    real attributes.
    """
    price_state = hass.states.get(price_sensor)
    if price_state is None:
        return "sensor_not_found"
    attrs = price_state.attributes
    if integration_type == PRICE_INTEGRATION_PVPC:
        if not any(f"price_{h:02d}h" in attrs for h in range(24)):
            return "no_price_data"
        return None
    if integration_type == PRICE_INTEGRATION_CKW:
        prices = attrs.get("prices")
        if not prices or not isinstance(prices, (list, tuple)):
            return "no_price_data"
        return None
    if integration_type == PRICE_INTEGRATION_EPEX:
        data = attrs.get("data")
        if not data or not isinstance(data, (list, tuple)):
            return "no_price_data"
        return None
    if integration_type == PRICE_INTEGRATION_ENTSOE:
        prices = attrs.get("prices_today")
        if not prices or not isinstance(prices, (list, tuple)):
            return "no_price_data"
        return None
    if integration_type == PRICE_INTEGRATION_ZONNEPLAN:
        return None if parse_zonneplan_prices(attrs) else "no_price_data"
    if integration_type == PRICE_INTEGRATION_FRANK_ENERGIE:
        return None if parse_frank_energie_prices(attrs) else "no_price_data"
    if integration_type == PRICE_INTEGRATION_ENERGYZERO:
        if not hass.services.has_service(ENERGYZERO_DOMAIN, ENERGYZERO_GET_PRICES_SERVICE):
            return "energyzero_unavailable"
        return None if is_energyzero_sensor(hass, price_sensor) else "no_price_data"
    if integration_type == PRICE_INTEGRATION_BE_ELECTRICITY:
        return None if parse_be_electricity_prices(attrs) else "no_price_data"
    # Nordpool
    if "raw_today" in attrs:
        return None
    if allow_service_cache and is_official_nordpool_sensor(hass, price_sensor, attrs):
        return None
    return "no_price_data"


def _validate_export_price_input(
    hass: HomeAssistant, user_input: dict[str, Any], import_integration_type: str
) -> dict[str, str]:
    """Validate the optional export/feed-in price sensor.

    Tibber is rejected here on purpose: its slots come from a service poll whose
    cache is keyed on the import sensor, so it cannot serve a second curve.
    """
    errors: dict[str, str] = {}
    export_sensor = user_input.get(CONF_EXPORT_PRICE_SENSOR)
    export_type = user_input.get(CONF_EXPORT_PRICE_INTEGRATION_TYPE)
    if not export_sensor:
        return errors
    if export_type == PRICE_INTEGRATION_TIBBER:
        errors[CONF_EXPORT_PRICE_INTEGRATION_TYPE] = "export_tibber_unsupported"
        return errors
    effective_type = export_type or import_integration_type
    if effective_type == PRICE_INTEGRATION_TIBBER:
        # The import curve is service-based, so an export sensor must say which
        # attribute layout it uses rather than inheriting an unusable type.
        errors[CONF_EXPORT_PRICE_INTEGRATION_TYPE] = "export_type_required"
        return errors
    error = _validate_price_sensor(
        hass, export_sensor, effective_type, allow_service_cache=False
    )
    if error:
        errors[CONF_EXPORT_PRICE_SENSOR] = error
    return errors


def _validate_offgrid_power_sensor(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, str]:
    """Validate the optional alternate meter used by off-grid mode."""
    sensor = user_input.get(CONF_OFFGRID_POWER_SENSOR)
    if not sensor:
        return {}
    if sensor == user_input.get("consumption_sensor"):
        return {CONF_OFFGRID_POWER_SENSOR: "offgrid_sensor_must_differ"}
    state = hass.states.get(sensor)
    if state is None:
        return {CONF_OFFGRID_POWER_SENSOR: "offgrid_sensor_not_found"}
    if state.attributes.get("unit_of_measurement", "") not in ("W", "kW"):
        return {CONF_OFFGRID_POWER_SENSOR: "offgrid_sensor_invalid_unit"}
    return {}


def _validate_solar_production_sensor(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, str]:
    """Validate the optional external solar source without allowing feedback."""
    sensor = user_input.get(CONF_SOLAR_PRODUCTION_SENSOR)
    if not sensor:
        return {}
    if is_omnibattery_solar_entity(hass, sensor):
        return {CONF_SOLAR_PRODUCTION_SENSOR: "solar_production_sensor_circular"}
    state = hass.states.get(sensor)
    if state is None:
        return {CONF_SOLAR_PRODUCTION_SENSOR: "solar_production_sensor_not_found"}
    if state.attributes.get("unit_of_measurement", "") not in ("W", "kW"):
        return {CONF_SOLAR_PRODUCTION_SENSOR: "solar_production_invalid_unit"}
    return {}


def _restore_unrenderable_sensors(
    hass: HomeAssistant, user_input: dict[str, Any], stored: dict[str, Any]
) -> dict[str, Any]:
    """Re-add optional sensors the entity picker could not render (#419).

    Home Assistant's ``ha-form`` drops a cleared field from ``user_input``, so an
    untouched field and a deliberately cleared one arrive here identically. The
    one case that stays distinguishable: the stored entity is missing from the
    state machine, which is exactly when ``ha-entity-picker`` shows an empty box
    and commits that emptiness on close. An empty submission is then not a user
    decision, so the stored value survives. Clearing a sensor whose entity does
    exist keeps working, and a preserved dead reference is surfaced by the
    ``configured_sensor_missing`` Repairs issue.
    """
    restored = dict(user_input)
    for key in (
        CONF_SOLAR_FORECAST_SENSOR,
        CONF_SOLAR_FORECAST_REMAINING_SENSOR,
        CONF_SOLAR_PRODUCTION_SENSOR,
        CONF_OFFGRID_POWER_SENSOR,
    ):
        current = stored.get(key)
        if restored.get(key) or not current:
            continue
        if hass.states.get(current) is None:
            _LOGGER.warning(
                "Options flow submitted %s empty while %s does not exist - keeping "
                "the stored sensor instead of clearing it",
                key, current,
            )
            restored[key] = current
    return restored


def _has_global_forecast_sensor(config) -> bool:
    """True when the sensors step already picked a solar forecast sensor."""
    return bool(
        config.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
        or config.get(CONF_SOLAR_FORECAST_SENSOR)
    )


def _resolve_forecast_sensor(hass, config, user_input, errors) -> str | None:
    """Resolve the per-mode solar forecast sensor, validating a supplied one.

    A sensor picked in the global sensors step wins and the per-mode field is
    not even shown; otherwise the optional field must carry a kWh/Wh unit.
    """
    if _has_global_forecast_sensor(config):
        return config.get(CONF_SOLAR_FORECAST_SENSOR)
    sensor = user_input.get("solar_forecast_sensor")
    if sensor:
        state = hass.states.get(sensor)
        if state is None:
            errors["solar_forecast_sensor"] = "sensor_not_found"
        elif state.attributes.get("unit_of_measurement", "") not in ("kWh", "Wh"):
            errors["solar_forecast_sensor"] = "invalid_unit"
    return sensor


def _phase_sensor_schema_field(key: str, default: str | None = None):
    """Return an optional phase current sensor field with its saved suggestion."""
    field = (
        vol.Optional(key, description={"suggested_value": default})
        if default
        else vol.Optional(key)
    )
    return field, EntitySelector(EntitySelectorConfig(domain="sensor"))


def _phase_protection_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the protection form schema with an optional pair per phase."""
    defaults = defaults or {}
    schema: dict = {}
    for key in (
        CONF_PHASE_1_CURRENT_SENSOR,
        CONF_PHASE_2_CURRENT_SENSOR,
        CONF_PHASE_3_CURRENT_SENSOR,
    ):
        field, selector = _phase_sensor_schema_field(key, defaults.get(key))
        schema[field] = selector
    for key in (
        CONF_PHASE_1_FUSE_SIZE,
        CONF_PHASE_2_FUSE_SIZE,
        CONF_PHASE_3_FUSE_SIZE,
    ):
        default = defaults.get(key)
        field = (
            vol.Optional(key, description={"suggested_value": default})
            if default is not None
            else vol.Optional(key)
        )
        schema[field] = NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=250,
                step=1,
                unit_of_measurement="A",
                mode=NumberSelectorMode.BOX,
            )
        )
    return vol.Schema(schema)


def _battery_phase_schema(default: str | None = None):
    """Return the phase selector used by battery limit and assignment forms."""
    return vol.Required(
        CONF_BATTERY_PHASE,
        default=normalize_battery_phase(default),
    ), SelectSelector(
        SelectSelectorConfig(
            options=[
                {"value": PHASE_UNASSIGNED, "label": "Unassigned"},
                {"value": PHASE_L1, "label": "L1"},
                {"value": PHASE_L2, "label": "L2"},
                {"value": PHASE_L3, "label": "L3"},
            ],
            translation_key="battery_phase",
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _validate_phase_protection(hass, user_input: dict[str, Any]) -> dict[str, str]:
    """Validate configured phase current sensors and fuse-size limits."""
    errors: dict[str, str] = {}
    phase_fields = (
        (
            CONF_PHASE_1_CURRENT_SENSOR,
            CONF_PHASE_1_FUSE_SIZE,
        ),
        (
            CONF_PHASE_2_CURRENT_SENSOR,
            CONF_PHASE_2_FUSE_SIZE,
        ),
        (
            CONF_PHASE_3_CURRENT_SENSOR,
            CONF_PHASE_3_FUSE_SIZE,
        ),
    )
    sensor_keys = tuple(sensor_key for sensor_key, _ in phase_fields)
    sensor_ids = [user_input.get(key) for key in sensor_keys]

    present_sensor_ids = [entity_id for entity_id in sensor_ids if entity_id]
    if len(set(present_sensor_ids)) != len(present_sensor_ids):
        for key, entity_id in zip(sensor_keys, sensor_ids):
            if entity_id and present_sensor_ids.count(entity_id) > 1:
                errors[key] = "phase_sensors_must_differ"

    for sensor_key, limit_key in phase_fields:
        entity_id = user_input.get(sensor_key)
        raw_limit = user_input.get(limit_key)
        sensor_present = bool(entity_id)
        limit_present = raw_limit not in (None, "")
        if sensor_present != limit_present:
            errors.setdefault(
                sensor_key if not sensor_present else limit_key,
                "phase_sensor_and_limit_required",
            )
            continue
        if not sensor_present:
            continue

        state = hass.states.get(entity_id) if entity_id else None
        if state is None:
            errors.setdefault(sensor_key, "phase_sensor_not_found")
            continue
        if not str(entity_id).startswith("sensor."):
            errors[sensor_key] = "phase_sensor_invalid_domain"
            continue
        unit = state.attributes.get("unit_of_measurement")
        if unit not in ("A", "mA"):
            errors[sensor_key] = "phase_sensor_invalid_unit"

        try:
            value = float(raw_limit)
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value) or value <= 0:
            errors[limit_key] = "phase_limit_must_be_positive"

    return errors


def _phase_assignment_is_valid(value: Any) -> bool:
    """Return whether a stored battery phase is normalized and usable."""
    # Empty values are accepted for legacy entries and normalized to the
    # explicit selector value when the form is submitted.
    return value in PHASE_VALUES or value == PHASE_UNASSIGNED or value in (None, "")


def _soc_selector_limits(
    brand: str, battery_version: str = DEFAULT_VERSION
) -> tuple[int, int, int, int, int, int]:
    """Return minimum and maximum SOC selector bounds and defaults.

    Read from the driver's ``DriverTraits`` (an unregistered brand: the default driver's).
    """
    traits = registry.traits_for_brand(brand, {CONF_BATTERY_VERSION: battery_version})
    min_lo, min_hi, min_default = traits.min_soc_range
    # Omnibattery enforces the charge ceiling in software. Sessy's reported SOC
    # spans 0–100 %, so the standard 100 % ceiling is valid for this driver.
    return min_lo, min_hi, min_default, traits.max_soc_low, 100, 100


_LOGGER = logging.getLogger(__name__)

_ALL_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# How many predictive-charging windows the user may configure.
MAX_CHARGING_WINDOWS = 3


def _setup_brand(brand: str | None) -> str:
    """The brand whose ``SETUP`` serves ``brand``: itself, or ``DEFAULT_BRAND`` if unregistered."""
    return brand if registry.is_registered(brand) else DEFAULT_BRAND


def _limits_setup(brand: str):
    """What the limits step and the slot forms ask: the ``SETUP`` of the brand's driver."""
    return registry.setup_for(_setup_brand(brand))


def _normalize_charging_windows(raw) -> list[dict]:
    """Config value (legacy single dict | list | None) → list of window dicts."""
    if not raw:
        return []
    if isinstance(raw, dict):
        return [raw]
    return list(raw)


def _parse_charging_windows(user_input: dict) -> tuple[list[dict], dict]:
    """Build the windows list from up to MAX_CHARGING_WINDOWS form rows.

    Window 1 is required (enforced by the schema). Rows 2..N are optional: a row
    with neither start nor end is skipped; a row with only one of them is an error.
    Returns (windows, errors).
    """
    windows: list[dict] = []
    errors: dict = {}
    for i in range(1, MAX_CHARGING_WINDOWS + 1):
        sfx = "" if i == 1 else f"_{i}"
        start = user_input.get(f"start_time{sfx}")
        end = user_input.get(f"end_time{sfx}")
        days = user_input.get(f"days{sfx}", _ALL_WEEKDAYS)
        if not start and not end:
            continue
        if not start or not end:
            errors[f"start_time{sfx}"] = "incomplete_window"
            continue
        windows.append({"start_time": start, "end_time": end, "days": days})
    return windows, errors


def _charging_window_schema_fields(existing_windows: list[dict]) -> dict:
    """Schema fragment for the window rows (row 1 required, rows 2..N optional)."""
    days_selector = SelectSelector(
        SelectSelectorConfig(
            options=_ALL_WEEKDAYS,
            translation_key="weekday",
            multiple=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )
    fields: dict = {}
    for i in range(1, MAX_CHARGING_WINDOWS + 1):
        sfx = "" if i == 1 else f"_{i}"
        existing = existing_windows[i - 1] if i - 1 < len(existing_windows) else None
        req = vol.Required if i == 1 else vol.Optional
        if existing:
            # Rows 2..N are clearable: a default would restore a row the user just emptied (#547).
            def prefill(value):
                return {"default": value} if i == 1 else {"description": {"suggested_value": value}}

            fields[req(f"start_time{sfx}", **prefill(existing["start_time"]))] = TimeSelector()
            fields[req(f"end_time{sfx}", **prefill(existing["end_time"]))] = TimeSelector()
            fields[vol.Optional(f"days{sfx}", default=existing.get("days", _ALL_WEEKDAYS))] = days_selector
        else:
            fields[req(f"start_time{sfx}")] = TimeSelector()
            fields[req(f"end_time{sfx}")] = TimeSelector()
            fields[vol.Optional(f"days{sfx}", default=_ALL_WEEKDAYS)] = days_selector
    return fields


def _time_ranges_overlap(start1: str, end1: str, start2: str, end2: str) -> bool:
    """Check if two time ranges overlap. Assumes start < end (no midnight crossing)."""
    from datetime import time as dt_time

    s1 = dt_time.fromisoformat(start1)
    e1 = dt_time.fromisoformat(end1)
    s2 = dt_time.fromisoformat(start2)
    e2 = dt_time.fromisoformat(end2)

    return s1 < e2 and s2 < e1


def _slots_overlap(new_slot: dict, existing_slots: list[dict]) -> bool:
    """Check if new_slot overlaps with any existing slot on shared days and scope.

    Two slots only conflict when they would compete for the same battery: either
    they share a concrete battery_scope, or one (or both) targets all batteries.
    """
    new_days = set(new_slot.get("days", []))
    new_scope = new_slot.get("battery_scope", SLOT_BATTERY_SCOPE_ALL)
    for slot in existing_slots:
        if not (new_days & set(slot.get("days", []))):
            continue
        scope = slot.get("battery_scope", SLOT_BATTERY_SCOPE_ALL)
        if scope != SLOT_BATTERY_SCOPE_ALL and new_scope != SLOT_BATTERY_SCOPE_ALL and scope != new_scope:
            continue
        if _time_ranges_overlap(
            new_slot["start_time"], new_slot["end_time"],
            slot["start_time"], slot["end_time"],
        ):
            return True
    return False


def _battery_scope_options(battery_configs: list[dict]) -> list[dict]:
    """Build battery scope selector options as {value, label} dicts.

    The label shows the user-facing battery name (CONF_NAME) when available,
    falling back to "Battery N" if the config dict has no name.
    """
    opts: list[dict] = [{"value": SLOT_BATTERY_SCOPE_ALL, "label": "All batteries"}]
    for i, bcfg in enumerate(battery_configs or []):
        name = bcfg.get(CONF_NAME) or f"Battery {i + 1}"
        opts.append({"value": f"battery_{i + 1}", "label": name})
    return opts


def _battery_count_schema(default: int = 1) -> vol.Schema:
    """Build the shared selector for the number of controllable batteries."""
    return vol.Schema(
        {
            vol.Required("num_batteries", default=default): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=MAX_BATTERIES, mode=NumberSelectorMode.SLIDER
                )
            )
        }
    )


def _scope_value_in_options(scope: str, opts: list[dict]) -> bool:
    return any(o["value"] == scope for o in opts)


def _battery_hardware_max(bcfg: dict, power_key: str | None = None) -> int:
    """Return the battery's hardware max power (W) for slot selectors.

    Marstek batteries identify their hardware envelope through
    ``battery_version``. Other drivers persist their discovered/configured
    charge and discharge ceilings instead, so falling back to the Marstek v2
    value would incorrectly cap their time-slot overrides at 2500 W.
    """
    ceiling = registry.driver_class(bcfg.get("brand", DEFAULT_BRAND)).slot_power_ceiling(bcfg)
    if ceiling is not None:
        return ceiling

    configured_limits: list[int] = []
    keys = (power_key,) if power_key else ("max_charge_power", "max_discharge_power")
    for key in keys:
        try:
            value = int(bcfg.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            configured_limits.append(value)
    if configured_limits:
        return max(configured_limits)

    return int(MAX_POWER_BY_VERSION.get(DEFAULT_VERSION, 2500))


def _scoped_battery_index(scope: str) -> int | None:
    """Parse "battery_N" → N-1. Returns None for "all" or invalid scope."""
    if not scope or scope == SLOT_BATTERY_SCOPE_ALL or not scope.startswith("battery_"):
        return None
    try:
        return int(scope.split("_", 1)[1]) - 1
    except (ValueError, IndexError):
        return None


def _scoped_battery_config(scope: str, battery_configs: list[dict]) -> dict:
    """Return the battery dict for `scope` (or {} for 'all' / invalid index)."""
    idx = _scoped_battery_index(scope)
    if idx is None:
        return {}
    if 0 <= idx < len(battery_configs):
        return battery_configs[idx]
    return {}


def _slot_target_indices(scope: str, num_batteries: int) -> list[int]:
    """Battery indices (0-based) covered by `scope`. Empty if scope invalid."""
    if scope == SLOT_BATTERY_SCOPE_ALL:
        return list(range(num_batteries))
    idx = _scoped_battery_index(scope)
    if idx is None or idx < 0 or idx >= num_batteries:
        return []
    return [idx]


def _battery_scope_name_map(battery_configs: list[dict]) -> str:
    """Human-readable list of 'battery_N → name' for description_placeholders."""
    parts = []
    for i, bcfg in enumerate(battery_configs or []):
        parts.append(f"battery_{i + 1} = {bcfg.get(CONF_NAME) or f'Battery {i + 1}'}")
    return ", ".join(parts) if parts else ""


def _clamp(val: int, low: int, high: int) -> int:
    return max(low, min(high, int(val)))


def _slot_field_key(battery_idx: int, field: str) -> str:
    """Step B form key: '<batteryN>__<field>'. Parsed back in _finalize_slot."""
    return f"battery_{battery_idx + 1}__{field}"


def _build_slot_step_a_schema(battery_configs: list[dict], defaults: dict) -> vol.Schema:
    """Step A: time, days, scope, allow ticks, SOC tick, power tick, mode."""
    scope_opts = _battery_scope_options(battery_configs)
    scope_default = defaults.get("battery_scope") or SLOT_BATTERY_SCOPE_ALL
    if not _scope_value_in_options(scope_default, scope_opts):
        scope_default = SLOT_BATTERY_SCOPE_ALL
    return vol.Schema({
        vol.Required("start_time", default=defaults.get("start_time") or "00:00:00"): TimeSelector(),
        vol.Required("end_time", default=defaults.get("end_time") or "00:00:00"): TimeSelector(),
        vol.Required("days", default=defaults.get("days") or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]):
            SelectSelector(SelectSelectorConfig(
                options=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                translation_key="weekday",
                multiple=True,
                mode=SelectSelectorMode.DROPDOWN,
            )),
        vol.Required("battery_scope", default=scope_default):
            SelectSelector(SelectSelectorConfig(
                options=scope_opts,
                multiple=False,
                mode=SelectSelectorMode.DROPDOWN,
            )),
        vol.Required("allow_charge", default=bool(defaults.get("allow_charge", DEFAULT_SLOT_ALLOW_CHARGE))): bool,
        vol.Required("allow_discharge", default=bool(defaults.get("allow_discharge", DEFAULT_SLOT_ALLOW_DISCHARGE))): bool,
        vol.Required("soc_override_enabled", default=bool(defaults.get("soc_override_enabled", DEFAULT_SLOT_SOC_OVERRIDE_ENABLED))): bool,
        vol.Required("power_override_enabled", default=bool(defaults.get("power_override_enabled", DEFAULT_SLOT_POWER_OVERRIDE_ENABLED))): bool,
        vol.Required("mode", default=defaults.get("mode") or DEFAULT_SLOT_MODE):
            SelectSelector(SelectSelectorConfig(
                options=[SLOT_MODE_PD, SLOT_MODE_MANUAL],
                translation_key="slot_mode",
                multiple=False,
                mode=SelectSelectorMode.LIST,
            )),
    })


def _build_slot_step_b_schema(
    needs_soc: bool,
    needs_power: bool,
    scope: str,
    battery_configs: list[dict],
    defaults: dict,
) -> vol.Schema:
    """Step B: optional SOC and/or power values, rendered per-battery.

    For each battery covered by `scope` (one for `battery_N`, all for `all`),
    render an independent set of fields keyed as `battery_<idx>__<field>`. The
    consumer (`_finalize_slot`) parses these into `slot["battery_limits"]`.

      - SOC sliders always range [12, 100].
      - Power sliders range [100, battery hardware max] per that specific battery.
      - Defaults pull from the slot's previous `battery_limits[battery_N]` if any,
        else from the battery's user-configured `min_soc`/`max_soc`/
        `max_charge_power`/`max_discharge_power`.
    """
    fields: dict = {}
    indices = _slot_target_indices(scope, len(battery_configs))
    prior = defaults.get("battery_limits") or {}
    for idx in indices:
        bcfg = battery_configs[idx]
        b_key = f"battery_{idx + 1}"
        b_prior = prior.get(b_key) or {}
        charge_max = _battery_hardware_max(bcfg, "max_charge_power")
        discharge_max = _battery_hardware_max(bcfg, "max_discharge_power")
        if needs_soc:
            soc_min_def = b_prior.get("soc_min") or int(bcfg.get("min_soc") or DEFAULT_SLOT_SOC_MIN_FLOOR)
            soc_max_def = b_prior.get("soc_max") or int(bcfg.get("max_soc") or DEFAULT_SLOT_SOC_MAX_CEILING)
            fields[vol.Required(
                _slot_field_key(idx, "soc_min"),
                default=_clamp(soc_min_def, DEFAULT_SLOT_SOC_MIN_FLOOR, 30),
            )] = NumberSelector(NumberSelectorConfig(
                min=DEFAULT_SLOT_SOC_MIN_FLOOR, max=30,
                step=1, mode=NumberSelectorMode.SLIDER,
            ))
            fields[vol.Required(
                _slot_field_key(idx, "soc_max"),
                default=_clamp(soc_max_def, 80, DEFAULT_SLOT_SOC_MAX_CEILING),
            )] = NumberSelector(NumberSelectorConfig(
                min=80, max=DEFAULT_SLOT_SOC_MAX_CEILING,
                step=1, mode=NumberSelectorMode.SLIDER,
            ))
        if needs_power:
            charge_def = b_prior.get("max_charge_power_w") or int(bcfg.get("max_charge_power") or charge_max)
            discharge_def = b_prior.get("max_discharge_power_w") or int(bcfg.get("max_discharge_power") or discharge_max)
            fields[vol.Required(
                _slot_field_key(idx, "max_charge_power_w"),
                default=_clamp(charge_def, 100, charge_max),
            )] = NumberSelector(NumberSelectorConfig(
                min=100, max=charge_max, step=50, unit_of_measurement="W",
                mode=NumberSelectorMode.SLIDER,
            ))
            fields[vol.Required(
                _slot_field_key(idx, "max_discharge_power_w"),
                default=_clamp(discharge_def, 100, discharge_max),
            )] = NumberSelector(NumberSelectorConfig(
                min=100, max=discharge_max, step=50, unit_of_measurement="W",
                mode=NumberSelectorMode.SLIDER,
            ))
    return vol.Schema(fields)


def _validate_slot_step_a(user_input: dict) -> dict:
    """Cross-field validation for step A. Returns errors dict (empty if valid)."""
    errors: dict = {}
    allow_c = bool(user_input.get("allow_charge"))
    allow_d = bool(user_input.get("allow_discharge"))
    if not (allow_c or allow_d):
        errors["base"] = "slot_does_nothing"
        return errors
    if user_input.get("mode") == SLOT_MODE_MANUAL and not user_input.get("power_override_enabled"):
        errors["base"] = "manual_requires_power"
        return errors
    if user_input.get("mode") == SLOT_MODE_MANUAL and allow_c and allow_d:
        errors["base"] = "manual_requires_single_direction"
        return errors
    if user_input["start_time"] >= user_input["end_time"]:
        errors["base"] = "midnight_crossing"
        return errors
    return errors


def _parse_step_b_battery_limits(step_b: dict | None) -> dict[str, dict]:
    """Group step B form fields by battery key.

    Field keys are encoded as `battery_<N>__<field>` (see _slot_field_key). The
    returned dict maps `battery_N` → `{soc_min, soc_max, max_charge_power_w,
    max_discharge_power_w}`, with int values. Missing fields are omitted.
    """
    if not step_b:
        return {}
    out: dict[str, dict] = {}
    for key, val in step_b.items():
        if "__" not in key:
            continue
        b_key, field = key.split("__", 1)
        if not b_key.startswith("battery_"):
            continue
        if val is None:
            continue
        try:
            out.setdefault(b_key, {})[field] = int(val)
        except (TypeError, ValueError):
            continue
    # Swap soc_min/soc_max if user inverted them
    for b_key, limits in out.items():
        if "soc_min" in limits and "soc_max" in limits and limits["soc_min"] > limits["soc_max"]:
            limits["soc_min"], limits["soc_max"] = limits["soc_max"], limits["soc_min"]
    return out


def _finalize_slot(
    step_a: dict, step_b: dict | None, existing: dict | None = None
) -> dict:
    """Merge step A and optional step B into the persisted slot shape.

    ``existing`` is the stored slot occupying this position, when there is one.
    Every stored key the form does not emit — today that is ``enabled``, written
    by the per-slot enable switch — is carried over from it, so re-saving the
    flow does not reset it to its default.

    The carry-over only happens when the form still describes the same schedule.
    Editing a slot into a different window replaces it, and a replacement must
    not inherit a disabled state that has no form field to reveal it.
    """
    soc_on = bool(step_a.get("soc_override_enabled", False))
    power_on = bool(step_a.get("power_override_enabled", False))
    parsed = _parse_step_b_battery_limits(step_b) if (soc_on or power_on) else {}
    # Strip fields that don't correspond to an enabled tick (defensive)
    battery_limits: dict[str, dict] = {}
    for b_key, limits in parsed.items():
        entry: dict = {}
        if soc_on:
            if "soc_min" in limits:
                entry["soc_min"] = limits["soc_min"]
            if "soc_max" in limits:
                entry["soc_max"] = limits["soc_max"]
        if power_on:
            if "max_charge_power_w" in limits:
                entry["max_charge_power_w"] = limits["max_charge_power_w"]
            if "max_discharge_power_w" in limits:
                entry["max_discharge_power_w"] = limits["max_discharge_power_w"]
        if entry:
            battery_limits[b_key] = entry
    slot = {
        "start_time": step_a["start_time"],
        "end_time": step_a["end_time"],
        "days": step_a["days"],
        "battery_scope": step_a.get("battery_scope", SLOT_BATTERY_SCOPE_ALL),
        "allow_charge": bool(step_a.get("allow_charge", False)),
        "allow_discharge": bool(step_a.get("allow_discharge", True)),
        "soc_override_enabled": soc_on,
        "power_override_enabled": power_on,
        "battery_limits": battery_limits,
        "mode": step_a.get("mode", DEFAULT_SLOT_MODE),
    }
    identity = ("start_time", "end_time", "days", "battery_scope")
    replaces_same_slot = bool(existing) and all(
        existing.get(key) == slot[key] for key in identity
    )
    merged = {**existing, **slot} if replaces_same_slot else dict(slot)
    merged.setdefault("enabled", True)
    return merged



def _mac_tracking_schema(defaults: dict) -> dict:
    """Schema entries for the per-battery "find me again by MAC" opt-in.

    Offered only for batteries addressed by IP; serial, ESPHome and MQTT are
    reached by a device path or device id, so they have no address that can
    drift. Defaults to off, so an install that never touches it is unaffected.
    """
    return {
        vol.Optional(
            CONF_TRACK_MAC, default=bool(defaults.get(CONF_TRACK_MAC, False))
        ): BooleanSelector(),
        vol.Optional(CONF_MAC, default=defaults.get(CONF_MAC) or ""): str,
    }


def _mac_defaults(hass, battery: dict) -> dict:
    """Battery values with the MAC pre-filled from Home Assistant's DHCP cache.

    Home Assistant keeps the leases it has seen, so on a setup where it shares a
    network with the batteries the field arrives already filled and the user
    only ticks the box. Where that cache is empty — Home Assistant on another
    subnet, or in a container without host networking — the field stays blank
    and is typed in by hand. The import is local and guarded because the dhcp
    component is not a declared dependency of this integration.
    """
    defaults = dict(battery)
    if defaults.get(CONF_MAC):
        return defaults
    try:
        from homeassistant.components import dhcp

        discovered = dhcp.async_discovered_service_info(hass)
    except Exception as err:  # noqa: BLE001 - absence of the cache is not an error
        # Expected on a large part of the supported range: the helper does not
        # exist before recent Home Assistant releases, and the dhcp component
        # itself may be absent. Log it so an empty field is explainable rather
        # than mysterious; the user types the MAC by hand.
        _LOGGER.debug("MAC auto-detection unavailable, manual entry required: %s", err)
        return defaults
    if detected := detect_mac(discovered or [], defaults.get(CONF_HOST) or ""):
        defaults[CONF_MAC] = detected
    return defaults


def _validate_mac_tracking(user_input: dict) -> str | None:
    """Return an error key when tracking is enabled without a usable MAC.

    Refusing here rather than storing a malformed value keeps the lookup in
    ``evaluate_lease`` a plain comparison: whatever is stored is already
    normalised, so a lease can never silently fail to match.
    """
    if not user_input.get(CONF_TRACK_MAC):
        return None
    if normalise_mac(user_input.get(CONF_MAC)) is None:
        return "invalid_mac"
    return None


def _apply_mac_tracking(user_input: dict, merged: dict) -> None:
    """Persist the opt-in and the normalised MAC onto a battery entry."""
    enabled = bool(user_input.get(CONF_TRACK_MAC))
    merged[CONF_TRACK_MAC] = enabled
    merged[CONF_MAC] = (normalise_mac(user_input.get(CONF_MAC)) or "") if enabled else ""


def _restore_submitted_values(data_schema: vol.Schema, user_input: dict[str, Any]) -> vol.Schema:
    """Return a form schema that prefills the values submitted with an error."""
    if not isinstance(data_schema.schema, dict):
        return data_schema

    schema: dict = {}
    for marker, validator in data_schema.schema.items():
        if (
            not isinstance(marker, vol.Marker)
            or not isinstance(marker.schema, str)
            or marker.schema not in user_input
        ):
            schema[marker] = validator
            continue

        value = user_input[marker.schema]
        restored_marker = copy.copy(marker)
        if isinstance(validator, Selector):
            description = (
                dict(marker.description)
                if isinstance(marker.description, dict)
                else {}
            )
            description["suggested_value"] = value
            restored_marker.description = description
        if not (
            isinstance(validator, (EntitySelector, DeviceSelector))
            and isinstance(marker, vol.Optional)
        ):
            restored_marker.default = lambda value=value: value
        schema[restored_marker] = validator

    return vol.Schema(schema, required=data_schema.required, extra=data_schema.extra)


def _wrap_preserving_values(name, method):
    """The step ``method`` wrapped to restore submitted values in error forms.

    Returns ``None`` when ``method`` is not a step with a ``user_input`` argument
    or is already wrapped.
    """
    # Read parameter names from the code object, not inspect.signature():
    # on Python 3.14 that evaluates annotations, and inherited HA steps
    # annotate with TYPE_CHECKING-only names (BluetoothServiceInfoBleak).
    code = method.__code__
    params = code.co_varnames[: code.co_argcount]
    if (
        not name.startswith("async_step_")
        or "user_input" not in params
        or getattr(method, "_preserves_submitted_values", False)
    ):
        return None

    # Position of user_input among the args passed after self.
    position = params.index("user_input") - 1

    @functools.wraps(method)
    async def wrapped(self, *args, __method=method, __position=position, **kwargs):
        submitted = kwargs.get(
            "user_input", args[__position] if len(args) > __position else None
        )
        result = await __method(self, *args, **kwargs)
        if (
            not isinstance(submitted, dict)
            or not isinstance(result, dict)
            or result.get("type") != "form"
            or not result.get("errors")
        ):
            return result

        data_schema = result.get("data_schema")
        if not isinstance(data_schema, vol.Schema):
            return result
        updated = dict(result)
        updated["data_schema"] = _restore_submitted_values(data_schema, submitted)
        return updated

    wrapped._preserves_submitted_values = True
    return wrapped


def _preserve_values_in_error_forms(flow_class):
    """Wrap flow steps so validation errors do not discard their submitted values."""
    for name, method in inspect.getmembers(flow_class, inspect.iscoroutinefunction):
        wrapped = _wrap_preserving_values(name, method)
        if wrapped is not None:
            setattr(flow_class, name, wrapped)
    return flow_class


def _flow_kind(flow) -> FlowKind:
    if isinstance(flow, OptionsFlowHandler):
        return FlowKind.OPTIONS
    if getattr(flow, "source", None) == SOURCE_RECONFIGURE:
        return FlowKind.RECONFIGURE
    return FlowKind.CONFIG


def _stored_battery(flow, entry) -> dict | None:
    if entry is None:
        return None
    batteries = entry.data.get("batteries", [])
    return batteries[flow.battery_index] if flow.battery_index < len(batteries) else None


def _live_limits(ctx: SetupContext, brand: str) -> dict:
    """The limits the loaded battery works with now; empty when it is not loaded.

    The entities change them after setup, and some are only known to the device.
    """
    coordinator = ctx.coordinator
    if coordinator is None or not isinstance(coordinator.driver, registry.driver_class(brand)):
        return {}
    live = {
        "max_charge_power": coordinator.configured_max_charge_power,
        "max_discharge_power": coordinator.configured_max_discharge_power,
    }
    # Elsewhere the SOC limit may be raised for a while (weekly full charge) and
    # the stored one is the user's; here the device keeps it.
    if coordinator.traits.device_owns_initial_config:
        live["max_soc"] = int(coordinator.max_soc)
        live["min_soc"] = int(coordinator.min_soc)
    return live


def _configured_power(battery: dict, live: dict, key: str, fallback: int) -> int:
    """The user's power limit: the loaded one, the slider's stored one, the form's."""
    return int(live.get(key, battery.get(f"user_{key}", battery.get(key, fallback))))


def _setup_context(flow, brand: str) -> SetupContext:
    """The context a brand's step handler receives (built per call; ``work`` persists)."""
    kind = _flow_kind(flow)
    if kind is FlowKind.OPTIONS:
        entry = flow.config_entry
    elif kind is FlowKind.RECONFIGURE:
        entry = flow._get_reconfigure_entry()
    else:
        entry = None
    return SetupContext(
        kind=kind,
        battery=_stored_battery(flow, entry),
        entry=entry,
        coordinator=live_coordinator(flow, entry, flow.battery_index),
        work=work_dict(flow, brand),
    )


async def _run_driver_step(flow, brand: str, handler, user_input):
    """Run a generated step: a FlowResult goes back as is, battery data is merged."""
    ctx = _setup_context(flow, brand)
    result = await handler(flow, user_input, ctx)
    if is_flow_result(result):
        return result
    if ctx.kind is not FlowKind.RECONFIGURE:
        flow._current_battery_data.update(result)
        return await flow.async_step_battery_limits()
    # Reconfigure: lay the data over the stored battery, then next battery or save.
    entry = ctx.entry
    if isinstance(result, ReplacesBattery):
        updated = dict(result)
    else:
        updated = dict(ctx.battery or {})
        updated.update(result)
    flow._reconfigure_batteries.append(updated)
    flow.battery_index += 1
    if flow.battery_index >= len(entry.data.get("batteries", [])):
        return flow.async_update_reload_and_abort(
            entry, data_updates={"batteries": flow._reconfigure_batteries}
        )
    return await flow.async_step_reconfigure_battery()


# Step id of the shared reconfigure dispatcher (``async_step_reconfigure_battery``).
# A brand may use it as its own step id; no method is generated over the dispatcher,
# which runs that handler itself.
_DISPATCHER_STEP = "reconfigure_battery"


def _install_driver_steps(flow_class, setup, brand: str, setter=setattr) -> list[str]:
    """Generate ``flow_class.async_step_<id>`` for every step of ``setup``.

    Each method is a real coroutine function with a real ``user_input`` parameter
    (no ``__getattr__``: a missing attribute must keep raising ``AttributeError``).
    Order: the flow classes are decorated with ``_preserve_values_in_error_forms``
    when the module is imported, before any brand's steps are installed (see
    ``_install_registered_driver_steps`` at the end of the module), so the
    decorator never sees these methods. This function therefore applies the same
    wrapper itself; were it run before the decorator, the decorator would skip the
    already-wrapped method. ``setter(cls, name, method)`` lets a test undo the
    install; the names installed are returned.
    """
    installed = []
    for step_id, handler in setup.steps.items():
        if step_id == _DISPATCHER_STEP:
            continue  # the shared dispatcher is a real method; see async_step_reconfigure_battery
        name = f"async_step_{step_id}"

        async def step(self, user_input=None, __handler=handler, __brand=brand):
            return await _run_driver_step(self, __brand, __handler, user_input)

        step.__name__ = name
        step.__qualname__ = f"{flow_class.__name__}.{name}"
        step.__doc__ = handler.__doc__
        method = _wrap_preserving_values(name, step) or step
        setter(flow_class, name, method)
        installed.append(name)
    return installed


def _brand_selector_options() -> list[dict[str, str]]:
    """Brand selector options: registry order and each driver's ``LABEL``."""
    return [
        {"value": brand, "label": cls.LABEL}
        for brand, cls in registry.DRIVERS.items()
    ]


async def _dispatch_brand_entry(flow, brand: str):
    """Go to the ``SETUP.entry_step`` of the brand's driver (``DEFAULT_BRAND`` if unregistered)."""
    brand = _setup_brand(brand)
    setup = registry.setup_for(brand)
    work_dict(flow, brand, reset=True)
    return await getattr(flow, f"async_step_{setup.entry_step}")()


@_preserve_values_in_error_forms
class MarstekVenusConfigFlow(LegacyDomainMigrationMixin, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Omnibattery."""

    VERSION = 21

    def __init__(self):
        """Initialize the config flow."""
        self.config_data = {}
        self.battery_configs = []
        self.battery_index = 0
        self.time_slots = []
        self.excluded_devices = []
        self._current_battery_data = {}  # Stores connection data between battery steps
        self._pending_slot_step_a: dict | None = None  # Buffer between slot step A and step B
        self._restore_declined = False  # Set when user skips the config-backup restore

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Ask for the consumption sensor and optional solar forecast sensor."""
        # Rebrand migration: a HACS domain rename leaves the legacy
        # marstek_venus_energy_manager config entries in .storage. The new domain
        # starts with zero entries, so the config flow is the only entry point HA
        # exposes — route to the seamless migration before any fresh setup.
        if async_has_legacy_entries(self.hass):
            return await self.async_step_migrate_legacy()

        # Full-delete recovery: if the integration was removed entirely (no legacy
        # and no current entries) but a config backup survived, offer to restore
        # it before falling through to a from-scratch setup.
        if (
            user_input is None
            and not self._restore_declined
            and not self._async_current_entries()
            and await async_has_config_backup(self.hass)
        ):
            return await self.async_step_restore_backup()

        errors = {}

        if user_input is not None:
            # The old field remains explicitly whole-day; the new field is the
            # provider's post-now value. Saving it replaces the legacy whole-day
            # value instead of persisting both forecast horizons.
            forecast_sensor = user_input.get(CONF_SOLAR_FORECAST_SENSOR)
            remaining_sensor = user_input.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
            solar_sensor = user_input.get(CONF_SOLAR_PRODUCTION_SENSOR)
            forecast_candidates = (
                ((CONF_SOLAR_FORECAST_REMAINING_SENSOR, remaining_sensor),)
                if remaining_sensor
                else ((CONF_SOLAR_FORECAST_SENSOR, forecast_sensor),)
            )
            for key, sensor in forecast_candidates:
                if sensor:
                    forecast_state = self.hass.states.get(sensor)
                    if forecast_state is None:
                        errors[key] = "sensor_not_found"
                    elif forecast_state.attributes.get("unit_of_measurement", "") not in ["kWh", "Wh"]:
                        errors[key] = "invalid_unit"

            errors.update(_validate_solar_production_sensor(self.hass, user_input))

            errors.update(_validate_offgrid_power_sensor(self.hass, user_input))

            if not errors:
                self.config_data["consumption_sensor"] = user_input["consumption_sensor"]
                offgrid_sensor = user_input.get(CONF_OFFGRID_POWER_SENSOR) or None
                if offgrid_sensor:
                    self.config_data[CONF_OFFGRID_POWER_SENSOR] = offgrid_sensor
                    self.config_data[CONF_OFFGRID_METER_INVERTED] = user_input.get(
                        CONF_OFFGRID_METER_INVERTED, False
                    )
                    self.config_data[CONF_OFFGRID_MODE_ENABLED] = False
                if remaining_sensor:
                    self.config_data.pop(CONF_SOLAR_FORECAST_SENSOR, None)
                    self.config_data[CONF_SOLAR_FORECAST_REMAINING_SENSOR] = remaining_sensor
                else:
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    self.config_data[CONF_SOLAR_FORECAST_REMAINING_SENSOR] = None
                self.config_data[CONF_SOLAR_PRODUCTION_SENSOR] = solar_sensor
                self.config_data[CONF_METER_INVERTED] = user_input.get(CONF_METER_INVERTED, False)
                self.config_data["max_contracted_power"] = user_input["max_contracted_power"]
                self.config_data[CONF_THREE_PHASE_ENABLED] = bool(
                    user_input.get(CONF_THREE_PHASE_ENABLED, DEFAULT_THREE_PHASE_ENABLED)
                )
                if self.config_data[CONF_THREE_PHASE_ENABLED]:
                    return await self.async_step_three_phase()
                return await self.async_step_batteries()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("consumption_sensor"):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(CONF_METER_INVERTED, default=False):
                        BooleanSelector(),
                    vol.Required("max_contracted_power", default=7000):
                        NumberSelector(
                            NumberSelectorConfig(
                                min=500, max=20000, step=100, mode=NumberSelectorMode.BOX
                            )
                        ),
                    vol.Optional(CONF_SOLAR_FORECAST_SENSOR):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(CONF_SOLAR_FORECAST_REMAINING_SENSOR):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(CONF_SOLAR_PRODUCTION_SENSOR):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(CONF_OFFGRID_POWER_SENSOR):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(CONF_OFFGRID_METER_INVERTED, default=False):
                        BooleanSelector(),
                    vol.Optional(
                        CONF_THREE_PHASE_ENABLED,
                        default=DEFAULT_THREE_PHASE_ENABLED,
                    ): BooleanSelector(),
                }
            ),
            errors=errors if errors else None,
        )

    async def async_step_three_phase(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure the optional per-phase safety sensors and limits."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_phase_protection(self.hass, user_input)
            if not errors:
                self.config_data.update(
                    {
                        key: user_input.get(key)
                        for key in (
                            CONF_PHASE_1_CURRENT_SENSOR,
                            CONF_PHASE_2_CURRENT_SENSOR,
                            CONF_PHASE_3_CURRENT_SENSOR,
                            CONF_PHASE_1_FUSE_SIZE,
                            CONF_PHASE_2_FUSE_SIZE,
                            CONF_PHASE_3_FUSE_SIZE,
                        )
                    }
                )
                return await self.async_step_batteries()

        return self.async_show_form(
            step_id="three_phase",
            data_schema=_phase_protection_schema(),
            errors=errors or None,
        )

    async def async_step_restore_backup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Offer to restore a previous configuration after a full delete.

        Reached from ``async_step_user`` only when nothing is left to migrate but
        a config backup survived the deletion. Restoring recreates the entry with
        its original data + options; entities reclaim their entity_ids (and the
        recorder history keyed by them). Declining falls through to fresh setup.
        """
        records = await async_load_config_backup(self.hass)
        if not records:
            self._restore_declined = True
            return await self.async_step_user()

        if user_input is None:
            return self.async_show_form(
                step_id="restore_backup",
                data_schema=vol.Schema(
                    {vol.Required("restore", default=True): BooleanSelector()}
                ),
                description_placeholders={"count": str(len(records))},
            )

        if not user_input["restore"]:
            self._restore_declined = True
            return await self.async_step_user()

        restored = await async_restore_config_backup(self.hass)
        return self.async_abort(
            reason="restore_successful",
            description_placeholders={"count": str(len(restored))},
        )

    async def async_step_batteries(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Ask for the number of batteries."""
        if user_input is not None:
            self.config_data["num_batteries"] = int(user_input["num_batteries"])
            return await self.async_step_battery_brand()

        return self.async_show_form(
            step_id="batteries",
            data_schema=_battery_count_schema(),
        )

    async def async_step_battery_brand(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3a: Select battery brand."""
        battery_num = self.battery_index + 1
        if user_input is not None:
            brand = user_input["brand"]
            self._current_battery_data = {"brand": brand}
            return await _dispatch_brand_entry(self, brand)

        return self.async_show_form(
            step_id="battery_brand",
            data_schema=vol.Schema(
                {
                    vol.Required("brand", default=DEFAULT_BRAND):
                        SelectSelector(SelectSelectorConfig(
                            options=_brand_selector_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )),
                }
            ),
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_limits(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3c: Power and SOC limits for the current battery."""
        errors: dict[str, str] = {}
        battery_num = self.battery_index + 1
        brand = self._current_battery_data.get("brand", DEFAULT_BRAND)
        limits = _limits_setup(brand)
        ctx = _setup_context(self, brand)
        max_charge_power, max_discharge_power = limits.power_ceilings(self._current_battery_data)
        (
            soc_min_lo,
            soc_min_hi,
            soc_min_default,
            soc_max_lo,
            soc_max_hi,
            soc_max_default,
        ) = _soc_selector_limits(brand, self._current_battery_data.get(CONF_BATTERY_VERSION, DEFAULT_VERSION))

        if user_input is not None:
            phase = user_input.get(CONF_BATTERY_PHASE, "")
            if self.config_data.get(CONF_THREE_PHASE_ENABLED) and not _phase_assignment_is_valid(phase):
                errors[CONF_BATTERY_PHASE] = "battery_phase_required"
            if mac_error := _validate_mac_tracking(user_input):
                errors[CONF_MAC] = mac_error
            if errors:
                user_input = None
            else:
                merged = dict(self._current_battery_data)
                # The form may not offer some of them (the brand's hook sets them).
                for key in ("max_charge_power", "max_discharge_power", "max_soc", "min_soc"):
                    if key in user_input:
                        merged[key] = int(user_input[key])
                if self.config_data.get(CONF_THREE_PHASE_ENABLED):
                    merged[CONF_BATTERY_PHASE] = normalize_battery_phase(phase)
                limits.finalize_limits(merged, user_input, self._current_battery_data, ctx)
                # Hysteresis is mandatory; floor the percent against SOC drift.
                merged["enable_charge_hysteresis"] = True
                merged["charge_hysteresis_percent"] = max(
                    MIN_CHARGE_HYSTERESIS_PERCENT,
                    int(user_input.get(
                        "charge_hysteresis_percent",
                        merged.get("charge_hysteresis_percent", DEFAULT_CHARGE_HYSTERESIS_PERCENT),
                    )),
                )
                _apply_mac_tracking(user_input, merged)
                self.battery_configs.append(merged)
                self.battery_index += 1

                if self.battery_index >= self.config_data["num_batteries"]:
                    self.config_data["batteries"] = self.battery_configs
                    return await self.async_step_time_slots()
                return await self.async_step_battery_brand()

        _schema: dict = {}
        default_charge = max(
            100,
            min(max_charge_power, int(self._current_battery_data.get("max_charge_power", max_charge_power))),
        )
        default_discharge = max(
            100,
            min(max_discharge_power, int(self._current_battery_data.get("max_discharge_power", max_discharge_power))),
        )
        _schema[vol.Required("max_charge_power", default=default_charge)] = NumberSelector(
            NumberSelectorConfig(min=100, max=max_charge_power, step=50, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)
        )
        _schema[vol.Required("max_discharge_power", default=default_discharge)] = NumberSelector(
            NumberSelectorConfig(min=100, max=max_discharge_power, step=50, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)
        )
        _schema.update({
            vol.Required("max_soc", default=soc_max_default):
                NumberSelector(NumberSelectorConfig(min=soc_max_lo, max=soc_max_hi, step=1, mode=NumberSelectorMode.SLIDER)),
            vol.Required(
                "min_soc",
                default=max(soc_min_lo, min(soc_min_hi, int(self._current_battery_data.get("min_soc", soc_min_default)))),
            ): NumberSelector(NumberSelectorConfig(min=soc_min_lo, max=soc_min_hi, step=1, mode=NumberSelectorMode.SLIDER)),
            vol.Required("charge_hysteresis_percent", default=DEFAULT_CHARGE_HYSTERESIS_PERCENT):
                NumberSelector(NumberSelectorConfig(min=MIN_CHARGE_HYSTERESIS_PERCENT, max=MAX_CHARGE_HYSTERESIS_PERCENT, step=1, mode=NumberSelectorMode.SLIDER)),
        })
        _schema = limits.limits_schema(self._current_battery_data, _schema, ctx)
        if self.config_data.get(CONF_THREE_PHASE_ENABLED):
            # Keep the established L1 suggestion for a brand-new setup while
            # allowing the explicit Unassigned option when needed.
            phase_field, phase_selector = _battery_phase_schema(PHASE_L1)
            _schema[phase_field] = phase_selector
        if is_ip_based(self._current_battery_data):
            _schema.update(
                _mac_tracking_schema(_mac_defaults(self.hass, self._current_battery_data))
            )
        return self.async_show_form(
            step_id="battery_limits",
            data_schema=vol.Schema(_schema),
            errors=errors or None,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_time_slots(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 4: Ask if user wants to configure time slots."""
        if user_input is not None:
            if user_input.get("configure_time_slots", False):
                return await self.async_step_add_time_slot()
            else:
                # No time slots configured, move to excluded devices
                self.config_data["no_discharge_time_slots"] = []
                return await self.async_step_excluded_devices()

        return self.async_show_form(
            step_id="time_slots",
            data_schema=vol.Schema(
                {
                    vol.Required("configure_time_slots", default=False): bool,
                }
            ),
            description_placeholders={
                "description": "Configure time slots that independently control when batteries may charge or discharge"
            },
        )

    async def async_step_add_time_slot(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 5A: Configure base attributes of a time slot."""
        slot_num = len(self.time_slots) + 1
        errors: dict = {}

        if user_input is not None:
            errors = _validate_slot_step_a(user_input)
            if not errors:
                if _slots_overlap(
                    {
                        "start_time": user_input["start_time"],
                        "end_time": user_input["end_time"],
                        "days": user_input["days"],
                        "battery_scope": user_input.get("battery_scope", SLOT_BATTERY_SCOPE_ALL),
                    },
                    self.time_slots,
                ):
                    errors["base"] = "overlapping_slots"
            if not errors:
                self._pending_slot_step_a = dict(user_input)
                if user_input.get("soc_override_enabled") or user_input.get("power_override_enabled"):
                    return await self.async_step_add_time_slot_details()
                return await self._finalize_time_slot(step_b=None)

        defaults = self._slot_defaults_from_existing(len(self.time_slots))
        if user_input:
            defaults = {**defaults, **user_input}

        return self.async_show_form(
            step_id="add_time_slot",
            data_schema=_build_slot_step_a_schema(self.battery_configs, defaults),
            errors=errors if errors else None,
            description_placeholders={"slot_num": str(slot_num)},
        )

    async def async_step_add_time_slot_details(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 5B: Optional SOC / power detail fields for the pending slot."""
        if self._pending_slot_step_a is None:
            return await self.async_step_add_time_slot()

        step_a = self._pending_slot_step_a
        scope = step_a.get("battery_scope", SLOT_BATTERY_SCOPE_ALL)
        needs_soc = bool(step_a.get("soc_override_enabled"))
        needs_power = bool(step_a.get("power_override_enabled"))
        slot_num = len(self.time_slots) + 1

        if user_input is not None:
            return await self._finalize_time_slot(step_b=user_input)

        defaults = self._slot_defaults_from_existing(len(self.time_slots))
        return self.async_show_form(
            step_id="add_time_slot_details",
            data_schema=_build_slot_step_b_schema(needs_soc, needs_power, scope, self.battery_configs, defaults),
            description_placeholders={
                "slot_num": str(slot_num),
                "battery_map": _battery_scope_name_map(self.battery_configs),
            },
        )

    async def _finalize_time_slot(self, step_b: dict | None) -> FlowResult:
        """Persist the pending slot and advance the flow."""
        if self._pending_slot_step_a is None:
            return await self.async_step_add_time_slot()
        slot = _finalize_slot(self._pending_slot_step_a, step_b)
        self.time_slots.append(slot)
        self._pending_slot_step_a = None
        if len(self.time_slots) < MAX_TIME_SLOTS:
            return await self.async_step_add_more_slots()
        self.config_data["no_discharge_time_slots"] = self.time_slots
        return await self.async_step_excluded_devices()

    def _slot_defaults_from_existing(self, index: int) -> dict:
        """Return previously-saved slot at `index`, or empty dict if none."""
        existing = self.config_data.get("no_discharge_time_slots", []) or []
        if 0 <= index < len(existing):
            return dict(existing[index])
        return {}

    async def async_step_add_more_slots(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 6: Ask if user wants to add more time slots."""
        if user_input is not None:
            if user_input.get("add_more", False):
                return await self.async_step_add_time_slot()
            else:
                # User finished adding slots, move to excluded devices
                self.config_data["no_discharge_time_slots"] = self.time_slots
                return await self.async_step_excluded_devices()

        return self.async_show_form(
            step_id="add_more_slots",
            data_schema=vol.Schema(
                {
                    vol.Required("add_more", default=False): bool,
                }
            ),
            description_placeholders={
                "current_slots": str(len(self.time_slots)),
                "max_slots": str(MAX_TIME_SLOTS),
            },
        )

    async def async_step_excluded_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 7: Ask if user wants to configure excluded devices."""
        if user_input is not None:
            if user_input.get("configure_excluded_devices", False):
                return await self.async_step_add_excluded_device()
            else:
                # No excluded devices configured, move to predictive charging
                self.config_data["excluded_devices"] = []
                return await self.async_step_predictive_charging()

        return self.async_show_form(
            step_id="excluded_devices",
            data_schema=vol.Schema(
                {
                    vol.Required("configure_excluded_devices", default=False): bool,
                }
            ),
            description_placeholders={
                "description": "Configure devices that should NOT be powered by battery"
            },
        )

    async def async_step_add_excluded_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 8: Add an excluded device configuration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            ev_no_telemetry = user_input.get("ev_charger_no_telemetry", False)
            dynamic_power_control = user_input.get("dynamic_power_control", False)
            power_sensor = user_input.get("power_sensor") or None
            activity_sensor = user_input.get("activity_sensor") or None
            remaining_demand_sensor = user_input.get("remaining_demand_sensor") or None
            remaining_demand_presence_sensor = (
                user_input.get("remaining_demand_presence_sensor") or None
            )
            if ev_no_telemetry:
                # Existing entries used power_sensor for this state entity.
                # New entries store it explicitly; runtime keeps the fallback.
                activity_sensor = activity_sensor or power_sensor
                if not activity_sensor:
                    errors["activity_sensor"] = "missing_activity_sensor"
            elif not power_sensor:
                errors["power_sensor"] = "missing_power_sensor"
            if dynamic_power_control and not activity_sensor:
                errors["activity_sensor"] = "missing_activity_sensor"

        if user_input is not None and not errors:
            # Save the excluded device
            excluded_device = {
                "power_sensor": power_sensor,
                "activity_sensor": activity_sensor,
                "remaining_demand_sensor": remaining_demand_sensor,
                "remaining_demand_presence_sensor": remaining_demand_presence_sensor,
                "included_in_consumption": user_input.get("included_in_consumption", True),
                "allow_solar_surplus": user_input.get("allow_solar_surplus", False),
                "dynamic_power_control": dynamic_power_control,
                "cover_home_when_active": user_input.get("cover_home_when_active", False),
                "ev_charger_no_telemetry": ev_no_telemetry,
                "is_ev_charger": user_input.get("is_ev_charger", False),
            }
            self.excluded_devices.append(excluded_device)

            # Check if user wants to add more devices (max 4)
            if len(self.excluded_devices) < 4:
                return await self.async_step_add_more_excluded_devices()
            else:
                # Max devices reached, move to predictive charging
                self.config_data["excluded_devices"] = self.excluded_devices
                return await self.async_step_predictive_charging()

        device_num = len(self.excluded_devices) + 1
        return self.async_show_form(
            step_id="add_excluded_device",
            data_schema=vol.Schema(
                {
                    vol.Optional("power_sensor"):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional("activity_sensor"):
                        EntitySelector(EntitySelectorConfig(domain=["sensor", "binary_sensor"])),
                    vol.Required("included_in_consumption", default=True): bool,
                    vol.Optional("allow_solar_surplus", default=False): bool,
                    vol.Optional("dynamic_power_control", default=False): bool,
                    vol.Optional("cover_home_when_active", default=False): bool,
                    vol.Optional("ev_charger_no_telemetry", default=False): bool,
                    vol.Optional("is_ev_charger", default=False): bool,
                    vol.Optional("remaining_demand_sensor"):
                        EntitySelector(
                            # No device_class filter: _read_sensor_kwh_opt accepts any
                            # convertible energy unit, and a template helper
                            # often carries no device_class at all.
                            EntitySelectorConfig(domain="sensor")
                        ),
                    vol.Optional("remaining_demand_presence_sensor"):
                        EntitySelector(
                            EntitySelectorConfig(
                                domain=["binary_sensor", "sensor", "device_tracker"]
                            )
                        ),
                }
            ),
            description_placeholders={
                "device_num": str(device_num),
                "description": f"Configure excluded device {device_num}"
            },
            errors=errors or None,
        )

    async def async_step_add_more_excluded_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 9: Ask if user wants to add more excluded devices."""
        if user_input is not None:
            if user_input.get("add_more", False):
                return await self.async_step_add_excluded_device()
            else:
                # User finished adding devices, move to predictive charging
                self.config_data["excluded_devices"] = self.excluded_devices
                return await self.async_step_predictive_charging()

        return self.async_show_form(
            step_id="add_more_excluded_devices",
            data_schema=vol.Schema(
                {
                    vol.Required("add_more", default=False): bool,
                }
            ),
            description_placeholders={
                "current_devices": str(len(self.excluded_devices)),
                "max_devices": "4",
            },
        )

    async def async_step_predictive_charging(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 10: Ask if user wants to configure predictive grid charging."""
        if user_input is not None:
            if user_input.get("configure_predictive_charging", False):
                return await self.async_step_predictive_charging_mode()
            else:
                # Predictive charging disabled - preserve global sensor if set in step 1
                self.config_data["enable_predictive_charging"] = False
                self.config_data["charging_time_slot"] = None
                self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_TIME_SLOT
                if not self.config_data.get(CONF_SOLAR_FORECAST_SENSOR):
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = None
                return await self._finish_setup()

        return self.async_show_form(
            step_id="predictive_charging",
            data_schema=vol.Schema(
                {
                    vol.Required("configure_predictive_charging", default=False): bool,
                }
            ),
        )

    async def async_step_predictive_charging_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 10b: Select predictive charging mode (Time Slot vs Dynamic Pricing)."""
        if user_input is not None:
            mode = user_input.get(CONF_PREDICTIVE_CHARGING_MODE, PREDICTIVE_MODE_TIME_SLOT)
            self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = mode
            if mode == PREDICTIVE_MODE_DYNAMIC_PRICING:
                return await self.async_step_dynamic_pricing_config()
            elif mode == PREDICTIVE_MODE_REALTIME_PRICE:
                return await self.async_step_realtime_price_config()
            else:
                return await self.async_step_predictive_charging_config()

        return self.async_show_form(
            step_id="predictive_charging_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PREDICTIVE_CHARGING_MODE, default=PREDICTIVE_MODE_TIME_SLOT):
                        SelectSelector(
                            SelectSelectorConfig(
                                options=[
                                    PREDICTIVE_MODE_TIME_SLOT,
                                    PREDICTIVE_MODE_DYNAMIC_PRICING,
                                    PREDICTIVE_MODE_REALTIME_PRICE,
                                ],
                                translation_key="predictive_charging_mode",
                                mode=SelectSelectorMode.LIST,
                            )
                        ),
                }
            ),
        )

    async def async_step_predictive_charging_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 11a: Configure time slot predictive grid charging.

        Only the charging windows and the optional per-mode forecast sensor
        live here; every other predictive knob is a dashboard entity.
        """
        errors: dict[str, str] = {}
        existing_config = self.config_data

        if user_input is not None:
            try:
                forecast_sensor = _resolve_forecast_sensor(
                    self.hass, existing_config, user_input, errors
                )
                windows, window_errors = _parse_charging_windows(user_input)
                errors.update(window_errors)

                if not errors:
                    self.config_data["enable_predictive_charging"] = True
                    self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_TIME_SLOT
                    self.config_data["charging_time_slot"] = windows
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    return await self._finish_setup()
            except Exception as e:
                _LOGGER.error("Error validating predictive charging config: %s", e)
                errors["base"] = "unknown"

        schema_dict = _charging_window_schema_fields(
            _normalize_charging_windows(existing_config.get("charging_time_slot"))
        )
        if not _has_global_forecast_sensor(existing_config):
            default_forecast = existing_config.get("solar_forecast_sensor", "")
            schema_dict[vol.Optional(
                "solar_forecast_sensor",
                description={"suggested_value": default_forecast} if default_forecast else {},
            )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        return self.async_show_form(
            step_id="predictive_charging_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_dynamic_pricing_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 11b: Configure dynamic pricing predictive grid charging.

        Only the price sources live here: they need validating against
        ``hass.states`` and a platform reload. Thresholds, feature toggles and
        every power/SOC limit are dashboard entities writing the same keys.
        """
        errors: dict[str, str] = {}
        existing_config = self.config_data

        if user_input is not None:
            try:
                integration_type = user_input[CONF_PRICE_INTEGRATION_TYPE]
                price_sensor = user_input.get(CONF_PRICE_SENSOR)

                # Tibber has no price sensor - it polls the tibber.get_prices service.
                if integration_type == PRICE_INTEGRATION_TIBBER:
                    price_sensor = None
                    if not self.hass.services.has_service("tibber", "get_prices"):
                        errors[CONF_PRICE_INTEGRATION_TYPE] = "tibber_unavailable"
                elif not price_sensor:
                    errors[CONF_PRICE_SENSOR] = "sensor_not_found"
                else:
                    error = _validate_price_sensor(
                        self.hass, price_sensor, integration_type
                    )
                    if error:
                        errors[CONF_PRICE_SENSOR] = error

                errors.update(
                    _validate_export_price_input(self.hass, user_input, integration_type)
                )
                forecast_sensor = _resolve_forecast_sensor(
                    self.hass, existing_config, user_input, errors
                )

                if not errors:
                    self.config_data["enable_predictive_charging"] = True
                    self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_DYNAMIC_PRICING
                    self.config_data[CONF_PRICE_INTEGRATION_TYPE] = integration_type
                    self.config_data[CONF_PRICE_SENSOR] = price_sensor
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    self.config_data["charging_time_slot"] = None
                    # Cleared entity/select fields arrive absent, so read them
                    # straight from the submission rather than falling back to
                    # the stored value - that is what makes the clear button work.
                    self.config_data[CONF_EXPORT_PRICE_SENSOR] = user_input.get(
                        CONF_EXPORT_PRICE_SENSOR
                    )
                    self.config_data[CONF_EXPORT_PRICE_INTEGRATION_TYPE] = user_input.get(
                        CONF_EXPORT_PRICE_INTEGRATION_TYPE
                    )
                    self.config_data[CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED] = user_input.get(
                        CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                        existing_config.get(
                            CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                            DEFAULT_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                        ),
                    )
                    return await self._finish_setup()
            except Exception as e:
                _LOGGER.error("Error validating dynamic pricing config: %s", e)
                errors["base"] = "unknown"

        form_input = user_input or {}
        default_integration = form_input.get(
            CONF_PRICE_INTEGRATION_TYPE,
            existing_config.get(CONF_PRICE_INTEGRATION_TYPE, PRICE_INTEGRATION_NORDPOOL),
        )
        default_sensor = form_input.get(
            CONF_PRICE_SENSOR, existing_config.get(CONF_PRICE_SENSOR, "")
        )
        default_export_sensor = (
            form_input.get(CONF_EXPORT_PRICE_SENSOR)
            if user_input is not None
            else existing_config.get(CONF_EXPORT_PRICE_SENSOR)
        )
        default_export_type = (
            form_input.get(CONF_EXPORT_PRICE_INTEGRATION_TYPE)
            if user_input is not None
            else existing_config.get(CONF_EXPORT_PRICE_INTEGRATION_TYPE)
        )

        schema_dict: dict = {
            vol.Required(CONF_PRICE_INTEGRATION_TYPE, default=default_integration):
                _price_integration_type_selector(),
            # Optional: not used by Tibber, which polls the tibber.get_prices service.
            vol.Optional(CONF_PRICE_SENSOR, default=default_sensor if default_sensor else vol.UNDEFINED):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
        }
        # Clearable: suggested_value pre-fills without voluptuous restoring the
        # old value when the field is emptied.
        schema_dict[vol.Optional(
            CONF_EXPORT_PRICE_SENSOR,
            description={"suggested_value": default_export_sensor} if default_export_sensor else {},
        )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        schema_dict[vol.Optional(
            CONF_EXPORT_PRICE_INTEGRATION_TYPE,
            description={"suggested_value": default_export_type} if default_export_type else {},
        )] = _price_integration_type_selector(_price_integration_export_options())
        # Always shown: the provider is picked in this same form, so hiding it
        # until Zonneplan is saved meant a second visit. The engine ignores it
        # unless the export curve is Zonneplan.
        schema_dict[vol.Optional(
            CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
            default=form_input.get(
                CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                existing_config.get(
                    CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                    DEFAULT_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                ),
            ),
        )] = BooleanSelector()
        if not _has_global_forecast_sensor(existing_config):
            default_forecast = (
                form_input.get("solar_forecast_sensor")
                if user_input is not None
                else existing_config.get("solar_forecast_sensor", "")
            )
            schema_dict[vol.Optional(
                "solar_forecast_sensor",
                description={"suggested_value": default_forecast} if default_forecast else {},
            )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        return self.async_show_form(
            step_id="dynamic_pricing_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_realtime_price_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 11d: Configure real-time price charging mode.

        Only the price sensors live here; the max-price threshold and the
        price discharge control are dashboard entities writing the same keys.
        """
        errors: dict[str, str] = {}
        existing_config = self.config_data

        if user_input is not None:
            try:
                price_sensor = user_input[CONF_PRICE_SENSOR]
                if self.hass.states.get(price_sensor) is None:
                    errors[CONF_PRICE_SENSOR] = "sensor_not_found"
                forecast_sensor = _resolve_forecast_sensor(
                    self.hass, existing_config, user_input, errors
                )

                if not errors:
                    self.config_data["enable_predictive_charging"] = True
                    self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_REALTIME_PRICE
                    self.config_data[CONF_PRICE_SENSOR] = price_sensor
                    self.config_data[CONF_AVERAGE_PRICE_SENSOR] = user_input.get(CONF_AVERAGE_PRICE_SENSOR) or None
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    self.config_data["charging_time_slot"] = None
                    return await self._finish_setup()
            except Exception as e:
                _LOGGER.error("Error validating real-time price config: %s", e)
                errors["base"] = "unknown"

        default_sensor = existing_config.get(CONF_PRICE_SENSOR, "")
        default_avg_sensor = existing_config.get(CONF_AVERAGE_PRICE_SENSOR, "")

        schema_dict: dict = {
            vol.Required(CONF_PRICE_SENSOR, default=default_sensor if default_sensor else vol.UNDEFINED):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_AVERAGE_PRICE_SENSOR,
                description={"suggested_value": default_avg_sensor} if default_avg_sensor else {}
            ):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
        }
        if not _has_global_forecast_sensor(existing_config):
            default_forecast = existing_config.get("solar_forecast_sensor", "")
            schema_dict[vol.Optional(
                "solar_forecast_sensor",
                description={"suggested_value": default_forecast} if default_forecast else {},
            )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        return self.async_show_form(
            step_id="realtime_price_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def _finish_setup(self) -> FlowResult:
        """Finish setup: dashboard-configurable features start present but disabled.

        Weekly full charge, solar charge delay, temperature charge limit,
        capacity protection, hourly balance and the PD controller are no longer
        part of the setup wizard. Their enable keys are written disabled so the
        switch/slider entities are created; everything is tuned live from the
        dashboard (the panel hides a disabled feature's parameters).
        """
        from .const import CONF_ENABLE_HOURLY_BALANCE

        self.config_data.setdefault(CONF_ENABLE_WEEKLY_FULL_CHARGE, False)
        self.config_data.setdefault(CONF_WEEKLY_FULL_CHARGE_DAY, "sun")
        self.config_data.setdefault(CONF_ENABLE_CHARGE_DELAY, False)
        self.config_data.setdefault(CONF_DELAY_SOC_SETPOINT_ENABLED, False)
        self.config_data.setdefault(CONF_ENABLE_TEMP_CHARGE_LIMIT, False)
        self.config_data.setdefault(CONF_CAPACITY_PROTECTION_ENABLED, False)
        self.config_data.setdefault(CONF_CAPACITY_PROTECTION_EXCLUDED_DEVICES, False)
        self.config_data.setdefault(CONF_ENABLE_HOURLY_BALANCE, False)
        self.config_data.setdefault(CONF_ENABLE_SYSTEM_POWER_LIMITS, False)
        self.config_data.setdefault(CONF_THREE_PHASE_ENABLED, DEFAULT_THREE_PHASE_ENABLED)
        self.config_data[CONF_ENABLE_BALANCE_MONITOR] = True
        return self.async_create_entry(
            title="Omnibattery",
            data=normalize_solar_forecast_config(self.config_data),
        )

    def _migrate_battery_registry_ids(
        self,
        entry: ConfigEntry,
        old_host: str,
        old_port: int,
        new_host: str,
        new_port: int,
        old_slave: int = DEFAULT_SLAVE_ID,
        new_slave: int = DEFAULT_SLAVE_ID,
    ) -> None:
        """Rename entity unique_ids and device identifiers when a battery's host/port/slave changes.

        Preserves long-term statistics and history by keeping the same entity_id.
        Battery-level keys follow `coordinator.device_key` (`{host}_{port}` for
        slave 1, `{host}_{port}_{slave}` otherwise); the device identifier is
        `(DOMAIN, device_key)`. Both are rewritten in place.
        """
        old_device_id = device_key_for(old_host, old_port, old_slave)
        new_device_id = device_key_for(new_host, new_port, new_slave)
        old_prefix = f"{old_device_id}_"
        new_prefix = f"{new_device_id}_"

        ent_reg = er.async_get(self.hass)
        for ent in list(ent_reg.entities.values()):
            if (
                ent.config_entry_id == entry.entry_id
                and ent.unique_id.startswith(old_prefix)
            ):
                new_uid = new_prefix + ent.unique_id[len(old_prefix):]
                ent_reg.async_update_entity(ent.entity_id, new_unique_id=new_uid)

        dev_reg = dr.async_get(self.hass)
        old_dev = get_entry_device(dev_reg, (DOMAIN, old_device_id), entry.entry_id)
        if old_dev is not None:
            new_identifiers = set(old_dev.identifiers)
            new_identifiers.discard((DOMAIN, old_device_id))
            new_identifiers.add((DOMAIN, new_device_id))
            dev_reg.async_update_device(
                old_dev.id, new_identifiers=new_identifiers
            )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reconfiguration — update battery connection settings (IP/port)."""
        self.battery_index = 0
        self._reconfigure_batteries: list[dict] = []
        return await self.async_step_reconfigure_battery()

    async def async_step_reconfigure_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Update connection settings for each battery during reconfiguration.

        The dispatcher: runs the reconfigure step of the stored battery's brand
        (an unregistered brand, or none, is served by ``DEFAULT_BRAND``). A brand
        whose reconfigure step id is this method's own (Marstek) has its handler
        run directly, since no method is generated over this one.
        """
        entry = self._get_reconfigure_entry()
        current_batteries = entry.data.get("batteries", [])
        current = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )

        brand = _setup_brand(current.get("brand", DEFAULT_BRAND))
        setup = registry.setup_for(brand)
        if setup is None or not setup.reconfigure_entry_step:
            brand = DEFAULT_BRAND
            setup = registry.setup_for(brand)
        if user_input is None:
            work_dict(self, brand, reset=True)
        step = setup.reconfigure_entry_step
        if step == _DISPATCHER_STEP:
            return await _run_driver_step(self, brand, setup.steps[step], user_input)
        return await getattr(self, f"async_step_{step}")(user_input)


    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OptionsFlowHandler(config_entry)


    async def async_step_dhcp(self, discovery_info: Any) -> FlowResult:
        """Follow a tracked battery when its router hands it a different address.

        Reached through the ``registered_devices`` matcher in the manifest, which
        fires for any device whose MAC sits in the device registry. All this step
        does is turn the lease into a verdict and act on it; the decision guards
        live in ``infra.mac_tracking`` so they can be tested without a running
        Home Assistant, and the one guard that needs the network — the candidate
        has to answer as the battery — runs here. It always aborts; there is no
        user-facing flow.
        """
        reason = await self._async_apply_dhcp_lease(
            getattr(discovery_info, "macaddress", None),
            getattr(discovery_info, "ip", "") or "",
        )
        return self.async_abort(reason=reason)

    def _dhcp_coordinator(self, entry: ConfigEntry, index: int):
        """Return the live coordinator of battery *index*, or None if there is none."""
        data = (self.hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
        coordinators = data.get("coordinators") or []
        if index >= len(coordinators):
            return None
        return coordinators[index]

    def _dhcp_reachability_probe(self, entry: ConfigEntry):
        """Return a callable answering whether battery *index* still responds.

        A device can hold two addresses at once, so a lease for a new address is
        not on its own a reason to abandon one that still works.
        """

        def _is_reachable(index: int) -> bool:
            coordinator = self._dhcp_coordinator(entry, index)
            return bool(getattr(coordinator, "is_available", False))

        return _is_reachable

    async def _async_probe_battery_endpoint(self, battery: dict, host: str) -> bool:
        """Re-probe ``host`` with the driver of this battery's brand.

        A brand the registry does not know is probed like the default one, as it
        always was.
        """
        brand = battery.get("brand", DEFAULT_BRAND)
        setup = registry.setup_for(brand) or registry.setup_for(DEFAULT_BRAND)
        result = await setup.reprobe(self.hass, battery, host)
        if not result:
            _LOGGER.error("Failed to connect to %s:%s (brand=%s)", host, battery.get(CONF_PORT), brand)
        return result

    async def _async_probe_candidate(
        self, entry: ConfigEntry, index: int, battery: dict, host: str
    ) -> bool:
        """Require the candidate address to answer before moving a battery onto it.

        A matching MAC only proves that *something* at ``host`` carries the
        battery's hardware address. A Wi-Fi-to-LAN bridge or a powerline adapter
        answers for everything behind it, so one MAC can cover both the battery
        and the bridge's own management address (#289): the lease alone cannot
        say which one arrived. Talking to the candidate is what tells them apart.

        It also makes the move symmetric with ``STILL_REACHABLE``. That guard
        refuses to leave an endpoint that answers; without this one, the same
        code would happily arrive on an endpoint that never did.

        The battery's own coordinator is closed first: a v3 Marstek accepts a
        single Modbus TCP connection at a time, and ``is_available`` being False
        does not prove its socket was released. On failure the previous
        connection is restored, so a lease that leads nowhere leaves the battery
        exactly as it was. On success it is left closed on purpose, because the
        entry is reloaded immediately after and rebuilds it on the new address.
        """
        coordinator = self._dhcp_coordinator(entry, index)
        if coordinator is None:
            return await self._async_probe_battery_endpoint(battery, host)

        async with coordinator.lock:
            await coordinator.driver.close()
            # Same settle margins as the options-flow probe: the device needs a
            # moment to release the slot before it accepts the next connection.
            await asyncio.sleep(0.5)
            answered = await self._async_probe_battery_endpoint(battery, host)
            if not answered:
                await asyncio.sleep(0.3)
                await coordinator.driver.connect()
            return answered

    async def _async_apply_dhcp_lease(self, mac: Any, host: str) -> str:
        """Move a battery onto ``host`` when exactly one may safely be moved.

        Returns the abort reason, which doubles as the log line: every refusal
        names the single guard that fired.
        """
        refusal = "no_tracked_battery"
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            batteries = [dict(b) for b in entry.data.get("batteries", [])]
            verdict = evaluate_lease(
                batteries, mac, host, self._dhcp_reachability_probe(entry)
            )
            if not verdict.should_update:
                if verdict.reason not in ("no_match", "invalid_mac"):
                    _LOGGER.debug(
                        "DHCP lease %s -> %s not applied to %s: %s",
                        mac, host, entry.title, verdict.reason,
                    )
                continue

            battery = batteries[verdict.index]

            # Last guard, and the only one that needs the network: the address
            # has to answer as this battery before anything is written.
            if not await self._async_probe_candidate(
                entry, verdict.index, battery, host
            ):
                _LOGGER.debug(
                    "DHCP lease %s -> %s not applied to %s: %s",
                    mac, host, entry.title, CANDIDATE_SILENT,
                )
                refusal = CANDIDATE_SILENT
                continue

            old_host = battery.get(CONF_HOST) or ""
            port = battery.get(CONF_PORT)
            slave = battery.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
            battery[CONF_HOST] = host

            # Rewrites the device key and the entity unique_ids while keeping the
            # entity_ids, so history and long-term statistics survive the move.
            self._migrate_battery_registry_ids(
                entry, old_host, port, host, port, slave, slave
            )
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, "batteries": batteries}
            )
            _LOGGER.info(
                "Battery %s moved from %s to %s (MAC %s); connection updated",
                battery.get(CONF_NAME, "?"), old_host, host, mac,
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            return "ip_updated"
        return refusal


@_preserve_values_in_error_forms
class OptionsFlowHandler(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        # NOTE: Do NOT set self.config_entry - it's a read-only property from OptionsFlow base class
        # The config_entry is automatically available as self.config_entry
        self.config_data = {}
        self.battery_configs = []
        self.battery_index = 0
        self.time_slots = []
        self.excluded_devices = []
        self._current_battery_data = {}  # Stores connection data between battery steps
        self._pending_slot_step_a: dict | None = None  # Buffer between slot step A and step B
        _LOGGER.info("OptionsFlowHandler initialized successfully for entry: %s", config_entry.entry_id)


    async def _save_and_finish(self) -> FlowResult:
        """Merge config_data into existing entry data, save, and reload."""
        new_data = dict(self.config_entry.data)
        new_data.update(self.config_data)
        if not new_data.get(CONF_OFFGRID_POWER_SENSOR):
            new_data.pop(CONF_OFFGRID_POWER_SENSOR, None)
            new_data.pop(CONF_OFFGRID_METER_INVERTED, None)
            new_data.pop(CONF_OFFGRID_MODE_ENABLED, None)
        new_data = normalize_solar_forecast_config(new_data)
        new_data[CONF_ENABLE_BALANCE_MONITOR] = True
        entry_id = self.config_entry.entry_id
        mark_reload_pending(self.hass, entry_id)
        try:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
        except BaseException:
            clear_reload_pending(self.hass, entry_id)
            raise

        async def _reload() -> None:
            try:
                await self.hass.config_entries.async_reload(entry_id)
            finally:
                clear_reload_pending(self.hass, entry_id)

        # The reload runs as its own task, not inside the flow, so a client that
        # closes the options dialog early cannot cancel the setup halfway.
        self.hass.async_create_task(_reload(), f"{DOMAIN} options reload")
        return self.async_create_entry(title="", data={})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show menu to select which section to configure."""
        # Weekly full charge, charge delay, temperature charge limit, capacity
        # protection, hourly balance and the PD controller are configured live
        # from the dashboard entities, not here.
        menu_options = [
            "sensors",
            "batteries",
            "time_slots",
            "excluded_devices",
            "predictive_charging",
        ]
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_remove_battery(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Remove one specific battery (the count flow can only drop the last one)."""
        batteries = list(self.config_entry.data.get("batteries", []))
        if user_input is not None:
            removed = batteries.pop(int(user_input["battery"]))
            self.config_data["batteries"] = batteries
            if self.config_entry.data.get(CONF_PRIMARY_BATTERY) == removed.get(CONF_NAME):
                self.config_data[CONF_PRIMARY_BATTERY] = ""
            result = await self._save_and_finish()
            # Drop the now-orphaned device (and its entities); same key as coordinator.device_key.
            slave = removed.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
            key = f"{removed.get(CONF_HOST)}_{removed.get(CONF_PORT)}"
            if slave != 1:
                key = f"{key}_{slave}"
            dev_reg = dr.async_get(self.hass)
            if device := get_entry_device(dev_reg, (DOMAIN, key), self.config_entry.entry_id):
                dev_reg.async_remove_device(device.id)
            return result

        return self.async_show_form(
            step_id="remove_battery",
            data_schema=vol.Schema({
                vol.Required("battery"): SelectSelector(SelectSelectorConfig(
                    options=[
                        {"value": str(i), "label": b.get(CONF_NAME, f"Battery {i + 1}")}
                        for i, b in enumerate(batteries)
                    ],
                )),
            }),
        )

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure consumption sensor and optional solar forecast sensor."""
        errors = {}
        current_three_phase_enabled = self.config_entry.data.get(
            CONF_THREE_PHASE_ENABLED,
            DEFAULT_THREE_PHASE_ENABLED,
        )
        try:
            if user_input is not None:
                # Validate both explicit forecast horizons.
                forecast_sensor = user_input.get(CONF_SOLAR_FORECAST_SENSOR)
                remaining_sensor = user_input.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
                solar_sensor = user_input.get(CONF_SOLAR_PRODUCTION_SENSOR)
                forecast_candidates = (
                    ((CONF_SOLAR_FORECAST_REMAINING_SENSOR, remaining_sensor),)
                    if remaining_sensor
                    else ((CONF_SOLAR_FORECAST_SENSOR, forecast_sensor),)
                )
                for key, sensor in forecast_candidates:
                    if sensor:
                        forecast_state = self.hass.states.get(sensor)
                        if forecast_state is None:
                            errors[key] = "sensor_not_found"
                        elif forecast_state.attributes.get("unit_of_measurement", "") not in ["kWh", "Wh"]:
                            errors[key] = "invalid_unit"

                errors.update(_validate_solar_production_sensor(self.hass, user_input))

                errors.update(_validate_offgrid_power_sensor(self.hass, user_input))

                if not errors:
                    # Runs after validation so a preserved dead reference cannot
                    # block the save with `sensor_not_found` (#419).
                    user_input = _restore_unrenderable_sensors(
                        self.hass, user_input, self.config_entry.data
                    )
                    forecast_sensor = user_input.get(CONF_SOLAR_FORECAST_SENSOR)
                    remaining_sensor = user_input.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
                    solar_sensor = user_input.get(CONF_SOLAR_PRODUCTION_SENSOR)
                    self.config_data["consumption_sensor"] = user_input["consumption_sensor"]
                    self.config_data[CONF_OFFGRID_POWER_SENSOR] = (
                        user_input.get(CONF_OFFGRID_POWER_SENSOR) or None
                    )
                    self.config_data[CONF_OFFGRID_METER_INVERTED] = user_input.get(
                        CONF_OFFGRID_METER_INVERTED, False
                    )
                    if remaining_sensor:
                        self.config_data.pop(CONF_SOLAR_FORECAST_SENSOR, None)
                        self.config_data[CONF_SOLAR_FORECAST_REMAINING_SENSOR] = remaining_sensor
                    else:
                        self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                        # This explicit marker removes a prior remaining sensor
                        # during the merge in `_save_and_finish`.
                        self.config_data[CONF_SOLAR_FORECAST_REMAINING_SENSOR] = None
                    self.config_data[CONF_SOLAR_PRODUCTION_SENSOR] = solar_sensor
                    self.config_data[CONF_METER_INVERTED] = user_input.get(CONF_METER_INVERTED, False)
                    self.config_data["max_contracted_power"] = user_input["max_contracted_power"]
                    enabled = bool(
                        user_input.get(
                            CONF_THREE_PHASE_ENABLED,
                            current_three_phase_enabled,
                        )
                    )
                    self.config_data[CONF_THREE_PHASE_ENABLED] = enabled
                    if enabled:
                        return await self.async_step_three_phase()
                    return await self._save_and_finish()

            # Load current configuration with defensive defaults
            current_sensor = self.config_entry.data.get("consumption_sensor", "")
            current_forecast = self.config_entry.data.get(CONF_SOLAR_FORECAST_SENSOR, "")
            current_remaining_forecast = self.config_entry.data.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR, "")
            current_solar = self.config_entry.data.get(CONF_SOLAR_PRODUCTION_SENSOR, "")
            current_offgrid = self.config_entry.data.get(CONF_OFFGRID_POWER_SENSOR, "")
            current_offgrid_inverted = self.config_entry.data.get(
                CONF_OFFGRID_METER_INVERTED, False
            )
            current_inverted = self.config_entry.data.get(CONF_METER_INVERTED, False)
            current_max_power = self.config_entry.data.get("max_contracted_power", 7000)
        except Exception as e:
            _LOGGER.error("Error in options flow sensors: %s", e, exc_info=True)
            return self.async_abort(reason="unknown_error")

        return self.async_show_form(
            step_id="sensors",
            data_schema=vol.Schema(
                {
                    vol.Required("consumption_sensor", default=current_sensor):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(CONF_METER_INVERTED, default=current_inverted):
                        BooleanSelector(),
                    vol.Required("max_contracted_power", default=current_max_power):
                        NumberSelector(
                            NumberSelectorConfig(
                                min=500, max=20000, step=100, mode=NumberSelectorMode.BOX
                            )
                        ),
                    vol.Optional(CONF_SOLAR_FORECAST_SENSOR, description={"suggested_value": current_forecast} if current_forecast else {}):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(CONF_SOLAR_FORECAST_REMAINING_SENSOR, description={"suggested_value": current_remaining_forecast} if current_remaining_forecast else {}):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(CONF_SOLAR_PRODUCTION_SENSOR, description={"suggested_value": current_solar} if current_solar else {}):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(CONF_OFFGRID_POWER_SENSOR, description={"suggested_value": current_offgrid} if current_offgrid else {}):
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Optional(
                        CONF_OFFGRID_METER_INVERTED,
                        default=current_offgrid_inverted,
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_THREE_PHASE_ENABLED,
                        default=current_three_phase_enabled,
                    ): BooleanSelector(),
                }
            ),
            errors=errors if errors else None,
        )

    async def async_step_three_phase(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure or edit the optional per-phase safety protection."""
        errors: dict[str, str] = {}
        current = self.config_entry.data
        defaults = {
            key: current.get(key)
            for key in (
                CONF_PHASE_1_CURRENT_SENSOR,
                CONF_PHASE_2_CURRENT_SENSOR,
                CONF_PHASE_3_CURRENT_SENSOR,
                CONF_PHASE_1_FUSE_SIZE,
                CONF_PHASE_2_FUSE_SIZE,
                CONF_PHASE_3_FUSE_SIZE,
            )
        }
        if user_input is not None:
            errors = _validate_phase_protection(self.hass, user_input)
            if not errors:
                self.config_data.update(
                    {
                        key: user_input.get(key)
                        for key in (
                            CONF_PHASE_1_CURRENT_SENSOR,
                            CONF_PHASE_2_CURRENT_SENSOR,
                            CONF_PHASE_3_CURRENT_SENSOR,
                            CONF_PHASE_1_FUSE_SIZE,
                            CONF_PHASE_2_FUSE_SIZE,
                            CONF_PHASE_3_FUSE_SIZE,
                        )
                    }
                )
                self.config_data[CONF_THREE_PHASE_ENABLED] = True
                batteries = [dict(b) for b in current.get("batteries", [])]
                if batteries:
                    # Protection configuration is deliberately a two-step
                    # operation: every physical assignment is confirmed after
                    # the current sensors, even when an older assignment is
                    # already saved. This makes the wiring explicit whenever
                    # the protection settings are edited, while keeping the
                    # saved phase as the form's initial selection.
                    self._phase_assignment_batteries = batteries
                    self._phase_assignment_index = 0
                    return await self.async_step_phase_assignments()
                return await self._save_and_finish()

        return self.async_show_form(
            step_id="three_phase",
            data_schema=_phase_protection_schema(defaults),
            errors=errors or None,
        )

    async def async_step_phase_assignments(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect every battery's physical L1/L2/L3 assignment atomically."""
        batteries = getattr(self, "_phase_assignment_batteries", [])
        index = getattr(self, "_phase_assignment_index", 0)
        if index >= len(batteries):
            self.config_data["batteries"] = batteries
            return await self._save_and_finish()

        errors: dict[str, str] = {}
        if user_input is not None:
            phase = user_input.get(CONF_BATTERY_PHASE)
            if not _phase_assignment_is_valid(phase):
                errors[CONF_BATTERY_PHASE] = "battery_phase_required"
            else:
                batteries[index][CONF_BATTERY_PHASE] = normalize_battery_phase(phase)
                self._phase_assignment_index = index + 1
                return await self.async_step_phase_assignments()

        current = batteries[index]
        phase_field, phase_selector = _battery_phase_schema(
            current.get(CONF_BATTERY_PHASE)
            if _phase_assignment_is_valid(current.get(CONF_BATTERY_PHASE))
            else PHASE_UNASSIGNED
        )
        return self.async_show_form(
            step_id="phase_assignments",
            data_schema=vol.Schema({phase_field: phase_selector}),
            errors=errors or None,
            description_placeholders={
                "battery_num": str(index + 1),
                "battery_name": str(current.get(CONF_NAME, f"Battery {index + 1}")),
            },
        )

    async def async_step_batteries(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Batteries submenu: add/modify, or remove a specific one (needs 2+)."""
        if len(self.config_entry.data.get("batteries", [])) < 2:
            return await self.async_step_battery_count()
        return self.async_show_menu(step_id="batteries", menu_options=["battery_count", "remove_battery"])

    async def async_step_battery_count(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure number of batteries."""
        try:
            if user_input is not None:
                self.config_data["num_batteries"] = int(user_input["num_batteries"])
                return await self.async_step_battery_brand()

            # Load current number of batteries with defensive handling
            batteries = self.config_entry.data.get("batteries", [])
            current_batteries = len(batteries) if batteries else 1
        except Exception as e:
            _LOGGER.error("Error in options flow batteries step: %s", e, exc_info=True)
            return self.async_abort(reason="unknown_error")

        return self.async_show_form(
            step_id="battery_count",
            data_schema=_battery_count_schema(current_batteries),
        )

    async def async_step_battery_brand(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select battery brand for the current battery slot."""
        battery_num = self.battery_index + 1
        current_batteries = self.config_entry.data.get("batteries", [])
        current_battery = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )
        current_brand = current_battery.get("brand", DEFAULT_BRAND)

        if user_input is not None:
            brand = user_input["brand"]
            # No connection step asks for MAC tracking, so the stored opt-in and
            # MAC ride along to battery_limits, the form that shows them.
            self._current_battery_data = {
                "brand": brand,
                CONF_TRACK_MAC: current_battery.get(CONF_TRACK_MAC, False),
                CONF_MAC: current_battery.get(CONF_MAC, ""),
            }
            return await _dispatch_brand_entry(self, brand)

        return self.async_show_form(
            step_id="battery_brand",
            data_schema=vol.Schema(
                {
                    vol.Required("brand", default=current_brand):
                        SelectSelector(SelectSelectorConfig(
                            options=_brand_selector_options(),
                            mode=SelectSelectorMode.DROPDOWN,
                        )),
                }
            ),
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_limits(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure power and SOC limits for the current battery."""
        errors: dict[str, str] = {}
        try:
            battery_num = self.battery_index + 1
            brand = self._current_battery_data.get("brand", DEFAULT_BRAND)
            limits = _limits_setup(brand)
            ctx = _setup_context(self, brand)
            max_charge_power, max_discharge_power = limits.power_ceilings(self._current_battery_data)
            (
                soc_min_lo,
                soc_min_hi,
                soc_min_default,
                soc_max_lo,
                soc_max_hi,
                soc_max_default,
            ) = _soc_selector_limits(brand, self._current_battery_data.get(CONF_BATTERY_VERSION, DEFAULT_VERSION))
            current_batteries = self.config_entry.data.get("batteries", [])

            if user_input is not None:
                phase = user_input.get(CONF_BATTERY_PHASE, "")
                if self.config_entry.data.get(
                    CONF_THREE_PHASE_ENABLED,
                    self.config_data.get(CONF_THREE_PHASE_ENABLED, DEFAULT_THREE_PHASE_ENABLED),
                ) and not _phase_assignment_is_valid(phase):
                    errors[CONF_BATTERY_PHASE] = "battery_phase_required"
                    user_input = None
                if user_input is not None and (
                    mac_error := _validate_mac_tracking(user_input)
                ):
                    errors[CONF_MAC] = mac_error
                    user_input = None

            if user_input is not None:
                # Start from existing battery config to preserve persisted keys not in this form.
                if self.battery_index < len(current_batteries):
                    merged = dict(current_batteries[self.battery_index])
                    merged.update(self._current_battery_data)
                else:
                    merged = dict(self._current_battery_data)
                # The form may not offer some of them (the brand's hook sets them).
                for key in ("max_charge_power", "max_discharge_power", "max_soc", "min_soc"):
                    if key in user_input:
                        merged[key] = int(user_input[key])
                        # The slider's value, when it has one, wins at setup.
                        if f"user_{key}" in merged:
                            merged[f"user_{key}"] = merged[key]
                if self.config_entry.data.get(
                    CONF_THREE_PHASE_ENABLED,
                    self.config_data.get(CONF_THREE_PHASE_ENABLED, DEFAULT_THREE_PHASE_ENABLED),
                ):
                    merged[CONF_BATTERY_PHASE] = normalize_battery_phase(phase)
                limits.finalize_limits(merged, user_input, self._current_battery_data, ctx)
                # Hysteresis is mandatory; floor the percent against SOC drift.
                merged["enable_charge_hysteresis"] = True
                merged["charge_hysteresis_percent"] = max(
                    MIN_CHARGE_HYSTERESIS_PERCENT,
                    int(user_input.get(
                        "charge_hysteresis_percent",
                        merged.get("charge_hysteresis_percent", DEFAULT_CHARGE_HYSTERESIS_PERCENT),
                    )),
                )
                _apply_mac_tracking(user_input, merged)
                self.battery_configs.append(merged)
                self.battery_index += 1

                num_batteries = self.config_data.get("num_batteries", 1)
                if self.battery_index >= num_batteries:
                    self.config_data["batteries"] = self.battery_configs
                    return await self._save_and_finish()
                return await self.async_step_battery_brand()

            if self.battery_index < len(current_batteries):
                current_battery = dict(current_batteries[self.battery_index])
                current_battery.update(self._current_battery_data)
                live = _live_limits(ctx, brand)
                defaults = {
                    "max_charge_power": min(_configured_power(current_battery, live, "max_charge_power", max_charge_power), max_charge_power),
                    "max_discharge_power": min(_configured_power(current_battery, live, "max_discharge_power", max_discharge_power), max_discharge_power),
                    "max_soc": live.get("max_soc", current_battery.get("max_soc", soc_max_default)),
                    "min_soc": live.get("min_soc", current_battery.get("min_soc", soc_min_default)),
                    "charge_hysteresis_percent": max(
                        MIN_CHARGE_HYSTERESIS_PERCENT,
                        int(current_battery.get("charge_hysteresis_percent", DEFAULT_CHARGE_HYSTERESIS_PERCENT)),
                    ),
                    CONF_BATTERY_PHASE: normalize_battery_phase(
                        current_battery.get(CONF_BATTERY_PHASE, PHASE_UNASSIGNED)
                    ),
                }
            else:
                defaults = {
                    "max_charge_power": max(
                        100,
                        min(
                            max_charge_power,
                            int(self._current_battery_data.get("max_charge_power", max_charge_power)),
                        ),
                    ),
                    "max_discharge_power": max(
                        100,
                        min(
                            max_discharge_power,
                            int(self._current_battery_data.get("max_discharge_power", max_discharge_power)),
                        ),
                    ),
                    "max_soc": soc_max_default,
                    "min_soc": self._current_battery_data.get("min_soc", soc_min_default),
                    "charge_hysteresis_percent": DEFAULT_CHARGE_HYSTERESIS_PERCENT,
                    CONF_BATTERY_PHASE: PHASE_UNASSIGNED,
                }
        except Exception as e:
            _LOGGER.error("Error in options flow battery_limits step: %s", e, exc_info=True)
            return self.async_abort(reason="unknown_error")

        _schema: dict = {}
        _schema[vol.Required("max_charge_power", default=defaults["max_charge_power"])] = NumberSelector(
            NumberSelectorConfig(min=min(100, defaults["max_charge_power"]), max=max_charge_power, step=50, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)
        )
        _schema[vol.Required("max_discharge_power", default=defaults["max_discharge_power"])] = NumberSelector(
            NumberSelectorConfig(min=min(100, defaults["max_discharge_power"]), max=max_discharge_power, step=50, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)
        )
        _schema.update({
            vol.Required("max_soc", default=max(soc_max_lo, min(soc_max_hi, defaults["max_soc"]))):
                NumberSelector(NumberSelectorConfig(min=soc_max_lo, max=soc_max_hi, step=1, mode=NumberSelectorMode.SLIDER)),
            vol.Required("min_soc", default=max(soc_min_lo, min(soc_min_hi, defaults["min_soc"]))):
                NumberSelector(NumberSelectorConfig(min=soc_min_lo, max=soc_min_hi, step=1, mode=NumberSelectorMode.SLIDER)),
            vol.Required("charge_hysteresis_percent", default=defaults["charge_hysteresis_percent"]):
                NumberSelector(NumberSelectorConfig(min=MIN_CHARGE_HYSTERESIS_PERCENT, max=MAX_CHARGE_HYSTERESIS_PERCENT, step=1, mode=NumberSelectorMode.SLIDER)),
        })
        _schema = limits.limits_schema(self._current_battery_data, _schema, ctx)
        if self.config_entry.data.get(
            CONF_THREE_PHASE_ENABLED,
            self.config_data.get(CONF_THREE_PHASE_ENABLED, DEFAULT_THREE_PHASE_ENABLED),
        ):
            phase_field, phase_selector = _battery_phase_schema(
                defaults.get(CONF_BATTERY_PHASE, PHASE_UNASSIGNED)
            )
            _schema[phase_field] = phase_selector
        if is_ip_based(self._current_battery_data):
            _schema.update(
                _mac_tracking_schema(_mac_defaults(self.hass, self._current_battery_data))
            )
        return self.async_show_form(
            step_id="battery_limits",
            data_schema=vol.Schema(_schema),
            errors=errors or None,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_time_slots(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask if user wants to configure time slots."""
        if user_input is not None:
            if user_input.get("configure_time_slots", False):
                # Reset time_slots list to start fresh
                self.time_slots = []
                return await self.async_step_add_time_slot()
            else:
                self.config_data["no_discharge_time_slots"] = []
                return await self._save_and_finish()

        # Check if time slots were previously configured
        existing_slots = self.config_entry.data.get("no_discharge_time_slots", [])
        has_existing_slots = len(existing_slots) > 0

        return self.async_show_form(
            step_id="time_slots",
            data_schema=vol.Schema(
                {
                    vol.Required("configure_time_slots", default=has_existing_slots): bool,
                }
            ),
        )

    async def async_step_add_time_slot(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step A: configure base attributes of a time slot."""
        errors: dict = {}
        batteries = self.config_entry.data.get("batteries", [])

        if user_input is not None:
            errors = _validate_slot_step_a(user_input)
            if not errors:
                if _slots_overlap(
                    {
                        "start_time": user_input["start_time"],
                        "end_time": user_input["end_time"],
                        "days": user_input["days"],
                        "battery_scope": user_input.get("battery_scope", SLOT_BATTERY_SCOPE_ALL),
                    },
                    self.time_slots,
                ):
                    errors["base"] = "overlapping_slots"
            if not errors:
                self._pending_slot_step_a = dict(user_input)
                if user_input.get("soc_override_enabled") or user_input.get("power_override_enabled"):
                    return await self.async_step_add_time_slot_details()
                return await self._finalize_time_slot(step_b=None)

        defaults = self._options_slot_defaults(len(self.time_slots))
        if user_input:
            defaults = {**defaults, **user_input}

        slot_num = len(self.time_slots) + 1
        return self.async_show_form(
            step_id="add_time_slot",
            data_schema=_build_slot_step_a_schema(batteries, defaults),
            errors=errors if errors else None,
            description_placeholders={"slot_num": str(slot_num)},
        )

    async def async_step_add_time_slot_details(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step B: optional SOC / power detail fields for the pending slot."""
        if self._pending_slot_step_a is None:
            return await self.async_step_add_time_slot()

        step_a = self._pending_slot_step_a
        batteries = self.config_entry.data.get("batteries", [])
        scope = step_a.get("battery_scope", SLOT_BATTERY_SCOPE_ALL)
        needs_soc = bool(step_a.get("soc_override_enabled"))
        needs_power = bool(step_a.get("power_override_enabled"))
        slot_num = len(self.time_slots) + 1

        if user_input is not None:
            return await self._finalize_time_slot(step_b=user_input)

        defaults = self._options_slot_defaults(len(self.time_slots))
        return self.async_show_form(
            step_id="add_time_slot_details",
            data_schema=_build_slot_step_b_schema(needs_soc, needs_power, scope, batteries, defaults),
            description_placeholders={
                "slot_num": str(slot_num),
                "battery_map": _battery_scope_name_map(batteries),
            },
        )

    async def _finalize_time_slot(self, step_b: dict | None) -> FlowResult:
        """Persist the pending slot and advance the flow."""
        if self._pending_slot_step_a is None:
            return await self.async_step_add_time_slot()
        slot = _finalize_slot(
            self._pending_slot_step_a,
            step_b,
            self._options_slot_defaults(len(self.time_slots)),
        )
        self.time_slots.append(slot)
        self._pending_slot_step_a = None
        if len(self.time_slots) < MAX_TIME_SLOTS:
            return await self.async_step_add_more_slots()
        self.config_data["no_discharge_time_slots"] = self.time_slots
        return await self._save_and_finish()

    def _options_slot_defaults(self, index: int) -> dict:
        """Return previously-saved slot at `index`, or empty dict if none."""
        existing = self.config_entry.data.get("no_discharge_time_slots", []) or []
        if 0 <= index < len(existing):
            return dict(existing[index])
        return {}

    async def async_step_add_more_slots(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask if user wants to add more time slots."""
        if user_input is not None:
            if user_input.get("add_more", False):
                return await self.async_step_add_time_slot()
            else:
                self.config_data["no_discharge_time_slots"] = self.time_slots
                return await self._save_and_finish()

        # Check if there are more existing slots to show
        existing_slots = self.config_entry.data.get("no_discharge_time_slots", [])
        has_more_existing = len(self.time_slots) < len(existing_slots)

        return self.async_show_form(
            step_id="add_more_slots",
            data_schema=vol.Schema(
                {
                    vol.Required("add_more", default=has_more_existing): bool,
                }
            ),
            description_placeholders={
                "current_slots": str(len(self.time_slots)),
                "max_slots": str(MAX_TIME_SLOTS),
            },
        )

    async def async_step_excluded_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask if user wants to configure excluded devices."""
        if user_input is not None:
            if user_input.get("configure_excluded_devices", False):
                # Reset excluded_devices list to start fresh
                self.excluded_devices = []
                return await self.async_step_add_excluded_device()
            else:
                self.config_data["excluded_devices"] = []
                return await self._save_and_finish()

        # Check if excluded devices were previously configured
        existing_devices = self.config_entry.data.get("excluded_devices", [])
        has_existing_devices = len(existing_devices) > 0

        return self.async_show_form(
            step_id="excluded_devices",
            data_schema=vol.Schema(
                {
                    vol.Required("configure_excluded_devices", default=has_existing_devices): bool,
                }
            ),
            description_placeholders={
                "description": "Configure devices with special management"
            },
        )

    async def async_step_add_excluded_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Add an excluded device configuration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            ev_no_telemetry = user_input.get("ev_charger_no_telemetry", False)
            dynamic_power_control = user_input.get("dynamic_power_control", False)
            power_sensor = user_input.get("power_sensor") or None
            activity_sensor = user_input.get("activity_sensor") or None
            remaining_demand_sensor = user_input.get("remaining_demand_sensor") or None
            remaining_demand_presence_sensor = (
                user_input.get("remaining_demand_presence_sensor") or None
            )
            if ev_no_telemetry:
                activity_sensor = activity_sensor or power_sensor
                if not activity_sensor:
                    errors["activity_sensor"] = "missing_activity_sensor"
            elif not power_sensor:
                errors["power_sensor"] = "missing_power_sensor"
            if dynamic_power_control and not activity_sensor:
                errors["activity_sensor"] = "missing_activity_sensor"

        if user_input is not None and not errors:
            # Save the excluded device
            excluded_device = {
                "power_sensor": power_sensor,
                "activity_sensor": activity_sensor,
                # Always written, None included: the merge below lays this dict
                # over the stored one, so an omitted key would resurrect the old
                # value and the field could never be cleared.
                "remaining_demand_sensor": remaining_demand_sensor,
                "remaining_demand_presence_sensor": remaining_demand_presence_sensor,
                "included_in_consumption": user_input.get("included_in_consumption", True),
                "allow_solar_surplus": user_input.get("allow_solar_surplus", False),
                "dynamic_power_control": dynamic_power_control,
                "cover_home_when_active": user_input.get("cover_home_when_active", False),
                "ev_charger_no_telemetry": ev_no_telemetry,
                "is_ev_charger": user_input.get("is_ev_charger", False),
            }
            # The Enabled switch and the Exclusion % slider write straight into
            # the stored record and have no field on this form. Lay the form
            # result over the stored device so those keys survive a re-save.
            # Only do that while the form still describes the same device:
            # replacing the device at this position must not inherit a disabled
            # state the user cannot see here, let alone change.
            stored = self.config_entry.data.get("excluded_devices", [])
            index = len(self.excluded_devices)
            previous = stored[index] if index < len(stored) else {}
            identity_field = (
                "power_sensor" if previous.get("power_sensor") else "activity_sensor"
            )
            previous_id = previous.get(identity_field)
            if previous_id and previous_id == excluded_device.get(identity_field):
                excluded_device = {**previous, **excluded_device}
            self.excluded_devices.append(excluded_device)

            # Check if user wants to add more devices (max 4)
            if len(self.excluded_devices) < 4:
                return await self.async_step_add_more_excluded_devices()
            else:
                self.config_data["excluded_devices"] = self.excluded_devices
                return await self._save_and_finish()

        # Load existing excluded devices if available and not yet added
        current_devices = self.config_entry.data.get("excluded_devices", [])
        device_num = len(self.excluded_devices)

        if device_num < len(current_devices):
            current_device = current_devices[device_num]
            default_sensor = current_device.get("power_sensor", "")
            default_included = current_device.get("included_in_consumption", True)
            default_allow_solar_surplus = current_device.get("allow_solar_surplus", False)
            default_dynamic_power_control = current_device.get("dynamic_power_control", False)
            default_cover_home = current_device.get("cover_home_when_active", False)
            default_ev_no_telemetry = current_device.get("ev_charger_no_telemetry", False)
            default_is_ev_charger = current_device.get("is_ev_charger", False)
            default_activity_sensor = current_device.get("activity_sensor", "")
            default_remaining_demand = current_device.get("remaining_demand_sensor") or ""
            default_remaining_presence = (
                current_device.get("remaining_demand_presence_sensor") or ""
            )
            if default_ev_no_telemetry and not default_activity_sensor:
                # Legacy no-telemetry entries stored their state entity in the
                # power_sensor field. Show it in the new field automatically.
                default_activity_sensor = default_sensor
        else:
            default_sensor = ""
            default_included = True
            default_allow_solar_surplus = False
            default_dynamic_power_control = False
            default_cover_home = False
            default_ev_no_telemetry = False
            default_is_ev_charger = False
            default_activity_sensor = ""
            default_remaining_demand = ""
            default_remaining_presence = ""

        device_num += 1
        power_sensor_field = (
            vol.Optional("power_sensor", default=default_sensor)
            if default_sensor
            else vol.Optional("power_sensor")
        )
        activity_sensor_field = (
            vol.Optional("activity_sensor", default=default_activity_sensor)
            if default_activity_sensor
            else vol.Optional("activity_sensor")
        )
        remaining_demand_field = (
            vol.Optional("remaining_demand_sensor", default=default_remaining_demand)
            if default_remaining_demand
            else vol.Optional("remaining_demand_sensor")
        )
        remaining_presence_field = (
            vol.Optional(
                "remaining_demand_presence_sensor", default=default_remaining_presence
            )
            if default_remaining_presence
            else vol.Optional("remaining_demand_presence_sensor")
        )
        return self.async_show_form(
            step_id="add_excluded_device",
            data_schema=vol.Schema(
                {
                    power_sensor_field:
                        EntitySelector(EntitySelectorConfig(domain="sensor")),
                    activity_sensor_field:
                        EntitySelector(EntitySelectorConfig(domain=["sensor", "binary_sensor"])),
                    vol.Required("included_in_consumption", default=default_included): bool,
                    vol.Optional("allow_solar_surplus", default=default_allow_solar_surplus): bool,
                    vol.Optional("dynamic_power_control", default=default_dynamic_power_control): bool,
                    vol.Optional("cover_home_when_active", default=default_cover_home): bool,
                    vol.Optional("ev_charger_no_telemetry", default=default_ev_no_telemetry): bool,
                    vol.Optional("is_ev_charger", default=default_is_ev_charger): bool,
                    remaining_demand_field:
                        EntitySelector(
                            # No device_class filter: _read_sensor_kwh_opt accepts any
                            # convertible energy unit, and a template helper
                            # often carries no device_class at all.
                            EntitySelectorConfig(domain="sensor")
                        ),
                    remaining_presence_field:
                        EntitySelector(
                            EntitySelectorConfig(
                                domain=["binary_sensor", "sensor", "device_tracker"]
                            )
                        ),
                }
            ),
            description_placeholders={
                "device_num": str(device_num),
                "description": f"Configure special device {device_num}"
            },
            errors=errors or None,
        )

    async def async_step_add_more_excluded_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask if user wants to add more excluded devices."""
        if user_input is not None:
            if user_input.get("add_more", False):
                return await self.async_step_add_excluded_device()
            else:
                self.config_data["excluded_devices"] = self.excluded_devices
                return await self._save_and_finish()

        # Check if there are more existing devices to show
        existing_devices = self.config_entry.data.get("excluded_devices", [])
        has_more_existing = len(self.excluded_devices) < len(existing_devices)

        return self.async_show_form(
            step_id="add_more_excluded_devices",
            data_schema=vol.Schema(
                {
                    vol.Required("add_more", default=has_more_existing): bool,
                }
            ),
            description_placeholders={
                "current_devices": str(len(self.excluded_devices)),
                "max_devices": "4",
            },
        )

    async def async_step_predictive_charging(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask if user wants to configure predictive grid charging in options flow."""
        if user_input is not None:
            if user_input.get("configure_predictive_charging", False):
                return await self.async_step_predictive_charging_mode()
            else:
                self.config_data["enable_predictive_charging"] = False
                self.config_data["charging_time_slot"] = None
                self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_TIME_SLOT
                return await self._save_and_finish()

        is_predictive_enabled = self.config_entry.data.get("enable_predictive_charging", False)

        return self.async_show_form(
            step_id="predictive_charging",
            data_schema=vol.Schema(
                {
                    vol.Required("configure_predictive_charging", default=is_predictive_enabled): bool,
                }
            ),
        )

    async def async_step_predictive_charging_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select predictive charging mode in options flow."""
        existing_mode = self.config_entry.data.get(CONF_PREDICTIVE_CHARGING_MODE, PREDICTIVE_MODE_TIME_SLOT)

        if user_input is not None:
            mode = user_input.get(CONF_PREDICTIVE_CHARGING_MODE, PREDICTIVE_MODE_TIME_SLOT)
            self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = mode
            if mode == PREDICTIVE_MODE_DYNAMIC_PRICING:
                return await self.async_step_dynamic_pricing_config()
            elif mode == PREDICTIVE_MODE_REALTIME_PRICE:
                return await self.async_step_realtime_price_config()
            else:
                return await self.async_step_predictive_charging_config()

        return self.async_show_form(
            step_id="predictive_charging_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PREDICTIVE_CHARGING_MODE, default=existing_mode):
                        SelectSelector(
                            SelectSelectorConfig(
                                options=[
                                    PREDICTIVE_MODE_TIME_SLOT,
                                    PREDICTIVE_MODE_DYNAMIC_PRICING,
                                    PREDICTIVE_MODE_REALTIME_PRICE,
                                ],
                                translation_key="predictive_charging_mode",
                                mode=SelectSelectorMode.LIST,
                            )
                        ),
                }
            ),
        )

    async def async_step_predictive_charging_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure time slot predictive grid charging in options flow.

        Only the charging windows and the optional per-mode forecast sensor
        live here; every other predictive knob is a dashboard entity.
        """
        errors: dict[str, str] = {}
        existing_config = self.config_entry.data

        if user_input is not None:
            try:
                forecast_sensor = _resolve_forecast_sensor(
                    self.hass, existing_config, user_input, errors
                )
                windows, window_errors = _parse_charging_windows(user_input)
                errors.update(window_errors)

                if not errors:
                    self.config_data["enable_predictive_charging"] = True
                    self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_TIME_SLOT
                    self.config_data["charging_time_slot"] = windows
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    return await self._save_and_finish()
            except Exception as e:
                _LOGGER.error("Error validating predictive charging config: %s", e)
                errors["base"] = "unknown"

        schema_dict = _charging_window_schema_fields(
            _normalize_charging_windows(existing_config.get("charging_time_slot"))
        )
        if not _has_global_forecast_sensor(existing_config):
            default_forecast = existing_config.get("solar_forecast_sensor", "")
            schema_dict[vol.Optional(
                "solar_forecast_sensor",
                description={"suggested_value": default_forecast} if default_forecast else {},
            )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        return self.async_show_form(
            step_id="predictive_charging_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_dynamic_pricing_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure dynamic pricing predictive grid charging in options flow.

        Only the price sources live here: they need validating against
        ``hass.states`` and a platform reload. Thresholds, feature toggles and
        every power/SOC limit are dashboard entities writing the same keys.
        """
        errors: dict[str, str] = {}
        existing_config = self.config_entry.data

        if user_input is not None:
            try:
                integration_type = user_input[CONF_PRICE_INTEGRATION_TYPE]
                price_sensor = user_input.get(CONF_PRICE_SENSOR)

                # Tibber has no price sensor - it polls the tibber.get_prices service.
                if integration_type == PRICE_INTEGRATION_TIBBER:
                    price_sensor = None
                    if not self.hass.services.has_service("tibber", "get_prices"):
                        errors[CONF_PRICE_INTEGRATION_TYPE] = "tibber_unavailable"
                elif not price_sensor:
                    errors[CONF_PRICE_SENSOR] = "sensor_not_found"
                else:
                    error = _validate_price_sensor(
                        self.hass, price_sensor, integration_type
                    )
                    if error:
                        errors[CONF_PRICE_SENSOR] = error

                errors.update(
                    _validate_export_price_input(self.hass, user_input, integration_type)
                )
                forecast_sensor = _resolve_forecast_sensor(
                    self.hass, existing_config, user_input, errors
                )

                if not errors:
                    self.config_data["enable_predictive_charging"] = True
                    self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_DYNAMIC_PRICING
                    self.config_data[CONF_PRICE_INTEGRATION_TYPE] = integration_type
                    self.config_data[CONF_PRICE_SENSOR] = price_sensor
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    self.config_data["charging_time_slot"] = None
                    # Cleared entity/select fields arrive absent, so read them
                    # straight from the submission rather than falling back to
                    # the stored value - that is what makes the clear button work.
                    self.config_data[CONF_EXPORT_PRICE_SENSOR] = user_input.get(
                        CONF_EXPORT_PRICE_SENSOR
                    )
                    self.config_data[CONF_EXPORT_PRICE_INTEGRATION_TYPE] = user_input.get(
                        CONF_EXPORT_PRICE_INTEGRATION_TYPE
                    )
                    self.config_data[CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED] = user_input.get(
                        CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                        existing_config.get(
                            CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                            DEFAULT_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                        ),
                    )
                    return await self._save_and_finish()
            except Exception as e:
                _LOGGER.error("Error validating dynamic pricing config: %s", e)
                errors["base"] = "unknown"

        form_input = user_input or {}
        default_integration = form_input.get(
            CONF_PRICE_INTEGRATION_TYPE,
            existing_config.get(CONF_PRICE_INTEGRATION_TYPE, PRICE_INTEGRATION_NORDPOOL),
        )
        default_sensor = form_input.get(
            CONF_PRICE_SENSOR, existing_config.get(CONF_PRICE_SENSOR, "")
        )
        default_export_sensor = (
            form_input.get(CONF_EXPORT_PRICE_SENSOR)
            if user_input is not None
            else existing_config.get(CONF_EXPORT_PRICE_SENSOR)
        )
        default_export_type = (
            form_input.get(CONF_EXPORT_PRICE_INTEGRATION_TYPE)
            if user_input is not None
            else existing_config.get(CONF_EXPORT_PRICE_INTEGRATION_TYPE)
        )

        schema_dict: dict = {
            vol.Required(CONF_PRICE_INTEGRATION_TYPE, default=default_integration):
                _price_integration_type_selector(),
            # Optional: not used by Tibber, which polls the tibber.get_prices service.
            vol.Optional(CONF_PRICE_SENSOR, default=default_sensor if default_sensor else vol.UNDEFINED):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
        }
        # Clearable: suggested_value pre-fills without voluptuous restoring the
        # old value when the field is emptied.
        schema_dict[vol.Optional(
            CONF_EXPORT_PRICE_SENSOR,
            description={"suggested_value": default_export_sensor} if default_export_sensor else {},
        )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        schema_dict[vol.Optional(
            CONF_EXPORT_PRICE_INTEGRATION_TYPE,
            description={"suggested_value": default_export_type} if default_export_type else {},
        )] = _price_integration_type_selector(_price_integration_export_options())
        # Always shown: the provider is picked in this same form, so hiding it
        # until Zonneplan is saved meant a second visit. The engine ignores it
        # unless the export curve is Zonneplan.
        schema_dict[vol.Optional(
            CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
            default=form_input.get(
                CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                existing_config.get(
                    CONF_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                    DEFAULT_ZONNEPLAN_EXPORT_BONUS_ENABLED,
                ),
            ),
        )] = BooleanSelector()
        if not _has_global_forecast_sensor(existing_config):
            default_forecast = (
                form_input.get("solar_forecast_sensor")
                if user_input is not None
                else existing_config.get("solar_forecast_sensor", "")
            )
            schema_dict[vol.Optional(
                "solar_forecast_sensor",
                description={"suggested_value": default_forecast} if default_forecast else {},
            )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        return self.async_show_form(
            step_id="dynamic_pricing_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_realtime_price_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure real-time price charging mode in options flow.

        Only the price sensors live here; the max-price threshold and the
        price discharge control are dashboard entities writing the same keys.
        """
        errors: dict[str, str] = {}
        existing_config = self.config_entry.data

        if user_input is not None:
            try:
                price_sensor = user_input[CONF_PRICE_SENSOR]
                if self.hass.states.get(price_sensor) is None:
                    errors[CONF_PRICE_SENSOR] = "sensor_not_found"
                forecast_sensor = _resolve_forecast_sensor(
                    self.hass, existing_config, user_input, errors
                )

                if not errors:
                    self.config_data["enable_predictive_charging"] = True
                    self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_REALTIME_PRICE
                    self.config_data[CONF_PRICE_SENSOR] = price_sensor
                    self.config_data[CONF_AVERAGE_PRICE_SENSOR] = user_input.get(CONF_AVERAGE_PRICE_SENSOR) or None
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    self.config_data["charging_time_slot"] = None
                    return await self._save_and_finish()
            except Exception as e:
                _LOGGER.error("Error validating real-time price config: %s", e)
                errors["base"] = "unknown"

        default_sensor = existing_config.get(CONF_PRICE_SENSOR, "")
        default_avg_sensor = existing_config.get(CONF_AVERAGE_PRICE_SENSOR, "")

        schema_dict: dict = {
            vol.Required(CONF_PRICE_SENSOR, default=default_sensor if default_sensor else vol.UNDEFINED):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_AVERAGE_PRICE_SENSOR,
                description={"suggested_value": default_avg_sensor} if default_avg_sensor else {}
            ):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
        }
        if not _has_global_forecast_sensor(existing_config):
            default_forecast = existing_config.get("solar_forecast_sensor", "")
            schema_dict[vol.Optional(
                "solar_forecast_sensor",
                description={"suggested_value": default_forecast} if default_forecast else {},
            )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        return self.async_show_form(
            step_id="realtime_price_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )


def _install_registered_driver_steps() -> None:
    """Generate the flow steps of every registered brand that declares a SETUP."""
    for brand, driver in registry.DRIVERS.items():
        if driver.SETUP is not None:
            for flow_class in (MarstekVenusConfigFlow, OptionsFlowHandler):
                _install_driver_steps(flow_class, driver.SETUP, brand)


_install_registered_driver_steps()
