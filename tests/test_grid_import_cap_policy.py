"""The grid import cap beats policy, never physics, and says when it falls short (#506 step 2).

A discharge blocker that exists for price, schedule or EV reasons gives way
while the physical import would cross the cap. Physical limits (min SOC,
temperature, an offline battery, phase limits, the low-SOC latch) and the
user's own per-battery discharge switch never do; neither does
``curtailment_floor`` (it guards the pre-discharge reserve: kept, see the
step-2 notes). When the meter stays above the cap the binary sensor says so
and one warning is logged per episode.

Closed loop as in ``test_grid_import_cap``: the battery may discharge only
while ``is_discharge_blocked(battery)`` is False, the same check the control
path makes.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.omnibattery import ChargeDischargeController
from tests.conftest import _FakeMarstekDriver

TOL = 20.0
CAP = 4500


class _Battery:
    def __init__(self, soc=60):
        self.name = "b1"
        self.driver = _FakeMarstekDriver()
        self.data = {"battery_soc": soc}
        self.is_available = True


def _ctrl(*, soc=60, reserve=False):
    from types import SimpleNamespace

    ctrl = object.__new__(ChargeDischargeController)
    ctrl.max_contracted_power = CAP
    ctrl.capacity_protection_enabled = reserve
    ctrl.capacity_protection_soc_threshold = 20
    ctrl.battery = _Battery(soc)
    ctrl.coordinators = [ctrl.battery]
    ctrl._non_responsive = SimpleNamespace(is_excluded=lambda _c: False)
    ctrl._measured_battery_power = lambda: None
    ctrl.previous_power = 0.0
    ctrl._excluded_included_adjustment = 0.0
    ctrl._setpoint_offsets = {"user_target": 0.0}
    ctrl._setpoint_overrides = {}
    ctrl._capacity_protection_active = False
    ctrl._capacity_protection_force_idle = False
    ctrl._capacity_protection_status = {}
    ctrl._global_discharge_blockers = {}
    ctrl._battery_discharge_blockers = {}
    ctrl.deadband = 50
    ctrl._effective_system_capacity = lambda batteries, is_charging: 2500.0 * len(batteries)
    return ctrl


def _block(ctrl, source, *, per_battery=False):
    record = {"reason": source, "details": None, "since": None}
    if per_battery:
        ctrl._battery_discharge_blockers.setdefault(ctrl.battery, {})[source] = record
    else:
        ctrl._global_discharge_blockers[source] = record


def _settle(ctrl, *, house, excluded=0.0, max_w=2500.0, d=0.0, steps=120):
    for _ in range(steps):
        grid = house + excluded - d
        ctrl.previous_power = -d
        ctrl._excluded_included_adjustment = excluded
        t, s = ctrl._apply_capacity_protection(grid - excluded, 0.0)
        if ctrl._capacity_protection_force_idle:
            ctrl._capacity_protection_force_idle = False
            d = 0.0
            continue
        d += 0.5 * (s - t)
        d = max(0.0, min(max_w, d))
        if ctrl.is_discharge_blocked(ctrl.battery):
            d = 0.0
    return house + excluded - d, d


POLICY_GLOBAL = [
    "price_discharge", "price_reserve", "curtailment_negative_window",
    "weekly_refill_hold", "weekly_full_hold", "time_slot_floor_hold",
    "ev_pause", "ev_charging", "excluded_device_dynamic_power_control",
]


@pytest.mark.parametrize("source", POLICY_GLOBAL)
def test_the_cap_overrides_a_fleet_wide_policy_block(source):
    ctrl = _ctrl()
    _block(ctrl, source)
    grid, d = _settle(ctrl, house=6000)
    assert grid == pytest.approx(CAP, abs=TOL)
    assert d == pytest.approx(1500, abs=TOL)


def test_the_cap_overrides_a_time_slot_window():
    ctrl = _ctrl()
    _block(ctrl, "time_slot_discharge", per_battery=True)
    grid, _ = _settle(ctrl, house=6000)
    assert grid == pytest.approx(CAP, abs=TOL)


@pytest.mark.parametrize(
    "source", ["min_soc", "user_battery_discharge_disabled", "curtailment_floor"]
)
def test_physical_user_and_reserve_blocks_are_never_overridden(source):
    ctrl = _ctrl()
    _block(ctrl, source, per_battery=True)
    grid, d = _settle(ctrl, house=6000)
    assert d == 0.0
    assert grid == pytest.approx(6000)


def test_a_policy_block_holds_below_the_cap():
    # Under the cap a price block keeps the battery idle, as before.
    ctrl = _ctrl()
    _block(ctrl, "price_discharge")
    grid, d = _settle(ctrl, house=3000)
    assert d == 0.0
    assert ctrl.is_discharge_blocked() is True


def test_483_longb99_price_block_with_an_excluded_device():
    ctrl = _ctrl()
    _block(ctrl, "price_discharge")
    grid, _ = _settle(ctrl, house=500, excluded=5200)
    assert grid == pytest.approx(CAP, abs=TOL)


def test_the_early_return_does_not_stop_a_cap_discharge():
    # The cycle stops a discharging battery when is_discharge_blocked() reads
    # True before it ever reads the meter; while the cap is engaged it must not.
    ctrl = _ctrl()
    _block(ctrl, "price_discharge")
    _settle(ctrl, house=6000)
    assert ctrl.previous_power < 0
    assert ctrl.is_discharge_blocked() is False


def test_the_block_returns_once_the_house_is_back_under_the_cap():
    ctrl = _ctrl()
    _block(ctrl, "price_discharge")
    _, d = _settle(ctrl, house=6000)
    _settle(ctrl, house=3000, d=d)
    assert ctrl.is_discharge_blocked() is True
    assert ctrl._capacity_protection_status["grid_import_cap_active"] is False


def test_blockers_shown_and_enforced_while_the_cap_is_engaged():
    ctrl, a, b = _two()
    _block(ctrl, "price_discharge")
    ctrl._battery_discharge_blockers[b] = {"min_soc": {"reason": "min_soc", "details": None, "since": None}}
    _settle_two(ctrl, house=6000)
    assert ctrl._cap_overrides_policy is True
    assert "price_discharge" not in ctrl.get_discharge_blockers(a)
    assert "min_soc" in ctrl.get_discharge_blockers(b)
    assert ctrl.is_discharge_blocked(b) is True


# --- Observability (D3) ---------------------------------------------------------

def test_status_reports_requested_and_delivered():
    ctrl = _ctrl()
    _block(ctrl, "user_battery_discharge_disabled", per_battery=True)
    _settle(ctrl, house=6000)
    status = ctrl._capacity_protection_status
    assert status["cap_requested_w"] == 1500
    assert status["cap_delivered_w"] == 0


def test_one_warning_per_uncovered_episode(caplog):
    ctrl = _ctrl()
    _block(ctrl, "min_soc", per_battery=True)
    with caplog.at_level(logging.WARNING):
        _settle(ctrl, house=6000, steps=3)
        assert "could not keep" not in caplog.text  # not sustained yet
        ctrl._cap_shortfall_since = time.monotonic() - 31
        _settle(ctrl, house=6000, steps=5)
        assert caplog.text.count("could not keep") == 1
        # Episode ends under the cap; a new one warns again.
        _settle(ctrl, house=3000, steps=2)
        _settle(ctrl, house=6000, steps=2)
        ctrl._cap_shortfall_since = time.monotonic() - 31
        _settle(ctrl, house=6000, steps=2)
        assert caplog.text.count("could not keep") == 2


def test_a_saturated_battery_is_reported_too():
    # Nothing holds the battery back, it simply is not big enough.
    ctrl = _ctrl()
    grid, d = _settle(ctrl, house=8000)
    assert d == pytest.approx(2500, abs=TOL)
    assert ctrl._capacity_protection_status["cap_requested_w"] == 3500
    assert ctrl._capacity_protection_status["cap_delivered_w"] == pytest.approx(2500, abs=TOL)


def test_slot_protection_discharge_ignores_policy_but_not_physics():
    ctrl = _ctrl()
    _block(ctrl, "time_slot_discharge", per_battery=True)
    assert ctrl.is_discharge_blocked(ctrl.battery, ignore_economic=True, ignore_policy=True) is False
    _block(ctrl, "min_soc", per_battery=True)
    assert ctrl.is_discharge_blocked(ctrl.battery, ignore_economic=True, ignore_policy=True) is True


# --- The planner: a no-discharge window no longer blocks peaks ------------------

def test_a_no_discharge_window_still_drains_the_load_above_the_cap():
    from custom_components.omnibattery.pricing.plan_inputs import battery_idle_intervals

    start = datetime(2026, 11, 10, 18, tzinfo=timezone.utc)
    hour = [(start, start + timedelta(hours=1))]
    intervals = battery_idle_intervals(
        hour, [6.0], [0.0], held_until=None, limit_kw=4.5,
        fleet_blocks=["window"], block_matches=lambda _i, _b: True,
        blocked_price_slots=None, tz=timezone.utc,
    )
    assert intervals[0].consumption_kwh == pytest.approx(1.5)
    no_cap = battery_idle_intervals(
        hour, [6.0], [0.0], held_until=None, limit_kw=0.0,
        fleet_blocks=["window"], block_matches=lambda _i, _b: True,
        blocked_price_slots=None, tz=timezone.utc,
    )
    assert no_cap[0].consumption_kwh == 0.0


# --- The control cycle's own policy gates (price, EV, solar-surplus device) ------

def _cycle(**overrides):
    import asyncio

    from tests.test_control_sample_scheduling import _main_controller, _state

    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    controller = _main_controller({"state": _state(6000, first)}, [])
    for key, value in overrides.items():
        setattr(controller, key, value)
    asyncio.run(controller._run_control_cycle(now=first + timedelta(seconds=1)))
    return controller


GATES = {
    "price": {"_price_based_discharge_blocked": True,
              "_operation_blockers_for_log": lambda _charging: "price_discharge"},
    "solar_surplus_device": {"_solar_surplus_discharge_blocked": True},
}


def _ev(pause, charging):
    from types import SimpleNamespace

    return {"_external_loads": SimpleNamespace(
        calculate_adjustment=lambda: 0.0,
        check_ev_charger_state=lambda: (pause, charging),
    )}


GATES["ev_pause"] = _ev(True, False)
GATES["ev_charging"] = _ev(False, True)


@pytest.mark.parametrize("gate", list(GATES))
def test_the_cycle_lets_the_cap_discharge_through_policy_gates(gate):
    assert _cycle(**GATES[gate]).previous_power == 0  # the gate holds on its own
    assert _cycle(**GATES[gate], _cap_overrides_policy=True).previous_power < 0


def test_the_binary_sensor_publishes_the_cap_attributes():
    from types import SimpleNamespace

    from custom_components.omnibattery.binary_sensor import CapacityProtectionStatusSensor

    ctrl = _ctrl()
    _block(ctrl, "price_discharge")
    _settle(ctrl, house=6000)
    entity = CapacityProtectionStatusSensor(None, SimpleNamespace(entry_id="e"), ctrl)
    attrs = entity.extra_state_attributes
    assert attrs["grid_import_cap_active"] is True
    assert attrs["cap_requested_w"] == 1500
    assert attrs["cap_delivered_w"] == pytest.approx(1500, abs=TOL)


# --- Mixed fleet: one battery held by policy, another by physics -----------------

def _two(soc=60):
    ctrl = _ctrl(soc=soc)
    a, b = ctrl.battery, _Battery(soc)
    b.name = "b2"
    ctrl.coordinators = [a, b]
    return ctrl, a, b


def _settle_two(ctrl, *, house, max_w=2500.0, steps=150):
    a, b = ctrl.coordinators
    da = db = 0.0
    for _ in range(steps):
        grid = house - da - db
        ctrl.previous_power = -(da + db)
        t, s = ctrl._apply_capacity_protection(grid, 0.0)
        step = 0.5 * (s - t)
        free = [c for c in (a, b) if not ctrl.is_discharge_blocked(c)]
        share = step / len(free) if free else 0.0
        da = max(0.0, min(max_w, da + share)) if a in free else 0.0
        db = max(0.0, min(max_w, db + share)) if b in free else 0.0
    return house - da - db, da, db


def test_mixed_fleet_the_policy_held_battery_covers_the_excess():
    ctrl, a, b = _two()
    _block(ctrl, "time_slot_discharge", per_battery=True)  # on a
    ctrl._battery_discharge_blockers[b] = {"min_soc": {"reason": "min_soc", "details": None, "since": None}}
    grid, da, db = _settle_two(ctrl, house=6000)
    assert db == 0.0
    assert grid == pytest.approx(CAP, abs=TOL)


def test_mixed_fleet_a_free_battery_that_holds_the_cap_leaves_the_policy_alone():
    # b is free and keeps the meter under the cap on its own: a stays blocked.
    ctrl, a, b = _two()
    _block(ctrl, "time_slot_discharge", per_battery=True)  # on a
    grid, da, db = _settle_two(ctrl, house=6000)
    assert da == 0.0
    assert db == pytest.approx(2500, abs=TOL)
    assert grid == pytest.approx(3500, abs=TOL)


def test_mixed_fleet_a_free_battery_too_small_gets_help():
    ctrl, a, b = _two()
    _block(ctrl, "time_slot_discharge", per_battery=True)  # on a
    grid, da, db = _settle_two(ctrl, house=8000)
    assert grid == pytest.approx(CAP, abs=TOL)
    assert da > 0


# --- A cycle that never evaluates the cap releases it ----------------------------

async def _true(*_a, **_k):
    return True


async def _noop(*_a, **_k):
    return None


def _exit_overrides(exit_name):
    from types import SimpleNamespace

    if exit_name == "manual_mode":
        return {
            "manual_mode_enabled": True,
            "_pricing_mgr": SimpleNamespace(
                maybe_check_price_data_health=lambda: None,
                clear_curtailment_runtime=lambda _reason: None,
            ),
            "_surplus_hold_mgr": None,
            "_discharge_reserve_mgr": None,
            "_apply_software_manual_setpoints": _noop,
        }
    if exit_name == "dead_meter":
        return {"_apply_dead_meter_fallback": _true}
    return {"_max_soc_mgr": SimpleNamespace(handle_measurement=_true)}


@pytest.mark.parametrize("exit_name", ["manual_mode", "dead_meter", "max_soc_measurement"])
def test_a_cycle_that_skips_the_cap_releases_its_override(exit_name):
    controller = _cycle(
        **_exit_overrides(exit_name),
        _cap_overrides_policy=True,
        _cap_shave_engaged=True,
        _capacity_protection_status={"grid_import_cap_active": True, "cap_requested_w": 900},
    )
    assert controller._cap_overrides_policy is False
    assert controller._cap_shave_engaged is False
    assert controller._capacity_protection_status["grid_import_cap_active"] is False


def test_release_helper_clears_flags_and_status():
    ctrl = _ctrl()
    _block(ctrl, "price_discharge")
    _settle(ctrl, house=6000)
    assert ctrl._cap_overrides_policy is True
    ctrl._release_grid_import_cap()
    assert ctrl._cap_overrides_policy is False
    assert ctrl.is_discharge_blocked() is True
    assert ctrl._capacity_protection_status["grid_import_cap_active"] is False


# --- A blocker whose owner yields to peak shaving must not make the cap flap ------

def test_the_discharge_reserve_does_not_withdraw_while_the_cap_overrides_it():
    """The reserve releases its price_reserve block while peak shaving is
    active. Recomputed every cycle, that used to drop the cap's own reason
    to engage: the cap let go, the reserve blocked again, and so on."""
    from custom_components.omnibattery.control.discharge_reserve import DischargeReserveManager

    ctrl = _ctrl()
    manager = object.__new__(DischargeReserveManager)
    manager._controller = ctrl
    d, engagements, was = 0.0, 0, False
    house = 6000.0
    for _ in range(120):
        # What _refresh_price_reserve_blocks does each cycle.
        if manager._release_guard() is None:
            _block(ctrl, "price_reserve", per_battery=True)
        else:
            ctrl._battery_discharge_blockers.pop(ctrl.battery, None)
        ctrl.previous_power = -d
        t, s = ctrl._apply_capacity_protection(house - d, 0.0)
        engaged = bool(ctrl._cap_shave_engaged)
        engagements += engaged and not was
        was = engaged
        d = max(0.0, min(2500.0, d + 0.5 * (s - t)))
        if ctrl.is_discharge_blocked(ctrl.battery):
            d = 0.0
    assert engagements == 1
    assert house - d == pytest.approx(CAP, abs=TOL)
