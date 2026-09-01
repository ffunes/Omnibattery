"""What the batteries actually have to cover, and the guards that ride on it.

A fleet behind one meter is not the only thing drawing on it. Where a second
regulator shares the meter -- a hybrid inverter running its own self-consumption
-- it removes the grid error before the PD loop ever sees one, so this
controller correctly commands nothing and the rest of the storage never runs.
Everything here is built on one reconstruction:

    uncovered = grid + sum(ac_power)

which is what the meter would read if every battery stopped: house consumption
less whatever PV is already supplying, with each battery's own contribution
removed. The *residual demand* is that figure measured against the target
somebody deliberately set, and it is the quantity all three guards act on:

  * the feedforward floors the command at it, either way it points;
  * the surplus guard refuses a discharge while it is negative;
  * the discharge ceiling caps a discharge at it.

Three things this module is careful about, each of which was a real defect:

1. The guards measure against ``compute_active_target()``, never against 0.
   ``curtailment_predischarge`` sets a negative override on purpose to build
   headroom before a negative-price window; guards anchored at 0 veto it
   outright under sun and trim it to house load at night.
2. ``uncovered_load_w`` gives up as soon as *any* configured battery is
   unreadable. A silent battery still delivers -- the hardware holds its last
   command -- so its output stays inside the meter reading while its term drops
   out of the sum, and the demand comes out low by exactly its output. This is a
   whole-fleet reconstruction; a partial one is not a weaker version of it.
3. There is one guard pipeline and one pending derived from it, rather than a
   pending per guard. The deadband shortcut assumes a grid on target needs no
   action, which is exactly wrong here: it is on target *because* the other
   regulator is carrying the load. ``guards_pending`` runs the same pipeline
   over the standing command and asks whether it would move, so a guard added to
   the pipeline later cannot be silently swallowed by the shortcut.
"""
from __future__ import annotations

import logging

from ..const import (
    GUARD_PENDING_TOLERANCE_W,
    SURPLUS_GUARD_HYSTERESIS_W,
)
from ..energy import effective_total_discharging_energy

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# the reconstruction
# ---------------------------------------------------------------------------
def _battery_ac_power_w(coordinator):
    """A battery's contribution at the AC bus, or None if it cannot be read.

    Positive while discharging. ``battery_power`` carries the opposite sign and
    stands in for drivers that report no AC figure of their own.
    """
    if not getattr(coordinator, "is_available", False) or not coordinator.data:
        return None
    ac = coordinator.data.get("ac_power")
    if ac is None:
        battery_power = coordinator.data.get("battery_power")
        ac = -battery_power if battery_power is not None else None
    return None if ac is None else float(ac)


def uncovered_load_w(controller, grid_w):
    """What the meter would read if every battery stopped, in watts.

    Negative while PV more than covers the house. None as soon as any configured
    battery is unreadable -- see the module docstring.
    """
    if grid_w is None:
        return None
    coordinators = list(getattr(controller, "coordinators", []))
    if not coordinators:
        return None
    total = float(grid_w)
    for coordinator in coordinators:
        contribution = _battery_ac_power_w(coordinator)
        if contribution is None:
            return None
        total += contribution
    return total


def residual_demand_w(controller, grid_w):
    """The load the batteries are meant to cover, in watts, or None.

    The uncovered load measured against the target somebody set rather than
    against zero. A deliberate export target (predischarge before a negative
    price window, a negative ``pd_target_grid_power``) is a demand on the
    batteries just as a house is, and anchoring at 0 instead cancels it.
    """
    uncovered = uncovered_load_w(controller, grid_w)
    if uncovered is None:
        return None
    return uncovered - float(controller.compute_active_target())


def grid_reading_w(controller):
    """The grid figure the diagnostics report against, in watts, or None.

    ``previous_sensor`` is the cycle's own reading, but it is cleared whenever
    another manager takes the wheel -- a max-SOC charge, for instance -- and a
    diagnostic that blanks out exactly when something interesting is happening is
    no diagnostic. Falls back to reading the configured meter directly.
    """
    reading = getattr(controller, "previous_sensor", None)
    if reading is not None:
        return reading
    entity_id = getattr(controller, "consumption_sensor", None)
    if not entity_id:
        return None
    state = controller.hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", None):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return -value if getattr(controller, "meter_inverted", False) else value


# ---------------------------------------------------------------------------
# which battery
# ---------------------------------------------------------------------------
def _is_manual_owned(controller, coordinator) -> bool:
    """Whether a battery is the user's to command rather than this controller's."""
    helper = getattr(controller, "_is_battery_manual_owned", None)
    if helper is not None:
        return bool(helper(coordinator))
    return bool(getattr(coordinator, "battery_manual_mode_enabled", False))


def automatic_batteries(controller) -> list:
    """The batteries the distribution will actually command."""
    return [
        coordinator for coordinator in getattr(controller, "coordinators", [])
        if not _is_manual_owned(controller, coordinator)
    ]


def discharge_order_key(controller, coordinator):
    """Sort key for the discharge ladder: fullest first, and sticky.

    The battery already discharging carries a 5 % edge, so two of them within
    that of each other do not trade places from one cycle to the next. Shared
    with :func:`primary_coordinator` rather than repeated there: an automatic
    primary picked on raw SOC would outrank the edge and reintroduce exactly the
    swapping it exists to prevent.
    """
    data = coordinator.data or {}
    soc = data.get("battery_soc", 50)
    if soc is None:
        soc = 50
    active = getattr(controller, "_active_discharge_batteries", None) or []
    is_active = coordinator in active
    effective_soc = float(soc) + (5.0 if is_active else 0.0)
    energy = effective_total_discharging_energy(data) or 0
    return (-effective_soc, energy - (2.5 if is_active else 0.0))


def primary_coordinator(controller):
    """The battery that serves the house first, nominated or chosen.

    Left on automatic -- or naming a battery that is no longer configured, or one
    the user has taken into manual mode -- this is whichever battery the ordinary
    discharge ordering would have picked anyway, hysteresis included. Switching
    the feedforward on without nominating anything therefore changes *when* a
    battery is asked, not which one.

    A battery under manual ownership is never the primary, nominated or not: the
    distribution leaves it out of the ladder, so a feedforward measured against
    its rating would be handed to the batteries that were left.

    None only when no battery can serve at all.
    """
    name = (getattr(controller, "primary_battery", "") or "").strip()
    batteries = automatic_batteries(controller)
    if name:
        for coordinator in batteries:
            if coordinator.name == name:
                return coordinator
    able = [
        coordinator for coordinator in batteries
        if controller._battery_power_limit(coordinator, False) > 0
    ]
    if not able:
        return None
    return min(able, key=lambda c: discharge_order_key(controller, c))


# ---------------------------------------------------------------------------
# the feedforward
# ---------------------------------------------------------------------------
def feedforward_candidate_w(controller, grid_w) -> float:
    """The floor the fleet should already be at, signed, whether or not it is on.

    Negative discharges, positive charges, 0 asks for nothing. Computed
    regardless of the switch so the figure can be checked against a meter before
    anyone commits to it; :func:`feedforward_w` is what the cycle acts on.

    The two directions are capped differently on purpose. A discharge is one
    battery's job -- the primary's -- so it is capped at that battery's rating. A
    surplus is a system figure the distribution shares out afterwards, so it is
    capped at what the fleet can absorb between them: capped at the head
    battery's own rating instead, that battery receives only its share of its own
    limit (2418 W of a 2500 W rating on the reference installation, with 6.8 kW
    of surplus going past it).

    The surplus claimed is the *uncovered* one, and that bound is load-bearing. A
    battery behind the same meter is ordinary household load to another regulator
    on it, so a charge command is never refused: whatever is asked for gets
    covered, from the sun if it is there and from the other battery if it is not.
    Asking for more than the real surplus pumps one battery into the other
    through two conversions with the meter sitting at zero.
    """
    demand = residual_demand_w(controller, grid_w)
    if demand is None:
        return 0.0
    if demand > 0:
        primary = primary_coordinator(controller)
        if primary is None:
            return 0.0
        limit = controller._battery_power_limit(primary, False)
        if limit <= 0:
            return 0.0
        return -float(min(demand, limit))
    if demand < 0:
        room = sum(
            controller._battery_power_limit(coordinator, True)
            for coordinator in automatic_batteries(controller)
        )
        if room <= 0:
            return 0.0
        return float(min(-demand, room))
    return 0.0


def feedforward_w(controller, grid_w) -> float:
    """The feedforward the control cycle acts on: zero while the switch is off."""
    if not getattr(controller, "primary_feedforward_enabled", False):
        return 0.0
    return feedforward_candidate_w(controller, grid_w)


def _apply_feedforward(controller, new_power, grid_w):
    """Floor the command at the real demand, whichever way it points.

    A floor, not a target: the loop may always ask for more, and every guard
    below plus every downstream blocker still has the last word.
    """
    floor = feedforward_w(controller, grid_w)
    if floor < 0 and new_power > floor:
        _LOGGER.debug(
            "Primary feedforward: raising %.0fW to %.0fW to cover the demand",
            new_power, floor,
        )
        return floor
    if floor > 0 and new_power < floor:
        _LOGGER.debug(
            "Primary feedforward: raising %.0fW to %.0fW to take the surplus",
            new_power, floor,
        )
        return floor
    return new_power


# ---------------------------------------------------------------------------
# the surplus guard
# ---------------------------------------------------------------------------
def surplus_blocks_discharge(controller, grid_w) -> bool:
    """Whether there is enough to spare that discharging would be waste.

    Discharging into a surplus is never right: the roof is already covering the
    house, so the energy leaving the battery can only charge another battery or
    go to the grid, and either way it has made a round trip for nothing. The
    meter alone cannot tell -- with a second regulator on it, one battery
    charging and another discharging cancel out and the grid reads zero, which
    the deadband then holds.

    Latching, with a band on the way in and none on the way out. A bare sign test
    would chatter through every cloud edge; release is immediate once the demand
    turns positive, because by then the house genuinely needs the battery and
    making it wait would import instead.
    """
    demand = residual_demand_w(controller, grid_w)
    latched = bool(getattr(controller, "_surplus_guard_latched", False))
    if demand is None:
        # No reading is not evidence either way; the last verdict stands.
        return latched
    band = max(float(getattr(controller, "deadband", 0) or 0), SURPLUS_GUARD_HYSTERESIS_W)
    if latched:
        if demand > 0:
            latched = False
    elif demand < -band:
        latched = True
    controller._surplus_guard_latched = latched
    return latched


def _apply_surplus_guard(controller, new_power, grid_w):
    """Refuse a discharge that the surplus is already covering."""
    # Evaluated even while charging: the latch tracks what the meter says, not
    # what was commanded, and freezing it through a charge would hand a stale
    # verdict to the first discharge that follows.
    blocked = surplus_blocks_discharge(controller, grid_w)
    if new_power >= 0 or not blocked:
        return new_power
    _LOGGER.debug(
        "Surplus guard: dropping %.0fW of discharge -- the demand is %.0fW short of "
        "needing it",
        abs(new_power), abs(residual_demand_w(controller, grid_w) or 0),
    )
    return 0


# ---------------------------------------------------------------------------
# the discharge ceiling
# ---------------------------------------------------------------------------
def _apply_discharge_ceiling(controller, new_power, grid_w):
    """Cap a discharge at the demand the house has actually left uncovered.

    The PD loop chases the raw meter, so anything else drawing on that meter
    becomes this controller's job -- including a second battery being charged by
    its own energy manager. Covering that discharges one battery into the other
    through two conversions, and where the other manager fills the gap from the
    grid, it is paid for twice. Observed at 21:39 with no sun: the EMMA drew
    211 W to charge the hybrid while the AC battery discharged 912 W against a
    787 W house.

    Left alone when the demand cannot be read, and when there is a surplus --
    that case belongs to the guard above, which vetoes rather than caps.
    """
    if new_power >= 0:
        return new_power
    demand = residual_demand_w(controller, grid_w)
    if demand is None or demand <= 0:
        return new_power
    # A deadband of slack, so measurement noise around the demand does not clamp
    # the command on and off every cycle.
    slack = float(getattr(controller, "deadband", 0) or 0)
    if -new_power <= demand + slack:
        return new_power
    _LOGGER.debug(
        "Discharge ceiling: trimming %.0fW to %.0fW -- the rest is not the house's load",
        -new_power, demand,
    )
    return -float(demand)


# ---------------------------------------------------------------------------
# the pipeline, and the one question the deadband has to ask it
# ---------------------------------------------------------------------------
def apply_guards(controller, new_power, grid_w):
    """Floor, then veto, then cap -- the single pipeline every caller uses.

    Order matters: the guard runs after the feedforward so it can veto that too,
    and the ceiling runs after the guard so a veto stands.
    """
    new_power = _apply_feedforward(controller, new_power, grid_w)
    new_power = _apply_surplus_guard(controller, new_power, grid_w)
    return _apply_discharge_ceiling(controller, new_power, grid_w)


def guards_pending(controller, grid_w) -> bool:
    """Whether the standing command needs revisiting despite a quiet meter.

    The deadband and stale-sample shortcuts both assume a meter on target means
    nothing to do. Here it can be on target *because* another regulator is
    carrying the load -- the very situation these guards exist to change -- so
    the question has to be put to the guards themselves: run the pipeline over
    the command already in force and see whether it would move it. Derived from
    :func:`apply_guards` rather than written per guard, so a guard added there
    later cannot be silently swallowed by the shortcut.
    """
    standing = float(getattr(controller, "previous_power", 0) or 0)
    guarded = apply_guards(controller, standing, grid_w)
    return abs(guarded - standing) > GUARD_PENDING_TOLERANCE_W
