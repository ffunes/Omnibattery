"""Tests for the arbitrage-margin charge gate.

The gate makes the grid-charge ceiling follow the day's spread instead of
sitting at a fixed price: a slot only qualifies when the expensive hours are far
enough above it to repay round-trip losses and still clear a required margin.

``effective_charge_ceiling`` does the combining and is the single place the
ceiling is computed; ``select_cheapest_hours`` just receives the result. These
tests exercise that pairing the same way the engine does.

Same conventions as ``test_pricing_calculations.py``: no hardware, no running
Home Assistant, far-future anchor so every slot counts as future.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.omnibattery.const import DEFAULT_ROUND_TRIP_EFFICIENCY
from custom_components.omnibattery.pricing import PriceSlot, calculations

_DAY = datetime(2999, 1, 1, 0, 0)
_EFF = DEFAULT_ROUND_TRIP_EFFICIENCY


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


def _gated(slots, hours, static=None, margin=None, eff=_EFF, now=None):
    """Run the engine's two-step: compute the ceiling, then select against it."""
    ceiling, _ = calculations.effective_charge_ceiling(
        slots, hours, static, margin, eff, now=now
    )
    return calculations.select_cheapest_hours(slots, hours, ceiling, now=now)


# ---------------------------------------------------------------------------
# expected_discharge_price
# ---------------------------------------------------------------------------

def test_expected_discharge_price_averages_priciest_hours():
    slots = _hourly([0.10, 0.20, 0.30, 0.40])
    assert calculations.expected_discharge_price(slots, hours_needed=2) == 0.35


def test_expected_discharge_price_single_hour():
    slots = _hourly([0.10, 0.20, 0.30, 0.40])
    assert calculations.expected_discharge_price(slots, hours_needed=1) == 0.40


def test_expected_discharge_price_caps_at_available_hours():
    """Asking for more hours than exist averages everything, without error."""
    slots = _hourly([0.10, 0.30])
    assert calculations.expected_discharge_price(slots, hours_needed=10) == 0.20


def test_expected_discharge_price_returns_none_without_data():
    assert calculations.expected_discharge_price([], hours_needed=2) is None
    assert calculations.expected_discharge_price(_hourly([0.1]), hours_needed=0) is None


# ---------------------------------------------------------------------------
# arbitrage_ceiling: enable/disable semantics
# ---------------------------------------------------------------------------

def test_margin_none_disables_the_gate():
    slots = _hourly([0.10, 0.20, 0.30, 0.40])
    assert calculations.arbitrage_ceiling(slots, 2, None, _EFF) is None


def test_margin_zero_disables_the_gate():
    """A NumberEntity cannot be cleared, so 0 has to mean off, not zero-margin."""
    slots = _hourly([0.10, 0.20, 0.30, 0.40])
    assert calculations.arbitrage_ceiling(slots, 2, 0, _EFF) is None
    assert calculations.arbitrage_ceiling(slots, 2, 0.0, _EFF) is None


def test_margin_zero_leaves_selection_untouched():
    slots = _hourly([0.10, 0.20, 0.30, 0.40])
    assert [s.price for s in _gated(slots, 2, margin=0.0)] == [
        s.price for s in calculations.select_cheapest_hours(slots, 2, None)
    ]


def test_arbitrage_ceiling_none_without_price_data():
    assert calculations.arbitrage_ceiling([], 2, 0.02, _EFF) is None


# ---------------------------------------------------------------------------
# effective_charge_ceiling: reported vs applied
# ---------------------------------------------------------------------------

def test_effective_ceiling_reports_the_value_it_applies():
    slots = _hourly([0.10, 0.12, 0.38, 0.42])
    effective, reported = calculations.effective_charge_ceiling(slots, 2, None, 0.02, _EFF)
    assert effective == reported
    assert effective == 0.40 * _EFF - 0.02


def test_effective_ceiling_static_wins_when_stricter():
    """The arbitrage value is still reported, but the static ceiling is applied."""
    slots = _hourly([0.10, 0.15, 0.40, 0.45])
    effective, reported = calculations.effective_charge_ceiling(slots, 2, 0.12, 0.02, _EFF)
    assert effective == 0.12
    assert reported > effective


def test_effective_ceiling_passes_static_through_when_gate_off():
    slots = _hourly([0.10, 0.20, 0.30, 0.40])
    assert calculations.effective_charge_ceiling(slots, 2, 0.25, None, _EFF) == (0.25, None)
    assert calculations.effective_charge_ceiling(slots, 2, None, None, _EFF) == (None, None)


# ---------------------------------------------------------------------------
# select_cheapest_hours: gate disabled (regression guard)
# ---------------------------------------------------------------------------

def test_disabled_gate_leaves_selection_untouched():
    """The pre-feature 3-arg call must still behave identically."""
    slots = _hourly([0.30, 0.10, 0.20, 0.40])
    baseline = calculations.select_cheapest_hours(slots, 2, None)
    assert sorted(s.price for s in baseline) == [0.10, 0.20]
    assert [s.price for s in baseline] == [s.price for s in _gated(slots, 2)]


def test_disabled_gate_leaves_sub_hourly_selection_untouched():
    slots = _quarter([0.30, 0.10, 0.20, 0.40, 0.15, 0.35, 0.25, 0.05])
    baseline = calculations.select_cheapest_hours(slots, 1, None)
    assert [s.start for s in baseline] == [s.start for s in _gated(slots, 1)]


# ---------------------------------------------------------------------------
# select_cheapest_hours: gate active
# ---------------------------------------------------------------------------

def test_wide_spread_still_charges():
    """Peak 0.40 x 0.85 = 0.34; cheap hours at 0.10/0.12 clear a 0.02 margin."""
    slots = _hourly([0.10, 0.12, 0.38, 0.42])
    assert sorted(s.price for s in _gated(slots, 2, margin=0.02)) == [0.10, 0.12]


def test_flat_day_is_skipped_entirely():
    """A winter-flat curve cannot repay conversion losses, so nothing is booked."""
    slots = _hourly([0.24, 0.245, 0.25, 0.26])
    assert _gated(slots, 2, margin=0.02) == []


def test_margin_size_controls_strictness():
    slots = _hourly([0.10, 0.20, 0.30, 0.40])
    loose = _gated(slots, 2, margin=0.01, eff=0.90)
    strict = _gated(slots, 2, margin=0.20, eff=0.90)
    assert len(loose) > len(strict)


def test_lower_efficiency_tightens_the_gate():
    slots = _hourly([0.20, 0.22, 0.30, 0.32])
    assert len(_gated(slots, 2, margin=0.02, eff=0.95)) > len(
        _gated(slots, 2, margin=0.02, eff=0.60)
    )


def test_gate_beats_a_static_ceiling_on_a_flat_day():
    slots = _hourly([0.24, 0.245, 0.25, 0.26])
    # A 0.25 ceiling alone happily books the two cheapest slots.
    assert len(calculations.select_cheapest_hours(slots, 2, 0.25)) == 2
    # The gate sees there is no spread behind them.
    assert _gated(slots, 2, static=0.25, margin=0.02) == []


def test_gate_applies_on_the_sub_hourly_block_path():
    """15-min slots dispatch to select_cheapest_blocks; the ceiling runs first."""
    wide = _quarter([0.10, 0.10, 0.10, 0.10, 0.40, 0.40, 0.40, 0.40])
    assert _gated(wide, 1, margin=0.02) != []

    flat = _quarter([0.24, 0.24, 0.24, 0.24, 0.26, 0.26, 0.26, 0.26])
    assert _gated(flat, 1, margin=0.02) == []


def test_negative_prices_are_valued_correctly():
    """Avoided import at a negative price is a loss, so the gate must tighten.

    Discharging into a -0.05 hour is worth 0.85 x -0.05 = -0.0425 per kWh, so
    charging only pays when the charge price is below -0.0425 - margin. The
    -0.10 slot clears that; -0.05 does not.
    """
    slots = _hourly([-0.10, -0.05, -0.05, -0.05])
    assert [s.price for s in _gated(slots, 1, margin=0.02, eff=0.85)] == [-0.10]


def test_expired_slots_drop_out_of_both_steps():
    """The engine passes one `now` to both calls so they see the same horizon."""
    slots = _hourly([0.10, 0.12, 0.38, 0.42])
    cutoff = _DAY + timedelta(hours=2)  # first two slots already expired
    assert _gated(slots, 1, margin=0.02, now=cutoff) == []


def test_peak_hours_survive_the_static_ceiling_for_the_estimate():
    """Regression: the discharge estimate must see slots the ceiling removes.

    If the expected-discharge average were computed after the static filter, the
    expensive hours would be gone and the gate would always refuse to charge.
    """
    slots = _hourly([0.10, 0.12, 0.50, 0.55])
    assert sorted(s.price for s in _gated(slots, 2, static=0.20, margin=0.02)) == [0.10, 0.12]
