"""Spanish 2.0TD fixed three-period tariff: a price source with no sensor."""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery.pricing import calculations
from custom_components.omnibattery.pricing.engine import PricingManager

PUNTA, LLANO, VALLE = 0.20, 0.15, 0.08
WEDNESDAY = date(2026, 11, 25)
SATURDAY = date(2026, 11, 28)


def prices(day, workdays=None):
    return [
        slot.price
        for slot in calculations.td20_price_slots([day], PUNTA, LLANO, VALLE, workdays)
    ]


def controller(**overrides):
    values = dict(
        price_integration_type="tarifa_20td",
        price_sensor=None,
        export_price_sensor=None,
        td20_price_punta=PUNTA,
        td20_price_llano=LLANO,
        td20_price_valle=VALLE,
        td20_export_price=None,
        td20_workday_entity=None,
        _td20_workdays={},
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def manager(ctrl, now, hass=None):
    mgr = PricingManager(hass or SimpleNamespace(), ctrl)
    mgr._now = lambda: now
    return mgr


def test_working_day_periods():
    assert prices(WEDNESDAY) == (
        [VALLE] * 8 + [LLANO] * 2 + [PUNTA] * 4 + [LLANO] * 4 + [PUNTA] * 4 + [LLANO] * 2
    )


def test_weekend_is_valle_all_day_without_workday():
    assert prices(SATURDAY) == [VALLE] * 24


def test_holiday_is_valle_and_workday_cannot_make_a_weekend_work():
    assert prices(WEDNESDAY, {WEDNESDAY: False}) == [VALLE] * 24
    assert prices(SATURDAY, {SATURDAY: True}) == [VALLE] * 24


def test_engine_reads_current_price_and_two_days_of_slots():
    mgr = manager(controller(), datetime(2026, 11, 25, 19, 30))
    assert mgr._get_current_price() == PUNTA
    slots, _status = mgr._raw_price_slots()
    assert len(slots) == 48
    assert slots[0].start == datetime(2026, 11, 25) and slots[-1].end == datetime(2026, 11, 27)


def test_missing_prices_give_no_slots():
    mgr = manager(controller(td20_price_llano=None), datetime(2026, 11, 25, 12))
    assert mgr._raw_price_slots() == ([], "no_slots")
    assert mgr._get_current_price() is None


def test_surplus_price_is_a_flat_export_curve():
    now = datetime(2026, 11, 25, 12)
    mgr = manager(controller(td20_export_price=0.05), now)
    assert mgr._has_export_curve()
    exports = mgr.get_future_export_price_slots(datetime(2026, 11, 25, 23, 59))
    assert len(exports) == 12 and {slot.price for slot in exports} == {0.05}
    # Zero is a real price, not "unset"; unset keeps the no-export-curve
    # behaviour every other provider has without an export sensor.
    assert manager(controller(td20_export_price=0.0), now)._has_export_curve()
    assert not manager(controller(), now)._has_export_curve()


def test_workday_service_marks_the_holiday_of_tomorrow():
    calls = []

    async def async_call(domain, service, data, **_kwargs):
        calls.append((domain, service, data["check_date"]))
        return {data["entity_id"]: {"workday": data["check_date"] != "2026-12-08"}}

    hass = SimpleNamespace(services=SimpleNamespace(async_call=async_call))
    ctrl = controller(td20_workday_entity="binary_sensor.workday_sensor")
    mgr = manager(ctrl, datetime(2026, 12, 7, 21), hass)

    asyncio.run(mgr._maybe_refresh_td20_workdays())
    asyncio.run(mgr._maybe_refresh_td20_workdays())  # cached: no second round

    assert calls == [
        ("workday", "check_date", "2026-12-07"),
        ("workday", "check_date", "2026-12-08"),
    ]
    slots, _status = mgr._raw_price_slots()
    assert slots[21].price == PUNTA  # Monday 21:00
    assert {slot.price for slot in slots[24:]} == {VALLE}  # Tuesday 8 December


def test_workday_failure_falls_back_to_weekends_only():
    async def async_call(*_args, **_kwargs):
        raise RuntimeError("workday not loaded")

    hass = SimpleNamespace(services=SimpleNamespace(async_call=async_call))
    ctrl = controller(td20_workday_entity="binary_sensor.workday_sensor")
    mgr = manager(ctrl, datetime(2026, 12, 7, 21), hass)
    asyncio.run(mgr._maybe_refresh_td20_workdays())
    assert ctrl._td20_workdays == {}
    assert mgr._get_current_price() == PUNTA


@pytest.mark.parametrize("options", [False, True])
async def test_flow_asks_prices_in_a_second_step_without_a_sensor(options):
    from custom_components.omnibattery.config_flow import (
        MarstekVenusConfigFlow,
        OptionsFlowHandler,
        _price_integration_export_options,
    )

    assert "tarifa_20td" not in _price_integration_export_options()
    config_entry = SimpleNamespace(
        entry_id="test", data={"solar_forecast_sensor": "sensor.forecast"}, options={}
    )
    flow = OptionsFlowHandler(config_entry) if options else MarstekVenusConfigFlow()
    flow.handler = "test"
    flow.config_data = {"solar_forecast_sensor": "sensor.forecast"}
    forecast = SimpleNamespace(state="5", attributes={"unit_of_measurement": "kWh"})
    flow.hass = SimpleNamespace(
        async_create_task=lambda coro, _name=None: coro.close(),
        config_entries=SimpleNamespace(async_get_known_entry=lambda _: config_entry),
        states=SimpleNamespace(get=lambda _: forecast),
    )
    finish = AsyncMock(return_value={"type": "create_entry"})
    flow._finish_setup = flow._save_and_finish = finish

    form = await flow.async_step_dynamic_pricing_config(
        {"price_integration_type": "tarifa_20td", "solar_forecast_sensor": "sensor.forecast"}
    )
    assert form["step_id"] == "td20_config"
    assert {marker.schema for marker in form["data_schema"].schema} == {
        "td20_price_punta", "td20_price_llano", "td20_price_valle",
        "td20_export_price", "td20_workday_entity",
    }
    finish.assert_not_awaited()

    await flow.async_step_td20_config(
        {"td20_price_punta": PUNTA, "td20_price_llano": LLANO, "td20_price_valle": VALLE}
    )
    finish.assert_awaited_once()
    assert flow.config_data["price_sensor"] is None
    assert flow.config_data["td20_price_valle"] == VALLE
    assert flow.config_data["td20_export_price"] is None
