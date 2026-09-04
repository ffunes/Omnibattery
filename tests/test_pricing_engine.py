"""Characterization tests for PricingManager (module-8 PR3).

These pin the *current* behavior of the runtime pricing engine extracted from
``ChargeDischargeController`` so the move to ``pricing/engine.py`` is proven
cero-cambio-funcional. Runtime state stays on the controller by reference; the
manager reads/writes it via ``self._controller`` (matching the production wiring
where ``sensor.py`` / ``binary_sensor.py`` and the PD control loop also touch it).

No hardware, no running Home Assistant. ``PricingManager.__init__`` only stores
``hass``/``controller`` references, so it is built directly with a SimpleNamespace
hass and a stub controller. Tests cover the pure / early-return branches that need
no ``hass`` and no time mocking.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.button import ReevaluateDynamicPricingButton
from custom_components.omnibattery.const import (
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    CONF_SOLAR_FORECAST_REMAINING_SENSOR,
    PRICE_INTEGRATION_CKW,
    PRICE_INTEGRATION_NORDPOOL,
    PRICE_INTEGRATION_TIBBER,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
    PREDICTIVE_MODE_TIME_SLOT,
)
from custom_components.omnibattery.pricing import (
    BatterySnapshot,
    CurtailmentPlan,
    PreDischargeSlot,
    PriceSlot,
)
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
    _apply_excluded_demand_claim,
)
from custom_components.omnibattery.tracking.consumption_profile import (
    ConsumptionForecast,
    INTERVAL_COUNT,
)
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput,
    SolarForecastPeriod,
)
from custom_components.omnibattery.pricing.nordpool import OfficialNordPoolSource
from custom_components.omnibattery.pricing.curtailment import (
    EXPORT_MODE_AUTOMATIC,
    EXPORT_MODE_CUSTOM,
    EXPORT_MODE_SELF_CONSUMPTION,
)


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------

def _controller(**overrides):
    """Stub controller exposing only the state/collaborators the manager reads.
    ``_removed`` / ``_set`` record discharge-block calls so tests can assert which
    branch of ``apply_price_discharge_block`` ran."""
    removed: list = []
    set_calls: list = []

    base = dict(
        # discharge-block recorders
        remove_discharge_block=lambda source: removed.append(source),
        set_discharge_block=lambda source, reason, details=None: set_calls.append(
            (source, reason, details)
        ),
        _price_based_discharge_blocked=False,
        # pricing state
        _dynamic_pricing_schedule=None,
        _dynamic_pricing_evaluated_date=None,
        _dp_evening_reevaluated_date=None,
        _dp_daily_avg_price=None,
        # config defaults (DP discharge-control path)
        predictive_charging_mode=PREDICTIVE_MODE_TIME_SLOT,
        dp_price_discharge_control=False,
        rt_price_discharge_control=False,
        price_sensor=None,
        price_integration_type=PRICE_INTEGRATION_NORDPOOL,
        max_price_threshold=None,
        discharge_price_threshold=None,
        min_arbitrage_margin=None,
        round_trip_efficiency=DEFAULT_ROUND_TRIP_EFFICIENCY,
        average_price_sensor=None,
    )
    base.update(overrides)
    ctrl = SimpleNamespace(**base)
    ctrl._removed = removed
    ctrl._set = set_calls
    return ctrl


def _mgr(ctrl):
    return PricingManager(SimpleNamespace(), ctrl)


def _schedule(slots):
    """Minimal schedule stand-in: only ``selected_slots`` is read here."""
    return SimpleNamespace(selected_slots=slots)


# ----------------------------------------------------------------------
# _get_price_unit
# ----------------------------------------------------------------------

def test_price_unit_ckw_is_chf():
    assert _mgr(_controller(price_integration_type=PRICE_INTEGRATION_CKW))._get_price_unit() == "CHF/kWh"


def test_price_unit_default_is_eur():
    assert _mgr(_controller(price_integration_type=PRICE_INTEGRATION_NORDPOOL))._get_price_unit() == "€/kWh"


def test_price_unit_uses_configured_nordpool_currency():
    state = SimpleNamespace(attributes={"unit_of_measurement": "SEK/kWh"})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state))
    ctrl = _controller(
        price_integration_type=PRICE_INTEGRATION_NORDPOOL,
        price_sensor="sensor.nord_pool_se3_current_price",
    )

    assert PricingManager(hass, ctrl)._get_price_unit() == "SEK/kWh"


def test_hacs_nordpool_current_price_and_unit_are_normalized_from_cents():
    state = SimpleNamespace(
        state="12.5",
        attributes={
            "price_in_cents": True,
            "unit": "kWh",
            "currency": "EUR",
            "unit_of_measurement": "c/kWh",
            "raw_today": [],
        },
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state))
    ctrl = _controller(
        price_integration_type=PRICE_INTEGRATION_NORDPOOL,
        price_sensor="sensor.nordpool_kwh_nl_eur",
    )
    manager = PricingManager(hass, ctrl)

    assert manager._get_current_price() == 0.125
    assert manager._get_price_unit() == "€/kWh"


def test_future_price_slots_reuses_parse_until_source_or_quarter_changes():
    state = SimpleNamespace(last_updated=datetime(2026, 8, 30, 0, 0))
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state))
    ctrl = _controller(price_sensor="sensor.nordpool")
    manager = PricingManager(hass, ctrl)
    current = datetime(2026, 8, 30, 0, 5)
    manager._now = lambda: current
    slot = PriceSlot(current, current + timedelta(minutes=15), 0.1)
    calls = []

    def parse_slots(*, horizon_end=None, quiet=False):
        calls.append((horizon_end, quiet))
        return [slot]

    manager._parse_price_data = parse_slots

    assert manager.get_future_price_slots() == [slot]
    assert manager.get_future_price_slots() == [slot]
    assert calls == [(None, True)]

    state.last_updated += timedelta(seconds=1)
    assert manager.get_future_price_slots() == [slot]
    current += timedelta(minutes=15)
    assert manager.get_future_price_slots() == [slot]
    assert calls == [(None, True), (None, True), (None, True)]


# ----------------------------------------------------------------------
# is_in_dynamic_pricing_slot
# ----------------------------------------------------------------------

def test_in_slot_false_when_no_schedule():
    assert _mgr(_controller()).is_in_dynamic_pricing_slot() is False


def test_in_slot_true_when_now_inside_a_slot():
    now = datetime.now()
    slot = PriceSlot(start=now - timedelta(minutes=30), end=now + timedelta(minutes=30), price=0.1)
    ctrl = _controller(_dynamic_pricing_schedule=_schedule([slot]))
    assert _mgr(ctrl).is_in_dynamic_pricing_slot() is True


def test_in_slot_false_when_slot_in_the_past():
    now = datetime.now()
    slot = PriceSlot(start=now - timedelta(hours=2), end=now - timedelta(hours=1), price=0.1)
    ctrl = _controller(_dynamic_pricing_schedule=_schedule([slot]))
    assert _mgr(ctrl).is_in_dynamic_pricing_slot() is False


# ----------------------------------------------------------------------
# evaluation-time guards (deterministic "already done today" branch)
# ----------------------------------------------------------------------

def test_evening_reeval_false_when_already_done_today():
    ctrl = _controller(_dp_evening_reevaluated_date=datetime.now().date())
    assert _mgr(ctrl)._is_evening_reevaluation_time() is False


def test_daily_dynamic_pricing_uses_persisted_remaining_sensor_when_cache_is_empty():
    """The 00:05 evaluation must not fall back to the legacy daily balance."""
    calls = []

    async def daily_decision():
        calls.append("daily")
        return {
            "should_charge": False,
            "avg_soc": 50.0,
            "energy_deficit_kwh": 0.0,
            "avg_consumption_kwh": 0.0,
        }

    async def remaining_decision(*, now=None):
        calls.append("remaining")
        return {
            "should_charge": False,
            "avg_soc": 50.0,
            "energy_deficit_kwh": 0.0,
            "avg_consumption_kwh": 0.0,
        }

    async def no_op(*_args, **_kwargs):
        return None

    ctrl = _controller(
        config_entry=SimpleNamespace(
            data={CONF_SOLAR_FORECAST_REMAINING_SENSOR: "sensor.remaining"},
            options={},
        ),
        solar_forecast_remaining_sensor=None,
        solar_forecast_sensor=None,
        _should_activate_grid_charging=daily_decision,
        _dp_eval_retry_count=0,
    )
    manager = _mgr(ctrl)
    manager._evaluate_remaining_grid_charging = remaining_decision
    manager._maybe_refresh_service_prices = no_op
    manager._parse_price_data = lambda horizon_end=None: []
    manager._build_curtailment_plan = lambda *_args, **_kwargs: CurtailmentPlan(
        status="no_risk", reason="none"
    )
    manager._send_dynamic_pricing_notification = no_op
    diagnostic_builds = []

    def build_diagnostics(**kwargs):
        diagnostic_builds.append(kwargs)
        return None

    manager._build_chronological_plan = build_diagnostics

    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.DAILY,
        )
    )

    assert calls == ["remaining"]
    assert len(diagnostic_builds) == 1
    assert diagnostic_builds[0]["diagnostic_only"] is True
    assert diagnostic_builds[0]["slots"] == []


def test_dynamic_pricing_builds_diagnostics_when_balance_needs_no_charge():
    async def no_charge_decision():
        return {
            "should_charge": False,
            "avg_soc": 80.0,
            "energy_deficit_kwh": 0.0,
            "avg_consumption_kwh": 0.0,
        }

    async def no_op(*_args, **_kwargs):
        return None

    start = datetime.now() + timedelta(hours=1)
    slots = [PriceSlot(start=start, end=start + timedelta(hours=1), price=0.1)]
    ctrl = _controller(
        config_entry=SimpleNamespace(data={}, options={}),
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        max_contracted_power=7000,
        max_charge_capacity=1200,
        _should_activate_grid_charging=no_charge_decision,
        _dp_eval_retry_count=0,
    )
    manager = _mgr(ctrl)
    manager._maybe_refresh_service_prices = no_op
    manager._parse_price_data = lambda horizon_end=None: slots
    manager._build_curtailment_plan = lambda *_args, **_kwargs: CurtailmentPlan(
        status="no_risk", reason="none"
    )
    manager._send_dynamic_pricing_notification = no_op
    diagnostic_builds = []

    def build_diagnostics(**kwargs):
        diagnostic_builds.append(kwargs)
        kwargs["decision_data"]["chronological_planning_active"] = False
        return None

    manager._build_chronological_plan = build_diagnostics

    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.DAILY,
        )
    )

    assert len(diagnostic_builds) == 1
    assert diagnostic_builds[0]["diagnostic_only"] is True
    assert diagnostic_builds[0]["slots"] == slots


# ----------------------------------------------------------------------
# _is_dp_soc_drop_reeval (SOC-drop upward re-eval, #411)
# ----------------------------------------------------------------------

def _coord(soc):
    """Coordinator stand-in exposing only ``data['battery_soc']``."""
    return SimpleNamespace(data={"battery_soc": soc})


def test_soc_drop_reeval_false_when_no_reference():
    # Before the 00:05 eval sets a reference, the trigger never fires.
    ctrl = _controller(_dp_last_eval_soc=None, coordinators=[_coord(20)])
    assert _mgr(ctrl)._is_dp_soc_drop_reeval() is False


def test_soc_drop_reeval_true_on_large_drop():
    # Reporter's case: eval'd at 60%, woke to 24% → 36% drop ≥ 30% threshold.
    ctrl = _controller(_dp_last_eval_soc=60.0, coordinators=[_coord(24)])
    assert _mgr(ctrl)._is_dp_soc_drop_reeval() is True


def test_soc_drop_reeval_false_below_threshold():
    # 60 → 40 is a 20% drop, under the 30% threshold.
    ctrl = _controller(_dp_last_eval_soc=60.0, coordinators=[_coord(40)])
    assert _mgr(ctrl)._is_dp_soc_drop_reeval() is False


def test_soc_drop_reeval_false_on_soc_rise():
    # Directional: a rise (charged up) never triggers an upward re-plan.
    ctrl = _controller(_dp_last_eval_soc=30.0, coordinators=[_coord(70)])
    assert _mgr(ctrl)._is_dp_soc_drop_reeval() is False


def test_soc_drop_reeval_false_when_no_coordinator_data():
    ctrl = _controller(_dp_last_eval_soc=60.0, coordinators=[SimpleNamespace(data=None)])
    assert _mgr(ctrl)._is_dp_soc_drop_reeval() is False


# ----------------------------------------------------------------------
# _apply_excluded_demand_claim (today-only solar reservation, #341)
# ----------------------------------------------------------------------

def _quarters(start, count):
    return [
        (start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)))
        for i in range(count)
    ]


_TODAY_END = datetime(2026, 8, 29, 0, 0)


def test_excluded_claim_scales_today_intervals_proportionally():
    boundaries = _quarters(datetime(2026, 8, 28, 12, 0), 4)
    solar, applied = _apply_excluded_demand_claim(
        boundaries, [1.0, 2.0, 1.0, 0.0], 2.0, _TODAY_END
    )

    assert applied == pytest.approx(2.0)
    assert sum(solar) == pytest.approx(2.0)
    assert solar == pytest.approx([0.5, 1.0, 0.5, 0.0])


def test_excluded_claim_is_capped_at_todays_solar():
    boundaries = _quarters(datetime(2026, 8, 28, 12, 0), 2)
    solar, applied = _apply_excluded_demand_claim(
        boundaries, [1.0, 1.0], 10.0, _TODAY_END
    )

    assert applied == pytest.approx(2.0)
    assert sum(solar) == pytest.approx(0.0)


def test_excluded_claim_never_touches_tomorrows_forecast():
    # A cross-midnight projection also carries tomorrow's forecast. The sensor
    # reports demand remaining *today*, so those intervals must stay intact.
    boundaries = _quarters(datetime(2026, 8, 28, 23, 30), 4)
    solar, applied = _apply_excluded_demand_claim(
        boundaries, [1.0, 1.0, 5.0, 5.0], 4.0, _TODAY_END
    )

    assert applied == pytest.approx(2.0)
    assert solar[:2] == pytest.approx([0.0, 0.0])
    assert solar[2:] == pytest.approx([5.0, 5.0])


def test_excluded_claim_without_solar_today_changes_nothing():
    boundaries = _quarters(datetime(2026, 8, 28, 23, 30), 4)
    intervals = [0.0, 0.0, 5.0, 5.0]
    solar, applied = _apply_excluded_demand_claim(
        boundaries, intervals, 4.0, _TODAY_END
    )

    assert applied == 0.0
    assert solar == intervals


def test_excluded_claim_of_zero_returns_the_intervals_untouched():
    boundaries = _quarters(datetime(2026, 8, 28, 12, 0), 2)
    intervals = [1.0, 2.0]
    solar, applied = _apply_excluded_demand_claim(
        boundaries, intervals, 0.0, _TODAY_END
    )

    assert applied == 0.0
    assert solar is intervals


# ----------------------------------------------------------------------
# _is_excluded_demand_reeval (excluded-device solar claim, #341)
# ----------------------------------------------------------------------

def _claim_ctrl(reference, current, **overrides):
    """Controller stub for the claim-driven re-evaluation predicate."""
    loads = SimpleNamespace(claimable_solar_demand_kwh=lambda: current)
    base = dict(
        _dp_last_eval_excluded_claim_kwh=reference,
        _external_loads=loads,
        _last_decision_data={},
        _dp_excluded_demand_reeval_at=None,
        _dp_excluded_demand_reeval_count=0,
    )
    base.update(overrides)
    return _controller(**base)


def _claim_mgr(ctrl, remaining_solar=12.0):
    """Manager whose live remaining-solar read is stubbed out."""
    manager = _mgr(ctrl)
    manager._remaining_solar_today_kwh = lambda _now: remaining_solar
    return manager


_CLAIM_NOW = datetime(2026, 8, 28, 12, 0)


def test_excluded_demand_reeval_false_before_first_evaluation():
    ctrl = _claim_ctrl(None, 7.0)
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(_CLAIM_NOW) is False


def test_excluded_demand_reeval_true_when_a_session_starts():
    # Nothing claimed at 00:05, 7 kWh claimed once the car is plugged in.
    ctrl = _claim_ctrl(0.0, 7.0)
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(_CLAIM_NOW) is True


def test_excluded_demand_reeval_true_when_a_session_ends():
    # Bidirectional: the released solar must go back to the battery plan.
    ctrl = _claim_ctrl(7.0, 0.0)
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(_CLAIM_NOW) is True


def test_excluded_demand_reeval_false_below_threshold():
    ctrl = _claim_ctrl(7.0, 5.5)
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(_CLAIM_NOW) is False


def test_excluded_demand_reeval_false_when_sensor_unavailable():
    ctrl = _claim_ctrl(7.0, None)
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(_CLAIM_NOW) is False


def test_excluded_demand_reeval_false_without_external_loads():
    ctrl = _claim_ctrl(0.0, 7.0, _external_loads=None)
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(_CLAIM_NOW) is False


def test_excluded_demand_reeval_respects_cooldown():
    ctrl = _claim_ctrl(
        0.0, 7.0,
        _dp_excluded_demand_reeval_at=_CLAIM_NOW - timedelta(minutes=5),
    )
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(_CLAIM_NOW) is False

    ctrl._dp_excluded_demand_reeval_at = _CLAIM_NOW - timedelta(minutes=20)
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(_CLAIM_NOW) is True


def test_excluded_demand_reeval_respects_daily_cap():
    ctrl = _claim_ctrl(0.0, 7.0, _dp_excluded_demand_reeval_count=4)
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(_CLAIM_NOW) is False


def test_excluded_demand_reeval_false_without_remaining_solar():
    # After sundown there is nothing left to reserve or release. The guard reads
    # the live remaining forecast, not the stored full-day figure.
    ctrl = _claim_ctrl(0.0, 7.0)
    manager = _claim_mgr(ctrl, remaining_solar=0.0)
    assert manager._is_excluded_demand_reeval(_CLAIM_NOW) is False


def test_excluded_demand_reeval_compares_raw_readings():
    # The stored reference is the raw reading, so a device asking for more than
    # the forecast can deliver does not re-trigger on every cycle.
    ctrl = _claim_ctrl(20.0, 20.0)
    manager = _claim_mgr(ctrl, remaining_solar=5.0)
    assert manager._is_excluded_demand_reeval(_CLAIM_NOW) is False


# ----------------------------------------------------------------------
# _is_solar_forecast_reeval (provider revises the remaining forecast)
# ----------------------------------------------------------------------

def _forecast_ctrl(reference, **overrides):
    """Controller stub for the forecast-driven re-evaluation predicate."""
    base = dict(
        _dp_last_eval_solar_remaining_kwh=reference,
        _last_decision_data={},
        _dp_solar_forecast_reeval_at=None,
        _dp_solar_forecast_reeval_count=0,
    )
    base.update(overrides)
    return _controller(**base)


def _forecast_mgr(ctrl, current):
    manager = _mgr(ctrl)
    manager._remaining_solar_today_kwh = lambda _now: current
    return manager


def test_solar_forecast_reeval_false_before_first_evaluation():
    ctrl = _forecast_ctrl(None)
    assert _forecast_mgr(ctrl, 4.0)._is_solar_forecast_reeval(_CLAIM_NOW) is False


def test_solar_forecast_reeval_true_when_the_day_turns_out_cloudy():
    # Planned on 13.1 kWh, provider now says 4.1: the deficit the plan denied
    # is real, and only a re-plan can still book cheap midday slots.
    ctrl = _forecast_ctrl(13.1)
    assert _forecast_mgr(ctrl, 4.1)._is_solar_forecast_reeval(_CLAIM_NOW) is True


def test_solar_forecast_reeval_true_when_the_day_clears_up():
    ctrl = _forecast_ctrl(4.1)
    assert _forecast_mgr(ctrl, 13.1)._is_solar_forecast_reeval(_CLAIM_NOW) is True


def test_solar_forecast_reeval_false_below_threshold():
    ctrl = _forecast_ctrl(6.0)
    assert _forecast_mgr(ctrl, 5.0)._is_solar_forecast_reeval(_CLAIM_NOW) is False


def test_solar_forecast_reeval_false_late_in_the_evening():
    # The 00:05 evaluation is close enough; don't clash with it.
    ctrl = _forecast_ctrl(13.1)
    manager = _forecast_mgr(ctrl, 0.0)
    assert manager._is_solar_forecast_reeval(datetime(2026, 8, 28, 23, 10)) is False


def test_solar_forecast_reeval_respects_cooldown():
    ctrl = _forecast_ctrl(
        13.1,
        _dp_solar_forecast_reeval_at=_CLAIM_NOW - timedelta(minutes=10),
    )
    assert _forecast_mgr(ctrl, 4.1)._is_solar_forecast_reeval(_CLAIM_NOW) is False

    ctrl._dp_solar_forecast_reeval_at = _CLAIM_NOW - timedelta(minutes=45)
    assert _forecast_mgr(ctrl, 4.1)._is_solar_forecast_reeval(_CLAIM_NOW) is True


def test_solar_forecast_reeval_respects_daily_cap():
    ctrl = _forecast_ctrl(13.1, _dp_solar_forecast_reeval_count=4)
    assert _forecast_mgr(ctrl, 4.1)._is_solar_forecast_reeval(_CLAIM_NOW) is False


def test_refresh_solar_forecast_reference_stores_the_live_remainder():
    ctrl = _forecast_ctrl(None)
    _forecast_mgr(ctrl, 4.1)._refresh_solar_forecast_reference(_CLAIM_NOW)
    assert ctrl._dp_last_eval_solar_remaining_kwh == 4.1


def test_refresh_solar_forecast_reference_disarms_the_trigger():
    ctrl = _forecast_ctrl(13.1)
    manager = _forecast_mgr(ctrl, 4.1)
    assert manager._is_solar_forecast_reeval(_CLAIM_NOW) is True
    manager._refresh_solar_forecast_reference(_CLAIM_NOW)
    assert manager._is_solar_forecast_reeval(_CLAIM_NOW) is False


def test_refresh_excluded_demand_reference_stores_the_raw_reading():
    ctrl = _claim_ctrl(None, 7.0)
    _claim_mgr(ctrl)._refresh_excluded_demand_reference()
    assert ctrl._dp_last_eval_excluded_claim_kwh == 7.0


def test_refresh_excluded_demand_reference_keeps_the_old_value_on_unavailable():
    # A blip must not reset the reference to 0: the sensor coming back at its
    # old value would then read as a full-value move and fire a spurious re-plan.
    ctrl = _claim_ctrl(4.0, None)
    _claim_mgr(ctrl)._refresh_excluded_demand_reference()
    assert ctrl._dp_last_eval_excluded_claim_kwh == 4.0


def test_refresh_excluded_demand_reference_leaves_an_unset_reference_alone():
    ctrl = _claim_ctrl(None, None)
    _claim_mgr(ctrl)._refresh_excluded_demand_reference()
    assert ctrl._dp_last_eval_excluded_claim_kwh is None


def test_excluded_demand_reeval_false_late_at_night():
    # 23:00 onward the 00:05 evaluation is close enough.
    ctrl = _claim_ctrl(0.0, 7.0)
    late = _CLAIM_NOW.replace(hour=23, minute=10)
    assert _claim_mgr(ctrl)._is_excluded_demand_reeval(late) is False


# ----------------------------------------------------------------------
# _evaluate_evening_recharge (excluded-device claim + published figures, #341)
# ----------------------------------------------------------------------

def _evening_ctrl(claim, remaining_solar, **overrides):
    async def _avg():
        return 8.0

    tracker = SimpleNamespace(
        get_dynamic_base_consumption=_avg,
        get_consumption_window_hours_per_day=lambda: 24.0,
        consumption_window_hours_in_range=lambda _a, _b: 6.0,
    )
    base = dict(
        coordinators=[
            SimpleNamespace(
                data={"battery_soc": 20.0, "battery_total_energy": 10.0},
                max_soc=100,
                min_soc=10,
            )
        ],
        _consumption_tracker=tracker,
        _last_decision_data={
            "solar_remaining_raw_kwh": 30.0,
            "solar_safety_margin_kwh": 2.0,
            "solar_remaining_effective_kwh": 28.0,
            "excluded_demand_claim_kwh": 0.0,
            "solar_available_to_battery_kwh": 28.0,
        },
        _dp_last_eval_soc=None,
        _dp_last_eval_excluded_claim_kwh=None,
        _external_loads=SimpleNamespace(claimable_solar_demand_kwh=lambda: claim),
        _predictive_grid_charge_margin_pct=0.0,
        _daily_home_energy_date=None,
        _daily_home_energy_kwh=0.0,
        _household_accumulator_date=None,
        _household_energy_accumulator=0.0,
    )
    base.update(overrides)
    return _controller(**base)


def _evening_mgr(ctrl, remaining_solar):
    manager = _mgr(ctrl)

    async def _noop_prices(force=False):
        return None

    manager._maybe_refresh_service_prices = _noop_prices
    manager._remaining_solar_today_kwh = lambda _now: remaining_solar
    manager._profile_remaining_consumption = lambda _start, _end: None
    manager._project_remaining_consumption = lambda *a, **k: (4.0, 0.6)
    return manager


def test_evening_recharge_deducts_the_claim_from_remaining_solar():
    # 6 kWh of solar left, 4 kWh of it claimed by the EV, 4 kWh of house load
    # and 1 kWh usable in the battery: a real deficit the morning plan reserved
    # for. Ignoring the claim would report solar covering the evening.
    ctrl = _evening_ctrl(claim=4.0, remaining_solar=6.0)
    asyncio.run(_evening_mgr(ctrl, 6.0)._evaluate_evening_recharge())

    decision = ctrl._last_decision_data
    assert decision["excluded_demand_claim_kwh"] == pytest.approx(4.0)
    assert decision["solar_available_to_battery_kwh"] == pytest.approx(2.0)


def test_evening_recharge_publishes_the_figures_it_used():
    # The published solar numbers must describe this horizon, not still report
    # the morning evaluation's 30 / 28 kWh.
    ctrl = _evening_ctrl(claim=4.0, remaining_solar=6.0)
    asyncio.run(_evening_mgr(ctrl, 6.0)._evaluate_evening_recharge())

    decision = ctrl._last_decision_data
    assert decision["solar_remaining_raw_kwh"] == pytest.approx(6.0)
    assert decision["solar_remaining_effective_kwh"] == pytest.approx(6.0)
    assert decision["solar_safety_margin_kwh"] == pytest.approx(0.0)
    assert (
        decision["solar_remaining_effective_kwh"]
        - decision["excluded_demand_claim_kwh"]
        == pytest.approx(decision["solar_available_to_battery_kwh"])
    )


def test_evening_recharge_claim_is_capped_at_remaining_solar():
    ctrl = _evening_ctrl(claim=50.0, remaining_solar=6.0)
    asyncio.run(_evening_mgr(ctrl, 6.0)._evaluate_evening_recharge())

    decision = ctrl._last_decision_data
    assert decision["excluded_demand_claim_kwh"] == pytest.approx(6.0)
    assert decision["solar_available_to_battery_kwh"] == pytest.approx(0.0)


def test_evening_recharge_rearms_the_claim_reference():
    ctrl = _evening_ctrl(claim=4.0, remaining_solar=6.0)
    asyncio.run(_evening_mgr(ctrl, 6.0)._evaluate_evening_recharge())

    assert ctrl._dp_last_eval_excluded_claim_kwh == pytest.approx(4.0)


# ----------------------------------------------------------------------
# _project_remaining_consumption (evening recharge deficit, #409)
# ----------------------------------------------------------------------

def test_remaining_consumption_keeps_historical_remainder_when_larger():
    # 18:00, 12 kWh used so far.  The historical 20 kWh average still has
    # 8 kWh unspent, more than its normal 5 kWh time-prorated remainder.
    remaining, rate = PricingManager._project_remaining_consumption(18.0, 12.0, 20.0)
    assert round(rate, 3) == 0.833
    assert round(remaining, 2) == 8.0


def test_remaining_consumption_uses_normal_remainder_after_heavy_day():
    # A heavy morning that has already passed the daily average must keep the
    # normal historical remainder, not project the morning spike until midnight.
    heavy, _ = PricingManager._project_remaining_consumption(18.0, 18.0, 17.0)
    assert heavy == pytest.approx(4.25)


def test_remaining_consumption_cold_accumulator_uses_avg_rate():
    # A cold accumulator after restart cannot subtract today's consumption, so
    # project the historical hourly average over the hours that remain.
    remaining, rate = PricingManager._project_remaining_consumption(
        18.0, 0.0, 24.0, accumulator_ready=False
    )
    assert rate == 1.0                  # 24 kWh / 24 h
    assert round(remaining, 2) == 6.0   # 1.0 × 6 h


def test_remaining_consumption_zero_at_midnight():
    remaining, _ = PricingManager._project_remaining_consumption(24.0, 20.0, 20.0)
    assert remaining == 0.0


def test_remaining_consumption_discussion_263_midday_baseline():
    # Discussion #263: at noon, 1.2 kWh already consumed from a 5.8 kWh daily
    # average leaves at least 4.6 kWh.  Its normal time-prorated remainder is
    # only 2.9 kWh, so the historical unspent energy must win.
    remaining, rate = PricingManager._project_remaining_consumption(12.0, 1.2, 5.8)
    assert rate == pytest.approx(5.8 / 24.0)
    assert remaining == pytest.approx(4.6)


def test_remaining_consumption_does_not_extrapolate_morning_spike():
    # Reported regression: at 07:47 the observed-rate projection turned an
    # 17.98 kWh daily average into 40.61 kWh remaining.  Once the day has already
    # exceeded its average, retain only the normal time-prorated remainder.
    now_h = 7.0 + 47.0 / 60.0
    consumed = 40.61 * now_h / (24.0 - now_h)
    remaining, rate = PricingManager._project_remaining_consumption(
        now_h, consumed, 17.98
    )
    assert remaining == pytest.approx(17.98 * (24.0 - now_h) / 24.0)
    assert remaining < 17.98
    assert rate == pytest.approx(17.98 / 24.0)


def test_remaining_consumption_respects_configured_consumption_window():
    remaining, rate = PricingManager._project_remaining_consumption(
        8.0,
        20.0,
        18.0,
        window_hours_per_day=18.0,
        remaining_window_hours=10.0,
    )
    assert remaining == pytest.approx(10.0)
    assert rate == pytest.approx(1.0)


def test_remaining_consumption_invalid_accumulator_date_uses_hourly_fallback():
    # A restored/stale accumulator from a previous day must not be subtracted
    # from today's average.  Its historical hourly fallback still covers the
    # hours remaining rather than dropping the estimate to zero.
    remaining, rate = PricingManager._project_remaining_consumption(
        12.0, 1.2, 5.8, accumulator_ready=False
    )
    assert rate == pytest.approx(5.8 / 24.0)
    assert remaining == pytest.approx(2.9)


def test_evening_recharge_uses_dynamic_base_consumption():
    """The evening path must resolve the tracker's async average locally."""
    calls = []

    async def get_average_consumption():
        calls.append(True)
        return 20.0

    async def no_op(*_args, **_kwargs):
        return None

    ctrl = _controller(
        coordinators=[
            SimpleNamespace(
                data={
                    "battery_soc": 0.0,
                    "battery_total_energy": 10.0,
                },
                min_soc=0.0,
                max_soc=100.0,
            )
        ],
        _consumption_tracker=SimpleNamespace(
            get_dynamic_base_consumption=get_average_consumption,
        ),
        _daily_home_energy_date=datetime.now().date(),
        _daily_home_energy_kwh=1.0,
        _last_decision_data={},
        _predictive_grid_charge_margin_pct=0.0,
    )
    manager = _mgr(ctrl)
    manager._maybe_refresh_service_prices = no_op
    manager._remaining_solar_today_kwh = lambda _now: 0.0
    manager._parse_price_data = lambda horizon_end=None: []

    asyncio.run(manager._evaluate_evening_recharge())

    assert calls == [True]


def test_pre_slot_reevaluation_uses_remaining_consumption_and_solar(monkeypatch):
    import asyncio

    now = datetime(2026, 8, 11, 12, 0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)

    async def get_average_consumption():
        return 20.0

    decision_calls = []

    async def should_activate(**overrides):
        decision_calls.append(overrides)
        return {"should_charge": False}

    slot = PriceSlot(
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=1, minutes=15),
        price=0.10,
    )
    schedule = SimpleNamespace(
        selected_slots=[slot],
        charging_needed=True,
        deficit_charging_needed=True,
        slot_purposes={slot: "deficit"},
    )
    ctrl = _controller(
        _dynamic_pricing_schedule=schedule,
        _dp_pre_evaluated_slots={},
        _dp_pre_evaluated_purposes={},
        _current_price_slot_active=False,
        _consumption_tracker=SimpleNamespace(
            get_dynamic_base_consumption=get_average_consumption,
        ),
        _household_accumulator_date=now.date(),
        _household_energy_accumulator=6.0,
        _should_activate_grid_charging=should_activate,
    )
    manager = _mgr(ctrl)
    manager._remaining_solar_today_kwh = lambda _now_h: 3.5

    asyncio.run(manager._check_dp_pre_slot_reevaluation())

    # 6 kWh used by noon leaves 14 kWh of the historical average.
    assert decision_calls == [{
        "consumption_override_kwh": 14.0,
        "solar_forecast_override_kwh": 3.5,
    }]
    assert ctrl._dp_pre_evaluated_slots[slot.start] is False
    assert ctrl._dp_pre_evaluated_purposes[slot.start] is None


def test_midday_calendar_rebuild_uses_remaining_consumption_and_solar(monkeypatch):
    """Manual rebuilds subtract the full-day home total from the forecast."""
    import asyncio

    now = datetime(2026, 8, 11, 12, 0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)

    async def get_average_consumption():
        return 5.8

    calls = []

    async def should_activate(**overrides):
        calls.append(overrides)
        return {"should_charge": False, "avg_soc": 50.0}

    async def no_op(*_args, **_kwargs):
        return None

    ctrl = _controller(
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        smart_predischarge_enabled=False,
        _dp_arbitrage_ceiling=None,
        _dp_last_eval_soc=None,
        _dp_eval_retry_count=0,
        _household_accumulator_date=now.date(),
        _household_energy_accumulator=0.2,
        _daily_home_energy_date=now.date(),
        _daily_home_energy_kwh=1.2,
        _consumption_tracker=SimpleNamespace(
            get_dynamic_base_consumption=get_average_consumption,
        ),
        _should_activate_grid_charging=should_activate,
    )
    manager = _mgr(ctrl)
    manager._maybe_refresh_service_prices = no_op
    manager._parse_price_data = lambda horizon_end=None: []
    manager._send_dynamic_pricing_notification = no_op
    manager._remaining_solar_today_kwh = lambda _now_h: 2.4

    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.REMAINING,
            extended_horizon=True,
        )
    )

    assert calls == [{
        "consumption_override_kwh": pytest.approx(4.6),
        "solar_forecast_override_kwh": 2.4,
    }]
    assert ctrl._last_decision_data["consumption_scope"] == "remaining"
    assert ctrl._last_decision_data["daily_avg_consumption_kwh"] == 5.8
    assert ctrl._last_decision_data["consumed_today_kwh"] == 1.2
    assert ctrl._last_decision_data["consumption_accumulator_source"] == "daily_home_energy"
    assert ctrl._last_decision_data["remaining_solar_kwh"] == 2.4


def test_remaining_fallback_is_conditioned_on_today_consumption():
    async def get_average_consumption():
        return 20.0

    calls = []

    async def should_activate(**overrides):
        calls.append(overrides)
        return {"should_charge": False}

    forecast = SimpleNamespace(
        energy_kwh=10.0,
        source="legacy_daily",
        coverage_ratio=0.0,
        total_days=2,
        fallback_reason="insufficient_days",
    )
    profile = SimpleNamespace(
        _timezone=lambda: None,
        forecast_energy_between=lambda *_args, **_kwargs: forecast,
    )
    now = datetime(2026, 8, 11, 12, 0)
    ctrl = _controller(
        _daily_home_energy_date=now.date(),
        _daily_home_energy_kwh=15.0,
        _consumption_tracker=SimpleNamespace(
            consumption_profile=profile,
            get_dynamic_base_consumption=get_average_consumption,
            # The forecast is requested from the tracker, not the profile.
            forecast_consumption_between=lambda *_args, **_kwargs: forecast,
        ),
        _should_activate_grid_charging=should_activate,
    )
    manager = _mgr(ctrl)
    manager._remaining_solar_today_kwh = lambda _now_h: 0.0

    decision = asyncio.run(manager._evaluate_remaining_grid_charging(now=now))

    assert calls == [{
        "consumption_override_kwh": pytest.approx(7.0),
        "solar_forecast_override_kwh": 0.0,
    }]
    assert decision["consumption_scope"] == "remaining_fallback"
    assert decision["consumption_fallback_correction_kwh"] == pytest.approx(-3.0)


def test_midnight_remaining_balance_uses_periods_when_scalar_is_zero():
    madrid = ZoneInfo("Europe/Madrid")
    now = datetime(2026, 8, 25, 0, 0, tzinfo=madrid)
    periods = [
        {
            "start": (now + timedelta(hours=8)).isoformat(),
            "end": (now + timedelta(hours=20)).isoformat(),
            "energy_kwh": 12.0,
        }
    ]
    state = SimpleNamespace(
        state="0",
        attributes={
            "unit_of_measurement": "kWh",
            "solar_forecast_periods": periods,
        },
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Madrid"),
        states=SimpleNamespace(get=lambda _entity_id: state),
    )
    calls = []

    async def get_average_consumption():
        return 6.0

    async def should_activate(**overrides):
        calls.append(overrides)
        return {
            "should_charge": False,
            "solar_forecast_kwh": overrides["solar_forecast_override_kwh"],
        }

    controller = _controller(
        solar_forecast_remaining_sensor="sensor.remaining",
        solar_forecast_sensor=None,
        _consumption_tracker=SimpleNamespace(
            consumption_profile=None,
            get_dynamic_base_consumption=get_average_consumption,
        ),
        _should_activate_grid_charging=should_activate,
    )
    manager = PricingManager(hass, controller)

    decision = asyncio.run(manager._evaluate_remaining_grid_charging(now=now))

    assert calls[0]["solar_forecast_override_kwh"] == pytest.approx(12.0)
    assert decision["remaining_solar_kwh"] == pytest.approx(12.0)
    assert decision["solar_forecast_conversion"] == "dated_periods_zero_scalar"


def test_manual_button_uses_remaining_horizon_at_midday():
    import asyncio

    calls = []

    class PricingStub:
        async def _evaluate_dynamic_pricing(self, *, horizon, extended_horizon=False):
            calls.append((horizon, extended_horizon))

    button = ReevaluateDynamicPricingButton(
        SimpleNamespace(_pricing_mgr=PricingStub())
    )
    asyncio.run(button.async_press())

    assert calls == [(DynamicPricingEvaluationHorizon.REMAINING, True)]


def test_startup_rebuild_uses_remaining_horizon(monkeypatch):
    import asyncio

    now = datetime(2026, 8, 11, 12, 0)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)
    monkeypatch.setattr(pricing_engine.asyncio, "sleep", no_sleep)
    calls = []

    async def evaluate(**kwargs):
        calls.append(kwargs)

    ctrl = _controller(
        _dynamic_pricing_evaluated_date=None,
        predictive_charging_enabled=True,
        coordinators=[SimpleNamespace(data={"battery_soc": 50.0})],
    )
    manager = _mgr(ctrl)
    manager._evaluate_dynamic_pricing = evaluate

    asyncio.run(manager.startup_evaluation())

    assert calls == [{
        "horizon": DynamicPricingEvaluationHorizon.REMAINING,
        "extended_horizon": True,
    }]


def test_price_retry_uses_remaining_horizon(monkeypatch):
    import asyncio

    now = datetime(2026, 8, 11, 0, 20)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pricing_engine, "datetime", FixedDateTime)
    calls = []

    async def evaluate(**kwargs):
        calls.append(kwargs)

    ctrl = _controller(
        _dynamic_pricing_evaluated_date=None,
        _dp_eval_retry_count=1,
    )
    manager = _mgr(ctrl)
    manager._maybe_refresh_service_prices = no_op
    manager._evaluate_dynamic_pricing = evaluate

    asyncio.run(manager.handle_dynamic_pricing_predictive_charging())

    assert calls == [{"horizon": DynamicPricingEvaluationHorizon.REMAINING}]


def test_energy_balance_accepts_remaining_horizon_overrides():
    import asyncio

    def fail_if_sensor_read(_entity_id):
        raise AssertionError("remaining-horizon evaluation must not read the daily forecast sensor")

    async def fail_if_average_read():
        raise AssertionError("remaining-horizon evaluation already supplied consumption")

    coordinator = SimpleNamespace(
        data={"battery_soc": 50.0, "battery_total_energy": 10.0},
        min_soc=10.0,
        max_soc=95.0,
    )
    ctrl = SimpleNamespace(
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        coordinators=[coordinator],
        _predictive_safety_margin_kwh=0.0,
        _predictive_grid_charge_margin_pct=0.0,
        _predictive_min_soc_floor=0.0,
        _predictive_min_soc_floor_enabled=False,
        _daily_consumption_history=[],
        solar_forecast_sensor="sensor.solar",
        hass=SimpleNamespace(
            states=SimpleNamespace(get=fail_if_sensor_read),
        ),
        _consumption_tracker=SimpleNamespace(
            get_dynamic_base_consumption=fail_if_average_read,
        ),
    )

    result = asyncio.run(
        ChargeDischargeController._should_activate_grid_charging(
            ctrl,
            consumption_override_kwh=3.0,
            solar_forecast_override_kwh=1.0,
        )
    )

    # 4 kWh usable + 1 kWh remaining solar covers the 3 kWh remaining load.
    assert result["should_charge"] is False
    assert result["avg_consumption_kwh"] == 3.0
    assert result["solar_forecast_kwh"] == 1.0
    assert result["consumption_scope"] == "remaining"


# ----------------------------------------------------------------------
# _remaining_solar_today_kwh (evening/SOC-drop recharge, pre-dawn blind spot)
# ----------------------------------------------------------------------

def _solar_ctrl(forecast="40.0", produced=0.0, t_start=None):
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: SimpleNamespace(state=forecast) if forecast is not None else None))
    ctrl = _controller(
        solar_forecast_sensor="sensor.solcast_today",
        _daily_solar_energy_kwh=produced,
        _solar_t_start=t_start,
        _consumption_tracker=SimpleNamespace(
            estimate_t_end=lambda: 21.0,
            get_solar_fraction_done=lambda now_h, t_start, t_end: 0.5,
        ),
    )
    return PricingManager(hass, ctrl)


def test_remaining_solar_predawn_uses_full_forecast():
    # #411 regression: SOC-drop re-eval fires pre-dawn (accumulator 0, no
    # T_start) → the whole forecast is still to come, not 0.
    assert _solar_ctrl()._remaining_solar_today_kwh(6.0) == 40.0


def test_remaining_solar_zero_when_no_production_after_fallback_hour():
    # Past T_START_FALLBACK_HOUR with nothing produced: solar sensor likely
    # broken — keep the conservative 0 so the evening top-up still books slots.
    assert _solar_ctrl()._remaining_solar_today_kwh(16.0) == 0.0


def test_remaining_solar_keeps_the_curve_when_accumulator_is_warm():
    # A poor morning is not deferred into the afternoon as fictional PV.
    assert _solar_ctrl(produced=10.0, t_start=8.0)._remaining_solar_today_kwh(12.0) == 20.0


def test_remaining_solar_uses_fraction_when_t_start_known():
    # Accumulator cold but production started → sinusoidal fraction (stub: 50%).
    assert _solar_ctrl(t_start=8.0)._remaining_solar_today_kwh(14.0) == 20.0


def test_curtailment_forecast_uses_sensor_value_without_hidden_haircut():
    tracker = SimpleNamespace(
        calculate_sunrise=lambda: 6.0,
        calculate_solar_noon=lambda: 12.0,
        get_solar_fraction_done=lambda hour, start, end: 0.5,
        get_avg_daily_consumption=lambda: 8.0,
    )
    ctrl = _controller(
        solar_forecast_sensor="sensor.solar",
        _solar_t_start=None,
        _consumption_tracker=tracker,
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda _entity_id: SimpleNamespace(state="4.03")
        )
    )

    forecast, _model, consumption = PricingManager(
        hass, ctrl
    )._curtailment_forecast_model(datetime(2026, 8, 13, 5, 42))

    assert forecast == pytest.approx(4.03)
    assert consumption == 8.0


def test_remaining_solar_zero_when_forecast_unavailable():
    assert _solar_ctrl(forecast="unavailable")._remaining_solar_today_kwh(6.0) == 0.0


def test_remaining_solar_zero_when_no_sensor_configured():
    ctrl = _controller(solar_forecast_sensor=None)
    assert PricingManager(SimpleNamespace(), ctrl)._remaining_solar_today_kwh(6.0) == 0.0


def test_extended_timeline_adds_tomorrow_periods_without_leaking_into_today():
    madrid = ZoneInfo("Europe/Madrid")
    now = datetime(2026, 8, 24, 23, 45, tzinfo=madrid)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    periods = (
        SolarForecastPeriod(
            midnight + timedelta(hours=8),
            midnight + timedelta(hours=12),
            4.0,
        ),
    )
    manager = PricingManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Madrid")),
        _controller(),
    )
    current_day = manager._solar_timeline_input(
        now,
        {
            "solar_forecast_input": SolarForecastInput(
                0.0,
                "remaining_sensor",
                periods=periods,
            )
        },
        horizon_end=midnight,
    )
    extended = manager._solar_timeline_input(
        now,
        {
            "solar_forecast_input": SolarForecastInput(
                0.0,
                "remaining_sensor",
                periods=periods,
            )
        },
        horizon_end=midnight + timedelta(hours=12),
    )
    repeated = manager._solar_timeline_input(
        now,
        {
            "remaining_solar_kwh": 0.0,
            "solar_remaining_raw_kwh": 4.0,
            "solar_forecast_periods": periods,
            "solar_forecast_conversion": "extended_dated_periods",
        },
        horizon_end=midnight + timedelta(hours=12),
    )

    assert current_day.remaining_kwh == 0.0
    assert extended.remaining_kwh == pytest.approx(4.0)
    assert extended.conversion == "extended_dated_periods"
    assert repeated.remaining_kwh == pytest.approx(4.0)


def test_extended_chronological_plan_uses_consumption_shape_by_date():
    """Tomorrow's matching hour must not overwrite today's profile shape."""
    madrid = ZoneInfo("Europe/Madrid")
    now = datetime(2026, 8, 24, 10, 0, tzinfo=madrid)
    tomorrow = now.date() + timedelta(days=1)
    today_shape = [0.0] * INTERVAL_COUNT
    tomorrow_shape = [0.0] * INTERVAL_COUNT
    today_shape[40] = 1.0
    tomorrow_shape[40] = 9.0
    aggregate = [0.0] * INTERVAL_COUNT
    aggregate[40] = 10.0
    forecast = ConsumptionForecast(
        10.0,
        aggregate,
        "profile",
        True,
        intervals_by_date={
            now.date(): today_shape,
            tomorrow: tomorrow_shape,
        },
    )
    tracker = SimpleNamespace(
        consumption_profile=SimpleNamespace(),
        forecast_consumption_between=lambda *_args, **_kwargs: forecast,
    )
    ctrl = _controller(
        _consumption_tracker=tracker,
        solar_profile_mode="off",
        _predictive_safety_margin_kwh=0.0,
        coordinators=[],
        _is_battery_manual_owned=lambda _coordinator: False,
        max_contracted_power=0.0,
        max_charge_capacity=0.0,
    )
    manager = _mgr(ctrl)
    horizon_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1, hours=12
    )

    projection = manager.build_extended_chronological_projection(
        now=now,
        slots=[],
        base_decision_data={
            "avg_consumption_kwh": 1.0,
            "solar_forecast_input": SolarForecastInput(0.0, "none"),
        },
        price_ceiling=None,
        horizon_end=horizon_end,
    )
    plan = projection.plan

    assert plan is not None
    at_ten = [
        item for item in plan.intervals
        if item.start.hour == 10 and item.start.minute == 0
    ]
    assert [(item.start.date(), item.consumption_kwh) for item in at_ten] == [
        (now.date(), pytest.approx(1.0)),
        (tomorrow, pytest.approx(9.0)),
    ]


def test_extended_projection_adapter_is_read_only_for_dashboard_callers():
    """A visual cross-midnight projection cannot alter predictive runtime state."""
    madrid = ZoneInfo("Europe/Madrid")
    now = datetime(2026, 8, 24, 10, 0, tzinfo=madrid)
    horizon_end = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1, hours=12)
    shape = [0.0] * INTERVAL_COUNT
    shape[40] = 1.0
    forecast = ConsumptionForecast(1.0, shape, "profile", True)
    tracker = SimpleNamespace(
        consumption_profile=SimpleNamespace(),
        forecast_consumption_between=lambda *_args, **_kwargs: forecast,
    )
    ctrl = _controller(
        _consumption_tracker=tracker,
        solar_profile_mode="off",
        _predictive_safety_margin_kwh=0.0,
        coordinators=[],
        _is_battery_manual_owned=lambda _coordinator: False,
        max_contracted_power=0.0,
        max_charge_capacity=0.0,
        _last_chronological_diagnostics={"before": "unchanged"},
    )
    base_decision_data = {
        "avg_consumption_kwh": 1.0,
        "solar_forecast_input": SolarForecastInput(0.0, "none"),
    }

    result = _mgr(ctrl).build_extended_chronological_projection(
        now=now,
        slots=[],
        base_decision_data=base_decision_data,
        price_ceiling=None,
        horizon_end=horizon_end,
    )

    assert result.plan is not None
    assert base_decision_data == {
        "avg_consumption_kwh": 1.0,
        "solar_forecast_input": SolarForecastInput(0.0, "none"),
    }
    assert ctrl._last_chronological_diagnostics == {"before": "unchanged"}
    assert result.diagnostics["chronological_planning_active"] is False
    with pytest.raises(TypeError):
        result.diagnostics["chronological_source"] = "mutated"


def test_extended_projection_refreshes_the_live_solar_budget():
    """A dashboard redraw must not reuse a morning solar remainder."""
    madrid = ZoneInfo("Europe/Madrid")
    now = datetime(2026, 8, 25, 18, 0, tzinfo=madrid)
    horizon_end = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1, hours=12)
    controller = _controller(
        solar_forecast_sensor=None,
        solar_forecast_remaining_sensor="sensor.remaining",
        solar_forecast_source="previous",
        solar_forecast_diagnostic_source="previous_source",
        solar_forecast_periods=("previous_periods",),
        solar_forecast_conversion="previous_conversion",
    )
    state = SimpleNamespace(
        state="1.81",
        attributes={"unit_of_measurement": "kWh"},
    )
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state))
    manager = PricingManager(hass, controller)
    captured: dict[str, object] = {}

    def capture_projection(**kwargs):
        captured.update(kwargs["decision_data"])
        return None

    manager._build_chronological_plan_for_horizon = capture_projection
    stale_input = SolarForecastInput(8.23, "stale", conversion="stale")
    base = {"solar_forecast_input": stale_input}

    result = manager.build_extended_chronological_projection(
        now=now,
        slots=[],
        base_decision_data=base,
        price_ceiling=None,
        horizon_end=horizon_end,
    )

    assert result.plan is None
    assert captured["solar_forecast_input"].remaining_kwh == pytest.approx(1.81)
    assert captured["solar_forecast_input"].source == "remaining_sensor"
    assert base["solar_forecast_input"] is stale_input
    assert controller.solar_forecast_source == "previous"
    assert controller.solar_forecast_diagnostic_source == "previous_source"
    assert controller.solar_forecast_periods == ("previous_periods",)
    assert controller.solar_forecast_conversion == "previous_conversion"


def test_canonical_diagnostics_refresh_writes_no_control_runtime_state():
    """Reload diagnostics are owned by PricingManager, not the dashboard."""
    madrid = ZoneInfo("Europe/Madrid")
    now = datetime(2026, 8, 24, 10, 0, tzinfo=madrid)
    schedule = SimpleNamespace(selected_slots=[])
    actuator_calls: list[object] = []
    ctrl = _controller(
        _last_decision_data={"should_charge": True, "control": "unchanged"},
        _last_chronological_diagnostics={"previous": "diagnostic"},
        _dynamic_pricing_schedule=schedule,
        _charge_delay_unlocked=False,
        _delay_setpoint_reached=False,
        _charge_delay_status={"state": "Delayed"},
        _daily_operation_last_projection_monotonic=17.0,
        _daily_operation_last_projection_signature=("saved",),
        grid_charging_active=True,
        coordinators=[
            SimpleNamespace(
                apply_power=lambda *args, **kwargs: actuator_calls.append(
                    (args, kwargs)
                )
            )
        ],
    )
    manager = _mgr(ctrl)
    decision_calls: list[datetime] = []
    build_calls: list[dict] = []

    local_decision_data = {
        "avg_consumption_kwh": 2.0,
        "planned_grid_charge_kwh": 0.0,
    }

    async def local_decision(*, now: datetime | None = None):
        assert now is not None
        decision_calls.append(now)
        return local_decision_data

    def build_plan(**kwargs):
        build_calls.append(kwargs)
        kwargs["decision_data"].update(
            {
                "chronological_source": "profile",
                "solar_timeline_source": "provider",
                "solar_timeline_effective_kwh": 1.2,
            }
        )
        manager._store_chronological_diagnostics(kwargs["decision_data"])
        return pricing_engine.ChronologicalPlan()

    manager._current_horizon_grid_charging_decision = local_decision
    manager._build_chronological_plan = build_plan
    before_decision_ref = ctrl._last_decision_data
    before_decision = copy.deepcopy(before_decision_ref)
    before_schedule = ctrl._dynamic_pricing_schedule
    before_flags = (
        ctrl._charge_delay_unlocked,
        ctrl._delay_setpoint_reached,
        copy.deepcopy(ctrl._charge_delay_status),
        ctrl._daily_operation_last_projection_monotonic,
        ctrl._daily_operation_last_projection_signature,
        ctrl.grid_charging_active,
    )

    refreshed = asyncio.run(
        manager.async_refresh_chronological_diagnostics(now=now)
    )

    assert refreshed is True
    assert decision_calls == [now]
    assert build_calls[0]["slots"] == []
    assert build_calls[0]["diagnostic_only"] is True
    assert "horizon_end" not in build_calls[0]
    assert "persist_diagnostics" not in build_calls[0]
    assert ctrl._last_decision_data is before_decision_ref
    assert ctrl._last_decision_data == before_decision
    assert local_decision_data == {
        "avg_consumption_kwh": 2.0,
        "planned_grid_charge_kwh": 0.0,
    }
    assert ctrl._dynamic_pricing_schedule is before_schedule
    assert (
        ctrl._charge_delay_unlocked,
        ctrl._delay_setpoint_reached,
        ctrl._charge_delay_status,
        ctrl._daily_operation_last_projection_monotonic,
        ctrl._daily_operation_last_projection_signature,
        ctrl.grid_charging_active,
    ) == before_flags
    assert actuator_calls == []
    assert ctrl._last_chronological_diagnostics == {
        "chronological_source": "profile",
        "solar_timeline_source": "provider",
        "solar_timeline_effective_kwh": 1.2,
    }


# ----------------------------------------------------------------------
# _evaluate_dynamic_pricing (discussion #87: schedule capped by headroom)
# ----------------------------------------------------------------------

def test_dynamic_pricing_sizes_slots_from_planned_charge_not_full_deficit():
    import asyncio

    async def should_charge():
        return {
            "should_charge": True,
            "avg_soc": 19.0,
            "avg_consumption_kwh": 4.552857,
            "energy_deficit_kwh": 4.552857,
            "planned_grid_charge_kwh": 1.5808,
        }

    async def no_op(*_args, **_kwargs):
        return None

    start = datetime.now() + timedelta(hours=1)
    slots = [
        PriceSlot(
            start=start + timedelta(minutes=15 * i),
            end=start + timedelta(minutes=15 * (i + 1)),
            price=0.30 - i / 1000,
        )
        for i in range(18)
    ]
    ctrl = _controller(
        _should_activate_grid_charging=should_charge,
        _last_decision_data=None,
        _dp_last_eval_soc=None,
        _dp_eval_retry_count=0,
        max_contracted_power=7000,
        max_charge_capacity=1200,
    )
    mgr = _mgr(ctrl)
    mgr._maybe_refresh_tibber_prices = no_op
    mgr._parse_price_data = lambda horizon_end=None: slots
    mgr._send_dynamic_pricing_notification = no_op

    asyncio.run(
        mgr._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.DAILY,
        )
    )

    assert ctrl._dynamic_pricing_schedule.hours_needed == 2.0
    assert len(ctrl._dynamic_pricing_schedule.selected_slots) == 8


# ----------------------------------------------------------------------
# apply_price_discharge_block — early-return branches (no hass touched)
# ----------------------------------------------------------------------

def test_discharge_block_removed_when_mode_not_price():
    ctrl = _controller(predictive_charging_mode=PREDICTIVE_MODE_TIME_SLOT)
    _mgr(ctrl).apply_price_discharge_block()
    assert ctrl._removed == ["price_discharge"]
    assert ctrl._set == []


def test_discharge_block_removed_when_dp_control_disabled():
    ctrl = _controller(
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        dp_price_discharge_control=False,
        price_sensor="sensor.price",
    )
    _mgr(ctrl).apply_price_discharge_block()
    assert ctrl._removed == ["price_discharge"]


def test_discharge_block_removed_when_dp_enabled_but_no_sensor():
    ctrl = _controller(
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        dp_price_discharge_control=True,
        price_sensor=None,
    )
    _mgr(ctrl).apply_price_discharge_block()
    assert ctrl._removed == ["price_discharge"]


def test_discharge_block_removed_when_rt_control_disabled():
    ctrl = _controller(
        predictive_charging_mode=PREDICTIVE_MODE_REALTIME_PRICE,
        rt_price_discharge_control=False,
        price_sensor="sensor.price",
    )
    _mgr(ctrl).apply_price_discharge_block()
    assert ctrl._removed == ["price_discharge"]


# ----------------------------------------------------------------------
# apply_price_discharge_block — separate discharge floor / idle band (#408)
# ----------------------------------------------------------------------

def _mgr_with_price(ctrl, price):
    """PricingManager whose price sensor reads ``price`` (Nordpool float path)."""
    state = SimpleNamespace(state=str(price), attributes={})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _eid: state))
    return PricingManager(hass, ctrl)


def _dp_band_controller(**overrides):
    base = dict(
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        dp_price_discharge_control=True,
        price_sensor="sensor.price",
        max_price_threshold=0.20,   # charge ceiling
        discharge_price_threshold=0.30,  # discharge floor
    )
    base.update(overrides)
    return _controller(**base)


def test_dp_discharge_floor_blocks_inside_idle_band():
    # price 0.25 sits in the idle band (ceiling 0.20 < 0.25 < floor 0.30):
    # discharge stays blocked. Single-threshold behavior would unblock at 0.21.
    ctrl = _dp_band_controller()
    _mgr_with_price(ctrl, 0.25).apply_price_discharge_block()
    assert ctrl._set and ctrl._set[0][0] == "price_discharge"
    assert ctrl._price_based_discharge_blocked is True


def test_dp_discharge_allowed_above_floor():
    ctrl = _dp_band_controller()
    _mgr_with_price(ctrl, 0.35).apply_price_discharge_block()
    assert ctrl._removed == ["price_discharge"]
    assert ctrl._price_based_discharge_blocked is False


def test_dp_discharge_floor_unset_falls_back_to_charge_ceiling():
    # Back-compat: no floor → reuse max_price_threshold (0.20) for both, so
    # price 0.25 > 0.20 unblocks discharge exactly as before #408.
    ctrl = _dp_band_controller(discharge_price_threshold=None)
    _mgr_with_price(ctrl, 0.25).apply_price_discharge_block()
    assert ctrl._removed == ["price_discharge"]


# ----------------------------------------------------------------------
# _maybe_refresh_tibber_prices (#21: default call only returns today)
# ----------------------------------------------------------------------

class _FakeTibberServices:
    """Records ``async_call`` args; ``get_prices`` always reports available."""

    def __init__(self):
        self.calls: list = []

    def has_service(self, domain, service):
        return domain == "tibber" and service == "get_prices"

    async def async_call(self, domain, service, data, blocking=True, return_response=True):
        self.calls.append(data)
        return {"prices": {}}


def test_tibber_refresh_requests_through_day_after_tomorrow():
    import asyncio
    from homeassistant.util import dt as dt_util

    services = _FakeTibberServices()
    hass = SimpleNamespace(services=services)
    ctrl = _controller(
        price_integration_type=PRICE_INTEGRATION_TIBBER,
        _tibber_price_slots=[],
        _tibber_prices_fetched_at=None,
    )

    asyncio.run(PricingManager(hass, ctrl)._maybe_refresh_tibber_prices(force=True))

    assert len(services.calls) == 1
    end = dt_util.parse_datetime(services.calls[0]["end"])
    assert end == dt_util.start_of_local_day() + timedelta(days=2)
    assert ctrl._price_based_discharge_blocked is False


# ----------------------------------------------------------------------
# Official Nord Pool service provider
# ----------------------------------------------------------------------

class _FakeNordPoolServices:
    """Records the official service request and returns one current-day area."""

    def __init__(self, response=None):
        self.calls: list = []
        self.response = response or {}

    def has_service(self, domain, service):
        return domain == "nordpool" and service == "get_prices_for_date"

    async def async_call(self, domain, service, data, blocking=True, return_response=True):
        self.calls.append((domain, service, data))
        return self.response


def test_official_nordpool_refresh_requests_today_and_selected_area(monkeypatch):
    import asyncio
    from homeassistant.util import dt as dt_util

    now = datetime.now()
    services = _FakeNordPoolServices(
        {
            "ES": [
                {
                    "start": (now - timedelta(minutes=15)).isoformat(),
                    "end": (now + timedelta(minutes=15)).isoformat(),
                    "price": 123.45,
                }
            ]
        }
    )
    hass = SimpleNamespace(
        services=services,
        states=SimpleNamespace(get=lambda _entity_id: SimpleNamespace(attributes={})),
    )
    source = [OfficialNordPoolSource("nordpool-entry", "ES")]
    monkeypatch.setattr(
        pricing_engine,
        "resolve_official_nordpool_source",
        lambda *_args: source[0],
    )
    ctrl = _controller(
        price_sensor="sensor.nord_pool_es_current_price",
        _nordpool_price_slots=[],
        _nordpool_prices_fetched_at=None,
    )
    manager = PricingManager(hass, ctrl)

    asyncio.run(manager._maybe_refresh_nordpool_prices(force=True))
    asyncio.run(manager._maybe_refresh_nordpool_prices())

    assert len(services.calls) == 1
    domain, service, data = services.calls[0]
    assert (domain, service) == ("nordpool", "get_prices_for_date")
    assert data == {
        "config_entry": "nordpool-entry",
        "date": dt_util.now().date(),
        "areas": ["ES"],
    }
    assert len(ctrl._nordpool_price_slots) == 1
    assert ctrl._nordpool_price_slots[0].price == 0.12345
    assert manager._get_current_price() == 0.12345

    # A hot-reload that selects another official market area must invalidate
    # the otherwise-fresh hourly cache immediately.
    source[0] = OfficialNordPoolSource("nordpool-entry", "FR")
    services.response = {
        "FR": [
            {
                "start": (now - timedelta(minutes=15)).isoformat(),
                "end": (now + timedelta(minutes=15)).isoformat(),
                "price": 200.0,
            }
        ]
    }
    asyncio.run(manager._maybe_refresh_nordpool_prices())

    assert len(services.calls) == 2
    assert services.calls[1][2]["areas"] == ["FR"]
    assert ctrl._nordpool_price_slots[0].price == 0.2


def test_hacs_nordpool_raw_today_does_not_call_official_service():
    import asyncio

    services = _FakeNordPoolServices()
    state = SimpleNamespace(
        state="0.10",
        attributes={
            "raw_today": [
                {
                    "start": datetime.now(),
                    "end": datetime.now() + timedelta(hours=1),
                    "value": 0.10,
                }
            ]
        },
    )
    hass = SimpleNamespace(
        services=services,
        states=SimpleNamespace(get=lambda _entity_id: state),
    )
    ctrl = _controller(
        price_sensor="sensor.nordpool_kwh_es_eur",
        _nordpool_price_slots=[],
        _nordpool_prices_fetched_at=None,
    )

    asyncio.run(PricingManager(hass, ctrl)._maybe_refresh_nordpool_prices(force=True))

    assert services.calls == []


# ----------------------------------------------------------------------
# Smart pre-discharge runtime lifecycle
# ----------------------------------------------------------------------

def test_smart_predischarge_is_scoped_to_predictive_dynamic_pricing():
    ctrl = _controller(
        smart_predischarge_enabled=True,
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
    )
    manager = _mgr(ctrl)

    assert manager._smart_predischarge_enabled() is True

    ctrl.predictive_charging_mode = PREDICTIVE_MODE_REALTIME_PRICE
    assert manager._smart_predischarge_enabled() is False


def test_smart_predischarge_cleanup_removes_override_and_blockers():
    calls = []
    ctrl = _controller()
    ctrl.coordinators = []
    ctrl.remove_setpoint_override = lambda source: calls.append(("override", source))
    ctrl.remove_discharge_block = lambda source, coordinator=None: calls.append(
        ("block", source, coordinator)
    )
    ctrl._curtailment_plan = CurtailmentPlan(status="predischarging", reason="selected")
    manager = _mgr(ctrl)

    manager.clear_curtailment_runtime("disabled")

    assert ("override", "curtailment_predischarge") in calls
    assert ("block", "curtailment_negative_window", None) in calls
    assert ctrl._curtailment_runtime_status == "disabled"
    assert ctrl._curtailment_runtime_reason == "disabled"


def test_smart_predischarge_runtime_starts_stops_and_protects_negative_window():
    now = datetime.now()
    active_pre_slot = PreDischargeSlot(
        now - timedelta(minutes=1), now + timedelta(minutes=10), 0.40
    )
    future_risk = PriceSlot(
        now + timedelta(hours=1), now + timedelta(hours=2), -0.10
    )
    calls = []
    state = SimpleNamespace(state="0.0", attributes={})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state))
    coordinator = SimpleNamespace(data={"battery_soc": 80.0}, is_available=True, name="b1")
    ctrl = _controller(
        smart_predischarge_enabled=True,
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        coordinators=[coordinator],
        consumption_sensor="sensor.grid",
        _curtailment_plan=CurtailmentPlan(
            status="planned",
            reason="headroom_required",
            risk_slots=[future_risk],
            selected_discharge_slots=[active_pre_slot],
            required_headroom_kwh=3.0,
        ),
        remove_setpoint_override=lambda source: calls.append(("remove_override", source)),
        set_setpoint_override=lambda source, value, priority=0: calls.append(
            ("set_override", source, value, priority)
        ),
        remove_discharge_block=lambda source, coordinator=None: calls.append(
            ("remove_block", source, coordinator)
        ),
        set_discharge_block=lambda source, reason, details=None, coordinator=None: calls.append(
            ("set_block", source, reason, coordinator)
        ),
        _apply_meter_transform=lambda _state: 0.0,
        _curtailment_active=False,
        _curtailment_active_export_target_w=0.0,
    )
    manager = PricingManager(hass, ctrl)
    manager._get_current_price = lambda: 0.30
    manager._curtailment_battery_snapshots = lambda: [
        BatterySnapshot("b1", 80.0, 10.0, 100.0, 10.0, 2000.0)
    ]

    manager.refresh_curtailment_runtime()

    assert any(call[:2] == ("set_override", "curtailment_predischarge") for call in calls)
    assert ctrl._curtailment_runtime_status == "predischarging"

    # The same runtime plan protects the negative window with a net-zero grid
    # target, so domestic load can still be supplied by the battery.
    ctrl._curtailment_plan = CurtailmentPlan(
        status="planned",
        reason="headroom_required",
        risk_slots=[PriceSlot(now - timedelta(minutes=1), now + timedelta(minutes=10), -0.10)],
        selected_discharge_slots=[],
        required_headroom_kwh=3.0,
    )
    manager._get_current_price = lambda: -0.10
    manager.refresh_curtailment_runtime()

    assert ctrl._curtailment_runtime_status == "protected_window"
    assert any(
        call[:2] == ("set_override", "curtailment_negative_window")
        and call[2:] == (0.0, 6)
        for call in calls
    )
    assert not any(
        call[0] == "set_block" and call[1] == "curtailment_negative_window"
        for call in calls
    )

    ctrl.smart_predischarge_enabled = False
    manager.refresh_curtailment_runtime()
    assert ctrl._curtailment_runtime_status == "disabled"
    assert ("remove_override", "curtailment_predischarge") in calls


def test_curtailment_auto_replans_after_headroom_changes():
    now = datetime.now()
    risk = PriceSlot(now + timedelta(hours=2), now + timedelta(hours=3), -0.10)
    candidate = PriceSlot(now + timedelta(minutes=30), now + timedelta(hours=1, minutes=30), 0.30)
    coordinator = SimpleNamespace(data={"battery_soc": 70.0}, is_available=True, name="b1")
    plan = CurtailmentPlan(
        status="protected",
        reason="headroom_sufficient",
        risk_slots=[risk],
        required_headroom_kwh=4.0,
        current_headroom_kwh=6.0,
    )
    ctrl = _controller(
        smart_predischarge_enabled=True,
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        coordinators=[coordinator],
        _curtailment_plan=plan,
        _curtailment_last_planned_headroom_kwh=6.0,
        _curtailment_last_auto_replan=None,
        _dynamic_pricing_schedule=_schedule([]),
    )
    manager = _mgr(ctrl)
    manager._curtailment_battery_snapshots = lambda: [
        BatterySnapshot("b1", 50.0, 10.0, 100.0, 10.0, 2000.0)
    ]
    manager.get_future_price_slots = lambda: [candidate, risk]
    rebuilt = []
    manager._build_curtailment_plan = lambda slots, reserved, now=None: rebuilt.append(
        (slots, reserved, now)
    )

    assert manager._maybe_rebuild_curtailment_plan(
        plan, manager._curtailment_battery_snapshots(), now
    ) is True
    assert rebuilt and rebuilt[0][0] == [candidate, risk]
    assert ctrl._curtailment_last_auto_replan == now


def test_curtailment_runtime_releases_space_for_underproduction_and_stops_on_excess():
    now = datetime.now()
    risk = PriceSlot(now + timedelta(minutes=5), now + timedelta(hours=1), -0.10)
    plan = CurtailmentPlan(
        status="protected",
        reason="headroom_sufficient",
        risk_slots=[risk],
        required_headroom_kwh=3.0,
        solar_reserve_remaining_kwh=3.0,
        solar_reserve_by_slot={risk: 3.0},
        solar_forecast_by_slot={risk: 4.0},
        consumption_forecast_by_slot={risk: 0.0},
        headroom_margin_kwh=0.0,
        opportunistic_space_kwh=1.0,
    )
    plan.actual_solar_by_slot = {risk: 2.0}
    ctrl = _controller(
        smart_predischarge_enabled=True,
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        max_charge_capacity=4000.0,
        _curtailment_plan=plan,
    )
    manager = _mgr(ctrl)
    snapshots = [BatterySnapshot("b1", 60.0, 10.0, 100.0, 10.0, 2000.0)]

    manager._update_curtailment_opportunistic_diagnostics(plan, snapshots, now)
    assert plan.solar_reserve_remaining_kwh == pytest.approx(1.5)
    assert plan.opportunistic_space_kwh == pytest.approx(2.5)
    assert plan.opportunistic_charge_reason == "solar_underproduction_released_space"

    plan.actual_solar_by_slot = {risk: 6.0}
    manager._update_curtailment_opportunistic_diagnostics(plan, snapshots, now)
    assert plan.solar_reserve_remaining_kwh == pytest.approx(4.5)
    assert plan.opportunistic_space_kwh == 0.0
    assert plan.opportunistic_charge_reason == "solar_overproduction_reduced_space"


def test_curtailment_daily_solar_accumulator_releases_space_progressively():
    now = datetime.now()
    risk = PriceSlot(now + timedelta(minutes=5), now + timedelta(hours=1), -0.10)
    plan = CurtailmentPlan(
        status="protected",
        reason="headroom_sufficient",
        risk_slots=[risk],
        solar_forecast_kwh=4.0,
        solar_reserve_by_slot={risk: 3.0},
        solar_forecast_by_slot={risk: 4.0},
        consumption_forecast_by_slot={risk: 0.0},
    )
    manager = _solar_ctrl(forecast="4.0", produced=1.0, t_start=6.0)
    manager._controller._curtailment_plan = plan
    snapshots = [BatterySnapshot("b1", 60.0, 10.0, 100.0, 10.0, 2000.0)]

    manager._update_curtailment_opportunistic_diagnostics(plan, snapshots, now)

    # The fake tracker says 50% of the forecast should have arrived by now;
    # 1 kWh actual versus 2 kWh expected halves the remaining reserve.
    assert plan.solar_reserve_remaining_kwh == pytest.approx(1.5)
    assert plan.opportunistic_space_kwh == pytest.approx(2.5)
    assert plan.opportunistic_charge_reason == "solar_underproduction_released_space"


def test_remaining_forecast_is_not_scaled_against_daily_solar_accumulator():
    now = datetime.now()
    risk = PriceSlot(now + timedelta(minutes=5), now + timedelta(hours=1), -0.10)
    plan = CurtailmentPlan(
        status="protected",
        reason="headroom_sufficient",
        risk_slots=[risk],
        solar_forecast_kwh=1.81,
        solar_forecast_is_remaining=True,
        solar_reserve_by_slot={risk: 1.5},
        solar_forecast_by_slot={risk: 1.81},
        consumption_forecast_by_slot={risk: 0.0},
    )
    manager = _solar_ctrl(forecast="20.52", produced=12.34, t_start=6.0)
    snapshots = [BatterySnapshot("b1", 60.0, 10.0, 100.0, 10.0, 2000.0)]

    manager._update_curtailment_opportunistic_diagnostics(plan, snapshots, now)

    assert plan.solar_reserve_remaining_kwh == pytest.approx(1.5)
    assert plan.opportunistic_space_kwh == pytest.approx(2.5)


def test_curtailment_export_settings_keep_legacy_compatibility_and_modes():
    legacy_zero = _controller(predischarge_max_export_power_w=0.0)
    legacy_custom = _controller(predischarge_max_export_power_w=750.0)
    automatic = _controller(
        predischarge_export_mode=EXPORT_MODE_AUTOMATIC,
        predischarge_max_export_power_w=750.0,
    )
    custom = _controller(
        predischarge_export_mode=EXPORT_MODE_CUSTOM,
        predischarge_export_limit_w=900.0,
    )

    assert _mgr(legacy_zero)._curtailment_export_settings() == (
        EXPORT_MODE_SELF_CONSUMPTION,
        0.0,
    )
    assert _mgr(legacy_custom)._curtailment_export_settings() == (
        EXPORT_MODE_CUSTOM,
        750.0,
    )
    assert _mgr(automatic)._curtailment_export_settings() == (
        EXPORT_MODE_AUTOMATIC,
        0.0,
    )
    assert _mgr(custom)._curtailment_export_settings() == (
        EXPORT_MODE_CUSTOM,
        900.0,
    )
