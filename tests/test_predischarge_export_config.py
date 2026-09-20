"""Configuration and compatibility tests for anti-curtailment export policy."""

from types import SimpleNamespace

from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    OptionsFlowHandler,
)
from custom_components.omnibattery.const import (
    CONF_PREDISCHARGE_EXPORT_MODE,
    CONF_PREDISCHARGE_MAX_EXPORT_POWER_W,
    DOMAIN,
    PREDISCHARGE_EXPORT_MODE_AUTOMATIC,
    PREDISCHARGE_EXPORT_MODE_CUSTOM,
    PREDISCHARGE_EXPORT_MODE_SELF_CONSUMPTION,
    normalize_predischarge_export_settings,
)
from custom_components.omnibattery.number import SmartPredischargeNumber


def _schema_defaults(result) -> dict[str, object]:
    """Return form defaults keyed by field name."""
    return {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if callable(marker.default)
    }


def _schema_fields(result) -> set[str]:
    """Return all field names exposed by a form."""
    return {marker.schema for marker in result["data_schema"].schema}


def _options_flow(entry: SimpleNamespace) -> OptionsFlowHandler:
    """Initialize an options flow as Home Assistant's flow manager does."""
    flow = OptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: (
                entry if entry_id == entry.entry_id else None
            )
        )
    )
    flow.handler = entry.entry_id
    return flow


def test_legacy_export_values_are_mapped_to_modes():
    """Old zero/positive limits map to self-consumption/custom respectively."""
    assert normalize_predischarge_export_settings(None, 0) == (
        PREDISCHARGE_EXPORT_MODE_SELF_CONSUMPTION,
        0.0,
    )
    assert normalize_predischarge_export_settings(None, 1800) == (
        PREDISCHARGE_EXPORT_MODE_CUSTOM,
        1800.0,
    )
    assert normalize_predischarge_export_settings(
        PREDISCHARGE_EXPORT_MODE_AUTOMATIC, 1800
    ) == (PREDISCHARGE_EXPORT_MODE_AUTOMATIC, 0.0)


async def test_initial_dynamic_pricing_form_exposes_three_export_modes():
    """The basic W field is replaced by the mode selector."""
    flow = MarstekVenusConfigFlow()

    result = await flow.async_step_dynamic_pricing_config()

    fields = _schema_fields(result)
    assert CONF_PREDISCHARGE_EXPORT_MODE in fields
    assert CONF_PREDISCHARGE_MAX_EXPORT_POWER_W not in fields
    mode_marker = next(
        marker
        for marker in result["data_schema"].schema
        if marker.schema == CONF_PREDISCHARGE_EXPORT_MODE
    )
    assert mode_marker.default() == PREDISCHARGE_EXPORT_MODE_AUTOMATIC
    assert {
        option
        for option in result["data_schema"].schema[mode_marker].config["options"]
    } == {
        PREDISCHARGE_EXPORT_MODE_SELF_CONSUMPTION,
        PREDISCHARGE_EXPORT_MODE_AUTOMATIC,
        PREDISCHARGE_EXPORT_MODE_CUSTOM,
    }


async def test_custom_mode_shows_limit_step_and_persists_numeric_contract():
    """Custom mode asks for W separately and keeps the engine's old key."""
    flow = MarstekVenusConfigFlow()
    flow.config_data["solar_forecast_sensor"] = "sensor.solar_forecast"
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda _entity_id: SimpleNamespace(attributes={"raw_today": []})
        ),
        services=SimpleNamespace(has_service=lambda *_args: False),
    )

    result = await flow.async_step_dynamic_pricing_config(
        {
            "price_integration_type": "nordpool",
            "price_sensor": "sensor.price",
            "price_discharge_control": False,
            CONF_PREDISCHARGE_EXPORT_MODE: PREDISCHARGE_EXPORT_MODE_CUSTOM,
        }
    )

    assert result["step_id"] == "predischarge_export_limit"
    assert _schema_fields(result) == {CONF_PREDISCHARGE_MAX_EXPORT_POWER_W}

    finished = await flow.async_step_predischarge_export_limit(
        {CONF_PREDISCHARGE_MAX_EXPORT_POWER_W: 1750}
    )

    assert finished["type"] == "create_entry"
    assert finished["data"][CONF_PREDISCHARGE_EXPORT_MODE] == PREDISCHARGE_EXPORT_MODE_CUSTOM
    assert finished["data"][CONF_PREDISCHARGE_MAX_EXPORT_POWER_W] == 1750.0


async def test_options_flow_infers_legacy_zero_and_positive_limits():
    """Existing entries open with the selector mode inferred from old data."""
    zero_entry = SimpleNamespace(entry_id="zero", data={CONF_PREDISCHARGE_MAX_EXPORT_POWER_W: 0})
    zero_flow = _options_flow(zero_entry)
    zero_form = await zero_flow.async_step_dynamic_pricing_config()
    assert _schema_defaults(zero_form)[CONF_PREDISCHARGE_EXPORT_MODE] == (
        PREDISCHARGE_EXPORT_MODE_SELF_CONSUMPTION
    )

    custom_entry = SimpleNamespace(
        entry_id="custom",
        data={CONF_PREDISCHARGE_MAX_EXPORT_POWER_W: 2200},
    )
    custom_flow = _options_flow(custom_entry)
    custom_form = await custom_flow.async_step_dynamic_pricing_config()
    assert _schema_defaults(custom_form)[CONF_PREDISCHARGE_EXPORT_MODE] == (
        PREDISCHARGE_EXPORT_MODE_CUSTOM
    )
    limit_form = await custom_flow.async_step_predischarge_export_limit()
    assert _schema_defaults(limit_form)[CONF_PREDISCHARGE_MAX_EXPORT_POWER_W] == 2200.0


async def test_number_entity_keeps_numeric_contract_and_selects_custom_mode():
    """Editing the legacy number remains a supported custom-limit shortcut."""
    entry = SimpleNamespace(
        entry_id="entry",
        data={
            CONF_PREDISCHARGE_EXPORT_MODE: PREDISCHARGE_EXPORT_MODE_AUTOMATIC,
            CONF_PREDISCHARGE_MAX_EXPORT_POWER_W: 0,
        },
    )
    updated: dict = {}
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=lambda _entry, *, data: updated.update(data)
        ),
        data={DOMAIN: {entry.entry_id: {}}},
    )
    entity = SmartPredischargeNumber(hass, entry, "export")
    entity.async_write_ha_state = lambda: None

    assert entity.native_value == 0.0
    await entity.async_set_native_value(1500)

    assert updated[CONF_PREDISCHARGE_MAX_EXPORT_POWER_W] == 1500.0
    assert updated[CONF_PREDISCHARGE_EXPORT_MODE] == PREDISCHARGE_EXPORT_MODE_CUSTOM
