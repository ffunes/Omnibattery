"""Static contracts for Daily Operation rendering decisions."""

from pathlib import Path


PANEL = Path("custom_components/omnibattery/frontend/marstek-panel.js")


def test_delayed_solar_opportunity_keeps_yellow_window_and_clock_context():
    panel = PANEL.read_text(encoding="utf-8")

    assert "const solarOpportunity = !snapshot.isSkipped[index]" in panel
    assert "const solarWindow = solarOpportunity;" in panel
    assert "cell.classList.toggle(\"daily-op-delay\", item.delay);" in panel
    assert "delayMark.hidden = !item.delay;" in panel


def test_daily_operation_action_colours_follow_the_visual_contract():
    panel = PANEL.read_text(encoding="utf-8")

    assert 'return bit === 1 ? "solar" : bit === 2 ? "grid" : "discharge";' in panel
    assert 'solar: "var(--daily-op-solar-charge)"' in panel
    assert 'grid: "var(--daily-op-grid)"' in panel
    assert 'discharge: "var(--daily-op-discharge)"' in panel
    assert 'item.decision === "not_needed" ? "not-needed" : "neutral"' in panel
    assert "const baseAction = item.solarWindow" in panel


def test_disabled_charge_delay_suppresses_stale_clock_markers():
    panel = PANEL.read_text(encoding="utf-8")

    assert "const delayEnabled = !snapshot.delayInfo" in panel
    assert "const delay = delayEnabled && !weeklyDelayBypassed" in panel


def test_disabled_hourly_balance_suppresses_feature_legend_and_markers():
    panel = PANEL.read_text(encoding="utf-8")

    assert "daily-op-legend-hourly-balance" in panel
    assert 'const hourlyBalanceState = this._stateFor(this._index().byKey, "hourly_balance");' in panel
    assert "hourlyBalanceEnabled: Boolean(hourlyBalanceState" in panel
    assert "ref.hourlyBalanceLegend.hidden = !snapshot.hourlyBalanceEnabled;" in panel
    assert "hourlyBalance: snapshot.hourlyBalanceEnabled" in panel


def test_open_cell_is_completed_from_the_previous_quarter_never_dropped():
    """No hole and no spike at the now marker: the open cell is always plotted.

    Dropping it below a minute of coverage left a 30-minute gap straddling the
    marker, and scaling it by 900 / seconds collapsed the point on a young
    quarter, so the forecast hand-off looked like a spike.
    """
    panel = PANEL.read_text(encoding="utf-8")

    assert (
        "if (seconds != null && seconds < 900 && (value != null || previous != null)) {"
        in panel
    )
    assert (
        "plotted[index] = (value ?? 0) + (previous ?? 0) * (900 - seconds) / 900;"
        in panel
    )
    assert "const previous = this._dailyOperationValueAt(plotted, index - 1);" in panel
    # The two shapes that produced the artefacts must not come back.
    assert "plotted[index] = seconds >= 60" not in panel
    assert "* 900 / seconds;" not in panel


def test_forecast_handoff_drops_the_open_cell_when_there_is_no_observed_point():
    panel = PANEL.read_text(encoding="utf-8")

    assert "if (!snapshot.isSkipped[index]) plotted[index] = observed;" in panel
    assert "if (observed != null && !snapshot.isSkipped[index]) plotted[index] = observed;" not in panel


def test_solar_forecast_needs_a_sustained_zero_run_before_erasing_the_day():
    panel = PANEL.read_text(encoding="utf-8")

    assert "const DAILY_OPERATION_SUNSET_ZERO_INTERVALS = 4;" in panel
    assert "// ponytail:" in panel
    assert (
        'return zeroIntervals >= DAILY_OPERATION_SUNSET_ZERO_INTERVALS ? "after" : "during";'
        in panel
    )
    assert 'if (this._dailyOperationSolarPhase(snapshot) !== "after") return plotted;' in panel
    assert "if (zeroIntervals < 1 || !priorProduction)" not in panel


def test_solar_phase_skips_an_open_cell_without_a_minute_of_coverage():
    panel = PANEL.read_text(encoding="utf-8")

    # Otherwise the phase would flip back to "during" for the first minute of
    # every quarter, making the post-sunset suppression flicker.
    assert (
        "const last = openCoverage != null && openCoverage >= 60 ? index : index - 1;"
        in panel
    )


def test_no_sun_left_reasons_are_silenced_outside_the_producing_window():
    panel = PANEL.read_text(encoding="utf-8")

    assert 'if (reason === "zero_budget") return phase === "during";' in panel
    assert (
        'if (reason === "learned_shape_no_future_energy") return phase !== "after";'
        in panel
    )
    assert 'const solarPhase = this._dailyOperationSolarPhase(snapshot);' in panel
    assert "this._dailyOperationFallbackReasonLabel(reason, solarPhase)" in panel
    # The old unconditional filter hid a zero budget even at midday, which is
    # exactly when it is worth showing.
    assert 'part.toLowerCase() !== "zero_budget"' not in panel
