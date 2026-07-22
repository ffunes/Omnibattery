"""Characterization tests for the pure pricing helpers (module-8 PR1).

These pin the *current* behavior of the price parsers and cheap-slot selection
math extracted verbatim from ``ChargeDischargeController`` into
``pricing.calculations`` so the move is proven cero-cambio-funcional.

No hardware, no running Home Assistant. The selection/hours math is pure Python;
the parsers only touch ``dt_util.as_local`` for timezone-aware inputs, so all
fixtures use naive datetimes / offset-less ISO strings to stay HA-free.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.omnibattery.const import CHARGE_EFFICIENCY
from custom_components.omnibattery.pricing import (
    PriceSlot,
    calculations,
)

# Far-future anchor so every built slot is always "future" relative to now()
# (select_cheapest_hours filters out slots whose end <= now).
_DAY = datetime(2999, 1, 1, 0, 0)


def _hourly(prices):
    """Build consecutive 1-hour PriceSlots starting at the anchor day."""
    return [
        PriceSlot(start=_DAY + timedelta(hours=i), end=_DAY + timedelta(hours=i + 1), price=p)
        for i, p in enumerate(prices)
    ]


def _quarter(prices):
    """Build consecutive 15-minute PriceSlots starting at the anchor day."""
    return [
        PriceSlot(
            start=_DAY + timedelta(minutes=15 * i),
            end=_DAY + timedelta(minutes=15 * (i + 1)),
            price=p,
        )
        for i, p in enumerate(prices)
    ]


# ----------------------------------------------------------------------
# calculate_charging_hours_needed
# ----------------------------------------------------------------------

def test_hours_needed_battery_is_bottleneck():
    # min(ICP 5000, cap 3000) = 3 kW; 5 / (3 * 0.85) = 1.96h -> round up to 2.0
    assert calculations.calculate_charging_hours_needed(5.0, 5000, 3000) == 2.0


def test_hours_needed_icp_is_bottleneck():
    # min(ICP 2000, cap 8000) = 2 kW; 5 / (2 * 0.85) = 2.94h -> round up to 3.0
    assert calculations.calculate_charging_hours_needed(5.0, 2000, 8000) == 3.0


def test_hours_needed_zero_power_fallback():
    assert calculations.calculate_charging_hours_needed(5.0, 0, 0) == 1.0


def test_hours_needed_always_half_hour_multiple():
    for deficit in (0.4, 1.3, 2.7, 9.1):
        result = calculations.calculate_charging_hours_needed(deficit, 4000, 4000)
        assert (result * 2) % 1 == 0  # exact multiple of 0.5
    # spot check the rounding direction
    assert CHARGE_EFFICIENCY == 0.85


# ----------------------------------------------------------------------
# calculate_planned_grid_charge_kwh
# ----------------------------------------------------------------------

def test_planned_grid_charge_is_capped_by_small_battery_headroom():
    planned = calculations.calculate_planned_grid_charge_kwh(4.552857, 1.5808)
    assert planned == 1.5808
    assert calculations.calculate_charging_hours_needed(planned, 7000, 1200) == 2.0


def test_planned_grid_charge_margin_is_applied_before_headroom_cap():
    assert calculations.calculate_planned_grid_charge_kwh(2.0, 5.0, 50.0) == 3.0
    assert calculations.calculate_planned_grid_charge_kwh(2.0, 2.5, 50.0) == 2.5


# ----------------------------------------------------------------------
# select_cheapest_hours (hourly path)
# ----------------------------------------------------------------------

def test_select_hours_picks_cheapest_n_sorted_by_start():
    slots = _hourly([0.30, 0.10, 0.20, 0.40])
    selected = calculations.select_cheapest_hours(slots, hours_needed=2, max_price_threshold=None)
    assert len(selected) == 2
    assert {round(s.price, 2) for s in selected} == {0.10, 0.20}
    assert [s.start for s in selected] == sorted(s.start for s in selected)


def test_select_hours_threshold_filters_then_selects():
    slots = _hourly([0.30, 0.10, 0.20, 0.40])
    selected = calculations.select_cheapest_hours(slots, hours_needed=2, max_price_threshold=0.25)
    assert {round(s.price, 2) for s in selected} == {0.10, 0.20}


def test_select_hours_threshold_too_low_returns_partial():
    slots = _hourly([0.30, 0.10, 0.20, 0.40])
    # only the 0.10 slot passes; cannot meet 2h -> returns what's available
    selected = calculations.select_cheapest_hours(slots, hours_needed=2, max_price_threshold=0.15)
    assert len(selected) == 1
    assert round(selected[0].price, 2) == 0.10


def test_select_hours_empty_after_filter():
    slots = _hourly([0.30, 0.10, 0.20, 0.40])
    assert calculations.select_cheapest_hours(slots, hours_needed=2, max_price_threshold=0.0) == []


def test_select_hours_dispatches_to_blocks_for_subhourly():
    # 8 consecutive 15-min slots: first 4 cheap, last 4 expensive.
    slots = _quarter([0.10, 0.10, 0.10, 0.10, 0.90, 0.90, 0.90, 0.90])
    selected = calculations.select_cheapest_hours(slots, hours_needed=1, max_price_threshold=None)
    # 1 hour == one consecutive block of 4 cheapest slots
    assert len(selected) == 4
    assert all(s.price == 0.10 for s in selected)
    assert [s.start for s in selected] == sorted(s.start for s in selected)


# ----------------------------------------------------------------------
# select_cheapest_blocks (sub-hourly path)
# ----------------------------------------------------------------------

def test_select_blocks_full_block_plus_remainder():
    slots = _quarter([0.10, 0.10, 0.10, 0.10, 0.20, 0.20, 0.20, 0.20])
    # 1.5h with 0.25h slots = 1 full block (4 slots) + 2 remainder slots = 6
    selected = calculations.select_cheapest_blocks(slots, hours_needed=1.5, slot_duration_h=0.25)
    assert len(selected) == 6
    assert [s.start for s in selected] == sorted(s.start for s in selected)


def test_select_blocks_prefers_cheapest_consecutive_window():
    slots = _quarter([0.90, 0.90, 0.90, 0.90, 0.10, 0.10, 0.10, 0.10])
    selected = calculations.select_cheapest_blocks(slots, hours_needed=1, slot_duration_h=0.25)
    assert len(selected) == 4
    assert all(s.price == 0.10 for s in selected)


# ----------------------------------------------------------------------
# Parsers (naive datetimes / offset-less ISO -> no dt_util.as_local needed)
# ----------------------------------------------------------------------

def test_parse_nordpool():
    attrs = {
        "raw_today": [
            {"start": _DAY, "end": _DAY + timedelta(hours=1), "value": 0.25},
            {"start": _DAY + timedelta(hours=1), "end": _DAY + timedelta(hours=2), "value": 0.30},
        ],
        "raw_tomorrow": [],
    }
    slots = calculations.parse_nordpool_prices(attrs)
    assert [s.price for s in slots] == [0.25, 0.30]
    assert all(isinstance(s, PriceSlot) for s in slots)


def test_parse_hacs_nordpool_normalizes_cents_to_major_currency_per_kwh():
    attrs = {
        "price_in_cents": True,
        "unit": "kWh",
        "currency": "EUR",
        "raw_today": [
            {"start": _DAY, "end": _DAY + timedelta(hours=1), "value": 12.5},
        ],
    }

    slots = calculations.parse_nordpool_prices(attrs)

    assert [s.price for s in slots] == [0.125]


def test_hacs_nordpool_normalizes_energy_scale_to_per_kwh():
    assert calculations.normalize_nordpool_hacs_price(125.0, {"unit": "MWh"}) == 0.125
    assert calculations.normalize_nordpool_hacs_price(0.000125, {"unit": "Wh"}) == 0.125


def test_parse_official_nordpool_service_prices_converts_mwh_to_kwh():
    response = {
        "ES": [
            {
                "start": "2999-01-01T00:00:00",
                "end": "2999-01-01T00:15:00",
                "price": 125.5,
            }
        ],
        "FR": [
            {
                "start": "2999-01-01T00:00:00",
                "end": "2999-01-01T00:15:00",
                "price": 999.0,
            }
        ],
    }

    slots = calculations.parse_nordpool_service_prices(response, "ES")

    assert len(slots) == 1
    assert slots[0].price == 0.1255
    assert slots[0].end - slots[0].start == timedelta(minutes=15)


def test_parse_pvpc():
    attrs = {"price_00h": 0.11, "price_01h": 0.12, "price_02h": "0.13"}
    slots = calculations.parse_pvpc_prices(attrs)
    assert [s.price for s in slots] == [0.11, 0.12, 0.13]
    # hourly, today's date, chronological
    assert all((s.end - s.start) == timedelta(hours=1) for s in slots)


def test_parse_ckw():
    attrs = {
        "prices": [
            {"start": "2999-01-01T00:00:00", "end": "2999-01-01T00:15:00", "price": 0.24},
            {"start": "2999-01-01T00:15:00", "end": "2999-01-01T00:30:00", "value": 0.26},
        ]
    }
    slots = calculations.parse_ckw_prices(attrs)
    assert [s.price for s in slots] == [0.24, 0.26]  # 2nd falls back to 'value'


def test_parse_epex():
    attrs = {
        "data": [
            {"start_time": "2999-01-01T00:00:00", "end_time": "2999-01-01T01:00:00", "price_per_kwh": 0.14},
        ]
    }
    slots = calculations.parse_epex_prices(attrs)
    assert len(slots) == 1 and slots[0].price == 0.14


def test_parse_tibber_infers_15min_end():
    prices_by_home = {
        "myHome": [
            {"start_time": "2999-01-01T00:00:00", "price": 0.3561},
            {"start_time": "2999-01-01T00:15:00", "price": 0.3486},
        ]
    }
    slots = calculations.parse_tibber_prices(prices_by_home)
    assert [s.price for s in slots] == [0.3561, 0.3486]
    # first slot's end inferred from the second slot's start
    assert slots[0].end == slots[1].start
    # last slot inherits the previous 15-min delta
    assert (slots[1].end - slots[1].start) == timedelta(minutes=15)


def test_parse_tibber_empty():
    assert calculations.parse_tibber_prices({}) == []


def test_parse_tibber_multi_home_uses_first():
    prices_by_home = {
        "home_a": [{"start_time": "2999-01-01T00:00:00", "price": 0.10}],
        "home_b": [{"start_time": "2999-01-01T00:00:00", "price": 0.99}],
    }
    slots = calculations.parse_tibber_prices(prices_by_home)
    assert [s.price for s in slots] == [0.10]


def test_parse_entsoe_infers_end_from_next_start():
    attrs = {
        "prices_today": [
            {"time": "2999-01-01T00:00:00", "price": 0.26},
            {"time": "2999-01-01T01:00:00", "price": 0.28},
        ],
        "prices_tomorrow": [],
    }
    slots = calculations.parse_entsoe_prices(attrs)
    assert [s.price for s in slots] == [0.26, 0.28]
    # first slot's end inferred from second slot's start
    assert slots[0].end == slots[1].start
    # last slot inherits the previous 1h delta
    assert (slots[1].end - slots[1].start) == timedelta(hours=1)
