"""Step 11: with the optimizer the panel draws the published plan's own quarters."""
from datetime import datetime, timedelta

import pytest

from custom_components.omnibattery.pricing.daily_timeline import (
    GRID_CHARGE_NOT_APPLICABLE,
    GRID_CHARGE_SCHEDULED,
)
from custom_components.omnibattery.tracking.daily_projection import overlay_plan_rows

T0 = datetime(2026, 11, 10, 0, 5)
Q = timedelta(minutes=15)


def _item(start, end):
    return {"start": start, "end": end, "index": 0, "soc_end_pct": 0.0, "context_mask": 8,
            "grid_charge_decision": GRID_CHARGE_NOT_APPLICABLE, "action_mask": 0}


def _dto():
    ends = [datetime(2026, 11, 10, 0, 15) + Q * k for k in range(3)]
    starts = [T0] + ends[:2]
    items = [_item(s, e) for s, e in zip(starts, ends)]
    return {"intervals": items[:2], "extended_intervals": items[2:] + [_item(ends[2], ends[2] + Q)]}


# (start, end, solar, house, delta at the end): a discharge, a grid charge, a solar charge.
ROWS = (
    (T0, datetime(2026, 11, 10, 0, 15), 0.0, 0.3, -0.25),
    (datetime(2026, 11, 10, 0, 15), datetime(2026, 11, 10, 0, 30), 0.0, 0.1, 0.65),
    (datetime(2026, 11, 10, 0, 30), datetime(2026, 11, 10, 0, 45), 1.0, 0.2, 1.05),
)


def test_draws_the_plan_soc_and_flows_that_add_up():
    dto = overlay_plan_rows(_dto(), ROWS, start_kwh=5.0, capacity_kwh=10.0, efficiency=0.8, now=T0)
    a, b = dto["intervals"]
    (c,) = dto["extended_intervals"]  # the quarter past the plan's last point is dropped
    assert [x["soc_end_pct"] for x in (a, b, c)] == pytest.approx([47.5, 56.5, 60.5])
    # Down 0.25 stored -> 0.2 to the house, the rest from the grid.
    assert a["battery_to_home_kwh"] == pytest.approx(0.2)
    assert a["grid_to_home_kwh"] == pytest.approx(0.1)
    assert a["action_mask"] == 4 and a["context_mask"] == 8
    # Up 0.9 stored -> 1.125 bought; scheduled.
    assert b["grid_to_battery_kwh"] == pytest.approx(1.125)
    assert b["grid_charge_decision"] == GRID_CHARGE_SCHEDULED and b["action_mask"] == 2
    # Up 0.4 stored -> 0.5 from the 0.8 kWh surplus, nothing from the grid.
    assert c["solar_to_battery_kwh"] == pytest.approx(0.5) and c["grid_to_battery_kwh"] == 0.0
    assert c["solar_to_home_kwh"] == pytest.approx(0.2)
    assert c["charge_power_w"] == pytest.approx(2000.0)
    for x in (a, b, c):
        assert x["solar_to_home_kwh"] + x["battery_to_home_kwh"] + x["grid_to_home_kwh"] == pytest.approx(x["consumption_kwh"])
        assert min(v for k, v in x.items() if k.endswith("_kwh")) >= 0.0


def test_mid_quarter_keeps_the_plan_value_and_scales_the_rest():
    later = T0 + timedelta(minutes=30)  # 00:30 quarter, 5 of 15 minutes gone
    dto = _dto()
    dto["intervals"] = [_item(later, datetime(2026, 11, 10, 0, 45))]
    dto["extended_intervals"] = []
    rows = ROWS
    out = overlay_plan_rows(dto, rows, start_kwh=5.0, capacity_kwh=10.0, efficiency=0.8, now=later)
    (x,) = out["intervals"]
    assert x["soc_end_pct"] == pytest.approx(60.5)
    assert x["consumption_kwh"] == pytest.approx(0.2 * 10 / 15)
    assert x["consumption_forecast_kwh"] == pytest.approx(0.2)
    assert x["solar_to_battery_kwh"] == pytest.approx(0.5 * 10 / 15)


def _controller(**kw):
    from types import SimpleNamespace

    from custom_components.omnibattery.const import PREDICTIVE_MODE_DYNAMIC_PRICING

    base = dict(
        _dp_eval_rows=ROWS, _dp_eval_anchor=(5.0, 10.0),
        _dp_eval_efficiency=0.8, predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        _last_decision_data={"planner": "optimizer"},
    )
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize("change", [
    {"predictive_charging_mode": "time_slot"},
    {"predictive_charging_enabled": False},
    {"_last_decision_data": {"planner": "allocator"}},
    {"_dp_eval_rows": None},
    {"_dp_eval_anchor": None},
])
def test_old_projection_unless_the_optimizer_plan_owns_the_control(change):
    from custom_components.omnibattery import ChargeDischargeController as CDC

    dto = _dto()
    assert CDC._daily_operation_plan_overlay(_controller(**change), dto, T0) is dto
    drawn = CDC._daily_operation_plan_overlay(_controller(), _dto(), T0)
    assert drawn["intervals"][0]["soc_end_pct"] == pytest.approx(47.5)


def test_aware_panel_clock_over_naive_plan_times():
    """In Home Assistant the engine's plan is on naive local wall clock
    (``datetime.now()``) and the panel's on an aware one (``dt_util.now()``)."""
    from zoneinfo import ZoneInfo

    from custom_components.omnibattery import ChargeDischargeController as CDC

    zone = ZoneInfo("Europe/Madrid")
    dto = _dto()
    for key in ("intervals", "extended_intervals"):
        for item in dto[key]:
            item["start"], item["end"] = (item[k].replace(tzinfo=zone) for k in ("start", "end"))
    drawn = CDC._daily_operation_plan_overlay(_controller(), dto, T0.replace(tzinfo=zone))
    assert drawn is not dto
    assert [x["soc_end_pct"] for x in drawn["intervals"]] == pytest.approx([47.5, 56.5])
    assert all(x["end"].tzinfo is zone for x in drawn["intervals"] + drawn["extended_intervals"])
