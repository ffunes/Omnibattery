"""Zonneplan provider contract and downstream pricing regression tests."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.const import DEFAULT_ZONNEPLAN_EXPORT_BONUS_ENABLED
from custom_components.omnibattery.pricing import calculations
from custom_components.omnibattery.pricing.engine import PricingManager


def entry(start="2999-01-01T12:00:00", end="2999-01-01T12:15:00", amount=3579015, excluded=None):
    result = dict(start_date=start, end_date=end, price_tax_included={"amount": amount})
    if excluded is not None:
        result["price_tax_excluded"] = {"amount": excluded}
    return result


@pytest.mark.parametrize("minutes", [15, 60])
def test_explicit_intervals_and_tax_inclusive_scale(minutes):
    start = datetime(2999, 1, 1, 12)
    slots = calculations.parse_zonneplan_prices({"forecast": [entry(start, start + timedelta(minutes=minutes))]})
    assert slots[0].price == pytest.approx(0.3579015)
    assert slots[0].end - slots[0].start == timedelta(minutes=minutes)


def test_legacy_datetime_and_gaps():
    slots = calculations.parse_zonneplan_prices({"forecast": [
        {"datetime": "2999-01-01T12:00:00", "electricity_price": 0},
        {"start_date": "2999-01-01T15:00:00", "electricity_price": -100000},
    ]})
    assert [s.price for s in slots] == [0, -0.01]
    assert slots[0].end == datetime(2999, 1, 1, 13)
    assert slots[-1].end == datetime(2999, 1, 1, 16)


@pytest.mark.parametrize("bad", [None, "bad", {}, entry(amount=None), entry(amount="nan"), entry(amount="inf"), entry(amount=True), entry(end=None), entry(end="2999-01-01T11:00:00")])
def test_bad_entries_do_not_discard_valid_prices(bad):
    assert len(calculations.parse_zonneplan_prices({"forecast": [bad, entry()]})) == 1


@pytest.mark.parametrize("forecast", [None, {}, "[]", 123])
def test_invalid_forecast(forecast):
    assert calculations.parse_zonneplan_prices({"forecast": forecast}) == []


def test_reject_gas_sensor():
    assert calculations.parse_zonneplan_prices({"unit_of_measurement": "€/m³", "forecast": [entry()]}) == []


def test_sort_deduplicate_and_keep_negative_price():
    early = entry(amount=-100000)
    late = entry("2999-01-01T13:00:00", "2999-01-01T13:15:00", 0)
    slots = calculations.parse_zonneplan_prices({"forecast": [late, early, early]})
    assert [s.price for s in slots] == [-0.01, 0]
    selected = calculations.select_cheapest_slots_by_duration(slots, 0.25, None)
    assert selected == [slots[0]]


def test_local_timezone_conversion(monkeypatch):
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Europe/Amsterdam"))
    slot = calculations.parse_zonneplan_prices({"forecast": [entry("2026-09-10T15:00:00Z", "2026-09-10T15:15:00Z")]})[0]
    assert slot.start == datetime(2026, 9, 10, 17)
    assert slot.end == datetime(2026, 9, 10, 17, 15)


def test_live_modern_hourly_forecast_entry(monkeypatch):
    """Real modern hourly sensor forecast observed on 2026-09-13."""
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Europe/Amsterdam"))
    forecast_entry = {
        "start_date": "2026-09-12T21:00:00+02:00",
        "end_date": "2026-09-12T22:00:00+02:00",
        "price_tax_included": {"amount": 3733562},
        "price_tax_excluded": {"amount": 2625081},
        "tariff_group": "normal",
        "sustainability_score": {"permille": 566},
    }
    slot = calculations.parse_prices_for_integration("zonneplan", {"forecast": [forecast_entry]})[0]
    assert slot.start == datetime(2026, 9, 12, 21)
    assert slot.end == datetime(2026, 9, 12, 22)
    assert slot.price == pytest.approx(0.3733562)


def test_clock_rollback_never_emits_negative_duration(monkeypatch):
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo("Europe/Amsterdam"))
    assert calculations.parse_zonneplan_prices({"forecast": [entry("2026-10-25T02:45:00+02:00", "2026-10-25T02:00:00+01:00")]}) == []


def test_engine_dispatch_and_current_state_no_double_scaling():
    controller = SimpleNamespace(price_integration_type="zonneplan", price_sensor="sensor.tariff")
    state = SimpleNamespace(state="0.3579015", attributes={"forecast": [entry()]})
    manager = PricingManager(SimpleNamespace(states=SimpleNamespace(get=lambda _: state)), controller)
    assert manager._get_current_price() == pytest.approx(0.3579015)
    slots = manager._parse_price_data(horizon_end=datetime(2999, 1, 2))
    assert len(slots) == 1
    assert slots[0].price == pytest.approx(0.3579015)


def test_shared_dispatch_and_stringified_forecast():
    attrs = {"forecast": [entry()]}
    assert calculations.parse_prices_for_integration("zonneplan", attrs) == calculations.parse_zonneplan_prices(attrs)
    assert calculations.stringified_price_attrs("zonneplan", {"forecast": "[]"}) == ["forecast"]


@pytest.mark.parametrize("explicit_export_type", [None, "zonneplan"])
@pytest.mark.parametrize("bonus_enabled, expected_export_price", [(False, 0.2), (True, 0.232)])
def test_independent_export_curve_uses_shared_dispatch(
    explicit_export_type, bonus_enabled, expected_export_price
):
    controller = SimpleNamespace(
        price_integration_type="zonneplan", price_sensor="sensor.import",
        export_price_sensor="sensor.export", export_price_integration_type=explicit_export_type,
        zonneplan_export_bonus_enabled=bonus_enabled,
        _consumption_tracker=SimpleNamespace(
            calculate_sunrise=lambda _day: 8.0, calculate_sunset=lambda _day: 18.0
        ),
        _price_data_status="ok",
    )
    states = {
        "sensor.import": SimpleNamespace(state="0.3579015", attributes={"forecast": [entry()]}),
        "sensor.export": SimpleNamespace(
            state="0.1", attributes={"forecast": [entry(amount=2000000, excluded=1000000)]}
        ),
    }
    manager = PricingManager(SimpleNamespace(states=SimpleNamespace(get=states.get)), controller)
    assert manager.get_future_export_price_slots(datetime(2999, 1, 2))[0].price == pytest.approx(
        expected_export_price
    )
    assert controller._price_data_status == "ok"
    assert manager.get_future_price_slots(datetime(2999, 1, 2))[0].price == pytest.approx(0.3579015)


@pytest.mark.parametrize(
    "start, end, expected",
    [
        ("2999-01-01T07:00:00", "2999-01-01T07:15:00", 0.2),
        ("2999-01-01T12:00:00", "2999-01-01T12:15:00", 0.232),
        ("2999-01-01T19:00:00", "2999-01-01T19:15:00", 0.2),
    ],
)
def test_export_bonus_only_applies_between_sunrise_and_sunset(start, end, expected):
    controller = SimpleNamespace(
        price_integration_type="zonneplan", price_sensor="sensor.import",
        export_price_sensor="sensor.export", export_price_integration_type="zonneplan",
        zonneplan_export_bonus_enabled=True,
        _consumption_tracker=SimpleNamespace(
            calculate_sunrise=lambda _day: 8.0, calculate_sunset=lambda _day: 18.0
        ),
        _price_data_status="ok",
    )
    state = SimpleNamespace(
        state="0.2", attributes={"forecast": [entry(start, end, 2000000, excluded=1000000)]}
    )
    manager = PricingManager(
        SimpleNamespace(states=SimpleNamespace(get=lambda _: state)), controller
    )
    assert manager.get_future_export_price_slots(datetime(2999, 1, 2))[0].price == pytest.approx(expected)


@pytest.mark.parametrize(
    "incl, excl, expected",
    [
        (-120000, -100000, 0.009),  # -1 ct excl: bonus makes the net price positive
        (-360000, -300000, -0.036),  # excl + 2 ct is negative: no bonus
        (-200000, -200000, -0.02),  # excl + 2 ct is zero: no bonus
    ],
)
def test_export_bonus_requires_positive_excluded_price_plus_fixed_part(incl, excl, expected):
    controller = SimpleNamespace(
        price_integration_type="zonneplan", price_sensor="sensor.import",
        export_price_sensor="sensor.export", export_price_integration_type="zonneplan",
        zonneplan_export_bonus_enabled=True,
        _consumption_tracker=SimpleNamespace(
            calculate_sunrise=lambda _day: 8.0, calculate_sunset=lambda _day: 18.0
        ),
        _price_data_status="ok",
    )
    state = SimpleNamespace(
        state="0", attributes={"forecast": [entry(amount=incl, excluded=excl)]}
    )
    manager = PricingManager(
        SimpleNamespace(states=SimpleNamespace(get=lambda _: state)), controller
    )
    assert manager.get_future_export_price_slots(datetime(2999, 1, 2))[0].price == pytest.approx(expected)


def test_battery_export_curve_never_includes_the_solar_bonus():
    controller = SimpleNamespace(
        price_integration_type="zonneplan", price_sensor="sensor.import",
        export_price_sensor="sensor.export", export_price_integration_type="zonneplan",
        zonneplan_export_bonus_enabled=True,
        _consumption_tracker=SimpleNamespace(
            calculate_sunrise=lambda _day: 8.0, calculate_sunset=lambda _day: 18.0
        ),
        _price_data_status="ok",
    )
    state = SimpleNamespace(
        state="0.2", attributes={"forecast": [entry(amount=2000000, excluded=1000000)]}
    )
    manager = PricingManager(
        SimpleNamespace(states=SimpleNamespace(get=lambda _: state)), controller
    )
    horizon = datetime(2999, 1, 2)
    assert manager.get_future_export_price_slots(horizon)[0].price == pytest.approx(0.232)
    assert manager.get_future_export_price_slots(horizon, solar_bonus=False)[0].price == pytest.approx(0.2)


def test_missing_export_sensor_falls_back_to_import_without_export_bonus():
    controller = SimpleNamespace(
        price_integration_type="zonneplan",
        price_sensor="sensor.import",
        export_price_sensor=None,
        export_price_integration_type="zonneplan",
        zonneplan_export_bonus_enabled=True,
        _price_data_status="ok",
    )
    state = SimpleNamespace(
        state="0.3579015", attributes={"forecast": [entry()]}
    )
    manager = PricingManager(
        SimpleNamespace(states=SimpleNamespace(get=lambda _: state)), controller
    )

    import_slots = manager.get_future_price_slots(datetime(2999, 1, 2))
    export_slots = manager.get_future_export_price_slots(datetime(2999, 1, 2))

    assert export_slots == import_slots
    assert export_slots[0].price == pytest.approx(0.3579015)


def test_shared_export_selector_and_validation():
    from custom_components.omnibattery.config_flow import _price_integration_export_options, _validate_price_sensor

    assert "zonneplan" in _price_integration_export_options()
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: SimpleNamespace(attributes={"forecast": [entry()]})))
    assert _validate_price_sensor(hass, "sensor.export", "zonneplan", allow_service_cache=False) is None


@pytest.mark.parametrize("options", [False, True])
async def test_zonneplan_export_bonus_shown_before_zonneplan_is_saved(options):
    """A fresh setup still on the Nordpool default must offer the bonus in the
    same form where Zonneplan gets picked, not only after a second visit."""
    from custom_components.omnibattery.config_flow import (
        MarstekVenusConfigFlow,
        OptionsFlowHandler,
    )

    config_entry = SimpleNamespace(entry_id="test", data={}, options={})
    flow = OptionsFlowHandler(config_entry) if options else MarstekVenusConfigFlow()
    flow.handler = "test"
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_known_entry=lambda _: config_entry),
        states=SimpleNamespace(get=lambda _: None),
    )
    form = await flow.async_step_dynamic_pricing_config()
    fields = {marker.schema: marker for marker in form["data_schema"].schema}
    assert fields["price_integration_type"].default() == "nordpool"
    assert fields["zonneplan_export_bonus_enabled"].default() is False


@pytest.mark.parametrize("options", [False, True])
async def test_switching_export_provider_keeps_selection_after_validation_error(options):
    from custom_components.omnibattery.config_flow import (
        MarstekVenusConfigFlow,
        OptionsFlowHandler,
    )

    config_entry = SimpleNamespace(
        entry_id="test",
        data={
            "price_integration_type": "nordpool",
            "price_sensor": "sensor.import",
            "export_price_sensor": "sensor.export",
            "export_price_integration_type": "nordpool",
        },
        options={},
    )
    flow = OptionsFlowHandler(config_entry) if options else MarstekVenusConfigFlow()
    if not options:
        flow.config_data.update(config_entry.data)
    flow.handler = config_entry.entry_id
    states = {
        "sensor.import": SimpleNamespace(
            state="0.1",
            attributes={
                "raw_today": [
                    {
                        "start": datetime.now(),
                        "end": datetime.now() + timedelta(hours=1),
                        "value": 0.1,
                    }
                ]
            },
        ),
        "sensor.export": SimpleNamespace(state="0.1", attributes={}),
        "sensor.forecast": SimpleNamespace(
            state="5", attributes={"unit_of_measurement": "kWh"}
        ),
    }
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda _: config_entry,
        ),
        states=SimpleNamespace(get=states.get),
    )

    form = await flow.async_step_dynamic_pricing_config(
        {
            "price_integration_type": "nordpool",
            "price_sensor": "sensor.import",
            "export_price_sensor": "sensor.export",
            "export_price_integration_type": "zonneplan",
            "solar_forecast_sensor": "sensor.forecast",
        }
    )

    assert form["type"] == "form"
    assert form["errors"]["export_price_sensor"] == "no_price_data"
    schema = form["data_schema"].schema
    fields = {marker.schema: marker for marker in schema}
    assert "zonneplan_export_bonus_enabled" in fields
    assert fields["price_integration_type"].default() == "nordpool"
    assert fields["price_sensor"].default() == "sensor.import"
    assert fields["export_price_sensor"].description == {
        "suggested_value": "sensor.export"
    }
    assert fields["export_price_integration_type"].description == {
        "suggested_value": "zonneplan"
    }
    assert fields["zonneplan_export_bonus_enabled"].default() is (
        DEFAULT_ZONNEPLAN_EXPORT_BONUS_ENABLED
    )
    assert fields["solar_forecast_sensor"].description == {
        "suggested_value": "sensor.forecast"
    }


def test_parse_error_logs_offending_entry(caplog):
    bad = entry(amount="invalid-amount")
    with caplog.at_level("DEBUG"):
        assert calculations.parse_zonneplan_prices({"forecast": [bad]}) == []
    assert "invalid-amount" in caplog.text


@pytest.mark.parametrize("options", [False, True])
@pytest.mark.parametrize("valid", [False, True])
@pytest.mark.parametrize("bonus_enabled", [None, False, True])
async def test_setup_and_options_validate_and_save_zonneplan(options, valid, bonus_enabled):
    from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow, OptionsFlowHandler

    config_entry = SimpleNamespace(entry_id="test", data={}, options={})
    flow = OptionsFlowHandler(config_entry) if options else MarstekVenusConfigFlow()
    if options:
        config_entry.data["price_integration_type"] = "zonneplan"
    else:
        flow.config_data["price_integration_type"] = "zonneplan"
    flow.handler = "test"
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda _: config_entry,
            async_update_entry=lambda *args, **kwargs: None,
            async_reload=AsyncMock(),
        ),
        states=SimpleNamespace(get=lambda _: SimpleNamespace(
            attributes={"forecast": [entry()]} if valid else {"forecast": [{"garbage": 1}]},
        )),
    )
    form = await flow.async_step_dynamic_pricing_config()
    selector = next(value for marker, value in form["data_schema"].schema.items() if marker.schema == "price_integration_type")
    assert "zonneplan" in selector.config["options"]
    user_input = {
        "price_integration_type": "zonneplan", "price_sensor": "sensor.tariff",
    }
    if bonus_enabled is not None:
        user_input["zonneplan_export_bonus_enabled"] = bonus_enabled
    result = await flow.async_step_dynamic_pricing_config(user_input)
    if valid:
        assert flow.config_data["price_integration_type"] == "zonneplan"
        assert flow.config_data["price_sensor"] == "sensor.tariff"
        assert flow.config_data["zonneplan_export_bonus_enabled"] is (
            DEFAULT_ZONNEPLAN_EXPORT_BONUS_ENABLED
            if bonus_enabled is None
            else bonus_enabled
        )
        assert result.get("errors", {}) == {}
    else:
        assert result["errors"]["price_sensor"] == "no_price_data"
