"""Zonneplan provider contract and downstream pricing regression tests."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.pricing import calculations
from custom_components.omnibattery.pricing.engine import PricingManager


def entry(start="2999-01-01T12:00:00", end="2999-01-01T12:15:00", amount=3579015):
    return dict(start_date=start, end_date=end, price_tax_included={"amount": amount})


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


@pytest.mark.parametrize("options", [False, True])
@pytest.mark.parametrize("valid", [False, True])
async def test_setup_and_options_validate_and_save_zonneplan(options, valid):
    from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow, OptionsFlowHandler

    config_entry = SimpleNamespace(entry_id="test", data={}, options={})
    flow = OptionsFlowHandler(config_entry) if options else MarstekVenusConfigFlow()
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
    result = await flow.async_step_dynamic_pricing_config({
        "price_integration_type": "zonneplan", "price_sensor": "sensor.tariff",
    })
    if valid:
        assert flow.config_data["price_integration_type"] == "zonneplan"
        assert flow.config_data["price_sensor"] == "sensor.tariff"
        assert result.get("errors", {}) == {}
    else:
        assert result["errors"]["price_sensor"] == "no_price_data"
