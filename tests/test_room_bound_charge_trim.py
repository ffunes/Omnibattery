"""The load the grid carries while a slot charges, for a plan bound by the room.

While a slot charges, the grid feeds the house, so the battery does not drain
then (#785). When the whole need fits in the battery, that load is need the
battery no longer has to store, and the plan trims it. When the need is larger
than the room, the battery fills its room either way and runs empty again
later: the carried load only cancels need beyond the room. Trimming the room
itself bought 3.1 of 5.5 kWh for a 21.6 kWh night deficit (11 Nov 2026).

``stored = min(room, need - carried)``: a need equal to the room gives the
plain #785 trim, and a need of at least room + carried gives the whole room.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.calculations import (
    net_of_load_carried_by_grid,
)
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalEvaluationRequest,
    EnergyInterval,
    allocate_price_slots,
    build_energy_deadlines,
    evaluate_chronological_request,
)

_START = datetime(2026, 11, 11, 0, 0)
_LOAD_KW = 0.8
_POWER_KW = 1.2
_USABLE = 0.5
_ROOM = 2.0


def _night(hours: int = 8, load_kw: float = _LOAD_KW) -> list[EnergyInterval]:
    return [
        EnergyInterval(
            _START + timedelta(minutes=15 * i),
            _START + timedelta(minutes=15 * (i + 1)),
            load_kw / 4,
            0.0,
        )
        for i in range(hours * 4)
    ]


def _slots(hours: int = 8) -> list[PriceSlot]:
    # Four cheap hours first, then dear ones: the plan charges early.
    return [
        PriceSlot(
            _START + timedelta(hours=h),
            _START + timedelta(hours=h + 1),
            0.10 if h < 4 else 0.30,
        )
        for h in range(hours)
    ]


def _allocate(*, required, uncapped=None, carried=True, room=_ROOM, intervals=None,
              power=_POWER_KW, slots=None):
    intervals = intervals or _night()
    return allocate_price_slots(
        intervals,
        build_energy_deadlines(intervals, _USABLE, usable_capacity_kwh=_USABLE + room),
        slots or _slots(),
        total_required_kwh=required,
        effective_power_kw=power,
        now=_START,
        horizon_end=_START + timedelta(hours=8),
        headroom_kwh=room,
        usable_initial_kwh=_USABLE,
        grid_carries_load_while_charging=carried,
        uncapped_required_kwh=uncapped,
    )


# A full night needs 6.4 kWh; with 0.5 kWh usable that is 5.9 kWh, well
# above the 2 kWh room.
_NEED = _LOAD_KW * 8 - _USABLE


def test_a_room_bound_plan_buys_the_whole_room():
    plan = _allocate(required=_ROOM, uncapped=_NEED)

    assert plan.allocated_kwh == pytest.approx(_ROOM, abs=1e-6)
    assert plan.reason == "ok"


def test_without_the_uncapped_need_the_plain_trim_applies():
    # Callers that do not pass the need keep the #785 behaviour.
    plain = _allocate(required=_ROOM)
    explicit_none = _allocate(required=_ROOM, uncapped=None)

    assert plain.allocated_kwh < _ROOM - 0.5
    assert explicit_none.allocated_kwh == pytest.approx(plain.allocated_kwh)


def test_a_need_that_fits_the_room_is_trimmed_exactly_as_before():
    # Need == required: nothing lies beyond the room, so #785 is unchanged.
    need = 1.5
    before = _allocate(required=need)
    after = _allocate(required=need, uncapped=need)

    assert after.allocated_kwh == pytest.approx(before.allocated_kwh)
    assert [a.planned_battery_kwh for a in after.allocations] == pytest.approx(
        [a.planned_battery_kwh for a in before.allocations]
    )
    assert after.allocated_kwh < need


def test_a_need_just_above_the_room_trims_only_the_rest():
    # The carried load first cancels the 0.3 kWh beyond the room.
    plain = _allocate(required=_ROOM)
    carried = _ROOM - plain.allocated_kwh
    assert carried > 0.3  # the scenario only means something if it trims

    plan = _allocate(required=_ROOM, uncapped=_ROOM + 0.3)

    assert plan.allocated_kwh == pytest.approx(_ROOM + 0.3 - carried, abs=1e-6)
    assert plain.allocated_kwh < plan.allocated_kwh < _ROOM


def test_a_need_of_room_plus_carried_load_is_exactly_the_whole_room():
    carried = _ROOM - _allocate(required=_ROOM).allocated_kwh

    plan = _allocate(required=_ROOM, uncapped=_ROOM + carried)

    assert plan.allocated_kwh == pytest.approx(_ROOM, abs=1e-6)


@pytest.mark.parametrize("need", [_ROOM + 0.01, 3.0, _NEED, 100.0])
def test_the_plan_never_stores_more_than_the_room(need):
    plan = _allocate(required=_ROOM, uncapped=need)

    assert plan.allocated_kwh <= _ROOM + 1e-6
    for allocation in plan.allocations:
        # No slot beyond what it can store in its hour.
        assert allocation.planned_battery_kwh <= _POWER_KW * CHARGE_EFFICIENCY + 1e-6


@pytest.mark.parametrize("need", [0.0, 1.0, _ROOM])
def test_a_need_at_or_below_the_required_total_changes_nothing(need):
    # A deadline-sized plan can require more than the scalar need; nothing
    # then lies beyond the room and the trim is the plain one.
    plain = _allocate(required=_ROOM)

    assert _allocate(required=_ROOM, uncapped=need).allocated_kwh == pytest.approx(
        plain.allocated_kwh
    )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -5.0])
def test_an_unusable_need_falls_back_to_the_plain_trim(bad):
    plain = _allocate(required=_ROOM)

    assert _allocate(required=_ROOM, uncapped=bad).allocated_kwh == pytest.approx(
        plain.allocated_kwh
    )


def test_the_need_does_nothing_when_the_grid_does_not_carry_the_load():
    # A weekly full charge (or a held Time Slot window) plans the room plainly.
    without = _allocate(required=_ROOM, carried=False)
    with_need = _allocate(required=_ROOM, uncapped=_NEED, carried=False)

    assert with_need.allocated_kwh == pytest.approx(without.allocated_kwh)
    assert with_need.allocated_kwh == pytest.approx(_ROOM, abs=1e-6)


def test_a_full_capacity_slot_still_covers_a_shortfall_first():
    # #785: slots at full capacity that cannot meet the need keep charging;
    # the room-bound rule must not push them past their capacity or the room.
    slots = [
        PriceSlot(_START, _START + timedelta(minutes=30), 0.10),
    ]
    plan = _allocate(required=_ROOM, uncapped=_NEED, slots=slots)
    capacity = _POWER_KW * CHARGE_EFFICIENCY * 0.5

    assert plan.allocated_kwh == pytest.approx(capacity, abs=1e-3)
    assert plan.allocated_kwh <= _ROOM


def test_the_request_passes_the_need_through():
    intervals = _night()
    request = ChronologicalEvaluationRequest(
        now=_START,
        horizon_end=_START + timedelta(hours=8),
        intervals=intervals,
        price_slots=_slots(),
        total_required_kwh=_ROOM,
        effective_power_kw=_POWER_KW,
        headroom_kwh=_ROOM,
        usable_initial_kwh=_USABLE,
        grid_carries_load_while_charging=True,
        uncapped_required_kwh=_NEED,
    )

    plan = evaluate_chronological_request(request).plan

    assert plan.allocated_kwh == pytest.approx(_ROOM, abs=1e-6)


def test_the_published_requirement_is_not_reduced_below_the_room():
    # The trim also lowers the plan's required totals; a room-bound plan must
    # keep reporting the room it buys, not the trimmed figure.
    plan = _allocate(required=_ROOM, uncapped=_NEED)

    assert plan.total_required_kwh == pytest.approx(_ROOM, abs=1e-6)
    assert plan.total_shortfall_kwh == pytest.approx(0.0, abs=1e-6)


# ----------------------------------------------------------------------
# Scalar fallback (no consumption profile)
# ----------------------------------------------------------------------

_RATE = 1.2


def _trimmed(planned: float, load: float = 0.2) -> float:
    rate = _RATE * CHARGE_EFFICIENCY
    return planned * rate / (rate + load)


@pytest.mark.parametrize(
    ("need", "expected"),
    [
        (None, _trimmed(2.0)),                    # no need given: plain trim
        (2.0, _trimmed(2.0)),                     # need fits the room: plain trim
        (2.1, 2.1 - (2.0 - _trimmed(2.0))),       # just above: trims the rest
        (2.0 + (2.0 - _trimmed(2.0)), 2.0),       # room + carried: whole room
        (20.0, 2.0),                              # far above: whole room
        (math.nan, _trimmed(2.0)),                # unusable: plain trim
        (math.inf, _trimmed(2.0)),
        ("bogus", _trimmed(2.0)),
    ],
)
def test_scalar_trim_matrix(need, expected):
    assert net_of_load_carried_by_grid(2.0, _RATE, 0.2, need_kwh=need) == pytest.approx(
        expected
    )


@pytest.mark.parametrize("need", [0.0, -1.0, 0.5])
def test_scalar_need_below_the_trimmed_figure_never_lowers_it(need):
    # A need under the plain trim cannot make the plan smaller than #785 did.
    assert net_of_load_carried_by_grid(2.0, _RATE, 0.2, need_kwh=need) == pytest.approx(
        _trimmed(2.0)
    )


def test_scalar_without_load_or_power_returns_the_plan():
    assert net_of_load_carried_by_grid(2.0, 0.0, 0.2, need_kwh=20.0) == 2.0
    assert net_of_load_carried_by_grid(2.0, _RATE, 0.0, need_kwh=20.0) == 2.0


# ----------------------------------------------------------------------
# End to end: the 00:05 plan of 11 Nov 2026
# ----------------------------------------------------------------------

def test_the_0005_plan_of_11_november_buys_the_whole_room():
    """00:05 DAILY run, horizon to the sunrise after next: 1.47 kWh stored in
    a 7 kWh, 800 W battery, a 1.1-1.6 kW heat-pump house, 33.3 kWh scalar
    deficit, 5.53 kWh room. The fixed plan books the whole room in the cheap
    night (it booked 0 kWh before, then 3.1 kWh with only the netting fix).
    Built twice, like the engine, so nothing is netted a second time."""
    from custom_components.omnibattery.pricing.engine import PricingManager
    from custom_components.omnibattery.solar_forecast import (
        SolarForecastInput,
        SolarForecastPeriod,
    )
    from custom_components.omnibattery.tracking.consumption_profile import (
        INTERVAL_COUNT,
        ConsumptionForecast,
    )
    from datetime import timezone

    now = datetime(2026, 11, 11, 0, 5, 2)
    horizon = datetime(2026, 11, 12, 7, 54, 44)
    day = datetime(2026, 11, 11)

    def load_kw(hour):
        return 1.2 if hour < 7 else 1.1 if hour < 17 else 1.6 if hour < 22 else 1.2

    shape = [load_kw(i // 4) / 4 for i in range(INTERVAL_COUNT)]

    def forecast_between(start, end, **_kw):
        total, t = 0.0, start
        while t < end:
            nxt = min(end, t.replace(minute=t.minute // 15 * 15, second=0, microsecond=0)
                      + timedelta(minutes=15))
            total += load_kw(t.hour) * (nxt - t).total_seconds() / 3600
            t = nxt
        return ConsumptionForecast(
            total, list(shape), "profile", True,
            intervals_by_date={(day + timedelta(days=i)).date(): list(shape) for i in range(3)},
        )

    weights = [1, 2, 3, 3.5, 3, 2, 1]
    periods = tuple(
        SolarForecastPeriod(
            datetime(2026, 11, 11, h, tzinfo=timezone.utc),
            datetime(2026, 11, 11, h + 1, tzinfo=timezone.utc),
            5.17 * w / sum(weights),
        )
        for h, w in zip(range(9, 16), weights)
    )

    def price(q):
        h = q // 4
        return 0.20 if h < 6 else 0.26 if h < 9 else 0.22 if h < 16 else 0.32 if h < 21 else 0.24

    slots = [
        PriceSlot(day + timedelta(minutes=15 * q), day + timedelta(minutes=15 * (q + 1)), price(q))
        for q in range(96)
    ]
    battery = SimpleNamespace(
        data={"battery_soc": 21.0, "battery_total_energy": 7.0},
        min_soc=0, max_soc=100, effective_max_discharge_power=800.0,
    )
    controller = SimpleNamespace(
        _consumption_tracker=SimpleNamespace(
            consumption_profile=SimpleNamespace(),
            forecast_consumption_between=forecast_between,
        ),
        coordinators=[battery],
        _is_battery_manual_owned=lambda _c: False,
        solar_profile_mode="active",
        _predictive_safety_margin_kwh=0.35,
        max_contracted_power=7000,
        max_charge_capacity=1200,
        max_price_threshold=None,
        predictive_charging_mode="dynamic_pricing",
        dp_price_discharge_control=False,
        price_sensor=None,
        min_arbitrage_margin=None,
        _weekly_full_charge_pending=lambda: False,
    )
    manager = PricingManager(SimpleNamespace(config=SimpleNamespace(time_zone="UTC")), controller)
    manager._read_remaining_solar_input = lambda **_kw: SolarForecastInput(
        5.17, "remaining_sensor", periods=periods
    )
    manager.energy_horizon_end = lambda _now: horizon
    decision = {
        "should_charge": True,
        "planned_grid_charge_kwh": 5.53,
        "energy_deficit_kwh": 33.34,
        "avg_consumption_kwh": 39.63,
        "remaining_consumption_kwh": 39.63,
        "floor_deficit_kwh": 0.0,
        "energy_horizon_end": horizon,
        "total_available_kwh": 6.29,
    }

    for _ in range(2):
        plan = manager._build_chronological_plan(
            now=now, slots=slots, decision_data=decision, price_ceiling=None,
        )

    booked = sum(a.planned_battery_kwh for a in plan.allocations)
    assert decision["planned_grid_charge_kwh"] == pytest.approx(5.53)
    assert booked == pytest.approx(5.53, abs=0.05)
    assert all(a.slot.start.hour < 6 for a in plan.allocations)  # the cheap night
    assert plan.total_shortfall_kwh == pytest.approx(0.0, abs=1e-6)
