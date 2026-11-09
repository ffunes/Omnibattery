"""Charging-window form: a cleared optional row stays cleared (#547)."""
from __future__ import annotations

import voluptuous as vol

from custom_components.omnibattery.config_flow import (
    _charging_window_schema_fields,
    _parse_charging_windows,
)


def test_cleared_second_window_is_dropped():
    existing = [
        {"start_time": "02:00:00", "end_time": "05:00:00", "days": ["mon"]},
        {"start_time": "12:00:00", "end_time": "14:00:00", "days": ["tue"]},
    ]
    schema = vol.Schema(_charging_window_schema_fields(existing))
    # HA omits emptied fields from the submission.
    submitted = schema({"start_time": "02:00:00", "end_time": "05:00:00"})
    windows, errors = _parse_charging_windows(submitted)
    assert errors == {}
    assert [w["start_time"] for w in windows] == ["02:00:00"]
