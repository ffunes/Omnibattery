"""The planner counts the slot hold: the grid carries a booked slot's whole load.

Dynamic Pricing keeps the battery idle for the rest of a booked slot once it
stored its amount (``dp_slot_hold``). The plan used to take only the load of
the minutes the slot charges off what it stores and left the rest of the slot
to the battery, so every slot ended fuller than planned. Re-plans shrink later
slots, but the last ones before sunrise have nothing after them, so the
excess stayed in the battery when the sun took over (15 Nov 2026).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.pricing import PriceSlot
from custom_components.omnibattery.pricing.engine import PricingManager
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput,
    SolarForecastPeriod,
)
from custom_components.omnibattery.tracking.consumption_profile import (
    INTERVAL_COUNT,
    ConsumptionForecast,
)
from custom_components.omnibattery.pricing.chronological import (
    _SLOT_HOLD_MIN_KWH,
    ChronologicalEvaluationRequest,
    EnergyInterval,
    allocate_price_slots,
    build_energy_deadlines,
    evaluate_chronological_request,
)

_START = datetime(2026, 11, 15, 0, 0)
_HOURS = 6
_LOAD_KW = 1.2
_POWER_KW = 2.5
_USABLE = 0.3
_ROOM = 8.0  # the whole night fits: the trim, not the room, sizes the plan


def _intervals(load_kw: float = _LOAD_KW) -> list[EnergyInterval]:
    return [
        EnergyInterval(
            _START + timedelta(minutes=15 * i),
            _START + timedelta(minutes=15 * (i + 1)),
            load_kw / 4,
            0.0,
        )
        for i in range(_HOURS * 4)
    ]


def _slots() -> list[PriceSlot]:
    # Cheap quarters early in each hour, so the plan charges in short slots
    # spread over the night.
    return [
        PriceSlot(
            _START + timedelta(minutes=15 * i),
            _START + timedelta(minutes=15 * (i + 1)),
            0.10 if i % 4 == 0 else 0.30,
        )
        for i in range(_HOURS * 4)
    ]


def _need(load_kw: float = _LOAD_KW) -> float:
    return load_kw * _HOURS - _USABLE


def _allocate(*, hold: bool, load_kw: float = _LOAD_KW, carried: bool = True):
    intervals = _intervals(load_kw)
    return allocate_price_slots(
        intervals,
        build_energy_deadlines(
            intervals, _USABLE, usable_capacity_kwh=_USABLE + _ROOM
        ),
        _slots(),
        total_required_kwh=_need(load_kw),
        effective_power_kw=_POWER_KW,
        now=_START,
        horizon_end=_START + timedelta(hours=_HOURS),
        headroom_kwh=_ROOM,
        usable_initial_kwh=_USABLE,
        grid_carries_load_while_charging=carried,
        battery_holds_rest_of_slot=hold,
    )


def _held_night(plan, load_kw: float = _LOAD_KW) -> tuple[float, float]:
    """(lowest, last) stored energy when every booked slot holds the battery.

    A booked quarter stores its planned amount and the grid carries its load;
    every other quarter drains the battery by the load.
    """
    booked = {a.slot.start: a.planned_battery_kwh for a in plan.allocations}
    energy = lowest = _USABLE
    for interval in _intervals(load_kw):
        if interval.start in booked:
            energy += booked[interval.start]
        else:
            energy -= interval.consumption_kwh
        lowest = min(lowest, energy)
    return lowest, energy


def test_with_the_hold_the_night_ends_empty_instead_of_with_a_surplus():
    old = _allocate(hold=False)
    new = _allocate(hold=True)

    _, old_end = _held_night(old)
    lowest, new_end = _held_night(new)

    # Old sizing under the hold: every slot's untrimmed rest stays in the
    # battery until morning.
    assert old_end > 0.5
    assert new_end == pytest.approx(0.0, abs=0.05)
    assert lowest >= -0.05
    assert new.reason == "ok"
    assert new.allocated_kwh < old.allocated_kwh


def test_the_hold_trims_each_slot_by_its_whole_load():
    old = {a.slot.start: a.planned_battery_kwh for a in _allocate(hold=False).allocations}
    new = {a.slot.start: a.planned_battery_kwh for a in _allocate(hold=True).allocations}

    assert new.keys() == old.keys()
    for start, amount in new.items():
        # The rest of each slot's load (beyond the minutes it charges) comes
        # off too, never more than the whole quarter's load.
        assert amount < old[start]
        assert old[start] - amount <= _LOAD_KW / 4 + 1e-9


def test_a_slot_whose_load_covers_its_amount_stays_booked_with_a_minimum():
    # Nearly enough stored: the night lacks 0.1 kWh, less than the 0.3 kWh
    # load of the quarter that supplies it. Trimmed to nothing, the slot
    # would not be entered and the battery would cover that quarter itself.
    intervals = _intervals()
    usable = _LOAD_KW * _HOURS - 0.1

    def allocate(hold: bool):
        return allocate_price_slots(
            intervals,
            build_energy_deadlines(
                intervals, usable, usable_capacity_kwh=usable + _ROOM
            ),
            _slots(),
            total_required_kwh=0.1,
            effective_power_kw=_POWER_KW,
            now=_START,
            horizon_end=_START + timedelta(hours=_HOURS),
            headroom_kwh=_ROOM,
            usable_initial_kwh=usable,
            grid_carries_load_while_charging=True,
            battery_holds_rest_of_slot=hold,
        )

    plain = allocate(False)
    held = allocate(True)

    assert len(held.allocations) == 1
    assert [a.slot for a in held.allocations] == [a.slot for a in plain.allocations]
    assert held.allocations[0].planned_battery_kwh == pytest.approx(_SLOT_HOLD_MIN_KWH)
    assert plain.allocations[0].planned_battery_kwh > _SLOT_HOLD_MIN_KWH


def test_without_the_hold_the_plain_trim_is_unchanged():
    default = allocate_price_slots(
        _intervals(),
        build_energy_deadlines(
            _intervals(), _USABLE, usable_capacity_kwh=_USABLE + _ROOM
        ),
        _slots(),
        total_required_kwh=_need(),
        effective_power_kw=_POWER_KW,
        now=_START,
        horizon_end=_START + timedelta(hours=_HOURS),
        headroom_kwh=_ROOM,
        usable_initial_kwh=_USABLE,
        grid_carries_load_while_charging=True,
    )
    off = _allocate(hold=False)

    assert [a.planned_battery_kwh for a in default.allocations] == pytest.approx(
        [a.planned_battery_kwh for a in off.allocations]
    )


def test_the_hold_needs_the_grid_carrying_model():
    # A weekly full charge turns the carried-load model off; the hold flag
    # alone changes nothing then.
    assert [
        a.planned_battery_kwh for a in _allocate(hold=True, carried=False).allocations
    ] == pytest.approx(
        [a.planned_battery_kwh for a in _allocate(hold=False, carried=False).allocations]
    )


# A night plan through the real plan builder (PricingManager).
BUILDER_NOW = datetime(2026, 11, 11, 0, 5, 2)
HORIZON = datetime(2026, 11, 12, 7, 54, 44)
DAY = datetime(2026, 11, 11)


def _load_kw(hour):
    return 1.2 if hour < 7 else 1.1 if hour < 17 else 1.6 if hour < 22 else 1.2


def _builder_scenario():
    """A night plan over a 30-hour horizon: 7 kWh, 800 W battery, heat-pump load."""
    shape = [_load_kw(i // 4) / 4 for i in range(INTERVAL_COUNT)]

    def forecast_between(start, end, **_kw):
        total, t = 0.0, start
        while t < end:
            nxt = min(end, t.replace(minute=t.minute // 15 * 15, second=0, microsecond=0)
                      + timedelta(minutes=15))
            total += _load_kw(t.hour) * (nxt - t).total_seconds() / 3600
            t = nxt
        return ConsumptionForecast(
            total, list(shape), "profile", True,
            intervals_by_date={(DAY + timedelta(days=i)).date(): list(shape) for i in range(3)},
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
        PriceSlot(DAY + timedelta(minutes=15 * q), DAY + timedelta(minutes=15 * (q + 1)), price(q))
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
    manager.energy_horizon_end = lambda _now: HORIZON
    decision = {
        "should_charge": True,
        "planned_grid_charge_kwh": 5.53,
        "energy_deficit_kwh": 33.34,
        "avg_consumption_kwh": 39.63,
        "remaining_consumption_kwh": 39.63,
        "floor_deficit_kwh": 0.0,
        "energy_horizon_end": HORIZON,
        "total_available_kwh": 6.29,
    }
    return manager, controller, slots, decision


@pytest.mark.parametrize(
    ("mode", "expected"), [("dynamic_pricing", True), ("time_slot", False)]
)
def test_the_plan_builder_turns_the_hold_on_in_dynamic_pricing_only(mode, expected):
    # Only Dynamic Pricing holds the battery in a booked slot.
    manager, controller, slots, decision = _builder_scenario()
    controller.predictive_charging_mode = mode
    seen = []
    evaluate = manager.evaluate_chronological_projection

    def capture(request):
        seen.append(request.battery_holds_rest_of_slot)
        return evaluate(request)

    manager.evaluate_chronological_projection = capture
    manager._build_chronological_plan(
        now=BUILDER_NOW, slots=slots, decision_data=decision, price_ceiling=None
    )

    assert seen and set(seen) == {expected}


def test_the_request_passes_the_hold_through():
    intervals = tuple(_intervals())

    def evaluate(hold: bool):
        return evaluate_chronological_request(
            ChronologicalEvaluationRequest(
                now=_START,
                horizon_end=_START + timedelta(hours=_HOURS),
                intervals=intervals,
                price_slots=tuple(_slots()),
                total_required_kwh=_need(),
                effective_power_kw=_POWER_KW,
                headroom_kwh=_ROOM,
                usable_initial_kwh=_USABLE,
                grid_carries_load_while_charging=True,
                battery_holds_rest_of_slot=hold,
            )
        ).plan.allocated_kwh

    assert evaluate(True) < evaluate(False)
