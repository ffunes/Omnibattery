"""Regression: the manual "Re-evaluate Dynamic Pricing" button's extended
horizon must reach through end of tomorrow, not a fixed +12h.

``extended_horizon`` was written for ``startup_evaluation`` (HA restarted after
the 00:05 window was missed) — the +12h floor guaranteed a minimum planning
window even for a restart minutes before midnight. The re-evaluate button
reuses the same flag for an unrelated case: a user asking for an updated plan
at an arbitrary time of day. Pressed in the evening (e.g. 21:00), +12h only
reaches ~09:00 the next day, silently excluding the rest of tomorrow's already
-published EPEX prices — including the cheap midday slots typical of
solar-heavy markets (afternoon oversupply depresses the day-ahead price).

Widening the request is always safe: ``_parse_price_data`` only ever returns
slots that exist in the price sensor's data, so on days where the day-ahead
publish is partial (observed in practice: some days only populated through
noon the next day) a wide horizon just yields fewer slots — it never waits or
errors.

No hardware, no running Home Assistant: ``PricingManager`` only stores its
``hass``/``controller`` references, matching the style of
``test_price_data_health.py`` and ``test_min_soc_floor.py``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import PRICE_INTEGRATION_EPEX
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import PricingManager


# ----------------------------------------------------------------------
# Part 1: the horizon boundary _evaluate_dynamic_pricing actually requests
# ----------------------------------------------------------------------

class _FixedDatetime(datetime):
    """datetime subclass whose .now() is pinned, everything else inherited."""
    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


@pytest.fixture
def frozen_evening(monkeypatch):
    """Pin 'now' to 21:03 on an arbitrary day — after EPEX has published
    tomorrow, well past the old +12h floor's usable range."""
    fixed = datetime(2026, 7, 31, 21, 3, 0)
    frozen = type("_Frozen", (_FixedDatetime,), {"_fixed": fixed})
    monkeypatch.setattr(pricing_engine, "datetime", frozen)
    return fixed


def _minimal_ctrl(*, extended=True):
    """Just enough for _evaluate_dynamic_pricing to reach the horizon
    computation and then exit cleanly via the no-price-data retry branch
    (charging_needed=True + slots=[] logs a warning and returns — no
    notification machinery needed)."""
    async def _should_activate():
        return {"should_charge": True, "avg_soc": 50.0, "energy_deficit_kwh": 1.0}

    return SimpleNamespace(
        _dp_arbitrage_ceiling=None,
        _should_activate_grid_charging=_should_activate,
        _last_decision_data=None,
        _dp_last_eval_soc=None,
        _dp_eval_retry_count=0,
        _dp_daily_avg_price=None,
    )


def _mgr_recording_horizon(ctrl):
    mgr = PricingManager(SimpleNamespace(), ctrl)
    captured = {}

    async def _no_refresh(force=False):
        return None

    def _recorder(*, horizon_end=None, quiet=False):
        captured["horizon_end"] = horizon_end
        return []

    mgr._maybe_refresh_service_prices = _no_refresh
    mgr._parse_price_data = _recorder
    return mgr, captured


def test_extended_horizon_reaches_end_of_tomorrow(frozen_evening):
    ctrl = _minimal_ctrl()
    mgr, captured = _mgr_recording_horizon(ctrl)

    asyncio.run(mgr._evaluate_dynamic_pricing(extended_horizon=True))

    expected = datetime(2026, 8, 1, 23, 59, 59)
    assert captured["horizon_end"] == expected


def test_extended_horizon_evening_press_exceeds_old_twelve_hour_floor(frozen_evening):
    """The bug this fix closes: at 21:03, the old max(end_of_today, now+12h)
    resolved to ~09:03 the next day — well short of tomorrow's afternoon
    cheap slots. Confirm the new horizon clears that old boundary."""
    ctrl = _minimal_ctrl()
    mgr, captured = _mgr_recording_horizon(ctrl)

    asyncio.run(mgr._evaluate_dynamic_pricing(extended_horizon=True))

    old_boundary = datetime(2026, 8, 1, 9, 3, 0)  # now + 12h, the old floor
    assert captured["horizon_end"] > old_boundary


def test_non_extended_horizon_is_unaffected(frozen_evening):
    """The normal 00:05 daily run (extended_horizon=False) must keep its
    today-only semantics — this fix only changes the manual/startup path."""
    ctrl = _minimal_ctrl()
    mgr, captured = _mgr_recording_horizon(ctrl)

    asyncio.run(mgr._evaluate_dynamic_pricing(extended_horizon=False))

    assert captured["horizon_end"] is None


# ----------------------------------------------------------------------
# Part 2: _parse_price_data against real (unmocked) EPEX data — proves the
# wide horizon is safe on a day where tomorrow's publish is only partial.
# ----------------------------------------------------------------------

def _epex_slots(entries):
    return [
        {
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "price_per_kwh": price,
        }
        for start, end, price in entries
    ]


def _manager_with_epex_data(monkeypatch, entries, *, now):
    monkeypatch.setattr(pricing_engine, "resolve_official_nordpool_source", lambda *_a: None)
    state = SimpleNamespace(state="0.30", attributes={"data": _epex_slots(entries)})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: state))
    ctrl = SimpleNamespace(
        hass=hass,
        price_sensor="sensor.epex_spot_data_total_price",
        price_integration_type=PRICE_INTEGRATION_EPEX,
        _price_data_status="not_evaluated",
        _nordpool_price_slots=[],
        _tibber_price_slots=[],
    )
    frozen = type("_Frozen", (_FixedDatetime,), {"_fixed": now})
    monkeypatch.setattr(pricing_engine, "datetime", frozen)
    return PricingManager(hass, ctrl)


def test_wide_horizon_includes_previously_missed_afternoon_slot(monkeypatch):
    """The concrete scenario from production: pressed at 21:03, tomorrow's
    cheap 14:00 slot must now be visible (it wasn't, under the old +12h cap
    which stopped at ~09:03)."""
    now = datetime(2026, 7, 31, 21, 3, 0)
    tomorrow_1400 = datetime(2026, 8, 1, 14, 0, 0)
    entries = [
        (now, now + timedelta(hours=1), 0.35),
        (tomorrow_1400, tomorrow_1400 + timedelta(minutes=15), 0.142),  # cheap
        (datetime(2026, 8, 1, 22, 0, 0), datetime(2026, 8, 1, 22, 15, 0), 0.375),
    ]
    mgr = _manager_with_epex_data(monkeypatch, entries, now=now)

    horizon = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    slots = mgr._parse_price_data(horizon_end=horizon)

    assert any(s.price == 0.142 for s in slots)


def test_wide_horizon_degrades_gracefully_when_tomorrow_only_partially_published(monkeypatch):
    """Some days EPEX only has tomorrow's data through noon at evaluation
    time. Requesting through end-of-tomorrow must not crash or fabricate
    slots — it should simply return what's actually there."""
    now = datetime(2026, 7, 31, 21, 3, 0)
    entries = [
        (now, now + timedelta(hours=1), 0.35),
        (datetime(2026, 8, 1, 11, 45, 0), datetime(2026, 8, 1, 12, 0, 0), 0.20),
        # nothing published past noon tomorrow
    ]
    mgr = _manager_with_epex_data(monkeypatch, entries, now=now)

    horizon = (now + timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)
    slots = mgr._parse_price_data(horizon_end=horizon)

    assert len(slots) == 2
    assert max(s.end for s in slots) == datetime(2026, 8, 1, 12, 0, 0)


if __name__ == "__main__":
    print("run via pytest")
