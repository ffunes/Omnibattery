"""Static checks for the panel's Forecast view against the backend contract."""
from __future__ import annotations

import re
from pathlib import Path

from custom_components import omnibattery
from custom_components.omnibattery import forecast_history_ws
from custom_components.omnibattery.tracking.forecast_history import ENERGY_METRICS

PANEL = (Path(omnibattery.__file__).parent / "frontend/marstek-panel.js").read_text(
    encoding="utf-8"
)
LANGUAGES = ("en", "es", "ca", "de", "fr", "nl")


def _language_block(lang: str) -> str:
    start = PANEL.index(f"\n  {lang}: {{", PANEL.index("const I18N = {"))
    end = PANEL.index("\n  },", start)
    return PANEL[start:end]


def test_every_forecast_string_is_translated_in_every_panel_language():
    used = set(re.findall(r'this\._t\("((?:fc|tabForecast)[A-Za-z]*)"', PANEL))
    used |= set(re.findall(r'lk: "(fc[A-Za-z]+)"', PANEL))
    used |= set(re.findall(r'\["(?:days|intervals|revisions)", "(fc[A-Za-z]+)"\]', PANEL))
    used |= set(re.findall(r'\["(?:day_ahead|start_of_day)", "(fc[A-Za-z]+)"\]', PANEL))
    assert {"tabForecast", "fcDayAhead", "fcExportDays"} <= used
    for lang in LANGUAGES:
        block = _language_block(lang)
        missing = sorted(key for key in used if f" {key}: " not in block)
        assert not missing, (lang, missing)


def test_panel_calls_the_registered_websocket_commands():
    suffixes = set(re.findall(r'_fcCall\(\{ type: "([a-z]+)"', PANEL))
    registered = {
        forecast_history_ws.WS_LIST,
        forecast_history_ws.WS_DAY,
        forecast_history_ws.WS_EXPORT,
    }
    assert suffixes == {"list", "day", "export"}
    assert {f"omnibattery/forecast_history/{s}" for s in suffixes} == registered
    for kind in forecast_history_ws.EXPORT_KINDS:
        assert f'["{kind}", "fcExport' in PANEL


def test_panel_metrics_match_the_backend_energy_metrics():
    block = PANEL[PANEL.index("const FORECAST_METRICS = ["):]
    block = block[: block.index("];")]
    assert tuple(re.findall(r'key: "([a-z_]+)"', block)) == ENERGY_METRICS


def test_forecast_tab_is_registered_and_its_timer_is_stopped():
    assert '["forecast", "mdi:chart-bell-curve-cumulative", this._t("tabForecast")]' in PANEL
    assert 'if (view !== "forecast") this._fcStopTimer();' in PANEL
    disconnected = PANEL[PANEL.index("  disconnectedCallback() {"):]
    assert "this._fcStopTimer();" in disconnected[: disconnected.index("\n  }\n")]


def _method(name: str) -> str:
    start = PANEL.index(f"\n  {name}(")
    return PANEL[start: PANEL.index("\n  }\n", start)]


def test_partial_plans_get_no_bar_and_no_zero_in_the_hover():
    accuracy = _method("_fcDrawAccuracy")
    # No ``|| 0`` fallbacks for the plans: a partial plan is null (no data).
    assert "cmpOf(d, \"day_ahead\").forecast : 0" not in accuracy
    assert "barOf(d, \"day_ahead\")" in accuracy and "barOf(d, \"start_of_day\")" in accuracy
    assert "gaps: true" in accuracy
    assert "r.accNote.hidden" in accuracy
    hover = _method("_hoverBar")
    assert "hv.gaps && g.values[li] == null" in hover


def test_the_no_plan_notice_comes_from_the_backend_flag():
    draw = _method("_fcDraw")
    assert "today_has_plan !== false" in draw
    assert "realtime" not in draw.lower()


def test_a_partial_plan_shows_no_whole_day_total():
    detail = _method("_fcDrawDetail")
    assert "running && full && dayTotal != null" in detail
    assert "fcCoverage" in detail and 'key === "day_ahead" && item.coverage' not in detail


def test_revisions_are_drawn_as_step_lines_that_reach_the_right_edge():
    drift = _method("_fcDrawDrift")
    assert drift.count("step: true") == 2
    assert "if (hi > last) times.push(hi);" in drift
    line = _method("_fcLineSVG")
    assert "s.step" in line and "s.marks" in line


def test_a_step_line_holds_its_value_up_to_the_point_where_it_ends():
    line = _method("_fcLineSVG")
    hold = 'if (open && s.step) d += "L" + X(i).toFixed(1) + " " + Y(prev).toFixed(1) + " ";'
    # Before a point without a value (the line ends there) and before a step.
    assert line.count(hold) == 2


def test_the_drift_axis_starts_at_the_first_drawn_revision():
    drift = _method("_fcDrawDrift")
    assert 'all.findIndex((p) => p.v != null)' in drift
    assert "const lo = points[0].at;" in drift


def test_a_metric_measured_nowhere_reads_as_a_plain_dash():
    text = _method("_fcActualText")
    assert 'if (value == null || measured <= 0) return "—";' in text
    assert "this._fcActualText(" in _method("_fcDrawAccuracy")
    assert "this._fcActualText(" in _method("_fcDrawDetail")
