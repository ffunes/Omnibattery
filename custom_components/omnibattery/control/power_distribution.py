"""Multi-battery PD load-sharing for Marstek Venus.

Owns the power-distribution algorithm extracted from ChargeDischargeController:
- Proportional power allocation across selected batteries (capped at per-battery
  limits, with iterative redistribution of excess).
- Minimum-battery selection with SOC ordering, per-step activation thresholds,
  power/SOC hysteresis, and wall-clock split-load holds.
- Deadband hold release (re-select + re-write when a split hold expires).

The split-load wall-clock holds are private to this module. The *active battery*
lists (``_active_charge_batteries`` / ``_active_discharge_batteries``) remain on
the controller because sensor.py, switch.py, and the main control loop read and
mutate them; this module reads/writes them by reference via ``self._controller``.

Per-battery limit and capacity primitives (``_battery_power_limit``,
``_clamp_to_system_capacity``) stay on the controller — they are entangled with
slot ceilings, voltage taper, and system caps — and are queried here as inputs.
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any

from ..const import (
    MULTI_BATTERY_CHARGE_CROSSOVER_W,
    MULTI_BATTERY_DISCHARGE_CROSSOVER_W,
    MULTI_BATTERY_HYSTERESIS_GAP,
    MULTI_BATTERY_MAX_ACTIVATION,
    MULTI_BATTERY_MIN_ACTIVATION,
    MULTI_BATTERY_SELECTION_HOLD_SECONDS,
)
from .charge_order import charge_allocation_weights, charge_order
from .residual_load import discharge_order_key, primary_coordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class PowerDistribution:
    """Multi-battery load-sharing allocation and selection."""

    def __init__(
        self,
        hass: "HomeAssistant",
        config_entry: "ConfigEntry",
        controller: Any,
    ) -> None:
        self._hass = hass
        self._config_entry = config_entry
        self._controller = controller
        # Wall-clock split-load holds (private to PD load sharing).
        self._charge_selection_hold_until = {}
        self._discharge_selection_hold_until = {}

    def _is_battery_manual_owned(self, coordinator) -> bool:
        """Return whether a battery is outside automatic ownership."""
        helper = getattr(self._controller, "_is_battery_manual_owned", None)
        if helper is not None:
            return bool(helper(coordinator))
        return bool(getattr(coordinator, "battery_manual_mode_enabled", False))

    def _round_to_5w(self, value: float) -> int:
        """Round value to nearest 5W granularity."""
        return round(value / 5) * 5

    def _ordered_batteries_for_operation(
        self,
        available_batteries: list,
        is_charging: bool,
    ) -> list:
        """Return batteries in the order they should be brought in.

        Charging follows the day's outlook (see ``control/charge_order.py``):
        which battery has to *start* first, which is not the same question as
        who gets how many watts.

        Discharging keeps the fullest-first ladder, with a nominated primary
        battery sorted ahead of it so it is the one that serves the house while
        a single battery suffices.
        """
        available_batteries = [
            coordinator for coordinator in available_batteries
            if not self._is_battery_manual_owned(coordinator)
        ]
        if is_charging:
            return charge_order(self._controller, available_batteries)

        primary = primary_coordinator(self._controller)

        def sort_key(coordinator):
            rank = 0 if coordinator is primary else 1
            return (rank, *discharge_order_key(self._controller, coordinator))

        return sorted(available_batteries, key=sort_key)

    def _distribute_power_by_limits(self, total_power: float, available_batteries: list, is_charging: bool) -> dict:
        """Distribute power among batteries, capped at their individual limits.

        Discharging shares out proportionally to each battery's limit, which is
        what it can contribute now. Charging shares by the room each has left
        instead, so the fleet aims at one finish time rather than the fastest
        battery finishing first and the slowest running out of daylight — see
        :func:`~.charge_order.charge_allocation_weights`. Either way a battery
        that would exceed its limit is capped and the excess redistributed.

        Returns dict mapping coordinator -> power (int, rounded to 5W).
        """
        available_batteries = [
            coordinator for coordinator in available_batteries
            if not self._is_battery_manual_owned(coordinator)
        ]
        if not available_batteries:
            return {}

        # Get each battery's individual limit
        limits = {}
        for c in available_batteries:
            limits[c] = self._controller._battery_power_limit(c, is_charging)

        total_capacity = sum(limits.values())
        if total_capacity <= 0:
            return {c: 0 for c in available_batteries}

        # Clamp total request to selected-battery capacity and optional system cap.
        remaining_power = self._controller._clamp_to_system_capacity(
            min(total_power, total_capacity),
            available_batteries,
            is_charging,
        )

        # What each share is measured against: the power limit on discharge,
        # the room left on charge. The cap below stays the power limit either way.
        weights = (
            charge_allocation_weights(available_batteries, limits)
            if is_charging
            else limits
        )

        allocation = {}
        remaining_batteries = list(available_batteries)

        # Iterative allocation: distribute proportionally, cap at limits, redistribute excess
        while remaining_power > 0 and remaining_batteries:
            current_capacity = sum(weights[c] for c in remaining_batteries)
            if current_capacity <= 0:
                break

            all_fit = True
            for c in list(remaining_batteries):
                share = remaining_power * (weights[c] / current_capacity)
                if share >= limits[c]:
                    # This battery is at its limit
                    allocation[c] = self._round_to_5w(limits[c])
                    remaining_power -= limits[c]
                    remaining_batteries.remove(c)
                    all_fit = False

            if all_fit:
                # All remaining batteries can handle their proportional share
                for c in remaining_batteries:
                    share = remaining_power * (weights[c] / current_capacity)
                    allocation[c] = self._round_to_5w(share)
                break

        # Ensure all batteries have an entry
        for c in available_batteries:
            if c not in allocation:
                allocation[c] = 0

        # Preserve the normal selector and proportional distribution exactly.
        # Three-phase protection is a final aggregate cap on that plan: it must
        # never activate an unselected battery or move rejected power elsewhere.
        phase_limiter = getattr(self._controller, "_phase_power_limiter", None)
        if phase_limiter is not None and phase_limiter.enabled:
            all_available = list(available_batteries)
            get_available = getattr(self._controller, "_get_available_batteries", None)
            if get_available is not None:
                try:
                    all_available = list(get_available(is_charging))
                except TypeError:
                    all_available = list(get_available(is_charging, True))
            return phase_limiter.limit_allocation(
                allocation,
                is_charging,
                self._ordered_batteries_for_operation(all_available, is_charging),
            )

        return allocation

    def _select_batteries_for_operation(
        self,
        total_power: float,
        available_batteries: list,
        is_charging: bool
    ) -> list:
        """Select minimum batteries needed for efficient load sharing.

        Activation threshold is derived per step from absolute efficiency crossover
        wattages (where splitting across 2 batteries beats a single battery on η external):
        - Discharge: 1500 W crossover → threshold = 1500 / this_battery_max
        - Charge:    1750 W crossover → threshold = 1750 / this_battery_max
        Clamped to [MIN_ACTIVATION, MAX_ACTIVATION] from const.py.
        Using each battery's own capacity ensures correct behaviour in heterogeneous
        setups (e.g. v3 2500 W + Venus A 1500 W).

        Prioritizes:
        - Discharge: Highest SOC first (drain fullest battery first)
        - Charge: Lowest SOC first (fill emptiest battery first)

        Hysteresis:
        - SOC: Active batteries get 5% effective SOC advantage to avoid ping-pong
        - Power: Deactivation threshold = activation threshold − 10 pp
        """
        available_batteries = [
            coordinator for coordinator in available_batteries
            if not self._is_battery_manual_owned(coordinator)
        ]
        # No power requested: clear load-sharing state. This must run before
        # the single-battery fast path so a one-battery system is not retained
        # as active while the controller is intentionally idle.
        if total_power <= 0:
            self._controller._active_discharge_batteries = []
            self._controller._active_charge_batteries = []
            self._discharge_selection_hold_until.clear()
            self._charge_selection_hold_until.clear()
            return []

        if len(available_batteries) <= 1:
            # Even with a single battery, update tracking state so the Active
            # Batteries diagnostic sensor correctly reflects charging/discharging
            # instead of always showing "Idle".
            selected = list(available_batteries)
            if is_charging:
                self._controller._active_charge_batteries = selected
                self._controller._active_discharge_batteries = []
                self._discharge_selection_hold_until.clear()
                self._charge_selection_hold_until.clear()
            else:
                self._controller._active_discharge_batteries = selected
                self._controller._active_charge_batteries = []
                self._discharge_selection_hold_until.clear()
                self._charge_selection_hold_until.clear()
            return selected

        # Clamp the request to the capacity of currently available batteries.
        total_power = self._controller._clamp_to_system_capacity(
            total_power,
            available_batteries,
            is_charging,
        )

        crossover_w = (
            MULTI_BATTERY_CHARGE_CROSSOVER_W if is_charging
            else MULTI_BATTERY_DISCHARGE_CROSSOVER_W
        )
        activation_threshold = MULTI_BATTERY_MIN_ACTIVATION  # updated per step in loop
        previous_active = (
            self._controller._active_charge_batteries if is_charging
            else self._controller._active_discharge_batteries
        )
        hold_until = (
            self._charge_selection_hold_until if is_charging
            else self._discharge_selection_hold_until
        )
        now = time.monotonic()

        sorted_batteries = self._ordered_batteries_for_operation(
            available_batteries,
            is_charging,
        )

        # Select minimum batteries needed
        selected = []
        combined_capacity = 0

        for battery in sorted_batteries:
            limit = self._controller._battery_power_limit(battery, is_charging)
            if limit <= 0:
                # Can't contribute in this direction (e.g. hardware
                # max_charge/discharge_power register reads 0) — skipping also
                # avoids a ZeroDivisionError below (issue: Venus D idle).
                continue
            selected.append(battery)
            combined_capacity += limit
            activation_threshold = max(
                MULTI_BATTERY_MIN_ACTIVATION,
                min(MULTI_BATTERY_MAX_ACTIVATION, crossover_w / limit)
            )
            if total_power <= combined_capacity * activation_threshold:
                break

        # Power hysteresis: avoid oscillating near the activation threshold.
        if len(available_batteries) > 1 and previous_active:
            # Case A — deactivation hysteresis: the loop dropped a previously-active battery.
            # Only confirm its removal if power fell clearly below the activation threshold;
            # otherwise re-add it so it stays active until the load genuinely drops.
            for battery in previous_active:
                if battery not in selected and battery in available_batteries:
                    limit = self._controller._battery_power_limit(battery, is_charging)
                    if limit <= 0:
                        continue
                    first_limit = (
                        self._controller._battery_power_limit(selected[0], is_charging)
                    ) if selected else limit
                    act_thr = max(MULTI_BATTERY_MIN_ACTIVATION,
                                  min(MULTI_BATTERY_MAX_ACTIVATION, crossover_w / first_limit))
                    deact_thr = max(MULTI_BATTERY_MIN_ACTIVATION, act_thr - MULTI_BATTERY_HYSTERESIS_GAP)
                    if total_power > combined_capacity * deact_thr:
                        selected.append(battery)
                        combined_capacity += limit

            # Case B — activation hysteresis: the loop just added a battery that was not
            # previously active.  Only commit to using it if power is clearly above the
            # threshold; if it is near the boundary, keep a single battery to prevent
            # rapid on/off cycling.
            if len(selected) > 1:
                last = selected[-1]
                if last not in previous_active:
                    last_limit = self._controller._battery_power_limit(last, is_charging)
                    capacity_without_last = combined_capacity - last_limit
                    prev_limit = self._controller._battery_power_limit(selected[-2], is_charging)
                    act_thr_with_hyst = min(
                        max(MULTI_BATTERY_MIN_ACTIVATION,
                            min(MULTI_BATTERY_MAX_ACTIVATION, crossover_w / prev_limit))
                        + MULTI_BATTERY_HYSTERESIS_GAP,
                        MULTI_BATTERY_MAX_ACTIVATION,
                    )
                    if total_power <= capacity_without_last * act_thr_with_hyst:
                        selected.pop()
                        combined_capacity -= last_limit

        split_condition_active = len(selected) > 1
        hold_refreshed = set()

        # Minimum split duration: when the selector decides that more than one
        # battery should participate, refresh a wall-clock hold for every battery
        # in the split. Deadband early returns may skip selector calls, so expiry
        # must be time-based rather than tied to PD write cycles.
        if split_condition_active:
            for battery in selected:
                hold_until[battery] = now + MULTI_BATTERY_SELECTION_HOLD_SECONDS
                hold_refreshed.add(battery)

        for battery in previous_active:
            if (
                battery not in selected
                and battery in available_batteries
                and hold_until.get(battery, 0) > now
            ):
                selected.append(battery)
                held_limit = self._controller._battery_power_limit(battery, is_charging)
                combined_capacity += held_limit
                remaining_seconds = math.ceil(hold_until[battery] - now)
                _LOGGER.debug(
                    "Load sharing [%s]: holding %s active for %d more seconds",
                    "charge" if is_charging else "discharge",
                    battery.name,
                    remaining_seconds,
                )

        for battery in list(hold_until):
            if hold_until[battery] <= now or battery not in selected:
                hold_until.pop(battery, None)

        # Log when selection changes
        if set(selected) != set(previous_active):
            mode = "charge" if is_charging else "discharge"
            _LOGGER.info(
                "Load sharing [%s]: %d/%d batteries active (%s) for %dW "
                "(activation=%.0f%%)",
                mode, len(selected), len(available_batteries),
                ", ".join(c.name for c in selected), int(total_power),
                activation_threshold * 100,
            )

        # Update tracking state: clear opposite list since charge/discharge are mutually exclusive
        if is_charging:
            self._controller._active_charge_batteries = list(selected)
            self._controller._active_discharge_batteries = []
            self._discharge_selection_hold_until.clear()
        else:
            self._controller._active_discharge_batteries = list(selected)
            self._controller._active_charge_batteries = []
            self._charge_selection_hold_until.clear()

        return selected

    async def _rebalance_expired_load_sharing_hold(
        self,
        *,
        grid_w: float,
        target_w: float,
    ) -> bool:
        """Release expired split-load holds even when the PD loop is in deadband."""
        if self._controller.previous_power == 0:
            return False

        is_charging = self._controller.previous_power > 0
        active_batteries = (
            self._controller._active_charge_batteries if is_charging
            else self._controller._active_discharge_batteries
        )
        active_batteries = [
            coordinator for coordinator in active_batteries
            if not self._is_battery_manual_owned(coordinator)
        ]
        if is_charging:
            self._controller._active_charge_batteries = active_batteries
        else:
            self._controller._active_discharge_batteries = active_batteries
        if len(active_batteries) <= 1:
            return False

        hold_until = (
            self._charge_selection_hold_until if is_charging
            else self._discharge_selection_hold_until
        )
        now = time.monotonic()
        if not any(hold_until.get(battery, 0) <= now for battery in active_batteries):
            return False

        available_batteries = self._controller._get_available_batteries(is_charging)
        if not available_batteries:
            return False

        selected_batteries = self._select_batteries_for_operation(
            abs(self._controller.previous_power),
            available_batteries,
            is_charging,
        )
        if set(selected_batteries) == set(active_batteries):
            return False

        requested_power = self._controller.previous_power
        power_allocation = self._distribute_power_by_limits(
            abs(requested_power),
            selected_batteries,
            is_charging,
        )
        assigned_power = sum(power_allocation.values())
        phase_limiter = getattr(self._controller, "_phase_power_limiter", None)
        if phase_limiter is not None and phase_limiter.enabled:
            self._controller.previous_power = (
                assigned_power if is_charging else -assigned_power
            )
        self._controller._log_power_command_plan(
            phase="hold_expired_deadband",
            grid_w=grid_w,
            target_w=target_w,
            previous_power_w=requested_power,
            requested_power_w=requested_power,
            is_charging=is_charging,
            available_batteries=available_batteries,
            selected_batteries=selected_batteries,
            power_allocation=power_allocation,
        )

        allocated_batteries = {
            coordinator
            for coordinator, power in power_allocation.items()
            if power > 0
        }
        for coordinator, power in power_allocation.items():
            if power <= 0:
                continue
            if is_charging:
                await self._controller._set_battery_power(coordinator, power, 0)
            else:
                await self._controller._set_battery_power(coordinator, 0, power)

        for coordinator in self._controller.coordinators:
            if self._is_battery_manual_owned(coordinator):
                continue
            if coordinator not in allocated_batteries:
                await self._controller._set_battery_power(coordinator, 0, 0)

        return True
