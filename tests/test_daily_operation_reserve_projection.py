"""The Daily Operation projection keeps what the discharge reserve holds.

The reserve keeps stored energy for dearer slots ahead and blocks discharge
until they come. Without it the projected discharge and SOC spent that energy
in the cheaper hours first, which the battery does not do.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.pricing.daily_timeline import (
    BatteryProjectionInput,
    ProjectionIntervalInput,
    simulate_battery_projection,
)
from custom_components.omnibattery.tracking.daily_projection import (
    DailyOperationProjectionRequest,
    build_daily_operation_projection,
)

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 6, 13, 0, tzinfo=BERLIN)
QUARTER = timedelta(minutes=15)


def _intervals(count: int, consumption: float = 0.5) -> list[ProjectionIntervalInput]:
    return [
        ProjectionIntervalInput(
            NOW + QUARTER * i, NOW + QUARTER * (i + 1), consumption_kwh=consumption
        )
        for i in range(count)
    ]


def _battery(stored: float = 2.0) -> BatteryProjectionInput:
    return BatteryProjectionInput("a", stored, 10.0, 0.0, 100.0, 4000.0, 4000.0)


def _discharged(result) -> list[float]:
    return [round(flow.battery_to_home_kwh, 6) for flow in result.intervals]


def test_without_a_reserve_the_battery_covers_the_house_at_once():
    result = simulate_battery_projection(_intervals(6), [_battery()])

    assert _discharged(result) == [0.5, 0.5, 0.5, 0.5, 0.0, 0.0]


def test_the_reserve_keeps_its_energy_until_the_claimed_slot():
    """1.5 kWh claimed for the 5th quarter on: 0.5 kWh above it is spent at
    once, the rest waits for the claimed slots."""
    floor = [1.5, 1.5, 1.5, 1.5, 1.0, 0.5]
    result = simulate_battery_projection(
        _intervals(6), [_battery()], discharge_reserve_kwh=floor
    )

    assert _discharged(result) == [0.5, 0.0, 0.0, 0.0, 0.5, 0.5]
    assert result.intervals[3].stored_energy_end_kwh == pytest.approx(1.5)


def test_the_reserve_is_a_fleet_floor_above_each_battery_s_minimum():
    batteries = [
        BatteryProjectionInput("a", 1.5, 5.0, 10.0, 100.0, 4000.0, 4000.0),
        BatteryProjectionInput("b", 1.5, 5.0, 10.0, 100.0, 4000.0, 4000.0),
    ]
    # 0.5 kWh floor each, 2.0 kWh usable; the reserve keeps 1.2 of it.
    result = simulate_battery_projection(
        _intervals(1, consumption=2.0), batteries, discharge_reserve_kwh=[1.2]
    )

    assert result.intervals[0].battery_to_home_kwh == pytest.approx(0.8)
    assert result.intervals[0].grid_to_home_kwh == pytest.approx(1.2)


def _request(claims=()):
    return DailyOperationProjectionRequest(
        now=NOW,
        plan_intervals=tuple(_intervals(16)),
        allocations=(),
        battery_inputs=(_battery(),),
        mode="dynamic_pricing",
        decision_data={},
        reserve_claims=claims,
    )


def test_the_projection_holds_the_soc_until_the_evening_claims():
    """13:00 now, 1.5 kWh claimed for 16:30 to 16:45 and 0.5 for 16:45."""
    claims = (
        (NOW + timedelta(hours=3, minutes=30), 1.0),
        (NOW + timedelta(hours=3, minutes=45), 0.5),
    )
    held = build_daily_operation_projection(_request(claims))
    plain = build_daily_operation_projection(_request())

    held_out = [item["battery_to_home_kwh"] for item in held["intervals"]]
    plain_out = [item["battery_to_home_kwh"] for item in plain["intervals"]]
    assert plain_out[:4] == pytest.approx([0.5] * 4)
    # Only the 0.5 kWh nobody claimed goes now; the battery then waits.
    assert held_out[0] == pytest.approx(0.5)
    assert held_out[1:14] == pytest.approx([0.0] * 13)
    assert held_out[14:16] == pytest.approx([0.5, 0.5])
    assert held["intervals"][13]["stored_energy_end_kwh"] == pytest.approx(1.5)


def _controller(status):
    return SimpleNamespace(
        _discharge_reserve_mgr=SimpleNamespace(get_status=lambda: status),
        _daily_operation_float=ChargeDischargeController._daily_operation_float,
    )


def test_the_controller_reads_the_live_claims_net_of_pv_and_capped_at_the_reserve():
    status = {
        "state": "reserving",
        "reserve_kwh": 1.5,
        "claims": [
            {"start": "2026-11-06T16:30:00", "claimed_kwh": 1.0, "pv_credit_kwh": 0.0},
            {"start": "2026-11-06T17:00:00", "claimed_kwh": 1.2, "pv_credit_kwh": 0.2},
            {"start": "2026-11-06T17:15:00", "claimed_kwh": 0.3, "pv_credit_kwh": 0.3},
        ],
    }

    claims = ChargeDischargeController._daily_operation_reserve_claims(
        _controller(status), NOW
    )

    assert [start for start, _kwh in claims] == [
        datetime(2026, 11, 6, 16, 30, tzinfo=BERLIN),
        datetime(2026, 11, 6, 17, 0, tzinfo=BERLIN),
    ]
    # 1.0 + 1.0 net of PV, scaled down to the 1.5 kWh the reserve holds.
    assert [kwh for _start, kwh in claims] == pytest.approx([0.75, 0.75])


@pytest.mark.parametrize("state", ["released", "disabled"])
def test_a_reserve_that_holds_nothing_keeps_nothing(state):
    status = {
        "state": state,
        "reserve_kwh": 0.0,
        "claims": [{"start": "2026-11-06T16:30:00", "claimed_kwh": 1.0}],
    }

    assert (
        ChargeDischargeController._daily_operation_reserve_claims(
            _controller(status), NOW
        )
        == ()
    )


def test_no_reserve_manager_keeps_nothing():
    assert (
        ChargeDischargeController._daily_operation_reserve_claims(
            SimpleNamespace(), NOW
        )
        == ()
    )


# ----------------------------------------------------------------------
# Energy that only arrives later: the reserve is asked at each slot
# ----------------------------------------------------------------------

from custom_components.omnibattery.const import (  # noqa: E402
    PREDICTIVE_MODE_DYNAMIC_PRICING,
)
from custom_components.omnibattery.control.discharge_reserve import (  # noqa: E402
    DischargeReserveManager,
)
from custom_components.omnibattery.pricing.discharge_reserve import (  # noqa: E402
    ReservePlan,
    ReserveSlot,
)

DAY = datetime(2026, 11, 7)


def _reserve_slot(hour, price, demand=0.0, surplus=0.0):
    start = DAY + timedelta(hours=hour)
    return ReserveSlot(start, start + timedelta(hours=1), price, demand, surplus)


def _midday_plan():
    """Sun at 13:00, a cheap 14:00 and a dear evening from 15:00 on."""
    return ReservePlan(
        slots=[
            _reserve_slot(13, 0.47, surplus=0.4),
            _reserve_slot(14, 0.47, demand=0.3),
            _reserve_slot(15, 0.60, demand=0.5),
            _reserve_slot(16, 0.60, demand=0.5),
            _reserve_slot(17, 0.60, demand=0.5),
        ]
    )


def _reserve_manager(plan=None, **overrides):
    controller = SimpleNamespace(
        discharge_reserve_enabled=True,
        discharge_reserve_min_saving=0.02,
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        manual_mode_enabled=False,
        _manual_slot_owned=None,
        coordinators=[],
        _curtailment_runtime_status=None,
        _capacity_protection_active=False,
    )
    for key, value in overrides.items():
        setattr(controller, key, value)
    manager = DischargeReserveManager(SimpleNamespace(), controller)
    manager._now = lambda: DAY + timedelta(hours=13)
    manager._plan = plan if plan is not None else _midday_plan()
    manager._plan_date = DAY.date()
    return manager


def _sunny_midday(count=20):
    """13:00 on, 15-minute steps: 0.1 kWh of sun in each of the first four,
    then the house alone."""
    start = DAY.replace(tzinfo=BERLIN) + timedelta(hours=13)
    return [
        ProjectionIntervalInput(
            start + QUARTER * i,
            start + QUARTER * (i + 1),
            solar_kwh=0.1 if i < 4 else 0.0,
            consumption_kwh=0.0 if i < 4 else 0.1,
        )
        for i in range(count)
    ]


def test_the_sun_stored_at_midday_waits_for_the_dear_evening():
    """The battery is empty now, so the live reserve holds nothing. The
    reserve asked at 14:00 keeps what the sun put in for 15:00 on."""
    fn = _reserve_manager().projected_reserve_fn()
    battery = BatteryProjectionInput("a", 0.0, 10.0, 0.0, 100.0, 4000.0, 4000.0)

    plain = simulate_battery_projection(_sunny_midday(), [battery])
    held = simulate_battery_projection(
        _sunny_midday(), [battery], discharge_reserve_fn=fn
    )

    # 14:00 to 15:00: spent at once without the reserve, kept with it.
    assert sum(_discharged(plain)[4:8]) > 0.3
    assert _discharged(held)[4:8] == [0.0] * 4
    # From 15:00 the dear slots get it.
    assert sum(_discharged(held)[8:]) == pytest.approx(sum(_discharged(plain)))


def test_nothing_is_held_when_no_later_slot_is_dearer():
    plan = ReservePlan(
        slots=[
            _reserve_slot(13, 0.47, surplus=0.4),
            _reserve_slot(14, 0.47, demand=0.3),
            _reserve_slot(15, 0.48, demand=0.5),
        ]
    )
    fn = _reserve_manager(plan).projected_reserve_fn()

    assert fn(DAY + timedelta(hours=14), 0.4, 9.6) == 0.0


def test_asking_the_reserve_leaves_its_status_alone():
    plan = _midday_plan()
    plan.claimed_kwh = 1.25
    plan.threshold_price = 0.5
    fn = _reserve_manager(plan).projected_reserve_fn()

    assert fn(DAY.replace(tzinfo=BERLIN) + timedelta(hours=14), 0.4, 9.6) > 0.0
    assert plan.claimed_kwh == 1.25
    assert plan.threshold_price == 0.5


def test_outside_the_plan_the_projection_keeps_its_claims():
    fn = _reserve_manager().projected_reserve_fn()

    assert fn(DAY + timedelta(hours=20), 0.4, 9.6) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"discharge_reserve_enabled": False},
        {"manual_mode_enabled": True},
        {"_capacity_protection_active": True},
    ],
)
def test_a_reserve_that_would_not_apply_asks_nothing(overrides):
    assert _reserve_manager(**overrides).projected_reserve_fn() is None


def test_the_request_hands_the_reserve_to_the_projection():
    fn = _reserve_manager().projected_reserve_fn()
    request = DailyOperationProjectionRequest(
        now=DAY.replace(tzinfo=BERLIN) + timedelta(hours=13),
        plan_intervals=tuple(_sunny_midday()),
        allocations=(),
        battery_inputs=(
            BatteryProjectionInput("a", 0.0, 10.0, 0.0, 100.0, 4000.0, 4000.0),
        ),
        mode="dynamic_pricing",
        decision_data={},
        reserve_floor=fn,
    )

    projection = build_daily_operation_projection(request)

    out = [item["battery_to_home_kwh"] for item in projection["intervals"]]
    assert out[4:8] == pytest.approx([0.0] * 4)
    assert sum(out[8:]) > 0.3
