"""Which battery is filled first, and how much of the surplus each one gets.

Two different questions, kept apart on purpose:

* :func:`charge_order` decides who has to *start* first, which matters while
  the surplus is too small to activate every battery.
* :func:`charge_allocation_weights` decides how a surplus large enough for
  several of them is split, and the answer is the room each has left rather
  than its power rating.

Sharing by power rating is the wrong shape for charging. Charge power differs by
an order of magnitude between an AC battery and a hybrid inverter, so the
proportional share hands the slow one the smaller slice -- while it is the one
needing the most hours to finish. Observed: a 2.5 kW battery offered 1053 W of a
4 kW surplus beside a 7 kW inverter taking 2947 W; the inverter full by early
afternoon and the other still at 52 % at sunset with a kilowatt going to the
grid. Shares proportional to the room left aim every battery at the same finish
time, which is the earliest any of them can be done. Discharge is untouched:
there the question is who can deliver now, and the power rating answers it.

The order follows the day. With sun enough for all of them, the battery needing
the most hours goes first, because it is the one at risk of not finishing. On a
day the forecast cannot fill them, the DC-coupled one leads instead: its
photovoltaic input never leaves DC, so scarce kilowatt-hours lose the least
there.

Two things about "scarce" that cost real time on the reference installation:

* it is measured against the room in the **DC-coupled battery**, not the room in
  the whole fleet. Asking "is there enough to fill everything" is almost never
  true on a mixed installation, so every day came out scarce and the AC battery
  was passed over on all of them -- while its own empty capacity was most of
  what made the day look scarce. It sat at its 12 % floor taking 0.02 kWh in a
  day; measured against the DC battery's room, the next day it took 13.99 kWh.
  Beyond that room there is nothing left to concentrate, so the day stops being
  scarce at exactly the point the preference stops paying. That also retires the
  hybrid from the front as it fills, with nobody having to command it aside.
* the solar and consumption halves run off **one clock**.
  :func:`read_remaining_solar_kwh` keeps its own ``now``; left to itself it
  converts the solar side against a different minute than the consumption side
  was trimmed to, and a test that pins the clock moves only half the sum.
"""
from __future__ import annotations

import logging

from homeassistant.util import dt as dt_util

from ..const import SCARCITY_HYSTERESIS_KWH
from ..solar_forecast import read_remaining_solar_kwh

_LOGGER = logging.getLogger(__name__)


def _is_dc_coupled(coordinator) -> bool:
    """Whether this battery's photovoltaic input reaches it without an AC stage."""
    return bool(getattr(getattr(coordinator, "driver", None), "dc_coupled", False))


def battery_remaining_kwh(coordinator):
    """How much room a battery still has, in kWh, or None if unknown."""
    capacity = getattr(coordinator, "battery_capacity_kwh", 0) or 0
    if capacity <= 0 and coordinator.data:
        capacity = coordinator.data.get("battery_total_energy") or 0
    if not capacity:
        return None
    soc = coordinator.data.get("battery_soc") if coordinator.data else None
    if soc is None:
        return None
    ceiling = float(getattr(coordinator, "max_soc", 100) or 100)
    return max(0.0, capacity * (ceiling - float(soc)) / 100.0)


def time_to_full_h(controller, coordinator) -> float:
    """Hours of charging at full power before this battery is done.

    The criterion that decides which battery has to start first. Charge power
    differs by an order of magnitude across a mixed fleet, so ordering by state
    of charge says nothing about who is at risk of not finishing before the sun
    goes.
    """
    remaining = battery_remaining_kwh(coordinator)
    if remaining is None:
        return 0.0
    limit = controller._battery_power_limit(coordinator, True)
    if limit <= 0:
        return 0.0
    return remaining / (limit / 1000.0)


def charge_allocation_weights(batteries, limits) -> dict:
    """What each battery's share of a charge surplus is measured against.

    The room it has left, so the fleet finishes together rather than the fastest
    one finishing first and the slowest running out of daylight. Falls back to
    the power limits -- the previous behaviour -- when the room cannot be worked
    out for *every* battery: a mixed sum of watt-hours and watts is not a
    weaker answer, it is a meaningless one.
    """
    weights = {}
    for coordinator in batteries:
        remaining = battery_remaining_kwh(coordinator)
        if remaining is None:
            return dict(limits)
        weights[coordinator] = remaining * 1000.0
    if sum(weights.values()) <= 0:
        # Everything full: nothing to weigh, and dividing by it would raise.
        return dict(limits)
    return weights


def charge_outlook_kwh(controller, now=None):
    """``(surplus still expected today, the room it is measured against)`` in kWh.

    Both halves have to describe the same stretch of day. The solar half comes
    from :func:`read_remaining_solar_kwh`, the integration's one normalized
    answer to "how much is still to come" -- a provider's remaining figure passes
    through untouched, a legacy whole-day sensor is converted once, and either
    unit is handled there. Consumption is cut to the part of its own measurement
    window still ahead, because a whole-day average against a remaining forecast
    tilts every afternoon towards scarcity.

    None while any input is missing, or while no battery is DC-coupled, which
    both mean "no opinion" rather than "scarce".
    """
    tracker = getattr(controller, "_consumption_tracker", None)
    if tracker is None:
        return None

    dc_coupled = [
        coordinator for coordinator in getattr(controller, "coordinators", [])
        if _is_dc_coupled(coordinator)
    ]
    if not dc_coupled:
        # Nothing to concentrate the scarce kilowatt-hours into, so the question
        # does not arise. Measuring against the fleet instead is the trap above.
        return None
    dc_room = sum(battery_remaining_kwh(c) or 0.0 for c in dc_coupled)

    now = now or dt_util.now()
    # One clock for both halves -- see the module docstring.
    solar = read_remaining_solar_kwh(
        controller.hass, controller, now=now, update_controller=False
    )
    # "fallback"/"unsafe_zero" is how that module says it had nothing usable to
    # read. Taking its 0 kWh at face value would call every day scarce.
    if (
        solar is None
        or getattr(solar, "source", None) in (None, "fallback")
        or getattr(solar, "conversion", None) == "unsafe_zero"
    ):
        return None

    now_h = now.hour + now.minute / 60.0
    remaining_consumption = tracker.get_avg_daily_consumption()
    try:
        window_per_day = float(tracker.get_consumption_window_hours_per_day())
        if window_per_day > 0:
            ahead = float(
                tracker.consumption_window_hours_in_range(now_h, tracker.estimate_t_end())
            )
            remaining_consumption *= max(0.0, ahead) / window_per_day
    except Exception:  # noqa: BLE001 - an unlearned tracker keeps the whole-day figure
        pass

    return solar.remaining_kwh - remaining_consumption, dc_room


def scarce_solar_day(controller, now=None) -> bool:
    """Whether today's sun is worth concentrating in the DC-coupled battery.

    True while the expected surplus still fits in it. Beyond that the excess has
    to be shared out regardless, and there is nothing to gain by keeping the
    other batteries waiting.

    Latched: a forecast wanders all day, and without a band the charge order
    would follow it. An unknown outlook leaves the standing verdict alone.
    """
    scarce = bool(getattr(controller, "_scarce_solar_latched", False))
    outlook = charge_outlook_kwh(controller, now=now)
    if outlook is None:
        if not any(
            _is_dc_coupled(coordinator)
            for coordinator in getattr(controller, "coordinators", [])
        ):
            # No DC battery at all: not "unknown", genuinely never scarce, and a
            # latch left standing from a removed battery would outlive it.
            controller._scarce_solar_latched = False
            return False
        return scarce
    surplus, dc_room = outlook
    if scarce:
        if surplus > dc_room + SCARCITY_HYSTERESIS_KWH:
            scarce = False
    elif surplus < dc_room - SCARCITY_HYSTERESIS_KWH:
        scarce = True
    controller._scarce_solar_latched = scarce
    return scarce


def charge_order(controller, batteries) -> list:
    """Batteries in the order they should be filled.

    Ample sun: longest time to full first -- that battery is the one at risk of
    not finishing, and the others can catch up in the time it needs anyway.

    Scarce sun: the DC-coupled one first, because the kilowatt-hours that do
    arrive are worth putting where the least of them is lost to conversion. Once
    the surplus no longer fits it the day counts as ample again, which is how an
    AC-coupled battery gets served without the hybrid ever being told to stand
    down.

    A nominated battery overrides both.
    """
    named = (getattr(controller, "charge_priority", "") or "").strip()
    scarce = scarce_solar_day(controller)
    active = getattr(controller, "_active_charge_batteries", None) or []

    def sort_key(coordinator):
        chosen = 0 if coordinator.name == named else 1
        # A battery already charging keeps a small edge, so two of them with
        # nearly equal claims do not trade places from one cycle to the next.
        head_start = 1.1 if coordinator in active else 1.0
        if scarce:
            efficient = 0 if _is_dc_coupled(coordinator) else 1
            room = (battery_remaining_kwh(coordinator) or 0.0) * head_start
            return (chosen, efficient, -room)
        return (chosen, -time_to_full_h(controller, coordinator) * head_start)

    return sorted(batteries, key=sort_key)
