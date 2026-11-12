"""Scenario bank for the predictive (Dynamic Pricing) charge planner.

Every ``tests/scenarios/*.json`` file is one reported night: the prices, the
fleet, the forecasts and the settings, plus what the plan should look like.
See ``tests/scenarios/README.md`` for the format.

Two levels, chosen by the file:

* ``allocator`` builds a ``ChronologicalEvaluationRequest`` from the file and
  calls the pure ``evaluate_chronological_request``.
* ``engine`` runs the real ``PricingManager._evaluate_dynamic_pricing`` on a
  stub controller, so the horizon, the fleet power, the arbitrage gating and
  the scalar balance are the production ones.

Same conventions as ``test_pricing_engine.py``: no hardware, no running Home
Assistant, naive local wall clock, the clock pinned to the file's ``now``.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import (
    CHARGE_EFFICIENCY,
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PRICE_INTEGRATION_NORDPOOL,
)
from custom_components.omnibattery.pricing import PriceSlot, calculations
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.chronological import (
    ChronologicalEvaluationRequest,
    EnergyInterval,
    cap_battery_drain,
    evaluate_chronological_request,
)
from custom_components.omnibattery.pricing.engine import (
    DynamicPricingEvaluationHorizon,
    PricingManager,
)
from custom_components.omnibattery.pricing.solar_timeline import build_boundaries
from custom_components.omnibattery.solar_forecast import (
    SolarForecastInput,
    SolarForecastPeriod,
    solar_forecast_period_energy_between,
)
from custom_components.omnibattery.tracking.consumption_profile import (
    INTERVAL_COUNT,
    ConsumptionForecast,
)
from tests.conftest import FakeCoordinator

SCENARIO_DIR = Path(__file__).parent / "scenarios"
QUARTER = timedelta(minutes=15)
_EPS = 1e-6

_TOP_KEYS = {
    "source", "note", "level", "xfail", "now", "time_zone", "sun", "prices",
    "consumption", "solar", "fleet", "settings", "expected",
}
_OPTIONAL_TOP_KEYS = {"xfail"}
_SETTINGS_KEYS = {
    "max_price_threshold", "min_arbitrage_margin", "round_trip_efficiency",
    "discharge_reserve", "peak_shaving", "contracted_power_w",
    "guaranteed_floor_soc", "safety_margin_kwh",
}
_EXPECTED_KEYS = {
    "reason", "total_kwh", "windows", "no_slot_before", "no_slot_after",
    "horizon_reaches", "no_charge_above_use_value",
}
_BATTERY_KEYS = {
    "capacity_kwh", "soc", "min_soc", "max_soc", "max_charge_w", "max_discharge_w",
}


class ScenarioError(Exception):
    """A bad scenario file or a harness defect. Never an expected failure."""


class ScenarioMismatch(AssertionError):
    """The plan differs from the file's ``expected``: the only thing ``xfail`` covers."""


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    name: str
    level: str
    now: datetime
    time_zone: str
    sunrise_h: float
    solar_noon_h: float
    slots: tuple[PriceSlot, ...]
    bins_kwh: tuple[float, ...]
    solar_remaining_kwh: float
    solar_periods: tuple[SolarForecastPeriod, ...]
    fleet: tuple[dict, ...]
    settings: dict
    expected: dict

    @property
    def horizon_end(self) -> datetime:
        """The control horizon: the next local day's sunrise."""
        midnight = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight + timedelta(days=1, hours=self.sunrise_h)

    @property
    def valued(self) -> bool:
        margin = self.settings["min_arbitrage_margin"]
        return bool(margin and margin > 0)


def _keys(where: str, given, allowed: set, optional: set = frozenset()) -> None:
    unknown, missing = set(given) - allowed, allowed - optional - set(given)
    if unknown or missing:
        raise ScenarioError(
            f"{where}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}"
        )


def _hours(value: str) -> float:
    hour, minute = value.split(":")
    return int(hour) + int(minute) / 60.0


def _price_slots(spec: dict) -> tuple[PriceSlot, ...]:
    _keys("prices", spec, {"slot_minutes", "blocks"})
    step = timedelta(minutes=spec["slot_minutes"])
    slots: list[PriceSlot] = []
    for block in spec["blocks"]:
        _keys("prices.blocks[]", block, {"start", "end", "price"})
        start = datetime.fromisoformat(block["start"])
        end = datetime.fromisoformat(block["end"])
        if (end - start) % step or end <= start:
            raise ScenarioError(f"price block {block} is not a multiple of slot_minutes")
        if slots and start < slots[-1].end:
            raise ScenarioError(f"price block {block} overlaps the previous one")
        while start < end:
            slots.append(PriceSlot(start, start + step, float(block["price"])))
            start += step
    return tuple(slots)


def _consumption_bins(spec: dict) -> tuple[float, ...]:
    """The household's daily shape as 96 quarter-hour kWh values."""
    _keys(
        "consumption", spec, {"intervals_kwh", "profile_kw", "daily_total_kwh"},
        {"intervals_kwh", "profile_kw", "daily_total_kwh"},
    )
    if ("intervals_kwh" in spec) == ("profile_kw" in spec):
        raise ScenarioError("consumption needs exactly one of intervals_kwh / profile_kw")
    if "intervals_kwh" in spec:
        bins = [float(value) for value in spec["intervals_kwh"]]
        if len(bins) != INTERVAL_COUNT:
            raise ScenarioError(f"intervals_kwh needs {INTERVAL_COUNT} values")
    else:
        steps = [(_hours(step["from"]), float(step["kw"])) for step in spec["profile_kw"]]
        if not steps or steps[0][0] != 0.0 or steps != sorted(steps, key=lambda s: s[0]):
            raise ScenarioError("profile_kw must start at 00:00 and be in time order")
        bins = [
            [kw for start, kw in steps if start <= index / 4.0][-1] * 0.25
            for index in range(INTERVAL_COUNT)
        ]
    total = spec.get("daily_total_kwh")
    if total is not None:
        bins = [value * float(total) / sum(bins) for value in bins]
    return tuple(bins)


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _keys(path.name, raw, _TOP_KEYS, _OPTIONAL_TOP_KEYS)
    _keys("source", raw["source"], {"issue", "kind", "reporter", "date", "url"})
    if raw["source"]["kind"] not in ("issue", "discussion", "derived", "synthetic"):
        raise ScenarioError(f"unknown source kind {raw['source']['kind']!r}")
    _keys("settings", raw["settings"], _SETTINGS_KEYS)
    _keys("expected", raw["expected"], _EXPECTED_KEYS, _EXPECTED_KEYS)
    _keys("sun", raw["sun"], {"sunrise", "solar_noon"})
    _keys("solar", raw["solar"], {"remaining_kwh", "periods"})
    if not raw["expected"]:
        raise ScenarioError("expected is empty: the scenario asserts nothing")
    if raw["level"] not in ("allocator", "engine"):
        raise ScenarioError(f"unknown level {raw['level']!r}")
    peak = raw["settings"]["peak_shaving"]
    if peak is not None:
        _keys("settings.peak_shaving", peak, {"limit_w", "soc_threshold"})
    for battery in raw["fleet"]:
        _keys("fleet[]", battery, _BATTERY_KEYS)
    zone = ZoneInfo(raw["time_zone"])
    periods = []
    for period in raw["solar"]["periods"]:
        _keys("solar.periods[]", period, {"start", "end", "kwh"})
        periods.append(
            SolarForecastPeriod(
                datetime.fromisoformat(period["start"]).replace(tzinfo=zone),
                datetime.fromisoformat(period["end"]).replace(tzinfo=zone),
                period["kwh"],
            )
        )
    return Scenario(
        name=path.stem,
        level=raw["level"],
        now=datetime.fromisoformat(raw["now"]),
        time_zone=raw["time_zone"],
        sunrise_h=_hours(raw["sun"]["sunrise"]),
        solar_noon_h=_hours(raw["sun"]["solar_noon"]),
        slots=_price_slots(raw["prices"]),
        bins_kwh=_consumption_bins(raw["consumption"]),
        solar_remaining_kwh=float(raw["solar"]["remaining_kwh"]),
        solar_periods=tuple(periods),
        fleet=tuple(raw["fleet"]),
        settings=raw["settings"],
        expected=raw["expected"],
    )


# ----------------------------------------------------------------------
# Outcome shared by both levels
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    reason: str
    # (slot, battery-side kWh) the plan books, in time order.
    booked: tuple[tuple[PriceSlot, float], ...]
    # The request the allocator received (the engine's first build).
    request: ChronologicalEvaluationRequest

    def describe(self) -> str:
        request = self.request
        lines = [
            f"reason={self.reason}  total={sum(k for _s, k in self.booked):.3f} kWh"
            f"  horizon_end={request.horizon_end:%Y-%m-%d %H:%M}",
            f"request: usable={request.usable_initial_kwh:.3f} kWh"
            f" headroom={request.headroom_kwh:.3f} kWh"
            f" required={request.total_required_kwh:.3f} kWh"
            f" power={request.effective_power_kw:.3f} kW"
            f" value_efficiency={request.import_value_efficiency}"
            f" margin={request.import_value_margin}"
            f" reserve_holds={request.reserve_holds}"
            f" from_deadlines={request.required_from_deadlines}",
        ]
        lines += [
            f"  {slot.start:%d %H:%M}-{slot.end:%H:%M} @{slot.price:.3f}: {kwh:.3f} kWh"
            for slot, kwh in self.booked
        ] or ["  (no slot booked)"]
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Level "allocator"
# ----------------------------------------------------------------------

def _run_allocator(scenario: Scenario) -> Outcome:
    """Adapt the file the simplest way the engine would, with pure functions only."""
    settings = scenario.settings
    if (
        settings["peak_shaving"] is not None
        or settings["guaranteed_floor_soc"] is not None
        or settings["safety_margin_kwh"]
        or (scenario.solar_remaining_kwh > 0 and not scenario.solar_periods)
    ):
        raise ScenarioError(
            "the allocator level has no peak shaving, guaranteed floor, safety "
            "margin or undated solar: use the engine level"
        )
    now, horizon_end = scenario.now, scenario.horizon_end
    zone = ZoneInfo(scenario.time_zone)
    intervals = []
    for start, end in build_boundaries(now, horizon_end):
        intervals.append(
            EnergyInterval(
                start,
                end,
                scenario.bins_kwh[start.hour * 4 + start.minute // 15]
                * ((end - start) / QUARTER),
                solar_forecast_period_energy_between(
                    scenario.solar_periods, start, end, timezone=zone
                ),
            )
        )
    intervals = cap_battery_drain(
        intervals, sum(b["max_discharge_w"] for b in scenario.fleet) / 1000.0
    )

    def energy(low: str, high: str) -> float:
        return sum(
            max(0.0, b[high] - b[low]) / 100.0 * b["capacity_kwh"]
            for b in scenario.fleet
        )

    usable, headroom = energy("min_soc", "soc"), energy("soc", "max_soc")
    balance = (
        sum(item.consumption_kwh for item in intervals)
        - usable
        - sum(item.solar_kwh for item in intervals)
    )
    margin, efficiency = settings["min_arbitrage_margin"], settings["round_trip_efficiency"]
    slots = [s for s in scenario.slots if s.end > now and s.start <= horizon_end]
    slot_ceilings, _reported = calculations.effective_charge_ceilings(
        slots, settings["max_price_threshold"], margin, efficiency, now=now
    )
    contracted_kw = settings["contracted_power_w"] / 1000.0
    request = ChronologicalEvaluationRequest(
        now=now,
        horizon_end=horizon_end,
        intervals=tuple(intervals),
        price_slots=tuple(slots),
        total_required_kwh=min(headroom, max(0.0, balance)),
        effective_power_kw=min(
            contracted_kw, sum(b["max_charge_w"] for b in scenario.fleet) / 1000.0
        ),
        headroom_kwh=headroom,
        usable_initial_kwh=usable,
        max_price_threshold=settings["max_price_threshold"],
        premium_price_threshold=settings["max_price_threshold"],
        slot_ceilings=slot_ceilings,
        import_value_efficiency=efficiency if scenario.valued else None,
        import_value_margin=margin if scenario.valued else 0.0,
        reserve_holds=scenario.valued and settings["discharge_reserve"],
        grid_carries_load_while_charging=True,
        grid_ceiling_kw=contracted_kw,
    )
    plan = evaluate_chronological_request(request).plan
    return Outcome(
        plan.reason,
        tuple((a.slot, a.planned_battery_kwh) for a in plan.allocations),
        request,
    )


# ----------------------------------------------------------------------
# Level "engine"
# ----------------------------------------------------------------------

class _Tracker:
    """Consumption tracker stand-in: a learned profile equal to the file's shape."""

    solar_profile = None

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario
        zone = ZoneInfo(scenario.time_zone)
        self.consumption_profile = SimpleNamespace(_timezone=lambda: zone)

    async def get_dynamic_base_consumption(self) -> float:
        return sum(self._scenario.bins_kwh)

    def calculate_sunrise(self, for_date=None) -> float:
        return self._scenario.sunrise_h

    def calculate_solar_noon(self, for_date=None) -> float:
        return self._scenario.solar_noon_h

    def forecast_consumption_between(self, start, end, *, fallback="legacy_daily"):
        """Mirror ``ConsumptionProfile.forecast_energy_between`` for a mature profile."""
        zone = ZoneInfo(self._scenario.time_zone)
        cursor, end = (
            value if value.tzinfo is None
            else value.astimezone(zone).replace(tzinfo=None)
            for value in (start, end)
        )
        bins = self._scenario.bins_kwh
        aggregate = [0.0] * INTERVAL_COUNT
        by_date: dict = {}
        while cursor < end:
            index = cursor.hour * 4 + cursor.minute // 15
            boundary = cursor.replace(
                minute=cursor.minute // 15 * 15, second=0, microsecond=0
            ) + QUARTER
            segment_end = min(end, boundary)
            aggregate[index] += bins[index] * ((segment_end - cursor) / QUARTER)
            by_date.setdefault(cursor.date(), list(bins))
            cursor = segment_end
        return ConsumptionForecast(
            sum(aggregate), aggregate, "profile", True,
            coverage_ratio=1.0, total_days=28, intervals_by_date=by_date,
        )


def _pin_clock(monkeypatch, manager: PricingManager, scenario: Scenario) -> None:
    """Pin every clock the evaluation reads to the scenario's ``now``.

    ``_now`` is the manager's own seam; the module ``datetime`` is patched the
    way the existing pricing tests do, for the reads that still bypass it.
    """
    zone = ZoneInfo(scenario.time_zone)

    class _ClockMeta(type):
        # The engine tests values with ``isinstance(x, datetime)`` against its
        # module-level name; a plain subclass would reject real datetimes.
        def __instancecheck__(cls, instance) -> bool:
            return isinstance(instance, datetime)

    class _Clock(datetime, metaclass=_ClockMeta):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return scenario.now
            return scenario.now.replace(tzinfo=zone).astimezone(tz)

    manager._now = lambda: scenario.now
    monkeypatch.setattr(pricing_engine, "datetime", _Clock, raising=False)
    monkeypatch.setattr(calculations, "datetime", _Clock, raising=False)


def _controller(scenario: Scenario) -> SimpleNamespace:
    settings = scenario.settings
    peak = settings["peak_shaving"]
    floor = settings["guaranteed_floor_soc"]
    coordinators = [
        FakeCoordinator(
            name=f"battery_{index}",
            device_key=f"scenario_{index}",
            data={
                "battery_soc": float(battery["soc"]),
                "battery_total_energy": float(battery["capacity_kwh"]),
            },
            min_soc=battery["min_soc"],
            max_soc=battery["max_soc"],
            max_charge_power=battery["max_charge_w"],
            max_discharge_power=battery["max_discharge_w"],
        )
        for index, battery in enumerate(scenario.fleet, start=1)
    ]
    controller = SimpleNamespace(
        config_entry=SimpleNamespace(data={}, options={}),
        coordinators=coordinators,
        _consumption_tracker=_Tracker(scenario),
        # Settings from the file.
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        max_price_threshold=settings["max_price_threshold"],
        min_arbitrage_margin=settings["min_arbitrage_margin"],
        round_trip_efficiency=settings["round_trip_efficiency"],
        max_contracted_power=settings["contracted_power_w"],
        max_charge_capacity=sum(b["max_charge_w"] for b in scenario.fleet),
        capacity_protection_enabled=peak is not None,
        capacity_protection_limit=peak["limit_w"] if peak else 0,
        capacity_protection_soc_threshold=peak["soc_threshold"] if peak else 0,
        _predictive_min_soc_floor_enabled=floor is not None,
        _predictive_min_soc_floor=floor or 0.0,
        _predictive_safety_margin_kwh=settings["safety_margin_kwh"],
        _discharge_reserve_mgr=SimpleNamespace(
            feature_enabled=lambda: bool(settings["discharge_reserve"]),
            mark_stale=lambda _reason: None,
        ),
        # Features the bank does not model: off.
        dp_price_discharge_control=False,
        discharge_price_threshold=None,
        negative_price_charging_enabled=False,
        smart_predischarge_enabled=False,
        charge_delay_enabled=False,
        solar_profile_mode="active",
        price_sensor="sensor.scenario_prices",
        price_integration_type=PRICE_INTEGRATION_NORDPOOL,
        _solar_t_start=None,
        _external_loads=None,
        _daily_consumption_history=[],
        # Runtime state the evaluation reads and writes.
        _dynamic_pricing_schedule=None,
        _dynamic_pricing_evaluated_date=None,
        _dp_eval_retry_count=0,
        _dp_price_publication_reeval_date=None,
        _dp_arbitrage_ceiling=None,
        _dp_daily_avg_price=None,
        _dp_last_eval_soc=None,
        _last_decision_data=None,
        _last_chronological_diagnostics=None,
    )
    # The controller's own balance and planning helpers, unbound on the stub
    # (the idiom of test_weekly_predictive_charge.py).
    for name in (
        "_should_activate_grid_charging",
        "_planning_floor_soc",
        "_charge_ceiling_soc",
        "_is_capacity_protection_soc_limited",
        "_capacity_protection_coordinators",
    ):
        method = getattr(ChargeDischargeController, name)
        setattr(
            controller, name,
            lambda *args, _method=method, **kwargs: _method(controller, *args, **kwargs),
        )
    controller._is_battery_manual_owned = ChargeDischargeController._is_battery_manual_owned
    # Nameplate power: no temperature, taper or slot derating in a scenario.
    controller._battery_power_limit = lambda coordinator, is_charging: (
        coordinator.max_charge_power if is_charging else coordinator.max_discharge_power
    )
    return controller


def _run_engine(scenario: Scenario, monkeypatch) -> Outcome:
    controller = _controller(scenario)
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone=scenario.time_zone),
        states=SimpleNamespace(get=lambda _entity_id: None),
    )
    controller.hass = hass
    manager = PricingManager(hass, controller)
    controller._pricing_mgr = manager
    _pin_clock(monkeypatch, manager, scenario)

    async def _no_io(*_args, **_kwargs):
        return None

    solar_input = SolarForecastInput(
        scenario.solar_remaining_kwh,
        "remaining_sensor",
        periods=scenario.solar_periods or None,
        original_source="remaining",
    )
    # The only replaced collaborators are the ones that would do I/O: the
    # price feed, the forecast sensor, the service refresh and the notification.
    manager._raw_price_slots = lambda *, quiet=False: (list(scenario.slots), "no_slots")
    manager._read_remaining_solar_input = lambda **_kwargs: solar_input
    manager._maybe_refresh_service_prices = _no_io
    manager._send_dynamic_pricing_notification = _no_io

    requests: list[ChronologicalEvaluationRequest] = []
    evaluate = manager.evaluate_chronological_projection

    def _recording(request):
        requests.append(request)
        return evaluate(request)

    manager.evaluate_chronological_projection = _recording

    asyncio.run(
        manager._evaluate_dynamic_pricing(
            horizon=DynamicPricingEvaluationHorizon.REMAINING
        )
    )

    decision = controller._last_decision_data or {}
    reason = decision.get("chronological_plan_reason")
    # Not a ScenarioMismatch: an xfail must never hide a planner fallback.
    assert reason is not None, "the chronological planner did not run"
    assert not str(reason).startswith("fallback"), f"planner fell back: {reason}"
    if not requests:
        raise ScenarioError("no chronological request reached the allocator")
    request = requests[0]
    _check_wiring(scenario, request, decision)

    schedule = controller._dynamic_pricing_schedule
    booked: dict[PriceSlot, float] = {}
    if schedule is not None and schedule.deficit_charging_needed:
        if not schedule.chronological_planning_active:
            raise ScenarioError("the schedule was not built by the chronological planner")
        booked = dict(schedule.slot_energy_targets_kwh)
    return Outcome(
        str(reason),
        tuple(sorted(booked.items(), key=lambda item: item[0].start)),
        request,
    )


def _check_wiring(scenario: Scenario, request, decision: dict) -> None:
    """The stub reached the planner with the file's settings, not a silent default."""
    settings = scenario.settings
    expected = {
        "now": scenario.now,
        "max_price_threshold": settings["max_price_threshold"],
        "import_value_efficiency": (
            settings["round_trip_efficiency"] if scenario.valued else None
        ),
        "import_value_margin": (
            settings["min_arbitrage_margin"] if scenario.valued else 0.0
        ),
        "reserve_holds": bool(scenario.valued and settings["discharge_reserve"]),
    }
    wrong = {
        name: (getattr(request, name), value)
        for name, value in expected.items()
        if getattr(request, name) != value
    }
    if decision.get("chronological_source") != "profile":
        wrong["chronological_source"] = (decision.get("chronological_source"), "profile")
    if request.effective_power_kw <= 0:
        wrong["effective_power_kw"] = (request.effective_power_kw, "> 0")
    if wrong:
        raise ScenarioError(f"harness wiring (got, wanted): {wrong}")


# ----------------------------------------------------------------------
# Assertions
# ----------------------------------------------------------------------

def use_value_violations(
    outcome: Outcome, prices: tuple[PriceSlot, ...], efficiency: float
) -> list[str]:
    """Booked slots that cost more than the import their energy displaces.

    The plan is replayed first in, first out: the battery covers the house
    (load less solar) with the oldest stored energy, a slot charges from its
    start at the planned power until its energy is stored and the grid carries
    the house meanwhile. A slot's use price is the average import price of
    the quarter-hours that drain its energy; energy still stored at the end of
    the horizon is not valued. A slot fails when
    ``price > use price x round-trip efficiency``.
    """
    request = outcome.request
    rate = request.effective_power_kw * CHARGE_EFFICIENCY
    capacity = request.usable_initial_kwh + request.headroom_kwh
    # FIFO batches: [kWh left, booked slot or None for free energy].
    stored: list[list] = [[request.usable_initial_kwh, None]]
    pending = {slot: kwh for slot, kwh in outcome.booked}
    drained: dict[PriceSlot, list[float]] = {slot: [0.0, 0.0] for slot in pending}
    for interval in sorted(request.intervals, key=lambda item: item.start):
        hours = (interval.end - interval.start).total_seconds() / 3600.0
        charging_hours = 0.0
        for slot, left in pending.items():
            if left <= _EPS or not (slot.start <= interval.start < slot.end):
                continue
            charging_hours = min(hours, left / rate) if rate > 0 else 0.0
            energy = min(left, rate * charging_hours)
            pending[slot] = left - energy
            stored.append([energy, slot])
        flow = (interval.solar_kwh - interval.consumption_kwh) * (
            1.0 - charging_hours / hours
        )
        if flow > 0:
            room = capacity - sum(batch[0] for batch in stored)
            stored.append([max(0.0, min(flow, room)), None])
            continue
        price = next(
            (s.price for s in prices if s.start <= interval.start < s.end), None
        )
        need = -flow
        for batch in stored:
            take = min(need, batch[0])
            batch[0] -= take
            need -= take
            if take > 0 and batch[1] is not None and price is not None:
                drained[batch[1]][0] += take
                drained[batch[1]][1] += take * price
    violations = []
    for slot, (kwh, cost) in sorted(drained.items(), key=lambda item: item[0].start):
        if kwh <= _EPS:
            continue
        use_price = cost / kwh
        if slot.price > use_price * efficiency + _EPS:
            violations.append(
                f"{slot.start:%d %H:%M} @{slot.price:.3f} > {use_price * efficiency:.3f}"
                f" (use price {use_price:.3f} x efficiency {efficiency:.2f},"
                f" {kwh:.2f} kWh used)"
            )
    return violations


def check_expected(scenario: Scenario, outcome: Outcome) -> None:
    expected = scenario.expected
    failures: list[str] = []

    def kwh_between(start: datetime, end: datetime) -> float:
        return sum(k for slot, k in outcome.booked if start <= slot.start < end)

    def in_range(label: str, value: float, spec: dict) -> None:
        low, high = spec.get("min_kwh"), spec.get("max_kwh")
        if (low is not None and value < low - _EPS) or (
            high is not None and value > high + _EPS
        ):
            failures.append(f"{label}: {value:.3f} kWh, expected {low} .. {high}")

    if "reason" in expected and outcome.reason != expected["reason"]:
        failures.append(f"reason {outcome.reason!r}, expected {expected['reason']!r}")
    if "total_kwh" in expected:
        _keys("expected.total_kwh", expected["total_kwh"], {"min_kwh", "max_kwh"}, {"min_kwh", "max_kwh"})
        in_range("total", sum(k for _s, k in outcome.booked), expected["total_kwh"])
    for window in expected.get("windows", ()):
        _keys("expected.windows[]", window, {"start", "end", "min_kwh", "max_kwh"}, {"min_kwh", "max_kwh"})
        in_range(
            f"window {window['start']} .. {window['end']}",
            kwh_between(
                datetime.fromisoformat(window["start"]),
                datetime.fromisoformat(window["end"]),
            ),
            window,
        )
    if "no_slot_before" in expected:
        limit = datetime.fromisoformat(expected["no_slot_before"])
        early = [s for s, k in outcome.booked if k > _EPS and s.start < limit]
        if early:
            failures.append(f"{len(early)} slot(s) booked before {limit:%d %H:%M}")
    if "no_slot_after" in expected:
        limit = datetime.fromisoformat(expected["no_slot_after"])
        late = [s for s, k in outcome.booked if k > _EPS and s.end > limit]
        if late:
            failures.append(f"{len(late)} slot(s) booked after {limit:%d %H:%M}")
    if "horizon_reaches" in expected:
        limit = datetime.fromisoformat(expected["horizon_reaches"])
        if outcome.request.horizon_end < limit:
            failures.append(
                f"horizon ends {outcome.request.horizon_end:%d %H:%M}, "
                f"expected to reach {limit:%d %H:%M}"
            )
    if expected.get("no_charge_above_use_value"):
        failures += [
            f"charged above its use value: {line}"
            for line in use_value_violations(
                outcome, scenario.slots, scenario.settings["round_trip_efficiency"]
            )
        ]
    if failures:
        raise ScenarioMismatch(
            f"{scenario.name}:\n- " + "\n- ".join(failures) + "\n" + outcome.describe()
        )


def run_scenario(scenario: Scenario, monkeypatch) -> Outcome:
    if scenario.level == "allocator":
        return _run_allocator(scenario)
    return _run_engine(scenario, monkeypatch)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

def _params():
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        reason = json.loads(path.read_text(encoding="utf-8")).get("xfail")
        marks = (
            [pytest.mark.xfail(strict=True, reason=reason, raises=ScenarioMismatch)]
            if reason
            else []
        )
        yield pytest.param(path, id=path.stem, marks=marks)


@pytest.mark.parametrize("path", list(_params()))
def test_scenario(path, monkeypatch):
    scenario = load_scenario(path)
    check_expected(scenario, run_scenario(scenario, monkeypatch))


def test_the_bank_has_scenarios_at_both_levels():
    levels = {load_scenario(path).level for path in SCENARIO_DIR.glob("*.json")}
    assert levels == {"allocator", "engine"}


def test_an_unknown_key_is_rejected(tmp_path):
    raw = json.loads(next(SCENARIO_DIR.glob("*.json")).read_text(encoding="utf-8"))
    raw["settings"]["min_arbitrage_margn"] = 0.05
    path = tmp_path / "typo.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ScenarioError, match="min_arbitrage_margn"):
        load_scenario(path)


def test_use_value_replay_flags_a_charge_used_at_its_own_price():
    """1 kWh bought at 0.40 and used in the same 0.40 hour loses the round trip;
    the same kWh used in a 0.60 hour does not."""
    base = datetime(2026, 1, 1)

    def outcome(use_hour: int) -> Outcome:
        intervals = tuple(
            EnergyInterval(
                base + index * QUARTER,
                base + (index + 1) * QUARTER,
                0.25 if index // 4 == use_hour else 0.0,
                0.0,
            )
            for index in range(12)
        )
        request = ChronologicalEvaluationRequest(
            now=base,
            horizon_end=base + timedelta(hours=3),
            intervals=intervals,
            price_slots=prices,
            total_required_kwh=1.0,
            effective_power_kw=4.0 / CHARGE_EFFICIENCY,
            headroom_kwh=5.0,
        )
        return Outcome("ok", ((prices[0], 0.75),), request)

    prices = (
        PriceSlot(base, base + timedelta(hours=1), 0.40),
        PriceSlot(base + timedelta(hours=1), base + timedelta(hours=2), 0.60),
        PriceSlot(base + timedelta(hours=2), base + timedelta(hours=3), 0.40),
    )

    # Hour 0: charged in the first quarter (the grid carries the house), used
    # in the other three at 0.40 -> 0.40 > 0.40 x 0.85.
    assert len(use_value_violations(outcome(0), prices, 0.85)) == 1
    # Hour 1 at 0.60 -> 0.40 <= 0.51.
    assert use_value_violations(outcome(1), prices, 0.85) == []
