"""Runtime tests for the price-aware discharge reserve.

No Home Assistant and no Modbus: the manager talks to a stand-in controller
(SimpleNamespace) exposing only the attributes its guards read.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
)
from custom_components.omnibattery.control.discharge_reserve import (
    GUARD_CAPACITY_PROTECTION,
    GUARD_FLEET_UNKNOWN,
    GUARD_CURTAILMENT,
    GUARD_MANUAL,
    GUARD_NEW_DAY,
    GUARD_NOT_ENABLED,
    GUARD_NO_PLAN,
    DischargeReserveManager,
)
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.discharge_reserve import STATUS_DISABLED

DAY = datetime(2026, 9, 4)
NOW = DAY + timedelta(hours=13)


def _slot(hour: float, price: float) -> PriceSlot:
    start = DAY + timedelta(hours=hour)
    return PriceSlot(start, start + timedelta(hours=1), price)


class _HashableNamespace(SimpleNamespace):
    """Test double that can be used as a coordinator-keyed dict key."""

    __hash__ = object.__hash__
    __eq__ = object.__eq__


def _coordinator(
    soc: float = 80.0,
    min_soc: float = 10.0,
    capacity: float = 10.0,
    available: bool = True,
    manual: bool = False,
    name: str = "battery-1",
):
    return _HashableNamespace(
        name=name,
        data={"battery_soc": soc, "battery_total_energy": capacity},
        is_available=available,
        battery_manual_mode_enabled=manual,
        min_soc=min_soc,
        max_soc=100.0,
    )


def _forecast(evening_kwh: float = 1.0):
    """A learned profile with all its energy in the 18:00-20:00 block."""
    intervals = [0.0] * 96
    for index in range(72, 80):  # 18:00 - 20:00
        intervals[index] = evening_kwh / 8.0
    return SimpleNamespace(
        intervals_kwh=intervals,
        intervals_by_date={DAY.date(): intervals},
        energy_kwh=evening_kwh,
        source="profile",
    )


def _pricing(**overrides):
    base = SimpleNamespace(
        get_future_price_slots=lambda horizon_end=None: [
            _slot(14, 0.20),
            _slot(18, 0.45),
            _slot(19, 0.45),
        ],
        _profile_remaining_consumption=lambda start, end: _forecast(4.0),
        _curtailment_forecast_model=lambda now: (0.0, None, None),
        _get_current_price=lambda: 0.20,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _controller(**overrides):
    base = SimpleNamespace(
        discharge_reserve_enabled=True,
        discharge_reserve_min_saving=0.05,
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        manual_mode_enabled=False,
        _manual_slot_owned=None,
        coordinators=[_coordinator()],
        _is_manual_slot_owned=lambda coordinator: False,
        _curtailment_runtime_status=None,
        _capacity_protection_active=False,
        _pricing_mgr=_pricing(),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _manager(controller=None, now: datetime = NOW) -> DischargeReserveManager:
    manager = DischargeReserveManager(SimpleNamespace(), controller or _controller())
    manager._now = lambda: now
    return manager


def _built(controller=None, now: datetime = NOW) -> DischargeReserveManager:
    manager = _manager(controller, now)
    asyncio.get_event_loop().run_until_complete(manager.async_rebuild_plan("test"))
    return manager


# ----------------------------------------------------------------------
# Scope
# ----------------------------------------------------------------------


def test_disabled_feature_reserves_nothing():
    manager = _manager(_controller(discharge_reserve_enabled=False))
    assert manager.reserve_soc_pct() == 0.0
    assert manager.get_status()["reason"] == GUARD_NOT_ENABLED
    assert manager.get_status()["state"] == STATUS_DISABLED


def test_realtime_price_mode_is_out_of_scope():
    """Real-time pricing has no forward curve, so it cannot see a dearer hour."""
    manager = _manager(
        _controller(predictive_charging_mode=PREDICTIVE_MODE_REALTIME_PRICE)
    )
    assert manager.reserve_soc_pct() == 0.0
    assert manager.get_status()["reason"] == GUARD_NOT_ENABLED


def test_paused_predictive_charging_is_out_of_scope():
    manager = _manager(_controller(predictive_charging_overridden=True))
    assert manager.reserve_soc_pct() == 0.0


def test_without_a_plan_no_floor_is_raised():
    manager = _manager()
    assert manager.reserve_soc_pct() == 0.0
    assert manager.get_status()["reason"] == GUARD_NO_PLAN


# ----------------------------------------------------------------------
# The reserve
# ----------------------------------------------------------------------


def test_evening_peak_raises_the_floor():
    """4 kWh of evening demand against 10 kWh of capacity is 40 SOC points."""
    manager = _built()
    assert manager.reserve_soc_pct() == pytest.approx(40.0)
    status = manager.get_status()
    assert status["state"] == "reserving"
    assert status["reserve_kwh"] == pytest.approx(4.0)
    assert len(status["reserved_slots"]) == 2


def test_expected_pv_removes_the_claim():
    """A forecast that covers the evening leaves nothing to reserve."""
    pricing = _pricing(_curtailment_forecast_model=lambda now: (20.0, None, None))
    manager = _built(_controller(_pricing_mgr=pricing))
    assert manager.reserve_soc_pct() == 0.0


def test_a_higher_min_saving_can_release_the_floor():
    """The slider is read live, not frozen into the plan."""
    controller = _controller()
    manager = _built(controller)
    assert manager.reserve_soc_pct() > 0

    controller.discharge_reserve_min_saving = 0.50
    assert manager.reserve_soc_pct() == 0.0


def test_reserve_shrinks_with_the_battery():
    """A battery that has already discharged cannot reserve what it lost."""
    coordinator = _coordinator()
    controller = _controller(coordinators=[coordinator])
    manager = _built(controller)
    assert manager.reserve_soc_pct() == pytest.approx(40.0)

    coordinator.data = {"battery_soc": 25.0, "battery_total_energy": 10.0}
    assert manager.reserve_soc_pct() == pytest.approx(15.0)


def test_unreadable_battery_state_releases_instead_of_reserving():
    """Not knowing what the battery holds is not a reason to hold it."""
    coordinator = _coordinator()
    controller = _controller(coordinators=[coordinator])
    manager = _built(controller)
    assert manager.reserve_soc_pct() > 0

    coordinator.is_available = False
    assert manager.reserve_soc_pct() == 0.0
    assert manager.get_status()["reason"] == GUARD_FLEET_UNKNOWN


def test_a_battery_that_dropped_out_no_longer_carries_its_share():
    """Capacity is read live, so the remaining battery is not over-reserved."""
    first = _coordinator(name="battery-1")
    second = _coordinator(name="battery-2")
    controller = _controller(coordinators=[first, second])
    manager = _built(controller)
    # 4 kWh over 20 kWh of fleet capacity.
    assert manager.reserve_soc_pct() == pytest.approx(20.0)

    second.is_available = False
    assert manager.reserve_soc_pct() == pytest.approx(40.0)


def test_the_reserve_never_reads_its_own_blocker():
    """The fleet read must not depend on the registry this feature writes."""
    controller = _controller()
    manager = _built(controller)

    def _boom(coordinator=None):
        raise AssertionError("discharge blockers must not be consulted")

    controller.get_discharge_blockers = _boom
    controller._pricing_mgr._curtailment_battery_snapshots = _boom
    assert manager.reserve_soc_pct() == pytest.approx(40.0)


def test_no_profile_means_no_reserve():
    pricing = _pricing(_profile_remaining_consumption=lambda start, end: None)
    manager = _built(_controller(_pricing_mgr=pricing))
    assert manager.reserve_soc_pct() == 0.0


# ----------------------------------------------------------------------
# Guards
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"manual_mode_enabled": True}, GUARD_MANUAL),
        ({"_manual_slot_owned": "battery-1"}, GUARD_MANUAL),
        ({"_curtailment_runtime_status": "predischarging"}, GUARD_CURTAILMENT),
        ({"_curtailment_runtime_status": "protected_window"}, GUARD_CURTAILMENT),
        ({"_capacity_protection_active": True}, GUARD_CAPACITY_PROTECTION),
    ],
)
def test_guards_release_the_floor(overrides, expected):
    controller = _controller()
    manager = _built(controller)
    assert manager.reserve_soc_pct() > 0

    for key, value in overrides.items():
        setattr(controller, key, value)
    assert manager.reserve_soc_pct() == 0.0
    assert manager.get_status()["reason"] == expected


def test_a_battery_in_manual_mode_releases_the_floor():
    controller = _controller(coordinators=[_coordinator(manual=True)])
    manager = _built(controller)
    assert manager.reserve_soc_pct() == 0.0
    assert manager.get_status()["reason"] == GUARD_MANUAL


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


def test_a_plan_from_yesterday_is_dropped():
    manager = _built()
    manager._now = lambda: NOW + timedelta(days=1)
    assert manager.reserve_soc_pct() == 0.0
    assert manager.get_status()["reason"] == GUARD_NEW_DAY
    assert manager.plan is None


def test_clear_drops_the_plan():
    manager = _built()
    manager.clear("test")
    assert manager.plan is None
    assert manager.reserve_soc_pct() == 0.0


def test_rebuild_is_throttled_until_marked_stale():
    calls = []

    def _slots(horizon_end=None):
        calls.append(horizon_end)
        return [_slot(18, 0.45)]

    controller = _controller(_pricing_mgr=_pricing(get_future_price_slots=_slots))
    manager = _built(controller)
    assert len(calls) == 1

    asyncio.get_event_loop().run_until_complete(manager.async_rebuild_plan("again"))
    assert len(calls) == 1

    manager.mark_stale("test")
    asyncio.get_event_loop().run_until_complete(manager.async_rebuild_plan("again"))
    assert len(calls) == 2


def test_disabling_the_feature_clears_a_live_plan():
    controller = _controller()
    manager = _built(controller)
    assert manager.plan is not None

    controller.discharge_reserve_enabled = False
    asyncio.get_event_loop().run_until_complete(manager.async_rebuild_plan("off"))
    assert manager.plan is None


def test_a_failing_planner_releases_instead_of_raising():
    def _boom(horizon_end=None):
        raise RuntimeError("no prices")

    controller = _controller(
        _pricing_mgr=_pricing(get_future_price_slots=_boom)
    )
    manager = _manager(controller)
    with pytest.raises(RuntimeError):
        asyncio.get_event_loop().run_until_complete(manager.async_rebuild_plan("boom"))
    # The engine hook is what converts that into a release; the manager itself
    # must at least never leave a stale plan behind.
    assert manager.plan is None


def test_horizon_stops_at_midnight():
    """Reserving past midnight would ignore tomorrow's sun."""
    manager = _manager()
    assert manager._horizon_end(NOW) == DAY + timedelta(days=1)


# ----------------------------------------------------------------------
# The seam: how the reserve reaches the battery
# ----------------------------------------------------------------------


def _blocks(reserve_pct: float, coordinators=None, effective_floor=None):
    """Run the controller's reserve blocker pass with a stand-in ``self``."""
    from custom_components.omnibattery import ChargeDischargeController

    coordinators = coordinators or [_coordinator()]
    registry: dict = {}

    def _floor(coordinator):
        if effective_floor is not None:
            return effective_floor, "slot_soc_override"
        return coordinator.min_soc, "min_soc"

    fake_self = SimpleNamespace(
        coordinators=coordinators,
        _effective_discharge_min_soc=_floor,
        _price_discharge_reserve_pct=lambda: reserve_pct,
        set_discharge_block=lambda source, reason, details=None, coordinator=None: registry.__setitem__(
            (coordinator.name, source), details
        ),
        remove_discharge_block=lambda source, coordinator=None: registry.pop(
            (coordinator.name, source), None
        ),
    )
    ChargeDischargeController._refresh_price_reserve_blocks(fake_self)
    return registry


def test_a_battery_below_the_reserve_is_blocked_from_discharging():
    """min_soc 10 + reserve 40 = 50; a battery at 45 has nothing spare."""
    registry = _blocks(40.0, [_coordinator(soc=45.0)])
    assert ("battery-1", "price_reserve") in registry
    assert registry[("battery-1", "price_reserve")]["reserved_floor"] == 50.0


def test_a_battery_above_the_reserve_keeps_discharging():
    registry = _blocks(40.0, [_coordinator(soc=70.0)])
    assert registry == {}


def test_no_reserve_blocks_nothing():
    registry = _blocks(0.0, [_coordinator(soc=15.0)])
    assert registry == {}


def test_a_manually_owned_battery_is_left_alone():
    registry = _blocks(40.0, [_coordinator(soc=15.0, manual=True)])
    assert registry == {}


def test_a_coupled_pack_battery_is_judged_by_its_fullest_pack():
    """Mirrors the min_soc blocker: the last pack with charge decides."""
    coordinator = _coordinator(soc=45.0)
    coordinator.data = dict(
        coordinator.data, battery_soc_pack_1=80.0, battery_soc_pack_2=10.0
    )
    assert _blocks(40.0, [coordinator]) == {}


def test_an_unreadable_soc_blocks_nothing():
    coordinator = _coordinator()
    coordinator.data = {"battery_soc": None, "battery_total_energy": 10.0}
    assert _blocks(40.0, [coordinator]) == {}


def test_an_explicit_slot_floor_is_what_the_reserve_is_added_to():
    """A discharge slot reaching below min_soc keeps its window."""
    registry = _blocks(5.0, [_coordinator(soc=25.0, min_soc=30.0)], effective_floor=15.0)
    assert registry == {}


def test_the_reserve_is_an_economic_blocker():
    """Safety and the pre-discharge planner must be able to look past it."""
    from custom_components.omnibattery import ChargeDischargeController

    coordinator = _coordinator()
    fake_self = SimpleNamespace(
        _global_discharge_blockers={},
        _battery_discharge_blockers={coordinator: {"price_reserve": {}}},
        _capacity_protection_overrides_curtailment=lambda: False,
    )
    blocked = ChargeDischargeController.is_discharge_blocked
    assert blocked(fake_self, coordinator) is True
    assert blocked(fake_self, coordinator, ignore_economic=True) is False


def test_engine_hook_releases_when_the_rebuild_fails():
    """A rebuild that raises must leave no reserve behind."""
    from custom_components.omnibattery.pricing.engine import PricingManager

    controller = _controller()
    manager = _built(controller)
    assert manager.plan is not None

    async def _boom(reason, *, force=False):
        raise RuntimeError("price sensor gone")

    manager.async_rebuild_plan = _boom
    controller._discharge_reserve_mgr = manager
    engine = SimpleNamespace(_controller=controller)
    asyncio.get_event_loop().run_until_complete(
        PricingManager._refresh_discharge_reserve_plan(engine, "test")
    )
    assert manager.plan is None
    assert manager.get_status()["reason"] == "rebuild_error"


def test_pv_that_refills_before_the_peak_is_not_reserved_now():
    """Morning hold would import now and export the sun that replaces it."""
    from custom_components.omnibattery.pricing.discharge_reserve import (
        REASON_PV_COVERS_IT,
        plan_discharge_reserve,
    )

    midday = _slot(12, 0.15)
    evening = _slot(19, 0.45)
    plan = plan_discharge_reserve(
        [midday, evening],
        {midday: 0.0, evening: 3.0},
        surplus_by_slot={midday: 4.0, evening: 0.0},
        free_space_kwh=5.0,
        usable_energy_kwh=6.0,
        total_capacity_kwh=10.0,
        current_price=0.15,
        min_saving=0.05,
        now=DAY + timedelta(hours=10),
    )
    assert plan.reserve_kwh == 0.0
    assert plan.reason == REASON_PV_COVERS_IT


def test_pv_is_only_credited_up_to_the_room_left_in_the_battery():
    from custom_components.omnibattery.pricing.discharge_reserve import (
        plan_discharge_reserve,
    )

    midday = _slot(12, 0.15)
    evening = _slot(19, 0.45)
    plan = plan_discharge_reserve(
        [midday, evening],
        {midday: 0.0, evening: 3.0},
        surplus_by_slot={midday: 4.0, evening: 0.0},
        free_space_kwh=1.0,
        usable_energy_kwh=6.0,
        total_capacity_kwh=10.0,
        current_price=0.15,
        min_saving=0.05,
        now=DAY + timedelta(hours=10),
    )
    assert plan.reserve_kwh == pytest.approx(2.0)


def test_a_battery_serving_the_backup_port_does_not_size_the_reserve():
    """Its energy never reaches the house, so it must not raise the reserve."""
    healthy = _coordinator(name="battery-1")
    backup = _coordinator(name="battery-2")
    controller = _controller(coordinators=[healthy, backup])
    controller._is_backup_function_active = lambda coordinator: coordinator is backup
    manager = _built(controller)
    # Only the healthy 10 kWh battery backs the 4 kWh claim.
    assert manager.reserve_soc_pct() == pytest.approx(40.0)


def test_a_non_responsive_battery_does_not_size_the_reserve():
    healthy = _coordinator(name="battery-1")
    dead = _coordinator(name="battery-2")
    controller = _controller(coordinators=[healthy, dead])
    controller._non_responsive = SimpleNamespace(
        is_excluded=lambda coordinator: coordinator is dead
    )
    manager = _built(controller)
    assert manager.reserve_soc_pct() == pytest.approx(40.0)


def test_an_unreadable_fleet_defers_the_rebuild_instead_of_caching_an_empty_plan():
    """A transient outage must not disable the reserve for the whole interval."""
    coordinator = _coordinator()
    controller = _controller(coordinators=[coordinator])
    manager = _manager(controller)
    coordinator.is_available = False
    asyncio.get_event_loop().run_until_complete(manager.async_rebuild_plan("outage"))
    assert manager.plan is None

    coordinator.is_available = True
    asyncio.get_event_loop().run_until_complete(manager.async_rebuild_plan("recovered"))
    assert manager.plan is not None
    assert manager.reserve_soc_pct() == pytest.approx(40.0)
