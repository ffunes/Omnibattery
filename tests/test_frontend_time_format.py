from pathlib import Path


PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")


def test_dashboard_clock_honors_home_assistant_time_format_preference():
    panel = PANEL.read_text(encoding="utf-8")

    assert 'if (preference === "12") return true;' in panel
    assert 'if (preference === "24") return false;' in panel
    assert 'preference === "system" ? undefined : this._lang()' in panel
    assert 'hourCycle: this._useAmPm() ? "h12" : "h23"' in panel


def test_internal_calendar_parts_remain_twenty_four_hour_numeric_fields():
    panel = PANEL.read_text(encoding="utf-8")

    date_parts = panel[panel.index("  _dateParts("):panel.index("  _zonedMidnight(")]
    assert 'hourCycle: "h23"' in date_parts
