"""Issue #683: a predictive slot must not raise the import peak it regulates.

Trace of 2026-11-10: peak limit 4400 W, contracted power 5000 W, charge floor
140 W. A slot started at the full ceiling on top of a 3.1 kW house, the next PD
step raised it to the 5000 W battery maximum (8 kW import), and for the rest of
the slot the charge sat on its positive floor while the house alone was above
the limit, without ever handing over to peak protection.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_control_sample_scheduling import (
    _predictive_controller,
    _state,
)

CEILING_W = 4400.0
FLOOR_W = 140.0


def _slot(*, previous_power=-FLOOR_W, protection=True, contracted=5000.0):
    """Controller in an active slot, using the settings of the reported trace."""
    first_report = datetime.now(timezone.utc)
    state_holder = {"state": None, "first_report": first_report, "step": 0}
    writes = []
    controller = _predictive_controller(state_holder, writes)
    controller.max_contracted_power = contracted
    controller.capacity_protection_enabled = protection
    controller.capacity_protection_limit = CEILING_W
    controller._capacity_protection_status = {}
    controller._capacity_protection_active = False
    controller.min_charge_power = FLOOR_W
    controller._effective_system_capacity = lambda _batteries, is_charging: 5000.0
    controller.previous_power = previous_power
    return controller, state_holder, writes


def _publish(controller, state_holder, grid):
    """Run one control cycle on a fresh meter publication two seconds later."""
    state_holder["state"] = _state(
        grid,
        state_holder["first_report"] + timedelta(seconds=2 * state_holder["step"]),
    )
    state_holder["step"] += 1
    asyncio.run(controller._handle_predictive_grid_charging())


def _closed_loop(controller, state_holder, writes, houses, *, telemetry=True):
    """Feed meter samples from an ideal plant: grid = house + battery power.

    Returns the battery power (+charge / -discharge) left after each cycle.
    """
    battery = -controller.previous_power
    result = []
    for house in houses:
        if telemetry:
            controller._measured_battery_power = lambda value=battery: value
        _publish(controller, state_holder, house + battery)
        if writes:
            battery = writes[-1][1] - writes[-1][2]
        result.append(battery)
    return result


# --- Gap 3: positive charge floor while the grid is already above the ceiling


def test_charge_floor_is_dropped_while_the_grid_is_above_the_ceiling():
    # House 100 W above the ceiling, no peak protection configured: the floor
    # only adds import, so the command must reach 0 W and stay there.
    controller, state_holder, writes = _slot(protection=False, contracted=CEILING_W)

    commands = _closed_loop(
        controller, state_holder, writes, [4500, 4501, 4500, 4501], telemetry=False
    )

    assert commands == [0, 0, 0, 0]
    assert controller.previous_power == 0
    assert controller._predictive_charge_suspended_for_demand is False


def test_charge_floor_returns_once_it_fits_under_the_ceiling():
    controller, state_holder, writes = _slot(protection=False, contracted=CEILING_W)
    _closed_loop(controller, state_holder, writes, [4500, 4501], telemetry=False)
    assert writes[-1][1:] == (0, 0)

    commands = _closed_loop(
        controller, state_holder, writes, [4000, 4001], telemetry=False
    )

    assert commands[0] >= FLOOR_W
    assert all(command >= FLOOR_W for command in commands)


def test_charge_floor_is_kept_inside_the_deadband_of_the_ceiling():
    # Grid 30-31 W above the ceiling with the floor engaged: an ordinary
    # overshoot, the floor stays exactly as before.
    controller, state_holder, writes = _slot(protection=False, contracted=CEILING_W)

    commands = _closed_loop(
        controller, state_holder, writes, [4290, 4291, 4290, 4291], telemetry=False
    )

    assert commands == [FLOOR_W] * 4


def test_house_hovering_under_the_ceiling_does_not_toggle_the_floor():
    # The floor itself (140 W) is larger than the trigger (50 W). With the house
    # at ceiling - 70 W, returning to the floor as soon as the grid is back under
    # the ceiling would re-trip the release on the next sample, forever.
    controller, state_holder, writes = _slot(protection=False, contracted=CEILING_W)

    commands = _closed_loop(
        controller,
        state_holder,
        writes,
        [4330 + (step % 2) for step in range(10)],
        telemetry=False,
    )

    changes = sum(1 for before, after in zip(commands, commands[1:]) if before != after)
    assert changes == 0
    assert commands[-1] == 0


def test_rate_limiter_still_governs_the_way_down_to_the_floor():
    # A large correction is rate-limited: the command walks down to 0 W in
    # steps of at most max_power_change_per_cycle and never crosses into discharge.
    controller, state_holder, writes = _slot(
        previous_power=-500.0, protection=False, contracted=CEILING_W
    )
    controller.max_power_change_per_cycle = 400.0

    _publish(controller, state_holder, 8000)
    assert controller.previous_power == -100.0
    assert writes[-1][1:] == (100.0, 0)

    _publish(controller, state_holder, 7640)
    assert controller.previous_power == 0
    assert writes[-1][1:] == (0, 0)


def test_stale_pass_does_not_bring_the_floor_back():
    controller, state_holder, writes = _slot(protection=False, contracted=CEILING_W)
    _closed_loop(controller, state_holder, writes, [4500, 4501], telemetry=False)
    assert controller.previous_power == 0

    # Meter silent beyond the stale tolerance: the safety pass keeps the idle
    # command instead of re-deciding on an old sample.
    state_holder["state"] = _state(
        4000, datetime.now(timezone.utc) - timedelta(seconds=70)
    )
    asyncio.run(controller._handle_predictive_grid_charging())
    asyncio.run(controller._handle_predictive_grid_charging())

    assert controller.previous_power == 0
    assert writes[-1][1:] == (0, 0)


# --- Gap 1: a new slot starts with the headroom the house leaves


def _new_slot(*, grid, battery=None):
    """Controller about to initialize a fresh slot (no resume, no history)."""
    controller, state_holder, writes = _slot(previous_power=0.0)
    controller._grid_charging_initialized = False
    controller._predictive_resume_charge_power = None
    controller._compute_predictive_target_soc = lambda: {controller.coordinators[0]: 80.0}
    controller._measured_battery_power = lambda: battery
    _publish(controller, state_holder, grid)
    return controller, state_holder, writes


def test_slot_starts_with_the_headroom_the_house_leaves():
    controller, _, writes = _new_slot(grid=3100)

    assert writes[-1][1] == pytest.approx(CEILING_W - 3100)
    assert FLOOR_W <= writes[-1][1] <= 1300


def test_slot_start_removes_a_discharging_battery_from_the_house_estimate():
    # The battery was covering 500 W of the house: the grid shows 2600 W but
    # the house is 3100 W, so the headroom is the same 1300 W (not 1800 W).
    controller, _, writes = _new_slot(grid=2600, battery=-500.0)

    assert writes[-1][1] == pytest.approx(CEILING_W - 3100)


def test_slot_start_never_exceeds_the_headroom_when_the_house_is_above_the_ceiling():
    controller, _, writes = _new_slot(grid=4700)

    assert writes[-1][1] == FLOOR_W


def test_slot_start_without_telemetry_uses_the_grid_reading():
    controller, _, writes = _new_slot(grid=3100, battery=None)

    assert writes[-1][1] == pytest.approx(CEILING_W - 3100)


def test_meter_samples_inside_the_settle_window_do_not_move_the_start_command():
    controller, state_holder, writes = _new_slot(grid=3100)
    start = writes[-1][1]
    assert controller._predictive_demand_settle_window_s() > 0

    # Samples that would make PD move the command (grid far under the ceiling).
    _publish(controller, state_holder, 3500)
    _publish(controller, state_holder, 3900)

    assert writes[-1][1] == pytest.approx(start)
    assert controller.previous_power == pytest.approx(-start)


def test_pd_moves_the_command_once_the_settle_window_has_passed():
    controller, state_holder, writes = _new_slot(grid=3100)
    start = writes[-1][1]
    controller._predictive_slot_start_monotonic -= 60.0

    # Grid 500 W under the ceiling: PD asks for more charge.
    _publish(controller, state_holder, 3900)

    assert writes[-1][1] > start


def test_resume_from_protection_keeps_its_behaviour():
    controller, state_holder, writes = _slot(previous_power=0.0)
    controller._grid_charging_initialized = False
    controller._predictive_resume_charge_power = 600.0
    controller._compute_predictive_target_soc = lambda: {controller.coordinators[0]: 80.0}
    controller._measured_battery_power = lambda: None
    _publish(controller, state_holder, 3100)

    assert controller._predictive_slot_start_monotonic == 0.0
    # Resume power is used as is, and PD acts on the very first sample.
    assert writes[-1][1] != pytest.approx(CEILING_W - 3100)


# --- Gap 2: the peak case is confirmed faster than the emergency case


def _confirm(controller, grid, *, fresh=True, battery=None):
    controller._measured_battery_power = lambda: battery
    return controller._predictive_hard_limit_confirmed(
        sensor_filtered=grid,
        target_power=CEILING_W,
        has_fresh_publication=fresh,
        sensor_within_stale_tolerance=fresh,
    )


def test_peak_is_confirmed_on_the_second_fresh_sample():
    controller, _, _ = _slot()
    controller.deadband = 50.0

    assert _confirm(controller, 4650) is False
    assert _confirm(controller, 4650) is True


def test_peak_just_above_the_trigger_counts():
    controller, _, _ = _slot()
    controller.deadband = 50.0

    assert _confirm(controller, 4460) is False
    assert _confirm(controller, 4460) is True


def test_peak_inside_the_trigger_does_not_count():
    controller, _, _ = _slot()
    controller.deadband = 50.0

    assert _confirm(controller, 4440) is False
    assert _confirm(controller, 4440) is False
    assert controller._predictive_hard_limit_samples == 0


def test_peak_uses_the_house_not_the_charge_command():
    controller, _, _ = _slot()
    controller.deadband = 50.0

    # 4700 W on the meter of which 400 W is the battery charging: house 4300 W.
    assert _confirm(controller, 4700, battery=400.0) is False
    assert _confirm(controller, 4700, battery=400.0) is False


def test_no_peak_protection_and_load_under_contracted_plus_margin_is_not_confirmed():
    controller, _, _ = _slot(protection=False, contracted=5000.0)
    controller.deadband = 50.0

    for _ in range(4):
        assert _confirm(controller, 5100) is False


def test_contracted_power_emergency_still_needs_three_samples():
    controller, _, _ = _slot(protection=False, contracted=5000.0)
    controller.deadband = 50.0

    assert _confirm(controller, 5300) is False
    assert _confirm(controller, 5300) is False
    assert _confirm(controller, 5300) is True


def test_a_stale_sample_resets_the_peak_count():
    controller, _, _ = _slot()
    controller.deadband = 50.0

    assert _confirm(controller, 4650) is False
    assert _confirm(controller, 4650, fresh=False) is False
    assert controller._predictive_hard_limit_samples == 0
    assert _confirm(controller, 4650) is False
    assert _confirm(controller, 4650) is True


def test_settle_window_with_a_battery_past_its_target_does_not_raise():
    # A battery past its predictive target may still absorb export (#470).
    # That path scales by the PD step, which the settle window skips.
    controller, state_holder, writes = _new_slot(grid=3100)
    main = controller.coordinators[0]
    extra = type(main)(name="past-target")
    controller.coordinators.append(extra)
    controller._get_available_batteries = (
        lambda is_charging, ignore_predictive_target=False, **_kwargs: (
            [main, extra] if ignore_predictive_target else [main]
        )
    )

    _publish(controller, state_holder, 3500)

    assert controller._predictive_surplus_power == 0.0
