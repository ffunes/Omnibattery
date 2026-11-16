"""Peak Shaving Limit Sync blueprint v2 (#506): it writes into Maximum grid import.

It writes max(minimum peak, monthly peak) - margin, kept inside the number
entity's range, and nothing while the peak sensor has no usable value.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.util import yaml as yaml_util
from jinja2 import Environment

PATH = Path("blueprints/peak_shaving_limit_sync_blueprint.yaml")


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!input", lambda loader, node: loader.construct_scalar(node))


def _blueprint() -> dict:
    return yaml.load(PATH.read_text(encoding="utf-8"), Loader=_Loader)


def test_the_blueprint_passes_home_assistant_schema():
    BLUEPRINT_SCHEMA(yaml_util.load_yaml(PATH))


def test_inputs_and_defaults():
    inputs = _blueprint()["blueprint"]["input"]
    assert "capacity_protection_limit" not in inputs
    assert "default" not in inputs["max_grid_import"]
    assert inputs["max_grid_import"]["selector"]["entity"]["filter"]["domain"] == "number"
    assert inputs["minimum_peak"]["default"] == 2500
    assert inputs["margin"]["default"] == 0


def _render(peak, unit, *, minimum=2500, margin=0, low=500, high=20000):
    def flt(value, default=None):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def to_int(value, default=0):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    env = Environment()
    env.filters["float"] = flt
    env.filters["int"] = to_int
    states = {"sensor.peak": peak}
    attrs = {
        ("sensor.peak", "unit_of_measurement"): unit,
        ("number.cap", "min"): low,
        ("number.cap", "max"): high,
    }
    out = env.from_string(_blueprint()["variables"]["target_w"]).render(
        states=lambda e: states.get(e, "unknown"),
        state_attr=lambda e, a: attrs.get((e, a)),
        monthly_peak_sensor="sensor.peak",
        max_grid_import="number.cap",
        minimum_peak=minimum,
        margin=margin,
    ).strip()
    return None if out == "none" else int(out)


@pytest.mark.parametrize(
    ("peak", "unit", "kwargs", "expected"),
    [
        ("4.2", "kW", {}, 4200),
        ("3100", "W", {}, 3100),
        ("0", "kW", {}, 2500),               # the 1st of the month: the minimum holds
        ("4.2", "kW", {"margin": 175}, 4025),
        ("30", "kW", {}, 20000),             # kept inside the entity range
        ("0", "kW", {"minimum": 0}, 500),
        ("unavailable", "kW", {}, None),     # nothing written
        ("3.1", "kWh", {}, None),            # not a power unit
    ],
)
def test_the_written_value(peak, unit, kwargs, expected):
    assert _render(peak, unit, **kwargs) == expected
