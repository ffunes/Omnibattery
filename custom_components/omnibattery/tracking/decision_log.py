"""Decision log: a JSON-lines file per day explaining what the controller did.

Home Assistant's own log hides the integration's info lines by default, and
those lines name a decision without the numbers behind it. This log keeps the
numbers, so a night can be reviewed the next morning ("why was the battery
still this full at sunrise?").

Files live in ``<config>/omnibattery_logs/decisions-YYYY-MM-DD.jsonl`` (local
date), one JSON object per line, and are kept for ``RETENTION_DAYS``. Events:

``start``    integration loaded, with the options in force (redacted).
``options``  the options changed.
``plan``     the planner's decision data or its booked slots changed.
``change``   the control state changed: mode, grid charging, active slot,
             charge delay, discharge reserve, surplus hold, blockers, ...
``state``    every quarter-hour: the full state, also when nothing changed.
``plan_trace`` the inputs and output of a control plan, untruncated: the
             energy curves, every deadline, every slot's amount and the price
             slots. Written when the booking changes, else every 15 minutes.

The log observes only. It reads the same side-effect-free snapshots as the
diagnostics download, never raises into the control cycle, and writes to disk
in the executor.
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

LOG_DIRECTORY = "omnibattery_logs"
FILE_PREFIX = "decisions-"
FILE_SUFFIX = ".jsonl"
RETENTION_DAYS = 14
STATE_INTERVAL = timedelta(minutes=15)
# Decision data also carries live values (stored energy, consumed today), so a
# re-plan with unchanged slots is written at most this often.
PLAN_MIN_SPACING = timedelta(minutes=1)
# A plan trace is large (every interval, deadline and price slot); write it
# when the booking changes, otherwise at most this often.
TRACE_MIN_SPACING = timedelta(minutes=15)
MAX_STRING = 200
MAX_COLLECTION = 24

# Decision-data entries that are bulky and already summarised elsewhere.
_SKIPPED_DECISION_KEYS = frozenset(
    {
        "daily_consumption_history",
        "solar_forecast_periods",
    }
)


def _clean(value: Any, depth: int = 0) -> Any:
    """JSON-safe, bounded copy: long lists and deep nesting are cut."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 4) if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:MAX_STRING]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if depth >= 3:
        return str(value)[:MAX_STRING]
    if isinstance(value, Mapping):
        items = list(value.items())
        cleaned = {str(k): _clean(v, depth + 1) for k, v in items[:MAX_COLLECTION]}
        if len(items) > MAX_COLLECTION:
            cleaned["_truncated"] = len(items)
        return cleaned
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        cleaned_list = [_clean(v, depth + 1) for v in items[:MAX_COLLECTION]]
        if len(items) > MAX_COLLECTION:
            cleaned_list.append(f"... {len(items) - MAX_COLLECTION} more")
        return cleaned_list
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:  # noqa: BLE001
            pass
    return str(value)[:MAX_STRING]


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe(builder: Callable[[], Any]) -> Any:
    try:
        return builder()
    except Exception as exc:  # noqa: BLE001 - one section must not drop the line
        return {"error": f"{type(exc).__name__}: {exc}"[:MAX_STRING]}


def _append_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(lines))


def _prune_files(directory: Path, oldest: date) -> None:
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        name = entry.name
        if not (name.startswith(FILE_PREFIX) and name.endswith(FILE_SUFFIX)):
            continue
        try:
            file_date = date.fromisoformat(name[len(FILE_PREFIX):-len(FILE_SUFFIX)])
        except ValueError:
            continue
        if file_date < oldest:
            try:
                os.remove(entry)
            except OSError:
                _LOGGER.debug("Decision log: could not remove %s", entry, exc_info=True)


class DecisionLogger:
    """Write the controller's decisions and state to a daily JSON-lines file."""

    def __init__(
        self,
        hass: Any,
        config_entry: Any,
        controller: Any,
        *,
        directory: str | os.PathLike[str] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        retention_days: int = RETENTION_DAYS,
    ) -> None:
        self._hass = hass
        self._entry = config_entry
        self._controller = controller
        if directory is None:
            directory = hass.config.path(LOG_DIRECTORY)
        self._directory = Path(directory)
        self._now_provider = now_provider or dt_util.now
        self._retention_days = max(1, int(retention_days))
        self._pending: list[tuple[date, str]] = []
        self._flushing = False
        self._last_state_slot: datetime | None = None
        self._last_plan_signature: str | None = None
        self._last_slots_signature: str | None = None
        self._last_plan_at: datetime | None = None
        self._last_change_signature: str | None = None
        self._last_options_signature: str | None = None
        self._last_trace: Any = None
        self._last_trace_signature: str | None = None
        self._last_trace_at: datetime | None = None
        self._pruned_for: date | None = None
        self.write_errors = 0

    @property
    def directory(self) -> Path:
        return self._directory

    def path_for(self, day: date) -> Path:
        return self._directory / f"{FILE_PREFIX}{day.isoformat()}{FILE_SUFFIX}"

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def _options(self) -> dict[str, Any]:
        from ..diagnostics import TO_REDACT  # local: diagnostics imports HA components

        from homeassistant.components.diagnostics import async_redact_data

        options = dict(getattr(self._entry, "data", {}) or {})
        options.update(dict(getattr(self._entry, "options", {}) or {}))
        options.pop("batteries", None)  # connection details, not behaviour
        redacted = async_redact_data(options, TO_REDACT)
        # Every option, not the first 24: a changed margin must show up.
        # Values are still bounded (long lists, deep nesting).
        return {str(key): _clean(value, 1) for key, value in sorted(redacted.items())}

    def _decision(self) -> dict[str, Any]:
        decision = getattr(self._controller, "_last_decision_data", None)
        if not isinstance(decision, Mapping):
            return {}
        return {
            str(key): _clean(value)
            for key, value in decision.items()
            if key not in _SKIPPED_DECISION_KEYS
        }

    def _batteries(self) -> list[dict[str, Any]]:
        from .. import ChargeDischargeController  # local: the package imports this module

        controller = self._controller
        delivered = ChargeDischargeController._coordinator_delivered_power
        result = []
        for coordinator in getattr(controller, "coordinators", ()) or ():
            data = getattr(coordinator, "data", None) or {}
            power = None
            if callable(delivered):
                try:
                    power = delivered(coordinator)
                except Exception:  # noqa: BLE001
                    power = None
            result.append(
                {
                    "name": str(getattr(coordinator, "name", "battery"))[:64],
                    "soc_pct": _number(data.get("battery_soc")),
                    "capacity_kwh": _number(data.get("battery_total_energy")),
                    # Controller convention: + charge / - discharge.
                    "power_w": _number(power),
                    "manual": bool(getattr(coordinator, "battery_manual_mode_enabled", False)),
                }
            )
        return result

    def _energy(self, batteries: list[dict[str, Any]]) -> dict[str, Any]:
        capacity = sum(b["capacity_kwh"] or 0.0 for b in batteries if b["soc_pct"] is not None)
        stored = sum(
            (b["capacity_kwh"] or 0.0) * max(0.0, min(100.0, b["soc_pct"])) / 100.0
            for b in batteries
            if b["soc_pct"] is not None
        )
        controller = self._controller
        tracker = getattr(controller, "_consumption_tracker", None)
        solar_kw = None
        read_solar = getattr(tracker, "_read_total_solar_power_kw", None)
        if callable(read_solar):
            try:
                solar_kw = _number(read_solar())
            except Exception:  # noqa: BLE001
                solar_kw = None
        grid_kw = _number(getattr(tracker, "_daily_grid_last_power_kw", None))
        return {
            "system_soc_pct": round(100.0 * stored / capacity, 2) if capacity > 0 else None,
            "stored_kwh": round(stored, 3),
            "capacity_kwh": round(capacity, 3),
            "solar_w": None if solar_kw is None else round(solar_kw * 1000.0),
            "grid_w": (
                round(grid_kw * 1000.0)
                if grid_kw is not None
                else _number(getattr(controller, "previous_sensor", None))
            ),
            "commanded_w": _number(getattr(controller, "previous_power", None)),
        }

    def _control(self) -> dict[str, Any]:
        from .. import diagnostics

        controller = self._controller
        blocker_info = _safe(lambda: diagnostics._charge_blocker_info(controller))
        blocker_reasons = blocker_info[1] if isinstance(blocker_info, tuple) else [blocker_info]
        delay = getattr(controller, "_charge_delay_status", None)
        schedule = _safe(lambda: diagnostics._dynamic_pricing_info(controller))
        return {
            "mode": _clean(getattr(controller, "predictive_charging_mode", None)),
            "predictive_enabled": bool(getattr(controller, "predictive_charging_enabled", False)),
            "predictive_overridden": bool(getattr(controller, "predictive_charging_overridden", False)),
            "manual_mode": bool(getattr(controller, "manual_mode_enabled", False)),
            "grid_charging_active": bool(getattr(controller, "grid_charging_active", False)),
            "realtime_price_charging": bool(getattr(controller, "_realtime_price_charging", False)),
            "active_slot_purpose": _clean(getattr(controller, "_active_dynamic_slot_purpose", None)),
            "price_data_status": _clean(getattr(controller, "_price_data_status", None)),
            "charge_delay": _clean(
                {
                    "enabled": bool(getattr(controller, "charge_delay_enabled", False)),
                    "unlocked": bool(getattr(controller, "_charge_delay_unlocked", False)),
                    "state": (delay or {}).get("state") if isinstance(delay, Mapping) else None,
                    "unlock_time": (delay or {}).get("estimated_unlock_time")
                    if isinstance(delay, Mapping)
                    else None,
                }
            ),
            "charge_blockers": _clean(blocker_reasons),
            "price_reserve_soc_pct": _number(getattr(controller, "_price_reserve_soc_pct", None)),
            "discharge_reserve": _clean(_safe(lambda: diagnostics._discharge_reserve_info(controller))),
            "surplus_hold": _clean(_safe(lambda: diagnostics._surplus_hold_info(controller))),
            "high_price_discharge": _clean(_safe(lambda: diagnostics._high_price_discharge_info(controller))),
            "schedule": _clean(
                {
                    key: schedule.get(key)
                    for key in (
                        "schedule_type",
                        "deficit_charging_needed",
                        "negative_price_charging_needed",
                        "selected_slots",
                    )
                }
                if isinstance(schedule, Mapping)
                else schedule
            ),
        }

    @staticmethod
    def _change_signature(control: Mapping[str, Any]) -> str:
        """The parts of the control state whose change is worth a line."""
        reserve = control.get("discharge_reserve") or {}
        hold = control.get("surplus_hold") or {}
        high = control.get("high_price_discharge") or {}
        delay = control.get("charge_delay") or {}
        key = {
            name: control.get(name)
            for name in (
                "mode",
                "predictive_enabled",
                "predictive_overridden",
                "manual_mode",
                "grid_charging_active",
                "realtime_price_charging",
                "active_slot_purpose",
                "price_data_status",
                "charge_blockers",
            )
        }
        key["delay"] = [delay.get("state"), delay.get("unlocked")] if isinstance(delay, Mapping) else None
        key["reserve"] = [
            reserve.get("state") if isinstance(reserve, Mapping) else None,
            round(control.get("price_reserve_soc_pct") or 0.0),
        ]
        key["hold"] = hold.get("state", hold.get("status")) if isinstance(hold, Mapping) else None
        key["high"] = high.get("state", high.get("status")) if isinstance(high, Mapping) else None
        return json.dumps(key, sort_keys=True, default=str)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record(self, now: datetime, event: str, payload: Mapping[str, Any]) -> None:
        line = {"t": now.replace(microsecond=0).isoformat(), "event": event, **payload}
        try:
            text = json.dumps(line, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as exc:
            text = json.dumps({"t": line["t"], "event": event, "error": str(exc)[:MAX_STRING]})
        self._pending.append((now.date(), text + "\n"))

    def log_start(self, version: str | None = None, now: datetime | None = None) -> None:
        current = self._local(now) if now is not None else self._now()
        options = _safe(self._options)
        self._last_options_signature = json.dumps(options, sort_keys=True, default=str)
        self._record(current, "start", {"version": _clean(version), "options": options})
        self.schedule_flush(current.date())

    def _now(self) -> datetime:
        return self._local(self._now_provider())

    def _local(self, value: datetime) -> datetime:
        """Home Assistant's wall clock: the control cycle passes UTC instants."""
        name = getattr(getattr(self._hass, "config", None), "time_zone", None)
        try:
            zone = ZoneInfo(str(name)) if name else None
        except Exception:  # noqa: BLE001 - unknown zone name
            zone = None
        if value.tzinfo is None:
            return value if zone is None else value.replace(tzinfo=zone)
        return value.astimezone(zone) if zone is not None else dt_util.as_local(value)

    def tick(self, now: datetime | None = None) -> None:
        """Called every control cycle; writes only what changed."""
        current = self._local(now) if now is not None else self._now()

        options = _safe(self._options)
        options_signature = json.dumps(options, sort_keys=True, default=str)
        if self._last_options_signature is None:
            self._last_options_signature = options_signature
        elif options_signature != self._last_options_signature:
            self._last_options_signature = options_signature
            self._record(current, "options", {"options": options})

        decision = _safe(self._decision)
        control = _safe(self._control)
        if not isinstance(control, Mapping):
            control = {"error": control}
        slots = (control.get("schedule") or {}).get("selected_slots") if isinstance(control.get("schedule"), Mapping) else None
        slots_signature = json.dumps(slots, sort_keys=True, default=str)
        plan_signature = json.dumps(decision, sort_keys=True, default=str)
        slots_changed = slots_signature != self._last_slots_signature
        plan_changed = plan_signature != self._last_plan_signature
        spaced = (
            self._last_plan_at is None
            or current - self._last_plan_at >= PLAN_MIN_SPACING
        )
        if decision and (slots_changed or (plan_changed and spaced)):
            self._record(
                current,
                "plan",
                {"decision": decision, "schedule": control.get("schedule"), "slots_changed": slots_changed},
            )
            self._last_plan_signature = plan_signature
            self._last_slots_signature = slots_signature
            self._last_plan_at = current

        self._record_trace(current)

        batteries = _safe(self._batteries)
        energy = (
            _safe(lambda: self._energy(batteries))
            if isinstance(batteries, list)
            else {"error": batteries}
        )
        change_signature = self._change_signature(control)
        if self._last_change_signature is not None and change_signature != self._last_change_signature:
            self._record(current, "change", {"energy": energy, "control": control})
        self._last_change_signature = change_signature

        slot = current.replace(
            minute=current.minute - current.minute % 15, second=0, microsecond=0
        )
        if self._last_state_slot != slot:
            self._last_state_slot = slot
            self._record(
                current,
                "state",
                {"energy": energy, "batteries": batteries, "control": control},
            )
        self.schedule_flush(current.date())

    def _record_trace(self, current: datetime) -> None:
        """Write the latest control plan's trace when it is new and worth it."""
        trace = getattr(self._controller, "_last_plan_trace", None)
        if not isinstance(trace, Mapping) or trace is self._last_trace:
            return
        self._last_trace = trace
        try:
            signature = json.dumps(
                [trace.get("allocations"), trace.get("deadlines")],
                sort_keys=True,
                default=str,
            )
        except (TypeError, ValueError):
            signature = None
        due = (
            self._last_trace_at is None
            or current - self._last_trace_at >= TRACE_MIN_SPACING
        )
        if signature == self._last_trace_signature and not due:
            return
        self._last_trace_signature = signature
        self._last_trace_at = current
        self._record(
            current,
            "plan_trace",
            {"trace": trace, "slot_targets_kwh": _safe(self._slot_targets)},
        )

    def _slot_targets(self) -> dict[str, float | None]:
        """Each booked slot's energy quota as the controller will charge it."""
        schedule = getattr(self._controller, "_dynamic_pricing_schedule", None)
        targets = getattr(schedule, "slot_energy_targets_kwh", None) or {}
        return {
            str(getattr(getattr(slot, "start", None), "isoformat", lambda: slot)()): _number(value)
            for slot, value in sorted(
                targets.items(), key=lambda item: str(getattr(item[0], "start", item[0]))
            )
        }

    # ------------------------------------------------------------------
    # Disk
    # ------------------------------------------------------------------

    def _take_pending(self) -> dict[date, list[str]]:
        grouped: dict[date, list[str]] = {}
        for day, text in self._pending:
            grouped.setdefault(day, []).append(text)
        self._pending = []
        return grouped

    def _write(self, grouped: dict[date, list[str]], prune_before: date | None) -> None:
        for day, lines in grouped.items():
            _append_lines(self.path_for(day), lines)
        if prune_before is not None:
            _prune_files(self._directory, prune_before)

    def schedule_flush(self, today: date | None = None) -> None:
        if not self._pending or self._flushing:
            return
        add_job = getattr(self._hass, "async_add_executor_job", None)
        if not callable(add_job):
            return
        prune_before = None
        if today is not None and today != self._pruned_for:
            self._pruned_for = today
            prune_before = today - timedelta(days=self._retention_days)
        grouped = self._take_pending()
        self._flushing = True

        async def run() -> None:
            try:
                await add_job(self._write, grouped, prune_before)
            except Exception:  # noqa: BLE001 - disk trouble must not reach control
                self.write_errors += 1
                _LOGGER.debug("Decision log: write failed", exc_info=True)
            finally:
                self._flushing = False
            # Lines recorded while this write ran go out right after it.
            if self._pending:
                self.schedule_flush(None)

        create_task = getattr(self._hass, "async_create_background_task", None)
        if callable(create_task):
            create_task(run(), "omnibattery_decision_log_write")
        else:
            self._hass.async_create_task(run())

    async def async_flush(self) -> None:
        """Write what is pending now (unload, tests)."""
        grouped = self._take_pending()
        if not grouped:
            return
        try:
            await self._hass.async_add_executor_job(self._write, grouped, None)
        except Exception:  # noqa: BLE001
            self.write_errors += 1
            _LOGGER.debug("Decision log: final write failed", exc_info=True)
