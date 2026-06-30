"""Characterization tests for ChargeDelayManager.

These pin the *current* behavior of the unified charge-delay gate so the
extraction from ``__init__.py`` can be proven to change nothing.

No Home Assistant entities and no Modbus: the manager is built via ``__new__``
(skipping ``__init__``, which needs a real hass for its Store) and wired to a
stand-in controller (SimpleNamespace). ``schedule_save`` is replaced with a spy
so the latch logic can be exercised without an event loop or storage.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from custom_components.omnibattery.control.charge_delay import (
    ChargeDelayManager,
    _TRANSIENT_UNLOCK_REASONS,
)
from custom_components.omnibattery.const import (
    DELAY_SOC_SETPOINT_HYSTERESIS,
)


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------

def _h_to_hhmm(hours):
    if hours is None:
        return None
    h = int(hours)
    m = int(round((hours - h) * 60))
    return f"{h:02d}:{m:02d}"


def _tracker(**overrides):
    """A stand-in ConsumptionTracker exposing only the methods the gate calls."""
    base = dict(
        get_today_target_soc=lambda: 80,
        get_avg_daily_consumption=lambda: 5.0,
        get_consumption_window_hours_per_day=lambda: 24.0,
        consumption_window_hours_in_range=lambda a, b: max(0.0, b - a),
        estimate_t_end=lambda: 16.0,
        get_solar_fraction_done=lambda now_h, t0, t1: 0.0,
        detect_solar_t_start=lambda: None,
        h_to_hhmm=_h_to_hhmm,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _coord(soc=50, total_energy=5.0, power=0, min_soc=20):
    return SimpleNamespace(
        data={
            "battery_soc": soc,
            "battery_total_energy": total_energy,
            "battery_power": power,
        },
        min_soc=min_soc,
    )


def _controller(**overrides):
    base = dict(
        charge_delay_enabled=True,
        _charge_delay_status={"state": "Idle", "safety_margin_min": 30},
        _charge_delay_unlocked=False,
        _delay_setpoint_reached=False,
        _delay_soc_setpoint_enabled=False,
        _delay_soc_setpoint=50,
        coordinators=[_coord()],
        solar_forecast_sensor="sensor.forecast",
        _forecast_unavailable_since=None,
        _forecast_grace_s=300,
        _charge_delay_forecast_cache=None,
        _charge_delay_balance_needs_charge=False,
        _charge_delay_balance_deadband_kwh=0.5,
        _daily_solar_energy_kwh=0.0,
        _delay_safety_margin_h=0.5,
        _delay_last_log_time=0,
        _solar_t_start=8.0,
        _charge_delay_last_date=None,
        _effective_system_capacity=lambda coords, is_charging: 3000.0,
        _weekly_charge_mgr=SimpleNamespace(is_active=lambda: False),
    )
    base["_consumption_tracker"] = overrides.pop("_consumption_tracker", _tracker())
    base["_balance_monitor_overrides_delay"] = overrides.pop(
        "_balance_monitor_overrides_delay", lambda: False
    )
    base.update(overrides)
    ctrl = SimpleNamespace(**base)
    return ctrl


def _make_mgr(ctrl, states=None):
    mgr = ChargeDelayManager.__new__(ChargeDelayManager)
    mgr._controller = ctrl
    mgr._store = None
    mgr._save_task = None
    ctrl.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda eid: (states or {}).get(eid))
    )
    # Spy: count persistence requests without touching a Store or event loop.
    mgr.saves = 0
    def _spy():
        mgr.saves += 1
    mgr.schedule_save = _spy
    return mgr


def _state(value):
    return SimpleNamespace(state=str(value))


# ----------------------------------------------------------------------
# is_charge_delayed: early-exit branches
# ----------------------------------------------------------------------

def test_disabled_returns_false():
    ctrl = _controller(charge_delay_enabled=False)
    mgr = _make_mgr(ctrl)
    assert mgr.is_charge_delayed() is False
    assert ctrl._charge_delay_status["state"] == "Disabled"


def test_balance_monitor_override_skips_delay():
    ctrl = _controller(_balance_monitor_overrides_delay=lambda: True)
    mgr = _make_mgr(ctrl)
    assert mgr.is_charge_delayed() is False
    assert ctrl._charge_delay_status["state"] == "Skipped - Full Charge Day"


def test_already_unlocked_returns_false():
    ctrl = _controller(_charge_delay_unlocked=True)
    mgr = _make_mgr(ctrl)
    assert mgr.is_charge_delayed() is False
    assert ctrl._charge_delay_status["state"] == "Charging allowed"


def test_unlock_when_should_delay_false():
    ctrl = _controller()
    mgr = _make_mgr(ctrl)
    mgr._should_delay_charge = lambda target: False
    assert mgr.is_charge_delayed() is False
    assert ctrl._charge_delay_unlocked is True
    assert mgr.saves == 1  # latch persisted on unlock


def test_keep_delay_when_should_delay_true():
    ctrl = _controller()
    mgr = _make_mgr(ctrl)
    mgr._should_delay_charge = lambda target: True
    assert mgr.is_charge_delayed() is True
    assert ctrl._charge_delay_unlocked is False


def test_transient_no_forecast_does_not_latch():
    # A fail-safe unlock (no forecast available) allows charging this cycle but
    # must NOT latch the permanent daily unlock, so it can re-arm later.
    assert "no_forecast" in _TRANSIENT_UNLOCK_REASONS
    ctrl = _controller(solar_forecast_sensor=None)
    mgr = _make_mgr(ctrl)
    assert mgr.is_charge_delayed() is False
    assert ctrl._charge_delay_status["unlock_reason"] == "no_forecast"
    assert ctrl._charge_delay_unlocked is False
    assert mgr.saves == 0  # nothing latched, nothing persisted


def test_delay_rearms_after_forecast_recovers():
    # First cycle: forecast unavailable past grace -> no_forecast unlock (not latched).
    from time import monotonic
    ctrl = _controller(
        _forecast_unavailable_since=monotonic() - 10_000,
        _forecast_grace_s=300,
    )
    mgr = _make_mgr(ctrl, states={"sensor.forecast": _state("unavailable")})
    assert mgr.is_charge_delayed() is False
    assert ctrl._charge_delay_unlocked is False
    # Next cycle the forecast is healthy again and the day is covered -> the
    # delay re-arms instead of staying disabled for the rest of the day.
    mgr._should_delay_charge = lambda target: True
    assert mgr.is_charge_delayed() is True
    assert ctrl._charge_delay_unlocked is False


def test_genuine_unlock_still_latches():
    # A real "conditions met" unlock (e.g. time_backup) keeps latching as before.
    ctrl = _controller()
    mgr = _make_mgr(ctrl)

    def _stub(target):
        ctrl._charge_delay_status["unlock_reason"] = "time_backup"
        return False

    mgr._should_delay_charge = _stub
    assert mgr.is_charge_delayed() is False
    assert ctrl._charge_delay_unlocked is True
    assert mgr.saves == 1


# ----------------------------------------------------------------------
# is_charge_delayed: SOC setpoint gating
# ----------------------------------------------------------------------

def test_setpoint_not_reached_blocks_below_setpoint():
    ctrl = _controller(
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=50,
        coordinators=[_coord(soc=40)],
    )
    mgr = _make_mgr(ctrl)
    mgr._should_delay_charge = lambda target: True
    # Below setpoint and not yet reached -> charge to setpoint (allow, return False)
    assert mgr.is_charge_delayed() is False
    assert ctrl._charge_delay_status["state"] == "Charging to setpoint"
    assert ctrl._delay_setpoint_reached is False


def test_setpoint_reached_latches_and_evaluates():
    ctrl = _controller(
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=50,
        coordinators=[_coord(soc=55)],
    )
    mgr = _make_mgr(ctrl)
    mgr._should_delay_charge = lambda target: True
    # At/above setpoint -> latch reached, persist, then evaluate delay
    assert mgr.is_charge_delayed() is True
    assert ctrl._delay_setpoint_reached is True
    assert mgr.saves == 1


def test_setpoint_hysteresis_reopens_below_threshold():
    ctrl = _controller(
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=50,
        _delay_setpoint_reached=True,
        coordinators=[_coord(soc=50 - DELAY_SOC_SETPOINT_HYSTERESIS - 1)],
    )
    mgr = _make_mgr(ctrl)
    mgr._should_delay_charge = lambda target: True
    assert mgr.is_charge_delayed() is False
    assert ctrl._delay_setpoint_reached is False
    assert ctrl._charge_delay_status["state"] == "Charging to setpoint"


# ----------------------------------------------------------------------
# _should_delay_charge: fail-safe forecast branches
# ----------------------------------------------------------------------

def test_no_forecast_sensor_unlocks():
    ctrl = _controller(solar_forecast_sensor=None)
    mgr = _make_mgr(ctrl)
    assert mgr._should_delay_charge(80) is False
    assert ctrl._charge_delay_status["unlock_reason"] == "no_forecast"


def test_forecast_unavailable_within_grace_holds_delay():
    ctrl = _controller(_forecast_unavailable_since=None, _forecast_grace_s=300)
    mgr = _make_mgr(ctrl, states={"sensor.forecast": _state("unavailable")})
    assert mgr._should_delay_charge(80) is True
    assert ctrl._charge_delay_status["state"] == "Waiting for forecast"
    assert ctrl._forecast_unavailable_since is not None


def test_forecast_unavailable_past_grace_unlocks():
    # grace already exhausted: _forecast_unavailable_since far in the past
    from time import monotonic
    ctrl = _controller(
        _forecast_unavailable_since=monotonic() - 10_000,
        _forecast_grace_s=300,
    )
    mgr = _make_mgr(ctrl, states={"sensor.forecast": _state("unavailable")})
    assert mgr._should_delay_charge(80) is False
    assert ctrl._charge_delay_status["unlock_reason"] == "no_forecast"


def test_zero_capacity_unlocks():
    ctrl = _controller(coordinators=[_coord(total_energy=0)])
    mgr = _make_mgr(ctrl, states={"sensor.forecast": _state(10.0)})
    assert mgr._should_delay_charge(80) is False
    assert ctrl._charge_delay_status["unlock_reason"] == "no_forecast"


def test_balance_needs_charge_unlocks_low_forecast():
    # avg_soc 30, min_soc 20, capacity 10 -> usable 1 kWh; RAW forecast 1.0;
    # consumption 5, deadband 0.5 -> (1 + 1.0) < (5 - 0.5) -> grid needed.
    # No pricing manager -> price-aware release is a no-op -> unlock(low_forecast).
    ctrl = _controller(
        coordinators=[_coord(soc=30, total_energy=10.0, min_soc=20)],
        _consumption_tracker=_tracker(get_avg_daily_consumption=lambda: 5.0),
    )
    mgr = _make_mgr(ctrl, states={"sensor.forecast": _state(1.0)})
    assert mgr._should_delay_charge(80) is False
    assert ctrl._charge_delay_status["unlock_reason"] == "low_forecast"


def test_balanced_day_holds_with_deadband():
    # #4 concrete day: usable ~0.4 + raw forecast 15.76 vs 15.40 consumption.
    # Pre-fix the 0.85 haircut (15.76 -> 13.40) flipped this into a false deficit
    # and a latched pre-dawn unlock. With the raw forecast + deadband the gate now
    # reads it as solar-sufficient and keeps the delay armed.
    ctrl = _controller(
        coordinators=[_coord(soc=24, total_energy=10.0, min_soc=20)],
        _consumption_tracker=_tracker(get_avg_daily_consumption=lambda: 15.40),
    )
    mgr = _make_mgr(ctrl, states={"sensor.forecast": _state(15.76)})
    mgr._should_delay_charge(80)
    assert ctrl._charge_delay_balance_needs_charge is False


def test_low_forecast_price_release_holds_for_cheaper_hour():
    # Genuine deficit, cheaper import hour ahead before solar -> hold, do not unlock.
    ctrl = _controller(_solar_t_start=8.0)
    mgr = _make_mgr(ctrl)
    mgr._price_optimal_release_h = lambda now_h, edge_h: 7.0
    assert mgr._low_forecast_price_release(5.0) is True
    assert ctrl._charge_delay_status["estimated_unlock_time"] == "07:00"
    assert "cheap import" in ctrl._charge_delay_status["state"]


def test_low_forecast_price_release_unlocks_when_now_cheapest():
    ctrl = _controller(_solar_t_start=8.0)
    mgr = _make_mgr(ctrl)
    mgr._price_optimal_release_h = lambda now_h, edge_h: now_h  # current slot cheapest
    assert mgr._low_forecast_price_release(5.0) is False


def test_low_forecast_price_release_unlocks_without_price_data():
    ctrl = _controller(_solar_t_start=8.0)
    mgr = _make_mgr(ctrl)
    mgr._price_optimal_release_h = lambda now_h, edge_h: None  # no price data
    assert mgr._low_forecast_price_release(5.0) is False


def test_low_forecast_price_release_unlocks_when_no_presolar_slack():
    # now is already past solar start -> no cheap pre-solar window, unlock now.
    ctrl = _controller(_solar_t_start=8.0)
    mgr = _make_mgr(ctrl)
    called = []
    mgr._price_optimal_release_h = lambda now_h, edge_h: called.append(1) or 9.0
    assert mgr._low_forecast_price_release(9.0) is False
    assert called == []  # short-circuits before touching prices


def test_low_forecast_price_release_uses_fallback_when_no_t_start():
    # No T_start yet (pre-dawn): the window edge falls back to T_START_FALLBACK_HOUR.
    ctrl = _controller(_solar_t_start=None)
    mgr = _make_mgr(ctrl)
    edges = []
    mgr._price_optimal_release_h = lambda now_h, edge_h: edges.append(edge_h) or 7.0
    assert mgr._low_forecast_price_release(5.0) is True
    assert edges == [11]  # T_START_FALLBACK_HOUR


# ----------------------------------------------------------------------
# _estimate_energy_balance_unlock_h: projection math
# ----------------------------------------------------------------------

def test_estimate_invalid_window_returns_none():
    ctrl = _controller()
    mgr = _make_mgr(ctrl)
    assert mgr._estimate_energy_balance_unlock_h(10.0, 1.0, 16.0, 8.0, 9.0) is None


def test_estimate_below_threshold_now_returns_now():
    # forecast 0 -> remaining_solar always 0 -> net <= 0 < threshold -> now_h
    ctrl = _controller()
    mgr = _make_mgr(ctrl)
    assert mgr._estimate_energy_balance_unlock_h(0.0, 1.0, 8.0, 16.0, 8.0) == 8.0


def test_estimate_crossing_between_now_and_tend():
    # forecast 10, consumption negligible -> net high at sunrise, 0 at sunset
    ctrl = _controller(
        _consumption_tracker=_tracker(consumption_window_hours_in_range=lambda a, b: 0.0),
    )
    mgr = _make_mgr(ctrl)
    result = mgr._estimate_energy_balance_unlock_h(10.0, 1.0, 8.0, 16.0, 8.0)
    assert 8.0 < result < 16.0


# ----------------------------------------------------------------------
# handle_daily_reset_and_eval: day rollover resets the latch
# ----------------------------------------------------------------------

def test_daily_reset_noop_when_disabled():
    ctrl = _controller(charge_delay_enabled=False, _charge_delay_unlocked=True)
    mgr = _make_mgr(ctrl)
    mgr.handle_daily_reset_and_eval()
    # disabled -> untouched
    assert ctrl._charge_delay_unlocked is True


def test_daily_reset_clears_latch_on_new_day():
    ctrl = _controller(
        _charge_delay_unlocked=True,
        _delay_setpoint_reached=True,
        _charge_delay_last_date=date(2020, 1, 1),
    )
    mgr = _make_mgr(ctrl)
    mgr._should_delay_charge = lambda target: True  # keep eval cheap
    mgr.handle_daily_reset_and_eval()
    assert ctrl._charge_delay_unlocked is False
    assert ctrl._delay_setpoint_reached is False
    assert ctrl._solar_t_start is None
    assert ctrl._charge_delay_last_date == date.today()
    assert mgr.saves == 1


def test_daily_reset_first_cycle_preserves_restored_unlock():
    # last_date is None (fresh start): a restored unlock must NOT be wiped.
    ctrl = _controller(_charge_delay_unlocked=True, _charge_delay_last_date=None)
    mgr = _make_mgr(ctrl)
    mgr._should_delay_charge = lambda target: True
    mgr.handle_daily_reset_and_eval()
    assert ctrl._charge_delay_unlocked is True
    assert ctrl._charge_delay_last_date == date.today()


# ----------------------------------------------------------------------
# refresh_setpoint_blocks: per-battery SOC-setpoint floor enforcement
# ----------------------------------------------------------------------

def _ncoord(name, soc):
    c = _coord(soc=soc)
    c.name = name
    return c


def _setpoint_controller(coords, **overrides):
    """Controller stub with a fake per-battery charge-block registry."""
    base = dict(
        charge_delay_enabled=True,
        _delay_soc_setpoint_enabled=True,
        _delay_soc_setpoint=70,
        _charge_delay_unlocked=False,
        _delay_setpoint_reached=False,
        coordinators=coords,
        _active_balance_overrides_delay=lambda: False,
        _balance_monitor_overrides_delay=lambda: False,
    )
    base.update(overrides)
    ctrl = SimpleNamespace(**base)
    # source -> set(coordinator.name) registry, mirroring per-battery charge
    # blocks. Keyed by name because SimpleNamespace coords are unhashable.
    ctrl.blocks = {}
    ctrl.set_charge_block = (
        lambda source, reason, details=None, coordinator=None:
        ctrl.blocks.setdefault(source, set()).add(coordinator.name)
    )
    ctrl.remove_charge_block = (
        lambda source, coordinator=None: ctrl.blocks.get(source, set()).discard(coordinator.name)
    )
    return ctrl


def _mgr_for(ctrl):
    mgr = ChargeDelayManager.__new__(ChargeDelayManager)
    mgr._controller = ctrl
    return mgr


def test_setpoint_blocks_only_battery_above_setpoint():
    # Floor-fill phase, mixed SOC: leader (>= 70) blocked, laggard (< 70) free.
    leader = _ncoord("Marstek", soc=87)
    laggard = _ncoord("Zendure", soc=69)
    ctrl = _setpoint_controller([leader, laggard])
    _mgr_for(ctrl).refresh_setpoint_blocks()
    assert ctrl.blocks.get("charge_delay_setpoint", set()) == {"Marstek"}


def test_setpoint_blocks_cleared_once_floor_reached():
    # _delay_setpoint_reached -> forecast phase governs all; no per-battery floor.
    leader = _ncoord("Marstek", soc=87)
    laggard = _ncoord("Zendure", soc=72)
    ctrl = _setpoint_controller([leader, laggard], _delay_setpoint_reached=True)
    ctrl.blocks["charge_delay_setpoint"] = {"Marstek"}  # stale block from prior cycle
    _mgr_for(ctrl).refresh_setpoint_blocks()
    assert ctrl.blocks.get("charge_delay_setpoint", set()) == set()


def test_setpoint_blocks_cleared_when_day_unlocked():
    leader = _ncoord("Marstek", soc=87)
    laggard = _ncoord("Zendure", soc=60)
    ctrl = _setpoint_controller([leader, laggard], _charge_delay_unlocked=True)
    _mgr_for(ctrl).refresh_setpoint_blocks()
    assert ctrl.blocks.get("charge_delay_setpoint", set()) == set()


def test_setpoint_blocks_noop_when_feature_disabled():
    leader = _ncoord("Marstek", soc=87)
    ctrl = _setpoint_controller([leader], _delay_soc_setpoint_enabled=False)
    _mgr_for(ctrl).refresh_setpoint_blocks()
    assert ctrl.blocks.get("charge_delay_setpoint", set()) == set()


# ----------------------------------------------------------------------
# _price_optimal_release_h: price-aware release within the feasible window
# ----------------------------------------------------------------------
from datetime import timedelta  # noqa: E402

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.omnibattery.pricing import (  # noqa: E402
    PriceSlot,
)


def _slot(hour, price):
    """Hourly PriceSlot for today at ``hour`` (matches the manager's today check)."""
    start = dt_util.now().replace(
        hour=int(hour), minute=0, second=0, microsecond=0
    )
    return PriceSlot(start=start, end=start + timedelta(hours=1), price=price)


def _price_ctrl(slots, **overrides):
    return _controller(
        price_sensor="sensor.price",
        _pricing_mgr=SimpleNamespace(get_future_price_slots=lambda horizon_end=None: slots),
        **overrides,
    )


def test_price_release_none_without_sensor():
    # No price_sensor / pricing manager → legacy edge release (returns None).
    mgr = _make_mgr(_controller())
    assert mgr._price_optimal_release_h(11.0, 16.0) is None


def test_price_release_none_on_empty_slots():
    mgr = _make_mgr(_price_ctrl([]))
    assert mgr._price_optimal_release_h(11.0, 16.0) is None


def test_price_release_holds_for_cheaper_hour_ahead():
    # Cheapest feasible hour is 13:00 (the trough); at 11:00 with edge 16:00 → hold.
    slots = [_slot(11, 0.21), _slot(12, 0.17), _slot(13, 0.14), _slot(14, 0.18)]
    mgr = _make_mgr(_price_ctrl(slots))
    assert mgr._price_optimal_release_h(11.0, 16.0) == 13.0


def test_price_release_now_when_current_is_cheapest():
    slots = [_slot(11, 0.14), _slot(12, 0.17), _slot(13, 0.21)]
    mgr = _make_mgr(_price_ctrl(slots))
    assert mgr._price_optimal_release_h(11.0, 16.0) == 11.0


def test_price_release_ignores_slots_past_edge():
    # The 13:00 trough sits past the feasibility edge (12.5) → unreachable, so the
    # cheapest *feasible* hour (12:00) wins. This is today's tight-solar shape: the
    # edge precedes the trough, so the trough cannot be captured without risking SOC.
    slots = [_slot(11, 0.21), _slot(12, 0.17), _slot(13, 0.14)]
    mgr = _make_mgr(_price_ctrl(slots))
    assert mgr._price_optimal_release_h(11.0, 12.5) == 12.0


def test_price_release_epsilon_prefers_releasing_now():
    # A negligibly-cheaper hour ahead (< 0.005 €/kWh) must not trigger a hold.
    slots = [_slot(11, 0.150), _slot(13, 0.147)]
    mgr = _make_mgr(_price_ctrl(slots))
    assert mgr._price_optimal_release_h(11.0, 16.0) == 11.0


def test_price_release_counts_current_partial_hour():
    # Called mid-hour (11:30): the 11:00 slot still covers now and counts as current.
    slots = [_slot(11, 0.14), _slot(12, 0.20)]
    mgr = _make_mgr(_price_ctrl(slots))
    assert mgr._price_optimal_release_h(11.5, 16.0) == 11.5
