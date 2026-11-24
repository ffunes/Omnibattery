"""Solar safety margin: learned from the forecast's misses until one is set.

No stored value means the learned margin (the 80th percentile of how far the
day's first forecast fell short of the real sun, last 14 days), any stored
value is used as given, and a number cannot be cleared, so the way back is an
action the panel's value editor calls -- as for the round-trip efficiency.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import _async_register_services
from custom_components.omnibattery.const import (
    CONF_PREDICTIVE_SAFETY_MARGIN_PCT,
    CONFIG_NUMBER_DEFINITIONS,
)
from custom_components.omnibattery.number import ConfigNumberEntity
from custom_components.omnibattery.pricing.calculations import (
    learned_solar_margin_pct,
    refresh_solar_safety_margin,
    solar_forecast_shortfalls,
)
from custom_components.omnibattery.tracking import consumption_tracker
from custom_components.omnibattery.tracking.consumption_tracker import ConsumptionTracker

DOMAIN = "omnibattery"
ACTION = "use_measured_solar_safety_margin"
PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")

# 2026-11-15..23 on the reporting install: Solcast at 00:00 against the sun
# that came. Shortfalls 4.8, 33.7, 8.5, 12.9, 34.1, 35.5, 46.9, 24.1, 36.4 %.
REAL_DAYS = [
    (7.629, 7.26), (11.6674, 7.74), (5.4834, 5.02), (7.816, 6.81), (19.4259, 12.81),
    (12.4799, 8.05), (6.7479, 3.58), (23.345, 17.72), (24.4334, 15.55),
]


def _day(forecast, actual, *, full=True, curtailed_h=0.0, day="2026-11-15"):
    return {"date": day, "forecast_kwh": forecast, "actual_kwh": actual,
            "curtailed_h": curtailed_h, "full": full}


HISTORY = [_day(f, a) for f, a in REAL_DAYS]


def test_the_margin_is_the_80th_percentile_of_the_shortfall():
    # Sorted: 4.8 8.5 12.9 24.1 33.7 34.1 35.5 36.4 46.9. Position 0.8 * 8 =
    # 6.4: 35.5 + 0.4 * (36.4 - 35.5) = 35.9 -> 36 %.
    assert learned_solar_margin_pct(HISTORY) == 36.0


def test_a_day_with_more_sun_than_forecast_counts_as_no_shortfall():
    assert solar_forecast_shortfalls([_day(5.0, 8.0)]) == [0.0]


def test_too_few_days_learn_nothing():
    assert learned_solar_margin_pct(HISTORY[:6]) is None
    assert learned_solar_margin_pct(HISTORY[:7]) is not None
    assert learned_solar_margin_pct([]) is None
    assert learned_solar_margin_pct(None) is None


def test_the_margin_stops_at_50():
    assert learned_solar_margin_pct([_day(10.0, 1.0)] * 7) == 50.0


def test_only_the_last_14_days_count():
    # Fourteen perfect days after the real ones: nothing left to cover.
    assert learned_solar_margin_pct(HISTORY + [_day(5.0, 5.0)] * 14) == 0.0


@pytest.mark.parametrize("record", [
    _day(10.0, 1.0, full=False),         # a "remaining" figure captured late
    _day(0.5, 0.1),                      # too little forecast to compare
    _day(10.0, 0.0),                     # no sun measured: no telemetry
    _day(10.0, 1.0, curtailed_h=0.5),    # batteries full: curtailed, not missed
    {"date": "2026-11-15"},              # a broken record
], ids=["late", "small", "no-sun", "curtailed", "broken"])
def test_days_that_say_nothing_about_the_forecast_are_left_out(record):
    assert solar_forecast_shortfalls([record]) == []


# ----------------------------------------------------------------------
# What the controller plans with
# ----------------------------------------------------------------------

def _controller(data, history=()):
    return SimpleNamespace(
        config_entry=SimpleNamespace(data=dict(data)),
        _consumption_tracker=SimpleNamespace(_solar_forecast_history=list(history)),
    )


def test_a_stored_value_is_used_as_given():
    controller = _controller({CONF_PREDICTIVE_SAFETY_MARGIN_PCT: 15.0}, HISTORY)
    refresh_solar_safety_margin(controller)

    assert controller._predictive_safety_margin_pct == 15.0
    assert controller._predictive_safety_margin_source == "configured"
    # Still known, to compare before letting go of the fixed value.
    assert controller._predictive_safety_margin_learned_pct == 36.0
    assert controller._solar_headroom_margin_pct == 15.0


def test_a_stored_zero_stays_off():
    controller = _controller({CONF_PREDICTIVE_SAFETY_MARGIN_PCT: 0.0}, HISTORY)
    refresh_solar_safety_margin(controller)

    assert controller._predictive_safety_margin_pct == 0.0
    assert controller._predictive_safety_margin_source == "configured"


def test_no_stored_value_means_learned():
    controller = _controller({}, HISTORY)
    refresh_solar_safety_margin(controller)

    assert controller._predictive_safety_margin_pct == 36.0
    assert controller._predictive_safety_margin_source == "measured"


def test_room_for_extra_sun_never_follows_the_learned_margin():
    # A forecast that falls short does not ask for more room before
    # negative-price hours. (The surplus hold target does follow the learned
    # margin: a larger one absorbs more today, which covers a shortfall.)
    controller = _controller({}, HISTORY)
    refresh_solar_safety_margin(controller)

    assert controller._solar_headroom_margin_pct == 10.0


def test_the_default_applies_until_there_are_enough_days():
    controller = _controller({}, HISTORY[:3])
    refresh_solar_safety_margin(controller)

    assert controller._predictive_safety_margin_pct == 10.0
    assert controller._predictive_safety_margin_source == "default"
    assert controller._predictive_safety_margin_learned_pct is None


# ----------------------------------------------------------------------
# Closing a day: the forecast and the sun of the same day, once
# ----------------------------------------------------------------------

YESTERDAY = date(2026, 11, 23)
TODAY = YESTERDAY + timedelta(days=1)


def _tracker(monkeypatch, *, forecast_date=YESTERDAY, forecast=24.4334, full=True):
    monkeypatch.setattr(
        consumption_tracker, "local_now", lambda: SimpleNamespace(date=lambda: TODAY)
    )
    controller = SimpleNamespace(
        config_entry=SimpleNamespace(data={}),
        _daily_solar_energy_kwh=15.55,
        _daily_solar_energy_date=YESTERDAY,
        _daily_home_energy_kwh=9.0,
        _daily_home_energy_date=YESTERDAY,
        _daily_grid_import_energy_kwh=1.0,
        _daily_grid_export_energy_kwh=0.0,
        _daily_grid_energy_date=YESTERDAY,
        _daily_solar_forecast_initial_kwh=forecast,
        _daily_solar_forecast_initial_date=forecast_date,
    )
    tracker = ConsumptionTracker.__new__(ConsumptionTracker)
    tracker._controller = controller
    tracker._solar_forecast_history = []
    tracker._daily_solar_curtailed_h = 0.2
    tracker._daily_solar_forecast_full = full
    tracker.save_daily_energy = lambda: None
    controller._consumption_tracker = tracker
    return tracker, controller


CLOSED = {"date": "2026-11-23", "forecast_kwh": 24.4334, "actual_kwh": 15.55,
          "curtailed_h": 0.2, "full": True}


def test_the_reset_first_closes_yesterday_once(monkeypatch):
    tracker, controller = _tracker(monkeypatch)

    tracker.handle_daily_energy_reset()
    assert tracker.capture_daily_solar_forecast(5.72) is True

    assert tracker._solar_forecast_history == [CLOSED]
    assert controller._daily_solar_energy_kwh == 0.0
    assert tracker._daily_solar_curtailed_h == 0.0
    assert tracker._daily_solar_forecast_full is True


def test_the_midnight_evaluation_first_closes_yesterday_with_its_own_forecast(monkeypatch):
    # The 00:00 evaluation can run before the daily reset: today's forecast
    # must not be paired with yesterday's sun.
    tracker, controller = _tracker(monkeypatch)

    assert tracker.capture_daily_solar_forecast(5.72) is True
    tracker.handle_daily_energy_reset()

    assert tracker._solar_forecast_history == [CLOSED]
    assert controller._daily_solar_forecast_initial_kwh == 5.72
    assert controller._daily_solar_energy_kwh == 0.0


def test_a_day_without_its_own_forecast_is_not_kept(monkeypatch):
    tracker, _controller_ = _tracker(monkeypatch, forecast_date=TODAY)
    tracker.handle_daily_energy_reset()
    assert tracker._solar_forecast_history == []

    tracker, _controller_ = _tracker(monkeypatch, forecast=None)
    tracker.handle_daily_energy_reset()
    assert tracker._solar_forecast_history == []


def test_a_forecast_captured_after_the_sun_started_is_not_a_whole_day(monkeypatch):
    tracker, controller = _tracker(monkeypatch)
    tracker.handle_daily_energy_reset()
    controller._daily_solar_energy_kwh = 1.2  # mid-morning: what is left of the day

    tracker.capture_daily_solar_forecast(4.0)

    assert tracker._daily_solar_forecast_full is False


def test_closing_a_day_refreshes_the_margin_and_keeps_14(monkeypatch):
    tracker, controller = _tracker(monkeypatch)
    tracker._solar_forecast_history = [
        _day(10.0, 5.0, day=f"2026-11-{d:02d}") for d in range(9, 23)
    ]

    tracker.handle_daily_energy_reset()

    assert len(tracker._solar_forecast_history) == 14
    assert tracker._solar_forecast_history[0]["date"] == "2026-11-10"
    assert tracker._solar_forecast_history[-1] == CLOSED
    assert controller._predictive_safety_margin_pct == 50.0
    assert controller._predictive_safety_margin_source == "measured"


def test_time_with_the_sun_held_back_is_counted(monkeypatch):
    tracker, controller = _tracker(monkeypatch)
    controller._daily_solar_energy_date = TODAY
    controller._daily_solar_energy_kwh = 0.0
    tracker._daily_solar_curtailed_h = 0.0
    tracker._read_total_solar_power_kw = lambda: 2.0
    tracker._solar_profile = SimpleNamespace(mode="off", _runtime_curtailment_active=False)
    clock = iter([0.0, 1800.0, 3600.0])
    monkeypatch.setattr(consumption_tracker, "monotonic", lambda: next(clock))
    tracker._daily_solar_last_time = None
    tracker._daily_solar_last_power_kw = None

    import asyncio

    asyncio.run(tracker.accumulate_daily_solar_energy())   # first sample
    asyncio.run(tracker.accumulate_daily_solar_energy())   # half an hour, free
    tracker._solar_profile._runtime_curtailment_active = True
    asyncio.run(tracker.accumulate_daily_solar_energy())   # half an hour, held back

    assert controller._daily_solar_energy_kwh == pytest.approx(2.0)
    assert tracker._daily_solar_curtailed_h == pytest.approx(0.5)


class _Store:
    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


async def test_the_days_survive_a_restart_on_another_day(monkeypatch):
    tracker, _controller_ = _tracker(monkeypatch)
    tracker._solar_forecast_history = list(HISTORY)
    tracker._daily_energy_store = _Store()
    await tracker.async_save_daily_energy()
    saved = dict(tracker._daily_energy_store.data, date="2026-11-20")  # a stale day

    restored, controller = _tracker(monkeypatch)
    restored._daily_energy_store = _Store(saved)
    await restored.load_daily_energy()

    # The history is back; the stale day's own totals are not added to it.
    assert restored._solar_forecast_history == HISTORY
    assert controller._predictive_safety_margin_pct == 36.0


# ----------------------------------------------------------------------
# The slider and the way back
# ----------------------------------------------------------------------

class _Entries:
    def __init__(self, entries=()):
        self._entries = list(entries)

    def async_entries(self, _domain):
        return list(self._entries)

    def async_update_entry(self, entry, **changes):
        for name, value in changes.items():
            setattr(entry, name, value)
        # The controller re-reads its settings on every entry update.
        controller = getattr(entry, "controller", None)
        if controller is not None:
            controller.config_entry = entry
            refresh_solar_safety_margin(controller)


class _Services:
    def __init__(self):
        self.handlers = {}

    def has_service(self, domain, service):
        return (domain, service) in self.handlers

    def async_register(self, domain, service, handler, schema=None):
        self.handlers[(domain, service)] = handler


def _slider(data, history=HISTORY):
    entry = SimpleNamespace(data=dict(data), entry_id="entry")
    controller = _controller(data, history)
    controller.config_entry = entry
    entry.controller = controller
    refresh_solar_safety_margin(controller)
    hass = SimpleNamespace(
        data={DOMAIN: {"entry": {"controller": controller}}},
        config_entries=_Entries([entry]),
        services=_Services(),
    )
    definition = next(
        d for d in CONFIG_NUMBER_DEFINITIONS if d["key"] == CONF_PREDICTIVE_SAFETY_MARGIN_PCT
    )
    return hass, entry, ConfigNumberEntity(hass, entry, definition)


def test_the_slider_shows_the_learned_value_in_use():
    _hass, _entry, slider = _slider({})

    assert slider.native_value == 36.0
    assert slider.extra_state_attributes == {
        "uses_default": True, "default_kind": "measured",
        "learned_pct": 36.0, "learned_days": 9,
    }
    assert slider.should_poll is True


def test_the_slider_says_default_while_there_is_too_little_history():
    _hass, _entry, slider = _slider({}, HISTORY[:2])

    assert slider.native_value == 10.0
    assert slider.extra_state_attributes == {
        "uses_default": True, "default_kind": "default",
        "learned_pct": None, "learned_days": 2,
    }


def test_a_fixed_value_still_shows_what_would_be_learned():
    _hass, _entry, slider = _slider({CONF_PREDICTIVE_SAFETY_MARGIN_PCT: 15.0})

    assert slider.native_value == 15.0
    assert slider.extra_state_attributes == {
        "uses_default": False, "default_kind": None,
        "learned_pct": 36.0, "learned_days": 9,
    }


def test_other_sliders_have_no_such_attributes():
    hass, entry, _slider_entity = _slider({})
    other = next(
        d for d in CONFIG_NUMBER_DEFINITIONS if d["key"] != CONF_PREDICTIVE_SAFETY_MARGIN_PCT
    )
    entity = ConfigNumberEntity(hass, entry, other)

    assert entity.extra_state_attributes is None
    assert entity.should_poll is False


async def test_the_action_drops_a_fixed_value():
    hass, entry, slider = _slider({CONF_PREDICTIVE_SAFETY_MARGIN_PCT: 15.0, "other": 1})
    _async_register_services(hass)

    await hass.services.handlers[(DOMAIN, ACTION)](SimpleNamespace(data={}))

    assert entry.data == {"other": 1}
    assert slider.native_value == 36.0
    assert slider.extra_state_attributes["default_kind"] == "measured"


async def test_the_action_leaves_a_learned_entry_alone():
    hass, entry, _slider_entity = _slider({"other": 1})
    untouched = entry.data
    _async_register_services(hass)

    await hass.services.handlers[(DOMAIN, ACTION)](SimpleNamespace(data={}))

    assert entry.data is untouched


def test_the_panel_resets_in_the_value_editor():
    panel = PANEL.read_text(encoding="utf-8")

    assert panel.count('{ key: "predictive_safety_margin_pct"') == 1
    assert f'reset: {{ service: "{ACTION}", lk: "actUseMeasured" }}' in panel
    # The help no longer promises a plain 10 % in any of the six languages.
    helps = [ln for ln in panel.splitlines()
             if ln.lstrip().startswith('predictive_safety_margin_pct: "')]
    assert len(helps) == 6
    assert all("14" in ln and ' 10 %.",' not in ln for ln in helps)


# ----------------------------------------------------------------------
# The first week: the days come from the Recorder
# ----------------------------------------------------------------------

async def test_missing_days_are_restored_from_the_recorder(monkeypatch):
    from datetime import datetime, timezone

    from custom_components.omnibattery import solar_forecast

    tracker, controller = _tracker(monkeypatch)
    tracker._solar_forecast_history = [_day(9.0, 9.0, day="2026-11-22")]  # kept as is
    tracker._hass = SimpleNamespace(config=SimpleNamespace(time_zone="UTC"))
    tracker._now_for_backfill = lambda: datetime(2026, 11, 24, 14, 0, tzinfo=timezone.utc)
    tracker._vacation_periods = []
    monkeypatch.setattr(
        solar_forecast, "get_configured_solar_forecast_sensor",
        lambda _ctrl, source: "sensor.remaining" if source == "remaining" else None,
    )
    monkeypatch.setattr(
        "homeassistant.helpers.entity_registry.async_get",
        lambda _hass: SimpleNamespace(async_get_entity_id=lambda *_a: "sensor.daily_solar"),
    )

    def _state(day, hour, minute, value):
        return SimpleNamespace(
            state=str(value), attributes={"unit_of_measurement": "kWh"},
            last_updated=datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc),
        )

    recorder = {
        # 23rd: carried-over 0 at the start, the day's forecast, then it falls.
        ("sensor.remaining", 23): [_state(23, 0, 0, 0), _state(23, 0, 1, 24.4334),
                                   _state(23, 9, 0, 20.0)],
        ("sensor.daily_solar", 23): [_state(23, 0, 0, 17.72), _state(23, 0, 1, 0.0),
                                     _state(23, 12, 0, 8.0), _state(23, 23, 0, 15.55)],
        # 21st: the forecast did not update in the first hour: left out.
        ("sensor.remaining", 21): [_state(21, 0, 0, 0), _state(21, 6, 0, 6.7)],
        ("sensor.daily_solar", 21): [_state(21, 23, 0, 3.58)],
        # 20th: a forecast, no sun recorded: left out.
        ("sensor.remaining", 20): [_state(20, 0, 2, 12.48)],
    }

    async def _query(_token, entity_id, day):
        return recorder.get((entity_id, day.day))

    tracker._query_recorder_day = _query
    saved = []
    tracker.save_daily_energy = lambda: saved.append(True)

    done = await tracker._async_backfill_solar_forecast_history(
        SimpleNamespace(is_valid=lambda: True)
    )

    assert done is True and saved == [True]
    assert tracker._solar_forecast_history == [
        _day(9.0, 9.0, day="2026-11-22"),
        {"date": "2026-11-23", "forecast_kwh": 24.4334, "actual_kwh": 15.55,
         "curtailed_h": 0.0, "full": True},
    ]
