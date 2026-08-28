"""Config flow for Omnibattery integration."""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.config_entries import ConfigFlow, OptionsFlow, ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_USERNAME,
    CONF_PASSWORD,
)
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    TimeSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    BooleanSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .infra.mac_tracking import (
    CANDIDATE_SILENT,
    CONF_MAC,
    CONF_TRACK_MAC,
    detect_mac,
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
    CONF_DC_PV_CONNECTED,
    CONF_SLAVE_ID,
    DEFAULT_SLAVE_ID,
    CONF_SERIAL_PORT,
    DEFAULT_VERSION,
    MAX_POWER_BY_VERSION,
    max_power_for_battery_version,
    MAX_BATTERIES,
    CONF_ENABLE_SYSTEM_POWER_LIMITS,
    CONF_CAPACITY_PROTECTION_ENABLED,
    CONF_CAPACITY_PROTECTION_EXCLUDED_DEVICES,
    CONF_PREDICTIVE_CHARGING_MODE,
    CONF_PRICE_SENSOR,
    CONF_PRICE_INTEGRATION_TYPE,
    CONF_MAX_PRICE_THRESHOLD,
    CONF_DISCHARGE_PRICE_THRESHOLD,
    CONF_SMART_PREDISCHARGE_ENABLED,
    CONF_SURPLUS_PRICE_HOLD_ENABLED,
    DEFAULT_SURPLUS_PRICE_HOLD_ENABLED,
    CONF_SURPLUS_HOLD_MIN_SAVING,
    DEFAULT_SURPLUS_HOLD_MIN_SAVING,
    CONF_EXPORT_PRICE_SENSOR,
    CONF_EXPORT_PRICE_INTEGRATION_TYPE,
    CONF_NEGATIVE_INJECTION_THRESHOLD,
    CONF_PREDISCHARGE_RESERVE_SOC,
    CONF_PREDISCHARGE_MAX_EXPORT_POWER_W,
    CONF_PREDISCHARGE_EXPORT_MODE,
    PREDISCHARGE_EXPORT_MODE_SELF_CONSUMPTION,
    PREDISCHARGE_EXPORT_MODE_AUTOMATIC,
    PREDISCHARGE_EXPORT_MODE_CUSTOM,
    PREDISCHARGE_EXPORT_MODES,
    DEFAULT_PREDISCHARGE_EXPORT_MODE,
    normalize_predischarge_export_settings,
    DEFAULT_SMART_PREDISCHARGE_ENABLED,
    DEFAULT_NEGATIVE_INJECTION_THRESHOLD,
    DEFAULT_PREDISCHARGE_RESERVE_SOC,
    DEFAULT_PREDISCHARGE_MAX_EXPORT_POWER_W,
    CONF_NEGATIVE_PRICE_CHARGING_ENABLED,
    DEFAULT_NEGATIVE_PRICE_CHARGING_ENABLED,
    CONF_AVERAGE_PRICE_SENSOR,
    CONF_DP_PRICE_DISCHARGE_CONTROL,
    CONF_RT_PRICE_DISCHARGE_CONTROL,
    PREDICTIVE_MODE_TIME_SLOT,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
    PRICE_INTEGRATION_NORDPOOL,
    PRICE_INTEGRATION_PVPC,
    PRICE_INTEGRATION_CKW,
    PRICE_INTEGRATION_EPEX,
    PRICE_INTEGRATION_ENTSOE,
    PRICE_INTEGRATION_TIBBER,
    CONF_METER_INVERTED,
    CONF_PREDICTIVE_SAFETY_MARGIN_KWH,
    DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH,
    CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT,
    DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT,
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
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
from .drivers.esphome import EsphomeEntityDriver
from .drivers.marstek import MarstekModbusDriver
from .drivers.zendure import (
    ZendureLocalDriver,
    detect_model as _detect_zendure_model,
    zendure_power_limits as _zendure_power_limits,
)
from .drivers.anker import AnkerModbusDriver
from .drivers.sessy import SessyLocalDriver
from .drivers.huawei import HuaweiSolarDriver
from .drivers.hoymiles import (
    DEFAULT_HOYMILES_MODEL,
    HOYMILES_MODEL_PROFILES,
    HoymilesMqttDriver,
    hoymiles_capacity_kwh,
    hoymiles_model_profile,
)
from .pricing.nordpool import is_official_nordpool_sensor

_ANKER_MAX_POWER_W = 3500
_SESSY_MAX_CHARGE_POWER_W = 2200
_SESSY_MAX_DISCHARGE_POWER_W = 1700
_SESSY_DEFAULT_MIN_SOC = 5
_HOYMILES_MODEL_AUTO = "auto"


def _hoymiles_model_selector(default: str = _HOYMILES_MODEL_AUTO):
    """Build the MQTT model selector, retaining automatic discovery by default."""
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
        PRICE_INTEGRATION_TIBBER,
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


def _predischarge_export_defaults(
    config: dict[str, Any],
    *,
    default_mode: str = DEFAULT_PREDISCHARGE_EXPORT_MODE,
) -> tuple[str, float]:
    """Return selector defaults, inferring the mode for legacy entries."""
    stored_mode = config.get(CONF_PREDISCHARGE_EXPORT_MODE)
    stored_power = config.get(
        CONF_PREDISCHARGE_MAX_EXPORT_POWER_W,
        DEFAULT_PREDISCHARGE_MAX_EXPORT_POWER_W,
    )
    if stored_mode is None and CONF_PREDISCHARGE_MAX_EXPORT_POWER_W not in config:
        stored_mode = default_mode
    return normalize_predischarge_export_settings(stored_mode, stored_power)


def _predischarge_export_from_input(
    user_input: dict[str, Any],
    *,
    fallback_mode: str,
    fallback_power: float = 0.0,
) -> tuple[str, float]:
    """Normalize submitted selector data while accepting legacy test/API data."""
    mode = user_input.get(CONF_PREDISCHARGE_EXPORT_MODE)
    if mode is None and CONF_PREDISCHARGE_MAX_EXPORT_POWER_W not in user_input:
        mode = fallback_mode
    return normalize_predischarge_export_settings(
        mode,
        user_input.get(CONF_PREDISCHARGE_MAX_EXPORT_POWER_W, fallback_power),
    )


def _predischarge_export_mode_selector(default: str):
    """Build the three-way deliberate-export selector."""
    return vol.Required(CONF_PREDISCHARGE_EXPORT_MODE, default=default), SelectSelector(
        SelectSelectorConfig(
            options=list(PREDISCHARGE_EXPORT_MODES),
            translation_key="predischarge_export_mode",
            mode=SelectSelectorMode.LIST,
        )
    )


def _predischarge_export_limit_selector(default: float):
    """Build the custom deliberate-export limit field."""
    return vol.Required(
        CONF_PREDISCHARGE_MAX_EXPORT_POWER_W,
        default=default,
    ), NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=10000,
            step=50,
            unit_of_measurement="W",
            mode=NumberSelectorMode.BOX,
        )
    )


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


def _soc_selector_limits(brand: str) -> tuple[int, int, int, int, int, int]:
    """Return minimum and maximum SOC selector bounds and defaults."""
    if brand == "zendure":
        min_lo, min_hi, min_default = 5, 50, 12
    elif brand == "anker":
        min_lo, min_hi, min_default = 0, 20, 10
    elif brand == "sessy":
        min_lo, min_hi, min_default = 0, 30, _SESSY_DEFAULT_MIN_SOC
    elif brand == "hoymiles":
        min_lo, min_hi, min_default = 0, 30, 10
    elif brand == "huawei":
        # The inverter keeps its own discharge cutoff as a backstop; this window
        # is what Omnibattery enforces on top of it.
        min_lo, min_hi, min_default = 0, 30, 10
    else:
        min_lo, min_hi, min_default = 12, 30, 12

    # Omnibattery enforces the charge ceiling in software. Sessy's reported SOC
    # spans 0–100 %, so the standard 100 % ceiling is valid for this driver.
    return min_lo, min_hi, min_default, 80, 100, 100


def _hoymiles_apply_probe_caps(
    battery_data: dict, caps: dict, *, upgrade_legacy_defaults: bool = False
) -> None:
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


def _anker_apply_probe_caps(battery_data: dict, caps: dict) -> None:
    """Store Anker hardware ceilings from probe for config seeding."""
    for src, dst in (
        ("device_max_charge_power", "device_max_charge_power"),
        ("device_max_discharge_power", "device_max_discharge_power"),
    ):
        if src in caps:
            battery_data[dst] = int(caps[src])


_HUAWEI_MAX_POWER_W = 15000


def _huawei_power_ceilings(battery_data: dict) -> tuple[int, int]:
    """Upper bound the limits form allows for a Huawei battery.

    Registers 37046/37048 report what the battery permits *right now*, and that
    moves with the pack count — a third pack raises it. Treating a momentary
    reading as a hard ceiling locks the user out of a figure their installation
    can genuinely reach, so it serves as the starting value instead.

    What the installation cannot exceed is the inverter's own maximum active
    power (30075): everything, charge and discharge alike, passes through it.
    Where that is unknown the probe's own figure stands in, and the constant is
    only a sanity bound against a malformed reading.
    """
    inverter_max = int(battery_data.get("device_inverter_max_power") or 0)
    charge = int(battery_data.get("device_max_charge_power") or 5000)
    discharge = int(battery_data.get("device_max_discharge_power") or 5000)
    return (
        max(100, min(_HUAWEI_MAX_POWER_W, inverter_max or charge, _HUAWEI_MAX_POWER_W)),
        max(100, min(_HUAWEI_MAX_POWER_W, inverter_max or discharge, _HUAWEI_MAX_POWER_W)),
    )


def _huawei_inverter_serial(hass, device_id: str) -> str | None:
    """Serial of the inverter a huawei_solar battery device belongs to.

    The battery device hangs off its inverter via ``via_device``, and
    huawei_solar identifies that inverter by its serial — the same string the
    inverter reports over Modbus.
    """
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if device is None:
        return None
    parent = registry.async_get(device.via_device_id) if device.via_device_id else None
    for candidate in (parent, device):
        if candidate is None:
            continue
        if candidate.serial_number:
            return candidate.serial_number
        for domain, identifier in candidate.identifiers:
            if domain == HUAWEI_SOLAR_DOMAIN:
                return identifier
    return None


def _huawei_device_matches_inverter(
    hass: Any,
    device_id: str | None,
    serial: str | None,
    slave_id: int | None,
) -> bool:
    """Whether the selected huawei_solar device belongs to the probed inverter.

    Missing serials are treated as unknown rather than contradictory: older
    huawei_solar versions and some firmware do not expose one. A known
    disagreement, however, must stop both setup and reconfiguration before the
    telemetry/command paths can be paired with different inverters.
    """
    if not device_id or not serial:
        return True
    device_serial = _huawei_inverter_serial(hass, device_id)
    if not device_serial or serial.upper() in device_serial.upper():
        return True
    _LOGGER.warning(
        "Huawei device %s belongs to inverter %s, but slave %s is %s",
        device_id, device_serial, slave_id, serial,
    )
    return False


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


def _seed_software_power_limits(merged: dict, brand: str) -> None:
    """Persist soft-max keys for Zendure (read-only chargeMaxLimit + software ceiling)."""
    if brand != "zendure":
        return
    merged["user_max_charge_power"] = int(merged["max_charge_power"])

_LOGGER = logging.getLogger(__name__)

# The integration that provides the Huawei control services, and the domain it
# identifies its devices under.
HUAWEI_SOLAR_DOMAIN = "huawei_solar"

# A Huawei inverter accepts one Modbus connection, so a second client needs a
# proxy in front. Supplied as a placeholder rather than written into the strings:
# hassfest rejects a literal URL in a translation.
_MODBUS_PROXY_URL = "https://github.com/Akulatraxas/ha-modbusproxy"

_ALL_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# How many predictive-charging windows the user may configure.
MAX_CHARGING_WINDOWS = 3


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
            fields[req(f"start_time{sfx}", default=existing["start_time"])] = TimeSelector()
            fields[req(f"end_time{sfx}", default=existing["end_time"])] = TimeSelector()
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
    version = bcfg.get(CONF_BATTERY_VERSION)
    if version in MAX_POWER_BY_VERSION:
        return max_power_for_battery_version(version, bcfg.get("ems_version"))

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


def _max_system_hardware_power(battery_configs: list[dict]) -> int:
    """Highest hardware power cap across configured batteries (W)."""
    if not battery_configs:
        return 2500
    return max(_battery_hardware_max(b) for b in battery_configs)


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


class MarstekVenusConfigFlow(LegacyDomainMigrationMixin, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Omnibattery."""

    VERSION = 12

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

    async def _test_connection(
        self,
        host: str,
        port: int,
        version: str = "v2",
        slave_id: int = DEFAULT_SLAVE_ID,
        brand: str = "marstek",
        serial_port: str | None = None,
        username: str = "",
        password: str = "",
    ) -> bool:
        """Test connection to a battery.

        ``username`` and ``password`` are only read for Sessy, whose local API
        can require authentication: probing it with empty credentials returns a
        refusal that is indistinguishable from an absent device (#289).
        """
        self._last_marstek_ems_version = None
        if brand == "zendure":
            _LOGGER.info("Probing Zendure device at %s:%s", host, port)
            result, _ = await ZendureLocalDriver.probe(host, port)
        elif brand == "sessy":
            _LOGGER.info("Probing Sessy device at %s:%s", host, port)
            result = await SessyLocalDriver.probe(host, port, username, password)
        elif brand == "anker":
            _LOGGER.info("Probing Anker Solarbank at %s:%s slave %s", host, port, slave_id)
            result, _ = await AnkerModbusDriver.probe(host, port, slave_id)
        elif serial_port:
            _LOGGER.info("Probing Marstek %s over serial %s slave %s", version, serial_port, slave_id)
            if version == "vD":
                result, self._last_marstek_ems_version = await MarstekModbusDriver.probe_details(
                    host, port, version, slave_id, serial_port=serial_port
                )
            else:
                result = await MarstekModbusDriver.probe(
                    host, port, version, slave_id, serial_port=serial_port
                )
        else:
            _LOGGER.info("Probing Marstek %s at %s:%s slave %s", version, host, port, slave_id)
            if version == "vD":
                result, self._last_marstek_ems_version = await MarstekModbusDriver.probe_details(
                    host, port, version, slave_id
                )
            else:
                result = await MarstekModbusDriver.probe(host, port, version, slave_id)
        if not result:
            _LOGGER.error("Failed to connect to %s:%s (brand=%s)", host, port, brand)
        return result

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
                                min=1000, max=20000, step=100, mode=NumberSelectorMode.BOX
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
            if brand == "zendure":
                return await self.async_step_battery_connection_zendure()
            if brand == "esphome":
                return await self.async_step_battery_connection_esphome()
            if brand == "anker":
                return await self.async_step_battery_connection_anker()
            if brand == "hoymiles":
                return await self.async_step_battery_connection_hoymiles()
            if brand == "sessy":
                return await self.async_step_battery_connection_sessy()
            if brand == "huawei":
                return await self.async_step_battery_connection_huawei()
            return await self.async_step_battery_connection()

        return self.async_show_form(
            step_id="battery_brand",
            data_schema=vol.Schema(
                {
                    vol.Required("brand", default="marstek"):
                        SelectSelector(SelectSelectorConfig(
                            options=[
                                {"value": "marstek", "label": "Marstek Venus"},
                                {"value": "zendure", "label": "Zendure SolarFlow"},
                                {"value": "esphome", "label": "Marstek via LilyGo RS485 (ESPHome)"},
                                {"value": "anker", "label": "Anker SOLIX Solarbank Max AC / 4 E5000 Pro"},
                                {"value": "sessy", "label": "Sessy"},
                                {"value": "hoymiles", "label": "Hoymiles MQTT"},
                                {"value": "huawei", "label": "Huawei SUN2000 + LUNA2000"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )),
                }
            ),
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3b (Marstek): Connection details and battery model."""
        errors = {}
        battery_num = self.battery_index + 1

        if user_input is not None:
            battery_version = user_input.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
            slave_id = user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
            serial_port = (user_input.get(CONF_SERIAL_PORT) or "").strip()
            host = (user_input.get(CONF_HOST) or "").strip()
            is_serial = bool(serial_port)

            if not is_serial and not host:
                # No IP and no serial port: nothing to connect to.
                errors["base"] = "host_or_serial_required"
            else:
                if is_serial:
                    # Serial has no IP:port; the path doubles as the battery's
                    # identity (device_key, naming). port stays a placeholder.
                    host = serial_port
                    port = user_input.get(CONF_PORT, 502)
                else:
                    port = user_input[CONF_PORT]

                connection_result = await self._test_connection(
                    host,
                    port,
                    battery_version,
                    slave_id,
                    brand="marstek",
                    serial_port=serial_port or None,
                )
                if not connection_result:
                    errors["base"] = "cannot_connect"
                else:
                    self._current_battery_data.update({
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_SERIAL_PORT: serial_port,
                        CONF_SLAVE_ID: slave_id,
                        CONF_BATTERY_VERSION: battery_version,
                        "brand": "marstek",
                        "ems_version": getattr(self, "_last_marstek_ems_version", None),
                    })
                    return await self.async_step_battery_limits()

        return self.async_show_form(
            step_id="battery_connection",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=f"Marstek Venus {battery_num}"): str,
                    vol.Optional(CONF_HOST): str,
                    vol.Optional(CONF_PORT, default=502): int,
                    vol.Optional(CONF_SERIAL_PORT): str,
                    vol.Required(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID):
                        vol.All(NumberSelector(NumberSelectorConfig(min=1, max=247, step=1, mode=NumberSelectorMode.BOX)), vol.Coerce(int)),
                    vol.Required(CONF_BATTERY_VERSION, default=DEFAULT_VERSION):
                        SelectSelector(SelectSelectorConfig(
                            options=[
                                {"value": "v2", "label": "Ev2"},
                                {"value": "v3", "label": "Ev3"},
                                {"value": "vA", "label": "A"},
                                {"value": "vD", "label": "D"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )),
                }
            ),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_connection_zendure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3b (Zendure): Connection details for a Zendure SolarFlow device."""
        errors = {}
        battery_num = self.battery_index + 1

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input.get(CONF_PORT, 80))
            ok, product = await ZendureLocalDriver.probe(host, port)
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                self._current_battery_data.update({
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_HOST: host,
                    CONF_PORT: port,
                    "brand": "zendure",
                    "zendure_model": _detect_zendure_model(product),
                })
                return await self.async_step_battery_limits()

        return self.async_show_form(
            step_id="battery_connection_zendure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=f"Zendure SolarFlow {battery_num}"): str,
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_PORT, default=80): int,
                }
            ),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_connection_esphome(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3b (ESPHome): pick the LilyGo/ESPHome device bridging the battery."""
        errors = {}
        placeholders: dict[str, str] = {"battery_num": str(self.battery_index + 1)}

        if user_input is not None:
            device_id = user_input["esphome_device"]
            _, missing = EsphomeEntityDriver.resolve(self.hass, device_id)
            if missing:
                errors["base"] = "esphome_entities_missing"
                placeholders["missing"] = ", ".join(missing)
            else:
                self._current_battery_data.update({
                    CONF_NAME: user_input[CONF_NAME],
                    # The registry device id doubles as the battery identity
                    # (device_key, persistence matching); there is no IP:port.
                    CONF_HOST: device_id,
                    CONF_PORT: 0,
                    "brand": "esphome",
                    "esphome_device_id": device_id,
                })
                return await self.async_step_battery_limits()

        return self.async_show_form(
            step_id="battery_connection_esphome",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=f"Marstek Venus {self.battery_index + 1}"): str,
                    vol.Required("esphome_device"):
                        DeviceSelector(DeviceSelectorConfig(integration="esphome")),
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_battery_connection_sessy(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure a Sessy through its local dongle HTTP API."""
        errors = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST], int(user_input.get(CONF_PORT, 80))
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            _LOGGER.info("Probing Sessy device at %s:%s", host, port)
            if await SessyLocalDriver.probe(host, port, username, password):
                self._current_battery_data.update({
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                    "brand": "sessy",
                })
                return await self.async_step_battery_limits()
            errors["base"] = "cannot_connect"
        return self.async_show_form(step_id="battery_connection_sessy", data_schema=vol.Schema({
            vol.Required(CONF_NAME, default=f"Sessy {self.battery_index + 1}"): str,
            vol.Required(CONF_HOST): str,
            vol.Optional(CONF_PORT, default=80): int,
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }), errors=errors, description_placeholders={"battery_num": str(self.battery_index + 1)})

    async def async_step_battery_connection_huawei(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure a Huawei SUN2000 + LUNA2000.

        Control takes one of two paths. By default set-points go through the
        Huawei Solar integration's services, which address the battery by
        device. With direct Modbus writes the driver addresses the inverter
        itself and needs nothing from that integration — which is why the device
        field is optional and only checked when it is actually used.
        """
        errors = {}
        battery_num = self.battery_index + 1
        entry = getattr(self, "config_entry", None)
        current_batteries = entry.data.get("batteries", []) if entry else []
        current_battery = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )

        if user_input is not None:
            host = (user_input[CONF_HOST] or "").strip()
            port = int(user_input.get(CONF_PORT, 502))
            raw_slave = user_input.get(CONF_SLAVE_ID)
            slave_id = None if raw_slave in (None, "") else int(raw_slave)
            device_id = user_input.get("huawei_battery_device") or ""
            direct_write = bool(user_input.get("huawei_direct_write", False))
            self._huawei_pending = {
                CONF_NAME: user_input[CONF_NAME],
                CONF_HOST: host,
                CONF_PORT: port,
                "huawei_battery_device_id": device_id,
                "huawei_direct_write": direct_write,
            }
            if not direct_write and not device_id:
                # Without direct writes every set-point is a service call, and
                # those address the battery by device. A missing integration is
                # a different problem than an unanswered question, so say which.
                errors["huawei_battery_device"] = (
                    "huawei_device_required"
                    if self.hass.config_entries.async_entries(HUAWEI_SOLAR_DOMAIN)
                    else "huawei_solar_missing"
                )
            elif slave_id is not None:
                _LOGGER.info(
                    "Probing Huawei inverter at %s:%s (slave %s)", host, port, slave_id
                )
                ok, model, max_charge, max_discharge, serial, inverter_max = await HuaweiSolarDriver.probe(
                    self.hass, host, port, slave_id
                )
                if ok:
                    result = await self._huawei_store(
                        slave_id, model, max_charge, max_discharge, serial,
                        inverter_max, errors,
                    )
                    if result is not None:
                        return result
                else:
                    # An id that does not answer is a guess worth replacing, not
                    # a reason to send the user away.
                    result = await self._huawei_search(host, port, errors)
                    if result is not None:
                        return result
            else:
                result = await self._huawei_search(host, port, errors)
                if result is not None:
                    return result

        return self.async_show_form(
            step_id="battery_connection_huawei",
            data_schema=self._huawei_schema(current_battery, battery_num),
            errors=errors,
            description_placeholders={
                "battery_num": str(battery_num),
                "proxy_url": _MODBUS_PROXY_URL,
            },
        )

    async def _huawei_search(self, host: str, port: int, errors: dict) -> FlowResult | None:
        """Look for inverters on the bus; returns None when nothing usable was found.

        One match is taken straight away. Several mean a cascade, which only the
        user can resolve — a battery belongs to one of them.
        """
        _LOGGER.info("Scanning %s:%s for Huawei inverters", host, port)
        found = await HuaweiSolarDriver.scan_slave_ids(self.hass, host, port)
        with_battery = [candidate for candidate in found if candidate[2]]
        if len(with_battery) == 1:
            sid = with_battery[0][0]
            ok, model, max_charge, max_discharge, serial, inverter_max = await HuaweiSolarDriver.probe(
                self.hass, host, port, sid
            )
            if ok:
                _LOGGER.info("Found a Huawei battery on slave %s", sid)
                # A mismatch leaves its own error behind and must not be
                # papered over by the scan verdict below.
                return await self._huawei_store(
                    sid, model, max_charge, max_discharge, serial, inverter_max, errors
                )
        if len(with_battery) > 1:
            self._huawei_candidates = with_battery
            return await self.async_step_battery_connection_huawei_slave()
        # A reachable inverter with no SOC means no battery is attached, which is
        # a different mistake than an unreachable address.
        errors["base"] = "no_battery" if found else "cannot_connect"
        return None

    async def async_step_battery_connection_huawei_slave(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick which inverter on the bus this battery belongs to."""
        candidates = getattr(self, "_huawei_candidates", [])
        if user_input is not None:
            slave_id = int(user_input[CONF_SLAVE_ID])
            pending = self._huawei_pending
            ok, model, max_charge, max_discharge, serial, inverter_max = await HuaweiSolarDriver.probe(
                self.hass, pending[CONF_HOST], pending[CONF_PORT], slave_id
            )
            errors: dict[str, str] = {}
            if ok:
                result = await self._huawei_store(
                    slave_id, model, max_charge, max_discharge, serial, inverter_max,
                    errors, "base",
                )
                if result is not None:
                    return result
            return self.async_show_form(
                step_id="battery_connection_huawei_slave",
                data_schema=self._huawei_slave_schema(candidates),
                errors=errors or {"base": "cannot_connect"},
                description_placeholders={"count": str(len(candidates))},
            )

        return self.async_show_form(
            step_id="battery_connection_huawei_slave",
            data_schema=self._huawei_slave_schema(candidates),
            description_placeholders={"count": str(len(candidates))},
        )

    async def _huawei_store(
        self, slave_id, model, max_charge, max_discharge, serial, inverter_max,
        errors, error_key: str = "huawei_battery_device",
    ) -> FlowResult | None:
        """Commit a validated Huawei battery, or refuse a mismatched pairing.

        On the service path the battery is named twice over: once as a Modbus
        address and once as a device in the registry. Nothing forces those to be
        the same inverter, and on a cascade they easily are not — telemetry would
        then come from one unit while the commands went to another. The inverter
        serial shows up on both sides, so the pairing can be checked. Returns
        None with ``errors`` filled when it does not hold.
        """
        device_id = self._huawei_pending.get("huawei_battery_device_id")
        if not _huawei_device_matches_inverter(
            self.hass, device_id, serial, slave_id
        ):
            errors[error_key] = "huawei_device_mismatch"
            return None
        self._current_battery_data.update({
            **self._huawei_pending,
            CONF_SLAVE_ID: slave_id,
            "brand": "huawei",
            "huawei_model": model,
        })
        if max_charge:
            self._current_battery_data["device_max_charge_power"] = int(max_charge)
        if max_discharge:
            self._current_battery_data["device_max_discharge_power"] = int(max_discharge)
        if inverter_max:
            self._current_battery_data["device_inverter_max_power"] = int(inverter_max)
        # An EMMA on the same bus carries the installation's grid meter. Finding
        # it here means a user with one gets a grid reading fast enough to
        # control against without configuring anything.
        emma = await HuaweiSolarDriver.find_emma_slave_id(
            self.hass, self._huawei_pending[CONF_HOST], self._huawei_pending[CONF_PORT]
        )
        if emma is not None:
            self._current_battery_data["huawei_emma_slave_id"] = emma
        # What the battery reports today is the sensible starting value; the
        # form's ceiling is wider, because adding a pack raises it.
        for key, probed in (
            ("max_charge_power", max_charge),
            ("max_discharge_power", max_discharge),
        ):
            if probed and not self._current_battery_data.get(key):
                self._current_battery_data[key] = int(probed)
        return await self.async_step_battery_limits()

    @staticmethod
    def _huawei_slave_schema(candidates: list) -> vol.Schema:
        return vol.Schema({
            vol.Required(CONF_SLAVE_ID, default=str(candidates[0][0]) if candidates else "1"):
                SelectSelector(SelectSelectorConfig(
                    options=[
                        {"value": str(sid), "label": f"{model} — Slave {sid}"}
                        for sid, model, _battery in candidates
                    ],
                    mode=SelectSelectorMode.LIST,
                )),
        })

    @staticmethod
    def _huawei_schema(current_battery: dict, battery_num: int) -> vol.Schema:
        """Form for a Huawei battery.

        The battery device is optional because it is only needed for the service
        control path; with direct writes the driver addresses the inverter over
        Modbus and needs nothing from huawei_solar. Requiring it there would make
        the form impossible to submit on an installation that does not run that
        integration at all. Which of the two is missing is checked on submit.
        """
        return vol.Schema({
            vol.Required(
                CONF_NAME,
                default=current_battery.get(CONF_NAME, f"Huawei LUNA2000 {battery_num}"),
            ): str,
            vol.Required(CONF_HOST, default=current_battery.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=current_battery.get(CONF_PORT, 502)): int,
            # No default: an empty field means "go and find it". The id is not
            # derivable, so prefilling a guess would only invite the user to
            # accept a wrong one.
            # A selector, not a bare vol.Any: the frontend is handed a
            # serialised schema, and vol.Any has no serialised form — a form
            # containing one cannot be drawn at all.
            vol.Optional(
                CONF_SLAVE_ID,
                description={"suggested_value": current_battery.get(CONF_SLAVE_ID)},
            ): NumberSelector(
                NumberSelectorConfig(min=0, max=247, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Optional(
                "huawei_direct_write",
                default=current_battery.get("huawei_direct_write", False),
            ): bool,
            vol.Optional(
                "huawei_battery_device",
                description={
                    "suggested_value": current_battery.get("huawei_battery_device_id")
                },
            ): DeviceSelector(
                DeviceSelectorConfig(integration=HUAWEI_SOLAR_DOMAIN, model="Batteries")
            ),
        })

    async def async_step_battery_connection_hoymiles(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure a Hoymiles battery through Home Assistant's MQTT broker."""
        errors = {}
        if user_input is not None:
            device_id = user_input["device_id"].strip()
            ok, caps = await HoymilesMqttDriver.probe(
                self.hass,
                device_id,
                model_hint=_hoymiles_model_hint(user_input),
            )
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                self._current_battery_data.update({CONF_NAME: user_input[CONF_NAME], CONF_HOST: device_id,
                    CONF_PORT: 0, "device_id": device_id, "brand": "hoymiles"})
                _hoymiles_apply_probe_caps(self._current_battery_data, caps)
                return await self.async_step_battery_limits()
        model_field, model_selector = _hoymiles_model_selector()
        return self.async_show_form(step_id="battery_connection_hoymiles", data_schema=vol.Schema({
            vol.Required(CONF_NAME, default=f"Hoymiles {self.battery_index + 1}"): str,
            vol.Required("device_id"): str,
            model_field: model_selector,
        }), errors=errors, description_placeholders={"battery_num": str(self.battery_index + 1)})


    async def async_step_battery_connection_anker(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3b (Anker): Connection details for Solarbank Max AC / 4 E5000 Pro."""
        errors = {}
        battery_num = self.battery_index + 1

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input.get(CONF_PORT, 502))
            slave_id = int(user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID))
            ok, caps = await AnkerModbusDriver.probe(host, port, slave_id)
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                self._current_battery_data.update({
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_SLAVE_ID: slave_id,
                    "brand": "anker",
                })
                _anker_apply_probe_caps(self._current_battery_data, caps)
                return await self.async_step_battery_limits()

        return self.async_show_form(
            step_id="battery_connection_anker",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=f"Anker Solarbank {battery_num}"): str,
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_PORT, default=502): int,
                    vol.Required(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID):
                        vol.All(NumberSelector(NumberSelectorConfig(min=1, max=247, step=1, mode=NumberSelectorMode.BOX)), vol.Coerce(int)),
                }
            ),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_limits(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3c: Power and SOC limits for the current battery."""
        errors: dict[str, str] = {}
        battery_num = self.battery_index + 1
        brand = self._current_battery_data.get("brand", "marstek")
        if brand == "zendure":
            max_charge_power, max_discharge_power = _zendure_power_limits(
                self._current_battery_data.get("zendure_model", "2400ac_pro")
            )
        elif brand == "anker":
            max_charge_power, max_discharge_power = _anker_power_ceilings(
                self._current_battery_data
            )
        elif brand == "sessy":
            max_charge_power = _SESSY_MAX_CHARGE_POWER_W
            max_discharge_power = _SESSY_MAX_DISCHARGE_POWER_W
        elif brand == "hoymiles":
            max_charge_power, max_discharge_power = _hoymiles_power_ceilings(self._current_battery_data)
        elif brand == "huawei":
            max_charge_power, max_discharge_power = _huawei_power_ceilings(self._current_battery_data)
        else:
            battery_version = self._current_battery_data.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
            max_charge_power = max_discharge_power = max_power_for_battery_version(
                battery_version, self._current_battery_data.get("ems_version")
            )
        (
            soc_min_lo,
            soc_min_hi,
            soc_min_default,
            soc_max_lo,
            soc_max_hi,
            soc_max_default,
        ) = _soc_selector_limits(brand)

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
                if brand == "anker":
                    # Power caps are device sensors (10036/10038), not setup inputs.
                    charge_w, discharge_w = _anker_power_ceilings(self._current_battery_data)
                    merged["max_charge_power"] = charge_w
                    merged["max_discharge_power"] = discharge_w
                else:
                    merged["max_charge_power"] = int(user_input["max_charge_power"])
                    merged["max_discharge_power"] = int(user_input["max_discharge_power"])
                merged["max_soc"] = int(user_input["max_soc"])
                merged["min_soc"] = int(user_input["min_soc"])
                if self.config_data.get(CONF_THREE_PHASE_ENABLED):
                    merged[CONF_BATTERY_PHASE] = normalize_battery_phase(phase)
                _seed_software_power_limits(merged, brand)
                # Hysteresis is mandatory; floor the percent against SOC drift.
                merged["enable_charge_hysteresis"] = True
                merged["charge_hysteresis_percent"] = max(
                    MIN_CHARGE_HYSTERESIS_PERCENT,
                    int(user_input.get("charge_hysteresis_percent", DEFAULT_CHARGE_HYSTERESIS_PERCENT)),
                )
                merged["backup_offgrid_threshold"] = int(user_input.get("backup_offgrid_threshold", 50))
                merged[CONF_DC_PV_CONNECTED] = bool(
                    user_input.get(CONF_DC_PV_CONNECTED, True)
                ) if merged.get(CONF_BATTERY_VERSION) in ("vA", "vD") else False
                merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = (
                    False if brand in ("zendure", "anker", "sessy", "hoymiles", "huawei")
                    else user_input.get(CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED)
                )
                if brand in ("zendure", "sessy", "hoymiles"):
                    capacity_default = (
                        _hoymiles_capacity_default(self._current_battery_data)
                        if brand == "hoymiles" else 0.0
                    )
                    merged["battery_capacity_kwh"] = round(float(user_input.get("battery_capacity_kwh", capacity_default)), 2)
                _apply_mac_tracking(user_input, merged)
                self.battery_configs.append(merged)
                self.battery_index += 1

                if self.battery_index >= self.config_data["num_batteries"]:
                    self.config_data["batteries"] = self.battery_configs
                    return await self.async_step_time_slots()
                return await self.async_step_battery_brand()

        _schema: dict = {}
        if brand != "anker":
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
            vol.Required("min_soc", default=soc_min_default):
                NumberSelector(NumberSelectorConfig(min=soc_min_lo, max=soc_min_hi, step=1, mode=NumberSelectorMode.SLIDER)),
            vol.Required("charge_hysteresis_percent", default=DEFAULT_CHARGE_HYSTERESIS_PERCENT):
                NumberSelector(NumberSelectorConfig(min=MIN_CHARGE_HYSTERESIS_PERCENT, max=MAX_CHARGE_HYSTERESIS_PERCENT, step=1, mode=NumberSelectorMode.SLIDER)),
            vol.Required("backup_offgrid_threshold", default=50):
                NumberSelector(NumberSelectorConfig(min=0, max=2500, step=10, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)),
        })
        if (
            brand == "marstek"
            and self._current_battery_data.get(CONF_BATTERY_VERSION) in ("vA", "vD")
        ):
            _schema[vol.Required(CONF_DC_PV_CONNECTED, default=True)] = BooleanSelector()
        if brand not in ("zendure", "anker", "sessy", "hoymiles", "huawei"):
            _schema[vol.Required(CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, default=DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED)] = bool
        if brand == "sessy":
            _schema[vol.Required("battery_capacity_kwh")] = NumberSelector(
                NumberSelectorConfig(min=0.01, max=100, step=0.01, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
            )
        elif brand in ("zendure", "hoymiles"):
            capacity_default = (
                _hoymiles_capacity_default(self._current_battery_data)
                if brand == "hoymiles" else 0.0
            )
            _schema[vol.Optional("battery_capacity_kwh", default=capacity_default)] = NumberSelector(
                NumberSelectorConfig(min=0.01 if brand == "hoymiles" else 0, max=100, step=0.01, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
            )
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
                "included_in_consumption": user_input.get("included_in_consumption", True),
                "allow_solar_surplus": user_input.get("allow_solar_surplus", False),
                "dynamic_power_control": dynamic_power_control,
                "cover_home_when_active": user_input.get("cover_home_when_active", False),
                "ev_charger_no_telemetry": ev_no_telemetry,
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
                    vol.Optional("remaining_demand_sensor"):
                        EntitySelector(
                            # No device_class filter: _read_sensor_kwh_opt accepts any
                            # convertible energy unit, and a template helper
                            # often carries no device_class at all.
                            EntitySelectorConfig(domain="sensor")
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
        """Step 11a: Configure time slot predictive grid charging."""
        errors = {}
        # Check if solar forecast sensor was already configured in step 1
        has_global_sensor = bool(
            self.config_data.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
            or self.config_data.get(CONF_SOLAR_FORECAST_SENSOR)
        )

        if user_input is not None:
                try:
                    if has_global_sensor:
                        forecast_sensor = self.config_data.get(CONF_SOLAR_FORECAST_SENSOR)
                    else:
                        forecast_sensor = user_input.get("solar_forecast_sensor")
                        if forecast_sensor:
                            forecast_state = self.hass.states.get(forecast_sensor)
                            if forecast_state is None:
                                errors["solar_forecast_sensor"] = "sensor_not_found"
                            else:
                                unit = forecast_state.attributes.get("unit_of_measurement", "")
                                if unit not in ["kWh", "Wh"]:
                                    errors["solar_forecast_sensor"] = "invalid_unit"

                    windows, window_errors = _parse_charging_windows(user_input)
                    errors.update(window_errors)

                    if not errors:
                        self.config_data["enable_predictive_charging"] = True
                        self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_TIME_SLOT
                        self.config_data["charging_time_slot"] = windows
                        self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                        self.config_data[CONF_PREDICTIVE_SAFETY_MARGIN_KWH] = user_input.get(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)
                        self.config_data[CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT] = user_input.get(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)

                        return await self._finish_setup()
                except Exception as e:
                    _LOGGER.error("Error validating predictive charging config: %s", e)
                    errors["base"] = "unknown"

        schema_dict = _charging_window_schema_fields([])
        if not has_global_sensor:
            schema_dict[vol.Optional("solar_forecast_sensor")] = EntitySelector(
                EntitySelectorConfig(domain="sensor")
            )
        schema_dict[vol.Optional(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, default=DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)] = NumberSelector(
            NumberSelectorConfig(min=0, max=20, step=0.1, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, default=DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)] = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=5, unit_of_measurement="%", mode=NumberSelectorMode.BOX)
        )
        return self.async_show_form(
            step_id="predictive_charging_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_dynamic_pricing_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 11b: Configure dynamic pricing predictive grid charging."""
        errors = {}
        has_global_sensor = bool(
            self.config_data.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
            or self.config_data.get(CONF_SOLAR_FORECAST_SENSOR)
        )

        if user_input is not None:
            try:
                integration_type = user_input[CONF_PRICE_INTEGRATION_TYPE]
                price_sensor = user_input.get(CONF_PRICE_SENSOR)

                # Tibber has no price sensor — it polls the tibber.get_prices service.
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

                # Validate solar forecast sensor if not global
                if has_global_sensor:
                    forecast_sensor = self.config_data.get(CONF_SOLAR_FORECAST_SENSOR)
                else:
                    forecast_sensor = user_input.get("solar_forecast_sensor")
                    if forecast_sensor:
                        forecast_state = self.hass.states.get(forecast_sensor)
                        if forecast_state is None:
                            errors["solar_forecast_sensor"] = "sensor_not_found"
                        else:
                            unit = forecast_state.attributes.get("unit_of_measurement", "")
                            if unit not in ["kWh", "Wh"]:
                                errors["solar_forecast_sensor"] = "invalid_unit"

                if not errors:
                    max_price = _parse_optional_float(user_input.get(CONF_MAX_PRICE_THRESHOLD))
                    discharge_price = _parse_optional_float(user_input.get(CONF_DISCHARGE_PRICE_THRESHOLD))

                    if max_price is not None and discharge_price is not None and discharge_price < max_price:
                        errors[CONF_DISCHARGE_PRICE_THRESHOLD] = "discharge_below_charge"
                    else:
                        self.config_data["enable_predictive_charging"] = True
                        self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_DYNAMIC_PRICING
                        self.config_data[CONF_PRICE_INTEGRATION_TYPE] = integration_type
                        self.config_data[CONF_PRICE_SENSOR] = price_sensor
                        self.config_data[CONF_MAX_PRICE_THRESHOLD] = max_price
                        self.config_data[CONF_DISCHARGE_PRICE_THRESHOLD] = discharge_price
                        self.config_data[CONF_DP_PRICE_DISCHARGE_CONTROL] = user_input.get(CONF_DP_PRICE_DISCHARGE_CONTROL, False)
                        self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                        self.config_data["charging_time_slot"] = None
                        self.config_data[CONF_PREDICTIVE_SAFETY_MARGIN_KWH] = user_input.get(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)
                        self.config_data[CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT] = user_input.get(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)
                        self.config_data[CONF_NEGATIVE_PRICE_CHARGING_ENABLED] = user_input.get(
                            CONF_NEGATIVE_PRICE_CHARGING_ENABLED,
                            DEFAULT_NEGATIVE_PRICE_CHARGING_ENABLED,
                        )
                        self.config_data[CONF_SMART_PREDISCHARGE_ENABLED] = user_input.get(
                            CONF_SMART_PREDISCHARGE_ENABLED, DEFAULT_SMART_PREDISCHARGE_ENABLED
                        )
                        self.config_data[CONF_SURPLUS_PRICE_HOLD_ENABLED] = user_input.get(
                            CONF_SURPLUS_PRICE_HOLD_ENABLED, DEFAULT_SURPLUS_PRICE_HOLD_ENABLED
                        )
                        self.config_data[CONF_SURPLUS_HOLD_MIN_SAVING] = user_input.get(
                            CONF_SURPLUS_HOLD_MIN_SAVING, DEFAULT_SURPLUS_HOLD_MIN_SAVING
                        )
                        self.config_data[CONF_EXPORT_PRICE_SENSOR] = user_input.get(
                            CONF_EXPORT_PRICE_SENSOR
                        )
                        self.config_data[CONF_EXPORT_PRICE_INTEGRATION_TYPE] = user_input.get(
                            CONF_EXPORT_PRICE_INTEGRATION_TYPE
                        )
                        self.config_data[CONF_NEGATIVE_INJECTION_THRESHOLD] = user_input.get(
                            CONF_NEGATIVE_INJECTION_THRESHOLD, DEFAULT_NEGATIVE_INJECTION_THRESHOLD
                        )
                        self.config_data[CONF_PREDISCHARGE_RESERVE_SOC] = user_input.get(
                            CONF_PREDISCHARGE_RESERVE_SOC, DEFAULT_PREDISCHARGE_RESERVE_SOC
                        )
                        export_mode, export_power = _predischarge_export_from_input(
                            user_input,
                            fallback_mode=DEFAULT_PREDISCHARGE_EXPORT_MODE,
                            fallback_power=DEFAULT_PREDISCHARGE_MAX_EXPORT_POWER_W,
                        )
                        self.config_data[CONF_PREDISCHARGE_EXPORT_MODE] = export_mode
                        self.config_data[CONF_PREDISCHARGE_MAX_EXPORT_POWER_W] = export_power
                        if (
                            export_mode == PREDISCHARGE_EXPORT_MODE_CUSTOM
                            and CONF_PREDISCHARGE_MAX_EXPORT_POWER_W not in user_input
                        ):
                            return await self.async_step_predischarge_export_limit()
                        return await self._finish_setup()
            except Exception as e:
                _LOGGER.error("Error validating dynamic pricing config: %s", e)
                errors["base"] = "unknown"

        schema_dict: dict = {
            vol.Required(CONF_PRICE_INTEGRATION_TYPE, default=PRICE_INTEGRATION_NORDPOOL):
                _price_integration_type_selector(),
            # Optional: not used by Tibber, which polls the tibber.get_prices service.
            vol.Optional(CONF_PRICE_SENSOR):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_MAX_PRICE_THRESHOLD):
                TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(CONF_DISCHARGE_PRICE_THRESHOLD):
                TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(CONF_DP_PRICE_DISCHARGE_CONTROL, default=False): bool,
        }
        if not has_global_sensor:
            schema_dict[vol.Optional("solar_forecast_sensor")] = EntitySelector(
                EntitySelectorConfig(domain="sensor")
            )
        schema_dict[vol.Optional(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, default=DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)] = NumberSelector(
            NumberSelectorConfig(min=0, max=20, step=0.1, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, default=DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)] = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=5, unit_of_measurement="%", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_NEGATIVE_PRICE_CHARGING_ENABLED, default=DEFAULT_NEGATIVE_PRICE_CHARGING_ENABLED)] = bool
        schema_dict[vol.Optional(CONF_SMART_PREDISCHARGE_ENABLED, default=DEFAULT_SMART_PREDISCHARGE_ENABLED)] = bool
        schema_dict[vol.Optional(CONF_SURPLUS_PRICE_HOLD_ENABLED, default=DEFAULT_SURPLUS_PRICE_HOLD_ENABLED)] = bool
        schema_dict[vol.Optional(CONF_SURPLUS_HOLD_MIN_SAVING, default=DEFAULT_SURPLUS_HOLD_MIN_SAVING)] = NumberSelector(
            NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="€/kWh", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_EXPORT_PRICE_SENSOR)] = EntitySelector(
            EntitySelectorConfig(domain="sensor")
        )
        schema_dict[vol.Optional(CONF_EXPORT_PRICE_INTEGRATION_TYPE)] = (
            _price_integration_type_selector(_price_integration_export_options())
        )
        schema_dict[vol.Optional(CONF_NEGATIVE_INJECTION_THRESHOLD, default=DEFAULT_NEGATIVE_INJECTION_THRESHOLD)] = NumberSelector(
            NumberSelectorConfig(min=-2, max=2, step=0.001, unit_of_measurement="€/kWh", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_PREDISCHARGE_RESERVE_SOC, default=DEFAULT_PREDISCHARGE_RESERVE_SOC)] = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, unit_of_measurement="%", mode=NumberSelectorMode.BOX)
        )
        mode_field, mode_selector = _predischarge_export_mode_selector(
            DEFAULT_PREDISCHARGE_EXPORT_MODE
        )
        schema_dict[mode_field] = mode_selector
        return self.async_show_form(
            step_id="dynamic_pricing_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_predischarge_export_limit(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure the W limit only for the custom export policy."""
        if user_input is not None:
            _mode, export_power = _predischarge_export_from_input(
                user_input,
                fallback_mode=PREDISCHARGE_EXPORT_MODE_CUSTOM,
            )
            self.config_data[CONF_PREDISCHARGE_EXPORT_MODE] = PREDISCHARGE_EXPORT_MODE_CUSTOM
            self.config_data[CONF_PREDISCHARGE_MAX_EXPORT_POWER_W] = export_power
            return await self._finish_setup()

        _mode, export_power = _predischarge_export_defaults(
            self.config_data,
            default_mode=PREDISCHARGE_EXPORT_MODE_CUSTOM,
        )
        limit_field, limit_selector = _predischarge_export_limit_selector(export_power)
        return self.async_show_form(
            step_id="predischarge_export_limit",
            data_schema=vol.Schema({limit_field: limit_selector}),
        )

    async def async_step_realtime_price_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 11d: Configure real-time price charging mode."""
        errors = {}
        has_global_sensor = bool(
            self.config_data.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
            or self.config_data.get(CONF_SOLAR_FORECAST_SENSOR)
        )

        if user_input is not None:
            try:
                price_sensor = user_input[CONF_PRICE_SENSOR]
                price_state = self.hass.states.get(price_sensor)
                if price_state is None:
                    errors[CONF_PRICE_SENSOR] = "sensor_not_found"

                if has_global_sensor:
                    forecast_sensor = self.config_data.get(CONF_SOLAR_FORECAST_SENSOR)
                else:
                    forecast_sensor = user_input.get("solar_forecast_sensor")
                    if forecast_sensor:
                        forecast_state = self.hass.states.get(forecast_sensor)
                        if forecast_state is None:
                            errors["solar_forecast_sensor"] = "sensor_not_found"
                        else:
                            unit = forecast_state.attributes.get("unit_of_measurement", "")
                            if unit not in ["kWh", "Wh"]:
                                errors["solar_forecast_sensor"] = "invalid_unit"

                if not errors:
                    max_price_raw = user_input.get(CONF_MAX_PRICE_THRESHOLD)
                    max_price = float(str(max_price_raw).replace(",", ".")) if max_price_raw else None
                    avg_sensor = user_input.get(CONF_AVERAGE_PRICE_SENSOR) or None

                    self.config_data["enable_predictive_charging"] = True
                    self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_REALTIME_PRICE
                    self.config_data[CONF_PRICE_SENSOR] = price_sensor
                    self.config_data[CONF_MAX_PRICE_THRESHOLD] = max_price
                    self.config_data[CONF_AVERAGE_PRICE_SENSOR] = avg_sensor
                    self.config_data[CONF_RT_PRICE_DISCHARGE_CONTROL] = user_input.get(CONF_RT_PRICE_DISCHARGE_CONTROL, False)
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    self.config_data["charging_time_slot"] = None
                    self.config_data[CONF_PREDICTIVE_SAFETY_MARGIN_KWH] = user_input.get(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)
                    self.config_data[CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT] = user_input.get(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)

                    return await self._finish_setup()
            except Exception as e:
                _LOGGER.error("Error validating real-time price config: %s", e)
                errors["base"] = "unknown"

        schema_dict: dict = {
            vol.Required(CONF_PRICE_SENSOR):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_MAX_PRICE_THRESHOLD):
                TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(CONF_AVERAGE_PRICE_SENSOR):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(CONF_RT_PRICE_DISCHARGE_CONTROL, default=False): bool,
        }
        if not has_global_sensor:
            schema_dict[vol.Optional("solar_forecast_sensor")] = EntitySelector(
                EntitySelectorConfig(domain="sensor")
            )
        schema_dict[vol.Optional(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, default=DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)] = NumberSelector(
            NumberSelectorConfig(min=0, max=20, step=0.1, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, default=DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)] = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=5, unit_of_measurement="%", mode=NumberSelectorMode.BOX)
        )
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
        def _device_key(host: str, port: int, slave: int) -> str:
            return f"{host}_{port}" if slave == 1 else f"{host}_{port}_{slave}"

        old_device_id = _device_key(old_host, old_port, old_slave)
        new_device_id = _device_key(new_host, new_port, new_slave)
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
        old_dev = dev_reg.async_get_device(identifiers={(DOMAIN, old_device_id)})
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
        """Update connection settings for each battery during reconfiguration."""
        entry = self._get_reconfigure_entry()
        current_batteries = entry.data.get("batteries", [])
        battery_num = self.battery_index + 1
        current = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )

        if current.get("brand", "marstek") == "zendure":
            return await self.async_step_reconfigure_battery_zendure(user_input)
        if current.get("brand", "marstek") == "esphome":
            return await self.async_step_reconfigure_battery_esphome(user_input)
        if current.get("brand", "marstek") == "anker":
            return await self.async_step_reconfigure_battery_anker(user_input)
        if current.get("brand", "marstek") == "sessy":
            return await self.async_step_reconfigure_battery_sessy(user_input)
        if current.get("brand", "marstek") == "hoymiles":
            return await self.async_step_reconfigure_battery_hoymiles(user_input)
        if current.get("brand", "marstek") == "huawei":
            return await self.async_step_reconfigure_battery_huawei(user_input)

        errors = {}

        if user_input is not None:
            battery_version = user_input.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
            slave_id = user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
            serial_port = (user_input.get(CONF_SERIAL_PORT) or "").strip()
            new_host = (user_input.get(CONF_HOST) or "").strip()
            is_serial = bool(serial_port)

            if is_serial:
                new_host = serial_port  # path doubles as identity (see add flow)
                new_port = user_input.get(CONF_PORT, 502)
            else:
                new_port = user_input.get(CONF_PORT, 502)

            if not is_serial and not new_host:
                errors["base"] = "host_or_serial_required"
            elif not await self._test_connection(
                new_host, new_port, battery_version, slave_id,
                serial_port=serial_port or None,
            ):
                errors["base"] = "cannot_connect"
            else:
                old_host = current.get(CONF_HOST)
                old_port = current.get(CONF_PORT)
                old_slave = current.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)

                if (
                    old_host
                    and old_port
                    and (old_host != new_host or old_port != new_port or old_slave != slave_id)
                ):
                    self._migrate_battery_registry_ids(
                        entry, old_host, old_port, new_host, new_port, old_slave, slave_id
                    )

                updated = dict(current)
                updated[CONF_NAME] = user_input[CONF_NAME]
                updated[CONF_HOST] = new_host
                updated[CONF_PORT] = new_port
                updated[CONF_SERIAL_PORT] = serial_port
                updated[CONF_SLAVE_ID] = slave_id
                updated[CONF_BATTERY_VERSION] = battery_version
                updated["ems_version"] = getattr(
                    self, "_last_marstek_ems_version", None
                )
                self._reconfigure_batteries.append(updated)
                self.battery_index += 1

                if self.battery_index >= len(current_batteries):
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={"batteries": self._reconfigure_batteries},
                    )
                return await self.async_step_reconfigure_battery()

        defaults = {
            CONF_NAME: current.get(CONF_NAME, f"Marstek Venus {battery_num}"),
            CONF_HOST: current.get(CONF_HOST, ""),
            CONF_PORT: current.get(CONF_PORT, 502),
            CONF_SERIAL_PORT: current.get(CONF_SERIAL_PORT, ""),
            CONF_SLAVE_ID: current.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID),
            CONF_BATTERY_VERSION: current.get(CONF_BATTERY_VERSION, DEFAULT_VERSION),
        }
        # A serial battery stores its path in CONF_HOST too; don't prefill the IP
        # field with the device path.
        host_default = "" if defaults[CONF_SERIAL_PORT] else defaults[CONF_HOST]

        return self.async_show_form(
            step_id="reconfigure_battery",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                    vol.Optional(CONF_HOST, default=host_default): str,
                    vol.Optional(CONF_PORT, default=defaults[CONF_PORT]): int,
                    vol.Optional(CONF_SERIAL_PORT, default=defaults[CONF_SERIAL_PORT]): str,
                    vol.Required(CONF_SLAVE_ID, default=defaults[CONF_SLAVE_ID]):
                        vol.All(NumberSelector(NumberSelectorConfig(min=1, max=247, step=1, mode=NumberSelectorMode.BOX)), vol.Coerce(int)),
                    vol.Required(
                        CONF_BATTERY_VERSION, default=defaults[CONF_BATTERY_VERSION]
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": "v2", "label": "Ev2"},
                                {"value": "v3", "label": "Ev3"},
                                {"value": "vA", "label": "A"},
                                {"value": "vD", "label": "D"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_reconfigure_battery_zendure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Update connection settings for a Zendure battery during reconfiguration."""
        entry = self._get_reconfigure_entry()
        current_batteries = entry.data.get("batteries", [])
        battery_num = self.battery_index + 1
        current = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )
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
                    self._migrate_battery_registry_ids(
                        entry, old_host, old_port, new_host, new_port
                    )

                updated = dict(current)
                updated[CONF_NAME] = user_input[CONF_NAME]
                updated[CONF_HOST] = new_host
                updated[CONF_PORT] = new_port
                updated["zendure_model"] = _detect_zendure_model(product)
                self._reconfigure_batteries.append(updated)
                self.battery_index += 1

                if self.battery_index >= len(current_batteries):
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={"batteries": self._reconfigure_batteries},
                    )
                return await self.async_step_reconfigure_battery()

        defaults = {
            CONF_NAME: current.get(CONF_NAME, f"Zendure SolarFlow {battery_num}"),
            CONF_HOST: current.get(CONF_HOST, ""),
            CONF_PORT: current.get(CONF_PORT, 80),
        }

        return self.async_show_form(
            step_id="reconfigure_battery_zendure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                    vol.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
                    vol.Required(CONF_PORT, default=defaults[CONF_PORT]): int,
                }
            ),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_reconfigure_battery_sessy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Update connection settings for a Sessy battery during reconfiguration."""
        entry = self._get_reconfigure_entry()
        current_batteries = entry.data.get("batteries", [])
        battery_num = self.battery_index + 1
        current = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )
        errors = {}

        if user_input is not None:
            new_host = user_input[CONF_HOST]
            new_port = int(user_input.get(CONF_PORT, 80))
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            _LOGGER.info("Probing Sessy device at %s:%s", new_host, new_port)
            if not await SessyLocalDriver.probe(
                new_host, new_port, username, password
            ):
                errors["base"] = "cannot_connect"
            else:
                old_host = current.get(CONF_HOST)
                old_port = current.get(CONF_PORT)
                if old_host and old_port and (old_host != new_host or old_port != new_port):
                    self._migrate_battery_registry_ids(
                        entry, old_host, old_port, new_host, new_port
                    )

                updated = dict(current)
                updated.update({
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_HOST: new_host,
                    CONF_PORT: new_port,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                    "brand": "sessy",
                })
                self._reconfigure_batteries.append(updated)
                self.battery_index += 1
                if self.battery_index >= len(current_batteries):
                    return self.async_update_reload_and_abort(
                        entry, data_updates={"batteries": self._reconfigure_batteries}
                    )
                return await self.async_step_reconfigure_battery()

        return self.async_show_form(
            step_id="reconfigure_battery_sessy",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Sessy {battery_num}")): str,
                vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=current.get(CONF_PORT, 80)): int,
                vol.Required(
                    CONF_USERNAME, default=current.get(CONF_USERNAME, "")
                ): str,
                vol.Required(
                    CONF_PASSWORD, default=current.get(CONF_PASSWORD, "")
                ): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_reconfigure_battery_esphome(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Update the ESPHome bridge device for a battery during reconfiguration."""
        entry = self._get_reconfigure_entry()
        current_batteries = entry.data.get("batteries", [])
        battery_num = self.battery_index + 1
        current = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )
        errors = {}
        placeholders: dict[str, str] = {"battery_num": str(battery_num)}

        if user_input is not None:
            device_id = user_input["esphome_device"]
            _, missing = EsphomeEntityDriver.resolve(self.hass, device_id)
            if missing:
                errors["base"] = "esphome_entities_missing"
                placeholders["missing"] = ", ".join(missing)
            else:
                old_host = current.get(CONF_HOST)
                old_port = current.get(CONF_PORT)
                if old_host and old_host != device_id:
                    self._migrate_battery_registry_ids(
                        entry, old_host, old_port, device_id, 0
                    )

                updated = dict(current)
                updated[CONF_NAME] = user_input[CONF_NAME]
                updated[CONF_HOST] = device_id
                updated[CONF_PORT] = 0
                updated["esphome_device_id"] = device_id
                self._reconfigure_batteries.append(updated)
                self.battery_index += 1

                if self.battery_index >= len(current_batteries):
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={"batteries": self._reconfigure_batteries},
                    )
                return await self.async_step_reconfigure_battery()

        schema: dict = {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Marstek Venus {battery_num}")): str,
        }
        current_device = current.get("esphome_device_id")
        if current_device:
            schema[vol.Required("esphome_device", default=current_device)] = DeviceSelector(
                DeviceSelectorConfig(integration="esphome")
            )
        else:
            schema[vol.Required("esphome_device")] = DeviceSelector(
                DeviceSelectorConfig(integration="esphome")
            )

        return self.async_show_form(
            step_id="reconfigure_battery_esphome",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders=placeholders,
        )


    async def async_step_reconfigure_battery_anker(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Update connection settings for an Anker battery during reconfiguration."""
        entry = self._get_reconfigure_entry()
        current_batteries = entry.data.get("batteries", [])
        battery_num = self.battery_index + 1
        current = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )
        errors = {}

        if user_input is not None:
            new_host = user_input[CONF_HOST]
            new_port = int(user_input.get(CONF_PORT, 502))
            slave_id = int(user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID))
            ok, _ = await _validate_anker_connection(
                self.hass,
                entry.entry_id,
                new_host,
                new_port,
                slave_id,
            )
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                old_host = current.get(CONF_HOST)
                old_port = current.get(CONF_PORT)

                if old_host and old_port and (old_host != new_host or old_port != new_port):
                    self._migrate_battery_registry_ids(
                        entry, old_host, old_port, new_host, new_port
                    )

                updated = dict(current)
                updated[CONF_NAME] = user_input[CONF_NAME]
                updated[CONF_HOST] = new_host
                updated[CONF_PORT] = new_port
                updated[CONF_SLAVE_ID] = slave_id
                updated["brand"] = "anker"
                self._reconfigure_batteries.append(updated)
                self.battery_index += 1

                if self.battery_index >= len(current_batteries):
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={"batteries": self._reconfigure_batteries},
                    )
                return await self.async_step_reconfigure_battery()

        defaults = {
            CONF_NAME: current.get(CONF_NAME, f"Anker Solarbank {battery_num}"),
            CONF_HOST: current.get(CONF_HOST, ""),
            CONF_PORT: current.get(CONF_PORT, 502),
            CONF_SLAVE_ID: current.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID),
        }

        return self.async_show_form(
            step_id="reconfigure_battery_anker",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                    vol.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
                    vol.Required(CONF_PORT, default=defaults[CONF_PORT]): int,
                    vol.Required(CONF_SLAVE_ID, default=defaults[CONF_SLAVE_ID]):
                        vol.All(NumberSelector(NumberSelectorConfig(min=1, max=247, step=1, mode=NumberSelectorMode.BOX)), vol.Coerce(int)),
                }
            ),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_reconfigure_battery_huawei(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Update a Huawei battery's connection without asking Marstek questions.

        Without this branch the reconfigure flow offered the Marstek form and
        probe: a battery version, a slave id meaning something else, and no way
        to reach the fields this brand actually needs.
        """
        entry = self._get_reconfigure_entry()
        current = entry.data.get("batteries", [])[self.battery_index]
        errors: dict[str, str] = {}

        if user_input is not None:
            host = (user_input[CONF_HOST] or "").strip()
            port = int(user_input.get(CONF_PORT, 502))
            raw_slave = user_input.get(CONF_SLAVE_ID)
            slave_id = None if raw_slave in (None, "") else int(raw_slave)
            device_id = user_input.get("huawei_battery_device") or ""
            direct_write = bool(user_input.get("huawei_direct_write", False))

            if not direct_write and not device_id:
                errors["huawei_battery_device"] = (
                    "huawei_device_required"
                    if self.hass.config_entries.async_entries(HUAWEI_SOLAR_DOMAIN)
                    else "huawei_solar_missing"
                )
            else:
                found = slave_id
                cascade = []
                if found is None:
                    candidates = await HuaweiSolarDriver.scan_slave_ids(self.hass, host, port)
                    with_battery = [candidate for candidate in candidates if candidate[2]]
                    if len(with_battery) > 1:
                        # A cascade is not a failure to connect; only the user
                        # can say which inverter this battery belongs to.
                        cascade = with_battery
                    elif with_battery:
                        found = with_battery[0][0]
                ok, model, max_charge, max_discharge, serial, inverter_max = (
                    await HuaweiSolarDriver.probe(self.hass, host, port, found)
                    if found is not None
                    else (False, None, None, None, None, None)
                )
                if cascade:
                    self._huawei_candidates = cascade
                    self._huawei_pending = {
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_HOST: host,
                        CONF_PORT: port,
                        "huawei_battery_device_id": device_id,
                        "huawei_direct_write": direct_write,
                    }
                    self._huawei_reconfigure_current = current
                    return await self.async_step_reconfigure_battery_huawei_slave()
                if not ok:
                    errors["base"] = "cannot_connect"
                elif not _huawei_device_matches_inverter(
                    self.hass, device_id, serial, found
                ):
                    # Same check the setup flow makes: telemetry and commands
                    # must reach the same inverter, which a cascade can break.
                    errors["huawei_battery_device"] = "huawei_device_mismatch"
                else:
                    return await self._huawei_reconfigure_store(
                        entry, current, user_input, host, port, found,
                        model, max_charge, max_discharge, inverter_max,
                    )
        return self.async_show_form(
            step_id="reconfigure_battery_huawei",
            data_schema=self._huawei_schema(current, self.battery_index + 1),
            errors=errors,
            description_placeholders={"battery_num": str(self.battery_index + 1)},
        )

    async def async_step_reconfigure_battery_huawei_slave(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick which inverter of a cascade this battery belongs to."""
        candidates = getattr(self, "_huawei_candidates", [])
        if user_input is not None:
            entry = self._get_reconfigure_entry()
            current = self._huawei_reconfigure_current
            pending = self._huawei_pending
            slave_id = int(user_input[CONF_SLAVE_ID])
            ok, model, max_charge, max_discharge, serial, inverter_max = (
                await HuaweiSolarDriver.probe(
                    self.hass, pending[CONF_HOST], pending[CONF_PORT], slave_id
                )
            )
            if ok:
                if not _huawei_device_matches_inverter(
                    self.hass,
                    pending.get("huawei_battery_device_id"),
                    serial,
                    slave_id,
                ):
                    # The selected device is not editable on the cascade form;
                    # return to the Huawei form so the user can choose the
                    # matching device or switch to direct writes.
                    return self.async_show_form(
                        step_id="reconfigure_battery_huawei",
                        data_schema=self._huawei_schema(
                            current, self.battery_index + 1
                        ),
                        errors={"huawei_battery_device": "huawei_device_mismatch"},
                        description_placeholders={
                            "battery_num": str(self.battery_index + 1)
                        },
                    )
                return await self._huawei_reconfigure_store(
                    entry, current, pending, pending[CONF_HOST], pending[CONF_PORT],
                    slave_id, model, max_charge, max_discharge, inverter_max,
                )
            return self.async_show_form(
                step_id="reconfigure_battery_huawei_slave",
                data_schema=self._huawei_slave_schema(candidates),
                errors={"base": "cannot_connect"},
                description_placeholders={"count": str(len(candidates))},
            )
        return self.async_show_form(
            step_id="reconfigure_battery_huawei_slave",
            data_schema=self._huawei_slave_schema(candidates),
            description_placeholders={"count": str(len(candidates))},
        )

    async def _huawei_reconfigure_store(
        self, entry, current, values, host, port, slave_id,
        model, max_charge, max_discharge, inverter_max,
    ) -> FlowResult:
        """Write the updated battery back, carrying its history with it."""
        old_host = current.get(CONF_HOST)
        old_port = current.get(CONF_PORT, 502)
        old_slave = current.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
        # The slave id is part of a battery's identity, so changing it renames
        # every entity and the device itself unless the registry follows.
        if old_host and (old_host != host or old_port != port or old_slave != slave_id):
            self._migrate_battery_registry_ids(
                entry, old_host, old_port, host, port, old_slave, slave_id
            )

        device_id = (
            values.get("huawei_battery_device_id")
            or values.get("huawei_battery_device")
            or ""
        )
        updated = dict(current)
        updated.update({
            CONF_NAME: values[CONF_NAME],
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_SLAVE_ID: slave_id,
            "brand": "huawei",
            "huawei_battery_device_id": device_id,
            "huawei_direct_write": bool(values.get("huawei_direct_write", False)),
            "huawei_model": model,
        })
        if max_charge:
            updated["device_max_charge_power"] = int(max_charge)
        if max_discharge:
            updated["device_max_discharge_power"] = int(max_discharge)
        if inverter_max:
            updated["device_inverter_max_power"] = int(inverter_max)

        # Re-detect rather than carry over: pointed at a different endpoint, a
        # remembered unit id would have the driver reading a grid meter that is
        # not there, or worse, some other device answering on that id.
        emma = await HuaweiSolarDriver.find_emma_slave_id(self.hass, host, port)
        if emma is not None:
            updated["huawei_emma_slave_id"] = emma
        else:
            updated.pop("huawei_emma_slave_id", None)

        self._reconfigure_batteries.append(updated)
        self.battery_index += 1
        if self.battery_index >= len(entry.data.get("batteries", [])):
            return self.async_update_reload_and_abort(
                entry, data_updates={"batteries": self._reconfigure_batteries}
            )
        return await self.async_step_reconfigure_battery()

    async def async_step_reconfigure_battery_hoymiles(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Update the MQTT device id without asking for broker credentials."""
        entry = self._get_reconfigure_entry()
        current = entry.data.get("batteries", [])[self.battery_index]
        errors = {}
        if user_input is not None:
            device_id = user_input["device_id"].strip()
            ok, caps = await HoymilesMqttDriver.probe(
                self.hass,
                device_id,
                model_hint=_hoymiles_model_hint(user_input),
            )
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                old_host, old_port = current.get(CONF_HOST), current.get(CONF_PORT, 0)
                if old_host and old_host != device_id:
                    self._migrate_battery_registry_ids(entry, old_host, old_port, device_id, 0)
                updated = dict(current)
                updated.update({CONF_NAME: user_input[CONF_NAME], CONF_HOST: device_id, CONF_PORT: 0,
                                "device_id": device_id, "brand": "hoymiles"})
                _hoymiles_apply_probe_caps(
                    updated, caps, upgrade_legacy_defaults=True
                )
                self._reconfigure_batteries.append(updated)
                self.battery_index += 1
                if self.battery_index >= len(entry.data.get("batteries", [])):
                    return self.async_update_reload_and_abort(entry, data_updates={"batteries": self._reconfigure_batteries})
                return await self.async_step_reconfigure_battery()
        model_field, model_selector = _hoymiles_model_selector(
            current.get("hoymiles_model", _HOYMILES_MODEL_AUTO)
        )
        return self.async_show_form(step_id="reconfigure_battery_hoymiles", data_schema=vol.Schema({
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, "Hoymiles")): str,
            vol.Required("device_id", default=current.get("device_id", current.get(CONF_HOST, ""))): str,
            model_field: model_selector,
        }), errors=errors, description_placeholders={"battery_num": str(self.battery_index + 1)})

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
        """Open a connection to ``host`` using this battery's stored settings."""
        return await self._test_connection(
            host,
            battery.get(CONF_PORT),
            battery.get(CONF_BATTERY_VERSION, DEFAULT_VERSION),
            battery.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID),
            brand=battery.get("brand", "marstek"),
            username=battery.get(CONF_USERNAME, ""),
            password=battery.get(CONF_PASSWORD, ""),
        )

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


    async def _test_connection(
        self,
        host: str,
        port: int,
        version: str = "v2",
        slave_id: int = DEFAULT_SLAVE_ID,
        brand: str = "marstek",
        serial_port: str | None = None,
    ) -> bool:
        """Test connection to a battery.

        For Zendure: simple HTTP probe (no single-slot constraint).
        For Marstek: temporarily closes any existing coordinator connection to
        free the single Modbus TCP slot (or the serial port), probes, then
        reconnects. ``serial_port`` probes over Modbus RTU instead of TCP (#350).
        """
        self._last_marstek_ems_version = None
        if brand == "zendure":
            _LOGGER.info("Probing Zendure device at %s:%s", host, port)
            ok, _ = await ZendureLocalDriver.probe(host, port)
            return ok

        if brand == "anker":
            _LOGGER.info("Probing Anker Solarbank at %s:%s slave %s", host, port, slave_id)
            ok, _ = await AnkerModbusDriver.probe(host, port, slave_id)
            return ok

        # Marstek: handle single-connection-slot constraint.
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        coordinators = entry_data.get("coordinators", [])
        existing_coordinator = None
        for coordinator in coordinators:
            if coordinator.host == host and coordinator.slave_id == slave_id:
                existing_coordinator = coordinator
                break

        if existing_coordinator is not None:
            _LOGGER.info(
                "Reusing coordinator for %s (version=%s) - closing connection for test",
                host, existing_coordinator.battery_version
            )
            async with existing_coordinator.lock:
                await existing_coordinator.driver.close()
                await asyncio.sleep(0.5)
                if version == "vD":
                    result, self._last_marstek_ems_version = await MarstekModbusDriver.probe_details(
                        host, port, version, slave_id, serial_port=serial_port
                    )
                else:
                    result = await MarstekModbusDriver.probe(
                        host, port, version, slave_id, serial_port=serial_port
                    )
                await asyncio.sleep(0.3)
                await existing_coordinator.driver.connect()
                if result:
                    _LOGGER.info("Test connection to %s successful, coordinator reconnected", host)
                else:
                    _LOGGER.warning("Test connection to %s failed after closing coordinator", host)
                return result
        else:
            _LOGGER.info("No existing coordinator for %s - opening new connection", host)
            if version == "vD":
                result, self._last_marstek_ems_version = await MarstekModbusDriver.probe_details(
                    host, port, version, slave_id, serial_port=serial_port
                )
            else:
                result = await MarstekModbusDriver.probe(
                    host, port, version, slave_id, serial_port=serial_port
                )
            return result

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
            await self.hass.config_entries.async_reload(entry_id)
        finally:
            clear_reload_pending(self.hass, entry_id)
        return self.async_create_entry(title="", data={})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show menu to select which section to configure."""
        # Weekly full charge, charge delay, temperature charge limit, capacity
        # protection, hourly balance and the PD controller are configured live
        # from the dashboard entities, not here.
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "sensors",
                "batteries",
                "time_slots",
                "excluded_devices",
                "predictive_charging",
            ],
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
                                min=1000, max=20000, step=100, mode=NumberSelectorMode.BOX
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
            step_id="batteries",
            data_schema=_battery_count_schema(current_batteries),
        )

    async def async_step_battery_brand(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select battery brand for the current battery slot."""
        battery_num = self.battery_index + 1
        current_batteries = self.config_entry.data.get("batteries", [])
        current_brand = (
            current_batteries[self.battery_index].get("brand", "marstek")
            if self.battery_index < len(current_batteries)
            else "marstek"
        )

        if user_input is not None:
            brand = user_input["brand"]
            self._current_battery_data = {"brand": brand}
            if brand == "zendure":
                return await self.async_step_battery_connection_zendure()
            if brand == "esphome":
                return await self.async_step_battery_connection_esphome()
            if brand == "anker":
                return await self.async_step_battery_connection_anker()
            if brand == "sessy":
                return await self.async_step_battery_connection_sessy()
            if brand == "huawei":
                return await self.async_step_battery_connection_huawei()
            if brand == "hoymiles":
                return await self.async_step_battery_connection_hoymiles()
            return await self.async_step_battery_connection()

        return self.async_show_form(
            step_id="battery_brand",
            data_schema=vol.Schema(
                {
                    vol.Required("brand", default=current_brand):
                        SelectSelector(SelectSelectorConfig(
                            options=[
                                {"value": "marstek", "label": "Marstek Venus"},
                                {"value": "zendure", "label": "Zendure SolarFlow"},
                                {"value": "esphome", "label": "Marstek via LilyGo RS485 (ESPHome)"},
                                {"value": "anker", "label": "Anker SOLIX Solarbank Max AC / 4 E5000 Pro"},
                                {"value": "sessy", "label": "Sessy"},
                                {"value": "hoymiles", "label": "Hoymiles MQTT"},
                                {"value": "huawei", "label": "Huawei SUN2000 + LUNA2000"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )),
                }
            ),
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_connection(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure connection details for a Marstek battery."""
        errors = {}

        try:
            battery_num = self.battery_index + 1
            current_batteries = self.config_entry.data.get("batteries", [])

            if user_input is not None:
                battery_version = user_input.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
                slave_id = user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID)
                serial_port = (user_input.get(CONF_SERIAL_PORT) or "").strip()
                host = (user_input.get(CONF_HOST) or "").strip()
                is_serial = bool(serial_port)

                if is_serial:
                    # Serial has no IP:port; the path doubles as identity (see add flow).
                    host = serial_port
                    port = user_input.get(CONF_PORT, 502)
                else:
                    port = user_input.get(CONF_PORT, 502)

                if not is_serial and not host:
                    errors["base"] = "host_or_serial_required"
                elif not await self._test_connection(
                    host, port, battery_version, slave_id,
                    brand="marstek", serial_port=serial_port or None,
                ):
                    errors["base"] = "cannot_connect"
                else:
                    self._current_battery_data.update({
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_SERIAL_PORT: serial_port,
                        CONF_SLAVE_ID: slave_id,
                        CONF_BATTERY_VERSION: battery_version,
                        "brand": "marstek",
                        "ems_version": getattr(self, "_last_marstek_ems_version", None),
                    })
                    return await self.async_step_battery_limits()

            if self.battery_index < len(current_batteries):
                current_battery = current_batteries[self.battery_index]
                defaults = {
                    CONF_NAME: current_battery.get(CONF_NAME, f"Marstek Venus {battery_num}"),
                    CONF_HOST: current_battery.get(CONF_HOST, ""),
                    CONF_PORT: current_battery.get(CONF_PORT, 502),
                    CONF_SERIAL_PORT: current_battery.get(CONF_SERIAL_PORT, ""),
                    CONF_SLAVE_ID: current_battery.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID),
                    CONF_BATTERY_VERSION: current_battery.get(CONF_BATTERY_VERSION, DEFAULT_VERSION),
                }
            else:
                defaults = {
                    CONF_NAME: f"Marstek Venus {battery_num}",
                    CONF_HOST: "",
                    CONF_PORT: 502,
                    CONF_SERIAL_PORT: "",
                    CONF_SLAVE_ID: DEFAULT_SLAVE_ID,
                    CONF_BATTERY_VERSION: DEFAULT_VERSION,
                }
        except Exception as e:
            _LOGGER.error("Error in options flow battery_connection step: %s", e, exc_info=True)
            return self.async_abort(reason="unknown_error")

        # A serial battery stores its path in CONF_HOST too; don't prefill the IP
        # field with the device path.
        host_default = "" if defaults[CONF_SERIAL_PORT] else defaults[CONF_HOST]

        return self.async_show_form(
            step_id="battery_connection",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                    vol.Optional(CONF_HOST, default=host_default): str,
                    vol.Optional(CONF_PORT, default=defaults[CONF_PORT]): int,
                    vol.Optional(CONF_SERIAL_PORT, default=defaults[CONF_SERIAL_PORT]): str,
                    vol.Required(CONF_SLAVE_ID, default=defaults[CONF_SLAVE_ID]):
                        vol.All(NumberSelector(NumberSelectorConfig(min=1, max=247, step=1, mode=NumberSelectorMode.BOX)), vol.Coerce(int)),
                    vol.Required(CONF_BATTERY_VERSION, default=defaults[CONF_BATTERY_VERSION]):
                        SelectSelector(SelectSelectorConfig(
                            options=[
                                {"value": "v2", "label": "Ev2"},
                                {"value": "v3", "label": "Ev3"},
                                {"value": "vA", "label": "A"},
                                {"value": "vD", "label": "D"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )),
                }
            ),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_connection_zendure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure connection details for a Zendure SolarFlow device."""
        errors = {}

        try:
            battery_num = self.battery_index + 1
            current_batteries = self.config_entry.data.get("batteries", [])

            if user_input is not None:
                host = user_input[CONF_HOST]
                port = int(user_input.get(CONF_PORT, 80))
                ok, product = await ZendureLocalDriver.probe(host, port)
                if not ok:
                    errors["base"] = "cannot_connect"
                else:
                    self._current_battery_data.update({
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_HOST: host,
                        CONF_PORT: port,
                        "brand": "zendure",
                        "zendure_model": _detect_zendure_model(product),
                    })
                    return await self.async_step_battery_limits()

            if self.battery_index < len(current_batteries):
                current_battery = current_batteries[self.battery_index]
                defaults = {
                    CONF_NAME: current_battery.get(CONF_NAME, f"Zendure SolarFlow {battery_num}"),
                    CONF_HOST: current_battery.get(CONF_HOST, ""),
                    CONF_PORT: current_battery.get(CONF_PORT, 80),
                }
            else:
                defaults = {
                    CONF_NAME: f"Zendure SolarFlow {battery_num}",
                    CONF_HOST: "",
                    CONF_PORT: 80,
                }
        except Exception as e:
            _LOGGER.error("Error in options flow battery_connection_zendure step: %s", e, exc_info=True)
            return self.async_abort(reason="unknown_error")

        return self.async_show_form(
            step_id="battery_connection_zendure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                    vol.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
                    vol.Optional(CONF_PORT, default=defaults[CONF_PORT]): int,
                }
            ),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_connection_sessy(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure a Sessy through its local dongle HTTP API."""
        errors = {}
        battery_num = self.battery_index + 1
        current_batteries = self.config_entry.data.get("batteries", [])
        current_battery = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input.get(CONF_PORT, 80))
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            _LOGGER.info("Probing Sessy device at %s:%s", host, port)
            if await SessyLocalDriver.probe(host, port, username, password):
                self._current_battery_data.update({
                    CONF_NAME: user_input[CONF_NAME],
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                    "brand": "sessy",
                })
                return await self.async_step_battery_limits()
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="battery_connection_sessy",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_NAME,
                    default=current_battery.get(CONF_NAME, f"Sessy {battery_num}"),
                ): str,
                vol.Required(CONF_HOST, default=current_battery.get(CONF_HOST, "")): str,
                vol.Optional(CONF_PORT, default=current_battery.get(CONF_PORT, 80)): int,
                vol.Required(
                    CONF_USERNAME,
                    default=current_battery.get(CONF_USERNAME, ""),
                ): str,
                vol.Required(
                    CONF_PASSWORD,
                    default=current_battery.get(CONF_PASSWORD, ""),
                ): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_connection_esphome(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Pick the LilyGo/ESPHome device bridging a Marstek battery."""
        errors = {}
        battery_num = self.battery_index + 1
        placeholders: dict[str, str] = {"battery_num": str(battery_num)}

        try:
            current_batteries = self.config_entry.data.get("batteries", [])

            if user_input is not None:
                device_id = user_input["esphome_device"]
                _, missing = EsphomeEntityDriver.resolve(self.hass, device_id)
                if missing:
                    errors["base"] = "esphome_entities_missing"
                    placeholders["missing"] = ", ".join(missing)
                else:
                    self._current_battery_data.update({
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_HOST: device_id,
                        CONF_PORT: 0,
                        "brand": "esphome",
                        "esphome_device_id": device_id,
                    })
                    return await self.async_step_battery_limits()

            if self.battery_index < len(current_batteries):
                current_battery = current_batteries[self.battery_index]
                defaults = {
                    CONF_NAME: current_battery.get(CONF_NAME, f"Marstek Venus {battery_num}"),
                    "esphome_device": current_battery.get("esphome_device_id"),
                }
            else:
                defaults = {
                    CONF_NAME: f"Marstek Venus {battery_num}",
                    "esphome_device": None,
                }
        except Exception as e:
            _LOGGER.error("Error in options flow battery_connection_esphome step: %s", e, exc_info=True)
            return self.async_abort(reason="unknown_error")

        device_selector = DeviceSelector(DeviceSelectorConfig(integration="esphome"))
        schema: dict = {vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str}
        if defaults["esphome_device"]:
            schema[vol.Required("esphome_device", default=defaults["esphome_device"])] = device_selector
        else:
            schema[vol.Required("esphome_device")] = device_selector

        return self.async_show_form(
            step_id="battery_connection_esphome",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders=placeholders,
        )


    async def async_step_battery_connection_huawei(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure a Huawei SUN2000 + LUNA2000.

        Control takes one of two paths. By default set-points go through the
        Huawei Solar integration's services, which address the battery by
        device. With direct Modbus writes the driver addresses the inverter
        itself and needs nothing from that integration — which is why the device
        field is optional and only checked when it is actually used.
        """
        errors = {}
        battery_num = self.battery_index + 1
        entry = getattr(self, "config_entry", None)
        current_batteries = entry.data.get("batteries", []) if entry else []
        current_battery = (
            current_batteries[self.battery_index]
            if self.battery_index < len(current_batteries)
            else {}
        )

        if user_input is not None:
            host = (user_input[CONF_HOST] or "").strip()
            port = int(user_input.get(CONF_PORT, 502))
            raw_slave = user_input.get(CONF_SLAVE_ID)
            slave_id = None if raw_slave in (None, "") else int(raw_slave)
            device_id = user_input.get("huawei_battery_device") or ""
            direct_write = bool(user_input.get("huawei_direct_write", False))
            self._huawei_pending = {
                CONF_NAME: user_input[CONF_NAME],
                CONF_HOST: host,
                CONF_PORT: port,
                "huawei_battery_device_id": device_id,
                "huawei_direct_write": direct_write,
            }
            if not direct_write and not device_id:
                # Without direct writes every set-point is a service call, and
                # those address the battery by device. A missing integration is
                # a different problem than an unanswered question, so say which.
                errors["huawei_battery_device"] = (
                    "huawei_device_required"
                    if self.hass.config_entries.async_entries(HUAWEI_SOLAR_DOMAIN)
                    else "huawei_solar_missing"
                )
            elif slave_id is not None:
                _LOGGER.info(
                    "Probing Huawei inverter at %s:%s (slave %s)", host, port, slave_id
                )
                ok, model, max_charge, max_discharge, serial, inverter_max = await HuaweiSolarDriver.probe(
                    self.hass, host, port, slave_id
                )
                if ok:
                    result = await self._huawei_store(
                        slave_id, model, max_charge, max_discharge, serial,
                        inverter_max, errors,
                    )
                    if result is not None:
                        return result
                else:
                    # An id that does not answer is a guess worth replacing, not
                    # a reason to send the user away.
                    result = await self._huawei_search(host, port, errors)
                    if result is not None:
                        return result
            else:
                result = await self._huawei_search(host, port, errors)
                if result is not None:
                    return result

        return self.async_show_form(
            step_id="battery_connection_huawei",
            data_schema=self._huawei_schema(current_battery, battery_num),
            errors=errors,
            description_placeholders={
                "battery_num": str(battery_num),
                "proxy_url": _MODBUS_PROXY_URL,
            },
        )

    async def _huawei_search(self, host: str, port: int, errors: dict) -> FlowResult | None:
        """Look for inverters on the bus; returns None when nothing usable was found.

        One match is taken straight away. Several mean a cascade, which only the
        user can resolve — a battery belongs to one of them.
        """
        _LOGGER.info("Scanning %s:%s for Huawei inverters", host, port)
        found = await HuaweiSolarDriver.scan_slave_ids(self.hass, host, port)
        with_battery = [candidate for candidate in found if candidate[2]]
        if len(with_battery) == 1:
            sid = with_battery[0][0]
            ok, model, max_charge, max_discharge, serial, inverter_max = await HuaweiSolarDriver.probe(
                self.hass, host, port, sid
            )
            if ok:
                _LOGGER.info("Found a Huawei battery on slave %s", sid)
                # A mismatch leaves its own error behind and must not be
                # papered over by the scan verdict below.
                return await self._huawei_store(
                    sid, model, max_charge, max_discharge, serial, inverter_max, errors
                )
        if len(with_battery) > 1:
            self._huawei_candidates = with_battery
            return await self.async_step_battery_connection_huawei_slave()
        # A reachable inverter with no SOC means no battery is attached, which is
        # a different mistake than an unreachable address.
        errors["base"] = "no_battery" if found else "cannot_connect"
        return None

    async def async_step_battery_connection_huawei_slave(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick which inverter on the bus this battery belongs to."""
        candidates = getattr(self, "_huawei_candidates", [])
        if user_input is not None:
            slave_id = int(user_input[CONF_SLAVE_ID])
            pending = self._huawei_pending
            ok, model, max_charge, max_discharge, serial, inverter_max = await HuaweiSolarDriver.probe(
                self.hass, pending[CONF_HOST], pending[CONF_PORT], slave_id
            )
            errors: dict[str, str] = {}
            if ok:
                result = await self._huawei_store(
                    slave_id, model, max_charge, max_discharge, serial, inverter_max,
                    errors, "base",
                )
                if result is not None:
                    return result
            return self.async_show_form(
                step_id="battery_connection_huawei_slave",
                data_schema=self._huawei_slave_schema(candidates),
                errors=errors or {"base": "cannot_connect"},
                description_placeholders={"count": str(len(candidates))},
            )

        return self.async_show_form(
            step_id="battery_connection_huawei_slave",
            data_schema=self._huawei_slave_schema(candidates),
            description_placeholders={"count": str(len(candidates))},
        )

    async def _huawei_store(
        self, slave_id, model, max_charge, max_discharge, serial, inverter_max,
        errors, error_key: str = "huawei_battery_device",
    ) -> FlowResult | None:
        """Commit a validated Huawei battery, or refuse a mismatched pairing.

        On the service path the battery is named twice over: once as a Modbus
        address and once as a device in the registry. Nothing forces those to be
        the same inverter, and on a cascade they easily are not — telemetry would
        then come from one unit while the commands went to another. The inverter
        serial shows up on both sides, so the pairing can be checked. Returns
        None with ``errors`` filled when it does not hold.
        """
        device_id = self._huawei_pending.get("huawei_battery_device_id")
        if not _huawei_device_matches_inverter(
            self.hass, device_id, serial, slave_id
        ):
            errors[error_key] = "huawei_device_mismatch"
            return None
        self._current_battery_data.update({
            **self._huawei_pending,
            CONF_SLAVE_ID: slave_id,
            "brand": "huawei",
            "huawei_model": model,
        })
        if max_charge:
            self._current_battery_data["device_max_charge_power"] = int(max_charge)
        if max_discharge:
            self._current_battery_data["device_max_discharge_power"] = int(max_discharge)
        if inverter_max:
            self._current_battery_data["device_inverter_max_power"] = int(inverter_max)
        # An EMMA on the same bus carries the installation's grid meter. Finding
        # it here means a user with one gets a grid reading fast enough to
        # control against without configuring anything.
        emma = await HuaweiSolarDriver.find_emma_slave_id(
            self.hass, self._huawei_pending[CONF_HOST], self._huawei_pending[CONF_PORT]
        )
        if emma is not None:
            self._current_battery_data["huawei_emma_slave_id"] = emma
        # What the battery reports today is the sensible starting value; the
        # form's ceiling is wider, because adding a pack raises it.
        for key, probed in (
            ("max_charge_power", max_charge),
            ("max_discharge_power", max_discharge),
        ):
            if probed and not self._current_battery_data.get(key):
                self._current_battery_data[key] = int(probed)
        return await self.async_step_battery_limits()

    @staticmethod
    def _huawei_slave_schema(candidates: list) -> vol.Schema:
        return vol.Schema({
            vol.Required(CONF_SLAVE_ID, default=str(candidates[0][0]) if candidates else "1"):
                SelectSelector(SelectSelectorConfig(
                    options=[
                        {"value": str(sid), "label": f"{model} — Slave {sid}"}
                        for sid, model, _battery in candidates
                    ],
                    mode=SelectSelectorMode.LIST,
                )),
        })

    @staticmethod
    def _huawei_schema(current_battery: dict, battery_num: int) -> vol.Schema:
        """Form for a Huawei battery.

        The battery device is optional because it is only needed for the service
        control path; with direct writes the driver addresses the inverter over
        Modbus and needs nothing from huawei_solar. Requiring it there would make
        the form impossible to submit on an installation that does not run that
        integration at all. Which of the two is missing is checked on submit.
        """
        return vol.Schema({
            vol.Required(
                CONF_NAME,
                default=current_battery.get(CONF_NAME, f"Huawei LUNA2000 {battery_num}"),
            ): str,
            vol.Required(CONF_HOST, default=current_battery.get(CONF_HOST, "")): str,
            vol.Optional(CONF_PORT, default=current_battery.get(CONF_PORT, 502)): int,
            # No default: an empty field means "go and find it". The id is not
            # derivable, so prefilling a guess would only invite the user to
            # accept a wrong one.
            # A selector, not a bare vol.Any: the frontend is handed a
            # serialised schema, and vol.Any has no serialised form — a form
            # containing one cannot be drawn at all.
            vol.Optional(
                CONF_SLAVE_ID,
                description={"suggested_value": current_battery.get(CONF_SLAVE_ID)},
            ): NumberSelector(
                NumberSelectorConfig(min=0, max=247, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Optional(
                "huawei_direct_write",
                default=current_battery.get("huawei_direct_write", False),
            ): bool,
            vol.Optional(
                "huawei_battery_device",
                description={
                    "suggested_value": current_battery.get("huawei_battery_device_id")
                },
            ): DeviceSelector(
                DeviceSelectorConfig(integration=HUAWEI_SOLAR_DOMAIN, model="Batteries")
            ),
        })

    async def async_step_battery_connection_hoymiles(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure a Hoymiles MQTT device id in the options flow."""
        errors = {}
        current_batteries = self.config_entry.data.get("batteries", [])
        current = current_batteries[self.battery_index] if self.battery_index < len(current_batteries) else {}
        if user_input is not None:
            device_id = user_input["device_id"].strip()
            ok, caps = await HoymilesMqttDriver.probe(
                self.hass,
                device_id,
                model_hint=_hoymiles_model_hint(user_input),
            )
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                self._current_battery_data.update({CONF_NAME: user_input[CONF_NAME], CONF_HOST: device_id,
                    CONF_PORT: 0, "device_id": device_id, "brand": "hoymiles"})
                _hoymiles_apply_probe_caps(
                    self._current_battery_data,
                    caps,
                    upgrade_legacy_defaults=bool(current),
                )
                return await self.async_step_battery_limits()
        model_field, model_selector = _hoymiles_model_selector(
            current.get("hoymiles_model", _HOYMILES_MODEL_AUTO)
        )
        return self.async_show_form(step_id="battery_connection_hoymiles", data_schema=vol.Schema({
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, f"Hoymiles {self.battery_index + 1}")): str,
            vol.Required("device_id", default=current.get("device_id", current.get(CONF_HOST, ""))): str,
            model_field: model_selector,
        }), errors=errors, description_placeholders={"battery_num": str(self.battery_index + 1)})

    async def async_step_battery_connection_anker(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure connection details for an Anker Solarbank Max AC / 4 E5000 Pro."""
        errors = {}

        try:
            battery_num = self.battery_index + 1
            current_batteries = self.config_entry.data.get("batteries", [])

            if user_input is not None:
                host = user_input[CONF_HOST]
                port = int(user_input.get(CONF_PORT, 502))
                slave_id = int(user_input.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID))
                ok, caps = await _validate_anker_connection(
                    self.hass,
                    self.config_entry.entry_id,
                    host,
                    port,
                    slave_id,
                )
                if not ok:
                    errors["base"] = "cannot_connect"
                else:
                    self._current_battery_data.update({
                        CONF_NAME: user_input[CONF_NAME],
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_SLAVE_ID: slave_id,
                        "brand": "anker",
                    })
                    _anker_apply_probe_caps(self._current_battery_data, caps)
                    return await self.async_step_battery_limits()

            if self.battery_index < len(current_batteries):
                current_battery = current_batteries[self.battery_index]
                defaults = {
                    CONF_NAME: current_battery.get(CONF_NAME, f"Anker Solarbank {battery_num}"),
                    CONF_HOST: current_battery.get(CONF_HOST, ""),
                    CONF_PORT: current_battery.get(CONF_PORT, 502),
                    CONF_SLAVE_ID: current_battery.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID),
                }
            else:
                defaults = {
                    CONF_NAME: f"Anker Solarbank {battery_num}",
                    CONF_HOST: "",
                    CONF_PORT: 502,
                    CONF_SLAVE_ID: DEFAULT_SLAVE_ID,
                }
        except Exception as e:
            _LOGGER.error("Error in options flow battery_connection_anker step: %s", e, exc_info=True)
            return self.async_abort(reason="unknown_error")

        return self.async_show_form(
            step_id="battery_connection_anker",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                    vol.Required(CONF_HOST, default=defaults[CONF_HOST]): str,
                    vol.Optional(CONF_PORT, default=defaults[CONF_PORT]): int,
                    vol.Required(CONF_SLAVE_ID, default=defaults[CONF_SLAVE_ID]):
                        vol.All(NumberSelector(NumberSelectorConfig(min=1, max=247, step=1, mode=NumberSelectorMode.BOX)), vol.Coerce(int)),
                }
            ),
            errors=errors,
            description_placeholders={"battery_num": str(battery_num)},
        )

    async def async_step_battery_limits(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure power and SOC limits for the current battery."""
        errors: dict[str, str] = {}
        try:
            battery_num = self.battery_index + 1
            brand = self._current_battery_data.get("brand", "marstek")
            if brand == "zendure":
                max_charge_power, max_discharge_power = _zendure_power_limits(
                    self._current_battery_data.get("zendure_model", "2400ac_pro")
                )
            elif brand == "anker":
                max_charge_power, max_discharge_power = _anker_power_ceilings(
                    self._current_battery_data
                )
            elif brand == "sessy":
                max_charge_power = _SESSY_MAX_CHARGE_POWER_W
                max_discharge_power = _SESSY_MAX_DISCHARGE_POWER_W
            elif brand == "hoymiles":
                max_charge_power, max_discharge_power = _hoymiles_power_ceilings(self._current_battery_data)
            elif brand == "huawei":
                max_charge_power, max_discharge_power = _huawei_power_ceilings(self._current_battery_data)
            else:
                battery_version = self._current_battery_data.get(CONF_BATTERY_VERSION, DEFAULT_VERSION)
                max_charge_power = max_discharge_power = max_power_for_battery_version(
                    battery_version, self._current_battery_data.get("ems_version")
                )
            (
                soc_min_lo,
                soc_min_hi,
                soc_min_default,
                soc_max_lo,
                soc_max_hi,
                soc_max_default,
            ) = _soc_selector_limits(brand)
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
                if brand == "anker":
                    charge_w, discharge_w = _anker_power_ceilings(merged)
                    merged["max_charge_power"] = charge_w
                    merged["max_discharge_power"] = discharge_w
                else:
                    merged["max_charge_power"] = int(user_input["max_charge_power"])
                    merged["max_discharge_power"] = int(user_input["max_discharge_power"])
                merged["max_soc"] = int(user_input["max_soc"])
                merged["min_soc"] = int(user_input["min_soc"])
                if self.config_entry.data.get(
                    CONF_THREE_PHASE_ENABLED,
                    self.config_data.get(CONF_THREE_PHASE_ENABLED, DEFAULT_THREE_PHASE_ENABLED),
                ):
                    merged[CONF_BATTERY_PHASE] = normalize_battery_phase(phase)
                _seed_software_power_limits(merged, brand)
                # Hysteresis is mandatory; floor the percent against SOC drift.
                merged["enable_charge_hysteresis"] = True
                merged["charge_hysteresis_percent"] = max(
                    MIN_CHARGE_HYSTERESIS_PERCENT,
                    int(user_input.get("charge_hysteresis_percent", DEFAULT_CHARGE_HYSTERESIS_PERCENT)),
                )
                merged["backup_offgrid_threshold"] = int(user_input.get("backup_offgrid_threshold", 50))
                merged[CONF_DC_PV_CONNECTED] = bool(
                    user_input.get(CONF_DC_PV_CONNECTED, True)
                ) if merged.get(CONF_BATTERY_VERSION) in ("vA", "vD") else False
                merged[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED] = (
                    False if brand in ("zendure", "anker", "sessy", "hoymiles", "huawei")
                    else user_input.get(CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED)
                )
                if brand in ("zendure", "sessy", "hoymiles"):
                    capacity_default = (
                        _hoymiles_capacity_default(self._current_battery_data)
                        if brand == "hoymiles" else 0.0
                    )
                    merged["battery_capacity_kwh"] = round(float(user_input.get("battery_capacity_kwh", capacity_default)), 2)
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
                defaults = {
                    "max_charge_power": min(current_battery.get("max_charge_power", max_charge_power), max_charge_power),
                    "max_discharge_power": min(current_battery.get("max_discharge_power", max_discharge_power), max_discharge_power),
                    "max_soc": current_battery.get("max_soc", soc_max_default),
                    "min_soc": current_battery.get("min_soc", soc_min_default),
                    "charge_hysteresis_percent": max(
                        MIN_CHARGE_HYSTERESIS_PERCENT,
                        int(current_battery.get("charge_hysteresis_percent", DEFAULT_CHARGE_HYSTERESIS_PERCENT)),
                    ),
                    "backup_offgrid_threshold": current_battery.get("backup_offgrid_threshold", 50),
                    CONF_DC_PV_CONNECTED: current_battery.get(
                        CONF_DC_PV_CONNECTED,
                        current_battery.get(CONF_BATTERY_VERSION) in ("vA", "vD"),
                    ),
                    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED: current_battery.get(
                        CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
                        DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
                    ),
                    "battery_capacity_kwh": current_battery.get(
                        "battery_capacity_kwh",
                        _hoymiles_capacity_default(self._current_battery_data)
                        if brand == "hoymiles" else 0.0,
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
                    "min_soc": soc_min_default,
                    "charge_hysteresis_percent": DEFAULT_CHARGE_HYSTERESIS_PERCENT,
                    "backup_offgrid_threshold": 50,
                    CONF_DC_PV_CONNECTED: self._current_battery_data.get(
                        CONF_BATTERY_VERSION
                    ) in ("vA", "vD"),
                    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED: DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
                    "battery_capacity_kwh": (
                        _hoymiles_capacity_default(self._current_battery_data)
                        if brand == "hoymiles" else 0.0
                    ),
                    CONF_BATTERY_PHASE: PHASE_UNASSIGNED,
                }
        except Exception as e:
            _LOGGER.error("Error in options flow battery_limits step: %s", e, exc_info=True)
            return self.async_abort(reason="unknown_error")

        _schema: dict = {}
        if brand != "anker":
            _schema[vol.Required("max_charge_power", default=defaults["max_charge_power"])] = NumberSelector(
                NumberSelectorConfig(min=100, max=max_charge_power, step=50, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)
            )
            _schema[vol.Required("max_discharge_power", default=defaults["max_discharge_power"])] = NumberSelector(
                NumberSelectorConfig(min=100, max=max_discharge_power, step=50, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)
            )
        _schema.update({
            vol.Required("max_soc", default=max(soc_max_lo, min(soc_max_hi, defaults["max_soc"]))):
                NumberSelector(NumberSelectorConfig(min=soc_max_lo, max=soc_max_hi, step=1, mode=NumberSelectorMode.SLIDER)),
            vol.Required("min_soc", default=max(soc_min_lo, min(soc_min_hi, defaults["min_soc"]))):
                NumberSelector(NumberSelectorConfig(min=soc_min_lo, max=soc_min_hi, step=1, mode=NumberSelectorMode.SLIDER)),
            vol.Required("charge_hysteresis_percent", default=defaults["charge_hysteresis_percent"]):
                NumberSelector(NumberSelectorConfig(min=MIN_CHARGE_HYSTERESIS_PERCENT, max=MAX_CHARGE_HYSTERESIS_PERCENT, step=1, mode=NumberSelectorMode.SLIDER)),
            vol.Required("backup_offgrid_threshold", default=defaults["backup_offgrid_threshold"]):
                NumberSelector(NumberSelectorConfig(min=0, max=2500, step=10, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)),
        })
        if (
            brand == "marstek"
            and self._current_battery_data.get(CONF_BATTERY_VERSION) in ("vA", "vD")
        ):
            _schema[
                vol.Required(
                    CONF_DC_PV_CONNECTED,
                    default=defaults[CONF_DC_PV_CONNECTED],
                )
            ] = BooleanSelector()
        if brand not in ("zendure", "anker", "sessy", "hoymiles", "huawei"):
            _schema[vol.Required(CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, default=defaults[CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED])] = bool
        if brand == "sessy":
            saved_capacity = float(defaults["battery_capacity_kwh"])
            capacity_field = (
                vol.Required("battery_capacity_kwh", default=saved_capacity)
                if saved_capacity > 0
                else vol.Required("battery_capacity_kwh")
            )
            _schema[capacity_field] = NumberSelector(
                NumberSelectorConfig(min=0.01, max=100, step=0.01, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
            )
        elif brand in ("zendure", "hoymiles"):
            _schema[vol.Optional("battery_capacity_kwh", default=defaults["battery_capacity_kwh"])] = NumberSelector(
                NumberSelectorConfig(min=0.01 if brand == "hoymiles" else 0, max=100, step=0.01, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
            )
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
                "included_in_consumption": user_input.get("included_in_consumption", True),
                "allow_solar_surplus": user_input.get("allow_solar_surplus", False),
                "dynamic_power_control": dynamic_power_control,
                "cover_home_when_active": user_input.get("cover_home_when_active", False),
                "ev_charger_no_telemetry": ev_no_telemetry,
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
            default_activity_sensor = current_device.get("activity_sensor", "")
            default_remaining_demand = current_device.get("remaining_demand_sensor") or ""
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
            default_activity_sensor = ""
            default_remaining_demand = ""

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
                    remaining_demand_field:
                        EntitySelector(
                            # No device_class filter: _read_sensor_kwh_opt accepts any
                            # convertible energy unit, and a template helper
                            # often carries no device_class at all.
                            EntitySelectorConfig(domain="sensor")
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
        """Configure time slot predictive grid charging in options flow."""
        errors = {}

        existing_config = self.config_entry.data
        existing_windows = _normalize_charging_windows(existing_config.get("charging_time_slot"))
        forecast_sensor_current = existing_config.get("solar_forecast_sensor", "")

        has_global_sensor = bool(
            self.config_entry.data.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
            or self.config_entry.data.get(CONF_SOLAR_FORECAST_SENSOR)
        )

        if user_input is not None:
            try:
                if has_global_sensor:
                    forecast_sensor = self.config_entry.data.get(CONF_SOLAR_FORECAST_SENSOR)
                else:
                    forecast_sensor = user_input.get("solar_forecast_sensor")
                    if forecast_sensor:
                        forecast_state = self.hass.states.get(forecast_sensor)
                        if forecast_state is None:
                            errors["solar_forecast_sensor"] = "sensor_not_found"
                        else:
                            unit = forecast_state.attributes.get("unit_of_measurement", "")
                            if unit not in ["kWh", "Wh"]:
                                errors["solar_forecast_sensor"] = "invalid_unit"

                windows, window_errors = _parse_charging_windows(user_input)
                errors.update(window_errors)

                if not errors:
                    self.config_data["enable_predictive_charging"] = True
                    self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_TIME_SLOT
                    self.config_data["charging_time_slot"] = windows
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    self.config_data[CONF_PREDICTIVE_SAFETY_MARGIN_KWH] = user_input.get(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)
                    self.config_data[CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT] = user_input.get(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)
                    return await self._save_and_finish()
            except Exception as e:
                _LOGGER.error("Error validating predictive charging config: %s", e)
                errors["base"] = "unknown"

        defaults = {
            "sensor": forecast_sensor_current if forecast_sensor_current else "",
            "margin": existing_config.get(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH),
            "grid_margin": existing_config.get(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT),
        }

        schema_dict = _charging_window_schema_fields(existing_windows)
        if not has_global_sensor:
            schema_dict[vol.Optional("solar_forecast_sensor", description={"suggested_value": defaults["sensor"]} if defaults["sensor"] else {})] = EntitySelector(
                EntitySelectorConfig(domain="sensor")
            )
        schema_dict[vol.Optional(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, default=defaults["margin"])] = NumberSelector(
            NumberSelectorConfig(min=0, max=20, step=0.1, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, default=defaults["grid_margin"])] = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=5, unit_of_measurement="%", mode=NumberSelectorMode.BOX)
        )
        return self.async_show_form(
            step_id="predictive_charging_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_dynamic_pricing_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure dynamic pricing predictive grid charging in options flow."""
        errors = {}
        has_global_sensor = bool(
            self.config_entry.data.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
            or self.config_entry.data.get(CONF_SOLAR_FORECAST_SENSOR)
        )
        existing_config = self.config_entry.data

        if user_input is not None:
            try:
                integration_type = user_input[CONF_PRICE_INTEGRATION_TYPE]
                price_sensor = user_input.get(CONF_PRICE_SENSOR)

                # Tibber has no price sensor — it polls the tibber.get_prices service.
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

                if has_global_sensor:
                    forecast_sensor = self.config_entry.data.get(CONF_SOLAR_FORECAST_SENSOR)
                else:
                    forecast_sensor = user_input.get("solar_forecast_sensor")
                    if forecast_sensor:
                        forecast_state = self.hass.states.get(forecast_sensor)
                        if forecast_state is None:
                            errors["solar_forecast_sensor"] = "sensor_not_found"
                        else:
                            unit = forecast_state.attributes.get("unit_of_measurement", "")
                            if unit not in ["kWh", "Wh"]:
                                errors["solar_forecast_sensor"] = "invalid_unit"

                if not errors:
                    max_price = _parse_optional_float(user_input.get(CONF_MAX_PRICE_THRESHOLD))
                    discharge_price = _parse_optional_float(user_input.get(CONF_DISCHARGE_PRICE_THRESHOLD))

                    if max_price is not None and discharge_price is not None and discharge_price < max_price:
                        errors[CONF_DISCHARGE_PRICE_THRESHOLD] = "discharge_below_charge"
                    else:
                        self.config_data["enable_predictive_charging"] = True
                        self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_DYNAMIC_PRICING
                        self.config_data[CONF_PRICE_INTEGRATION_TYPE] = integration_type
                        self.config_data[CONF_PRICE_SENSOR] = price_sensor
                        self.config_data[CONF_MAX_PRICE_THRESHOLD] = max_price
                        self.config_data[CONF_DISCHARGE_PRICE_THRESHOLD] = discharge_price
                        self.config_data[CONF_DP_PRICE_DISCHARGE_CONTROL] = user_input.get(CONF_DP_PRICE_DISCHARGE_CONTROL, False)
                        self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                        self.config_data["charging_time_slot"] = None
                        self.config_data[CONF_PREDICTIVE_SAFETY_MARGIN_KWH] = user_input.get(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)
                        self.config_data[CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT] = user_input.get(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)
                        self.config_data[CONF_NEGATIVE_PRICE_CHARGING_ENABLED] = user_input.get(
                            CONF_NEGATIVE_PRICE_CHARGING_ENABLED,
                            existing_config.get(CONF_NEGATIVE_PRICE_CHARGING_ENABLED, DEFAULT_NEGATIVE_PRICE_CHARGING_ENABLED),
                        )
                        self.config_data[CONF_SMART_PREDISCHARGE_ENABLED] = user_input.get(
                            CONF_SMART_PREDISCHARGE_ENABLED,
                            existing_config.get(CONF_SMART_PREDISCHARGE_ENABLED, DEFAULT_SMART_PREDISCHARGE_ENABLED),
                        )
                        self.config_data[CONF_SURPLUS_PRICE_HOLD_ENABLED] = user_input.get(
                            CONF_SURPLUS_PRICE_HOLD_ENABLED,
                            existing_config.get(
                                CONF_SURPLUS_PRICE_HOLD_ENABLED,
                                DEFAULT_SURPLUS_PRICE_HOLD_ENABLED,
                            ),
                        )
                        self.config_data[CONF_SURPLUS_HOLD_MIN_SAVING] = user_input.get(
                            CONF_SURPLUS_HOLD_MIN_SAVING,
                            existing_config.get(
                                CONF_SURPLUS_HOLD_MIN_SAVING,
                                DEFAULT_SURPLUS_HOLD_MIN_SAVING,
                            ),
                        )
                        # Cleared entity/select fields arrive absent, so read them
                        # straight from the submission rather than falling back to
                        # the stored value — that is what makes the × button work.
                        self.config_data[CONF_EXPORT_PRICE_SENSOR] = user_input.get(
                            CONF_EXPORT_PRICE_SENSOR
                        )
                        self.config_data[CONF_EXPORT_PRICE_INTEGRATION_TYPE] = user_input.get(
                            CONF_EXPORT_PRICE_INTEGRATION_TYPE
                        )
                        self.config_data[CONF_NEGATIVE_INJECTION_THRESHOLD] = user_input.get(
                            CONF_NEGATIVE_INJECTION_THRESHOLD,
                            existing_config.get(CONF_NEGATIVE_INJECTION_THRESHOLD, DEFAULT_NEGATIVE_INJECTION_THRESHOLD),
                        )
                        self.config_data[CONF_PREDISCHARGE_RESERVE_SOC] = user_input.get(
                            CONF_PREDISCHARGE_RESERVE_SOC,
                            existing_config.get(CONF_PREDISCHARGE_RESERVE_SOC, DEFAULT_PREDISCHARGE_RESERVE_SOC),
                        )
                        existing_export_mode, existing_export_power = _predischarge_export_defaults(
                            existing_config,
                            default_mode=PREDISCHARGE_EXPORT_MODE_SELF_CONSUMPTION,
                        )
                        export_mode, export_power = _predischarge_export_from_input(
                            user_input,
                            fallback_mode=existing_export_mode,
                            fallback_power=existing_export_power,
                        )
                        self.config_data[CONF_PREDISCHARGE_EXPORT_MODE] = export_mode
                        self.config_data[CONF_PREDISCHARGE_MAX_EXPORT_POWER_W] = export_power
                        if (
                            export_mode == PREDISCHARGE_EXPORT_MODE_CUSTOM
                            and CONF_PREDISCHARGE_MAX_EXPORT_POWER_W not in user_input
                        ):
                            return await self.async_step_predischarge_export_limit()
                        return await self._save_and_finish()
            except Exception as e:
                _LOGGER.error("Error validating dynamic pricing config: %s", e)
                errors["base"] = "unknown"

        default_integration = existing_config.get(CONF_PRICE_INTEGRATION_TYPE, PRICE_INTEGRATION_NORDPOOL)
        default_sensor = existing_config.get(CONF_PRICE_SENSOR, "")
        default_max_price = existing_config.get(CONF_MAX_PRICE_THRESHOLD)
        default_discharge_price = existing_config.get(CONF_DISCHARGE_PRICE_THRESHOLD)
        default_forecast = existing_config.get("solar_forecast_sensor", "")
        default_dp_discharge_control = existing_config.get(CONF_DP_PRICE_DISCHARGE_CONTROL, False)
        default_margin = existing_config.get(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)
        default_grid_margin = existing_config.get(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)
        default_negative_price_enabled = existing_config.get(
            CONF_NEGATIVE_PRICE_CHARGING_ENABLED,
            DEFAULT_NEGATIVE_PRICE_CHARGING_ENABLED,
        )
        default_smart_predischarge = existing_config.get(
            CONF_SMART_PREDISCHARGE_ENABLED, DEFAULT_SMART_PREDISCHARGE_ENABLED
        )
        default_surplus_hold = existing_config.get(
            CONF_SURPLUS_PRICE_HOLD_ENABLED, DEFAULT_SURPLUS_PRICE_HOLD_ENABLED
        )
        default_surplus_min_saving = existing_config.get(
            CONF_SURPLUS_HOLD_MIN_SAVING, DEFAULT_SURPLUS_HOLD_MIN_SAVING
        )
        default_export_sensor = existing_config.get(CONF_EXPORT_PRICE_SENSOR)
        default_export_type = existing_config.get(CONF_EXPORT_PRICE_INTEGRATION_TYPE)
        default_negative_threshold = existing_config.get(
            CONF_NEGATIVE_INJECTION_THRESHOLD, DEFAULT_NEGATIVE_INJECTION_THRESHOLD
        )
        default_reserve_soc = existing_config.get(
            CONF_PREDISCHARGE_RESERVE_SOC, DEFAULT_PREDISCHARGE_RESERVE_SOC
        )
        default_export_mode = _predischarge_export_defaults(
            existing_config,
            default_mode=PREDISCHARGE_EXPORT_MODE_SELF_CONSUMPTION,
        )[0]

        schema_dict: dict = {
            vol.Required(CONF_PRICE_INTEGRATION_TYPE, default=default_integration):
                _price_integration_type_selector(),
            # Optional: not used by Tibber, which polls the tibber.get_prices service.
            vol.Optional(CONF_PRICE_SENSOR, default=default_sensor if default_sensor else vol.UNDEFINED):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_MAX_PRICE_THRESHOLD,
                description={"suggested_value": str(default_max_price)} if default_max_price is not None else {}
            ):
                TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_DISCHARGE_PRICE_THRESHOLD,
                description={"suggested_value": str(default_discharge_price)} if default_discharge_price is not None else {}
            ):
                TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(CONF_DP_PRICE_DISCHARGE_CONTROL, default=default_dp_discharge_control): bool,
        }
        if not has_global_sensor:
            schema_dict[vol.Optional(
                "solar_forecast_sensor",
                description={"suggested_value": default_forecast} if default_forecast else {}
            )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        schema_dict[vol.Optional(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, default=default_margin)] = NumberSelector(
            NumberSelectorConfig(min=0, max=20, step=0.1, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, default=default_grid_margin)] = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=5, unit_of_measurement="%", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_NEGATIVE_PRICE_CHARGING_ENABLED, default=default_negative_price_enabled)] = bool
        schema_dict[vol.Optional(CONF_SMART_PREDISCHARGE_ENABLED, default=default_smart_predischarge)] = bool
        schema_dict[vol.Optional(CONF_SURPLUS_PRICE_HOLD_ENABLED, default=default_surplus_hold)] = bool
        schema_dict[vol.Optional(CONF_SURPLUS_HOLD_MIN_SAVING, default=default_surplus_min_saving)] = NumberSelector(
            NumberSelectorConfig(min=0, max=1, step=0.001, unit_of_measurement="€/kWh", mode=NumberSelectorMode.BOX)
        )
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
        schema_dict[vol.Optional(CONF_NEGATIVE_INJECTION_THRESHOLD, default=default_negative_threshold)] = NumberSelector(
            NumberSelectorConfig(min=-2, max=2, step=0.001, unit_of_measurement="€/kWh", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_PREDISCHARGE_RESERVE_SOC, default=default_reserve_soc)] = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=1, unit_of_measurement="%", mode=NumberSelectorMode.BOX)
        )
        mode_field, mode_selector = _predischarge_export_mode_selector(default_export_mode)
        schema_dict[mode_field] = mode_selector
        return self.async_show_form(
            step_id="dynamic_pricing_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_predischarge_export_limit(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure the W limit only for the custom export policy."""
        if user_input is not None:
            _mode, export_power = _predischarge_export_from_input(
                user_input,
                fallback_mode=PREDISCHARGE_EXPORT_MODE_CUSTOM,
            )
            self.config_data[CONF_PREDISCHARGE_EXPORT_MODE] = PREDISCHARGE_EXPORT_MODE_CUSTOM
            self.config_data[CONF_PREDISCHARGE_MAX_EXPORT_POWER_W] = export_power
            return await self._save_and_finish()

        export_config = dict(self.config_entry.data)
        export_config.update(self.config_data)
        _mode, export_power = _predischarge_export_defaults(
            export_config,
            default_mode=PREDISCHARGE_EXPORT_MODE_CUSTOM,
        )
        limit_field, limit_selector = _predischarge_export_limit_selector(export_power)
        return self.async_show_form(
            step_id="predischarge_export_limit",
            data_schema=vol.Schema({limit_field: limit_selector}),
        )

    async def async_step_realtime_price_config(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure real-time price charging mode in options flow."""
        errors = {}
        existing_config = self.config_entry.data
        has_global_sensor = bool(
            self.config_entry.data.get(CONF_SOLAR_FORECAST_REMAINING_SENSOR)
            or self.config_entry.data.get(CONF_SOLAR_FORECAST_SENSOR)
        )

        if user_input is not None:
            try:
                price_sensor = user_input[CONF_PRICE_SENSOR]
                price_state = self.hass.states.get(price_sensor)
                if price_state is None:
                    errors[CONF_PRICE_SENSOR] = "sensor_not_found"

                if has_global_sensor:
                    forecast_sensor = self.config_entry.data.get(CONF_SOLAR_FORECAST_SENSOR)
                else:
                    forecast_sensor = user_input.get("solar_forecast_sensor")
                    if forecast_sensor:
                        forecast_state = self.hass.states.get(forecast_sensor)
                        if forecast_state is None:
                            errors["solar_forecast_sensor"] = "sensor_not_found"
                        else:
                            unit = forecast_state.attributes.get("unit_of_measurement", "")
                            if unit not in ["kWh", "Wh"]:
                                errors["solar_forecast_sensor"] = "invalid_unit"

                if not errors:
                    max_price_raw = user_input.get(CONF_MAX_PRICE_THRESHOLD)
                    max_price = float(str(max_price_raw).replace(",", ".")) if max_price_raw else None
                    avg_sensor = user_input.get(CONF_AVERAGE_PRICE_SENSOR) or None

                    self.config_data["enable_predictive_charging"] = True
                    self.config_data[CONF_PREDICTIVE_CHARGING_MODE] = PREDICTIVE_MODE_REALTIME_PRICE
                    self.config_data[CONF_PRICE_SENSOR] = price_sensor
                    self.config_data[CONF_MAX_PRICE_THRESHOLD] = max_price
                    self.config_data[CONF_AVERAGE_PRICE_SENSOR] = avg_sensor
                    self.config_data[CONF_RT_PRICE_DISCHARGE_CONTROL] = user_input.get(CONF_RT_PRICE_DISCHARGE_CONTROL, False)
                    self.config_data[CONF_SOLAR_FORECAST_SENSOR] = forecast_sensor
                    self.config_data["charging_time_slot"] = None
                    self.config_data[CONF_PREDICTIVE_SAFETY_MARGIN_KWH] = user_input.get(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)
                    self.config_data[CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT] = user_input.get(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)
                    return await self._save_and_finish()
            except Exception as e:
                _LOGGER.error("Error validating real-time price config: %s", e)
                errors["base"] = "unknown"

        default_sensor = existing_config.get(CONF_PRICE_SENSOR, "")
        default_max_price = existing_config.get(CONF_MAX_PRICE_THRESHOLD)
        default_avg_sensor = existing_config.get(CONF_AVERAGE_PRICE_SENSOR, "")
        default_rt_discharge_control = existing_config.get(CONF_RT_PRICE_DISCHARGE_CONTROL, False)
        default_forecast = existing_config.get("solar_forecast_sensor", "")
        default_margin = existing_config.get(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, DEFAULT_PREDICTIVE_SAFETY_MARGIN_KWH)
        default_grid_margin = existing_config.get(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, DEFAULT_PREDICTIVE_GRID_CHARGE_MARGIN_PCT)

        schema_dict: dict = {
            vol.Required(CONF_PRICE_SENSOR, default=default_sensor if default_sensor else vol.UNDEFINED):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_MAX_PRICE_THRESHOLD,
                description={"suggested_value": str(default_max_price)} if default_max_price is not None else {}
            ):
                TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_AVERAGE_PRICE_SENSOR,
                description={"suggested_value": default_avg_sensor} if default_avg_sensor else {}
            ):
                EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(CONF_RT_PRICE_DISCHARGE_CONTROL, default=default_rt_discharge_control): bool,
        }
        if not has_global_sensor:
            schema_dict[vol.Optional(
                "solar_forecast_sensor",
                description={"suggested_value": default_forecast} if default_forecast else {}
            )] = EntitySelector(EntitySelectorConfig(domain="sensor"))
        schema_dict[vol.Optional(CONF_PREDICTIVE_SAFETY_MARGIN_KWH, default=default_margin)] = NumberSelector(
            NumberSelectorConfig(min=0, max=20, step=0.1, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)
        )
        schema_dict[vol.Optional(CONF_PREDICTIVE_GRID_CHARGE_MARGIN_PCT, default=default_grid_margin)] = NumberSelector(
            NumberSelectorConfig(min=0, max=100, step=5, unit_of_measurement="%", mode=NumberSelectorMode.BOX)
        )
        return self.async_show_form(
            step_id="realtime_price_config",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )
