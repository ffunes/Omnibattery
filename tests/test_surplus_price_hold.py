"""Runtime tests for the price-aware surplus absorption hold.

No Home Assistant and no Modbus: the manager talks to a stand-in controller
(SimpleNamespace) that exposes only the attributes the guards read, plus a
minimal charge-blocker registry.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from time import monotonic
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING
from custom_components.omnibattery.control.surplus_price_hold import (
    BLOCKER_SOURCE,
    GUARD_CAPACITY_PROTECTION,
    GUARD_CHARGE_DELAY,
    GUARD_CURTAILMENT,
    GUARD_DYNAMIC_PRICING_SLOT,
    GUARD_EV_PAUSE,
    GUARD_MANUAL,
    GUARD_NEGATIVE_PRICE,
    GUARD_NOT_ENABLED,
    GUARD_NO_PLAN,
    GUARD_SOC_FLOOR,
    GUARD_TARGET_UNAVAILABLE,
    GUARD_WEEKLY_FULL_CHARGE,
    SurplusPriceHoldManager,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.curtailment import BatterySnapshot
from custom_components.omnibattery.pricing.surplus_absorption import (
    REASON_CHEAPER_WINDOW_AHEAD,
    plan_surplus_absorption,
)


class _HashableNamespace(SimpleNamespace):
    """Test double that can participate in coordinator-keyed dicts."""

    __hash__ = object.__hash__
    __eq__ = object.__eq__


DAY = datetime(2026, 8, 2)
NOW = DAY + timedelta(hours=9, minutes=30)
DEADLINE = DAY + timedelta(hours=19)


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------


def _slot(hour: float, price: float) -> PriceSlot:
    start = DAY + timedelta(hours=hour)
    return PriceSlot(start, start + timedelta(hours=1), price)


def _snapshot(soc: float = 50.0, floor: float = 10.0) -> BatterySnapshot:
    return BatterySnapshot("battery-1", soc, 10.0, 100.0, floor, 2500.0, True)


def _pricing(**overrides):
    """A pricing-manager stand-in exposing only what the guards touch."""
    base = SimpleNamespace(
        is_in_dynamic_pricing_slot=lambda: False,
        _negative_price_feature_enabled=lambda: False,
        _opportunistic_target_pending=lambda: False,
        _current_price_is_opportunistic=lambda: False,
        _curtailment_battery_snapshots=lambda: [_snapshot()],
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _controller(**overrides):
    blockers: dict[str, dict] = {}

    controller = SimpleNamespace(
        surplus_price_hold_enabled=True,
        surplus_hold_min_saving=0.0,
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        charge_delay_enabled=False,
        _charge_delay_mgr=None,
        _current_price_slot_active=False,
        _curtailment_runtime_status=None,
        _curtailment_opportunistic_charge_limit_w=0.0,
        _weekly_charge_mgr=None,
        _force_full_charge=False,
        _capacity_protection_active=False,
        _manual_slot_owned=None,
        manual_mode_enabled=False,
        coordinators=[],
        _predictive_safety_margin_kwh=0.0,
        max_charge_capacity=2500.0,
        _solar_t_start=6.0,
        _consumption_tracker=SimpleNamespace(
            estimate_t_end=lambda: 19.0,
            calculate_sunrise=lambda: 6.0,
            calculate_solar_noon=lambda: 13.0,
        ),
    )
    controller._blockers = blockers
    controller._battery_blockers: dict = {}
    controller.get_charge_blockers = (
        lambda coordinator=None: dict(blockers)
        if coordinator is None
        else dict(controller._battery_blockers)
    )
    controller.set_charge_block = (
        lambda source, reason, details=None, coordinator=None: blockers.__setitem__(
            source, {"reason": reason, "details": details}
        )
    )
    controller.remove_charge_block = (
        lambda source, coordinator=None: blockers.pop(source, None)
    )
    controller._pricing_mgr = _pricing()
    for key, value in overrides.items():
        setattr(controller, key, value)
    return controller


def _manager(controller=None, *, now: datetime = NOW) -> SurplusPriceHoldManager:
    manager = SurplusPriceHoldManager(None, controller or _controller())
    # Pin the clock: the plans below are built on a fixed day.
    manager._now = lambda: now
    return manager


def _holding_plan(min_saving: float = 0.0):
    """A plan that holds at NOW: the cheap window is still ahead."""
    slots = [_slot(9, 0.28), _slot(12, 0.12)]
    plan = plan_surplus_absorption(
        slots,
        {slot: 2.0 for slot in slots},
        [_snapshot()],
        remaining_consumption_kwh=1.0,
        usable_energy_kwh=0.0,
        max_charge_power_w=2500.0,
        deadline=DEADLINE,
        min_saving=min_saving,
        now=NOW,
    )
    assert plan.status == "planned"
    return plan


def _prime(manager: SurplusPriceHoldManager, plan=None) -> None:
    """Install a cached plan without going through the async rebuild."""
    manager._plan = plan if plan is not None else _holding_plan()
    manager._plan_date = NOW.date()
    manager._live_target_kwh = manager._plan.target_kwh


# ----------------------------------------------------------------------
# Scope gate
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"surplus_price_hold_enabled": False},
        {"predictive_charging_enabled": False},
        {"predictive_charging_mode": "realtime_price"},
        {"predictive_charging_mode": "time_slot"},
    ],
)
def test_feature_is_scoped_to_enabled_dynamic_pricing(overrides):
    manager = _manager(_controller(**overrides))
    _prime(manager)

    assert manager.feature_enabled() is False
    assert manager.is_hold_active() is False
    assert manager.get_status()["reason"] == GUARD_NOT_ENABLED


def test_hold_applies_when_a_cheaper_window_is_ahead():
    manager = _manager()
    _prime(manager)

    assert manager.is_hold_active() is True
    assert manager.get_status()["reason"] == REASON_CHEAPER_WINDOW_AHEAD


def test_no_plan_means_no_hold():
    manager = _manager()

    assert manager.is_hold_active() is False
    assert manager.get_status()["reason"] == GUARD_NO_PLAN


def test_a_plan_from_yesterday_is_discarded():
    manager = _manager()
    _prime(manager)
    manager._plan_date = (NOW - timedelta(days=1)).date()

    assert manager.is_hold_active() is False
    assert manager.plan is None


# ----------------------------------------------------------------------
# Guards
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, expected",
    [
        (
            {"_pricing_mgr": _pricing(is_in_dynamic_pricing_slot=lambda: True)},
            GUARD_DYNAMIC_PRICING_SLOT,
        ),
        ({"_current_price_slot_active": True}, GUARD_DYNAMIC_PRICING_SLOT),
        (
            {
                "_pricing_mgr": _pricing(
                    _negative_price_feature_enabled=lambda: True,
                    _opportunistic_target_pending=lambda: True,
                    _current_price_is_opportunistic=lambda: True,
                )
            },
            GUARD_NEGATIVE_PRICE,
        ),
        ({"_curtailment_runtime_status": "protected_window"}, GUARD_CURTAILMENT),
        ({"_curtailment_runtime_status": "predischarging"}, GUARD_CURTAILMENT),
        ({"_curtailment_opportunistic_charge_limit_w": 800.0}, GUARD_CURTAILMENT),
        (
            {"_weekly_charge_mgr": SimpleNamespace(is_active=lambda: True)},
            GUARD_WEEKLY_FULL_CHARGE,
        ),
        ({"_force_full_charge": True}, GUARD_WEEKLY_FULL_CHARGE),
        ({"_capacity_protection_active": True}, GUARD_CAPACITY_PROTECTION),
        ({"manual_mode_enabled": True}, GUARD_MANUAL),
        ({"_manual_slot_owned": ["battery-1"]}, GUARD_MANUAL),
        (
            {
                "_pricing_mgr": _pricing(
                    _curtailment_battery_snapshots=lambda: [_snapshot(soc=10.0)]
                )
            },
            GUARD_SOC_FLOOR,
        ),
    ],
)
def test_each_guard_releases_the_hold(overrides, expected):
    manager = _manager(_controller(**overrides))
    _prime(manager)

    assert manager.is_hold_active() is False
    assert manager.get_status()["reason"] == expected


def test_the_global_charge_delay_blocker_releases_the_hold():
    controller = _controller(charge_delay_enabled=True)
    controller._blockers["charge_delay"] = {"reason": "charge_delay"}
    manager = _manager(controller)
    _prime(manager)

    assert manager.is_hold_active() is False
    assert manager.get_status()["reason"] == GUARD_CHARGE_DELAY


def test_the_per_battery_charge_delay_setpoint_blocker_releases_the_hold():
    """The setpoint phase is charge delay deliberately letting the battery charge."""
    controller = _controller(
        charge_delay_enabled=True,
        coordinators=[SimpleNamespace(battery_manual_mode_enabled=False)],
    )
    controller._battery_blockers["charge_delay_setpoint"] = {"reason": "charge_delay"}
    manager = _manager(controller)
    _prime(manager)

    assert manager.is_hold_active() is False
    assert manager.get_status()["reason"] == GUARD_CHARGE_DELAY


def test_charge_delay_blockers_are_ignored_while_the_feature_is_off():
    controller = _controller(charge_delay_enabled=False)
    controller._blockers["charge_delay"] = {"reason": "charge_delay"}
    manager = _manager(controller)
    _prime(manager)

    assert manager.is_hold_active() is True


def test_ev_pause_releases_without_adding_a_second_blocker():
    controller = _controller()
    controller._blockers["ev_pause"] = {"reason": "ev_pause"}
    manager = _manager(controller)
    _prime(manager)

    assert manager.is_hold_active() is False
    assert manager.get_status()["reason"] == GUARD_EV_PAUSE


def test_a_manual_owned_battery_releases_the_hold():
    controller = _controller(
        coordinators=[SimpleNamespace(battery_manual_mode_enabled=True)]
    )
    manager = _manager(controller)
    _prime(manager)

    assert manager.is_hold_active() is False
    assert manager.get_status()["reason"] == GUARD_MANUAL


def test_unreadable_battery_snapshots_release_the_hold():
    def _raise():
        raise RuntimeError("coordinator gone")

    controller = _controller(
        _pricing_mgr=_pricing(_curtailment_battery_snapshots=_raise)
    )
    manager = _manager(controller)
    _prime(manager)

    assert manager.is_hold_active() is False


# ----------------------------------------------------------------------
# Blocker registration
# ----------------------------------------------------------------------


def test_blocker_is_registered_and_removed_with_the_hold():
    from custom_components.omnibattery import ChargeDischargeController

    controller = _controller()
    manager = _manager(controller)
    controller._surplus_hold_mgr = manager
    _prime(manager)

    ChargeDischargeController._refresh_surplus_price_hold_block(controller)
    assert BLOCKER_SOURCE in controller.get_charge_blockers()

    controller._capacity_protection_active = True
    ChargeDischargeController._refresh_surplus_price_hold_block(controller)
    assert BLOCKER_SOURCE not in controller.get_charge_blockers()


def test_disabled_feature_registers_nothing():
    from custom_components.omnibattery import ChargeDischargeController

    controller = _controller(surplus_price_hold_enabled=False)
    manager = _manager(controller)
    controller._surplus_hold_mgr = manager

    ChargeDischargeController._refresh_surplus_price_hold_block(controller)

    assert controller.get_charge_blockers() == {}
    assert manager.plan is None


def test_clear_drops_the_plan_and_the_blocker():
    controller = _controller()
    manager = _manager(controller)
    _prime(manager)
    controller.set_charge_block(BLOCKER_SOURCE, BLOCKER_SOURCE)

    manager.clear("feature_disabled")

    assert manager.plan is None
    assert controller.get_charge_blockers() == {}
    assert manager.get_status()["reason"] == "feature_disabled"


# ----------------------------------------------------------------------
# Rebuild scheduling
# ----------------------------------------------------------------------


async def test_rebuild_is_skipped_while_the_feature_is_off():
    controller = _controller(surplus_price_hold_enabled=False)
    manager = _manager(controller)

    await manager.async_rebuild_plan("test")

    assert manager.plan is None


def test_mark_stale_forces_the_next_rebuild():
    manager = _manager()
    _prime(manager)
    manager._last_rebuild_mono = 10.0 ** 9

    assert manager._rebuild_due(NOW) is False
    manager.mark_stale("reevaluated")
    assert manager._rebuild_due(NOW) is True


def test_status_reports_the_next_release_window():
    manager = _manager()
    _prime(manager)

    manager.is_hold_active()
    status = manager.get_status()

    assert status["next_release_at"] == (DAY + timedelta(hours=12)).isoformat()
    assert status["target_kwh"] == pytest.approx(1.0)


# ----------------------------------------------------------------------
# Effect on the PD charge pool
# ----------------------------------------------------------------------


def _pool_controller():
    """The small controller surface ``_get_available_batteries`` reads."""
    from custom_components.omnibattery import ChargeDischargeController

    battery = _HashableNamespace(
        name="battery-1",
        battery_manual_mode_enabled=False,
        data={"battery_soc": 50},
        is_available=True,
        _consecutive_failures=0,
        rs485_user_disabled=False,
        enable_charge_hysteresis=False,
        max_soc=100,
        min_soc=10,
        _discharge_min_soc_latched=False,
    )
    controller = ChargeDischargeController.__new__(ChargeDischargeController)
    controller.coordinators = [battery]
    controller._non_responsive = SimpleNamespace(is_excluded=lambda _c: False)
    controller._is_backup_function_active = lambda _c: False
    controller._is_manual_slot_owned = lambda _c: False
    controller.is_discharge_blocked = lambda _c, **_kw: False
    controller.get_discharge_blockers = lambda _c: {}
    controller._weekly_full_charge_unlocked = lambda: False
    controller._weekly_charge_mgr = SimpleNamespace(
        is_battery_full=lambda _coordinator: False,
    )
    controller._effective_charge_max_soc = lambda _c, _weekly: (100, "min_soc")
    controller._normal_balance_recal_override = {}
    return controller, battery


def test_the_hold_empties_the_charge_pool_so_surplus_exports():
    """No battery available to charge means PD clamps to 0 W and PV exports."""
    controller, battery = _pool_controller()

    controller.get_charge_blockers = lambda _c: {}
    assert controller._get_available_batteries(True) == [battery]

    controller.get_charge_blockers = lambda _c: {BLOCKER_SOURCE: "surplus_price_hold"}
    assert controller._get_available_batteries(True) == []


def test_the_hold_leaves_discharge_untouched():
    """Self-consumption from the battery must continue while surplus exports."""
    controller, battery = _pool_controller()
    controller.get_charge_blockers = lambda _c: {BLOCKER_SOURCE: "surplus_price_hold"}

    assert controller._get_available_batteries(False) == [battery]


# ----------------------------------------------------------------------
# Review regressions
# ----------------------------------------------------------------------


def test_a_paused_predictive_charging_switch_disables_the_feature():
    """The runtime override pauses the whole planner, this feature included."""
    manager = _manager(_controller(predictive_charging_overridden=True))
    _prime(manager)

    assert manager.feature_enabled() is False
    assert manager.is_hold_active() is False


def test_an_unusable_live_target_releases_rather_than_holding():
    manager = _manager()
    _prime(manager)
    manager._live_target_failed = True

    assert manager.is_hold_active() is False
    assert manager.get_status()["reason"] == GUARD_TARGET_UNAVAILABLE


def test_the_minimum_saving_slider_takes_effect_without_a_rebuild():
    controller = _controller()
    manager = _manager(controller)
    _prime(manager, _holding_plan(min_saving=0.0))
    assert manager.is_hold_active() is True

    # 0.28 -> 0.12 is a 0.16 saving, so a 0.20 requirement must release.
    controller.surplus_hold_min_saving = 0.20

    assert manager.is_hold_active() is False


def test_the_live_target_refresh_reads_only_battery_snapshots():
    """The per-cycle path must never run the remaining-horizon evaluation."""
    def _forbidden(**_kwargs):
        raise AssertionError("remaining-horizon evaluation ran on a control cycle")

    controller = _controller(
        _pricing_mgr=_pricing(_evaluate_remaining_grid_charging=_forbidden)
    )
    manager = _manager(controller)
    _prime(manager)
    manager._planned_remaining_consumption_kwh = 4.0
    manager._last_rebuild_mono = monotonic()

    asyncio.run(manager.async_rebuild_plan("control_cycle"))

    # Stored 4.0 kWh above the floor covers the 4.0 kWh still expected.
    assert manager._live_target_kwh == pytest.approx(0.0)


def test_a_date_rollover_does_not_touch_the_blocker_registry():
    controller = _controller()
    manager = _manager(controller)
    _prime(manager)
    controller.set_charge_block("other_feature", "other_feature")
    manager._plan_date = (NOW - timedelta(days=1)).date()

    manager.is_hold_active()

    assert list(controller.get_charge_blockers()) == ["other_feature"]


def test_the_daylight_share_scales_the_load_to_the_solar_window():
    manager = _manager()
    deadline = DAY + timedelta(hours=19)

    # 09:30 to 19:00 is 9.5 of the 14.5 hours left before midnight.
    share = manager._daylight_share(14.5, NOW, deadline)

    assert share == pytest.approx(9.5, abs=0.05)


def test_the_daylight_share_uses_the_consumption_window_model():
    controller = _controller()
    controller._consumption_tracker.consumption_window_hours_in_range = (
        lambda start, end: 2.0 if end <= 19.0 else 8.0
    )
    manager = _manager(controller)

    share = manager._daylight_share(8.0, NOW, DAY + timedelta(hours=19))

    assert share == pytest.approx(2.0)


def test_the_daylight_share_is_zero_after_the_deadline():
    manager = _manager()

    assert manager._daylight_share(10.0, NOW, NOW - timedelta(hours=1)) == 0.0
