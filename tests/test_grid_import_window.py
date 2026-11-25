"""15-minute billing window for the grid import cap (#506 step 4).

With the window switch on, the cap limits the mean import of the current
local quarter (:00/:15/:30/:45) instead of every instant:

    cap = (techo * 900 s - E_q) / t_rest      (W allowed for the rest)

E_q is the quarter's import measured from the grid meter. Within 30 s of a
boundary the cap never exceeds the configured value (meter clock drift);
after a restart, a meter gap or a long sample across a boundary the quarter
is not trusted and the cap is the configured value until the next boundary.
Grid charging never goes above the configured value (min(window, techo)).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.tracking.consumption_tracker import (
    ConsumptionTracker,
    grid_import_window_default,
    grid_window_cap_w,
)
from tests.conftest import _FakeMarstekDriver

CAP = 2500.0
Q0 = datetime(2026, 11, 16, 10, 0, tzinfo=timezone(timedelta(hours=1)))


def _tracker(*, valid_at=Q0):
    """A tracker whose quarter starting at ``valid_at`` is trusted."""
    t = object.__new__(ConsumptionTracker)
    if valid_at is not None:
        t.update_grid_window(valid_at - timedelta(seconds=2), 0.0, 2.0)
        t.update_grid_window(valid_at, 0.0, 2.0)
    return t


# --- The accumulator ---------------------------------------------------------------

def test_the_quarter_resets_at_the_local_boundary():
    t = _tracker()
    t.update_grid_window(Q0 + timedelta(minutes=14, seconds=58), 0.5, 2.0)
    assert t.window_import_kwh == pytest.approx(0.5)
    t.update_grid_window(Q0 + timedelta(minutes=15), 0.01, 2.0)
    # A sample that crosses the boundary belongs to the new quarter.
    assert t.window_quarter == Q0 + timedelta(minutes=15)
    assert t.window_import_kwh == pytest.approx(0.01)
    assert t.window_valid is True


def test_export_does_not_discount(monkeypatch):
    t = _tracker()
    ctrl = SimpleNamespace(
        _apply_meter_transform=lambda state: state, consumption_sensor="sensor.grid",
        _daily_grid_import_energy_kwh=0.0, _daily_grid_export_energy_kwh=0.0,
    )
    t._controller = ctrl
    readings = iter([3600.0, -3600.0])
    t._hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: next(readings)))
    clock = iter([100.0, 101.0])
    monkeypatch.setattr(
        "custom_components.omnibattery.tracking.consumption_tracker.monotonic",
        lambda: next(clock),
    )
    t._daily_grid_last_time = 99.0
    t._daily_grid_last_power_kw = 3.6
    import asyncio

    asyncio.run(t.accumulate_daily_grid_energy(now=Q0 + timedelta(seconds=40)))
    after_import = t.window_import_kwh
    assert after_import == pytest.approx(0.001)  # 3.6 kW for 1 s
    asyncio.run(t.accumulate_daily_grid_energy(now=Q0 + timedelta(seconds=41)))
    # 3.6 kW -> -3.6 kW: half a second of import, then export that counts nothing.
    assert t.window_import_kwh == pytest.approx(after_import + 0.00025)


# --- Validity --------------------------------------------------------------------------

def test_a_restart_mid_quarter_is_not_trusted_until_the_next_boundary():
    t = object.__new__(ConsumptionTracker)
    t.update_grid_window(Q0 + timedelta(minutes=7), 0.0, None)
    t.update_grid_window(Q0 + timedelta(minutes=7, seconds=2), 0.001, 2.0)
    assert t.window_valid is False
    t.update_grid_window(Q0 + timedelta(minutes=15), 0.001, 2.0)
    assert t.window_valid is True


def test_a_meter_gap_invalidates_the_quarter():
    t = _tracker()
    t.update_grid_window(Q0 + timedelta(minutes=3), None, None)
    t.update_grid_window(Q0 + timedelta(minutes=3, seconds=10), 0.001, None)
    assert t.window_valid is False
    t.update_grid_window(Q0 + timedelta(minutes=15), 0.001, 2.0)
    assert t.window_valid is True


def test_a_long_sample_across_the_boundary_is_not_trusted():
    t = _tracker()
    t.update_grid_window(Q0 + timedelta(minutes=15, seconds=20), 0.05, 45.0)
    assert t.window_valid is False


# --- The effective cap ----------------------------------------------------------------

def test_half_the_budget_with_half_the_quarter_left_is_the_cap():
    used = CAP * 450 / 3.6e6
    assert grid_window_cap_w(CAP, Q0, Q0 + timedelta(seconds=450), used) == pytest.approx(CAP)


def test_a_spent_budget_allows_nothing():
    used = CAP * 900 / 3.6e6
    assert grid_window_cap_w(CAP, Q0, Q0 + timedelta(seconds=600), used) == 0.0


def test_quiet_minutes_raise_the_cap():
    used = 500 * 600 / 3.6e6
    assert grid_window_cap_w(CAP, Q0, Q0 + timedelta(seconds=600), used) == pytest.approx(6500)


@pytest.mark.parametrize("seconds", [5, 29, 871, 895])
def test_the_guard_band_never_exceeds_the_cap(seconds):
    # Budget nearly unused: the formula alone would allow far more.
    assert grid_window_cap_w(CAP, Q0, Q0 + timedelta(seconds=seconds), 0.0) <= CAP


def test_past_the_quarter_end_the_configured_value_holds():
    assert grid_window_cap_w(CAP, Q0, Q0 + timedelta(seconds=905), 0.9) == CAP


# --- The controller --------------------------------------------------------------------

class _Battery:
    def __init__(self):
        self.name = "b1"
        self.driver = _FakeMarstekDriver()
        self.data = {"battery_soc": 60}
        self.is_available = True


def _ctrl(tracker=None, *, window=True, price_block=True):
    ctrl = object.__new__(ChargeDischargeController)
    ctrl.max_contracted_power = CAP
    ctrl.grid_import_window_enabled = window
    ctrl._consumption_tracker = tracker
    ctrl.capacity_protection_enabled = False
    ctrl.capacity_protection_soc_threshold = 20
    ctrl.battery = _Battery()
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
    ctrl._global_discharge_blockers = (
        {"price_discharge": {"reason": "price", "details": None, "since": None}}
        if price_block else {}
    )
    ctrl._battery_discharge_blockers = {}
    ctrl._effective_system_capacity = lambda batteries, is_charging: 2500.0 * len(batteries)
    ctrl.deadband = 50
    return ctrl


def test_switch_off_is_the_instantaneous_cap():
    t = _tracker()
    t.update_grid_window(Q0 + timedelta(seconds=600), 500 * 600 / 3.6e6, 2.0)
    assert _ctrl(t, window=False)._grid_import_cap_w() == CAP
    assert _ctrl(t, window=True)._grid_import_cap_w() == pytest.approx(6500)


def test_an_untrusted_quarter_is_the_instantaneous_cap():
    t = object.__new__(ConsumptionTracker)
    t.update_grid_window(Q0 + timedelta(seconds=600), 0.0, None)
    assert _ctrl(t)._grid_import_cap_w() == CAP


def test_grid_charging_never_goes_above_the_configured_value():
    t = _tracker()
    t.update_grid_window(Q0 + timedelta(seconds=600), 500 * 600 / 3.6e6, 2.0)
    ctrl = _ctrl(t)
    assert ctrl._grid_import_cap_w() > CAP
    assert ctrl._grid_charge_cap_w() == CAP
    assert ctrl._predictive_charge_ceiling() == CAP


def test_a_spent_budget_stops_grid_charging_and_covers_the_house():
    t = _tracker()
    t.update_grid_window(Q0 + timedelta(seconds=600), CAP * 900 / 3.6e6, 2.0)
    ctrl = _ctrl(t)
    assert ctrl._grid_import_cap_w() == 0.0
    assert ctrl._grid_charge_cap_w() == 0.0
    # 0 W effective is not "cap off": the held battery covers the whole house.
    target, sensor = ctrl._apply_capacity_protection(1500.0, 0.0)
    assert ctrl._capacity_protection_status["grid_import_cap_active"] is True
    assert target == 0.0 and sensor == 1500.0
    assert ctrl.is_discharge_blocked(ctrl.battery) is False


def test_the_planner_ceiling_ignores_the_window():
    from custom_components.omnibattery.pricing.engine import PricingManager

    t = _tracker()
    t.update_grid_window(Q0 + timedelta(seconds=600), 0.0, 2.0)
    for window in (False, True):
        manager = object.__new__(PricingManager)
        manager._controller = _ctrl(t, window=window)
        assert manager._contracted_charge_ceiling() == CAP


@pytest.mark.parametrize(
    ("country", "expected"), [("BE", True), ("ES", False), (None, False)]
)
def test_the_switch_default_follows_the_country(country, expected):
    assert grid_import_window_default(country) is expected


# --- One whole quarter, closed loop ---------------------------------------------------

def _quarter(house_at, *, max_w=2500.0, energy_wh=1e9, step=2.0):
    """Run one quarter; return (mean import W, discharge Wh, engagements)."""
    t = _tracker()
    ctrl = _ctrl(t)
    d = 0.0
    used_wh = 0.0
    engagements = 0
    was = False
    now = Q0
    while now < Q0 + timedelta(seconds=900):
        now += timedelta(seconds=step)
        house = house_at((now - Q0).total_seconds())
        grid = house - d
        t.update_grid_window(now, max(grid, 0.0) * step / 3.6e6, step)
        ctrl.previous_power = -d
        target, sensor = ctrl._apply_capacity_protection(grid, 0.0)
        engaged = bool(getattr(ctrl, "_cap_shave_engaged", False))
        engagements += engaged and not was
        was = engaged
        d += 0.5 * (sensor - target)
        limit = max_w if used_wh < energy_wh else 0.0
        d = max(0.0, min(limit, d))
        if ctrl.is_discharge_blocked(ctrl.battery):
            d = 0.0
        used_wh += d * step / 3600.0
    return t.window_import_kwh * 3.6e6 / 900.0, used_wh, engagements


def test_kettle_at_the_start_of_the_quarter_is_shaved_as_today():
    mean, used, engagements = _quarter(lambda s: 3500.0 if s < 120 else 500.0)
    assert mean <= CAP * 1.01
    assert used > 20  # the battery covered the kettle above the cap
    assert engagements <= 2


def test_kettle_after_quiet_minutes_is_left_alone():
    mean, used, _ = _quarter(lambda s: 3500.0 if 600 <= s < 720 else 500.0)
    assert used == 0.0
    assert mean <= CAP


def test_a_peak_the_battery_could_not_cover_is_paid_back():
    # 7 kW for a minute with 2 kW of battery; then the house sits at 2.4 kW,
    # under the cap, and the battery pulls the rest of the quarter down.
    mean, _, engagements = _quarter(
        lambda s: 7000.0 if s < 60 else 2400.0, max_w=2000.0
    )
    assert mean <= CAP * 1.01
    assert engagements <= 2


def test_a_house_just_over_the_cap_all_quarter_engages_once():
    mean, _, engagements = _quarter(lambda _s: 2600.0)
    assert mean <= CAP * 1.01
    assert engagements == 1


def test_a_spent_budget_makes_the_battery_cover_the_whole_house():
    # The battery is empty for the first 10 minutes while the house draws
    # 4 kW, so the budget goes; once it has energy again it covers it all.
    t = _tracker()
    ctrl = _ctrl(t)
    t.update_grid_window(Q0 + timedelta(seconds=600), 4000 * 600 / 3.6e6, 2.0)
    target, sensor = ctrl._apply_capacity_protection(1500.0, 0.0)
    assert ctrl._grid_import_cap_w() == 0.0
    assert target == 0.0  # regulate the meter to zero


# --- Observability -----------------------------------------------------------------------

def test_one_warning_per_quarter_over_budget(caplog):
    t = _tracker()
    ctrl = _ctrl(t, price_block=False)
    with caplog.at_level(logging.WARNING):
        t.update_grid_window(Q0 + timedelta(seconds=600), CAP * 899 / 3.6e6, 2.0)
        ctrl._apply_capacity_protection(3000.0, 0.0)
        assert "budget" not in caplog.text
        t.update_grid_window(Q0 + timedelta(seconds=602), 0.01, 2.0)
        ctrl._apply_capacity_protection(3000.0, 0.0)
        ctrl._apply_capacity_protection(3000.0, 0.0)
        assert caplog.text.count("budget") == 1
        t.update_grid_window(Q0 + timedelta(minutes=15), 0.0, 2.0)
        t.update_grid_window(Q0 + timedelta(minutes=25), CAP * 901 / 3.6e6, 2.0)
        ctrl._apply_capacity_protection(3000.0, 0.0)
        assert caplog.text.count("budget") == 2


def test_the_binary_sensor_shows_the_window():
    from custom_components.omnibattery.binary_sensor import CapacityProtectionStatusSensor

    t = _tracker()
    t.update_grid_window(Q0 + timedelta(seconds=600), 500 * 600 / 3.6e6, 2.0)
    ctrl = _ctrl(t)
    ctrl._apply_capacity_protection(500.0, 0.0)
    attrs = CapacityProtectionStatusSensor(None, SimpleNamespace(entry_id="e"), ctrl).extra_state_attributes
    assert attrs["window_valid"] is True
    assert attrs["window_import_kwh"] == pytest.approx(0.083, abs=0.001)
    assert attrs["window_cap_w"] == 6500


# --- The switch -------------------------------------------------------------------------

def _switch_ctrl(data, country):
    ctrl = object.__new__(ChargeDischargeController)
    ctrl.config_entry = SimpleNamespace(data=data)
    ctrl.hass = SimpleNamespace(config=SimpleNamespace(country=country))
    return ctrl


@pytest.mark.parametrize(
    ("data", "country", "expected"),
    [({}, "BE", True), ({}, "ES", False), ({}, None, False),
     ({"grid_import_window_15min": False}, "BE", False),
     ({"grid_import_window_15min": True}, "ES", True)],
)
def test_the_setting_is_the_stored_value_or_the_country_default(data, country, expected):
    assert _switch_ctrl(data, country)._grid_import_window_setting(data) is expected


async def test_the_switch_writes_the_entry_and_the_controller():
    from custom_components.omnibattery.switch import GridImportWindowSwitch

    entry = SimpleNamespace(data={})
    updates = []
    hass = SimpleNamespace(config_entries=SimpleNamespace(
        async_update_entry=lambda e, data: updates.append(data)
    ))
    ctrl = SimpleNamespace(grid_import_window_enabled=False)
    switch = GridImportWindowSwitch(hass, entry, ctrl)
    switch.async_write_ha_state = lambda: None
    await switch.async_turn_on()
    assert ctrl.grid_import_window_enabled is True and switch.is_on
    assert updates[-1]["grid_import_window_15min"] is True


# --- A spent budget on the charge side ---------------------------------------------------

def _spent_tracker():
    t = _tracker()
    t.update_grid_window(Q0 + timedelta(seconds=600), CAP * 900 / 3.6e6, 2.0)
    return t


def test_a_spent_budget_leaves_no_manual_grid_charge():
    ctrl = _ctrl(_spent_tracker())
    ctrl.offgrid_mode_enabled = False
    ctrl.primary_consumption_sensor = "sensor.grid"
    ctrl.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: 1500.0))
    ctrl._apply_meter_transform = lambda state: state
    battery = SimpleNamespace(data={}, manual_force_mode="Charge", manual_set_charge_power=2000,
                              commanded_charge_power=0, commanded_discharge_power=0)
    ctrl._coordinator_delivered_power = lambda _c: 0.0
    assert ctrl._manual_charge_allowance([battery]) == 0.0


def test_a_spent_budget_stops_the_automatic_grid_charge():
    import asyncio

    from tests.test_control_sample_scheduling import _main_controller, _state

    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    controller = _main_controller({"state": _state(1500, first)}, [])
    controller._compute_pd_new_power = lambda *_a: 1000.0
    controller.previous_power = 0.0
    controller.last_output_sign = 0
    controller.max_contracted_power = CAP
    controller.grid_import_window_enabled = True
    spent = _spent_tracker()

    async def _no_accumulate(*_a, **_k):
        return None

    controller._consumption_tracker = SimpleNamespace(
        window_valid=spent.window_valid, window_quarter=spent.window_quarter,
        window_sample_at=spent.window_sample_at, window_import_kwh=spent.window_import_kwh,
        handle_accumulator_daily_reset=lambda: None,
        accumulate_household_consumption=_no_accumulate,
        handle_daily_energy_reset=lambda: None,
        accumulate_daily_solar_energy=_no_accumulate,
        accumulate_daily_home_energy=_no_accumulate,
        accumulate_daily_grid_energy=_no_accumulate,
        maybe_save_accumulators=lambda: None,
    )
    asyncio.run(controller._run_control_cycle(now=first + timedelta(seconds=1)))
    assert controller.previous_power == 0.0
