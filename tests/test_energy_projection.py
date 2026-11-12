"""The shared energy projection and High-Price Sale's use of it (S6)."""
from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.omnibattery.control.high_price_discharge import (
    HighPriceDischargeManager,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)
from custom_components.omnibattery.solar_forecast import (
    SolarForecast,
    SolarForecastInput,
    SolarForecastPeriod,
)
from custom_components.omnibattery.tracking.consumption_tracker import (
    ConsumptionTracker,
)
from tests import test_predictive_scenarios as T

BELL = [0.2, 0.6, 1.0, 1.3, 1.3, 1.0, 0.45, 0.15]  # 08:00-16:00, 6.0 kWh


class _Tracker(T._Tracker):
    def get_solar_fraction_done(self, now_h, t_start, t_end):
        return ConsumptionTracker.get_solar_fraction_done(now_h, t_start, t_end)

    def get_avg_daily_consumption(self):
        return sum(self._scenario.bins_kwh)

    def get_consumption_window_hours_per_day(self):
        return 24.0

    def consumption_window_hours_in_range(self, a, b):
        return max(0.0, b - a)


def _sunny_scenario(safety: float = 0.0):
    base = T.load_scenario(T.SCENARIO_DIR / "834-horizon-extends-with-solar-periods.json")
    zone = ZoneInfo(base.time_zone)
    now = datetime(2026, 11, 10, 11, 0)
    periods = tuple(
        SolarForecastPeriod(
            datetime(2026, 11, 10, 8 + i, tzinfo=zone),
            datetime(2026, 11, 10, 9 + i, tzinfo=zone),
            kwh,
        )
        for i, kwh in enumerate(BELL)
    )
    day = datetime(2026, 11, 10)
    prices = tuple(
        PriceSlot(day + timedelta(hours=h), day + timedelta(hours=h + 1), 0.40 + 0.01 * (h % 5))
        for h in range(0, 24)
    )
    return dataclasses.replace(
        base,
        now=now,
        slots=prices + base.slots,
        solar_periods=periods + base.solar_periods,
        solar_remaining_kwh=3.9,  # 11:00-16:00 of the bell
        fleet=({**base.fleet[0], "soc": 60},),
        settings={**base.settings, "safety_margin_kwh": safety},
    )


def _build(scenario, monkeypatch):
    ctrl = T._controller(scenario)
    ctrl._consumption_tracker = _Tracker(scenario)
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone=scenario.time_zone),
        states=SimpleNamespace(get=lambda _e: None),
    )
    ctrl.hass = hass
    mgr = PricingManager(hass, ctrl)
    ctrl._pricing_mgr = mgr
    T._pin_clock(monkeypatch, mgr, scenario)

    async def _no_io(*_a, **_k):
        return None

    solar_input = SolarForecastInput(
        scenario.solar_remaining_kwh,
        "remaining_sensor",
        periods=scenario.solar_periods or None,
        original_source="remaining",
    )
    mgr._raw_price_slots = lambda *, quiet=False: (list(scenario.slots), "no_slots")
    mgr._read_remaining_solar_input = lambda **_k: solar_input
    mgr._maybe_refresh_service_prices = _no_io
    mgr._send_dynamic_pricing_notification = _no_io
    ctrl.discharge_reserve_enabled = False
    ctrl.solar_forecast_periods = tuple(scenario.solar_periods)

    def fake_read(_hass, controller, *, update_controller=True):
        fc = SolarForecast(
            scenario.solar_remaining_kwh, "remaining", "sensor.fc_remaining",
            periods=tuple(scenario.solar_periods), conversion="none",
        )
        if update_controller:
            controller.solar_forecast_periods = fc.periods
        return fc

    monkeypatch.setattr(pricing_engine, "read_solar_forecast_kwh", fake_read, raising=True)
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", ZoneInfo(scenario.time_zone))
    return ctrl, mgr


def _claim(ctrl, kwh):
    ctrl._external_loads = SimpleNamespace(claimable_solar_demand_kwh=lambda: kwh)


def test_projection_matches_the_planners_request(monkeypatch):
    scenario = _sunny_scenario()
    ctrl, mgr = _build(scenario, monkeypatch)
    requests = []
    evaluate = mgr.evaluate_chronological_projection

    def _recording(request):
        requests.append(request)
        return evaluate(request)

    mgr.evaluate_chronological_projection = _recording
    asyncio.run(
        mgr._evaluate_dynamic_pricing(horizon=DynamicPricingEvaluationHorizon.REMAINING)
    )
    request = requests[0]
    projection = mgr.energy_projection(scenario.now, request.horizon_end)
    assert projection is not None
    assert len(projection.boundaries) == len(request.intervals)
    assert sum(projection.solar_kwh) > 1.0
    for index, interval in enumerate(request.intervals):
        assert projection.solar_kwh[index] == pytest.approx(interval.solar_kwh, abs=1e-6)
    # Uncapped consumption: the request's is capped to the battery's delivery.
    assert sum(projection.consumption_kwh) >= sum(i.consumption_kwh for i in request.intervals) - 1e-6
    assert projection.solar_source


def test_excluded_claim_reduces_todays_solar(monkeypatch):
    scenario = _sunny_scenario()
    ctrl, mgr = _build(scenario, monkeypatch)
    horizon_end = mgr.energy_horizon_end(scenario.now)
    base = mgr.energy_projection(scenario.now, horizon_end)
    _claim(ctrl, 0.6)
    claimed = mgr.energy_projection(scenario.now, horizon_end)
    assert sum(base.solar_kwh) - sum(claimed.solar_kwh) == pytest.approx(0.6, abs=1e-6)


def test_projection_is_none_without_a_forecast(monkeypatch):
    scenario = _sunny_scenario()
    ctrl, mgr = _build(scenario, monkeypatch)
    ctrl._consumption_tracker.forecast_consumption_between = lambda *a, **k: None
    assert mgr.energy_projection(scenario.now, mgr.energy_horizon_end(scenario.now)) is None


def _hps_solar_total(scenario, mgr, ctrl):
    ctrl.high_price_discharge_enabled = True
    manager = HighPriceDischargeManager(ctrl.hass, ctrl)
    manager._now = lambda: scenario.now
    horizon_end = mgr.energy_horizon_end(scenario.now)
    slots = manager._build_horizon(mgr, scenario.now, horizon_end)
    assert slots
    return sum(slot.solar_kwh for slot in slots), slots, horizon_end


def test_high_price_sale_solar_drops_by_the_excluded_claim(monkeypatch):
    scenario = _sunny_scenario()
    ctrl, mgr = _build(scenario, monkeypatch)
    base, _slots, _end = _hps_solar_total(scenario, mgr, ctrl)
    _claim(ctrl, 0.6)
    claimed, _slots, _end = _hps_solar_total(scenario, mgr, ctrl)
    assert base - claimed == pytest.approx(0.6, abs=1e-6)


def test_high_price_sale_solar_follows_the_planners_safety_margin(monkeypatch):
    plain = _sunny_scenario()
    ctrl, mgr = _build(plain, monkeypatch)
    base, _slots, horizon_end = _hps_solar_total(plain, mgr, ctrl)
    ctrl._predictive_safety_margin_kwh = 0.5
    margin, _slots, _end = _hps_solar_total(plain, mgr, ctrl)
    assert base - margin > 0.1
    # The planner's own projection loses the same energy over the same horizon.
    projection_margin = sum(mgr.energy_projection(plain.now, horizon_end).solar_kwh)
    ctrl._predictive_safety_margin_kwh = 0.0
    projection_base = sum(mgr.energy_projection(plain.now, horizon_end).solar_kwh)
    assert base - margin == pytest.approx(projection_base - projection_margin, abs=1e-6)


def test_high_price_sale_slot_solar_is_the_projection_over_the_slot(monkeypatch):
    scenario = _sunny_scenario()
    ctrl, mgr = _build(scenario, monkeypatch)
    from custom_components.omnibattery.pricing.plan_inputs import energy_over

    _total, slots, horizon_end = _hps_solar_total(scenario, mgr, ctrl)
    projection = mgr.energy_projection(scenario.now, horizon_end)
    zone = ZoneInfo(scenario.time_zone)
    for slot in slots:
        if slot.export_price is None:
            assert slot.solar_kwh == 0.0
            continue
        start = slot.start.astimezone(zone).replace(tzinfo=None)
        end = slot.end.astimezone(zone).replace(tzinfo=None)
        assert slot.solar_kwh == pytest.approx(
            energy_over(projection.boundaries, projection.solar_kwh, start, end), abs=1e-6
        )
