"""Tests for the excluded-device claim on the solar forecast (#341).

An excluded device whose consumption the home sensor already sees (an EV
charger on solar surplus) eats part of the remaining forecast itself. Without a
claim the energy balance counts that sunshine as available to the battery,
reports "sufficient energy" and never schedules a cheap grid slot.

``_should_activate_grid_charging`` only touches a handful of attributes, so it
is exercised unbound on a stub controller (no Home Assistant runtime needed),
following ``test_min_soc_floor.py``.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.omnibattery import ChargeDischargeController


class _Coord:
    def __init__(self, soc, capacity_kwh, min_soc=10, max_soc=100):
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}


def _consumption(value):
    async def _f():
        return value
    return _f


class _Loads:
    """Stub for ExternalLoads exposing only the claim reader."""

    def __init__(self, claim):
        self._claim = claim

    def claimable_solar_demand_kwh(self):
        return self._claim


def _ctrl(*, claim=None, solar="20.0", consumption=8.0, soc=50.0,
          capacity=10.0, floor=0.0, safety_margin=0.0, with_loads=True):
    ctrl = SimpleNamespace(
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        coordinators=[_Coord(soc, capacity)],
        _predictive_safety_margin_kwh=safety_margin,
        _predictive_grid_charge_margin_pct=0.0,
        _predictive_min_soc_floor=floor,
        _predictive_min_soc_floor_enabled=floor > 0,
        _daily_consumption_history=[],
        solar_forecast_sensor="sensor.solar" if solar is not None else None,
        hass=SimpleNamespace(
            states=SimpleNamespace(
                get=lambda _e: None if solar is None else SimpleNamespace(state=solar)
            )
        ),
        _consumption_tracker=SimpleNamespace(
            get_dynamic_base_consumption=_consumption(consumption)
        ),
    )
    if with_loads:
        ctrl._external_loads = _Loads(claim)
    return ctrl


def _run(ctrl, **kwargs):
    return asyncio.run(
        ChargeDischargeController._should_activate_grid_charging(ctrl, **kwargs)
    )


# --- baseline: nothing configured behaves exactly as before --------------------

def test_no_claim_keeps_todays_numbers():
    baseline = _run(_ctrl(with_loads=False))
    unconfigured = _run(_ctrl(claim=None))

    assert unconfigured["excluded_demand_claim_kwh"] == 0.0
    for key, value in baseline.items():
        assert unconfigured[key] == value


def test_unavailable_sensor_claims_nothing():
    result = _run(_ctrl(claim=None))

    assert result["excluded_demand_claim_kwh"] == 0.0
    assert result["should_charge"] is False


# --- the claim itself ---------------------------------------------------------

def test_claim_reduces_total_available_and_creates_a_deficit():
    # 20 kWh solar, 8 kWh consumption, 4 kWh usable → comfortably positive.
    # An EV that will take 17 kWh leaves 3 kWh of solar: 7 available vs 8 needed.
    result = _run(_ctrl(claim=17.0))

    assert result["excluded_demand_claim_kwh"] == pytest.approx(17.0)
    assert result["solar_available_to_battery_kwh"] == pytest.approx(3.0)
    assert result["total_available_kwh"] == pytest.approx(7.0)
    assert result["energy_deficit_kwh"] == pytest.approx(1.0)
    assert result["should_charge"] is True


def test_claim_is_capped_at_the_available_solar():
    # Beyond the forecast the device draws from the grid, and that draw already
    # sits inside the consumption forecast — claiming it would count it twice.
    result = _run(_ctrl(claim=100.0))

    assert result["excluded_demand_claim_kwh"] == pytest.approx(20.0)
    assert result["solar_available_to_battery_kwh"] == pytest.approx(0.0)
    assert result["total_available_kwh"] == pytest.approx(4.0)


def test_claim_applies_after_the_safety_margin():
    result = _run(_ctrl(claim=5.0, safety_margin=2.0))

    assert result["solar_remaining_effective_kwh"] == pytest.approx(18.0)
    assert result["excluded_demand_claim_kwh"] == pytest.approx(5.0)
    assert result["solar_available_to_battery_kwh"] == pytest.approx(13.0)


def test_claim_reduces_solar_surplus():
    # Surplus is capped at the room to max (5 kWh here), so pick a claim that
    # pushes it under that cap: 20 - 10 claimed - 8 consumption = 2 kWh left.
    without = _run(_ctrl(claim=None))
    with_claim = _run(_ctrl(claim=10.0))

    assert without["solar_surplus_kwh"] == pytest.approx(5.0)
    assert with_claim["solar_surplus_kwh"] == pytest.approx(2.0)


def test_negative_claim_is_ignored():
    result = _run(_ctrl(claim=-5.0))

    assert result["excluded_demand_claim_kwh"] == 0.0
    assert result["solar_available_to_battery_kwh"] == pytest.approx(20.0)


def test_claim_never_lowers_the_floor_deficit():
    # Floor 40% on a 10 kWh battery at 20% → 2 kWh floor deficit. A claim can
    # only raise the deficit, never push it under the floor.
    floor_only = _run(_ctrl(claim=None, soc=20.0, floor=40.0))
    with_claim = _run(_ctrl(claim=3.0, soc=20.0, floor=40.0))

    assert floor_only["energy_deficit_kwh"] == pytest.approx(2.0)
    assert with_claim["energy_deficit_kwh"] >= floor_only["energy_deficit_kwh"]


def test_conservative_branch_reports_zero_claim():
    # No solar forecast at all: there is nothing to reserve.
    result = _run(_ctrl(claim=17.0, solar=None))

    assert result["solar_forecast_kwh"] is None
    assert result["excluded_demand_claim_kwh"] == 0.0
    assert result["solar_available_to_battery_kwh"] == 0.0


def test_claim_applies_to_the_remaining_horizon_overrides():
    # The remaining-horizon evaluation passes its own consumption and solar
    # figures; the claim must be measured against those, not the daily ones.
    result = _run(
        _ctrl(claim=4.0),
        consumption_override_kwh=5.0,
        solar_forecast_override_kwh=6.0,
    )

    assert result["excluded_demand_claim_kwh"] == pytest.approx(4.0)
    assert result["solar_available_to_battery_kwh"] == pytest.approx(2.0)
    assert result["total_available_kwh"] == pytest.approx(6.0)
    assert result["should_charge"] is False
