"""Calendar-month full-charge scheduling, separate from battery control.

Jeroen.nl's public MCP supplies Dutch market-price forecasts. They rank days
only; the existing pricing engine still owns grid charging and consumer prices.
"""
from __future__ import annotations

import asyncio
import calendar
from datetime import date, datetime, timedelta
import json
import logging
import math
from time import monotonic

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_FULL_CHARGE_SCHEDULE,
    WEEKDAY_MAP,
    CONF_MONTHLY_FULL_CHARGE_PRICE,
    CONF_MONTHLY_FULL_CHARGE_PRICE_ENABLED,
)

_LOGGER = logging.getLogger(__name__)
FORECAST_URL = "https://jeroen.nl/mcp"
FORECAST_SOURCE = "https://jeroen.nl/dynamische-energie/stroom/prijzen/voorspelling"


def parse_forecast(reply: dict, now: datetime, hours: float = 2.0) -> dict[date, float]:
    """Read a fresh, finite daily EUR/kWh curve from a public MCP reply."""
    result = reply.get("result", {})
    if result.get("isError") or "error" in reply:
        raise ValueError("Forecast service returned an error")
    payload = result.get("structuredContent")
    if payload is None:
        payload = json.loads(next(
            item["text"] for item in result.get("content", [])
            if item.get("type") == "text"
        ))
    made_at = dt_util.parse_datetime(payload.get("forecast_made_at_utc", ""))
    if made_at is None or made_at.tzinfo is None:
        raise ValueError("Forecast has no publication timestamp")
    age = (now - made_at).total_seconds()
    if not -300 <= age <= 36 * 3600:
        raise ValueError("Forecast is stale")
    if payload.get("unit") != "EUR/kWh" or payload.get("timezone") != "Europe/Amsterdam":
        raise ValueError("Unsupported forecast unit or timezone")
    columns = payload.get("columns", [])
    slots = {}
    for row in payload.get("rows", []):
        try:
            item = dict(zip(columns, row, strict=True))
            start = dt_util.parse_datetime(item["time_utc"])
            value = item.get("known_market_price")
            if value is None:
                value = item["forecast_p50"]
            value = float(value)
            if start is not None and start.tzinfo is not None and math.isfinite(value):
                slots[start] = (start + timedelta(minutes=15), value)
        except (KeyError, ValueError, TypeError):
            continue
    return score_days(slots, now, hours)


def score_days(slots: dict, now: datetime, hours: float) -> dict[date, float]:
    """Average cost of enough cheapest intervals to cover a full-charge window.

    Require full-day coverage, retain negative prices, weight partial intervals,
    and compare only future energy. Overlaps invalidate the curve.
    """
    days = {}
    previous_end = None
    for start, (end, value) in sorted(slots.items()):
        if previous_end is not None and start < previous_end:
            return {}
        previous_end = end
        day = start.astimezone(now.tzinfo).date()
        coverage, available = days.setdefault(day, [0.0, []])
        days[day][0] += (end - start).total_seconds() / 3600
        duration = (end - max(start, now)).total_seconds() / 3600
        if duration > 0:
            available.append((value, duration))
    scores = {}
    for day, (coverage, available) in days.items():
        if coverage < 20:
            continue
        remaining, cost = hours, 0.0
        for value, duration in sorted(available):
            take = min(remaining, duration)
            cost += take * value
            remaining -= take
            if remaining <= 1e-6:
                scores[day] = cost / hours
                break
    return scores


class MonthlyFullChargeSchedule:
    """Persist completion by battery and latch a started monthly cycle."""

    def __init__(self, manager) -> None:
        self.manager = manager
        self.last_full: dict[str, str] = {}
        self.run_date: date | None = None
        self.selected_date: date | None = None
        self.prices: dict[date, float] = {}
        self.forecast_received_at: datetime | None = None
        self.reason = "waiting"
        self._next_refresh = 0.0
        self._refresh_task = None

    @property
    def controller(self):
        return self.manager._controller

    @property
    def settings(self):
        return self.controller.config_entry.data

    @property
    def source(self) -> str:
        return self.settings.get("monthly_full_charge_source", "simple")

    @property
    def enabled(self) -> bool:
        return self.settings.get(CONF_FULL_CHARGE_SCHEDULE, "weekly") == "monthly"

    def observe_full(self, now: datetime) -> None:
        """Natural and forced full charges both satisfy a battery's month."""
        changed = False
        month = now.strftime("%Y-%m")
        for battery in self.controller.coordinators:
            if (self.last_full.get(getattr(battery, "device_key", battery.name), "").startswith(month)
                    or not self.manager.is_battery_full(battery)):
                continue
            self.last_full[getattr(battery, "device_key", battery.name)] = now.date().isoformat()
            changed = True
        if changed:
            self.manager._create_background_task(
                self.manager.save_state(), "omnibattery_monthly_completion_save"
            )

    def complete(self, now: datetime) -> bool:
        batteries = [b for b in self.controller.coordinators
                     if not getattr(b, "battery_manual_mode_enabled", False)]
        month = now.strftime("%Y-%m")
        return bool(batteries) and all(
            self.last_full.get(getattr(b, "device_key", b.name), "").startswith(month) for b in batteries
        )

    def hours_needed(self) -> float:
        gap = 0.0
        for battery in self.controller.coordinators:
            if getattr(battery, "battery_manual_mode_enabled", False):
                continue
            data = battery.data or {}
            try:
                soc = float(data.get("battery_soc") or 0)
                capacity = float(data.get("battery_total_energy") or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(soc) and math.isfinite(capacity):
                gap += max(0.0, (100 - soc) / 100) * max(0.0, capacity)
        pricing = getattr(self.controller, "_pricing_mgr", None)
        sizing = getattr(pricing, "_weekly_charge_hours_needed", None)
        hours = float(sizing(gap)) if callable(sizing) else 2.0
        return max(1.0, hours) if math.isfinite(hours) else 24.0

    def target_day(self, now: datetime) -> date | None:
        """Rolling five-day choice within the final seven calendar days."""
        if getattr(self, "_last_source", self.source) != self.source:
            self.prices = {}
            self.forecast_received_at = None
            self.__dict__.pop("_forecast_reply", None)
            self.__dict__.pop("_forecast_score_key", None)
            self.__dict__.pop("_sensor_cache_key", None)
            self._next_refresh = 0.0
        self._last_source = self.source
        today = now.date()
        last = calendar.monthrange(today.year, today.month)[1]
        if today.day < last - 6 or self.complete(now):
            return None
        if self.source == "simple":
            weekday = WEEKDAY_MAP.get(self.controller.weekly_full_charge_day, 6)
            target = today.replace(day=last)
            target -= timedelta(days=(target.weekday() - weekday) % 7)
            self.reason = "monthly_cadence"
            return max(today, target)
        if self.source == "sensor":
            self.read_sensor(now)
        if self.source == "jeroen_nl" and hasattr(self, "_forecast_reply"):
            key = (now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0), round(self.hours_needed(), 1))
            if getattr(self, "_forecast_score_key", None) != key:
                self._forecast_score_key = key
                try:
                    self.prices = parse_forecast(self._forecast_reply, now, self.hours_needed())
                except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
                    self.prices = {}
        month_end = (now.replace(day=last, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        if today.day == last or now + timedelta(hours=self.hours_needed()) >= month_end:
            self.reason = "month_end_fallback"
            return today
        fresh = (self.forecast_received_at is not None
                 and now - self.forecast_received_at <= timedelta(hours=36))
        candidates = [(price, day) for day, price in self.prices.items()
                      if fresh and today <= day <= today + timedelta(days=4)
                      and day.month == today.month and day.year == today.year]
        # Incomplete days are excluded by the provider parser.
        if not candidates:
            self.reason = "waiting_for_forecast"
            return None
        self.reason = "forecast_cheapest_day"
        return min(candidates)[1]

    def read_sensor(self, now: datetime) -> None:
        """Accept price-series sensors exposing raw_today/tomorrow/forecast slots."""
        entity = self.settings.get("monthly_full_charge_forecast_sensor")
        state = self.manager._hass.states.get(entity) if entity else None
        key = (state, now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0), round(self.hours_needed(), 1))
        if getattr(self, "_sensor_cache_key", None) == key:
            return
        self._sensor_cache_key = key
        self.prices = {}
        self.forecast_received_at = None
        if state is None or state.state in {"unknown", "unavailable"}:
            return
        if now - state.last_updated > timedelta(hours=36):
            return
        # All slots in one sensor must use the same unit/basis. Only ranking
        # uses these values; the early trigger always reads the actual tariff.
        if state.attributes.get("unit_of_measurement") not in {"EUR/kWh", "€/kWh", "EUR/MWh", "ct/kWh"}:
            return
        slots = {}
        for attr in ("raw_forecast", "raw_tomorrow", "raw_today"):
            entries = state.attributes.get(attr, [])
            if not isinstance(entries, (list, tuple)):
                continue
            for entry in entries:
                try:
                    start = dt_util.parse_datetime(entry["start"])
                    end = dt_util.parse_datetime(entry["end"])
                    value = float(entry["value"])
                    if start is None or end is None or start.tzinfo is None or end.tzinfo is None:
                        continue
                    seconds = (end - start).total_seconds()
                    if not math.isfinite(value) or not 0 < seconds <= 3600:
                        continue
                    slots[start] = (end, value)
                except (KeyError, TypeError, ValueError):
                    continue
        self.prices = score_days(slots, now, self.hours_needed())
        self.forecast_received_at = state.last_updated

    def is_active(self, now: datetime) -> bool:
        ctrl = self.controller
        if self.run_date and (self.run_date.year, self.run_date.month) != (now.year, now.month):
            if ctrl.weekly_full_charge_registers_written:
                ctrl._weekly_charge_needs_restore = True
            ctrl.weekly_full_charge_registers_written = False
            ctrl.weekly_full_charge_complete = False
            ctrl._force_full_charge = False
            self.run_date = None
            self.selected_date = None
            ctrl._weekly_charge_status["state"] = "Idle"
        if ctrl._force_full_charge:
            if self.run_date is None:
                self.run_date = now.date()
            return not ctrl.weekly_full_charge_complete
        if self.run_date and ctrl.weekly_full_charge_complete and not self.complete(now):
            ctrl.weekly_full_charge_complete = False
        if self.run_date:
            # Let the existing lifecycle finish and restore hardware limits.
            return not ctrl.weekly_full_charge_complete
        if self.complete(now):
            self.reason = "already_full_this_month"
            ctrl._weekly_charge_status["state"] = "Complete"
            return False
        self.selected_date = self.target_day(now)
        cheap = False
        if self.settings.get(CONF_MONTHLY_FULL_CHARGE_PRICE_ENABLED, False):
            pricing = getattr(ctrl, "_pricing_mgr", None)
            get_price = getattr(pricing, "_get_current_price", None)
            price = get_price() if callable(get_price) else None
            threshold = self.settings.get(CONF_MONTHLY_FULL_CHARGE_PRICE, 0.0)
            cheap = (price is not None and math.isfinite(price)
                     and price <= threshold
                     and getattr(ctrl, "predictive_charging_enabled", False)
                     and ctrl.predictive_charging_mode in {"dynamic_pricing", "realtime_price"})
        if not cheap and self.selected_date != now.date():
            return False
        self.run_date = now.date()
        self.selected_date = now.date()
        if cheap:
            self.reason = "price_threshold"
        ctrl.weekly_full_charge_complete = False
        ctrl._dp_config_dirty = True
        self.manager._create_background_task(
            self.manager.save_state(), "omnibattery_monthly_start_save"
        )
        return True

    def refresh_if_due(self, now: datetime) -> None:
        """Fetch outside the control loop; outages retry hourly, success six-hourly."""
        last = calendar.monthrange(now.year, now.month)[1]
        if (self.source != "jeroen_nl" or now.day < last - 6 or self.complete(now) or monotonic() < self._next_refresh
                or (self._refresh_task is not None and not self._refresh_task.done())):
            return
        self._next_refresh = monotonic() + 3600
        self._refresh_task = self.manager._create_background_task(
            self._refresh(), "omnibattery_monthly_forecast"
        )

    async def _refresh(self) -> None:
        try:
            session = async_get_clientsession(self.manager._hass)
            async with session.post(
                FORECAST_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "get_electricity_price_forecast",
                    "arguments": {"days": 5, "resolution": "quarter"},
                }},
                headers={"Accept": "application/json, text/event-stream"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                response.raise_for_status()
                reply = await response.json()
                prices = parse_forecast(reply, dt_util.now(), self.hours_needed())
                payload = reply["result"].get("structuredContent")
                if payload is None:
                    payload = json.loads(next(x["text"] for x in reply["result"]["content"] if x.get("type") == "text"))
                made_at = dt_util.parse_datetime(payload["forecast_made_at_utc"])
            if self.source != "jeroen_nl":
                return
            self.__dict__.pop("_forecast_score_key", None)
            self._forecast_reply = reply
            self.prices = prices
            self.forecast_received_at = made_at
            self._next_refresh = monotonic() + 6 * 3600
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError,
                TypeError, StopIteration, AttributeError) as err:
            _LOGGER.warning("Monthly full charge: Jeroen.nl forecast unavailable: %s", err)

    def dump(self) -> dict:
        return {"last_full": dict(self.last_full),
                "run_date": self.run_date.isoformat() if self.run_date else None}

    def load(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        history = data.get("last_full", {})
        if not isinstance(history, dict):
            history = {}
        for name, value in history.items():
            try:
                self.last_full[str(name)] = date.fromisoformat(value).isoformat()
            except (TypeError, ValueError):
                continue
        try:
            self.run_date = date.fromisoformat(data["run_date"])
        except (KeyError, TypeError, ValueError):
            self.run_date = None
