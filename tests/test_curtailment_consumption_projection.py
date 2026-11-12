"""Smart Pre-Discharge sizes the house load from the shared energy projection
(the learned profile), not from a flat spread (S6)."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_energy_projection import _build
from tests.test_hold_consumption_projection import DAY, _profile_kwh, _scenario


def test_curtailment_pre_discharge_sizes_house_load_from_the_profile(monkeypatch):
    """00:05, 0.3 kW night: before the risk window the house drains ~1.89 kWh by
    the profile (the flat spread said 4.02); the plan discharges what the house
    really absorbs, not 4.02."""
    now = DAY + timedelta(minutes=5)
    scenario = _scenario(now, 0.30, soc=90, remaining=24.9999, negative_hours=range(12, 16))
    ctrl, mgr = _build(scenario, monkeypatch)
    ctrl.smart_predischarge_enabled = True
    ctrl.predischarge_reserve_soc = 0.0
    ctrl.negative_injection_threshold = 0.0
    ctrl._is_backup_function_active = lambda *_a, **_k: False
    ctrl.backup_function_enabled = False
    ctrl._non_responsive = SimpleNamespace(is_excluded=lambda _c: False)
    ctrl._is_manual_slot_owned = lambda _c: False
    ctrl.get_discharge_blockers = lambda _c=None: {}
    ctrl._effective_discharge_min_soc = lambda c: (c.min_soc, None)
    ctrl.solar_forecast_source = "remaining"

    plan = mgr._build_curtailment_plan(list(scenario.slots[:24]), now=now)

    daily = [s for s in scenario.slots[:24] if s.end > now]
    for slot in daily:
        assert plan.consumption_forecast_by_slot[slot] == pytest.approx(
            _profile_kwh(scenario, max(slot.start, now), slot.end), abs=0.01
        ), slot.start
    assert plan.consumption_forecast_by_slot[daily[0]] == pytest.approx(0.3 * 55 / 60, abs=0.01)
    risk_start = DAY + timedelta(hours=12)
    net_load = sum(
        max(0.0, plan.consumption_forecast_by_slot[s] - plan.solar_forecast_by_slot[s])
        for s in daily if s.end <= risk_start
    )
    # The house draws 1.89 kWh net of sun before the risk window (the flat
    # spread said 4.02); the plan discharges that and no more.
    assert net_load == pytest.approx(1.89, abs=0.05)
    assert plan.planned_discharge_kwh == pytest.approx(net_load, abs=0.01)
