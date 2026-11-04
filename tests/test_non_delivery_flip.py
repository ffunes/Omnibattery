"""A commanded direction flip keeps what kind of fault an open spell is.

The flip resets the episode bookkeeping (fail count, exclusion, wake budget) and
keeps the start of the non-delivery spell, so a fault that survives every flip
still reaches the battery_not_delivering Repair. It also keeps the spell's
reason: the full-battery sweep in ``_run_control_cycle`` only ends a
charge-side spell, and a battery that is full is never commanded to charge
again, so nothing else would end it.

These tests run the real ``_set_battery_power`` for the flip and the real
``_run_control_cycle`` for the sweep and the Repair check.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.drivers import SetpointResult
from custom_components.omnibattery.tracking.non_responsive_tracker import (
    NON_DELIVERY_REPAIR_AFTER_S,
    NonResponsiveTracker,
)
from tests.conftest import FakeCoordinator
from tests.test_control_sample_scheduling import _main_controller, _state

_TRACKER = "custom_components.omnibattery.tracking.non_responsive_tracker"


def _controller():
    """Cycle harness with one battery that the real _set_battery_power can drive."""
    reported_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    controller = _main_controller({"state": _state(100, reported_at)}, [])
    coord = FakeCoordinator(
        name="BAT1",
        data={
            "battery_soc": 99,
            "battery_power": 0,
            "force_mode": 0,
            "set_charge_power": 0,
            "set_discharge_power": 0,
        },
        # The inverter has not started moving yet when the readback comes in.
        apply_power=AsyncMock(
            side_effect=lambda net, read_back=False: SetpointResult(
                ok=True, net_power_w=net, confirmed=True, battery_power_w=0,
            )
        ),
    )
    coord._is_shutting_down = False
    controller.coordinators = [coord]
    controller._non_responsive = NonResponsiveTracker(fail_threshold=3)
    controller._is_backup_function_active = lambda c: False
    controller._is_manual_slot_owned = lambda c: False
    controller.get_charge_blockers = lambda c: {}
    controller.get_discharge_blockers = lambda c: {}
    controller._log_low_power_delivery = lambda coordinator, **k: None
    # Idle while it was excluded: the next move command is a flip.
    controller._last_commanded_net_sign = {coord: 0}
    controller._charge_engage_started = {}
    controller._discharge_engage_started = {}
    controller._high_soc_taper_started = {}
    controller._idle_commanded_started = {}
    controller._idle_runaway_handled = {}
    controller._check_non_delivery = ChargeDischargeController._check_non_delivery.__get__(
        controller
    )
    return controller, coord, reported_at


def _expire_cooldown(tracker, coord):
    """Move the exclusion one cooldown into the past and let it expire."""
    info = tracker.batteries[coord]
    info["excluded_at"] -= timedelta(minutes=tracker.cooldown_min)
    info["degraded_since"] = info["excluded_at"]
    assert tracker.is_excluded(coord) is False


def _age_spell_past_repair_threshold(tracker, coord):
    tracker.batteries[coord]["degraded_since"] -= timedelta(
        seconds=NON_DELIVERY_REPAIR_AFTER_S
    )


def test_full_battery_after_a_charge_flip_ends_the_spell_without_a_repair():
    """Excluded for a charge-side non-delivery, retried after the cooldown with
    a new charge command, then full: the sweep ends the spell and no Repair is
    raised, even once the spell is old enough for one."""
    controller, coord, reported_at = _controller()
    tracker = controller._non_responsive
    for _ in range(6):  # wake round + second threshold-cross (exclude)
        tracker.record_non_delivery(coord, 2400, 235, reason="charge_non_delivery")
    _expire_cooldown(tracker, coord)

    asyncio.run(ChargeDischargeController._set_battery_power(controller, coord, 1250, 0))
    assert coord.apply_power.await_count == 1
    assert coord in controller._charge_engage_started
    assert tracker.batteries[coord]["degraded_since"] is not None

    controller._weekly_charge_mgr.is_battery_full = lambda c: True
    _age_spell_past_repair_threshold(tracker, coord)
    with patch(f"{_TRACKER}.ir.async_create_issue") as create_issue:
        asyncio.run(controller._run_control_cycle(now=reported_at))

    create_issue.assert_not_called()
    assert tracker.batteries[coord]["degraded_since"] is None
    assert tracker.last_reason(coord) == ""


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (
            lambda tracker, coord: tracker.record_non_delivery(
                coord, 600, 0, reason="standby_no_delivery",
            ),
            "standby_no_delivery",
        ),
        (
            lambda tracker, coord: tracker.record_comm_failure(
                coord, "modbus_write_failed",
            ),
            "modbus_write_failed",
        ),
    ],
    ids=["discharge", "comms"],
)
def test_full_battery_after_a_flip_keeps_a_discharge_or_comms_fault(record, reason):
    """A full battery is still a discharge candidate: a discharge or comms fault
    survives the flip into discharge and the full-battery sweep, and its Repair
    names the real reason."""
    controller, coord, reported_at = _controller()
    tracker = controller._non_responsive
    for _ in range(6):
        record(tracker, coord)
    assert tracker.batteries[coord]["excluded_at"] is not None
    _expire_cooldown(tracker, coord)

    asyncio.run(ChargeDischargeController._set_battery_power(controller, coord, 0, 600))
    assert coord.apply_power.await_count == 1
    assert coord in controller._discharge_engage_started

    controller._weekly_charge_mgr.is_battery_full = lambda c: True
    _age_spell_past_repair_threshold(tracker, coord)
    with patch(f"{_TRACKER}.ir.async_create_issue") as create_issue:
        asyncio.run(controller._run_control_cycle(now=reported_at))

    create_issue.assert_called_once()
    assert create_issue.call_args.kwargs["translation_placeholders"]["reason"] == reason
    assert tracker.batteries[coord]["degraded_since"] is not None
