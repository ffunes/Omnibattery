"""Decision log: daily JSON-lines files that explain the controller's choices."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from custom_components.omnibattery.tracking.decision_log import (
    DecisionLogger,
    _clean,
)

AMS = ZoneInfo("Europe/Amsterdam")


class Hass:
    """Runs executor jobs inline and keeps background tasks to await."""

    def __init__(self, tmp_path):
        self.config = SimpleNamespace(
            path=lambda *parts: str(tmp_path.joinpath(*parts)), time_zone="Europe/Amsterdam"
        )
        self.tasks = []

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_background_task(self, coro, name):
        self.tasks.append(asyncio.ensure_future(coro))

    async def drain(self):
        while self.tasks:
            await self.tasks.pop(0)


def _controller(**overrides):
    coordinator = SimpleNamespace(
        name="Venus 1",
        data={"battery_soc": 80.0, "battery_total_energy": 5.12, "ac_power": 300.0},
        battery_manual_mode_enabled=False,
    )
    values = dict(
        coordinators=[coordinator],
        predictive_charging_mode="dynamic_pricing",
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        manual_mode_enabled=False,
        grid_charging_active=False,
        _realtime_price_charging=False,
        _active_dynamic_slot_purpose=None,
        _price_data_status="ok",
        charge_delay_enabled=True,
        _charge_delay_unlocked=False,
        _charge_delay_status={"state": "Waiting for solar", "estimated_unlock_time": "09:30"},
        _price_reserve_soc_pct=35.0,
        _last_decision_data={
            "energy_deficit_kwh": 1.25,
            "planned_grid_charge_kwh": 2.0,
            "solar_forecast_kwh": 3.4,
            "daily_consumption_history": [1] * 50,
            "decision_reason": "deficit",
        },
        _dynamic_pricing_schedule=None,
        previous_power=-300,
        previous_sensor=120.0,
        _consumption_tracker=SimpleNamespace(
            _read_total_solar_power_kw=lambda: 0.0, _daily_grid_last_power_kw=0.12
        ),
    )
    values.update(overrides)
    controller = SimpleNamespace(**values)
    return controller


def _entry():
    return SimpleNamespace(
        entry_id="entry",
        data={"batteries": [{"host": "10.0.0.2"}], "consumption_sensor": "sensor.grid"},
        options={"pricing_safety_margin": 0.5, "password": "secret"},
    )


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_start_state_and_plan_lines_are_written_with_numbers(tmp_path):
    hass = Hass(tmp_path)
    clock = datetime(2026, 11, 10, 22, 3, tzinfo=AMS)
    log = DecisionLogger(hass, _entry(), _controller(), now_provider=lambda: clock)
    log.log_start("1.6.1b1", now=clock)
    log.tick(clock)
    await hass.drain()

    lines = _lines(log.path_for(date(2026, 11, 10)))
    assert [line["event"] for line in lines] == ["start", "plan", "state"]
    start, plan, state = lines
    assert start["version"] == "1.6.1b1"
    assert start["options"]["pricing_safety_margin"] == 0.5
    assert start["options"]["password"] == "**REDACTED**"
    assert "batteries" not in start["options"]
    assert plan["decision"]["energy_deficit_kwh"] == 1.25
    assert "daily_consumption_history" not in plan["decision"]
    assert state["energy"]["system_soc_pct"] == 80.0
    assert state["energy"]["stored_kwh"] == 4.096
    assert state["batteries"][0]["power_w"] == -300.0  # discharging
    assert state["control"]["charge_delay"]["state"] == "Waiting for solar"
    assert state["control"]["price_reserve_soc_pct"] == 35.0


async def test_state_is_written_once_per_quarter_and_changes_in_between(tmp_path):
    hass = Hass(tmp_path)
    controller = _controller()
    log = DecisionLogger(hass, _entry(), controller)
    start = datetime(2026, 11, 10, 23, 1, tzinfo=AMS)
    for minute in range(0, 20):
        now = start + timedelta(minutes=minute)
        if minute == 5:
            controller.grid_charging_active = True
            controller._active_dynamic_slot_purpose = "deficit"
        log.tick(now)
        await hass.drain()

    events = [(line["event"], line["t"][11:16]) for line in _lines(log.path_for(date(2026, 11, 10)))]
    assert ("state", "23:01") in events
    assert ("state", "23:15") in events
    assert [e for e in events if e[0] == "state"] == [("state", "23:01"), ("state", "23:15")]
    assert ("change", "23:06") in events


async def test_plan_lines_are_spaced_unless_the_slots_change(tmp_path):
    hass = Hass(tmp_path)
    controller = _controller()
    log = DecisionLogger(hass, _entry(), controller)
    start = datetime(2026, 11, 10, 1, 0, tzinfo=AMS)
    log.tick(start)
    controller._last_decision_data = {**controller._last_decision_data, "stored_energy_kwh": 4.0}
    log.tick(start + timedelta(seconds=20))  # changed, but within a minute
    log.tick(start + timedelta(seconds=70))
    await hass.drain()

    plans = [line for line in _lines(log.path_for(date(2026, 11, 10))) if line["event"] == "plan"]
    assert len(plans) == 2
    assert plans[1]["decision"]["stored_energy_kwh"] == 4.0


async def test_options_change_is_logged(tmp_path):
    hass = Hass(tmp_path)
    entry = _entry()
    log = DecisionLogger(hass, entry, _controller())
    now = datetime(2026, 11, 10, 12, 0, tzinfo=AMS)
    log.log_start(now=now)
    entry.options = {**entry.options, "pricing_safety_margin": 1.0}
    log.tick(now + timedelta(minutes=1))
    await hass.drain()

    options = [line for line in _lines(log.path_for(date(2026, 11, 10))) if line["event"] == "options"]
    assert options and options[0]["options"]["pricing_safety_margin"] == 1.0


async def test_lines_go_to_the_file_of_their_local_day_and_old_files_are_removed(tmp_path):
    hass = Hass(tmp_path)
    log = DecisionLogger(hass, _entry(), _controller(), retention_days=14)
    log.directory.mkdir(parents=True)
    old = log.path_for(date(2026, 10, 20))
    keep = log.path_for(date(2026, 10, 30))
    other = log.directory / "notes.txt"
    for path in (old, keep, other):
        path.write_text("x\n", encoding="utf-8")

    log.tick(datetime(2026, 11, 10, 23, 59, tzinfo=AMS))
    await hass.drain()
    log.tick(datetime(2026, 11, 11, 0, 1, tzinfo=AMS))
    await hass.drain()

    assert log.path_for(date(2026, 11, 10)).exists()
    assert log.path_for(date(2026, 11, 11)).exists()
    assert not old.exists()
    assert keep.exists() and other.exists()


async def test_a_failing_section_or_disk_never_raises(tmp_path):
    hass = Hass(tmp_path)

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    log = DecisionLogger(hass, _entry(), _controller(_consumption_tracker=Broken()))
    log.tick(datetime(2026, 11, 10, 3, 0, tzinfo=AMS))

    async def failing(func, *args):
        raise OSError("disk full")

    hass.async_add_executor_job = failing
    await hass.drain()
    log.tick(datetime(2026, 11, 10, 3, 15, tzinfo=AMS))
    await hass.drain()
    assert log.write_errors == 1


async def test_every_option_is_logged_so_a_changed_margin_shows(tmp_path):
    hass = Hass(tmp_path)
    entry = _entry()
    entry.options = {
        **{f"option_{i:02d}": i for i in range(47)},
        "password": "secret",
        "min_arbitrage_margin": 0.0,
    }
    log = DecisionLogger(hass, entry, _controller())
    now = datetime(2026, 11, 12, 15, 55, tzinfo=AMS)
    log.log_start(now=now)
    entry.options = {**entry.options, "min_arbitrage_margin": 0.05}
    log.tick(now + timedelta(minutes=1))
    await hass.drain()

    lines = _lines(log.path_for(date(2026, 11, 12)))
    start = next(line for line in lines if line["event"] == "start")
    change = next(line for line in lines if line["event"] == "options")
    assert "_truncated" not in start["options"] and len(start["options"]) == 50
    assert start["options"]["min_arbitrage_margin"] == 0.0
    assert change["options"]["min_arbitrage_margin"] == 0.05
    assert start["options"]["password"] == "**REDACTED**"


def _trace(allocations):
    return {
        "now": "2026-11-12T02:05:00",
        "deadlines": [[f"2026-11-12T{h:02d}:00:00", 0.2 * h, "depletion"] for h in range(30)],
        "allocations": allocations,
        "intervals": [["2026-11-12T00:00:00", 0.2, 0.0, 0.3]] * 120,
    }


async def test_plan_traces_are_written_whole_when_new_and_spaced_otherwise(tmp_path):
    hass = Hass(tmp_path)
    from custom_components.omnibattery.pricing import PriceSlot

    slot = PriceSlot(datetime(2026, 11, 12, 3, 0), datetime(2026, 11, 12, 3, 15), 0.18)
    schedule = SimpleNamespace(slot_energy_targets_kwh={slot: 0.62}, selected_slots=[slot])
    controller = _controller(_dynamic_pricing_schedule=schedule)
    log = DecisionLogger(hass, _entry(), controller)
    start = datetime(2026, 11, 12, 2, 5, tzinfo=AMS)
    booked = [["2026-11-12T03:00:00", 0.18, 0.62, "2026-11-12T04:00:00", "deadline"]]

    controller._last_plan_trace = _trace(booked)
    log.tick(start)
    log.tick(start + timedelta(minutes=1))  # same object: nothing new
    controller._last_plan_trace = _trace(booked)  # rebuilt, same booking
    log.tick(start + timedelta(minutes=2))
    controller._last_plan_trace = _trace(booked + [["2026-11-12T03:15:00", 0.18, 0.4, None, "flexible"]])
    log.tick(start + timedelta(minutes=3))  # booking changed: written at once
    controller._last_plan_trace = _trace(booked)
    log.tick(start + timedelta(minutes=10))  # changed back: written
    controller._last_plan_trace = _trace(booked)
    log.tick(start + timedelta(minutes=26))  # same booking, 16 min later: written
    await hass.drain()

    traces = [line for line in _lines(log.path_for(date(2026, 11, 12))) if line["event"] == "plan_trace"]
    assert [line["t"][11:16] for line in traces] == ["02:05", "02:08", "02:15", "02:31"]
    first = traces[0]
    assert len(first["trace"]["deadlines"]) == 30  # not cut at 24
    assert len(first["trace"]["intervals"]) == 120
    assert first["slot_targets_kwh"] == {"2026-11-12T03:00:00": 0.62}


async def test_no_plan_trace_without_a_control_plan(tmp_path):
    hass = Hass(tmp_path)
    log = DecisionLogger(hass, _entry(), _controller())
    log.tick(datetime(2026, 11, 12, 2, 5, tzinfo=AMS))
    await hass.drain()
    events = {line["event"] for line in _lines(log.path_for(date(2026, 11, 12)))}
    assert "plan_trace" not in events


def test_clean_bounds_size_and_handles_odd_values():
    cleaned = _clean({"many": list(range(100)), "nan": float("nan"), "when": date(2026, 1, 2)})
    assert len(cleaned["many"]) == 25 and cleaned["many"][-1] == "... 76 more"
    assert cleaned["nan"] is None
    assert cleaned["when"] == "2026-01-02"


async def test_utc_instants_from_the_control_cycle_are_logged_in_local_time(tmp_path):
    hass = Hass(tmp_path)
    log = DecisionLogger(hass, _entry(), _controller())
    # 23:30 UTC is already 00:30 on the next local day in Amsterdam.
    log.tick(datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc))
    await hass.drain()

    assert not log.path_for(date(2026, 11, 10)).exists()
    lines = _lines(log.path_for(date(2026, 11, 11)))
    assert lines[0]["t"] == "2026-11-11T00:30:00+01:00"
