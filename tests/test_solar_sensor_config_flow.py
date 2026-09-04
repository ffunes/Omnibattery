"""Regression tests for the optional solar-sensor validation."""

from types import SimpleNamespace

from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    OptionsFlowHandler,
)
from custom_components.omnibattery.const import (
    CONF_SOLAR_FORECAST_REMAINING_SENSOR,
    CONF_SOLAR_FORECAST_SENSOR,
    CONF_SOLAR_PRODUCTION_SENSOR,
)


def _hass_with_states(states: dict[str, object]) -> SimpleNamespace:
    """Build the small Home Assistant surface used by the sensor steps."""
    return SimpleNamespace(
        states=SimpleNamespace(get=states.get),
        config_entries=SimpleNamespace(async_entries=lambda _domain: []),
    )


def _state(unit: str) -> SimpleNamespace:
    return SimpleNamespace(attributes={"unit_of_measurement": unit})


async def test_initial_flow_reports_production_sensor_unit_error_on_that_field():
    flow = MarstekVenusConfigFlow()
    flow.hass = _hass_with_states(
        {"sensor.grid_power_va": _state("VA")}
    )

    result = await flow.async_step_user(
        {
            "consumption_sensor": "sensor.grid",
            "max_contracted_power": 7000,
            CONF_SOLAR_PRODUCTION_SENSOR: "sensor.grid_power_va",
        }
    )

    assert result["errors"] == {
        CONF_SOLAR_PRODUCTION_SENSOR: "solar_production_invalid_unit"
    }


async def test_initial_flow_rejects_omnibattery_solar_output_as_input():
    flow = MarstekVenusConfigFlow()
    flow.hass = _hass_with_states(
        {"sensor.omnibattery_solar_power": _state("W")}
    )

    result = await flow.async_step_user(
        {
            "consumption_sensor": "sensor.grid",
            "max_contracted_power": 7000,
            CONF_SOLAR_PRODUCTION_SENSOR: "sensor.omnibattery_solar_power",
        }
    )

    assert result["errors"] == {
        CONF_SOLAR_PRODUCTION_SENSOR: "solar_production_sensor_circular"
    }


async def test_initial_flow_validates_remaining_forecast_unit_separately():
    flow = MarstekVenusConfigFlow()
    flow.hass = _hass_with_states({"sensor.remaining": _state("W")})

    result = await flow.async_step_user(
        {
            "consumption_sensor": "sensor.grid",
            "max_contracted_power": 7000,
            CONF_SOLAR_FORECAST_REMAINING_SENSOR: "sensor.remaining",
        }
    )

    assert result["errors"] == {CONF_SOLAR_FORECAST_REMAINING_SENSOR: "invalid_unit"}


async def test_initial_remaining_forecast_replaces_unreadable_legacy_candidate():
    flow = MarstekVenusConfigFlow()
    flow.hass = _hass_with_states({"sensor.remaining": _state("kWh")})

    await flow.async_step_user(
        {
            "consumption_sensor": "sensor.grid",
            "max_contracted_power": 7000,
            # This legacy sensor no longer exists, but it will be discarded.
            CONF_SOLAR_FORECAST_SENSOR: "sensor.old_today",
            CONF_SOLAR_FORECAST_REMAINING_SENSOR: "sensor.remaining",
        }
    )

    assert flow.config_data[CONF_SOLAR_FORECAST_REMAINING_SENSOR] == "sensor.remaining"
    assert CONF_SOLAR_FORECAST_SENSOR not in flow.config_data


async def test_predictive_steps_recognize_global_remaining_forecast():
    for step_name in (
        "async_step_predictive_charging_config",
        "async_step_dynamic_pricing_config",
        "async_step_realtime_price_config",
    ):
        flow = MarstekVenusConfigFlow()
        flow.config_data[CONF_SOLAR_FORECAST_REMAINING_SENSOR] = "sensor.remaining"

        result = await getattr(flow, step_name)()
        fields = {
            getattr(marker, "schema", marker)
            for marker in result["data_schema"].schema
        }

        assert "solar_forecast_sensor" not in fields
        assert "solar_profile_mode" not in fields


async def test_options_flow_reports_production_sensor_unit_error_on_that_field():
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={"consumption_sensor": "sensor.grid", "max_contracted_power": 7000},
        options={},
    )
    flow = OptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            get={"sensor.grid_power_va": _state("VA")}.get
        ),
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: (
                entry if entry_id == entry.entry_id else None
            ),
            async_entries=lambda _domain: [],
        ),
    )
    flow.handler = entry.entry_id

    result = await flow.async_step_sensors(
        {
            "consumption_sensor": "sensor.grid",
            "max_contracted_power": 7000,
            CONF_SOLAR_PRODUCTION_SENSOR: "sensor.grid_power_va",
        }
    )

    assert result["errors"] == {
        CONF_SOLAR_PRODUCTION_SENSOR: "solar_production_invalid_unit"
    }


async def test_options_flow_rejects_legacy_omnibattery_solar_output_as_input():
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={"consumption_sensor": "sensor.grid", "max_contracted_power": 7000},
        options={},
    )
    flow = OptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            get={"sensor.marstek_venus_system_solar_power": _state("W")}.get
        ),
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: (
                entry if entry_id == entry.entry_id else None
            ),
            async_entries=lambda _domain: [],
        ),
    )
    flow.handler = entry.entry_id

    result = await flow.async_step_sensors(
        {
            "consumption_sensor": "sensor.grid",
            "max_contracted_power": 7000,
            CONF_SOLAR_PRODUCTION_SENSOR: "sensor.marstek_venus_system_solar_power",
        }
    )

    assert result["errors"] == {
        CONF_SOLAR_PRODUCTION_SENSOR: "solar_production_sensor_circular"
    }


async def test_options_merge_replaces_legacy_forecast_with_remaining():
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={CONF_SOLAR_FORECAST_SENSOR: "sensor.today"},
        options={},
    )
    flow = OptionsFlowHandler(entry)
    flow.handler = entry.entry_id
    updated = {}

    async def reload_entry(_entry_id):
        return True

    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda _entry_id: entry,
            async_update_entry=lambda _entry, data: updated.update(data),
            async_reload=reload_entry,
        )
    )
    flow.config_data = {CONF_SOLAR_FORECAST_REMAINING_SENSOR: "sensor.remaining"}
    flow.async_create_entry = lambda **_kwargs: {}

    await flow._save_and_finish()

    assert updated[CONF_SOLAR_FORECAST_REMAINING_SENSOR] == "sensor.remaining"
    assert CONF_SOLAR_FORECAST_SENSOR not in updated


async def test_options_merge_can_change_remaining_forecast_back_to_today():
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={CONF_SOLAR_FORECAST_REMAINING_SENSOR: "sensor.remaining"},
        options={},
    )
    flow = OptionsFlowHandler(entry)
    flow.handler = entry.entry_id
    updated = {}

    async def reload_entry(_entry_id):
        return True

    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda _entry_id: entry,
            async_update_entry=lambda _entry, data: updated.update(data),
            async_reload=reload_entry,
        )
    )
    flow.config_data = {
        CONF_SOLAR_FORECAST_SENSOR: "sensor.today",
        CONF_SOLAR_FORECAST_REMAINING_SENSOR: None,
    }
    flow.async_create_entry = lambda **_kwargs: {}

    await flow._save_and_finish()

    assert updated[CONF_SOLAR_FORECAST_SENSOR] == "sensor.today"
    assert CONF_SOLAR_FORECAST_REMAINING_SENSOR not in updated


async def test_options_merge_can_clear_remaining_forecast():
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={CONF_SOLAR_FORECAST_REMAINING_SENSOR: "sensor.remaining"},
        options={},
    )
    flow = OptionsFlowHandler(entry)
    flow.handler = entry.entry_id
    updated = {}

    async def reload_entry(_entry_id):
        return True

    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda _entry_id: entry,
            async_update_entry=lambda _entry, data: updated.update(data),
            async_reload=reload_entry,
        )
    )
    flow.config_data = {
        CONF_SOLAR_FORECAST_SENSOR: None,
        CONF_SOLAR_FORECAST_REMAINING_SENSOR: None,
    }
    flow.async_create_entry = lambda **_kwargs: {}

    await flow._save_and_finish()

    assert CONF_SOLAR_FORECAST_SENSOR not in updated
    assert CONF_SOLAR_FORECAST_REMAINING_SENSOR not in updated
