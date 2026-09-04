from pathlib import Path


PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")


def test_dashboard_sliders_track_touch_without_relying_on_focus():
    panel = PANEL.read_text(encoding="utf-8")

    # Both slider builders (battery controls and system controls) use the same
    # explicit touch/pointer guard.
    assert panel.count("this._wireRangeInteraction(range);") == 2
    assert 'range.addEventListener("pointerdown", begin);' in panel
    assert 'range.addEventListener("touchstart", begin, { passive: true });' in panel
    assert panel.count("if (!this._rangePatchLocked(w.el, state") == 2


def test_dashboard_sliders_hold_the_submitted_value_until_ha_confirms_it():
    panel = PANEL.read_text(encoding="utf-8")

    assert panel.count("this._markRangePending(range, value);") == 2
    assert "range.__pendingUntil = Date.now() + 4000;" in panel
    assert "Math.abs(actual - pending) <= tolerance" in panel
