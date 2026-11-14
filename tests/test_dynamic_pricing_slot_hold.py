"""Dynamic Pricing: a slot that charged holds the battery until it ends.

Field case 2026-11-14, 02:45-04:00, quarter-hour prices, battery empty: every
booked quarter charged its small target (2 -> 4 % in about two minutes),
stopped, and the battery then covered the house for the rest of the quarter,
back to 2 %. Energy bought in a quarter was spent in the same quarter at the
same price, losing the round trip, and the battery switched between charging
and discharging every quarter. The grid now carries the house until the slot
ends.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing import (
    SLOT_PURPOSE_DEFICIT,
    SLOT_PURPOSE_NEGATIVE_PRICE,
)

from tests.test_dynamic_pricing_slot_handover import (  # noqa: F401 - fixture
    _at,
    _balance,
    _Battery,
    _controller,
    _manager,
    _run,
    _schedule,
    _slots,
    clock,
)


def _with_blocks(ctrl):
    ctrl.blocks = {}
    ctrl.set_discharge_block = lambda source, reason, details=None: ctrl.blocks.__setitem__(
        source, details
    )
    ctrl.remove_discharge_block = lambda source: ctrl.blocks.pop(source, None)
    return ctrl


def _setup(purposes, battery, required_kwh):
    ctrl = _with_blocks(
        _controller(battery, None, _schedule(purposes))
    )
    ctrl._last_decision_data = _balance(battery, required_kwh)()
    return ctrl, _manager(ctrl, _balance(battery, required_kwh))


def test_a_slot_that_reached_its_target_holds_the_battery_until_it_ends(clock):
    battery = _Battery(2.0)
    a, b = _slots(_at(9, 30), [0.20, 0.19])
    ctrl, manager = _setup({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}, battery, 0.4)

    _run(manager)
    assert ctrl._current_price_slot_active is True
    manager.apply_dynamic_slot_hold()
    assert "dp_slot_hold" not in ctrl.blocks  # charging: nothing to hold

    battery.soc = 4.0  # the target, two minutes in
    clock.value = _at(9, 32)
    _run(manager)
    assert ctrl._current_price_slot_active is False
    manager.apply_dynamic_slot_hold()
    assert ctrl.blocks["dp_slot_hold"] == {"slot_end": "09:45"}

    clock.value = _at(9, 44)
    manager.apply_dynamic_slot_hold()
    assert "dp_slot_hold" in ctrl.blocks

    # The slot is over: the battery may discharge again (or the next slot
    # charges and holds on its own account).
    clock.value = _at(10, 0)
    manager.apply_dynamic_slot_hold()
    assert "dp_slot_hold" not in ctrl.blocks


def test_a_slot_that_did_not_charge_holds_nothing(clock):
    # Enough stored already: the slot is skipped, the battery covers the house.
    battery = _Battery(50.0)
    (a,) = _slots(_at(9, 30), [0.20])
    ctrl, manager = _setup({a: SLOT_PURPOSE_DEFICIT}, battery, 1.0)

    _run(manager)
    assert ctrl._current_price_slot_active is False
    manager.apply_dynamic_slot_hold()
    assert "dp_slot_hold" not in ctrl.blocks


def test_a_replan_that_withdraws_the_slot_releases_the_hold(clock):
    battery = _Battery(2.0)
    a, b = _slots(_at(9, 30), [0.20, 0.19])
    ctrl, manager = _setup({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}, battery, 0.4)
    _run(manager)
    battery.soc = 4.0
    clock.value = _at(9, 32)
    _run(manager)
    manager.apply_dynamic_slot_hold()
    assert "dp_slot_hold" in ctrl.blocks

    ctrl._dynamic_pricing_schedule = _schedule({b: SLOT_PURPOSE_DEFICIT})
    manager.apply_dynamic_slot_hold()
    assert "dp_slot_hold" not in ctrl.blocks


def test_a_negative_price_slot_keeps_its_own_behaviour(clock):
    battery = _Battery(2.0)
    (a,) = _slots(_at(9, 30), [-0.05])
    ctrl, manager = _setup({a: SLOT_PURPOSE_NEGATIVE_PRICE}, battery, 0.0)
    ctrl._dp_slot_hold_until = None
    _run(manager)
    assert getattr(ctrl, "_dp_slot_hold_until", None) is None
    manager.apply_dynamic_slot_hold()
    assert "dp_slot_hold" not in ctrl.blocks


def test_the_hold_is_an_economic_block_that_safety_paths_may_ignore():
    ctrl = object.__new__(ChargeDischargeController)
    ctrl._global_discharge_blockers = {"dp_slot_hold": {}}
    ctrl._battery_discharge_blockers = {}
    assert ChargeDischargeController.is_discharge_blocked(ctrl) is True
    assert ChargeDischargeController.is_discharge_blocked(ctrl, ignore_economic=True) is False


def test_the_projection_holds_the_battery_in_booked_slots(clock):
    battery = _Battery(2.0)
    a, b, c = _slots(_at(9, 30), [0.20, 0.19, 0.30])
    ctrl, manager = _setup({a: SLOT_PURPOSE_DEFICIT, b: SLOT_PURPOSE_DEFICIT}, battery, 0.4)
    schedule = ctrl._dynamic_pricing_schedule
    schedule.slot_energy_targets_kwh = {b: 0.12}
    _run(manager)
    battery.soc = 4.0
    clock.value = _at(9, 32)
    _run(manager)

    hold = manager.dynamic_slot_hold_fn()
    assert hold(_at(9, 40), 0.2, 6.0) == 0.2  # the slot held now
    assert hold(_at(9, 50), 0.3, 6.0) == 0.3  # a booked slot still to charge
    assert hold(c.start, 0.3, 6.0) is None  # not booked
    assert hold(_at(9, 30) - timedelta(minutes=15), 0.3, 6.0) is None
    assert isinstance(_at(9, 30), datetime)
