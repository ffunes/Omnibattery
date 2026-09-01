"""Tests for which battery is filled first, and with how much.

Charge power differs by an order of magnitude between an AC battery and a hybrid
inverter. Sharing a surplus by power limit hands the slow one the smaller share
-- while it is the one needing the most hours to finish. Measured on the
reference installation: the 7 kW inverter full by early afternoon, the 2.5 kW
battery still at 52 % at sunset with a kilowatt going to the grid.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.control import charge_order as mod
from custom_components.omnibattery.control.charge_order import (
    battery_remaining_kwh,
    charge_order,
    charge_outlook_kwh,
    scarce_solar_day,
    time_to_full_h,
)


class _Battery:
    """A stand-in coordinator.

    A class rather than a namespace: the distributor keys its allocation
    dictionaries by coordinator.
    """

    def __init__(self, name, *, capacity, soc, limit_w, dc_coupled=False, max_soc=100):
        self.name = name
        self.battery_capacity_kwh = capacity
        self.max_soc = max_soc
        self.data = {"battery_soc": soc}
        self.is_available = True
        self.driver = SimpleNamespace(dc_coupled=dc_coupled)
        self._limit_w = limit_w


def _battery(name, **kwargs):
    return _Battery(name, **kwargs)


def _controller(
    batteries, *, forecast=None, avg_consumption=20.0, priority="",
    unit="kWh", t_end=20.0, window_per_day=24.0, hours_ahead=None,
):
    """A controller stub carrying the pieces the outlook reads.

    ``hours_ahead`` is what the tracker reports as consumption-window hours still
    to come; left None it is derived from the clock, which is what makes the
    horizon tests sensitive to the time of day.
    """
    def _hours_in_range(now_h, end_h):
        if hours_ahead is not None:
            return hours_ahead
        return max(0.0, end_h - now_h)

    return SimpleNamespace(
        coordinators=batteries,
        charge_priority=priority,
        _scarce_solar_latched=False,
        _active_charge_batteries=[],
        _solar_t_start=8.0,
        solar_forecast_sensor="sensor.forecast" if forecast is not None else None,
        _consumption_tracker=SimpleNamespace(
            get_avg_daily_consumption=lambda: avg_consumption,
            estimate_t_end=lambda: t_end,
            get_solar_fraction_done=lambda now_h, t_start, end_h: (
                0.0 if end_h <= (t_start or 0.0)
                else max(0.0, min(1.0, (now_h - (t_start or 0.0)) / (end_h - (t_start or 0.0))))
            ),
            get_consumption_window_hours_per_day=lambda: window_per_day,
            consumption_window_hours_in_range=_hours_in_range,
        ),
        hass=SimpleNamespace(states=SimpleNamespace(
            get=lambda _eid: SimpleNamespace(
                state=str(forecast), attributes={"unit_of_measurement": unit}
            )
            if forecast is not None else None
        )),
        _battery_power_limit=lambda coordinator, is_charging: coordinator._limit_w,
    )


def _at(monkeypatch, hour):
    """Pin the clock. Both halves of the outlook run off it, so an unpinned test
    follows whatever hour the suite happens to run at."""
    monkeypatch.setattr(
        mod.dt_util, "now",
        lambda: datetime(2026, 8, 25, hour, 0, tzinfo=timezone.utc),
    )


def _reference():
    """The reference installation mid-afternoon: hybrid full, AC battery half."""
    return [
        _battery("Marstek", capacity=15.36, soc=52, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=100, limit_w=7000, dc_coupled=True),
    ]


# ----------------------------------------------------------------------
# how long each battery still needs
# ----------------------------------------------------------------------
def test_remaining_energy_respects_the_configured_ceiling():
    battery = _battery("Marstek", capacity=15.36, soc=50, limit_w=2500, max_soc=90)
    assert battery_remaining_kwh(battery) == pytest.approx(6.144)


def test_a_full_battery_wants_nothing():
    assert battery_remaining_kwh(_reference()[1]) == 0.0


def test_hours_to_full_is_energy_over_power_not_state_of_charge():
    """The 7 kW inverter at 40 % is quicker than the 2.5 kW battery at 60 %."""
    controller = _controller([])
    slow = _battery("Marstek", capacity=15.36, soc=60, limit_w=2500)
    fast = _battery("Huawei", capacity=13.8, soc=40, limit_w=7000, dc_coupled=True)
    assert time_to_full_h(controller, slow) == pytest.approx(2.458, abs=0.01)
    assert time_to_full_h(controller, fast) == pytest.approx(1.183, abs=0.01)
    # Lower state of charge, yet done sooner -- which is the whole point.
    assert time_to_full_h(controller, fast) < time_to_full_h(controller, slow)


def test_a_battery_that_cannot_charge_needs_no_hours():
    blocked = _battery("Marstek", capacity=15.36, soc=10, limit_w=0)
    assert time_to_full_h(_controller([]), blocked) == 0.0


# ----------------------------------------------------------------------
# the order
# ----------------------------------------------------------------------
def test_with_sun_to_spare_the_slow_battery_goes_first(monkeypatch):
    _at(monkeypatch, 8)
    batteries = [
        _battery("Marstek", capacity=15.36, soc=20, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=20, limit_w=7000, dc_coupled=True),
    ]
    controller = _controller(batteries, forecast=60.0, hours_ahead=24.0)
    assert [c.name for c in charge_order(controller, batteries)] == ["Marstek", "Huawei"]


def test_on_a_thin_day_the_efficient_battery_goes_first(monkeypatch):
    """Scarce kilowatt-hours belong where the least of them is lost."""
    _at(monkeypatch, 8)
    batteries = [
        _battery("Marstek", capacity=15.36, soc=20, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=20, limit_w=7000, dc_coupled=True),
    ]
    controller = _controller(batteries, forecast=5.0, hours_ahead=0.0)
    assert [c.name for c in charge_order(controller, batteries)] == ["Huawei", "Marstek"]


def test_a_nominated_battery_overrides_both_rules(monkeypatch):
    _at(monkeypatch, 8)
    batteries = _reference()
    controller = _controller(batteries, forecast=5.0, priority="Marstek", hours_ahead=0.0)
    assert charge_order(controller, batteries)[0].name == "Marstek"


def test_a_wandering_forecast_does_not_reshuffle_the_order(monkeypatch):
    """A forecast moves all day; the verdict may not follow every step."""
    _at(monkeypatch, 8)
    batteries = [
        _battery("Marstek", capacity=10.0, soc=0, limit_w=2500),
        _battery("Huawei", capacity=10.0, soc=0, limit_w=7000, dc_coupled=True),
    ]
    controller = _controller(batteries, forecast=31.0, hours_ahead=24.0)

    def _forecast(value):
        return lambda _eid: SimpleNamespace(
            state=str(value), attributes={"unit_of_measurement": "kWh"}
        )

    # The measure is the hybrid's 10 kWh of room, not the fleet's 20. Against
    # 20 kWh of consumption a 31 kWh forecast leaves 11: ample, but only just.
    controller.hass.states.get = _forecast(31.0)
    assert scarce_solar_day(controller) is False

    for forecast in (30.5, 29.5, 30.0, 29.0):
        controller.hass.states.get = _forecast(forecast)
        assert scarce_solar_day(controller) is False, forecast

    # Clearly short of the need, by more than the band: now it flips.
    controller.hass.states.get = _forecast(20.0)
    assert scarce_solar_day(controller) is True


def test_without_a_forecast_there_is_no_opinion():
    controller = _controller(_reference(), forecast=None)
    assert scarce_solar_day(controller) is False


# ----------------------------------------------------------------------
# what the day is measured against
#
# The scarce branch concentrates the surplus in the DC-coupled battery, so
# "is today scarce" has to be asked about *that* battery's room. Asked about the
# fleet it answers yes on almost every mixed installation, and the AC-coupled
# battery is passed over for good.
# ----------------------------------------------------------------------
def test_a_day_is_scarce_only_while_the_surplus_fits_the_dc_battery(monkeypatch):
    _at(monkeypatch, 8)
    batteries = [
        _battery("Marstek", capacity=15.36, soc=12, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=89, limit_w=7000, dc_coupled=True),
    ]
    controller = _controller(batteries, forecast=10.0, avg_consumption=12.0,
                             hours_ahead=0.0)
    # 1.5 kWh of room in the hybrid; the Marstek's 13.5 kWh is not the measure.
    assert charge_outlook_kwh(controller)[1] == pytest.approx(1.52, abs=0.05)


def test_the_reference_installation_stops_calling_every_day_scarce(monkeypatch):
    """Huawei at 89 %, Marstek at 12 %, 10 kWh of sun still ahead.

    The live case: measured against the fleet's 15 kWh of room this came out
    scarce, the hybrid went first and the Marstek took 0.02 kWh in a day.
    Measured against the hybrid's room, the next day it took 13.99 kWh.
    """
    _at(monkeypatch, 8)
    batteries = [
        _battery("Marstek", capacity=15.36, soc=12, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=89, limit_w=7000, dc_coupled=True),
    ]
    controller = _controller(batteries, forecast=10.0, avg_consumption=12.0,
                             hours_ahead=0.0)
    assert scarce_solar_day(controller) is False
    # Ample: longest to fill goes first, which is how the Marstek gets served
    # without the hybrid ever being commanded to stand down.
    assert charge_order(controller, batteries)[0].name == "Marstek"


def test_a_genuinely_thin_day_still_fills_the_hybrid_first(monkeypatch):
    """Less sun than the hybrid alone can hold: concentrate, don't split."""
    _at(monkeypatch, 8)
    batteries = [
        _battery("Marstek", capacity=15.36, soc=12, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=30, limit_w=7000, dc_coupled=True),
    ]
    # 4 kWh ahead against 9.7 kWh of room in the hybrid.
    controller = _controller(batteries, forecast=4.0, avg_consumption=12.0,
                             hours_ahead=0.0)
    assert scarce_solar_day(controller) is True
    assert charge_order(controller, batteries)[0].name == "Huawei"


def test_an_empty_ac_battery_cannot_make_the_day_scarce_by_itself(monkeypatch):
    """The trap that kept the Marstek at its floor: its own empty capacity was
    most of what made the day look scarce, so the emptier it got the more
    certain it was to be skipped again."""
    _at(monkeypatch, 8)
    full_hybrid = _battery("Huawei", capacity=13.8, soc=98, limit_w=7000,
                           dc_coupled=True)
    for soc in (50, 20, 5):
        batteries = [_battery("Marstek", capacity=15.36, soc=soc, limit_w=2500),
                     full_hybrid]
        controller = _controller(batteries, forecast=6.0, avg_consumption=12.0,
                                 hours_ahead=0.0)
        assert scarce_solar_day(controller) is False, f"at SOC {soc}"


def test_the_hybrid_hands_over_as_it_fills(monkeypatch):
    """No command to the hybrid is needed -- its shrinking room does it."""
    _at(monkeypatch, 8)
    seen = []
    for soc in (10, 50, 95):
        batteries = [
            _battery("Marstek", capacity=15.36, soc=12, limit_w=2500),
            _battery("Huawei", capacity=13.8, soc=soc, limit_w=7000, dc_coupled=True),
        ]
        # 4 kWh still to come: it fits the hybrid's room at 10 % and at 50 %,
        # but not the 0.7 kWh it has left at 95 %.
        controller = _controller(batteries, forecast=4.0, avg_consumption=12.0,
                                 hours_ahead=0.0)
        seen.append(charge_order(controller, batteries)[0].name)
    assert seen == ["Huawei", "Huawei", "Marstek"]


def test_without_a_dc_battery_the_day_is_never_scarce(monkeypatch):
    """An all-AC fleet has nothing to concentrate into, so the question does not
    arise -- and asking it about the fleet's own room is the trap above."""
    _at(monkeypatch, 8)
    batteries = [
        _battery("A", capacity=10.0, soc=0, limit_w=2500),
        _battery("B", capacity=10.0, soc=50, limit_w=2500),
    ]
    controller = _controller(batteries, forecast=1.0, avg_consumption=12.0,
                             hours_ahead=0.0)
    assert charge_outlook_kwh(controller) is None
    assert scarce_solar_day(controller) is False
    # A latch left standing from a battery that has since gone must not outlive it.
    controller._scarce_solar_latched = True
    assert scarce_solar_day(controller) is False


# ----------------------------------------------------------------------
# the forecast: unit, and which stretch of day it describes
#
# The setup accepts a forecast sensor in kWh or Wh, so the raw state cannot be
# read as kWh and left at that. Both figures also have to describe the same
# stretch of day: a whole-day forecast against a whole-day consumption average
# answers a question about this morning when it is asked at six in the evening.
# ----------------------------------------------------------------------
def _outlook(monkeypatch, hour=8, **kwargs):
    _at(monkeypatch, hour)
    batteries = [
        _battery("Marstek", capacity=10.0, soc=0, limit_w=2500),
        _battery("Huawei", capacity=10.0, soc=0, limit_w=7000, dc_coupled=True),
    ]
    return charge_outlook_kwh(_controller(batteries, **kwargs))


def test_a_forecast_in_watt_hours_is_not_taken_for_kilowatt_hours(monkeypatch):
    """A thousandfold error, and it would call every day ample."""
    surplus_kwh, room = _outlook(monkeypatch, forecast=30.0, unit="kWh", hours_ahead=24.0)
    surplus_wh, _ = _outlook(monkeypatch, forecast=30000.0, unit="Wh", hours_ahead=24.0)
    assert surplus_kwh == pytest.approx(surplus_wh)
    # The hybrid's room, which is what scarcity is measured against -- the pair
    # holds 20 kWh between them.
    assert room == pytest.approx(10.0)


def test_a_foreign_unit_is_refused_rather_than_guessed_at(monkeypatch):
    """Neither kWh nor Wh: the shared reader declines, and so does the outlook."""
    assert _outlook(monkeypatch, forecast=30.0, unit="MWh") is None


def test_the_consumption_still_ahead_is_taken_off_the_solar_left(monkeypatch):
    """What this module owns: the reader supplies the sun, we net off the load."""
    # Nothing of the window behind us yet, so the reader hands back all 30 kWh.
    # Half the consumption window is still ahead: 10 of the 20 kWh daily average.
    surplus, _ = _outlook(
        monkeypatch, forecast=30.0, avg_consumption=20.0,
        hours_ahead=12.0, window_per_day=24.0,
    )
    assert surplus == pytest.approx(20.0, abs=0.02)


def test_the_same_forecast_says_less_as_the_day_goes_on(monkeypatch):
    """Without production figures the elapsed part of the window stands in."""
    def at(hour):
        # Production window 08:00-20:00, consumption window the whole day.
        return _outlook(monkeypatch, hour, forecast=30.0, avg_consumption=12.0)[0]

    at_eight, at_two, at_seven = at(8), at(14), at(19)
    assert at_eight > at_two > at_seven

    # At 08:00 the whole 30 kWh is still ahead, and only the twelve hours of
    # consumption up to sunset count against it -- 6 of the 12 kWh daily average.
    assert at_eight == pytest.approx(24.0, abs=0.5)
    # Half the production window gone: 15 kWh left against 3 kWh still to use.
    assert at_two == pytest.approx(12.0, abs=0.5)
    # An hour before sunset there is almost nothing left to plan with.
    assert at_seven == pytest.approx(2.0, abs=0.5)


def test_no_forecast_sensor_yields_no_opinion(monkeypatch):
    assert _outlook(monkeypatch, forecast=None) is None


def test_an_unreadable_forecast_yields_no_opinion(monkeypatch):
    _at(monkeypatch, 8)
    controller = _controller(
        [_battery("A", capacity=10.0, soc=0, limit_w=1000, dc_coupled=True)],
        forecast=30.0,
    )
    controller.hass.states.get = lambda _eid: SimpleNamespace(
        state="unavailable", attributes={}
    )
    assert charge_outlook_kwh(controller) is None


def test_a_remaining_forecast_sensor_is_taken_as_it_stands(monkeypatch):
    """It already answers the question, so it passes through untouched."""
    _at(monkeypatch, 14)
    batteries = [_battery("Huawei", capacity=10.0, soc=0, limit_w=7000, dc_coupled=True)]
    controller = _controller(batteries, forecast=8.0, avg_consumption=12.0,
                             hours_ahead=0.0)
    controller.solar_forecast_remaining_sensor = "sensor.remaining"
    controller.hass.states.get = lambda eid: SimpleNamespace(
        state="8.0" if eid == "sensor.remaining" else "23.0",
        attributes={"unit_of_measurement": "kWh"},
    )
    assert charge_outlook_kwh(controller)[0] == pytest.approx(8.0)


def test_a_remaining_sensor_in_watt_hours_is_converted_too(monkeypatch):
    _at(monkeypatch, 14)
    batteries = [_battery("Huawei", capacity=10.0, soc=0, limit_w=7000, dc_coupled=True)]
    controller = _controller(batteries, forecast=8.0, avg_consumption=12.0,
                             hours_ahead=0.0)
    controller.solar_forecast_remaining_sensor = "sensor.remaining"
    controller.hass.states.get = lambda eid: SimpleNamespace(
        state="8000", attributes={"unit_of_measurement": "Wh"}
    )
    assert charge_outlook_kwh(controller)[0] == pytest.approx(8.0)


def test_a_legacy_whole_day_forecast_is_converted_to_what_is_left(monkeypatch):
    """The legacy path, which is what most installations still have. Half way
    through an 08:00-20:00 window, half the day's figure counts as produced."""
    _at(monkeypatch, 14)
    batteries = [_battery("Huawei", capacity=10.0, soc=0, limit_w=7000, dc_coupled=True)]
    controller = _controller(batteries, forecast=23.0, avg_consumption=12.0,
                             hours_ahead=0.0)
    assert charge_outlook_kwh(controller)[0] == pytest.approx(11.5, abs=0.05)


def test_one_clock_drives_both_halves(monkeypatch):
    """The reader keeps its own ``now``; passed one explicitly, pinning the clock
    moves the solar side and the consumption side together."""
    _at(monkeypatch, 8)
    batteries = [_battery("Huawei", capacity=10.0, soc=0, limit_w=7000, dc_coupled=True)]
    controller = _controller(batteries, forecast=24.0, avg_consumption=24.0,
                             t_end=20.0, window_per_day=24.0)
    # 08:00: the whole 24 kWh of sun ahead, 12 of the 24 hours of consumption.
    assert charge_outlook_kwh(controller)[0] == pytest.approx(12.0, abs=0.1)
    # 14:00, one clock: half the sun gone (12 kWh) and half those hours with it
    # (6 kWh). Two clocks would have moved only one of the two.
    _at(monkeypatch, 14)
    assert charge_outlook_kwh(controller)[0] == pytest.approx(6.0, abs=0.1)


# ----------------------------------------------------------------------
# how the surplus is split
# ----------------------------------------------------------------------
def _distributor(batteries):
    from custom_components.omnibattery.control.power_distribution import PowerDistribution

    controller = _controller(batteries)
    controller._clamp_to_system_capacity = lambda power, _b, _c: power
    selector = PowerDistribution.__new__(PowerDistribution)
    selector._controller = controller
    selector._is_battery_manual_owned = lambda _c: False
    return selector


def _split(batteries, watts):
    allocation = _distributor(batteries)._distribute_power_by_limits(
        watts, batteries, is_charging=True
    )
    return {c.name: allocation[c] for c in batteries}


def test_the_split_follows_the_room_left_not_the_power_rating():
    """4 kW across a 2.5 kW battery with room and a 7 kW one nearly full.

    By power rating the slow one was offered 1053 W of this while the inverter
    took 2947 W -- and was still at 52 % at sunset with a kilowatt exported.
    """
    batteries = [
        _battery("Marstek", capacity=15.36, soc=20, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=90, limit_w=7000, dc_coupled=True),
    ]
    split = _split(batteries, 4000)
    assert split["Marstek"] == 2500
    assert split["Huawei"] == 1500


def test_two_alike_batteries_still_share_evenly():
    batteries = [
        _battery("A", capacity=10.0, soc=50, limit_w=1000),
        _battery("B", capacity=10.0, soc=50, limit_w=1000),
    ]
    assert _split(batteries, 1000) == {"A": 500, "B": 500}


def test_batteries_are_aimed_at_finishing_together():
    """Shares proportional to the room left means one finish time, not two."""
    batteries = [
        _battery("Marstek", capacity=15.36, soc=67.4, limit_w=2500),   # 5.0 kWh left
        _battery("Huawei", capacity=13.8, soc=0, limit_w=7000, dc_coupled=True),
    ]
    split = _split(batteries, 4000)
    hours = {
        "Marstek": 5.0 / (split["Marstek"] / 1000),
        "Huawei": 13.8 / (split["Huawei"] / 1000),
    }
    assert hours["Marstek"] == pytest.approx(hours["Huawei"], rel=0.02)


def test_a_full_battery_is_passed_over():
    assert _split(_reference(), 2000) == {"Marstek": 2000, "Huawei": 0}


def test_nothing_is_left_on_the_table():
    """Surplus stops flowing only once every battery is at its limit."""
    batteries = [
        _battery("Marstek", capacity=15.36, soc=0, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=0, limit_w=7000, dc_coupled=True),
    ]
    assert sum(_split(batteries, 6000).values()) == 6000


def test_an_unknown_capacity_falls_back_to_the_old_share():
    batteries = [
        _battery("A", capacity=0, soc=50, limit_w=1000),
        _battery("B", capacity=0, soc=50, limit_w=3000),
    ]
    for battery in batteries:
        battery.data = {}
    assert _split(batteries, 2000) == {"A": 500, "B": 1500}


def test_one_unknown_capacity_takes_the_whole_split_back_to_ratings():
    """Mixing watt-hours and watts in one normalization is not a weaker answer,
    it is a meaningless one."""
    known = _battery("A", capacity=10.0, soc=50, limit_w=1000)
    unknown = _battery("B", capacity=0, soc=50, limit_w=3000)
    unknown.data = {}
    assert _split([known, unknown], 2000) == {"A": 500, "B": 1500}


def test_discharge_still_shares_by_power():
    """Only charging changed; discharge is a question of who can deliver now."""
    batteries = [
        _battery("Marstek", capacity=15.36, soc=50, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=50, limit_w=7000, dc_coupled=True),
    ]
    allocation = _distributor(batteries)._distribute_power_by_limits(
        4750, batteries, is_charging=False
    )
    assert {c.name: allocation[c] for c in batteries} == {"Marstek": 1250, "Huawei": 3500}


def test_the_head_battery_reaches_its_own_rating():
    """What the surplus feedforward is for. Mid-morning on the reference
    installation: the hybrid at 97 % with almost no room, the AC battery at 21 %
    with 12 kWh of it. A system figure capped at 2500 W left the AC battery on
    2418 -- its share of its own rating -- while 6.8 kW of surplus went past."""
    batteries = [
        _battery("Marstek", capacity=15.36, soc=21, limit_w=2500),
        _battery("Huawei", capacity=13.8, soc=97, limit_w=7000, dc_coupled=True),
    ]
    assert _split(batteries, 2500)["Marstek"] == 2420    # the old, capped figure
    assert _split(batteries, 6828)["Marstek"] == 2500    # the whole surplus


# ----------------------------------------------------------------------
# the discharge ladder is untouched apart from the primary
# ----------------------------------------------------------------------
def _ladder(batteries, primary="", is_charging=False, **kwargs):
    from custom_components.omnibattery.control.power_distribution import PowerDistribution

    controller = _controller(batteries, **kwargs)
    controller.primary_battery = primary
    controller._active_discharge_batteries = []
    selector = PowerDistribution.__new__(PowerDistribution)
    selector._controller = controller
    selector._is_battery_manual_owned = lambda _c: False
    return [
        c.name
        for c in selector._ordered_batteries_for_operation(batteries, is_charging)
    ]


def test_the_selector_brings_batteries_in_by_hours_not_by_state_of_charge(monkeypatch):
    """The ordering reaches the selector, not just the helper: the emptier
    hybrid still finishes sooner, so the slow battery has to start first."""
    _at(monkeypatch, 8)
    batteries = [
        _battery("Marstek", capacity=15.36, soc=30, limit_w=2500),   # 4.3 h
        _battery("Huawei", capacity=13.8, soc=10, limit_w=7000, dc_coupled=True),  # 1.8 h
    ]
    assert _ladder(batteries, is_charging=True, forecast=60.0, hours_ahead=0.0) == [
        "Marstek", "Huawei",
    ]


def test_discharge_normally_starts_with_the_fullest():
    batteries = [
        _battery("Huawei", capacity=13.8, soc=40, limit_w=7000, dc_coupled=True),
        _battery("Marstek", capacity=15.36, soc=55, limit_w=2500),
    ]
    assert _ladder(batteries) == ["Marstek", "Huawei"]


def test_a_nominated_primary_goes_first_even_when_emptier():
    batteries = [
        _battery("Huawei", capacity=13.8, soc=90, limit_w=7000, dc_coupled=True),
        _battery("Marstek", capacity=15.36, soc=20, limit_w=2500),
    ]
    assert _ladder(batteries, primary="Marstek") == ["Marstek", "Huawei"]
