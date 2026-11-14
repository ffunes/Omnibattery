"""Forecast history: day-ahead, start-of-day and measured values per day."""
from __future__ import annotations

import copy
import csv
import io
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery.tracking.daily_timeline import (
    ACTION_GRID_CHARGE,
    ACTION_SOLAR_CHARGE,
    DailyOperationTimelineManager,
)
from custom_components.omnibattery.tracking.forecast_history import (
    MAX_REVISIONS_PER_HORIZON,
    ForecastHistoryManager,
    view_from_timeline_payload,
)

AMS = ZoneInfo("Europe/Amsterdam")


class FakeStore:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data)
        self.writes: list[dict] = []

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.data = copy.deepcopy(data)
        self.writes.append(copy.deepcopy(data))


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _setup(clock: Clock, *, timeline_store=None, history_store=None, startup_settle_s=0):
    """``startup_settle_s``: 0 records from the first observation on, as most
    tests want; ``None`` keeps the wait after a restart."""
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Amsterdam"))
    entry = SimpleNamespace(entry_id="entry", data={}, options={})
    timeline = DailyOperationTimelineManager(
        hass,
        entry,
        SimpleNamespace(pricing_mode="dynamic_pricing"),
        store=timeline_store or FakeStore(),
        now_provider=clock,
        debounce_seconds=0.0,
    )
    history = ForecastHistoryManager(
        hass, entry, store=history_store or FakeStore(), startup_settle_s=startup_settle_s
    )
    history.attach(timeline)
    return timeline, history


def _plan(day: datetime, *, solar=0.25, tomorrow_solar=0.3, extension=96, grid_index=None):
    """A projection with solar in 08:00-16:00 today and tomorrow."""
    intervals = [
        {"index": i, "solar_kwh": solar if 32 <= i < 64 else 0.0, "consumption_kwh": 0.1}
        for i in range(96)
    ]
    if grid_index is not None:
        intervals[grid_index]["grid_to_battery_kwh"] = 0.5
        intervals[grid_index]["action_mask"] = ACTION_GRID_CHARGE
    start = datetime.combine(day.date() + timedelta(days=1), datetime.min.time(), tzinfo=AMS)
    extended = [
        {
            "extension_index": i,
            "start": (start + timedelta(minutes=15 * i)).isoformat(),
            "end": (start + timedelta(minutes=15 * (i + 1))).isoformat(),
            "solar_kwh": tomorrow_solar if 32 <= i < 64 else 0.0,
            "consumption_kwh": 0.12,
            "grid_to_battery_kwh": 0.4 if i == 8 else 0.0,
            "soc_end_pct": 50.0,
        }
        for i in range(extension)
    ]
    return {
        "intervals": intervals,
        "extended_intervals": extended,
        "extended_horizon": {"interval_count": max(48, extension)},
    }


def _rebuild(timeline, history, clock, plan, decision=None):
    timeline.rebuild_future_projection(plan, now=clock.value, mode="dynamic_pricing")
    return history.observe(timeline, decision=decision, now=clock.value)


def test_view_reads_actuals_plan_and_extension_from_the_timeline_payload():
    clock = Clock(datetime(2026, 11, 10, 10, 7, tzinfo=AMS))
    timeline, _history = _setup(clock)
    timeline.rebuild_future_projection(_plan(clock.value, extension=48), now=clock.value)

    view = view_from_timeline_payload(timeline.forecast_history_payload())

    assert view["date"].isoformat() == "2026-11-10"
    assert view["extension_coverage"] == 48
    assert view["planned"]["solar"][40] == pytest.approx(0.25)
    assert view["extension"]["grid_charge"][8] == pytest.approx(0.4)
    assert view["extension"]["solar"][60] is None  # beyond the horizon


def test_day_ahead_plan_is_frozen_at_rollover_and_compared_with_actuals():
    clock = Clock(datetime(2026, 11, 10, 21, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value, tomorrow_solar=0.3))

    # Next day: measure 0.2 kWh solar per quarter instead of the forecast 0.3.
    clock.value = datetime(2026, 11, 11, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)
    record = history._days["2026-11-11"]
    assert record["day_ahead"]["coverage"] == 96
    assert record["day_ahead"]["totals"]["solar"] == pytest.approx(9.6)
    assert history._days["2026-11-10"]["finalized"] is True

    solar = [0.2 if 32 <= i < 64 else 0.0 for i in range(96)]
    for index in range(96):
        clock.value = datetime(2026, 11, 11, 0, 1, tzinfo=AMS) + timedelta(minutes=15 * index)
        timeline.refresh_actual_partial(
            solar_capture={"interval_energy_kwh": solar, "interval_coverage_s": [900.0] * 96},
            now=clock.value,
        )
    clock.value = datetime(2026, 11, 12, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)

    summary = next(d for d in history.list_days()["days"] if d["date"] == "2026-11-11")
    compare = summary["day_ahead"]["compare"]["solar"]
    assert summary["finalized"] is True
    assert compare["forecast"] == pytest.approx(9.6)
    assert compare["actual"] == pytest.approx(6.4)
    assert compare["error"] == pytest.approx(3.2)
    assert compare["error_pct"] == pytest.approx(50.0)


def test_partial_day_ahead_horizon_compares_only_the_covered_quarters():
    clock = Clock(datetime(2026, 11, 10, 21, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value, extension=48))
    clock.value = datetime(2026, 11, 11, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)
    consumption = [0.1] * 96
    clock.value = datetime(2026, 11, 12, 0, 1, tzinfo=AMS)
    timeline._actual_consumption_kwh = list(consumption)
    timeline.refresh_actual_partial(now=clock.value)

    summary = next(d for d in history.list_days()["days"] if d["date"] == "2026-11-11")
    compare = summary["day_ahead"]["compare"]["consumption"]
    assert summary["day_ahead"]["coverage"] == 48
    assert compare["intervals"] == 48
    assert compare["forecast"] == pytest.approx(48 * 0.12)
    assert compare["actual"] == pytest.approx(48 * 0.1)


def test_start_of_day_plan_is_captured_once_after_the_daily_run():
    plan = {"planning_horizon_reason": "daily_evaluation"}
    clock = Clock(datetime(2026, 11, 10, 0, 2, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value), plan)
    assert "start_of_day" not in history._days["2026-11-10"]

    clock.value = datetime(2026, 11, 10, 0, 7, tzinfo=AMS)
    _rebuild(timeline, history, clock, _plan(clock.value, solar=0.2), plan)
    captured = history._days["2026-11-10"]["start_of_day"]
    assert captured["captured_at"].startswith("2026-11-10T00:07")
    assert captured["totals"]["solar"] == pytest.approx(6.4)
    assert captured["coverage"] == 96

    clock.value = datetime(2026, 11, 10, 9, 0, tzinfo=AMS)
    _rebuild(timeline, history, clock, _plan(clock.value, solar=0.4), plan)
    assert history._days["2026-11-10"]["start_of_day"]["totals"]["solar"] == pytest.approx(6.4)


def test_revisions_record_material_changes_with_minimum_spacing():
    clock = Clock(datetime(2026, 11, 10, 14, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value), {"planning_horizon_reason": "extended"})
    # Same plan a minute later: nothing new.
    clock.value += timedelta(minutes=1)
    _rebuild(timeline, history, clock, _plan(clock.value), {"planning_horizon_reason": "extended"})
    # A big change five minutes later is still throttled...
    clock.value += timedelta(minutes=5)
    _rebuild(timeline, history, clock, _plan(clock.value, tomorrow_solar=0.1), {"planning_horizon_reason": "extended"})
    # ...but a horizon change is structural and always recorded.
    clock.value += timedelta(minutes=1)
    _rebuild(timeline, history, clock, _plan(clock.value, extension=48), {"planning_horizon_reason": "no_tomorrow_prices"})

    day_ahead = [r for r in history._days["2026-11-11"]["revisions"] if r["h"] == "day_ahead"]
    assert [r["cov"] for r in day_ahead] == [96, 48]
    assert day_ahead[-1]["reason"] == "no_tomorrow_prices"

    clock.value += timedelta(minutes=20)
    _rebuild(timeline, history, clock, _plan(clock.value, extension=48, tomorrow_solar=0.1), {"planning_horizon_reason": "no_tomorrow_prices"})
    day_ahead = [r for r in history._days["2026-11-11"]["revisions"] if r["h"] == "day_ahead"]
    assert len(day_ahead) == 3
    intraday = [r for r in history._days["2026-11-10"]["revisions"] if r["h"] == "intraday"]
    assert intraday and intraday[0]["p_solar"] > 0


def _intraday(history, day):
    return [r for r in history._days[day]["revisions"] if r["h"] == "intraday"]


def test_balance_checks_between_plans_add_no_revisions_or_scalars():
    """The cheaper balance checks replace the decision data without saying
    which horizon they planned. On 13 Nov their 21.6 kWh deficit alternated
    with the plan's 10.9 kWh every few minutes. The projection does not depend
    on which decision is current, so the alternation must change nothing."""
    clock = Clock(datetime(2026, 11, 13, 1, 20, tzinfo=AMS))
    timeline, history = _setup(clock)
    plan = {"planning_horizon_reason": "no_tomorrow_prices", "energy_deficit_kwh": 10.9}
    balance_check = {"energy_deficit_kwh": 21.6}  # no planning_horizon_reason
    timeline._actual_consumption_kwh = [0.1] * 96  # the house uses what was planned
    _rebuild(timeline, history, clock, _plan(clock.value), plan)
    for step in range(12):
        clock.value += timedelta(minutes=5)
        _rebuild(timeline, history, clock, _plan(clock.value), balance_check if step % 2 == 0 else plan)

    revisions = history._days["2026-11-13"]["revisions"]
    assert [r.get("deficit") for r in revisions if r["h"] == "intraday"] == [10.9]
    assert all(r.get("deficit") != 21.6 for r in revisions)


def test_a_moving_forecast_is_recorded_between_full_plans():
    """Only the 00:05 run and a few triggers re-plan in full; the pre-slot
    and evening balance checks in between left the chart a straight line
    while the forecast really moved. Those revisions carry no scalars."""
    clock = Clock(datetime(2026, 11, 13, 6, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    plan = {"planning_horizon_reason": "no_tomorrow_prices", "energy_deficit_kwh": 10.9}
    _rebuild(timeline, history, clock, _plan(clock.value, solar=0.2), plan)
    for hour, solar in ((8, 0.25), (10, 0.3), (12, 0.35)):
        clock.value = datetime(2026, 11, 13, hour, 0, tzinfo=AMS)
        _rebuild(timeline, history, clock, _plan(clock.value, solar=solar), {"energy_deficit_kwh": 21.6})

    intraday = _intraday(history, "2026-11-13")
    assert len(intraday) == 4
    assert [r.get("reason") for r in intraday] == ["no_tomorrow_prices", None, None, None]
    assert [r.get("deficit") for r in intraday] == [10.9, None, None, None]
    # 0.2 x 32 quarters, then 0.25 x 32, 0.3 x 24 and 0.35 x 16 still ahead.
    assert [round(r["a_solar"] + r["p_solar"], 3) for r in intraday] == [6.4, 8.0, 7.2, 5.6]

    # The same full plan again is no structural change by itself.
    clock.value += timedelta(minutes=1)
    assert _rebuild(timeline, history, clock, _plan(clock.value, solar=0.35), plan) is False


def test_the_dynamic_pricing_start_of_day_plan_waits_for_the_full_run():
    clock = Clock(datetime(2026, 11, 13, 0, 7, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value, solar=0.4), {"energy_deficit_kwh": 21.6})
    assert "start_of_day" not in history._days["2026-11-13"]

    clock.value += timedelta(minutes=3)
    _rebuild(timeline, history, clock, _plan(clock.value, solar=0.2),
             {"planning_horizon_reason": "daily_evaluation"})
    assert history._days["2026-11-13"]["start_of_day"]["totals"]["solar"] == pytest.approx(6.4)


def test_time_slot_records_the_start_of_day_plan_and_revisions_without_a_reason():
    clock = Clock(datetime(2026, 11, 13, 0, 4, tzinfo=AMS))
    timeline, history = _setup(clock)

    def rebuild(solar):
        timeline.rebuild_future_projection(_plan(clock.value, solar=solar), now=clock.value, mode="time_slot")
        return history.observe(timeline, decision={"should_charge": False}, now=clock.value)

    rebuild(0.2)
    assert "start_of_day" not in history._days["2026-11-13"]  # before 00:06
    clock.value = datetime(2026, 11, 13, 0, 6, tzinfo=AMS)
    rebuild(0.2)
    captured = history._days["2026-11-13"]["start_of_day"]
    assert captured["captured_at"].startswith("2026-11-13T00:06")
    assert captured["coverage"] == 96
    assert captured["totals"]["solar"] == pytest.approx(6.4)

    clock.value = datetime(2026, 11, 13, 9, 0, tzinfo=AMS)
    rebuild(0.3)
    intraday = _intraday(history, "2026-11-13")
    assert len(intraday) == 2 and {r["mode"] for r in intraday} == {"time_slot"}
    assert history.list_days()["today_has_plan"] is True


def test_revision_count_is_bounded_per_horizon():
    clock = Clock(datetime(2026, 11, 10, 0, 30, tzinfo=AMS))
    timeline, history = _setup(clock)
    timeline.rebuild_future_projection(_plan(clock.value), now=clock.value)
    for step in range(MAX_REVISIONS_PER_HORIZON + 10):
        clock.value = datetime(2026, 11, 10, 0, 30, tzinfo=AMS) + timedelta(minutes=step)
        view = view_from_timeline_payload(timeline.forecast_history_payload())
        view["extension_coverage"] = 0
        view["mode"] = f"mode-{step}"  # structural: always recorded
        history.observe(view=view, now=clock.value)
    intraday = [r for r in history._days["2026-11-10"]["revisions"] if r["h"] == "intraday"]
    assert len(intraday) == MAX_REVISIONS_PER_HORIZON
    assert intraday[0]["mode"] == "mode-0"
    assert intraday[-1]["mode"] == f"mode-{MAX_REVISIONS_PER_HORIZON + 9}"


def test_measured_grid_charge_is_split_by_active_time():
    clock = Clock(datetime(2026, 11, 10, 3, 1, tzinfo=AMS))
    timeline, history = _setup(clock)
    timeline.record_runtime_decision(
        action_mask=ACTION_GRID_CHARGE, charge_power_w=2000, duration_s=600, at=clock.value
    )
    timeline.record_runtime_decision(
        action_mask=ACTION_SOLAR_CHARGE, charge_power_w=2000, duration_s=200, at=clock.value
    )
    clock.value = datetime(2026, 11, 11, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)

    actual = history._days["2026-11-10"]["actual"]
    # 2000 W for 800 s charged 0.444 kWh, three quarters of it on grid time.
    assert actual["series"]["grid_charge"][12] == pytest.approx(0.3333, abs=1e-3)
    assert actual["totals"]["grid_charge"] == pytest.approx(0.333, abs=1e-3)


async def test_a_stored_diary_from_a_past_day_is_frozen_at_load():
    clock = Clock(datetime(2026, 11, 10, 21, 0, tzinfo=AMS))
    timeline_store = FakeStore()
    timeline, history = _setup(clock, timeline_store=timeline_store)
    timeline.rebuild_future_projection(_plan(clock.value), now=clock.value)
    await timeline.async_save()

    # Home Assistant was down over midnight and starts again the next morning.
    clock.value = datetime(2026, 11, 11, 8, 0, tzinfo=AMS)
    history_store = FakeStore()
    timeline2, history2 = _setup(clock, timeline_store=timeline_store, history_store=history_store)
    await history2.async_load()
    await timeline2.async_load()

    assert history2._days["2026-11-10"]["finalized"] is True
    assert history2._days["2026-11-11"]["day_ahead"]["coverage"] == 96


async def test_history_persists_and_prunes_old_days():
    clock = Clock(datetime(2026, 11, 10, 12, 0, tzinfo=AMS))
    store = FakeStore(
        {
            "version": 1,
            "days": {
                "2026-08-01": {"date": "2026-08-01", "revisions": []},
                "2026-11-09": {"date": "2026-11-09", "finalized": True, "revisions": []},
                "not-a-date": {},
            },
        }
    )
    _timeline, history = _setup(clock, history_store=store)
    assert await history.async_load() is True
    assert sorted(history._days) == ["2026-11-09"]
    history.request_save()
    assert await history.async_shutdown() is True
    assert list(store.data["days"]) == ["2026-11-09"]


def test_day_detail_includes_live_plan_for_today_and_tomorrow():
    clock = Clock(datetime(2026, 11, 10, 14, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value))

    today = history.day_detail("2026-11-10")
    tomorrow = history.day_detail("2026-11-11")

    assert today["actual_partial"] is True
    assert today["series"]["current_plan"]["solar"][60] == pytest.approx(0.25)
    assert today["series"]["actual"]["solar"][60] is None  # not measured yet
    assert tomorrow["series"]["current_plan"]["grid_charge"][8] == pytest.approx(0.4)
    assert history.day_detail("garbage") is None


def test_csv_exports_have_one_row_per_day_interval_and_revision():
    clock = Clock(datetime(2026, 11, 10, 21, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value))
    clock.value = datetime(2026, 11, 11, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)

    days = list(csv.DictReader(io.StringIO(history.export_csv("days"))))
    intervals = list(csv.DictReader(io.StringIO(history.export_csv("intervals"))))
    revisions = list(csv.DictReader(io.StringIO(history.export_csv("revisions"))))

    assert [row["date"] for row in days] == ["2026-11-10", "2026-11-11"]
    assert days[1]["day_ahead_coverage_intervals"] == "96"
    assert days[1]["day_ahead_solar_kwh"] == "9.6"
    assert len(intervals) == 2 * 96
    assert intervals[8 + 96]["time"] == "02:00"
    assert intervals[8 + 96]["day_ahead_grid_charge"] == "0.4"
    assert {row["horizon"] for row in revisions} == {"intraday", "day_ahead"}


def test_websocket_commands_answer_from_the_entry_history():
    from custom_components.omnibattery import forecast_history_ws as ws

    clock = Clock(datetime(2026, 11, 10, 14, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value))
    hass = SimpleNamespace(data={"omnibattery": {"entry": {"forecast_history": history}, "pid_controller": object()}})

    class Connection:
        def __init__(self):
            self.results, self.errors = [], []

        def send_result(self, msg_id, result):
            self.results.append((msg_id, result))

        def send_error(self, msg_id, code, message):
            self.errors.append((msg_id, code))

    connection = Connection()
    ws.ws_forecast_history_list(hass, connection, {"id": 1, "type": ws.WS_LIST})
    ws.ws_forecast_history_day(hass, connection, {"id": 2, "type": ws.WS_DAY, "date": "2026-11-11"})
    ws.ws_forecast_history_day(hass, connection, {"id": 3, "type": ws.WS_DAY, "date": "bad"})
    ws.ws_forecast_history_export(hass, connection, {"id": 4, "type": ws.WS_EXPORT, "kind": "revisions"})
    ws.ws_forecast_history_list(
        SimpleNamespace(data={}), connection, {"id": 5, "type": ws.WS_LIST}
    )

    assert [msg_id for msg_id, _ in connection.results] == [1, 2, 4]
    assert connection.results[0][1]["today"] == "2026-11-10"
    assert connection.results[2][1]["csv"].startswith("date,recorded_at,horizon")
    assert connection.errors == [(3, "invalid_format"), (5, "not_found")]


# --- the running quarter's battery flows (the projection simulates from now) --

CHARGE_W = 2500.0  # 0.625 kWh per quarter


def _charging_projection(now, charge_quarters=(8, 9)):
    """What build_daily_operation_projection publishes at ``now``: from the
    running quarter on, with only the rest of that quarter's battery flows."""
    index = now.hour * 4 + now.minute // 15
    elapsed = (now.minute % 15) * 60 + now.second
    intervals = []
    for i in range(index, 96):
        item = {"index": i, "solar_kwh": 0.0, "consumption_kwh": 0.1}
        if i in charge_quarters:
            share = (900 - elapsed) / 900 if i == index else 1.0
            item["grid_to_battery_kwh"] = 0.625 * share
            item["action_mask"] = ACTION_GRID_CHARGE
        intervals.append(item)
    return {"intervals": intervals, "extended_intervals": [], "extended_horizon": {"interval_count": 0}}


def _run_charging_night(start, end):
    clock = Clock(start)
    timeline, history = _setup(clock)
    timeline._actual_consumption_kwh = [0.1] * 96
    totals = []
    while clock.value <= end:
        index = clock.value.hour * 4 + clock.value.minute // 15
        # The controller samples once a minute; a sample in a charging
        # quarter covers the minute before it.
        charging = index in (8, 9) and not (clock.value.minute % 15 == 0 and clock.value.second == 0)
        timeline.record_runtime_decision(
            action_mask=ACTION_GRID_CHARGE if charging else 0,
            charge_power_w=CHARGE_W if charging else 0.0,
            duration_s=60, at=clock.value,
        )
        timeline.rebuild_future_projection(
            _charging_projection(clock.value), now=clock.value, mode="time_slot"
        )
        history.observe(timeline, decision={}, now=clock.value)
        view = view_from_timeline_payload(timeline.forecast_history_payload())
        entry = history._intraday_revision(view, {}, clock.value, None)
        totals.append(entry["a_grid_charge"] + entry["p_grid_charge"])
        clock.value += timedelta(minutes=1)
    return timeline, history, totals


def test_a_charging_quarter_keeps_the_expected_day_total():
    """Two planned 0.625 kWh quarters, charged exactly as planned: the expected
    total stays 1.25 kWh through each quarter and across its boundaries. It
    used to drop by what the running quarter had already charged and jump
    back when it closed, and every swing was stored as a revision."""
    start = datetime(2026, 11, 13, 1, 50, 30, tzinfo=AMS)
    _timeline, history, totals = _run_charging_night(start, start + timedelta(minutes=50))

    # Samples at :30 put one sample's half-minute into the next quarter.
    tolerance = 0.625 / 30 + 1e-3
    assert all(abs(total - 1.25) <= tolerance for total in totals), totals
    assert len(_intraday(history, "2026-11-13")) == 1


def test_the_current_plan_starts_at_the_running_quarter_with_its_measured_part():
    start = datetime(2026, 11, 13, 1, 50, 30, tzinfo=AMS)
    _timeline, history, _totals = _run_charging_night(start, start + timedelta(minutes=19))
    # 02:09:30: quarter 8 has charged ten samples, five and a half minutes are left.
    plan = history.day_detail("2026-11-13")["series"]["current_plan"]

    assert plan["grid_charge"][7] is None and plan["consumption"][7] is None
    assert plan["grid_charge"][8] == pytest.approx(0.625, abs=0.625 / 30 + 1e-3)
    assert plan["grid_charge"][9] == pytest.approx(0.625)
    # Solar and consumption are full-quarter figures already.
    assert plan["consumption"][8] == pytest.approx(0.1)


def test_a_start_of_day_capture_in_a_charging_quarter_holds_the_whole_quarter():
    clock = Clock(datetime(2026, 11, 13, 0, 1, tzinfo=AMS))
    timeline, history = _setup(clock)
    for minute in range(1, 8):
        clock.value = datetime(2026, 11, 13, 0, minute, tzinfo=AMS)
        timeline.record_runtime_decision(
            action_mask=ACTION_GRID_CHARGE, charge_power_w=CHARGE_W, duration_s=60, at=clock.value
        )
    timeline.rebuild_future_projection(
        _charging_projection(clock.value, charge_quarters=(0, 1)), now=clock.value, mode="time_slot"
    )
    history.observe(timeline, decision={}, now=clock.value)

    captured = history._days["2026-11-13"]["start_of_day"]
    assert captured["series"]["grid_charge"][0] == pytest.approx(0.625, abs=1e-3)
    assert captured["totals"]["grid_charge"] == pytest.approx(1.25, abs=1e-3)


# --- a start-of-day plan captured late in the day ------------------------------

def test_a_late_start_of_day_plan_covers_only_the_quarters_after_it():
    """The day of the update: the diary holds the morning, the history is
    new. The quarters before the capture held the plan as each stood when it
    closed; compared over the whole day a correct plan read -97 %."""
    clock = Clock(datetime(2026, 11, 13, 10, 7, tzinfo=AMS))
    timeline, history = _setup(clock)
    timeline._actual_consumption_kwh = [0.1] * 96
    timeline.rebuild_future_projection(_plan(clock.value), now=clock.value, mode="time_slot")
    history.observe(timeline, decision={}, now=clock.value)

    captured = history._days["2026-11-13"]["start_of_day"]
    assert captured["from_index"] == 40 and captured["coverage"] == 56
    assert all(captured["series"][name][39] is None for name in ("solar", "consumption", "soc"))
    assert captured["totals"]["consumption"] == pytest.approx(5.6)

    clock.value = datetime(2026, 11, 14, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)
    summary = next(d for d in history.list_days()["days"] if d["date"] == "2026-11-13")
    compare = summary["start_of_day"]["compare"]["consumption"]
    assert summary["start_of_day"]["coverage"] == 56
    assert compare["intervals"] == 56
    assert compare["error"] == pytest.approx(0.0)


async def test_an_older_late_start_of_day_entry_is_normalised_at_load():
    clock = Clock(datetime(2026, 11, 13, 12, 0, tzinfo=AMS))
    series = {name: [0.1] * 96 for name in ("solar", "consumption", "grid_charge", "discharge")}
    series["soc"] = [50.0] * 96
    store = FakeStore({"version": 1, "days": {
        "2026-11-12": {"date": "2026-11-12", "revisions": [], "start_of_day": {
            "captured_at": "2026-11-12T10:07:00+01:00", "from_index": 40,
            "series": series, "totals": {"consumption": 9.6},
        }},
        "2026-11-11": {"date": "2026-11-11", "revisions": [], "start_of_day": {
            "captured_at": "2026-11-11T00:07:00+01:00", "from_index": 0,
            "series": series, "totals": {"consumption": 9.6},
        }},
    }})
    _timeline, history = _setup(clock, history_store=store)
    await history.async_load()

    late = history._days["2026-11-12"]["start_of_day"]
    assert late["coverage"] == 56 and late["series"]["consumption"][39] is None
    assert late["totals"]["consumption"] == pytest.approx(5.6)
    assert late["captured_at"] == "2026-11-12T10:07:00+01:00"
    whole = history._days["2026-11-11"]["start_of_day"]
    assert whole["coverage"] == 96 and whole["totals"]["consumption"] == pytest.approx(9.6)
    summary = next(d for d in history.list_days()["days"] if d["date"] == "2026-11-12")
    assert summary["start_of_day"]["coverage"] == 56


# --- quarters Home Assistant did not measure -----------------------------------

def test_quarters_without_measurements_are_not_measured_zero():
    """A two-hour gap on a perfectly forecast day read solar +33 % and
    consumption +9 %: the profiles hand over 0 kWh with 0 s coverage."""
    clock = Clock(datetime(2026, 11, 12, 21, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value, tomorrow_solar=0.3))

    gap = range(40, 48)  # 10:00-12:00
    solar = [0.0 if i in gap else (0.3 if 32 <= i < 64 else 0.0) for i in range(96)]
    load = [0.0 if i in gap else 0.12 for i in range(96)]
    cover = [0.0 if i in gap else 900.0 for i in range(96)]
    for index in range(96):
        clock.value = datetime(2026, 11, 13, 0, 1, tzinfo=AMS) + timedelta(minutes=15 * index)
        timeline.refresh_actual_partial(
            solar_capture={"interval_energy_kwh": solar, "interval_coverage_s": cover},
            consumption_capture={"interval_energy_kwh": load, "interval_coverage_s": cover},
            now=clock.value,
        )
    clock.value = datetime(2026, 11, 14, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)

    summary = next(d for d in history.list_days()["days"] if d["date"] == "2026-11-13")
    for name in ("solar", "consumption"):
        compare = summary["day_ahead"]["compare"][name]
        assert compare["intervals"] == 88, name
        assert compare["error"] == pytest.approx(0.0), name
    assert summary["actual_measured"] == {
        "solar": 88, "consumption": 88, "grid_charge": 88, "discharge": 88,
    }
    actual = history._days["2026-11-13"]["actual"]["series"]
    assert actual["grid_charge"][41] is None and actual["discharge"][41] is None
    assert actual["grid_charge"][20] == 0.0  # observed, moved nothing


def test_a_day_without_coverage_information_keeps_its_values():
    clock = Clock(datetime(2026, 11, 13, 10, 0, tzinfo=AMS))
    timeline, _history = _setup(clock)
    timeline._actual_consumption_kwh = [0.1] * 96
    payload = timeline.forecast_history_payload()
    payload["actual"].pop("consumption_coverage_s")
    payload["actual"].pop("solar_coverage_s")

    view = view_from_timeline_payload(payload, final=True)

    assert view["actual"]["consumption"] == [0.1] * 96


# --- modes without a plan --------------------------------------------------------

def test_a_mode_without_a_plan_says_so_in_the_list():
    clock = Clock(datetime(2026, 11, 13, 10, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    # Real-Time Price and manual mode: a projection without intervals.
    timeline.rebuild_future_projection(
        {"intervals": [], "mode": "realtime_price", "stale": False}, now=clock.value
    )
    assert history.list_days()["today_has_plan"] is False

    timeline.rebuild_future_projection(_plan(clock.value), now=clock.value, mode="time_slot")
    assert history.list_days()["today_has_plan"] is True

    # An unavailable projection says nothing about the mode.
    timeline.rebuild_future_projection(
        {"intervals": [], "mode": "time_slot", "stale": True, "stale_reason": "projection_unavailable"},
        now=clock.value,
    )
    assert history.list_days()["today_has_plan"] is None


# --- measured grid charge --------------------------------------------------------

def _charge_quarter(grid_w, action_mask):
    clock = Clock(datetime(2026, 11, 13, 12, 0, 30, tzinfo=AMS))
    timeline, history = _setup(clock)
    for minute in range(15):
        clock.value = datetime(2026, 11, 13, 12, minute, 30, tzinfo=AMS)
        timeline.record_runtime_decision(
            {"grid_power_w": grid_w, "ac_charge_power_w": CHARGE_W},
            action_mask=action_mask, charge_power_w=CHARGE_W, duration_s=60, at=clock.value,
        )
    clock.value = datetime(2026, 11, 14, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)
    return history._days["2026-11-13"]["actual"]["series"]["grid_charge"][48]


@pytest.mark.parametrize(
    ("grid_w", "expected"),
    [
        # A daytime slot: solar exceeds the house by 0.8 kW, so the grid
        # imports 1.7 of the 2.5 kW charge: 0.425 kWh in the quarter.
        (1700.0, 0.425),
        # A night slot: all of it.
        (2600.0, 0.625),
    ],
)
def test_measured_grid_charge_is_what_the_grid_delivered(grid_w, expected):
    assert _charge_quarter(grid_w, ACTION_GRID_CHARGE) == pytest.approx(expected, abs=1e-3)


def test_solar_only_charging_is_no_grid_charge():
    assert _charge_quarter(40.0, ACTION_SOLAR_CHARGE) == 0.0  # PD ripple on the meter


# --- labels ------------------------------------------------------------------------

def test_the_day_ahead_plan_is_stamped_with_its_last_projection():
    """A Dynamic Pricing schedule evaluated a day earlier is re-projected every
    minute; the frozen series is the last projection, not that evaluation."""
    clock = Clock(datetime(2026, 11, 12, 23, 58, tzinfo=AMS))
    timeline, history = _setup(clock)
    timeline.rebuild_future_projection(
        _plan(clock.value), now=clock.value, mode="dynamic_pricing",
        evaluated_at=datetime(2026, 11, 11, 23, 0, tzinfo=AMS),
    )
    history.observe(timeline, decision={}, now=clock.value)
    clock.value = datetime(2026, 11, 13, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)

    assert history._days["2026-11-13"]["day_ahead"]["captured_at"].startswith("2026-11-12T23:58")


# --- no plan ahead: nothing to revise, nothing to capture ---------------------

PLANLESS = {"intervals": [], "mode": "realtime_price", "stale": False}


def test_a_mode_without_a_plan_stores_no_revisions():
    """Real-Time Price and manual mode: the "expected" total would only be the
    measured part, rising from 0 to the day's total in 96 revisions."""
    clock = Clock(datetime(2026, 11, 13, 0, 1, tzinfo=AMS))
    timeline, history = _setup(clock)
    load = [0.1] * 96
    for index in range(96):
        clock.value = datetime(2026, 11, 13, 0, 7, tzinfo=AMS) + timedelta(minutes=15 * index)
        timeline.refresh_actual_partial(
            consumption_capture={"interval_energy_kwh": load, "interval_coverage_s": [900.0] * 96},
            now=clock.value,
        )
        timeline.rebuild_future_projection(PLANLESS, now=clock.value)
        history.observe(timeline, decision={}, now=clock.value)

    record = history._days["2026-11-13"]
    assert record["revisions"] == []
    assert "start_of_day" not in record


def test_a_planned_day_still_records_its_last_quarter():
    clock = Clock(datetime(2026, 11, 13, 23, 31, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value, extension=0))
    clock.value = datetime(2026, 11, 13, 23, 52, tzinfo=AMS)
    plan = _plan(clock.value, extension=0)
    plan["intervals"][95]["consumption_kwh"] = 0.5
    _rebuild(timeline, history, clock, plan)

    intraday = _intraday(history, "2026-11-13")
    assert len(intraday) == 2 and intraday[-1]["idx"] == 95


def test_no_start_of_day_entry_without_a_plan_and_the_real_one_follows():
    """Manual mode at the first observation after 00:06 (the update day):
    the closed cells still carry their old plan, but nothing is planned now.
    An empty entry would block the real capture when the plan returns."""
    clock = Clock(datetime(2026, 11, 13, 0, 7, tzinfo=AMS))
    timeline, history = _setup(clock)
    timeline.rebuild_future_projection(_plan(clock.value), now=clock.value, mode="time_slot")
    clock.value = datetime(2026, 11, 13, 10, 7, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)
    timeline.rebuild_future_projection(PLANLESS, now=clock.value)
    history.observe(timeline, decision={}, now=clock.value)
    assert "start_of_day" not in history._days["2026-11-13"]

    clock.value = datetime(2026, 11, 13, 12, 7, tzinfo=AMS)
    timeline.rebuild_future_projection(_plan(clock.value), now=clock.value, mode="time_slot")
    history.observe(timeline, decision={}, now=clock.value)
    captured = history._days["2026-11-13"]["start_of_day"]
    assert captured["coverage"] == 48
    assert captured["totals"]["consumption"] == pytest.approx(4.8)


# --- a solar sensor that is silent at night ----------------------------------

def test_a_solar_sensor_silent_at_night_is_no_gap():
    """An external solar sensor is unavailable at night, so the solar profile
    has coverage only while the sun is up. Home Assistant observed the whole
    day: night quarters produced nothing, they were not unmeasured."""
    clock = Clock(datetime(2026, 11, 12, 21, 0, tzinfo=AMS))
    timeline, history = _setup(clock)
    _rebuild(timeline, history, clock, _plan(clock.value, tomorrow_solar=0.3))
    solar = [0.3 if 34 <= i < 64 else 0.0 for i in range(96)]
    solar_cover = [900.0 if 34 <= i < 64 else 0.0 for i in range(96)]
    for index in range(96):
        clock.value = datetime(2026, 11, 13, 0, 1, tzinfo=AMS) + timedelta(minutes=15 * index)
        timeline.refresh_actual_partial(
            solar_capture={"interval_energy_kwh": solar, "interval_coverage_s": solar_cover},
            consumption_capture={"interval_energy_kwh": [0.12] * 96, "interval_coverage_s": [900.0] * 96},
            now=clock.value,
        )
    clock.value = datetime(2026, 11, 14, 0, 1, tzinfo=AMS)
    timeline.refresh_actual_partial(now=clock.value)

    summary = next(d for d in history.list_days()["days"] if d["date"] == "2026-11-13")
    assert summary["actual_measured"]["solar"] == 96
    series = history._days["2026-11-13"]["actual"]["series"]["solar"]
    assert series[10] == 0.0 and series[40] == pytest.approx(0.3)
    # Quarters 32 and 33 were forecast at 0.3 but the sensor was still silent.
    assert summary["day_ahead"]["compare"]["solar"]["error"] == pytest.approx(0.6)


# --- the first projections after a restart -----------------------------------

def _startup_plan(now, *, extension, grid_kwh):
    plan = _plan(now, extension=extension)
    plan["extended_intervals"][8]["grid_to_battery_kwh"] = grid_kwh
    return plan


def test_nothing_is_recorded_while_the_plan_settles_after_a_restart():
    """The first projections after a restart are built before the planner has
    run. On 13 Nov tomorrow's plan was stored with a 48-quarter horizon and
    then with 7.5 instead of 4.5 kWh grid charge, within a minute of the
    restart; the short horizon also broke the day-ahead line."""
    start = datetime(2026, 11, 13, 22, 9, 14, tzinfo=AMS)
    clock = Clock(start)
    timeline, history = _setup(clock, startup_settle_s=None)

    def observe(seconds, plan):
        clock.value = start + timedelta(seconds=seconds)
        timeline.rebuild_future_projection(plan, now=clock.value, mode="time_slot")
        return history.observe(timeline, decision={}, now=clock.value)

    # No prices yet: half the horizon. Then slots at full power, then the plan.
    assert observe(0, _startup_plan(start, extension=48, grid_kwh=0.0)) is False
    assert observe(5, _startup_plan(start, extension=96, grid_kwh=7.5)) is False
    assert observe(62, _startup_plan(start, extension=96, grid_kwh=4.5)) is False
    assert not history._days

    assert observe(121, _startup_plan(start, extension=96, grid_kwh=4.5)) is True
    tomorrow = history._days["2026-11-14"]["revisions"]
    assert [(r["cov"], r["p_grid_charge"]) for r in tomorrow] == [(96, 4.5)]
    assert len(_intraday(history, "2026-11-13")) == 1
    captured = history._days["2026-11-13"]["start_of_day"]
    assert captured["captured_at"].startswith("2026-11-13T22:11")


async def test_a_restart_with_an_unchanged_plan_adds_no_revision():
    """The revisions from before the restart stay the reference: once the plan
    has settled on the same totals, the restart leaves no trace."""
    start = datetime(2026, 11, 13, 22, 9, 14, tzinfo=AMS)
    clock = Clock(start)
    before = {
        "t": "2026-11-13T21:49:00+01:00", "h": "day_ahead", "mode": "time_slot", "cov": 96,
        "p_solar": 9.6, "p_consumption": 11.52, "p_grid_charge": 0.4, "p_discharge": 0.0,
        "soc_end": 50.0,
    }
    store = FakeStore({"version": 1, "days": {
        "2026-11-14": {"date": "2026-11-14", "revisions": [before]},
    }})
    timeline, history = _setup(clock, history_store=store, startup_settle_s=None)
    await history.async_load()

    for seconds, extension in ((0, 48), (5, 96), (130, 96)):
        clock.value = start + timedelta(seconds=seconds)
        timeline.rebuild_future_projection(
            _plan(clock.value, extension=extension), now=clock.value, mode="time_slot"
        )
        history.observe(timeline, decision={}, now=clock.value)

    assert history._days["2026-11-14"]["revisions"] == [before]


def test_a_clock_that_jumped_back_does_not_prolong_the_wait():
    start = datetime(2026, 11, 13, 22, 9, 14, tzinfo=AMS)
    clock = Clock(start)
    timeline, history = _setup(clock, startup_settle_s=None)
    timeline.rebuild_future_projection(_plan(start), now=start, mode="time_slot")
    assert history.observe(timeline, decision={}, now=start) is False
    # The time was corrected by ten minutes: the first observation is no
    # reference any more, so recording starts.
    earlier = start - timedelta(minutes=10)
    assert history.observe(
        view=view_from_timeline_payload(timeline.forecast_history_payload()),
        decision={}, now=earlier,
    ) is True
