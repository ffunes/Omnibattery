"""The predictive steps ask only for what an entity cannot provide.

Thresholds, feature toggles and every power/SOC limit are live dashboard
entities writing the same ``entry.data`` keys, so duplicating them in the flow
was the whole reason the options form had twenty-one fields. What is left needs
validation against ``hass.states`` and a platform reload, which an entity
cannot do.
"""

from types import SimpleNamespace

from custom_components.omnibattery.config_flow import OptionsFlowHandler


def _schema_keys(result) -> set[str]:
    return {str(key) for key in result["data_schema"].schema}


def _flow(data: dict) -> OptionsFlowHandler:
    entry = SimpleNamespace(entry_id="test-entry", data=data, options={})
    flow = OptionsFlowHandler(entry)
    flow.handler = entry.entry_id
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _e: SimpleNamespace(attributes={})),
        services=SimpleNamespace(has_service=lambda *_a: True),
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: (
                entry if entry_id == entry.entry_id else None
            ),
            async_entries=lambda _domain: [],
        ),
    )
    return flow


async def test_dynamic_pricing_asks_only_for_price_sources():
    result = await _flow({}).async_step_dynamic_pricing_config()

    assert _schema_keys(result) == {
        "price_integration_type",
        "price_sensor",
        "export_price_sensor",
        "export_price_integration_type",
        "solar_forecast_sensor",
    }


async def test_realtime_price_asks_only_for_price_sources():
    result = await _flow({}).async_step_realtime_price_config()

    assert _schema_keys(result) == {
        "price_sensor",
        "average_price_sensor",
        "solar_forecast_sensor",
    }


async def test_time_slot_asks_only_for_windows_and_forecast():
    result = await _flow({}).async_step_predictive_charging_config()

    assert "predictive_safety_margin_kwh" not in _schema_keys(result)
    assert "solar_forecast_sensor" in _schema_keys(result)


async def test_forecast_field_is_hidden_when_the_sensors_step_set_one():
    result = await _flow(
        {"solar_forecast_remaining_sensor": "sensor.remaining"}
    ).async_step_dynamic_pricing_config()

    assert "solar_forecast_sensor" not in _schema_keys(result)


async def test_submitting_leaves_the_entity_owned_keys_alone():
    """A saved step must not reset a slider the dashboard owns."""
    flow = _flow({"max_price_threshold": 0.12, "predischarge_reserve_soc": 35})

    await flow.async_step_dynamic_pricing_config(
        {"price_integration_type": "nordpool", "price_sensor": "sensor.price"}
    )

    assert "max_price_threshold" not in flow.config_data
    assert "predischarge_reserve_soc" not in flow.config_data
