"""Forecast history: what the planner predicted versus what really happened.

The daily operation timeline only keeps the local day on display and rewrites
its plan whenever the planner runs. This module keeps a bounded record per
local day so forecasts can be compared with reality afterwards:

``day_ahead``
    The plan for the day as it stood just before midnight (the timeline's
    cross-midnight extension). Coverage depends on the planning horizon: until
    tomorrow's sunrise, or the whole day once tomorrow's prices are known.
``start_of_day``
    The first full-day plan after the 00:05 daily run. Captured later in the
    day (the day of the update, a restart), it covers only the quarters from
    the capture on.
``actual``
    The measured quarter-hours, frozen when the day rolls over.
``revisions``
    Compact totals each time the prediction for that day changed noticeably,
    both from the previous day (``day_ahead``) and during the day itself
    (``intraday``), so the drift of the prediction can be charted.

The module never influences control. It reads the timeline's own JSON payload,
so the live path and the restart path (a stored diary from a past day) share
one parser.
"""
from __future__ import annotations

import csv
import inspect
import io
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

FORECAST_HISTORY_STORE_VERSION = 1
FORECAST_HISTORY_STORE_KEY = "forecast_history"
FORECAST_HISTORY_RETENTION_DAYS = 60
FORECAST_HISTORY_SAVE_DELAY_S = 300

INTERVAL_COUNT = 96
INTERVAL_MINUTES = 15

# A measured quarter needs at least this much sensor coverage; less means
# Home Assistant was not running for most of it (the profiles hand over 0 kWh
# with 0 s coverage then). Same threshold as the consumption profile's
# MIN_INTERVAL_COVERAGE_S.
MIN_MEASURED_COVERAGE_S = 0.75 * INTERVAL_MINUTES * 60

# A revision is kept when the prediction for a day moved at least this much,
# and never more often than every REVISION_MIN_SPACING unless the planning
# mode, horizon reason or day-ahead coverage changed.
REVISION_KWH_DELTA = 0.1
REVISION_SOC_DELTA = 2.0
REVISION_MIN_SPACING = timedelta(minutes=15)
MAX_REVISIONS_PER_HORIZON = 120

# The automatic dynamic-pricing run plans the full day at 00:05.
START_OF_DAY_CAPTURE_AFTER = time(0, 6)

# After a restart the first projections are built before the planner has run:
# without prices the horizon is shorter, and Dynamic Pricing's first checks
# book slots at full power until the startup evaluation sizes them (13 Nov
# 2026: 48 quarters, then 7.5 instead of 4.5 kWh, within a minute). Nothing is
# recorded until the plan has had this long to settle.
STARTUP_SETTLE = timedelta(minutes=2)

HORIZON_INTRADAY = "intraday"
HORIZON_DAY_AHEAD = "day_ahead"

SOURCE_DAY_AHEAD = "day_ahead"
SOURCE_START_OF_DAY = "start_of_day"
SOURCES = (SOURCE_DAY_AHEAD, SOURCE_START_OF_DAY)

# Series kept per source. ``soc`` is a percentage, the others kWh/15 min.
ENERGY_METRICS = ("solar", "consumption", "grid_charge", "discharge")
SERIES_METRICS = (*ENERGY_METRICS, "soc")

_ACTION_GRID_CHARGE = 2

# Planner scalars copied into intraday revisions (``_last_decision_data``).
_DECISION_FIELDS = {
    "energy_deficit_kwh": "deficit",
    "planned_grid_charge_kwh": "planned_grid",
    "deferred_grid_charge_kwh": "deferred_grid",
    "solar_forecast_kwh": "solar_forecast",
}


def _num(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _non_negative(value: Any) -> float | None:
    parsed = _num(value)
    return None if parsed is None else max(0.0, parsed)


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def _series(raw: Any, *, length: int = INTERVAL_COUNT) -> list[float | None]:
    values = list(raw) if isinstance(raw, (list, tuple)) else []
    values = values[:length] + [None] * max(0, length - len(values))
    return [_non_negative(value) for value in values]


def _sum(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(sum(present), 3) if present else None


def _last(values: Sequence[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _text(value: Any, limit: int = 64) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _cell_grid_charge(cell: Mapping[str, Any]) -> float | None:
    """Battery charge attributed to the grid in one measured quarter-hour.

    The diary measures what the grid delivered into the battery per sample
    (``actual_grid_to_battery_kwh``: the AC-side charge, at most the grid
    import). In a daytime slot solar covers part of the charge, which the
    plan's ``grid_to_battery_kwh`` leaves out as well. Older diaries only
    know what entered the battery: there the energy is split by how long grid
    and solar charging were active.
    """
    charge = _non_negative(cell.get("actual_charge_to_battery_kwh"))
    if charge is None:
        return None
    seconds = cell.get("observed_seconds_by_action")
    seconds = seconds if isinstance(seconds, Mapping) else {}
    grid_s = _non_negative(seconds.get("grid_charge")) or 0.0
    solar_s = _non_negative(seconds.get("solar_charge")) or 0.0
    mask = int(_num(cell.get("actual_action_mask")) or 0)
    if grid_s <= 0.0 and not mask & _ACTION_GRID_CHARGE:
        return 0.0
    from_grid = _non_negative(cell.get("actual_grid_to_battery_kwh"))
    if from_grid is not None:
        return min(from_grid, charge)
    if grid_s + solar_s > 0.0:
        return charge * grid_s / (grid_s + solar_s)
    return charge


def _coverage(raw: Any) -> list[float] | None:
    """Per-quarter sensor coverage in seconds, or ``None`` when unknown.

    A diary without coverage (simple captures, older stores) or with none at
    all for the day carries no information on gaps: its values stay measured.
    """
    if not isinstance(raw, (list, tuple)):
        return None
    values = [_non_negative(value) or 0.0 for value in list(raw)[:INTERVAL_COUNT]]
    values += [0.0] * (INTERVAL_COUNT - len(values))
    return values if any(value > 0.0 for value in values) else None


def view_from_timeline_payload(
    payload: Mapping[str, Any], *, final: bool = False
) -> dict[str, Any] | None:
    """Reduce a daily-timeline Store payload to the series this module needs.

    ``final`` reads a finished day: its last open quarter is over as well, so
    it is judged like a closed one. Returns ``None`` for a payload that is not
    a recognisable diary.
    """
    if not isinstance(payload, Mapping):
        return None
    local_date = _parse_date(payload.get("local_date", payload.get("date")))
    cells = payload.get("cells")
    if local_date is None or not isinstance(cells, list) or len(cells) != INTERVAL_COUNT:
        return None
    actual = payload.get("actual") if isinstance(payload.get("actual"), Mapping) else {}
    planned = payload.get("planned") if isinstance(payload.get("planned"), Mapping) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    closed_raw = payload.get("closed")
    closed = [
        bool(value)
        for value in (list(closed_raw) if isinstance(closed_raw, list) else [])
    ][:INTERVAL_COUNT]
    closed += [False] * (INTERVAL_COUNT - len(closed))
    last_open = payload.get("last_open_sample")
    current_index = (
        int(_num(last_open.get("index")) or 0) if isinstance(last_open, Mapping) else 0
    )
    current_index = max(0, min(INTERVAL_COUNT - 1, current_index))
    cell_maps = [cell if isinstance(cell, Mapping) else {} for cell in cells]

    actual_series = {
        "solar": _series(actual.get("solar_kwh")),
        "consumption": _series(actual.get("consumption_kwh")),
        "grid_charge": [_cell_grid_charge(cell) for cell in cell_maps],
        "discharge": [
            _non_negative(cell.get("actual_discharge_from_battery_kwh"))
            for cell in cell_maps
        ],
        "soc": [_non_negative(cell.get("actual_soc_pct")) for cell in cell_maps],
    }
    coverage = {
        "solar": _coverage(actual.get("solar_coverage_s")),
        "consumption": _coverage(actual.get("consumption_coverage_s")),
    }
    known = [values for values in coverage.values() if values is not None]
    for index in range(INTERVAL_COUNT):
        # The running quarter keeps what was measured so far: it is the first
        # part of that quarter's plan (see _remaining_plan).
        if index == current_index and not closed[index] and not final:
            for name in ("grid_charge", "discharge"):
                if actual_series[name][index] is None:
                    actual_series[name][index] = 0.0
            continue
        # A quarter the profiles hardly covered was not observed: Home
        # Assistant was not running, and its 0 kWh is no measurement.
        if known:
            observed = any(values[index] >= MIN_MEASURED_COVERAGE_S for values in known)
        else:
            observed = closed[index] or index == current_index
        consumption_cover = coverage["consumption"]
        if consumption_cover is not None and consumption_cover[index] < MIN_MEASURED_COVERAGE_S:
            actual_series["consumption"][index] = None
        # Solar has no coverage of its own to go by: an external solar sensor
        # is unavailable at night while Home Assistant runs, which means no
        # production, not a gap. Only a quarter nobody observed is unmeasured,
        # so a daytime sensor dropout while Home Assistant runs counts as 0.
        if known and not observed:
            actual_series["solar"][index] = None
        # An observed quarter without charge or discharge moved no energy;
        # keep that as an explicit zero so totals and charts agree.
        for name in ("grid_charge", "discharge"):
            if not observed:
                actual_series[name][index] = None
            elif actual_series[name][index] is None:
                actual_series[name][index] = 0.0

    planned_series = {
        "solar": _series(planned.get("solar_kwh")),
        "consumption": _series(planned.get("consumption_kwh")),
        "grid_charge": [
            _non_negative(cell.get("planned_grid_to_battery_kwh")) for cell in cell_maps
        ],
        "discharge": [
            _non_negative(cell.get("planned_battery_to_home_kwh")) for cell in cell_maps
        ],
        "soc": [_non_negative(cell.get("planned_soc_end_pct")) for cell in cell_maps],
    }

    extension = {name: [None] * INTERVAL_COUNT for name in SERIES_METRICS}
    covered = [False] * INTERVAL_COUNT
    raw_extension = payload.get("extended_projection")
    for item in raw_extension if isinstance(raw_extension, list) else []:
        if not isinstance(item, Mapping):
            continue
        index_value = _num(item.get("extension_index", item.get("index")))
        if index_value is None:
            continue
        index = int(index_value)
        if not 0 <= index < INTERVAL_COUNT:
            continue
        covered[index] = True
        extension["solar"][index] = _non_negative(item.get("solar_kwh")) or 0.0
        extension["consumption"][index] = _non_negative(item.get("consumption_kwh")) or 0.0
        extension["grid_charge"][index] = _non_negative(item.get("grid_to_battery_kwh")) or 0.0
        discharge = _non_negative(item.get("battery_to_home_kwh"))
        if discharge is None:
            discharge = _non_negative(item.get("discharge_from_battery_kwh"))
        extension["discharge"][index] = discharge or 0.0
        extension["soc"][index] = _non_negative(item.get("soc_end_pct"))

    return {
        "date": local_date,
        "mode": _text(metadata.get("mode")) or "unknown",
        "plan_evaluated_at": _text(metadata.get("plan_evaluated_at")),
        "generated_at": _text(metadata.get("generated_at")),
        "stale": bool(metadata.get("stale", False)),
        "current_index": current_index,
        "closed": closed,
        "actual": actual_series,
        "planned": planned_series,
        "extension": extension,
        "extension_coverage": sum(covered),
    }


def _rounded_series(series: Mapping[str, Sequence[float | None]]) -> dict[str, list[float | None]]:
    return {
        name: [
            _round(value, 2 if name == "soc" else 4)
            for value in list(series.get(name) or [])[:INTERVAL_COUNT]
        ]
        for name in SERIES_METRICS
    }


def _series_totals(series: Mapping[str, Sequence[float | None]]) -> dict[str, float | None]:
    totals: dict[str, float | None] = {
        name: _sum(series.get(name) or []) for name in ENERGY_METRICS
    }
    totals["soc_end"] = _round(_last(series.get("soc") or []), 1)
    return totals


def _remaining_plan(view: Mapping[str, Any]) -> dict[str, list[float | None]]:
    """Today's plan from the running quarter on; ``None`` before it.

    A closed quarter's cell holds the plan as it stood when the quarter
    closed, which is not the current plan. The projection simulates from
    now, so for the running quarter its battery flows hold only the rest of
    the quarter: what was measured in it so far is the first part of the same
    plan. Solar and consumption are full-quarter figures there already, so
    adding the measured part would count it twice.
    """
    closed = view["closed"]
    current = view["current_index"]
    plan = {
        name: [
            None if index < current or closed[index] else value
            for index, value in enumerate(view["planned"][name])
        ]
        for name in SERIES_METRICS
    }
    if not closed[current]:
        for name in ("grid_charge", "discharge"):
            planned = plan[name][current]
            if planned is not None:
                plan[name][current] = planned + (view["actual"][name][current] or 0.0)
    return plan


def _plans_ahead(plan: Mapping[str, Sequence[float | None]]) -> bool:
    """Whether a remaining plan holds any planned solar or consumption."""
    return any(
        value is not None for name in ("solar", "consumption") for value in plan[name]
    )


def _covered_entry(series: dict[str, list[float | None]], from_index: int) -> dict[str, Any]:
    """A stored plan that starts at ``from_index``: nothing before it counts."""
    from_index = max(0, min(INTERVAL_COUNT - 1, int(from_index)))
    if from_index:
        for name in SERIES_METRICS:
            values = series.get(name) or []
            series[name] = [
                None if index < from_index else value for index, value in enumerate(values)
            ]
    return {
        "from_index": from_index,
        "coverage": INTERVAL_COUNT - from_index,
        "series": series,
        "totals": _series_totals(series),
    }


class ForecastHistoryManager:
    """Keep a bounded per-day record of predictions and the measured result."""

    def __init__(
        self,
        hass: Any,
        config_entry: Any,
        *,
        store: Any = None,
        now_provider: Callable[[], datetime] | None = None,
        retention_days: int = FORECAST_HISTORY_RETENTION_DAYS,
        save_delay_s: float = FORECAST_HISTORY_SAVE_DELAY_S,
        startup_settle_s: float | None = None,
    ) -> None:
        self._hass = hass
        self._config_entry = config_entry
        entry_id = getattr(config_entry, "entry_id", "unknown")
        self._store = (
            store
            if store is not None
            else Store(
                hass,
                FORECAST_HISTORY_STORE_VERSION,
                f"{DOMAIN}.{entry_id}.{FORECAST_HISTORY_STORE_KEY}",
            )
        )
        self._now_provider = now_provider
        self._retention_days = max(1, int(retention_days))
        self._save_delay_s = max(0.0, float(save_delay_s))
        self._startup_settle = (
            STARTUP_SETTLE
            if startup_settle_s is None
            else timedelta(seconds=max(0.0, float(startup_settle_s)))
        )
        self._first_observed_at: datetime | None = None
        self._days: dict[str, dict[str, Any]] = {}
        self._timeline: Any = None
        self._last_projection: tuple[date, datetime] | None = None
        self._dirty = False
        self._loaded = False
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    @property
    def retention_days(self) -> int:
        return self._retention_days

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def attach(self, timeline: Any) -> None:
        """Follow ``timeline``: freeze each day when it rolls over."""
        self._timeline = timeline
        add_hook = getattr(timeline, "add_day_closed_listener", None)
        if callable(add_hook):
            add_hook(self.close_day_from_payload)

    def _now(self) -> datetime:
        """Local wall clock: the timeline's own clock keeps both in step."""
        if self._now_provider is not None:
            return self._now_provider()
        local = getattr(self._timeline, "as_local_datetime", None)
        if callable(local):
            try:
                return local()
            except Exception:  # noqa: BLE001 - fall back to Home Assistant's clock
                _LOGGER.debug("Forecast history: timeline clock failed", exc_info=True)
        return dt_util.now()

    def _day(self, value: date) -> dict[str, Any]:
        key = value.isoformat()
        record = self._days.get(key)
        if record is None:
            record = {"date": key, "revisions": []}
            self._days[key] = record
        return record

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def observe(
        self,
        timeline: Any = None,
        *,
        decision: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        view: Mapping[str, Any] | None = None,
    ) -> bool:
        """Record the current plan after the timeline rebuilt its projection."""
        if view is None:
            source = timeline if timeline is not None else self._timeline
            payload_fn = getattr(source, "forecast_history_payload", None)
            if not callable(payload_fn):
                return False
            view = view_from_timeline_payload(payload_fn())
        if view is None or view.get("stale"):
            return False
        current = now or self._now()
        # The first projections of this run are not the plan yet (see
        # STARTUP_SETTLE). A clock that jumped back does not prolong the wait.
        if self._first_observed_at is None:
            self._first_observed_at = current
        try:
            settling = current - self._first_observed_at
        except TypeError:  # naive and aware clocks mixed: no way to tell
            settling = self._startup_settle
        if timedelta(0) <= settling < self._startup_settle:
            return False
        local_date: date = view["date"]
        changed = False
        record = self._day(local_date)
        if record.get("mode") != view["mode"]:
            record["mode"] = view["mode"]
            changed = True

        # The frozen day-ahead plan is the last projection before midnight.
        self._last_projection = (local_date, current)

        decision_map = decision if isinstance(decision, Mapping) else {}
        # Only a full control plan says which horizon it planned. The cheaper
        # balance checks replace the decision data in between without one;
        # their scalars are no plan (13 Nov 2026: their 21.6 kWh deficit
        # alternated with the plan's 10.9 kWh). The projection's series do
        # not depend on which decision is current, so they are recorded on
        # every observation; the scalars only with a full plan.
        reason = _text(decision_map.get("planning_horizon_reason"))
        if not reason:
            decision_map = {}

        # Closed cells keep the plan they closed with, so only the quarters
        # from the running one on say whether there is a plan now. Real-Time
        # Price and manual mode have none: an "expected" total there is just
        # the measured part (96 revisions rising from 0 to the day's total),
        # and a start-of-day entry without values would block the real one
        # when the plan returns later that day.
        plan = _remaining_plan(view)
        planned_ahead = _plans_ahead(plan)

        if (
            planned_ahead
            and SOURCE_START_OF_DAY not in record
            and current.date() == local_date
            and current.time() >= START_OF_DAY_CAPTURE_AFTER
            # Dynamic Pricing plans the day in its 00:05 run; a projection
            # from before it is yesterday evening's plan.
            and (reason or view["mode"] != "dynamic_pricing")
        ):
            entry = _covered_entry(_rounded_series(plan), view["current_index"])
            entry["captured_at"] = current.replace(second=0, microsecond=0).isoformat()
            record[SOURCE_START_OF_DAY] = entry
            changed = True

        if planned_ahead:
            changed |= self._add_revision(
                record,
                self._intraday_revision(view, decision_map, current, reason, plan=plan),
            )
        if view["extension_coverage"]:
            tomorrow = self._day(local_date + timedelta(days=1))
            changed |= self._add_revision(
                tomorrow,
                self._day_ahead_revision(view, decision_map, current, reason),
            )
        if changed:
            self._prune(current.date())
            self.request_save()
        return changed

    @staticmethod
    def _intraday_revision(
        view: Mapping[str, Any],
        decision: Mapping[str, Any],
        current: datetime,
        reason: str | None,
        *,
        plan: Mapping[str, Sequence[float | None]] | None = None,
    ) -> dict[str, Any]:
        closed = view["closed"]
        if plan is None:
            plan = _remaining_plan(view)
        entry: dict[str, Any] = {
            "t": current.replace(second=0, microsecond=0).isoformat(),
            "h": HORIZON_INTRADAY,
            "mode": view["mode"],
            "idx": view["current_index"],
        }
        for name in ENERGY_METRICS:
            done = _sum(
                [view["actual"][name][i] for i in range(INTERVAL_COUNT) if closed[i]]
            ) or 0.0
            remaining = _sum(plan[name]) or 0.0
            entry[f"a_{name}"] = round(done, 3)
            entry[f"p_{name}"] = round(remaining, 3)
        entry["soc_end"] = _round(_last(plan["soc"]), 1)
        for key, short in _DECISION_FIELDS.items():
            value = _num(decision.get(key))
            if value is not None:
                entry[short] = round(value, 3)
        if reason:
            entry["reason"] = reason
        return entry

    @staticmethod
    def _day_ahead_revision(
        view: Mapping[str, Any],
        decision: Mapping[str, Any],
        current: datetime,
        reason: str | None,
    ) -> dict[str, Any]:
        extension = view["extension"]
        entry: dict[str, Any] = {
            "t": current.replace(second=0, microsecond=0).isoformat(),
            "h": HORIZON_DAY_AHEAD,
            "mode": view["mode"],
            "cov": view["extension_coverage"],
        }
        for name in ENERGY_METRICS:
            entry[f"p_{name}"] = _sum(extension[name]) or 0.0
        entry["soc_end"] = _round(_last(extension["soc"]), 1)
        deferred = _num(decision.get("deferred_grid_charge_kwh"))
        if deferred is not None:
            entry["deferred_grid"] = round(deferred, 3)
        if reason:
            entry["reason"] = reason
        return entry

    @staticmethod
    def _revision_total(entry: Mapping[str, Any], name: str) -> float:
        return (_num(entry.get(f"a_{name}")) or 0.0) + (_num(entry.get(f"p_{name}")) or 0.0)

    def _add_revision(self, record: dict[str, Any], entry: dict[str, Any]) -> bool:
        revisions: list[dict[str, Any]] = record.setdefault("revisions", [])
        same = [item for item in revisions if item.get("h") == entry["h"]]
        previous = same[-1] if same else None
        if previous is not None:
            structural = any(previous.get(key) != entry.get(key) for key in ("mode", "cov"))
            # Only a full plan names its horizon reason; the balance checks in
            # between carry none, so compare with the last plan that did.
            reason = entry.get("reason")
            if reason:
                reasoned = [item.get("reason") for item in same if item.get("reason")]
                structural |= not reasoned or reasoned[-1] != reason
            moved = any(
                abs(self._revision_total(previous, name) - self._revision_total(entry, name))
                >= REVISION_KWH_DELTA
                for name in ENERGY_METRICS
            )
            soc_prev, soc_now = _num(previous.get("soc_end")), _num(entry.get("soc_end"))
            if soc_prev is not None and soc_now is not None:
                moved |= abs(soc_prev - soc_now) >= REVISION_SOC_DELTA
            if not structural and not moved:
                return False
            previous_at = _parse_datetime(previous.get("t"))
            current_at = _parse_datetime(entry.get("t"))
            if (
                not structural
                and previous_at is not None
                and current_at is not None
                and current_at - previous_at < REVISION_MIN_SPACING
            ):
                return False
        if len(same) >= MAX_REVISIONS_PER_HORIZON:
            # Keep the oldest history; the newest slot tracks the latest plan.
            revisions.remove(same[-1])
        revisions.append(entry)
        revisions.sort(key=lambda item: str(item.get("t", "")))
        return True

    def close_day_from_payload(self, payload: Mapping[str, Any]) -> bool:
        """Freeze one finished diary: its measurements and tomorrow's plan."""
        # The day is over: its last open quarter is final as well. Quarters
        # Home Assistant did not observe stay unmeasured.
        view = view_from_timeline_payload(payload, final=True)
        if view is None:
            return False
        local_date: date = view["date"]
        record = self._day(local_date)
        actual = _rounded_series(view["actual"])
        record["actual"] = {"series": actual, "totals": _series_totals(actual)}
        record["mode"] = view["mode"]
        record["finalized"] = True
        if view["extension_coverage"]:
            series = _rounded_series(view["extension"])
            tomorrow = self._day(local_date + timedelta(days=1))
            # The series is the last projection before midnight. The plan's
            # own evaluation time can be a day older (a Dynamic Pricing
            # schedule is re-projected every minute without a new run).
            last = self._last_projection
            captured_at = (
                last[1].replace(second=0, microsecond=0).isoformat()
                if last is not None and last[0] == local_date
                else view["generated_at"] or view["plan_evaluated_at"]
            )
            tomorrow[SOURCE_DAY_AHEAD] = {
                "captured_at": captured_at,
                "coverage": view["extension_coverage"],
                "series": series,
                "totals": _series_totals(series),
            }
        self._prune(self._now().date())
        self.request_save()
        return True

    def _prune(self, today: date) -> None:
        oldest = today - timedelta(days=self._retention_days)
        newest = today + timedelta(days=1)
        for key in list(self._days):
            parsed = _parse_date(key)
            if parsed is None or parsed < oldest or parsed > newest:
                self._days.pop(key, None)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _payload(self) -> dict[str, Any]:
        return {
            "version": FORECAST_HISTORY_STORE_VERSION,
            "days": {key: self._days[key] for key in sorted(self._days)},
        }

    async def async_load(self) -> bool:
        try:
            loaded = self._store.async_load()
            data = await loaded if inspect.isawaitable(loaded) else loaded
        except Exception as exc:  # noqa: BLE001 - never block setup
            self._last_error = f"load: {exc}"
            self._loaded = True
            _LOGGER.warning("Forecast history: failed to load Store: %s", exc)
            return False
        self._loaded = True
        days = data.get("days") if isinstance(data, Mapping) else None
        if not isinstance(days, Mapping):
            return False
        for key, record in days.items():
            if _parse_date(key) is not None and isinstance(record, Mapping):
                restored = dict(record)
                if not isinstance(restored.get("revisions"), list):
                    restored["revisions"] = []
                restored["date"] = key
                self._normalize_start_of_day(restored)
                self._days[key] = restored
        self._prune(self._now().date())
        return True

    @staticmethod
    def _normalize_start_of_day(record: dict[str, Any]) -> None:
        """Older stores kept a late capture's quarters before ``from_index``.

        Those quarters held the plan as each stood when it closed, not the
        plan at the capture; compared over the whole day they made a correct
        plan look 97 % off on the day of the update.
        """
        entry = record.get(SOURCE_START_OF_DAY)
        if not isinstance(entry, Mapping) or "coverage" in entry:
            return
        from_index = int(_num(entry.get("from_index")) or 0)
        raw = entry.get("series") if isinstance(entry.get("series"), Mapping) else {}
        series = {name: list(raw.get(name) or []) for name in SERIES_METRICS}
        normalized = _covered_entry(series, from_index)
        record[SOURCE_START_OF_DAY] = {**entry, **normalized}

    def request_save(self) -> None:
        self._dirty = True
        delay_save = getattr(self._store, "async_delay_save", None)
        if callable(delay_save):
            try:
                delay_save(self._payload_for_delayed_save, self._save_delay_s)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"save: {exc}"

    def _payload_for_delayed_save(self) -> dict[str, Any]:
        self._dirty = False
        return self._payload()

    async def async_save(self) -> bool:
        try:
            saved = self._store.async_save(self._payload())
            if inspect.isawaitable(saved):
                await saved
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"save: {exc}"
            _LOGGER.error("Forecast history: failed to save Store: %s", exc)
            return False
        self._dirty = False
        self._last_error = None
        return True

    async def async_shutdown(self) -> bool:
        if not self._dirty:
            return True
        return await self.async_save()

    # ------------------------------------------------------------------
    # Public read model
    # ------------------------------------------------------------------

    def _live_view(self) -> dict[str, Any] | None:
        payload_fn = getattr(self._timeline, "forecast_history_payload", None)
        if not callable(payload_fn):
            return None
        try:
            return view_from_timeline_payload(payload_fn())
        except Exception:  # noqa: BLE001 - the read model is best effort
            _LOGGER.debug("Forecast history: live timeline view failed", exc_info=True)
            return None

    @staticmethod
    def compare(
        forecast: Sequence[float | None], actual: Sequence[float | None]
    ) -> dict[str, Any] | None:
        """Totals over the quarters both series cover, plus the error."""
        pairs = [
            (f, a)
            for f, a in zip(forecast, actual)
            if f is not None and a is not None
        ]
        if not pairs:
            return None
        forecast_total = sum(f for f, _ in pairs)
        actual_total = sum(a for _, a in pairs)
        error = forecast_total - actual_total
        return {
            "intervals": len(pairs),
            "forecast": round(forecast_total, 3),
            "actual": round(actual_total, 3),
            "error": round(error, 3),
            "error_pct": round(100.0 * error / actual_total, 1) if actual_total > 0.05 else None,
            "mae": round(sum(abs(f - a) for f, a in pairs) / len(pairs), 4),
        }

    def _actual_for(self, key: str, live: Mapping[str, Any] | None) -> dict[str, Any] | None:
        record = self._days.get(key) or {}
        actual = record.get("actual")
        if isinstance(actual, Mapping):
            return actual
        if live is not None and live["date"].isoformat() == key:
            series = _rounded_series(live["actual"])
            # Only measured quarters count while the day is still running.
            for index, closed in enumerate(live["closed"]):
                if not closed:
                    for name in SERIES_METRICS:
                        series[name][index] = None
            return {"series": series, "totals": _series_totals(series), "partial": True}
        return None

    def _summary(self, key: str, live: Mapping[str, Any] | None) -> dict[str, Any]:
        record = self._days.get(key) or {"date": key}
        actual = self._actual_for(key, live)
        summary: dict[str, Any] = {
            "date": key,
            "mode": record.get("mode"),
            "finalized": bool(record.get("finalized")),
            "actual": dict(actual["totals"]) if actual else None,
            "actual_partial": bool(actual and actual.get("partial")),
            # Quarters measured per metric: a finished day with fewer than 96
            # had Home Assistant down for part of it (or started mid-day).
            "actual_measured": (
                {
                    name: sum(
                        1
                        for value in (actual.get("series") or {}).get(name) or []
                        if value is not None
                    )
                    for name in ENERGY_METRICS
                }
                if actual
                else None
            ),
            "revision_count": len(record.get("revisions") or []),
        }
        for source in SOURCES:
            entry = record.get(source)
            if not isinstance(entry, Mapping):
                summary[source] = None
                continue
            coverage = entry.get("coverage")
            if coverage is None and source == SOURCE_START_OF_DAY:
                coverage = INTERVAL_COUNT - int(_num(entry.get("from_index")) or 0)
            item: dict[str, Any] = {
                "captured_at": entry.get("captured_at"),
                "coverage": coverage,
                "totals": dict(entry.get("totals") or {}),
            }
            if actual:
                item["compare"] = {
                    name: self.compare(
                        (entry.get("series") or {}).get(name) or [],
                        (actual.get("series") or {}).get(name) or [],
                    )
                    for name in ENERGY_METRICS
                }
            summary[source] = item
        return summary

    def list_days(self) -> dict[str, Any]:
        live = self._live_view()
        keys = set(self._days)
        if live is not None:
            keys.add(live["date"].isoformat())
        return {
            "retention_days": self._retention_days,
            "today": self._now().date().isoformat(),
            # Real-Time Price and manual mode make no plan ahead: their
            # projection has no intervals, so only measurements are recorded.
            "today_has_plan": self._has_plan(live),
            "days": [self._summary(key, live) for key in sorted(keys, reverse=True)],
        }

    @staticmethod
    def _has_plan(live: Mapping[str, Any] | None) -> bool | None:
        """Whether today has a plan ahead; ``None`` when that is unknown.

        A missing or stale projection (not built yet, a runtime error) says
        nothing about the mode, so the panel shows no notice for it.
        """
        if live is None or live.get("stale"):
            return None
        return bool(live["extension_coverage"]) or _plans_ahead(_remaining_plan(live))

    def day_detail(self, key: str) -> dict[str, Any] | None:
        if _parse_date(key) is None:
            return None
        live = self._live_view()
        record = self._days.get(key) or {"date": key, "revisions": []}
        result = self._summary(key, live)
        actual = self._actual_for(key, live)
        result["series"] = {
            "actual": (actual or {}).get("series"),
            **{
                source: (record.get(source) or {}).get("series")
                for source in SOURCES
            },
        }
        result["revisions"] = list(record.get("revisions") or [])
        if live is not None:
            live_key = live["date"].isoformat()
            if live_key == key:
                result["series"]["current_plan"] = _rounded_series(_remaining_plan(live))
            elif (live["date"] + timedelta(days=1)).isoformat() == key and live[
                "extension_coverage"
            ]:
                result["series"]["current_plan"] = _rounded_series(live["extension"])
        return result

    def export_csv(self, kind: str = "days") -> str:
        """CSV export: one row per day, or one row per quarter-hour."""
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        live = self._live_view()
        keys = sorted(set(self._days) | ({live["date"].isoformat()} if live else set()))
        if kind == "intervals":
            header = ["date", "interval", "time"]
            for source in ("actual", *SOURCES):
                header += [f"{source}_{name}" for name in SERIES_METRICS]
            writer.writerow(header)
            for key in keys:
                record = self._days.get(key) or {}
                actual = self._actual_for(key, live) or {}
                sources = {
                    "actual": actual.get("series") or {},
                    **{
                        source: (record.get(source) or {}).get("series") or {}
                        for source in SOURCES
                    },
                }
                for index in range(INTERVAL_COUNT):
                    row: list[Any] = [
                        key,
                        index,
                        f"{index * INTERVAL_MINUTES // 60:02d}:{index * INTERVAL_MINUTES % 60:02d}",
                    ]
                    for source in ("actual", *SOURCES):
                        for name in SERIES_METRICS:
                            values = sources[source].get(name) or []
                            value = values[index] if index < len(values) else None
                            row.append("" if value is None else value)
                    writer.writerow(row)
            return buffer.getvalue()

        if kind == "revisions":
            fields = [
                "a_solar", "p_solar", "a_consumption", "p_consumption",
                "a_grid_charge", "p_grid_charge", "a_discharge", "p_discharge",
                "soc_end", "deficit", "planned_grid", "deferred_grid",
                "solar_forecast", "cov", "reason",
            ]
            writer.writerow(["date", "recorded_at", "horizon", "mode", *fields])
            for key in keys:
                for entry in (self._days.get(key) or {}).get("revisions") or []:
                    writer.writerow(
                        [key, entry.get("t"), entry.get("h"), entry.get("mode")]
                        + ["" if entry.get(name) is None else entry.get(name) for name in fields]
                    )
            return buffer.getvalue()

        header = ["date", "mode", "finalized"]
        header += [f"actual_{name}_kwh" for name in ENERGY_METRICS] + ["actual_soc_end"]
        for source in SOURCES:
            header += [f"{source}_captured_at"]
            if source == SOURCE_DAY_AHEAD:
                header += [f"{source}_coverage_intervals"]
            for name in ENERGY_METRICS:
                header += [
                    f"{source}_{name}_kwh",
                    f"{source}_{name}_actual_same_window_kwh",
                    f"{source}_{name}_error_kwh",
                ]
        writer.writerow(header)
        for key in keys:
            summary = self._summary(key, live)
            actual = summary.get("actual") or {}
            row = [key, summary.get("mode") or "", summary["finalized"]]
            row += [
                "" if actual.get(name) is None else actual.get(name)
                for name in (*ENERGY_METRICS, "soc_end")
            ]
            for source in SOURCES:
                item = summary.get(source) or {}
                row.append(item.get("captured_at") or "")
                if source == SOURCE_DAY_AHEAD:
                    row.append(item.get("coverage") or "")
                compare = item.get("compare") or {}
                totals = item.get("totals") or {}
                for name in ENERGY_METRICS:
                    metric = compare.get(name) or {}
                    forecast = metric.get("forecast", totals.get(name))
                    row += [
                        "" if forecast is None else forecast,
                        "" if metric.get("actual") is None else metric.get("actual"),
                        "" if metric.get("error") is None else metric.get("error"),
                    ]
            writer.writerow(row)
        return buffer.getvalue()


def _parse_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
