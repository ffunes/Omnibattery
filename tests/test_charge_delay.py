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

from custom_components.marstek_venus_energy_manager.charge_delay import (
    ChargeDelayManager,
)
from custom_components.marstek_venus_energy_manager.const import (
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
    # avg_soc 30, min_soc 20, capacity 10 -> usable ~1 kWh; forecast 1*0.85=0.85;
    # consumption 5 -> (1 + 0.85) < 5 -> grid needed -> unlock(low_forecast)
    ctrl = _controller(
        coordinators=[_coord(soc=30, total_energy=10.0, min_soc=20)],
        _consumption_tracker=_tracker(get_avg_daily_consumption=lambda: 5.0),
    )
    mgr = _make_mgr(ctrl, states={"sensor.forecast": _state(1.0)})
    assert mgr._should_delay_charge(80) is False
    assert ctrl._charge_delay_status["unlock_reason"] == "low_forecast"


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
