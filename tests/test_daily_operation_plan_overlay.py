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


# (start, end, solar, house, delta at the end, margin sun): a discharge, a grid
# charge, a solar charge.
ROWS = (
    (T0, datetime(2026, 11, 10, 0, 15), 0.0, 0.3, -0.25, 0.0),
    (datetime(2026, 11, 10, 0, 15), datetime(2026, 11, 10, 0, 30), 0.0, 0.1, 0.65, 0.0),
    (datetime(2026, 11, 10, 0, 30), datetime(2026, 11, 10, 0, 45), 1.0, 0.2, 1.05, 0.0),
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


def test_mid_quarter_starts_at_the_live_level_and_scales_the_rest():
    later = T0 + timedelta(minutes=30)  # 00:30 quarter, 5 of 15 minutes gone
    dto = _dto()
    dto["intervals"] = [_item(later, datetime(2026, 11, 10, 0, 45))]
    dto["extended_intervals"] = []
    rows = ROWS
    out = overlay_plan_rows(dto, rows, start_kwh=5.0, capacity_kwh=10.0, efficiency=0.8, now=later)
    (x,) = out["intervals"]
    # Live 5.0 kWh plus the 10 minutes left of the quarter's +0.4.
    assert x["soc_end_pct"] == pytest.approx(50.0 + 4.0 * 10 / 15)
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


def test_a_room_discharge_draws_its_drop_and_only_the_house_flow():
    """Smart Pre-Discharge in the plan: the line falls by the room the plan
    makes; the battery covers the house, the exported rest has no field."""
    rows = ((T0, datetime(2026, 11, 10, 0, 15), 0.0, 0.1, -0.6, 0.0),) + ROWS[1:]
    dto = overlay_plan_rows(_dto(), rows, start_kwh=5.0, capacity_kwh=10.0, efficiency=0.8, now=T0)
    a = dto["intervals"][0]
    assert a["soc_end_pct"] == pytest.approx(44.0)
    assert a["battery_to_home_kwh"] == pytest.approx(0.1) and a["grid_to_home_kwh"] == 0.0


def _day(now, sun=0.0):
    """18 Nov 2026: plan evaluated at 11:15 at 55 % of 10.4 kWh that drains
    0.07 kWh a quarter until 18:00; the live battery is at 81.5 % at 15:40."""
    eval_at = datetime(2026, 11, 18, 11, 15)
    rows, delta = [], 0.0
    for k in range(27):
        start = eval_at + Q * k
        delta -= 0.07
        rows.append((start, start + Q, 0.0, 0.2, delta, sun))
    items = [_item(r[0], r[1]) for r in rows if r[1] > now]
    return rows, {"intervals": items, "extended_intervals": []}


def test_a_plan_evaluated_hours_ago_starts_at_the_live_level():
    """The panel drew the plan's 40 % right after a live 81.5 %."""
    now = datetime(2026, 11, 18, 15, 40)
    rows, dto = _day(now)
    out = overlay_plan_rows(dto, rows, start_kwh=0.815 * 10.4, capacity_kwh=10.4,
                            efficiency=0.9, now=now, floor_kwh=0.0, top_kwh=10.4)
    first = out["intervals"][0]["soc_end_pct"]
    assert 81.5 - 0.07 / 10.4 * 100 <= first <= 81.5
    socs = [x["soc_end_pct"] for x in out["intervals"]]
    assert socs[-1] == pytest.approx(socs[0] - 0.07 * (len(socs) - 1) / 10.4 * 100)


def test_the_sun_the_margin_took_off_lifts_the_line_up_to_the_top():
    now = datetime(2026, 11, 18, 11, 15)
    rows, dto = _day(now, sun=0.4)
    out = overlay_plan_rows(dto, rows, start_kwh=5.72, capacity_kwh=10.4,
                            efficiency=0.9, now=now, floor_kwh=0.5, top_kwh=10.0)
    socs = [x["soc_end_pct"] for x in out["intervals"]]
    assert all(b >= a for a, b in zip(socs, socs[1:])) and socs[-1] == pytest.approx(100.0 * 10.0 / 10.4)


def test_the_replay_stops_at_the_floor_and_leaves_a_level_past_the_top():
    now = datetime(2026, 11, 18, 11, 15)
    rows, dto = _day(now)
    out = overlay_plan_rows(dto, rows, start_kwh=0.6, capacity_kwh=10.4,
                            efficiency=0.9, now=now, floor_kwh=0.5, top_kwh=10.0)
    assert out["intervals"][-1]["stored_energy_end_kwh"] == pytest.approx(0.5)
    rows, dto = _day(now, sun=0.07)  # flat plan
    out = overlay_plan_rows(dto, rows, start_kwh=10.3, capacity_kwh=10.4,
                            efficiency=0.9, now=now, floor_kwh=0.5, top_kwh=10.0)
    assert out["intervals"][0]["stored_energy_end_kwh"] == pytest.approx(10.3)


def test_margin_sun_spares_the_discharge_then_charges_the_surplus():
    from types import SimpleNamespace as I

    from custom_components.omnibattery.tracking.daily_projection import margin_sun_kwh

    # Plan: margin left 0.05 of sun under a 0.2 house; battery gave 0.15 (0.1875 stored at 0.8).
    planned, shown = I(consumption_kwh=0.2, solar_kwh=0.05), I(consumption_kwh=0.2, solar_kwh=0.5)
    # 0.15 spared (0.1875 stored) + 0.3 new surplus (0.24 stored).
    assert margin_sun_kwh(planned, shown, -0.1875, 0.8) == pytest.approx(0.1875 + 0.24)
    # No margin: nothing to add.
    assert margin_sun_kwh(shown, shown, 0.24, 0.8) == 0.0
    # Battery idle (drain 0): only the larger surplus counts.
    assert margin_sun_kwh(I(consumption_kwh=0.0, solar_kwh=0.1), I(consumption_kwh=0.0, solar_kwh=0.3),
                          0.08, 0.8) == pytest.approx(0.16)
    # A sale (1.0 out under a 0.15 house): the sun spares the house's share only.
    assert margin_sun_kwh(planned, shown, -1.0 / 0.8, 0.8) == pytest.approx(0.15 / 0.8 + 0.24)


def test_the_live_battery_snapshot_anchors_the_line():
    from types import SimpleNamespace as B

    from custom_components.omnibattery import ChargeDischargeController as CDC

    live = [B(stored_kwh=8.0, capacity_kwh=10.0)]
    drawn = CDC._daily_operation_plan_overlay(_controller(), _dto(), T0, live)
    assert drawn["intervals"][0]["soc_end_pct"] == pytest.approx(77.5)
    # Another fleet than the plan's keeps the plan's anchor.
    live = [B(stored_kwh=4.0, capacity_kwh=5.0)]
    drawn = CDC._daily_operation_plan_overlay(_controller(), _dto(), T0, live)
    assert drawn["intervals"][0]["soc_end_pct"] == pytest.approx(47.5)
