"""Diagnostics for Omnibattery config entries.

Home Assistant calls :func:`async_get_config_entry_diagnostics` when the user
presses *Download diagnostics* on the integration. It returns a JSON-serialisable
dump of connection health, driver traits and non-responsive-tracker state
(everything that otherwise lives only in transient logs), with host/serial and
connection credentials redacted.
"""
from __future__ import annotations

import math
from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Identifiers and user sensor references that could deanonymise the dump.
# async_redact_data recurses into nested dicts/lists, so per-battery "host"
# entries inside a batteries list are covered too.
# Configured sensor entity ids are intentionally kept: they are often enough
# to diagnose an issue and do not expose connection or authentication details.
TO_REDACT = {
    "host",
    "username",
    "password",
    "serial",
    "serial_port",
    "ip_address",
    "mac",
}


def _safe_number(value: object) -> float | None:
    """Return a finite float, or ``None`` for an unavailable value."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rounded_number(value: object, digits: int = 3) -> float | None:
    """Return a rounded finite number suitable for a diagnostics payload."""
    number = _safe_number(value)
    return round(number, digits) if number is not None else None


def _value_from_sources(
    sources: tuple[object | None, ...], names: tuple[str, ...]
) -> tuple[object | None, str | None]:
    """Read the first available attribute/key from a list of runtime objects."""
    for source in sources:
        if source is None:
            continue
        for name in names:
            if isinstance(source, dict):
                if name in source and source[name] is not None:
                    return source[name], name
            else:
                value = getattr(source, name, None)
                if value is not None:
                    return value, name
    return None, None


def _json_safe(value: object) -> object:
    """Make best-effort diagnostics values JSON serialisable.

    Diagnostics should remain useful while the controller is starting, being
    reconfigured, or represented by a lightweight test double. In particular,
    blocker records can contain datetimes and arbitrary detail values.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


def _serialise_blockers(registry: object) -> dict[str, object]:
    """Return a JSON-safe copy of an operation-blocker registry."""
    if not isinstance(registry, dict):
        return {}
    result: dict[str, object] = {}
    for source, record in registry.items():
        source_key = str(source)
        if isinstance(record, dict):
            result[source_key] = {
                "reason": record.get("reason") or source_key,
                "details": _json_safe(record.get("details") or {}),
                "since": _json_safe(record.get("since")),
            }
        else:
            result[source_key] = _json_safe(record)
    return result


def _charge_blocker_info(controller) -> tuple[dict[str, object], list[str]]:
    """Collect charge blockers without requiring the full controller API."""
    if controller is None:
        return {}, []

    global_registry = getattr(controller, "_global_charge_blockers", None)
    if not isinstance(global_registry, dict):
        getter = getattr(controller, "get_charge_blockers", None)
        if callable(getter):
            try:
                global_registry = getter()
            except (AttributeError, TypeError, ValueError):
                global_registry = {}

    batteries: dict[str, object] = {}
    battery_registries = getattr(controller, "_battery_charge_blockers", None)
    if isinstance(battery_registries, dict):
        for coordinator, registry in battery_registries.items():
            name = getattr(coordinator, "name", None) or str(coordinator)
            serialised = _serialise_blockers(registry)
            if serialised:
                batteries[str(name)] = serialised

    blockers: dict[str, object] = {
        "global": _serialise_blockers(global_registry),
        "batteries": batteries,
    }
    reasons: list[str] = []

    def collect(registry: object, scope: str = "") -> None:
        if not isinstance(registry, dict):
            return
        for source, record in registry.items():
            if isinstance(record, dict):
                reason = record.get("reason") or source
            else:
                reason = record or source
            label = f"{scope}{source}: {reason}" if scope else f"{source}: {reason}"
            if label not in reasons:
                reasons.append(str(label))

    collect(global_registry)
    for battery_name, registry in batteries.items():
        collect(registry, f"{battery_name} — ")
    return blockers, reasons


def _export_configuration(controller, plan) -> dict[str, object]:
    """Describe export configuration while supporting old and new attribute names."""
    config_entry = getattr(controller, "config_entry", None)
    config_data = getattr(config_entry, "data", None)
    config_options = getattr(config_entry, "options", None)
    sources = (controller, plan, config_data, config_options)
    mode, _mode_source = _value_from_sources(
        sources,
        (
            "predischarge_export_mode",
            "smart_predischarge_export_mode",
            "export_mode",
            "curtailment_export_mode",
        ),
    )
    limit, _limit_source = _value_from_sources(
        sources,
        (
            "predischarge_max_export_power_w",
            "predischarge_export_limit_w",
            "max_export_power_w",
            "export_limit_w",
            "curtailment_export_limit_w",
        ),
    )
    limit_number = _rounded_number(limit, 1)
    if mode is not None:
        mode_value = str(mode)
    elif limit_number is not None:
        # Legacy configurations only had the numeric field. Preserve their
        # meaning in the diagnostic even before an options-flow migration.
        mode_value = "self_consumption" if limit_number <= 0 else "custom"
    else:
        mode_value = None

    labels = {
        "self_consumption": "Solo autoconsumo",
        "automatic": "Automático",
        "auto": "Automático",
        "custom": "Límite personalizado",
        "custom_limit": "Límite personalizado",
    }
    return {
        "mode": mode_value,
        "mode_label": labels.get(mode_value, mode_value) if mode_value else None,
        "limit_w": limit_number,
        "limit_description": (
            "Límite de exportación deliberada a red, no potencia total de descarga"
            if limit_number is not None or mode_value is not None
            else None
        ),
    }


def _curtailment_info(controller) -> dict[str, object]:
    """Return anti-curtailment and opportunistic-charge diagnostics.

    The runtime has evolved over several releases, so this deliberately reads
    optional state with aliases and derives the opportunistic space only when
    both operands are available. ``None`` means unavailable; it never means
    that the solar reserve is safe to consume.
    """
    if controller is None:
        export = _export_configuration(None, None)
        return {
            "status": "unavailable",
            "reason": "controller_unavailable",
            "required_headroom_kwh": None,
            "current_headroom_kwh": None,
            "forecast_solar_surplus_kwh": None,
            "solar_reserve_remaining_kwh": None,
            "current_free_space_kwh": None,
            "opportunistic_space_available_kwh": None,
            "charge_limit_reason": "controller_unavailable",
            "charge_limit_reasons": ["controller_unavailable"],
            "charge_blocked": None,
            "charge_blockers": {"global": {}, "batteries": {}},
            "export": export,
            "export_mode": export["mode"],
            "export_limit_w": export["limit_w"],
        }

    plan = getattr(controller, "_curtailment_plan", None)
    runtime_state = getattr(controller, "_curtailment_diagnostics", None)
    sources = (runtime_state, controller, plan)

    required_headroom, _ = _value_from_sources(
        (plan, runtime_state, controller),
        ("required_headroom_kwh", "required_headroom"),
    )
    current_headroom, _ = _value_from_sources(
        (plan, runtime_state, controller),
        ("current_headroom_kwh", "current_headroom"),
    )
    forecast_surplus, _ = _value_from_sources(
        (plan, runtime_state, controller),
        ("solar_surplus_kwh", "forecast_solar_surplus_kwh"),
    )

    status, _ = _value_from_sources(
        (controller, plan), ("_curtailment_runtime_status", "status")
    )
    reason, _ = _value_from_sources(
        (controller, plan), ("_curtailment_runtime_reason", "reason")
    )

    current_free, current_source = _value_from_sources(
        sources,
        (
            "current_free_space_kwh",
            "free_space_kwh",
            "available_headroom_kwh",
            "_curtailment_current_headroom_kwh",
            "current_headroom_kwh",
            "current_headroom",
        ),
    )
    reserve, reserve_source = _value_from_sources(
        sources,
        (
            "solar_reserve_remaining_kwh",
            "remaining_solar_reserve_kwh",
            "_curtailment_solar_reserve_remaining_kwh",
            "curtailment_solar_reserve_remaining_kwh",
            "solar_reserve_kwh",
            "required_headroom_kwh",
            "required_headroom",
        ),
    )
    opportunistic_space, opportunity_source = _value_from_sources(
        sources,
        (
            "opportunistic_space_available_kwh",
            "available_opportunistic_space_kwh",
            "_curtailment_opportunistic_space_kwh",
            "opportunistic_space_kwh",
        ),
    )

    current_number = _safe_number(current_free)
    reserve_number = _safe_number(reserve)
    if current_number is not None:
        current_number = max(0.0, current_number)
    if reserve_number is not None:
        reserve_number = max(0.0, reserve_number)
    if opportunistic_space is None and current_number is not None and reserve_number is not None:
        # Keep this expression visible in the dump: it is the safety boundary
        # between the space that must remain for PV and the negative-price
        # opportunity that may use only the remainder.
        opportunistic_number = max(0.0, current_number - reserve_number)
        opportunity_source = "current_free_space_kwh - solar_reserve_remaining_kwh"
    else:
        opportunistic_number = _safe_number(opportunistic_space)
        if opportunistic_number is not None:
            opportunistic_number = max(0.0, opportunistic_number)

    blockers, blocker_reasons = _charge_blocker_info(controller)
    explicit_reason, _ = _value_from_sources(
        sources,
        (
            "charge_limit_reason",
            "opportunistic_charge_reason",
            "opportunistic_charge_limit_reason",
            "_curtailment_opportunistic_charge_reason",
            "grid_charge_limit_reason",
        ),
    )
    reasons = list(blocker_reasons)
    if explicit_reason and str(explicit_reason) != "not_calculated":
        explicit_reason = str(explicit_reason)
        if explicit_reason not in reasons:
            reasons.append(explicit_reason)

    decision = getattr(controller, "_last_decision_data", None)
    decision_reason = decision.get("reason") if isinstance(decision, dict) else None
    if (
        decision_reason
        and isinstance(decision, dict)
        and decision.get("should_charge") is False
        and not reasons
    ):
        reasons.append(str(decision_reason))

    purpose = getattr(controller, "_active_dynamic_slot_purpose", None)
    if (
        not reasons
        and purpose in {"negative_price", "combined"}
        and opportunistic_number is not None
        and opportunistic_number <= 1e-6
    ):
        reasons.append("solar_reserve_exhausted")
    if not reasons and status == "protected_window":
        reasons.append("solar_reserve_protected_window")

    charge_blocked = bool(blocker_reasons)
    if not charge_blocked and opportunistic_number is not None:
        charge_blocked = (
            (
                purpose in {"negative_price", "combined"}
                or status == "protected_window"
            )
            and opportunistic_number <= 1e-6
        )
    if (
        not charge_blocked
        and opportunistic_number is None
        and explicit_reason in {
            "solar_reserve_exhausted",
            "solar_reserve_protected",
            "solar_reserve_shortfall",
        }
    ):
        charge_blocked = True

    export = _export_configuration(controller, plan)
    return {
        "status": _json_safe(status),
        "reason": _json_safe(reason),
        "required_headroom_kwh": _rounded_number(required_headroom),
        "current_headroom_kwh": _rounded_number(current_headroom),
        "forecast_solar_surplus_kwh": _rounded_number(forecast_surplus),
        "solar_reserve_remaining_kwh": _rounded_number(reserve_number),
        "solar_reserve_source": reserve_source,
        "current_free_space_kwh": _rounded_number(current_number),
        "current_free_space_source": current_source,
        "opportunistic_space_available_kwh": _rounded_number(opportunistic_number),
        "opportunistic_space_source": opportunity_source,
        "charge_limit_reason": reasons[0] if reasons else None,
        "charge_limit_reasons": reasons,
        "charge_blocked": charge_blocked,
        "charge_blockers": blockers,
        "active_slot_purpose": _json_safe(purpose),
        "export": export,
        "export_mode": export["mode"],
        "export_limit_w": export["limit_w"],
    }


def _driver_info(coordinator) -> dict[str, Any]:
    """Static driver traits (no host/serial, which are identifiers)."""
    driver = coordinator.driver
    caps = coordinator.capabilities
    return {
        "connected": driver.connected,
        "model_label": driver.model_label,
        "capabilities": asdict(caps) if is_dataclass(caps) else str(caps),
    }


def _tracker_info(controller, coordinator) -> dict[str, Any]:
    """Non-responsive exclusion state for one battery (side-effect free)."""
    tracker = getattr(controller, "_non_responsive", None)
    if tracker is None:
        return {}
    info = tracker.batteries.get(coordinator, {})
    return {
        # excluded_names() reads without mutating; is_excluded() would reset the
        # fail counter on cooldown expiry, which a read-only dump must not do.
        "excluded": coordinator.name in tracker.excluded_names(),
        "fail_count": info.get("fail_count", 0),
        "reason": info.get("reason"),
        "retry_attempted": info.get("retry_attempted", False),
        "wake_used": info.get("wake_used", False),
    }


def _dynamic_pricing_info(controller) -> dict[str, Any]:
    """Return JSON-safe typed calendar diagnostics."""
    if controller is None:
        return {}
    schedule = getattr(controller, "_dynamic_pricing_schedule", None)
    info = {
        "solar_forecast_source": getattr(controller, "solar_forecast_source", None),
        "solar_forecast_diagnostic_source": getattr(
            controller, "solar_forecast_diagnostic_source", None
        ),
        "solar_forecast_remaining_sensor": getattr(
            controller, "solar_forecast_remaining_sensor", None
        ),
        "negative_price_charging_enabled": getattr(
            controller, "negative_price_charging_enabled", False
        ),
        "active_slot_purpose": getattr(
            controller, "_active_dynamic_slot_purpose", None
        ),
    }
    if schedule is None:
        info["schedule_type"] = None
        info["selected_slots"] = []
        return info
    selected_slots = getattr(schedule, "selected_slots", None) or []
    info.update(
        {
            "schedule_type": getattr(schedule, "schedule_type", "deficit"),
            "deficit_charging_needed": getattr(
                schedule,
                "deficit_charging_needed",
                getattr(schedule, "charging_needed", False),
            ),
            "negative_price_charging_needed": getattr(
                schedule, "negative_price_charging_needed", False
            ),
            "negative_price_energy_kwh": getattr(
                schedule, "negative_price_energy_kwh", 0.0
            ),
            "selected_slots": [
                {
                    "start": (
                        slot.start.isoformat()
                        if hasattr(getattr(slot, "start", None), "isoformat")
                        else getattr(slot, "start", None)
                    ),
                    "end": (
                        slot.end.isoformat()
                        if hasattr(getattr(slot, "end", None), "isoformat")
                        else getattr(slot, "end", None)
                    ),
                    "price": _json_safe(getattr(slot, "price", None)),
                    "purpose": (
                        schedule.purpose_for(slot)
                        if hasattr(schedule, "purpose_for")
                        else "deficit"
                    ),
                }
                for slot in selected_slots
            ],
        }
    )
    return info


def _timeline_int(value: object) -> int | None:
    """Return a finite non-negative integer for timeline counters."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return max(0, int(number))


def _timeline_scalar(value: object) -> object:
    """Keep optional controller status fields scalar and JSON-safe."""
    safe = _json_safe(value)
    return safe if safe is None or isinstance(safe, (str, bool, int, float)) else None


def _timeline_action_counts(masks: object) -> tuple[dict[str, int], int, int]:
    """Count action bits and double/triple overlaps without exposing masks."""
    counts = {"solar_charge": 0, "grid_charge": 0, "discharge": 0}
    double = 0
    triple = 0
    if not isinstance(masks, (list, tuple)):
        return counts, double, triple
    for raw_mask in masks[:96]:
        mask = _timeline_int(raw_mask) or 0
        bit_count = sum(bool(mask & bit) for bit in (1, 2, 4))
        if bit_count == 2:
            double += 1
        elif bit_count >= 3:
            triple += 1
        if mask & 1:
            counts["solar_charge"] += 1
        if mask & 2:
            counts["grid_charge"] += 1
        if mask & 4:
            counts["discharge"] += 1
    return counts, double, triple


def _daily_operation_timeline_summary(controller) -> dict[str, Any]:
    """Return bounded diagnostics for the current daily-operation snapshot.

    The entity adapter owns the compatibility work for public/legacy manager
    names and duck-typed snapshots. This function only projects its DTO into
    diagnostic metadata and counts; it deliberately never returns ``series``
    or ``operations`` and never reads a manager/store attribute directly.
    """
    try:
        from .sensor import _daily_operation_timeline_attributes

        payload = _daily_operation_timeline_attributes(controller)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never block download
        return {
            "available": False,
            "error": str(exc)[:160],
            "local_date": None,
            "mode": None,
            "schema_version": 1,
            "version": 1,
            "date": None,
            "current_index": None,
            "interval_count": 96,
            "last_updated": None,
            "last_update": None,
            "plan_evaluated_at": None,
            "revision": 0,
            "snapshot_build_count": 0,
            "notification_count": 0,
            "save_count": 0,
            "snapshot_age_s": None,
            "last_save_age_s": None,
            "publications_last_minute": 0,
            "writes_last_minute": 0,
            "sources": {},
            "freshness": {},
            "counts": {
                "real_cells": 0,
                "actual_cells": 0,
                "real": 0,
                "actual": 0,
                "planned_cells": 0,
                "forecast_cells": 0,
                "planned": 0,
                "forecast": 0,
                "unknown_cells": 96,
                "partial_cells": 0,
                "unknown": 96,
                "partial": 0,
                "by_action": {},
                "actions": {},
                "double_overlaps": 0,
                "triple_overlaps": 0,
                "overlap_double": 0,
                "overlap_triple": 0,
            },
            "restoration": {},
            "stale": None,
            "stale_reason": None,
            "last_error": str(exc)[:160],
            "setpoint": {},
            "delay": {},
        }

    setpoint = dict(payload.get("setpoint") or {})
    delay = dict(payload.get("delay") or {})
    charge_delay_status = getattr(controller, "_charge_delay_status", None)
    if isinstance(charge_delay_status, dict):
        state = charge_delay_status.get("state")
        target_soc = _rounded_number(charge_delay_status.get("target_soc"), 3)
        estimated_setpoint = next(
            (
                charge_delay_status.get(name)
                for name in (
                    "estimated_setpoint_time",
                    "setpoint_estimated_at",
                    "estimated_completion_at",
                )
                if charge_delay_status.get(name) is not None
            ),
            None,
        )
        if target_soc is not None and "target_soc" not in setpoint:
            setpoint["target_soc"] = target_soc
        if estimated_setpoint is not None and "estimated_completion" not in setpoint:
            setpoint["estimated_completion"] = _timeline_scalar(estimated_setpoint)
        if state is not None and "state" not in setpoint:
            setpoint["state"] = _timeline_scalar(state)

        estimated_unlock = charge_delay_status.get("estimated_unlock_time")
        if estimated_unlock is not None and "estimated_unlock_time" not in delay:
            delay["estimated_unlock_time"] = _timeline_scalar(estimated_unlock)
        if state is not None and "state" not in delay:
            delay["state"] = _timeline_scalar(state)
        reason = charge_delay_status.get("unlock_reason")
        if reason is not None and "reason" not in delay:
            delay["reason"] = _timeline_scalar(reason)

    series = payload.get("series") or {}
    operations = payload.get("operations") or {}
    actual_action = operations.get("actual_action_mask") or [0] * 96
    planned_action = operations.get("planned_action_mask") or [0] * 96
    actual_overlap = operations.get("actual_coexistence_mask") or actual_action
    planned_overlap = operations.get("planned_coexistence_mask") or planned_action
    actual_context = operations.get("actual_context_mask") or [0] * 96
    planned_context = operations.get("planned_context_mask") or [0] * 96

    actual_by_action, actual_double, actual_triple = _timeline_action_counts(
        actual_action
    )
    planned_by_action, planned_double, planned_triple = _timeline_action_counts(
        planned_action
    )

    def _at(values: object, index: int) -> object:
        if isinstance(values, (list, tuple)) and index < len(values):
            return values[index]
        return None

    real_cells = 0
    planned_cells = 0
    partial_cells = 0
    combined_masks: list[int] = []
    combined_overlap_masks: list[int] = []
    for index in range(96):
        actual_values_present = any(
            _at(series.get(key), index) is not None
            for key in ("solar_actual_kwh", "consumption_actual_kwh")
        )
        planned_values_present = any(
            _at(series.get(key), index) is not None
            for key in ("solar_forecast_kwh", "consumption_forecast_kwh")
        )
        actual_mask = _timeline_int(_at(actual_action, index)) or 0
        planned_mask = _timeline_int(_at(planned_action, index)) or 0
        actual_overlap_mask = _timeline_int(_at(actual_overlap, index)) or 0
        planned_overlap_mask = _timeline_int(_at(planned_overlap, index)) or 0
        actual_context_mask = _timeline_int(_at(actual_context, index)) or 0
        planned_context_mask = _timeline_int(_at(planned_context, index)) or 0
        coverage = _timeline_int(_at(series.get("actual_coverage_s"), index))
        actual_present = bool(
            actual_values_present or actual_mask or actual_context_mask or coverage
        )
        planned_present = bool(
            planned_values_present or planned_mask or planned_context_mask
        )
        real_cells += int(actual_present)
        planned_cells += int(planned_present)
        if coverage is not None and 0 < coverage < 900:
            partial_cells += 1
        combined_masks.append(actual_mask | planned_mask)
        combined_overlap_masks.append(actual_overlap_mask | planned_overlap_mask)

    combined_by_action, _ignored_double, _ignored_triple = _timeline_action_counts(
        combined_masks
    )
    _ignored_actions, double_overlaps, triple_overlaps = _timeline_action_counts(
        combined_overlap_masks
    )
    _actual_overlap_actions, actual_double, actual_triple = _timeline_action_counts(
        actual_overlap
    )
    _planned_overlap_actions, planned_double, planned_triple = _timeline_action_counts(
        planned_overlap
    )
    unknown_cells = max(0, 96 - sum(
        1
        for index in range(96)
        if (
            any(
                _at(series.get(key), index) is not None
                for key in ("solar_actual_kwh", "consumption_actual_kwh")
            )
            or any(
                _at(series.get(key), index) is not None
                for key in ("solar_forecast_kwh", "consumption_forecast_kwh")
            )
            or (_timeline_int(_at(actual_action, index)) or 0)
            or (_timeline_int(_at(planned_action, index)) or 0)
            or (_timeline_int(_at(actual_context, index)) or 0)
            or (_timeline_int(_at(planned_context, index)) or 0)
            or (_timeline_int(_at(series.get("actual_coverage_s"), index)) or 0)
        )
    ))

    return {
        "available": bool(payload.get("timeline_available")),
        "local_date": payload.get("local_date"),
        "mode": payload.get("mode"),
        "schema_version": payload.get("schema_version", 1),
        "version": payload.get("schema_version", 1),
        "date": payload.get("local_date"),
        "timezone": payload.get("timezone"),
        "interval_minutes": 15,
        "interval_count": 96,
        "revision": payload.get("revision", 0),
        "snapshot_build_count": payload.get("snapshot_build_count", 0),
        "notification_count": payload.get("notification_count", 0),
        "save_count": payload.get("save_count", 0),
        "snapshot_age_s": payload.get("snapshot_age_s"),
        "last_save_age_s": payload.get("last_save_age_s"),
        "publications_last_minute": payload.get("publications_last_minute", 0),
        "writes_last_minute": payload.get("writes_last_minute", 0),
        "current_index": payload.get("current_index"),
        "last_updated": payload.get("generated_at"),
        "last_update": payload.get("generated_at"),
        "plan_evaluated_at": payload.get("plan_evaluated_at"),
        "sources": payload.get("sources") or {},
        "freshness": payload.get("freshness") or {},
        "counts": {
            "real_cells": real_cells,
            "actual_cells": real_cells,
            "real": real_cells,
            "actual": real_cells,
            "planned_cells": planned_cells,
            "forecast_cells": planned_cells,
            "planned": planned_cells,
            "forecast": planned_cells,
            "unknown_cells": unknown_cells,
            "partial_cells": partial_cells,
            "unknown": unknown_cells,
            "partial": partial_cells,
            "by_action": combined_by_action,
            "actions": combined_by_action,
            "actual_by_action": actual_by_action,
            "planned_by_action": planned_by_action,
            "double_overlaps": double_overlaps,
            "triple_overlaps": triple_overlaps,
            "overlap_double": double_overlaps,
            "overlap_triple": triple_overlaps,
            "actual_double_overlaps": actual_double,
            "actual_triple_overlaps": actual_triple,
            "planned_double_overlaps": planned_double,
            "planned_triple_overlaps": planned_triple,
        },
        "restoration": payload.get("restoration") or {},
        "stale": payload.get("stale"),
        "stale_reason": payload.get("stale_reason"),
        "last_error": payload.get("last_error"),
        "setpoint": setpoint,
        "delay": delay,
        "estimated_setpoint_time": (
            setpoint.get("estimated_completion")
        ),
        "estimated_unlock_time": (
            delay.get("estimated_unlock_time")
        ),
    }


# Keep the descriptive alias available to duck-typed/unit tests and callers
# that use the terminology from the implementation plan.
_daily_operation_timeline_info = _daily_operation_timeline_summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a redacted health/driver/tracker dump for one config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinators = data.get("coordinators") or []
    controller = data.get("controller")

    batteries = [
        {
            "health": coord.health_snapshot(),
            "driver": _driver_info(coord),
            "tracker": _tracker_info(controller, coord),
            "runtime": {
                "battery_manual_mode_enabled": bool(
                    getattr(coord, "battery_manual_mode_enabled", False)
                ),
                "fronius_internal_control_disabled": bool(
                    getattr(coord, "fronius_internal_control_disabled", True)
                ),
                "automatic_pool": not controller._is_battery_manual_owned(coord)
                if controller is not None
                else not bool(getattr(coord, "battery_manual_mode_enabled", False)),
            },
        }
        for coord in coordinators
    ]

    manual_batteries = [
        coord.name for coord in coordinators
        if getattr(coord, "battery_manual_mode_enabled", False)
    ]
    automatic_batteries = [
        coord.name for coord in coordinators
        if not (
            controller._is_battery_manual_owned(coord)
            if controller is not None
            else getattr(coord, "battery_manual_mode_enabled", False)
        )
    ]
    fronius_released_batteries = [
        coord.name for coord in coordinators
        if getattr(coord, "brand", None) == "fronius_gen24"
        and not getattr(coord, "fronius_internal_control_disabled", True)
    ]

    consumption_profile = {}
    solar_profile = {}
    vacation = {}
    recorder_backfill = {}
    tracker = getattr(controller, "_consumption_tracker", None)
    profile = getattr(tracker, "consumption_profile", None)
    if profile is not None:
        try:
            consumption_profile = _json_safe(profile.diagnostics())
        except Exception as exc:  # noqa: BLE001
            consumption_profile = {"error": str(exc)}
    backfill_info = getattr(tracker, "backfill_diagnostics", None)
    if callable(backfill_info):
        try:
            recorder_backfill = _json_safe(backfill_info())
        except Exception as exc:  # noqa: BLE001
            recorder_backfill = {"error": str(exc)}
    vacation_info = getattr(tracker, "vacation_diagnostics", None)
    if callable(vacation_info):
        try:
            vacation = _json_safe(vacation_info())
        except Exception as exc:  # noqa: BLE001
            vacation = {"error": str(exc)}
    solar_tracker = getattr(tracker, "solar_profile", None)
    if solar_tracker is not None:
        try:
            solar_profile = _json_safe(solar_tracker.diagnostics())
        except Exception as exc:  # noqa: BLE001
            solar_profile = {"error": str(exc)}

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "batteries": batteries,
        "control_pool": {
            "manual_batteries": manual_batteries,
            "automatic_batteries": automatic_batteries,
            "fronius_released_batteries": fronius_released_batteries,
        },
        "dynamic_pricing": _dynamic_pricing_info(controller),
        "daily_operation_timeline": _daily_operation_timeline_summary(controller),
        "consumption_profile": consumption_profile,
        "recorder_backfill": recorder_backfill,
        "vacation_learning": vacation,
        "solar_profile": solar_profile,
        "curtailment": _curtailment_info(controller),
        "phase_protection": async_redact_data(
            controller._phase_power_limiter.diagnostics(), TO_REDACT
        )
        if controller is not None
        and getattr(controller, "_phase_power_limiter", None) is not None
        else {},
    }
