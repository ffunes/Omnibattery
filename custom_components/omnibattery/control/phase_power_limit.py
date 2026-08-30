"""Per-phase AC current safety limits for three-phase installations.

The global controller intentionally knows nothing about the phase meters. This
module is a safety envelope around automatic battery assignments: it reconstructs
the non-battery phase current from the live RMS current reading and the measured
AC battery power, then limits the next battery order in either direction.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BATTERY_PHASE,
    CONF_METER_INVERTED,
    CONF_PHASE_1_CURRENT_SENSOR,
    CONF_PHASE_1_FUSE_SIZE,
    CONF_PHASE_2_CURRENT_SENSOR,
    CONF_PHASE_2_FUSE_SIZE,
    CONF_PHASE_3_CURRENT_SENSOR,
    CONF_PHASE_3_FUSE_SIZE,
    CONF_SLOT_ENABLED,
    CONF_SLOT_MODE,
    CONF_THREE_PHASE_ENABLED,
    CONF_TIME_SLOTS,
    DEFAULT_THREE_PHASE_ENABLED,
    MAX_SENSOR_STALE_S,
    PHASE_BATTERY_POWER_FACTOR,
    PHASE_CONFIG,
    PHASE_L1,
    PHASE_L2,
    PHASE_L3,
    PHASE_NOMINAL_VOLTAGE_V,
    PHASE_UNASSIGNED,
    PHASE_VALUES,
    SLOT_MODE_MANUAL,
)

_LOGGER = logging.getLogger(__name__)

PHASE_SENSOR_KEYS = {
    PHASE_L1: CONF_PHASE_1_CURRENT_SENSOR,
    PHASE_L2: CONF_PHASE_2_CURRENT_SENSOR,
    PHASE_L3: CONF_PHASE_3_CURRENT_SENSOR,
}
PHASE_LIMIT_KEYS = {
    PHASE_L1: CONF_PHASE_1_FUSE_SIZE,
    PHASE_L2: CONF_PHASE_2_FUSE_SIZE,
    PHASE_L3: CONF_PHASE_3_FUSE_SIZE,
}
PHASE_LABELS = {PHASE_L1: "L1", PHASE_L2: "L2", PHASE_L3: "L3"}
ROUNDING_W = 5


@dataclass(frozen=True)
class PhaseSensorReading:
    """Normalized phase current reading and its safety status."""

    value_a: float | None
    reason: str | None = None
    age_s: float | None = None


def _state_timestamp(state: Any) -> datetime | None:
    """Return the newest publication timestamp available on a HA state."""
    if state is None:
        return None
    return getattr(state, "last_reported", None) or getattr(
        state, "last_updated", None
    )


def normalize_current_sensor_state(
    state: Any,
    *,
    meter_inverted: bool = False,
    now: datetime | None = None,
    max_age_s: float = MAX_SENSOR_STALE_S,
) -> PhaseSensorReading:
    """Normalize an A/mA sensor using the integration's grid-meter convention.

    Positive values mean import and negative values mean export.  A missing
    timestamp is accepted for duck-typed unit tests and old HA state objects;
    current Home Assistant ``State`` objects always provide one.
    """
    if state is None:
        return PhaseSensorReading(None, "sensor_not_found")

    raw_state = getattr(state, "state", None)
    if raw_state in (None, "unknown", "unavailable"):
        return PhaseSensorReading(None, "sensor_unavailable")

    try:
        value = float(raw_state)
    except (TypeError, ValueError):
        return PhaseSensorReading(None, "sensor_not_numeric")
    if not math.isfinite(value):
        return PhaseSensorReading(None, "sensor_not_numeric")

    attributes = getattr(state, "attributes", {}) or {}
    unit = attributes.get("unit_of_measurement")
    if unit == "mA":
        value /= 1000.0
    elif unit != "A":
        return PhaseSensorReading(None, "sensor_invalid_unit")

    timestamp = _state_timestamp(state)
    age_s = None
    if timestamp is not None:
        reference = now or dt_util.utcnow()
        try:
            age_s = max(0.0, (reference - timestamp).total_seconds())
        except (TypeError, ValueError):
            return PhaseSensorReading(None, "sensor_invalid_timestamp")
        if age_s > max_age_s:
            return PhaseSensorReading(None, "sensor_stale", age_s)

    if meter_inverted:
        value = -value
    return PhaseSensorReading(value, age_s=age_s)


def calculate_phase_budgets(
    grid_a: float,
    battery_current_a: float,
    limit_a: float,
) -> dict[str, float]:
    """Return base current and safe charge/discharge budgets for one phase.

    ``battery_current_a`` follows the controller convention: positive is charge
    and negative is discharge. The grid reading already includes that battery
    current, hence the explicit base-current reconstruction. The configured
    phase limit is also an absolute cap on the battery current equivalent in
    either direction; the base-current calculation can reduce that cap but
    must never enlarge it.
    """
    base_a = float(grid_a) - float(battery_current_a)
    limit = max(0.0, float(limit_a))

    # The phase meter must stay inside [-limit, +limit]. Battery current uses
    # the controller sign (+charge/-discharge), so the meter constraint gives
    # the signed battery interval [(-limit - base), (limit - base)]. Apply the
    # same absolute limit to the battery command itself and intersect both
    # intervals before exposing directional magnitudes to the distributor.
    battery_min_a = max(-limit, -limit - base_a)
    battery_max_a = min(limit, limit - base_a)
    return {
        "base_a": base_a,
        "charge_budget_a": max(0.0, battery_max_a),
        "discharge_budget_a": max(0.0, -battery_min_a),
    }


def _battery_current_from_power(power_w: float) -> float:
    """Estimate signed battery AC current from its active-power telemetry.

    Battery commands and telemetry are in active watts, while the phase safety
    sensor measures RMS current. The fixed nominal-voltage/power-factor pair is
    deliberately conservative and keeps the user-facing configuration focused
    on the actual fuse rating.
    """
    watts_per_amp = PHASE_NOMINAL_VOLTAGE_V * PHASE_BATTERY_POWER_FACTOR
    if watts_per_amp <= 0:
        return 0.0
    return float(power_w) / watts_per_amp


def _current_budget_to_power(budget_a: float) -> float:
    """Convert an available RMS-current budget to a conservative watt cap."""
    return max(0.0, float(budget_a)) * PHASE_NOMINAL_VOLTAGE_V * PHASE_BATTERY_POWER_FACTOR


def _round_down(value: float, granularity: int = ROUNDING_W) -> int:
    """Round a safety value down so rounding can never cross a limit."""
    if value <= 0:
        return 0
    return int(math.floor((float(value) + 1e-9) / granularity) * granularity)


class PhasePowerLimiter:
    """Read phase current telemetry and constrain automatic assignments."""

    def __init__(
        self,
        hass: Any,
        config_entry: Any,
        controller: Any | None = None,
        *,
        max_age_s: float = MAX_SENSOR_STALE_S,
        rounding_w: int = ROUNDING_W,
    ) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self.controller = controller
        self.max_age_s = max_age_s
        self.rounding_w = rounding_w
        self.enabled = False
        self.meter_inverted = False
        self._phase_settings: dict[str, tuple[str | None, float]] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._planned: dict[Any, tuple[bool, int]] = {}
        self._limited_batteries: dict[Any, dict[str, Any]] = {}
        self._last_log_signature: dict[str, tuple[Any, ...]] = {}
        self._manual_warning_created = False
        self.refresh_config()

    @property
    def phase_values(self) -> tuple[str, ...]:
        """Normalized phase values accepted by runtime configuration."""
        return PHASE_VALUES

    def refresh_config(self) -> None:
        """Reload configuration and update coordinator phase metadata."""
        data = getattr(self.config_entry, "data", {}) or {}
        self.enabled = bool(data.get(CONF_THREE_PHASE_ENABLED, DEFAULT_THREE_PHASE_ENABLED))
        self.meter_inverted = bool(data.get(CONF_METER_INVERTED, False))
        self._phase_settings = {}
        for phase in PHASE_VALUES:
            sensor_key, limit_key = PHASE_CONFIG[phase]
            raw_limit = data.get(limit_key)
            try:
                limit = float(raw_limit)
            except (TypeError, ValueError):
                limit = 0.0
            self._phase_settings[phase] = (data.get(sensor_key), limit)

        for coordinator in getattr(self.controller, "coordinators", []) or []:
            if hasattr(coordinator, "_config_entry"):
                battery_data = next(
                    (
                        battery
                        for battery in data.get("batteries", [])
                        if battery.get("host") == getattr(coordinator, "host", None)
                        and battery.get("port") == getattr(coordinator, "port", None)
                        and battery.get("slave_id", 1)
                        == getattr(coordinator, "slave_id", 1)
                    ),
                    None,
                )
                if battery_data is not None:
                    phase = battery_data.get(CONF_BATTERY_PHASE)
                    coordinator.phase = (
                        phase if phase in PHASE_VALUES else PHASE_UNASSIGNED
                    )

    def begin_cycle(self) -> None:
        """Forget distribution plans from the previous control cycle."""
        self._planned.clear()
        self._limited_batteries.clear()

    def _battery_phase(self, coordinator: Any) -> str | None:
        phase = getattr(coordinator, "phase", None)
        if phase not in PHASE_VALUES:
            phase = getattr(coordinator, CONF_BATTERY_PHASE, None)
        return phase if phase in PHASE_VALUES else None

    def _phase_has_batteries(self, phase: str) -> bool:
        """Return whether a battery is assigned to a physical phase."""
        return any(
            self._battery_phase(coordinator) == phase
            for coordinator in getattr(self.controller, "coordinators", []) or []
        )

    def _battery_power(self, coordinator: Any) -> float:
        """Read measured AC power in the controller's +charge/-discharge form."""
        if self.controller is not None:
            getter = getattr(self.controller, "_coordinator_delivered_power", None)
            if getter is not None:
                try:
                    value = getter(coordinator)
                    if value is not None and math.isfinite(float(value)):
                        return float(value)
                except (TypeError, ValueError):
                    pass

        data = getattr(coordinator, "data", None) or {}
        ac_power = data.get("ac_power")
        if ac_power is not None:
            try:
                return -float(ac_power)
            except (TypeError, ValueError):
                return 0.0
        battery_power = data.get("battery_power")
        try:
            return float(battery_power) if battery_power is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _read_phase(self, phase: str) -> tuple[PhaseSensorReading, float]:
        sensor_id, _ = self._phase_settings.get(phase, (None, 0.0))
        state = self.hass.states.get(sensor_id) if sensor_id else None
        reading = normalize_current_sensor_state(
            state,
            meter_inverted=self.meter_inverted,
            max_age_s=self.max_age_s,
        )
        battery_power = sum(
            self._battery_power(coordinator)
            for coordinator in getattr(self.controller, "coordinators", []) or []
            if self._battery_phase(coordinator) == phase
        )
        return reading, battery_power

    def phase_snapshot(self, phase: str) -> dict[str, Any]:
        """Return the current phase calculation, including degradation reason."""
        sensor_id, limit = self._phase_settings.get(phase, (None, 0.0))
        snapshot: dict[str, Any] = {
            "phase": phase,
            "sensor": sensor_id,
            "configured": bool(sensor_id) and limit > 0,
            "reading_a": None,
            "limit_a": limit if limit > 0 else None,
            "base_a": None,
            "charge_budget_a": 0.0,
            "discharge_budget_a": 0.0,
            "charge_budget_w": 0.0,
            "discharge_budget_w": 0.0,
            "assigned_power_w": 0.0,
            "requested_power_w": 0.0,
            "degraded": False,
            "reason": None,
        }
        if not self.enabled:
            snapshot["reason"] = "disabled"
            self._snapshots[phase] = snapshot
            return snapshot
        if not sensor_id and limit <= 0:
            snapshot["reason"] = "not_configured"
            self._snapshots[phase] = snapshot
            return snapshot
        if not sensor_id or limit <= 0:
            snapshot.update({"degraded": True, "reason": "invalid_configuration"})
            self._log_state_change(snapshot)
            self._snapshots[phase] = snapshot
            return snapshot

        reading, battery_power = self._read_phase(phase)
        if reading.value_a is None:
            snapshot.update({"degraded": True, "reason": reading.reason})
            self._log_state_change(snapshot)
            self._snapshots[phase] = snapshot
            return snapshot

        budgets = calculate_phase_budgets(
            reading.value_a,
            _battery_current_from_power(battery_power),
            limit,
        )
        snapshot.update(
            {
                "reading_a": reading.value_a,
                "limit_a": limit,
                "base_a": budgets["base_a"],
                "charge_budget_a": budgets["charge_budget_a"],
                "discharge_budget_a": budgets["discharge_budget_a"],
                "charge_budget_w": _round_down(
                    _current_budget_to_power(budgets["charge_budget_a"]),
                    self.rounding_w,
                ),
                "discharge_budget_w": _round_down(
                    _current_budget_to_power(budgets["discharge_budget_a"]),
                    self.rounding_w,
                ),
            }
        )
        self._snapshots[phase] = snapshot
        return snapshot

    def all_snapshots(self) -> dict[str, dict[str, Any]]:
        """Refresh and return all phase diagnostics."""
        return {phase: self.phase_snapshot(phase) for phase in PHASE_VALUES}

    def has_degraded_phase(self) -> bool:
        """Return whether any configured phase currently fails safe."""
        if not self.enabled:
            return False
        return any(
            snapshot["degraded"]
            and (
                snapshot.get("configured", False)
                or self._phase_has_batteries(phase)
            )
            for phase, snapshot in self.all_snapshots().items()
        )

    def _individual_limit(self, coordinator: Any, is_charging: bool) -> float:
        if self.controller is not None:
            getter = getattr(self.controller, "_battery_power_limit", None)
            if getter is not None:
                try:
                    return max(0.0, float(getter(coordinator, is_charging)))
                except (TypeError, ValueError):
                    return 0.0
        key = "max_charge_power" if is_charging else "max_discharge_power"
        try:
            return max(0.0, float(getattr(coordinator, key, 0)))
        except (TypeError, ValueError):
            return 0.0

    def _record_limited_battery(
        self,
        coordinator: Any,
        phase: str,
        requested: float,
        assigned: float,
        is_charging: bool,
        reason: str = "phase_limit",
    ) -> None:
        """Remember a battery whose automatic order was reduced by this guard."""
        requested = max(0.0, float(requested))
        assigned = max(0.0, min(requested, float(assigned)))
        if phase not in PHASE_VALUES or assigned >= requested:
            self._limited_batteries.pop(coordinator, None)
            return

        self._limited_batteries[coordinator] = {
            "battery": str(getattr(coordinator, "name", coordinator)),
            "phase": phase,
            "direction": "charging" if is_charging else "discharging",
            "requested_power_w": int(requested),
            "assigned_power_w": int(assigned),
            "limited_power_w": int(requested - assigned),
            "reason": reason or "phase_limit",
        }

    def _record_allocation_limits(
        self,
        requested_allocation: dict[Any, float],
        allocation: dict[Any, int],
        is_charging: bool,
    ) -> None:
        """Record per-battery reductions after overflow has been redistributed."""
        for coordinator, requested in requested_allocation.items():
            phase = self._battery_phase(coordinator)
            requested_power = max(0.0, float(requested))
            assigned_power = max(0.0, float(allocation.get(coordinator, 0)))
            if phase not in PHASE_VALUES:
                self._limited_batteries.pop(coordinator, None)
                continue

            if assigned_power < requested_power:
                snapshot = self._snapshots.get(phase) or self.phase_snapshot(phase)
                reason = (
                    snapshot.get("reason")
                    if snapshot.get("degraded")
                    else "phase_limit"
                )
                self._record_limited_battery(
                    coordinator,
                    phase,
                    requested_power,
                    assigned_power,
                    is_charging,
                    reason or "phase_limit",
                )
            else:
                self._limited_batteries.pop(coordinator, None)

    def _cap_group_allocation(
        self,
        total: int,
        requested: dict[Any, int],
    ) -> dict[Any, int]:
        """Scale an existing plan down without changing its participating set."""
        limits = {
            coordinator: _round_down(value, self.rounding_w)
            for coordinator, value in requested.items()
        }
        capacity = sum(limits.values())
        total = min(max(0, int(total)), capacity)
        if total <= 0 or capacity <= 0:
            return {coordinator: 0 for coordinator in requested}

        allocation = {
            coordinator: _round_down(
                total * limits[coordinator] / capacity,
                self.rounding_w,
            )
            for coordinator in requested
        }
        remaining = total - sum(allocation.values())
        # Fill whole increments in the original plan order without allowing any
        # battery to exceed the power chosen by the normal distributor.
        while remaining >= self.rounding_w:
            changed = False
            for coordinator in requested:
                room = limits[coordinator] - allocation[coordinator]
                if room >= self.rounding_w and remaining >= self.rounding_w:
                    allocation[coordinator] += self.rounding_w
                    remaining -= self.rounding_w
                    changed = True
            if not changed:
                break
        return allocation

    def limit_allocation(
        self,
        requested_allocation: dict[Any, float],
        is_charging: bool,
        available_batteries: list[Any] | None = None,
    ) -> dict[Any, int]:
        """Cap protected phases and leave unassigned batteries unrestricted."""
        allocation = {coordinator: 0 for coordinator in requested_allocation}
        if not self.enabled:
            return {
                coordinator: max(0, int(power))
                for coordinator, power in requested_allocation.items()
            }

        # A battery explicitly marked Unassigned is outside this safety
        # envelope. Preserve its normal allocation so enabling phase protection
        # does not change its operation.
        for coordinator in requested_allocation:
            if self._battery_phase(coordinator) not in PHASE_VALUES:
                allocation[coordinator] = max(
                    0, int(requested_allocation[coordinator])
                )

        for phase in PHASE_VALUES:
            phase_request = {
                coordinator: max(0, int(power))
                for coordinator, power in requested_allocation.items()
                if self._battery_phase(coordinator) == phase
            }
            if not phase_request:
                continue

            snapshot = self.phase_snapshot(phase)
            requested_total = sum(phase_request.values())
            if snapshot["degraded"]:
                budget = 0
            elif snapshot.get("reason") == "not_configured":
                budget = requested_total
            else:
                budget = int(
                    snapshot[
                        "charge_budget_w" if is_charging else "discharge_budget_w"
                    ]
                )
            group_alloc = self._cap_group_allocation(
                min(requested_total, budget),
                phase_request,
            )
            for coordinator, value in group_alloc.items():
                allocation[coordinator] = value
            assigned = sum(group_alloc.values())
            snapshot["requested_power_w"] = requested_total
            snapshot["assigned_power_w"] = (
                assigned if is_charging else -assigned
            )
            if assigned < requested_total:
                self._log_limit(snapshot, requested_total, assigned)

        # Restore the normal plan's aggregate request using only spare capacity
        # on healthy phases. Existing selected batteries keep priority; additional
        # batteries are activated only when a capped phase left real overflow.
        remaining = _round_down(
            sum(max(0, int(power)) for power in requested_allocation.values())
            - sum(allocation.values()),
            self.rounding_w,
        )
        candidates: list[Any] = list(requested_allocation)
        for coordinator in available_batteries or []:
            if coordinator not in candidates:
                candidates.append(coordinator)
                allocation[coordinator] = 0

        for coordinator in candidates:
            if remaining < self.rounding_w:
                break
            phase = self._battery_phase(coordinator)
            if phase not in PHASE_VALUES:
                battery_room = _round_down(
                    self._individual_limit(coordinator, is_charging)
                    - allocation[coordinator],
                    self.rounding_w,
                )
                extra = min(remaining, battery_room)
                if extra <= 0:
                    continue
                allocation[coordinator] += extra
                remaining -= extra
                continue
            snapshot = self._snapshots.get(phase) or self.phase_snapshot(phase)
            if snapshot["degraded"]:
                continue
            assigned_on_phase = sum(
                power
                for battery, power in allocation.items()
                if self._battery_phase(battery) == phase
            )
            if snapshot.get("reason") == "not_configured":
                phase_room = remaining
            else:
                budget = int(
                    snapshot[
                        "charge_budget_w" if is_charging else "discharge_budget_w"
                    ]
                )
                phase_room = _round_down(
                    budget - assigned_on_phase,
                    self.rounding_w,
                )
            battery_room = _round_down(
                self._individual_limit(coordinator, is_charging)
                - allocation[coordinator],
                self.rounding_w,
            )
            extra = min(remaining, phase_room, battery_room)
            if extra <= 0:
                continue
            allocation[coordinator] += extra
            remaining -= extra

        # Refresh final per-phase assigned values after overflow placement.
        for phase, snapshot in self._snapshots.items():
            if phase not in PHASE_VALUES:
                continue
            assigned = sum(
                power
                for coordinator, power in allocation.items()
                if self._battery_phase(coordinator) == phase
            )
            snapshot["assigned_power_w"] = assigned if is_charging else -assigned

        self._record_allocation_limits(
            requested_allocation,
            allocation,
            is_charging,
        )
        self._planned.update({
            coordinator: (is_charging, value)
            for coordinator, value in allocation.items()
        })
        active = [coordinator for coordinator, value in allocation.items() if value > 0]
        if self.controller is not None:
            if is_charging:
                self.controller._active_charge_batteries = active
            else:
                self.controller._active_discharge_batteries = active
        for snapshot in self._snapshots.values():
            self._log_state_change(snapshot)
        return allocation

    def _commanded_direction_power(self, coordinator: Any, is_charging: bool) -> float:
        key = "commanded_charge_power" if is_charging else "commanded_discharge_power"
        try:
            return max(0.0, float(getattr(coordinator, key, 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def limit_single_command(
        self,
        coordinator: Any,
        charge_power: float,
        discharge_power: float,
    ) -> tuple[int, int]:
        """Apply the aggregate phase budget to a non-distribution command."""
        if not self.enabled or (charge_power <= 0 and discharge_power <= 0):
            return max(0, int(charge_power)), max(0, int(discharge_power))
        if charge_power > 0 and discharge_power > 0:
            return 0, 0

        is_charging = charge_power > 0
        requested = charge_power if is_charging else discharge_power
        planned = self._planned.get(coordinator)
        if (
            planned is not None
            and planned[0] == is_charging
            and abs(float(planned[1]) - float(requested)) < self.rounding_w
        ):
            return (planned[1], 0) if is_charging else (0, planned[1])

        phase = self._battery_phase(coordinator)
        if phase not in PHASE_VALUES:
            own_limit = _round_down(
                self._individual_limit(coordinator, is_charging), self.rounding_w
            )
            allowed = _round_down(min(float(requested), own_limit), self.rounding_w)
            self._limited_batteries.pop(coordinator, None)
            return (allowed, 0) if is_charging else (0, allowed)
        snapshot = self.phase_snapshot(phase)
        if snapshot["degraded"]:
            snapshot["requested_power_w"] = requested
            snapshot["assigned_power_w"] = 0
            self._record_limited_battery(
                coordinator,
                phase,
                requested,
                0,
                is_charging,
                snapshot.get("reason") or "phase_degraded",
            )
            return 0, 0

        own_limit = _round_down(
            self._individual_limit(coordinator, is_charging), self.rounding_w
        )
        if snapshot.get("reason") == "not_configured":
            allowed = _round_down(
                min(float(requested), own_limit),
                self.rounding_w,
            )
            self._limited_batteries.pop(coordinator, None)
            return (allowed, 0) if is_charging else (0, allowed)

        budget = (
            snapshot["charge_budget_w"]
            if is_charging
            else snapshot["discharge_budget_w"]
        )
        other_power = sum(
            self._commanded_direction_power(other, is_charging)
            for other in getattr(self.controller, "coordinators", []) or []
            if (
                other is not coordinator
                and self._battery_phase(other) == phase
                and not self.controller._is_battery_manual_owned(other)
            )
        )
        allowed = _round_down(
            min(float(requested), max(0.0, float(budget) - other_power), own_limit),
            self.rounding_w,
        )
        phase_available = max(0.0, float(budget) - other_power)
        if phase_available < requested:
            self._record_limited_battery(
                coordinator,
                phase,
                requested,
                allowed,
                is_charging,
            )
            snapshot["requested_power_w"] = requested
            snapshot["assigned_power_w"] = allowed if is_charging else -allowed
            self._log_limit(snapshot, requested, allowed)
        else:
            self._limited_batteries.pop(coordinator, None)
        return (allowed, 0) if is_charging else (0, allowed)

    def _log_state_change(self, snapshot: dict[str, Any]) -> None:
        phase = snapshot.get("phase", "?")
        signature = (
            snapshot.get("degraded"),
            snapshot.get("reason"),
            round(float(snapshot.get("assigned_power_w") or 0) / 50),
            round(float(snapshot.get("requested_power_w") or 0) / 50),
        )
        previous = self._last_log_signature.get(phase)
        if previous == signature:
            return
        self._last_log_signature[phase] = signature
        if snapshot.get("degraded"):
            _LOGGER.warning(
                "Three-phase current protection %s degraded: sensor=%s reason=%s; "
                "automatic assignments on this phase are limited to 0 W",
                PHASE_LABELS.get(phase, phase),
                snapshot.get("sensor"),
                snapshot.get("reason"),
            )
        elif previous and previous[0]:
            _LOGGER.info(
                "Three-phase current protection %s recovered: reading=%.1fA limit=%.1fA",
                PHASE_LABELS.get(phase, phase),
                snapshot.get("reading_a") or 0,
                snapshot.get("limit_a") or 0,
            )

    def _log_limit(
        self,
        snapshot: dict[str, Any],
        requested: float,
        allowed: float,
    ) -> None:
        phase = snapshot.get("phase", "?")
        signature = (
            round(float(snapshot.get("reading_a") or 0) * 2),
            round(float(snapshot.get("limit_a") or 0) * 2),
            round(float(requested) / 50),
            round(float(allowed) / 50),
        )
        log_key = f"limit:{phase}"
        if self._last_log_signature.get(log_key) == signature:
            return
        self._last_log_signature[log_key] = signature
        _LOGGER.info(
            "Three-phase current protection %s limit active: reading=%.1fA limit=%.1fA "
            "request=%.0fW result=%.0fW",
            PHASE_LABELS.get(phase, phase),
            snapshot.get("reading_a") or 0,
            snapshot.get("limit_a") or 0,
            requested,
            allowed,
        )

    def diagnostics(self) -> dict[str, Any]:
        """Return configuration and current per-phase safety state."""
        phases = self.all_snapshots()
        phase_batteries = {phase: [] for phase in PHASE_VALUES}
        unassigned_batteries: list[str] = []
        coordinators = list(getattr(self.controller, "coordinators", []) or [])
        for coordinator in coordinators:
            name = str(getattr(coordinator, "name", coordinator))
            phase = self._battery_phase(coordinator)
            if phase in phase_batteries:
                phase_batteries[phase].append(name)
            else:
                unassigned_batteries.append(name)

        for phase, snapshot in phases.items():
            snapshot["batteries"] = phase_batteries.get(phase, [])

        limited_details = [
            dict(self._limited_batteries[coordinator])
            for coordinator in coordinators
            if coordinator in self._limited_batteries
        ]
        degraded_phases = [
            phase
            for phase, snapshot in phases.items()
            if snapshot.get("degraded")
            and (snapshot.get("configured") or phase_batteries.get(phase))
        ]
        if not self.enabled:
            state = "disabled"
            limited_details = []
        elif degraded_phases:
            state = "degraded"
        elif limited_details:
            state = "limiting"
        else:
            state = "active"

        return {
            "state": state,
            "enabled": self.enabled,
            "protection_enabled": self.enabled,
            "meter_inverted": self.meter_inverted,
            "limited_batteries": [
                detail["battery"] for detail in limited_details
            ],
            "limited_battery_details": limited_details,
            "unassigned_batteries": unassigned_batteries,
            "degraded_phases": degraded_phases,
            "sensors": {
                PHASE_SENSOR_KEYS[phase]: self._phase_settings.get(phase, (None, 0.0))[0]
                for phase in PHASE_VALUES
            },
            "fuse_sizes_a": {
                PHASE_LIMIT_KEYS[phase]: self._phase_settings.get(phase, (None, 0.0))[1]
                for phase in PHASE_VALUES
            },
            "current_conversion": {
                "nominal_voltage_v": PHASE_NOMINAL_VOLTAGE_V,
                "battery_power_factor": PHASE_BATTERY_POWER_FACTOR,
                "watts_per_amp": PHASE_NOMINAL_VOLTAGE_V * PHASE_BATTERY_POWER_FACTOR,
            },
            "phases": phases,
            "manual_mode_warning": self._manual_warning_created,
        }

    def _has_manual_time_slot(self) -> bool:
        """Return whether an enabled manual operation slot is configured."""
        data = getattr(self.config_entry, "data", {}) or {}
        slots = data.get(CONF_TIME_SLOTS, [])
        if isinstance(slots, dict):
            slots = [slots]
        if not isinstance(slots, list):
            return False
        return any(
            isinstance(slot, dict)
            and slot.get(CONF_SLOT_ENABLED, True)
            and slot.get(CONF_SLOT_MODE) == SLOT_MODE_MANUAL
            for slot in slots
        )

    def update_manual_mode_warning(self, entry_id: str, enabled: bool) -> None:
        """Expose active manual-register or manual-slot bypasses in Repairs."""
        issue_id = f"three_phase_manual_mode_{entry_id}"
        if self.enabled and (enabled or self._has_manual_time_slot()):
            ir.async_create_issue(
                self.hass,
                "omnibattery",
                issue_id,
                is_fixable=False,
                is_persistent=True,
                issue_domain="omnibattery",
                severity=ir.IssueSeverity.WARNING,
                translation_key="three_phase_manual_mode",
            )
            self._manual_warning_created = True
        else:
            ir.async_delete_issue(self.hass, "omnibattery", issue_id)
            self._manual_warning_created = False
