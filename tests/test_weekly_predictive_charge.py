"""Weekly full charge feeds the predictive energy balance (#404).

The balance answers "will I run out of battery", never "is the battery full",
so a weekly 100% day produced no deficit and nothing charged unless the sun
happened to cover it. The weekly gap now enters the same balance the guaranteed
floor uses, and the charge ceiling rises to 100% for the whole planning chain.

``_should_activate_grid_charging`` touches few attributes, so it is exercised
unbound on a stub controller (no Home Assistant runtime needed).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController


class _Coord:
    def __init__(self, soc, capacity_kwh, min_soc=12, max_soc=90):
        self.name = f"battery_{soc:.0f}"
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}


def _consumption(value):
    async def _f():
        return value
    return _f


def _ctrl(coords, *, weekly, solar="5.0", consumption=2.0):
    return SimpleNamespace(
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        coordinators=list(coords),
        _weekly_charge_mgr=SimpleNamespace(is_active=lambda: weekly),
        _predictive_safety_margin_kwh=0.0,
        _predictive_grid_charge_margin_pct=0.0,
        _predictive_min_soc_floor=0.0,
        _predictive_min_soc_floor_enabled=False,
        _daily_consumption_history=[],
        solar_forecast_sensor="sensor.solar" if solar is not None else None,
        hass=SimpleNamespace(
            states=SimpleNamespace(
                get=lambda _e: None if solar is None else SimpleNamespace(state=solar)
            )
        ),
        _consumption_tracker=SimpleNamespace(
            get_dynamic_base_consumption=_consumption(consumption)
        ),
    )


def _run(ctrl):
    return asyncio.run(ChargeDischargeController._should_activate_grid_charging(ctrl))


# 10 kWh pack at 50%, min 12%, max 90%: usable 3.8 kWh, gap to 100% is 5.0 kWh.
# Consumption 2.0 kWh, so the day is solar-positive and the plain balance says
# "no charge" in every case below.


def test_weekly_day_charges_the_gap_the_sun_cannot_cover():
    # Solar 5.0 - consumption 2.0 = 3.0 kWh surplus into the pack.
    # Weekly needs 5.0 kWh → grid buys the remaining 2.0 kWh.
    result = _run(_ctrl([_Coord(50.0, 10.0)], weekly=True, solar="5.0"))

    assert result["should_charge"] is True
    assert abs(result["energy_deficit_kwh"] - 2.0) < 0.05
    assert result["weekly_full_charge_active"] is True
    assert "Weekly full charge" in result["reason"]


def test_weekly_day_buys_nothing_when_solar_covers_the_gap():
    # Solar 8.0 - consumption 2.0 = 6.0 kWh surplus ≥ the 5.0 kWh gap.
    result = _run(_ctrl([_Coord(50.0, 10.0)], weekly=True, solar="8.0"))

    assert result["should_charge"] is False


def test_no_weekly_charge_on_an_ordinary_day():
    # Same numbers, weekly inactive → the ceiling stays at max_soc, no deficit.
    result = _run(_ctrl([_Coord(50.0, 10.0)], weekly=False, solar="5.0"))

    assert result["should_charge"] is False
    assert result["weekly_full_charge_active"] is False


def test_weekly_headroom_reaches_100_not_max_soc():
    # planned_grid_charge_kwh is clipped to the headroom. At max_soc=90 the clip
    # would be 4.0 kWh; the weekly ceiling of 100% must not clip the 5.0 kWh gap.
    result = _run(_ctrl([_Coord(50.0, 10.0)], weekly=True, solar=None))

    assert abs(result["energy_deficit_kwh"] - 5.0) < 0.05
    assert abs(result["planned_grid_charge_kwh"] - 5.0) < 0.05


def test_conservative_branch_takes_the_whole_gap():
    # No solar forecast → that branch assumes zero solar, so does the weekly gap.
    result = _run(_ctrl([_Coord(80.0, 10.0)], weekly=True, solar=None))

    assert result["should_charge"] is True
    assert abs(result["energy_deficit_kwh"] - 2.0) < 0.05


def _target_ctrl(coords, *, weekly, deficit_kwh):
    return SimpleNamespace(
        coordinators=list(coords),
        _weekly_charge_mgr=SimpleNamespace(is_active=lambda: weekly),
        _last_decision_data={"energy_deficit_kwh": deficit_kwh},
        _predictive_grid_charge_margin_pct=0.0,
    )


def test_deficit_targets_may_reach_100_on_the_weekly_day():
    coord = _Coord(50.0, 10.0)
    targets = ChargeDischargeController._compute_deficit_target_soc(
        _target_ctrl([coord], weekly=True, deficit_kwh=5.0)
    )

    assert abs(targets[coord] - 100.0) < 0.5


def test_deficit_targets_still_stop_at_max_soc_otherwise():
    coord = _Coord(50.0, 10.0)
    targets = ChargeDischargeController._compute_deficit_target_soc(
        _target_ctrl([coord], weekly=False, deficit_kwh=5.0)
    )

    assert abs(targets[coord] - 90.0) < 0.5
