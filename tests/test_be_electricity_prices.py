"""BE Electricity Prices (renaudallard/homeassistant_be_electricity_prices) as a price source.

Its ``current_price`` sensor exposes every slot of today and tomorrow as
``{start, energy, network, taxes, all_in}`` rows, for every contract kind,
time-of-use ones such as ENGIE Empower Flextime included.
"""
from __future__ import annotations

from datetime import timedelta

from custom_components.omnibattery.const import PRICE_INTEGRATION_BE_ELECTRICITY
from custom_components.omnibattery.pricing import calculations


def _row(start, all_in):
    return {"start": start, "energy": 0.1, "network": 0.1, "taxes": 0.05, "all_in": all_in}


# Flextime weekday shape: off-peak until 01:00, super off-peak 01–07, peak from 07.
FLEXTIME = {
    "today": [
        _row("2999-01-01T00:00:00", 0.34),
        _row("2999-01-01T01:00:00", 0.29),
        _row("2999-01-01T07:00:00", 0.39),
    ],
    "tomorrow": [_row("2999-01-02T00:00:00", 0.34)],
}


def test_uses_the_all_in_price_and_infers_slot_ends():
    slots = calculations.parse_be_electricity_prices(FLEXTIME)
    assert [s.price for s in slots] == [0.34, 0.29, 0.39, 0.34]
    assert slots[0].end == slots[1].start
    assert slots[1].end == slots[2].start
    # The last slot inherits the length of the one before it.
    assert slots[-1].end - slots[-1].start == slots[-1].start - slots[-2].start


def test_quarter_hourly_contracts_keep_their_grid():
    attrs = {
        "today": [_row(f"2999-01-01T00:{m:02d}:00", 0.2 + m / 1000) for m in (0, 15, 30, 45)],
        "tomorrow": [],
    }
    slots = calculations.parse_be_electricity_prices(attrs)
    assert len(slots) == 4
    assert all(s.end - s.start == timedelta(minutes=15) for s in slots)


def test_tomorrow_may_be_empty_and_injection_rows_parse():
    attrs = {"today": [{"start": "2999-01-01T00:00:00", "injection": 0.03}], "tomorrow": []}
    slots = calculations.parse_be_electricity_prices(attrs)
    assert [s.price for s in slots] == [0.03]
    assert slots[0].end - slots[0].start == timedelta(hours=1)


def test_bad_rows_are_skipped_and_nothing_parses_to_nothing():
    attrs = {"today": [{"start": "nonsense", "all_in": 0.3}, {"all_in": 0.3}, "x"], "tomorrow": "[]"}
    assert calculations.parse_be_electricity_prices(attrs) == []
    assert calculations.parse_be_electricity_prices({}) == []


def test_dispatch_and_string_guard():
    assert len(calculations.parse_prices_for_integration(PRICE_INTEGRATION_BE_ELECTRICITY, FLEXTIME)) == 4
    assert calculations.stringified_price_attrs(
        PRICE_INTEGRATION_BE_ELECTRICITY, {"today": "[]", "tomorrow": []}
    ) == ["today"]
