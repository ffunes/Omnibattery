"""The predictive steps ask only for what an entity cannot provide.

Thresholds, feature toggles and every power/SOC limit are live dashboard
entities writing the same ``entry.data`` keys, so duplicating them in the flow
was the whole reason the options form had twenty-one fields. What is left needs
validation against ``hass.states`` and a platform reload, which an entity
cannot do.
"""

from types import SimpleNamespace

import pytest

from custom_components.omnibattery.config_flow import (
    MarstekVenusConfigFlow,
    OptionsFlowHandler,
    _preserve_values_in_error_forms,
)


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


def _flow_with_states(data: dict, states: dict, options: bool):
    if options:
        flow = _flow(data)
        flow.hass.states = SimpleNamespace(get=states.get)
    else:
        flow = MarstekVenusConfigFlow()
        flow.config_data.update(data)
        flow.handler = "test-entry"
        flow.hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    return flow


async def test_dynamic_pricing_asks_only_for_price_sources():
    result = await _flow({}).async_step_dynamic_pricing_config()

    assert _schema_keys(result) == {
        "price_integration_type",
        "price_sensor",
        "export_price_sensor",
        "export_price_integration_type",
        "zonneplan_export_bonus_enabled",
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

    assert "predictive_safety_margin_pct" not in _schema_keys(result)
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


@pytest.mark.parametrize("options", [False, True])
async def test_time_slot_validation_preserves_submitted_values(options):
    states = {
        "sensor.forecast": SimpleNamespace(
            state="5", attributes={"unit_of_measurement": "kWh"}
        )
    }
    flow = _flow_with_states(
        {"charging_time_slot": {"start_time": "22:00", "end_time": "23:00", "days": ["mon"]}},
        states,
        options,
    )

    form = await flow.async_step_predictive_charging_config(
        {
            "start_time": "01:00",
            "end_time": "02:00",
            "days": ["tue"],
            "start_time_2": "03:00",
            "solar_forecast_sensor": "sensor.forecast",
        }
    )

    assert form["type"] == "form"
    assert form["errors"]["start_time_2"] == "incomplete_window"
    fields = {marker.schema: marker for marker in form["data_schema"].schema}
    assert fields["start_time"].default() == "01:00"
    assert fields["end_time"].default() == "02:00"
    assert fields["days"].default() == ["tue"]
    assert fields["start_time_2"].default() == "03:00"
    assert fields["solar_forecast_sensor"].description == {
        "suggested_value": "sensor.forecast"
    }


@pytest.mark.parametrize("options", [False, True])
async def test_realtime_price_validation_preserves_submitted_values(options):
    states = {
        "sensor.forecast": SimpleNamespace(
            state="5", attributes={"unit_of_measurement": "kWh"}
        )
    }
    flow = _flow_with_states(
        {
            "price_sensor": "sensor.old_price",
            "average_price_sensor": "sensor.old_average",
        },
        states,
        options,
    )

    form = await flow.async_step_realtime_price_config(
        {
            "price_sensor": "sensor.new_price",
            "average_price_sensor": "sensor.new_average",
            "solar_forecast_sensor": "sensor.forecast",
        }
    )

    assert form["type"] == "form"
    assert form["errors"]["price_sensor"] == "sensor_not_found"
    fields = {marker.schema: marker for marker in form["data_schema"].schema}
    assert fields["price_sensor"].default() == "sensor.new_price"
    assert fields["average_price_sensor"].description == {
        "suggested_value": "sensor.new_average"
    }
    assert fields["solar_forecast_sensor"].description == {
        "suggested_value": "sensor.forecast"
    }


async def test_preserving_values_never_evaluates_step_annotations():
    """Python 3.14 evaluates annotations lazily; HA's inherited steps use
    TYPE_CHECKING-only names (BluetoothServiceInfoBleak) and must not crash
    the import of config_flow."""

    def _unresolvable(_format):
        raise NameError("name 'BluetoothServiceInfoBleak' is not defined")

    class Base:
        async def async_step_bluetooth(self, discovery_info):
            return discovery_info

        async_step_bluetooth.__annotate__ = _unresolvable

    @_preserve_values_in_error_forms
    class Flow(Base):
        async def async_step_user(self, user_input=None):
            return user_input

    assert await Flow().async_step_user({"a": 1}) == {"a": 1}
    assert await Flow().async_step_user(user_input={"a": 2}) == {"a": 2}
    assert await Flow().async_step_user() is None
