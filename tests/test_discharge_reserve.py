"""Runtime tests for the price-aware discharge reserve.

No Home Assistant and no Modbus: the manager talks to a stand-in controller
(SimpleNamespace) exposing only the attributes its guards read.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    CHARGE_EFFICIENCY,
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
from custom_components.omnibattery.solar_forecast import SolarForecastPeriod

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
        energy_horizon_end=lambda _now: DAY + timedelta(days=1, hours=7),
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
        # Well above anything these slots can offer, so only the tests that
        # lower it deliberately see the charge-power cap on the PV credit.
        max_charge_capacity=10_000.0,
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


def test_a_guard_release_publishes_no_figures_from_the_last_cycle():
    """It never ran the calculation, so it has nothing to report but the guard."""
    controller = _controller()
    manager = _built(controller)
    assert manager.reserve_soc_pct() > 0
    assert manager.get_status()["claimed_kwh"] > 0

    controller.manual_mode_enabled = True
    assert manager.reserve_soc_pct() == 0.0
    status = manager.get_status()
    assert status["reason"] == GUARD_MANUAL
    assert "claims" not in status
    assert "claimed_kwh" not in status
    assert "reserve_kwh" not in status


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


def test_horizon_stops_at_sunrise():
    """The small hours carry no sun, so midnight was never the right cut."""
    manager = _manager()
    assert manager._horizon_end(NOW) == DAY + timedelta(days=1, hours=7)


def test_a_pre_dawn_peak_claims_stored_energy():
    """A 06:00 peak is tonight's problem; at midnight it belonged to no plan."""
    pre_dawn = _slot(30, 0.45)  # 06:00 the next morning, before the 07:00 sunrise
    intervals = [0.0] * 96
    for index in range(24, 28):  # 06:00 - 07:00
        intervals[index] = 3.0 / 4.0
    pricing = _pricing(
        # Unlike the shared stub, this one honours the horizon the way
        # _filter_future_slots does: that bound is the thing under test.
        get_future_price_slots=lambda horizon_end=None: [
            slot
            for slot in (_slot(14, 0.20), pre_dawn)
            if horizon_end is None or slot.end <= horizon_end
        ],
        _profile_remaining_consumption=lambda start, end: SimpleNamespace(
            intervals_kwh=intervals,
            intervals_by_date={(DAY + timedelta(days=1)).date(): intervals},
            energy_kwh=3.0,
            source="profile",
        ),
    )
    manager = _built(_controller(_pricing_mgr=pricing))

    assert manager.reserve_soc_pct() == pytest.approx(30.0)  # 3 kWh of 10 kWh
    reserved = manager.get_status()["reserved_slots"]
    assert [slot["start"] for slot in reserved] == [pre_dawn.start.isoformat()]


OCT_27 = datetime(2026, 10, 27)
OCT_27_SUNRISE = OCT_27 + timedelta(hours=7, minutes=6)
OCT_28_SUNRISE = OCT_27 + timedelta(days=1, hours=7, minutes=7)


def _after_midnight_pricing(forecast_kwh: float = 13.8):
    """27 Oct: a dear morning before sunrise, a dearer evening, sun in between.

    ``energy_horizon_end`` and the slot filter behave like the real ones, and the
    solar fraction is zero before sunrise, so the horizon is what decides.
    """

    def quarter(hour: float, price: float) -> PriceSlot:
        start = OCT_27 + timedelta(hours=hour)
        return PriceSlot(start, start + timedelta(minutes=15), price)

    slots = [
        quarter(0.25, 0.354),
        quarter(6.25, 0.3877),
        quarter(6.5, 0.418),
        quarter(6.75, 0.418),
        quarter(12.0, 0.25),
        quarter(12.25, 0.25),
        quarter(18.0, 0.52),
        quarter(18.25, 0.52),
        quarter(18.5, 0.52),
    ]
    intervals = [0.0] * 96
    for index in (25, 26, 27):  # 06:15 - 07:00
        intervals[index] = 0.5
    for index in (72, 73, 74):  # 18:00 - 18:45
        intervals[index] = 1.0
    profile = SimpleNamespace(
        intervals_kwh=intervals,
        intervals_by_date={OCT_27.date(): intervals},
        energy_kwh=sum(intervals),
        source="profile",
    )

    def fraction(hour: float) -> float:
        return max(0.0, min(1.0, (hour - 7.1) / 10.0))

    return _pricing(
        get_future_price_slots=lambda horizon_end=None: [
            slot for slot in slots if horizon_end is None or slot.end <= horizon_end
        ],
        _profile_remaining_consumption=lambda start, end: profile,
        _curtailment_forecast_model=lambda now: (forecast_kwh, fraction, None),
        _solar_forecast_is_remaining=lambda: True,
        _get_current_price=lambda: 0.354,
        energy_horizon_end=lambda now: (
            OCT_28_SUNRISE if now.date() == OCT_27.date() else OCT_27_SUNRISE
        ),
    )


def test_horizon_after_midnight_ends_at_this_morning_s_sunrise():
    """Before sunrise the night being protected is still the current one."""
    manager = _manager(_controller(_pricing_mgr=_after_midnight_pricing()))
    just_past_midnight = OCT_27 + timedelta(minutes=5)
    assert manager._horizon_end(just_past_midnight) == OCT_27_SUNRISE


@pytest.mark.parametrize("minutes_after_sunrise", [0, 1, 300])
def test_horizon_moves_to_tomorrow_once_the_sun_is_up(minutes_after_sunrise):
    manager = _manager(_controller(_pricing_mgr=_after_midnight_pricing()))
    now = OCT_27_SUNRISE + timedelta(minutes=minutes_after_sunrise)
    assert manager._horizon_end(now) == OCT_28_SUNRISE


def test_the_evening_peak_does_not_release_the_reserve_after_midnight():
    """Replay of 27 Oct 00:05: usable energy 1.859 kWh, 13.8 kWh forecast.

    With the horizon on tomorrow's sunrise the dearer evening took every claim,
    the day's sun paid for all of them, and the battery drained into the night
    with nothing held for the morning.
    """
    now = OCT_27 + timedelta(minutes=5)
    controller = _controller(
        _pricing_mgr=_after_midnight_pricing(),
        discharge_reserve_min_saving=0.029,
        # 1.859 kWh above the 10 % floor of the 10 kWh stand-in battery.
        coordinators=[_coordinator(soc=28.59)],
    )
    manager = _built(controller, now)

    # 1.5 kWh of pre-sunrise demand on the 10 kWh stand-in battery.
    assert manager.reserve_soc_pct() == pytest.approx(15.0)
    status = manager.get_status()
    assert status["reason"] == "reserved_for_dearer_hours"
    assert status["pv_credit_kwh"] == 0.0
    assert all(
        datetime.fromisoformat(slot["start"]) < OCT_27_SUNRISE
        for slot in status["reserved_slots"]
    )


NOV_2 = datetime(2026, 11, 2)
NOV_2_SUNRISE = NOV_2 + timedelta(hours=7, minutes=15, seconds=28)
NOV_3_SUNRISE = NOV_2 + timedelta(days=1, hours=7, minutes=17, seconds=1)
SUN_TAKES_OVER = NOV_2 + timedelta(hours=10)
QUARTER = timedelta(minutes=15)
LOAD_PER_QUARTER = 0.25


def _nov_2_pricing(
    forecast_kwh: float = 13.6,
    now: datetime = NOV_2 + timedelta(minutes=5),
    load=None,
):
    """2 Nov: dear from 05:45, still dear long after sunrise, sun from 10:00.

    Weak sun from 07:15 to 10:00 (5 % of the day) that never covers the house,
    then strong sun to 16:00. The slot filter is the real one: a slot that
    starts on the horizon stays in, which is how the sunrise slot got the day's
    whole forecast.
    """

    def price(hour: float) -> float:
        if 5.75 <= hour < 10.0:
            return 0.60
        if 17.0 <= hour < 20.0:
            return 0.55
        return 0.45 if 10.0 <= hour < 17.0 else 0.47

    slots = [
        PriceSlot(NOV_2 + QUARTER * i, NOV_2 + QUARTER * (i + 1), price(i / 4.0))
        for i in range(96)
    ]
    intervals = [LOAD_PER_QUARTER] * 96
    for index, kwh in (load or {}).items():
        intervals[index] = kwh
    profile = SimpleNamespace(
        intervals_kwh=intervals,
        intervals_by_date={NOV_2.date(): intervals},
        energy_kwh=sum(intervals),
        source="profile",
    )

    def fraction(hour: float) -> float:
        if hour <= 7.25:
            return 0.0
        if hour <= 10.0:
            return 0.05 * (hour - 7.25) / 2.75
        return min(1.0, 0.05 + 0.95 * (hour - 10.0) / 6.0)

    return _pricing(
        get_future_price_slots=lambda horizon_end=None: [
            slot
            for slot in slots
            if slot.end > now and (horizon_end is None or slot.start <= horizon_end)
        ],
        _profile_remaining_consumption=lambda start, end: profile,
        _curtailment_forecast_model=lambda _now: (forecast_kwh, fraction, None),
        _solar_forecast_is_remaining=lambda: True,
        _get_current_price=lambda: 0.47,
        energy_horizon_end=lambda when: (
            NOV_3_SUNRISE if when.date() == NOV_2.date() else NOV_2_SUNRISE
        ),
    )


def _nov_2_manager(now: datetime = NOV_2 + timedelta(minutes=5), **pricing_kwargs):
    controller = _controller(
        _pricing_mgr=_nov_2_pricing(now=now, **pricing_kwargs),
        discharge_reserve_min_saving=0.029,
    )
    return _built(controller, now)


def test_the_reserve_holds_on_until_the_sun_covers_the_house():
    """2 Nov: empty at 07:15, then 2.70 kWh imported at 0.586 by 09:10."""
    manager = _nov_2_manager()

    # 1.5 kWh before sunrise plus 11 quarters at 0.25 kWh less the weak sun.
    weak_sun = 13.6 * 0.05 / 11
    expected_kwh = 6 * LOAD_PER_QUARTER + 11 * (LOAD_PER_QUARTER - weak_sun)
    assert manager.reserve_soc_pct() == pytest.approx(10 * expected_kwh, rel=1e-3)
    status = manager.get_status()
    assert status["horizon_end"] == SUN_TAKES_OVER.isoformat()
    assert status["pv_credit_kwh"] == 0.0
    starts = [slot["start"] for slot in status["reserved_slots"]]
    assert (NOV_2 + timedelta(hours=9, minutes=45)).isoformat() in starts
    assert SUN_TAKES_OVER.isoformat() not in starts


def test_the_sunrise_slot_does_not_receive_the_whole_day_s_sun():
    """It lost its claim when the forecast was spread over the night's slots."""
    manager = _nov_2_manager()
    manager.reserve_soc_pct()
    starts = [slot["start"] for slot in manager.get_status()["reserved_slots"]]
    assert (NOV_2 + timedelta(hours=7, minutes=15)).isoformat() in starts
    assert manager.plan.horizon_surplus_kwh == pytest.approx(0.0)


def test_one_quiet_quarter_hour_is_not_the_sun_taking_over():
    """A learned profile dips for 15 minutes; the house is back on the grid after."""
    # 08:30: the weak sun beats this one slot, but not the hour that starts there.
    manager = _nov_2_manager(load={34: 0.01})
    manager.reserve_soc_pct()
    assert manager.get_status()["horizon_end"] == SUN_TAKES_OVER.isoformat()


def test_a_day_the_sun_never_covers_the_house_holds_for_the_evening():
    """Nothing refills the battery, so the evening's hours are still its problem."""
    manager = _nov_2_manager(forecast_kwh=2.0)
    manager.reserve_soc_pct()
    status = manager.get_status()
    assert status["horizon_end"] == NOV_3_SUNRISE.isoformat()
    assert status["pv_credit_kwh"] == 0.0
    starts = [slot["start"] for slot in status["reserved_slots"]]
    assert (NOV_2 + timedelta(hours=18)).isoformat() in starts


def test_no_solar_forecast_keeps_the_horizon_on_sunrise():
    """Not knowing the sun is no reason to hold the night's energy for the evening."""
    manager = _nov_2_manager(forecast_kwh=0.0)
    manager.reserve_soc_pct()
    assert manager.get_status()["horizon_end"] == NOV_2_SUNRISE.isoformat()


@pytest.mark.parametrize(
    "now",
    [
        SUN_TAKES_OVER,  # the sun covers the house from this slot on
        SUN_TAKES_OVER + timedelta(hours=1),  # still before the solar midpoint
        NOV_2 + timedelta(hours=14),  # past it, with sun still ahead
        NOV_2 + timedelta(hours=23, minutes=30),  # the evening before the next night
    ],
)
def test_once_the_sun_has_taken_over_the_horizon_is_tomorrow_s_sunrise(now):
    manager = _nov_2_manager(now=now)
    assert manager.plan.horizon_end == NOV_3_SUNRISE


def test_a_later_uncovered_slot_does_not_pull_the_horizon_back_in():
    """Once the sun has covered the house today, a forecast dip must not flip back."""
    manager = _nov_2_manager(now=SUN_TAKES_OVER)
    assert manager.plan.horizon_end == NOV_3_SUNRISE

    later = SUN_TAKES_OVER + timedelta(minutes=20)
    # The refreshed profile now says 10:15 and 10:30 outrun the sun.
    manager._controller._pricing_mgr = _nov_2_pricing(
        now=later, load={41: 2.0, 42: 2.0}
    )
    manager._now = lambda: later
    asyncio.get_event_loop().run_until_complete(
        manager.async_rebuild_plan("test", force=True)
    )
    assert manager.plan.horizon_end == NOV_3_SUNRISE


def _measured(solar_kw, home_kw):
    """The consumption tracker's live readings: total solar and home load."""
    return SimpleNamespace(
        _read_total_solar_power_kw=lambda: solar_kw,
        get_validated_home_power_kw=lambda: home_kw,
    )


def _nov_2_measured_manager(now, tracker, **pricing_kwargs):
    controller = _controller(
        _pricing_mgr=_nov_2_pricing(now=now, **pricing_kwargs),
        discharge_reserve_min_saving=0.029,
        _consumption_tracker=tracker,
    )
    return _built(controller, now)


def _refresh(manager, now, **pricing_kwargs):
    manager._controller._pricing_mgr = _nov_2_pricing(now=now, **pricing_kwargs)
    manager._now = lambda: now
    asyncio.get_event_loop().run_until_complete(
        manager.async_rebuild_plan("test", force=True)
    )


def test_the_take_over_waits_for_the_panels_to_carry_the_house():
    """5 Nov: the forecast's slot passed at 10:03 while the house imported
    1.2 kW more than the panels made. A refresh that moves the sun later then
    brings the late morning back into the plan."""
    manager = _nov_2_measured_manager(SUN_TAKES_OVER, _measured(0.76, 2.0))
    assert manager.plan.horizon_end == NOV_3_SUNRISE

    later = SUN_TAKES_OVER + timedelta(minutes=20)
    _refresh(manager, later, load={41: 2.0, 42: 2.0})
    assert manager.plan.horizon_end == NOV_2 + timedelta(hours=10, minutes=45)


def test_once_the_panels_carry_the_house_the_take_over_holds():
    manager = _nov_2_measured_manager(SUN_TAKES_OVER, _measured(2.4, 2.0))

    _refresh(manager, SUN_TAKES_OVER + timedelta(minutes=20), load={41: 2.0, 42: 2.0})
    assert manager.plan.horizon_end == NOV_3_SUNRISE


def test_unreadable_measurements_keep_the_forecast_take_over():
    manager = _nov_2_measured_manager(SUN_TAKES_OVER, _measured(None, 2.0))

    _refresh(manager, SUN_TAKES_OVER + timedelta(minutes=20), load={41: 2.0, 42: 2.0})
    assert manager.plan.horizon_end == NOV_3_SUNRISE


def _nov_2_at_measured_share(now, ratio):
    controller = _controller(
        _pricing_mgr=_nov_2_pricing(now=now), discharge_reserve_min_saving=0.029
    )
    manager = _manager(controller, now)
    manager._measured_solar_ratio = lambda _now: ratio
    asyncio.get_event_loop().run_until_complete(manager.async_rebuild_plan("test"))
    return manager


def test_a_day_the_panels_run_far_below_the_forecast_keeps_the_evening():
    """At 40 % of the forecast the sun never covers the house, so the horizon
    no longer ends at the forecast's 10:00 and the evening peak is claimed."""
    as_forecast = _nov_2_at_measured_share(NOV_2 + timedelta(hours=9), None)
    assert as_forecast.plan.horizon_end == SUN_TAKES_OVER

    measured = _nov_2_at_measured_share(NOV_2 + timedelta(hours=9), 0.4)
    measured.reserve_soc_pct()
    status = measured.get_status()
    assert status["horizon_end"] == NOV_3_SUNRISE.isoformat()
    assert status["measured_solar_ratio"] == 0.4
    starts = [slot["start"] for slot in status["reserved_slots"]]
    assert (NOV_2 + timedelta(hours=18)).isoformat() in starts


# ----------------------------------------------------------------------
# The provider's solar curve
# ----------------------------------------------------------------------


def _hourly(day: datetime, first: int, last: int) -> list[PriceSlot]:
    return [
        PriceSlot(day + timedelta(hours=hour), day + timedelta(hours=hour + 1), 0.3)
        for hour in range(first, last)
    ]


def _periods(day: datetime, energy_by_hour: dict[int, float]):
    """Provider periods in UTC, the zone the curve manager below runs in."""
    return tuple(
        SolarForecastPeriod(
            (day + timedelta(hours=hour)).replace(tzinfo=timezone.utc),
            (day + timedelta(hours=hour + 1)).replace(tzinfo=timezone.utc),
            energy,
        )
        for hour, energy in energy_by_hour.items()
    )


def _curve_manager(periods=None, **controller_overrides) -> DischargeReserveManager:
    controller = _controller(**controller_overrides)
    if periods is not None:
        controller.solar_forecast_periods = periods
    manager = DischargeReserveManager(
        SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), controller
    )
    manager._now = lambda: NOW
    return manager


# The flat-horizon sine: sun from 07:00, an even 1 kWh an hour until noon.
SINE_MORNING = {slot: 1.0 for slot in _hourly(DAY, 7, 12)}

# The provider: the panels see nothing until 09:00, the mountain's hours.
MOUNTAIN_MORNING = {7: 0.0, 8: 0.0, 9: 1.0, 10: 2.0, 11: 3.0}


def test_the_provider_curve_moves_the_sun_to_when_the_panels_see_it():
    manager = _curve_manager(_periods(DAY, MOUNTAIN_MORNING))
    shaped = manager._follow_provider_curve(SINE_MORNING, NOW)

    by_hour = {slot.start.hour: kwh for slot, kwh in shaped.items()}
    assert by_hour[7] == 0.0
    assert by_hour[8] == 0.0
    assert by_hour[9] == pytest.approx(5.0 / 6.0)
    assert by_hour[11] == pytest.approx(2.5)
    # The day's energy is the sine's; only its split moved.
    assert sum(shaped.values()) == pytest.approx(5.0)


def test_without_provider_periods_the_sine_stays():
    manager = _curve_manager()
    assert manager._follow_provider_curve(SINE_MORNING, NOW) == SINE_MORNING


def test_a_day_the_periods_do_not_cover_keeps_the_sine():
    tomorrow = DAY + timedelta(days=1)
    solar = {**SINE_MORNING, **{slot: 0.5 for slot in _hourly(tomorrow, 7, 9)}}
    manager = _curve_manager(_periods(DAY, MOUNTAIN_MORNING))
    shaped = manager._follow_provider_curve(solar, NOW)

    for slot in _hourly(tomorrow, 7, 9):
        assert shaped[slot] == 0.5
    assert shaped[_hourly(DAY, 7, 8)[0]] == 0.0


def test_each_day_keeps_its_own_energy():
    tomorrow = DAY + timedelta(days=1)
    solar = {**SINE_MORNING, **{slot: 0.5 for slot in _hourly(tomorrow, 7, 9)}}
    periods = _periods(DAY, MOUNTAIN_MORNING) + _periods(tomorrow, {7: 0.0, 8: 4.0})
    shaped = _curve_manager(periods)._follow_provider_curve(solar, NOW)

    today = sum(kwh for slot, kwh in shaped.items() if slot.start.date() == DAY.date())
    assert today == pytest.approx(5.0)
    assert shaped[_hourly(tomorrow, 7, 8)[0]] == 0.0
    assert shaped[_hourly(tomorrow, 8, 9)[0]] == pytest.approx(1.0)


def test_a_provider_that_leaves_out_its_dark_hours_still_covers_the_day():
    """Solcast sends no period for an hour without sun."""
    solar = {
        **{slot: 0.0 for slot in _hourly(DAY, 0, 7)},
        **SINE_MORNING,
    }
    manager = _curve_manager(_periods(DAY, MOUNTAIN_MORNING))
    shaped = manager._follow_provider_curve(solar, NOW)
    assert shaped[_hourly(DAY, 7, 8)[0]] == 0.0
    assert sum(shaped.values()) == pytest.approx(5.0)


def test_a_curve_with_a_hole_in_daylight_is_not_trusted():
    periods = _periods(DAY, {7: 0.0, 8: 0.0, 11: 3.0})
    manager = _curve_manager(periods)
    assert manager._follow_provider_curve(SINE_MORNING, NOW) == SINE_MORNING


def _measured_ratio(measured_kwh, now, *, on=DAY, source="sensor.solar_now"):
    manager = _curve_manager(_periods(DAY, MOUNTAIN_MORNING))
    manager._controller.solar_production_sensor = source
    manager._controller._daily_solar_energy_kwh = measured_kwh
    manager._controller._daily_solar_energy_date = on.date()
    return manager._measured_solar_ratio(now)


def test_the_measured_share_is_today_s_production_over_the_provider_s_so_far():
    # By 10:30 the provider expected 1.0 + half of 2.0 kWh.
    assert _measured_ratio(1.0, DAY + timedelta(hours=10, minutes=30)) == pytest.approx(0.5)


def test_too_little_expected_yet_is_not_judged():
    assert _measured_ratio(0.0, DAY + timedelta(hours=9, minutes=15)) is None


def test_without_a_production_source_nothing_is_judged():
    """The accumulator reads 0.0 all day when nothing feeds it; that must not
    read as a day at the lower clamp."""
    assert _measured_ratio(0.0, DAY + timedelta(hours=12), source=None) is None


def test_yesterday_s_production_is_not_today_s():
    assert _measured_ratio(1.0, DAY + timedelta(hours=12), on=DAY - timedelta(days=1)) is None


@pytest.mark.parametrize("measured, expected", [(0.1, 0.25), (20.0, 1.5)])
def test_the_measured_share_is_clamped(measured, expected):
    assert _measured_ratio(measured, DAY + timedelta(hours=12)) == pytest.approx(expected)


def test_only_today_s_sun_still_ahead_takes_the_measured_share():
    manager = _curve_manager()
    manager._measured_solar_ratio = lambda _now: 0.5
    past, ahead = _hourly(DAY, 9, 10)[0], _hourly(DAY, 14, 15)[0]
    tomorrow = _hourly(DAY + timedelta(days=1), 10, 11)[0]
    scaled = manager._follow_measured_production(
        {past: 1.0, ahead: 2.0, tomorrow: 3.0}, NOW
    )
    assert scaled == {past: 1.0, ahead: 1.0, tomorrow: 3.0}


def test_the_morning_demand_follows_the_provider_curve():
    """Before the panels see the sun the house still runs on the battery."""
    slots = _hourly(DAY, 7, 12)

    def fraction(hour: float) -> float:
        return max(0.0, min(1.0, (hour - 7.0) / 5.0))

    intervals = [0.0] * 96
    for index in range(28, 48):  # 07:00 - 12:00, 0.5 kWh an hour
        intervals[index] = 0.125
    profile = SimpleNamespace(
        intervals_kwh=intervals,
        intervals_by_date={DAY.date(): intervals},
        energy_kwh=sum(intervals),
        source="profile",
    )
    pricing = _pricing(
        get_future_price_slots=lambda horizon_end=None: slots,
        _profile_remaining_consumption=lambda start, end: profile,
        _curtailment_forecast_model=lambda now: (5.0, fraction, None),
        _solar_forecast_is_remaining=lambda: True,
    )
    now = DAY + timedelta(hours=6)

    flat = _curve_manager(_pricing_mgr=pricing)
    demand, _ = flat._demand_and_surplus_by_slot(pricing, slots, now, None)
    assert demand[slots[0]] == 0.0

    mountain = _curve_manager(
        _periods(DAY, MOUNTAIN_MORNING), _pricing_mgr=pricing
    )
    demand, surplus = mountain._demand_and_surplus_by_slot(pricing, slots, now, None)
    assert demand[slots[0]] == pytest.approx(0.5)
    assert demand[slots[1]] == pytest.approx(0.5)
    assert surplus[slots[0]] == 0.0
    assert surplus[slots[4]] > 0.0


# ----------------------------------------------------------------------
# The seam: how the reserve reaches the battery
# ----------------------------------------------------------------------


def _blocks(reserve_pct: float, coordinators=None, effective_floor=None, manager=None):
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
        _discharge_reserve_mgr=manager,
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


def test_a_coupled_pack_battery_is_judged_by_its_emptiest_pack():
    """Mirrors the min_soc blocker: the first pack to empty decides (#350)."""
    coordinator = _coordinator(soc=45.0)
    coordinator.data = dict(
        coordinator.data, battery_soc_pack_1=80.0, battery_soc_pack_2=10.0
    )
    # min(pack) = 10 %, below the 10 + 40 reserved floor, so the reserve holds.
    assert ("battery-1", "price_reserve") in _blocks(40.0, [coordinator])


def test_an_unreadable_soc_blocks_nothing():
    coordinator = _coordinator()
    coordinator.data = {"battery_soc": None, "battery_total_energy": 10.0}
    assert _blocks(40.0, [coordinator]) == {}


def test_a_reserve_a_rounding_error_below_the_soc_still_holds():
    """5 + 22.999999999999996 is 28 for a battery that reports whole percent."""
    registry = _blocks(22.999999999999996, [_coordinator(soc=28.0, min_soc=5.0)])
    assert ("battery-1", "price_reserve") in registry


@pytest.mark.parametrize(
    ("capacity", "min_soc", "soc"),
    [(2.56, 5.0, 28.0), (2.56, 12.0, 58.0), (5.12, 5.0, 28.0)],
)
def test_a_reserve_of_everything_stored_holds_the_battery(capacity, min_soc, soc):
    """The reserve claims all of it, so the battery must not discharge a step.

    floor + reserve_soc_pct lands a rounding error below these SOCs.
    """
    pricing = _pricing(_profile_remaining_consumption=lambda start, end: _forecast(50.0))
    coordinator = _coordinator(soc=soc, min_soc=min_soc, capacity=capacity)
    manager = _built(_controller(coordinators=[coordinator], _pricing_mgr=pricing))
    pct = manager.reserve_soc_pct()
    status = manager.get_status()
    assert status["reserve_kwh"] == pytest.approx(status["usable_energy_kwh"])

    assert ("battery-1", "price_reserve") in _blocks(pct, [coordinator])


def _fleet_blocks(coordinators, evening_kwh: float):
    """The real manager and the blocker pass, as one control cycle runs them."""
    pricing = _pricing(
        _profile_remaining_consumption=lambda start, end: _forecast(evening_kwh)
    )
    manager = _built(_controller(coordinators=coordinators, _pricing_mgr=pricing))
    pct = manager.reserve_soc_pct()
    return _blocks(pct, coordinators, manager=manager), manager


@pytest.mark.parametrize(
    "batteries",
    [
        # #700: two Venus E with the same floor, the fleet floor at 65.73 %.
        [(68.0, 12.0, 5.12), (65.0, 12.0, 5.12)],
        # Different floors and sizes, the fleet floor at 5.54 points.
        [(20.0, 12.0, 5.12), (9.0, 5.0, 8.16)],
    ],
)
def test_every_battery_is_held_once_everything_stored_is_reserved(batteries):
    """One fleet percentage held one battery and let the other discharge."""
    coordinators = [
        _coordinator(soc=soc, min_soc=floor, capacity=capacity, name=f"battery-{i}")
        for i, (soc, floor, capacity) in enumerate(batteries, start=1)
    ]
    registry, _ = _fleet_blocks(coordinators, evening_kwh=50.0)
    for i, (soc, _floor, _capacity) in enumerate(batteries, start=1):
        assert registry[(f"battery-{i}", "price_reserve")]["reserved_floor"] == soc


def test_no_battery_is_held_while_the_fleet_has_more_than_the_reserve():
    """4 kWh reserved, 5 kWh stored: the fleet may still spend 1 kWh.

    The fleet floor (20 points on 20 kWh) held the emptier battery already.
    """
    coordinators = [
        _coordinator(soc=40.0, name="battery-1"),
        _coordinator(soc=30.0, name="battery-2"),
    ]
    registry, manager = _fleet_blocks(coordinators, evening_kwh=4.0)
    assert manager.get_status()["reserve_kwh"] == pytest.approx(4.0)
    assert registry == {}


def test_both_batteries_are_held_when_the_fleet_reaches_the_reserve():
    coordinators = [
        _coordinator(soc=30.0, name="battery-1"),
        _coordinator(soc=30.0, name="battery-2"),
    ]
    registry, _ = _fleet_blocks(coordinators, evening_kwh=4.0)
    assert set(registry) == {("battery-1", "price_reserve"), ("battery-2", "price_reserve")}
    details = registry[("battery-1", "price_reserve")]
    assert details["reserve_soc_pct"] == pytest.approx(20.0)
    assert details["fleet_reserve_soc_pct"] == pytest.approx(20.0)


def test_a_battery_on_its_floor_is_not_held_by_the_reserve():
    """Its share is zero, so min_soc stops it, not the reserve.

    Holding it too showed price_reserve_hold while the other battery still
    discharged its share.
    """
    coordinators = [
        _coordinator(soc=12.0, min_soc=12.0, capacity=5.12, name="Venus 1"),
        _coordinator(soc=60.0, min_soc=5.0, capacity=8.16, name="Zendure 1"),
    ]
    registry, manager = _fleet_blocks(coordinators, evening_kwh=2.0)
    assert manager.get_status()["battery_reserve_soc_pct"]["Venus 1"] == 0.0
    assert ("Venus 1", "price_reserve") not in registry
    assert registry == {}


def test_a_battery_on_its_floor_is_not_held_next_to_one_that_is():
    coordinators = [
        _coordinator(soc=12.0, min_soc=12.0, capacity=5.12, name="Venus 1"),
        _coordinator(soc=30.0, min_soc=5.0, capacity=8.16, name="Zendure 1"),
    ]
    registry, _ = _fleet_blocks(coordinators, evening_kwh=50.0)
    assert set(registry) == {("Zendure 1", "price_reserve")}


def test_the_status_shows_each_battery_s_share():
    coordinators = [
        _coordinator(soc=68.0, min_soc=12.0, capacity=5.12, name="Venus 1"),
        _coordinator(soc=65.0, min_soc=12.0, capacity=5.12, name="Venus 2"),
    ]
    _, manager = _fleet_blocks(coordinators, evening_kwh=50.0)
    assert manager.get_status()["battery_reserve_soc_pct"] == {
        "Venus 1": 56.0,
        "Venus 2": 53.0,
    }


def test_a_battery_left_out_of_the_fleet_is_judged_by_the_fleet_figure():
    """Unchanged for a battery the manager did not size the reserve against."""
    coordinator = _coordinator(soc=45.0)
    manager = SimpleNamespace(battery_reserve_soc_pct=lambda coordinator: None)
    registry = _blocks(40.0, [coordinator], manager=manager)
    assert registry[("battery-1", "price_reserve")]["reserved_floor"] == 50.0


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


def test_a_morning_peak_no_longer_cancels_the_whole_day_s_pv_credit():
    """The night of 2026-09-11: held 2 kWh overnight against 13.9 kWh of sun.

    One 07:00 slot above ``price + min_saving`` used to end the credit window
    for every later claim, so the evening peak was reserved at 00:30 as if the
    day held no sun at all. Only the morning claim, which no sun precedes, can
    survive the sweep now.
    """
    from custom_components.omnibattery.pricing.discharge_reserve import (
        REASON_RESERVED,
        plan_discharge_reserve,
    )

    morning = _slot(7, 0.42)
    midday = _slot(12, 0.10)
    evening = _slot(19, 0.52)
    plan = plan_discharge_reserve(
        [morning, midday, evening],
        {morning: 0.5, midday: 0.0, evening: 2.0},
        surplus_by_slot={morning: 0.0, midday: 8.0, evening: 0.0},
        free_space_kwh=1.5,
        usable_energy_kwh=3.0,
        total_capacity_kwh=5.12,
        current_price=0.345,
        min_saving=0.05,
        now=DAY + timedelta(minutes=30),
    )
    assert plan.reason == REASON_RESERVED
    assert plan.reserve_kwh == pytest.approx(0.5)


def test_the_room_for_pv_is_the_room_at_pv_time_not_the_room_now():
    """Demand served before the sun arrives is space the sun can land in."""
    from custom_components.omnibattery.pricing.discharge_reserve import (
        plan_discharge_reserve,
    )

    morning = _slot(10, 0.10)
    midday = _slot(12, 0.10)
    evening = _slot(19, 0.45)
    plan = plan_discharge_reserve(
        [morning, midday, evening],
        {morning: 1.0, midday: 0.0, evening: 3.0},
        surplus_by_slot={morning: 0.0, midday: 4.0, evening: 0.0},
        free_space_kwh=1.0,
        usable_energy_kwh=6.0,
        total_capacity_kwh=10.0,
        current_price=0.15,
        min_saving=0.05,
        now=DAY + timedelta(hours=9),
    )
    # 1.0 kWh of room now plus the 1.0 kWh the morning takes out of the battery.
    assert plan.reserve_kwh == pytest.approx(1.0)


def test_demand_that_follows_the_sun_makes_no_room_for_it():
    """A battery with no room exports the surplus; the peak still needs the grid.

    The evening demand is real, but it happens after the sun has already been
    exported, so it cannot be counted as space the sun could have landed in.
    """
    from custom_components.omnibattery.pricing.discharge_reserve import (
        REASON_RESERVED,
        plan_discharge_reserve,
    )

    midday = _slot(12, 0.10)
    evening = _slot(18, 0.12)
    peak = _slot(19, 0.50)
    plan = plan_discharge_reserve(
        [midday, evening, peak],
        {midday: 0.0, evening: 2.0, peak: 2.0},
        surplus_by_slot={midday: 3.0, evening: 0.0, peak: 0.0},
        free_space_kwh=0.0,
        usable_energy_kwh=2.0,
        total_capacity_kwh=5.0,
        current_price=0.15,
        min_saving=0.05,
        now=DAY + timedelta(hours=9),
    )
    assert plan.reason == REASON_RESERVED
    assert plan.reserve_kwh == pytest.approx(2.0)


def test_the_same_sun_cannot_pay_for_two_peaks():
    """3 kWh of PV covers the first claim; the second keeps what is left over."""
    from custom_components.omnibattery.pricing.discharge_reserve import (
        plan_discharge_reserve,
    )

    midday = _slot(12, 0.10)
    first_peak = _slot(17, 0.45)
    second_peak = _slot(19, 0.50)
    plan = plan_discharge_reserve(
        [midday, first_peak, second_peak],
        {midday: 0.0, first_peak: 2.0, second_peak: 2.5},
        surplus_by_slot={midday: 3.0, first_peak: 0.0, second_peak: 0.0},
        free_space_kwh=10.0,
        usable_energy_kwh=6.0,
        total_capacity_kwh=10.0,
        current_price=0.15,
        min_saving=0.05,
        now=DAY + timedelta(hours=9),
    )
    assert plan.reserve_kwh == pytest.approx(1.5)


# ----------------------------------------------------------------------
# What the entity can explain on its own
# ----------------------------------------------------------------------


def test_the_plan_records_what_the_claims_and_the_sun_each_did():
    """reserve_kwh is claimed_kwh minus pv_credit_kwh, and all three are kept."""
    from custom_components.omnibattery.pricing.discharge_reserve import (
        plan_discharge_reserve,
    )

    morning = _slot(7, 0.42)
    midday = _slot(12, 0.10)
    evening = _slot(19, 0.52)
    plan = plan_discharge_reserve(
        [morning, midday, evening],
        {morning: 0.5, midday: 0.0, evening: 2.0},
        surplus_by_slot={morning: 0.0, midday: 8.0, evening: 0.0},
        free_space_kwh=1.5,
        usable_energy_kwh=3.0,
        total_capacity_kwh=5.12,
        current_price=0.345,
        min_saving=0.05,
        now=DAY + timedelta(minutes=30),
    )
    assert plan.threshold_price == pytest.approx(0.395)
    assert plan.claimed_kwh == pytest.approx(2.5)
    assert plan.pv_credit_kwh == pytest.approx(2.0)
    assert plan.reserve_kwh == pytest.approx(plan.claimed_kwh - plan.pv_credit_kwh)
    assert plan.horizon_demand_kwh == pytest.approx(2.5)
    assert plan.horizon_surplus_kwh == pytest.approx(8.0)
    assert [
        (slot.start.hour, round(claimed, 3), round(credit, 3))
        for slot, claimed, credit in plan.claim_breakdown
    ] == [(7, 0.5, 0.0), (19, 2.0, 2.0)]


def test_a_released_cycle_still_shows_the_claim_the_sun_paid_off():
    """pv_refills_in_time reserves nothing, so the claim must be kept elsewhere."""
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
    assert plan.reason == REASON_PV_COVERS_IT
    assert plan.selected_slots == []
    assert plan.claimed_kwh == pytest.approx(3.0)
    assert plan.pv_credit_kwh == pytest.approx(3.0)
    assert len(plan.claim_breakdown) == 1


def test_a_cycle_that_reserves_nothing_clears_the_previous_figures():
    """Stale attributes would explain the wrong cycle."""
    plan = _built().plan
    assert plan is not None

    plan.reserve_kwh_at(NOW, 0.20)
    assert plan.claimed_kwh > 0

    # 17:00 at 0.42: nothing ahead clears the 0.05 margin any more.
    plan.reserve_kwh_at(DAY + timedelta(hours=17), 0.42)
    assert plan.claimed_kwh == 0.0
    assert plan.pv_credit_kwh == 0.0
    assert plan.threshold_price == pytest.approx(0.47)
    assert plan.claim_breakdown == []


def test_the_status_attributes_carry_the_audit_trail():
    manager = _built()
    manager.reserve_soc_pct()
    status = manager.get_status()
    assert status["threshold_price"] == pytest.approx(0.25)
    assert status["claimed_kwh"] == pytest.approx(4.0)
    assert status["pv_credit_kwh"] == 0.0
    assert status["horizon_demand_kwh"] == pytest.approx(4.0)
    assert status["horizon_surplus_kwh"] == 0.0
    assert [claim["start"] for claim in status["claims"]] == [
        (DAY + timedelta(hours=18)).isoformat(),
        (DAY + timedelta(hours=19)).isoformat(),
    ]


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


def test_only_part_of_the_expected_surplus_is_credited():
    """A day-ahead forecast must not release the whole reserve overnight.

    Nothing corrects the credit before dawn, so a cloudy morning would leave
    the evening peak bought at peak price. Crediting a fraction lets the
    5-minute rebuild firm the release up as the real production arrives.
    """
    from custom_components.omnibattery.control.discharge_reserve import (
        SURPLUS_CREDIT_FACTOR,
    )

    midday = _slot(12, 0.15)
    evening = _slot(19, 0.45)
    pricing = _pricing(
        get_future_price_slots=lambda horizon_end=None: [midday, evening],
        _curtailment_forecast_model=lambda now: (8.0, None, None),
    )
    manager = _manager()
    demand, surplus = manager._demand_and_surplus_by_slot(
        pricing, [midday, evening], NOW, NOW + timedelta(hours=12)
    )

    # 8 kWh spread evenly over two slots; the learned profile puts 2 kWh in
    # the 19:00 slot and nothing at midday. The charge power is high enough
    # here that only the forecast haircut and the charge losses apply.
    assert surplus[midday] == pytest.approx(SURPLUS_CREDIT_FACTOR * CHARGE_EFFICIENCY * 4.0)
    assert surplus[evening] == pytest.approx(SURPLUS_CREDIT_FACTOR * CHARGE_EFFICIENCY * 2.0)
    assert SURPLUS_CREDIT_FACTOR < 1.0
    # Net demand is untouched: the haircut is on the credit, not the load.
    assert demand[evening] == pytest.approx(0.0)


def test_the_surplus_credit_is_capped_by_the_charge_power():
    """Sun the battery cannot take in the hour never pays an evening claim.

    A 5 kWh surplus hour behind a 2.5 kW charger puts 2.5 kWh in the battery,
    not 5. Crediting the forecast unbounded released a reserve against kWh
    that were always going to be exported.
    """
    from custom_components.omnibattery.control.discharge_reserve import (
        SURPLUS_CREDIT_FACTOR,
    )

    midday = _slot(12, 0.15)
    evening = _slot(19, 0.45)
    pricing = _pricing(
        get_future_price_slots=lambda horizon_end=None: [midday, evening],
        _curtailment_forecast_model=lambda now: (10.0, None, None),
    )
    manager = _manager(_controller(max_charge_capacity=2500.0))
    _demand, surplus = manager._demand_and_surplus_by_slot(
        pricing, [midday, evening], NOW, NOW + timedelta(hours=12)
    )

    # 10 kWh over two slots: 5 kWh of raw midday surplus, capped to the
    # 2.5 kWh the charger can take in an hour.
    assert surplus[midday] == pytest.approx(SURPLUS_CREDIT_FACTOR * CHARGE_EFFICIENCY * 2.5)


def test_no_charge_capacity_credits_no_surplus():
    """With nothing able to charge, no sun lands in the battery."""
    midday = _slot(12, 0.15)
    evening = _slot(19, 0.45)
    pricing = _pricing(
        get_future_price_slots=lambda horizon_end=None: [midday, evening],
        _curtailment_forecast_model=lambda now: (8.0, None, None),
    )
    manager = _manager(_controller(max_charge_capacity=0.0))
    _demand, surplus = manager._demand_and_surplus_by_slot(
        pricing, [midday, evening], NOW, NOW + timedelta(hours=12)
    )

    assert surplus[midday] == pytest.approx(0.0)
    assert surplus[evening] == pytest.approx(0.0)


# ----------------------------------------------------------------------
# The evening sees tomorrow's morning up to the sun
# ----------------------------------------------------------------------

TOMORROW = DAY + timedelta(days=1)
TOMORROW_SUNRISE = TOMORROW + timedelta(hours=7)
EVENING = DAY + timedelta(hours=21)


def _evening_pricing(now=EVENING):
    """21:00 to tomorrow noon, hourly: the night 0.30, tomorrow 07:00 to 09:00
    dear at 0.55, then 0.30. The house uses 0.5 kWh an hour."""
    slots = [
        PriceSlot(
            now + timedelta(hours=i),
            now + timedelta(hours=i + 1),
            0.55 if 7 <= (now + timedelta(hours=i)).hour < 9 and i > 3 else 0.30,
        )
        for i in range(15)
    ]
    intervals = [0.125] * 96
    profile = SimpleNamespace(
        intervals_kwh=intervals,
        intervals_by_date={DAY.date(): intervals, TOMORROW.date(): intervals},
        energy_kwh=sum(intervals),
        source="profile",
    )
    return _pricing(
        get_future_price_slots=lambda horizon_end=None: [
            slot for slot in slots if horizon_end is None or slot.start <= horizon_end
        ],
        _profile_remaining_consumption=lambda start, end: profile,
        # After sunset a remaining-today sensor reads 0; past the midpoint.
        _curtailment_forecast_model=lambda _now: (0.0, lambda hour: 1.0, None),
        _solar_forecast_is_remaining=lambda: True,
        _get_current_price=lambda: 0.30,
        energy_horizon_end=lambda _when: TOMORROW_SUNRISE,
    )


def _evening_manager(periods=None):
    manager = _curve_manager(
        periods,
        _pricing_mgr=_evening_pricing(),
        discharge_reserve_min_saving=0.05,
        coordinators=[_coordinator(soc=80.0)],
    )
    manager._now = lambda: EVENING
    asyncio.get_event_loop().run_until_complete(manager.async_rebuild_plan("test"))
    return manager


# Tomorrow's provider periods: the sun beats the 0.5 kWh an hour from 09:00.
TOMORROW_SUN = {7: 0.05, 8: 0.3, 9: 0.8, 10: 1.2, 11: 1.5}


def test_the_evening_plan_runs_to_where_the_sun_covers_the_house_tomorrow():
    """#721, 21:38: the plan ended at sunrise while the house ran on the battery
    until about 09:00, so the dear slots after sunrise were not kept."""
    manager = _evening_manager(_periods(TOMORROW, TOMORROW_SUN))

    assert manager.plan.horizon_end == TOMORROW + timedelta(hours=9)
    manager.reserve_soc_pct()
    claimed = {slot["start"] for slot in manager.get_status()["claims"]}
    assert (TOMORROW + timedelta(hours=8)).isoformat() in claimed


def test_without_tomorrow_s_periods_the_evening_plan_ends_at_sunrise():
    assert _evening_manager().plan.horizon_end == TOMORROW_SUNRISE


def test_a_morning_the_sun_does_not_cover_by_noon_keeps_sunrise():
    dull = {hour: 0.1 for hour in range(7, 12)}
    assert _evening_manager(_periods(TOMORROW, dull)).plan.horizon_end == TOMORROW_SUNRISE
