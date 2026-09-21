"""Regression: ``_should_activate_grid_charging`` must not crash when no solar
forecast sensor is configured (no-solar installs leave it unset, per the docs).

Before the fix, ``self.hass.states.get(self.solar_forecast_sensor)`` was called
unconditionally, so an unset (``None``) sensor raised
``AttributeError: 'NoneType' object has no attribute 'lower'`` deep inside
``EntityRegistry.get`` — surfaced to the user as a 500 on the
"Re-evaluate Dynamic Pricing" button. The method already handles a *dead*
sensor (``forecast_state is None``) via its conservative-mode branch; the bug
was reaching ``states.get`` at all with no sensor configured.

Exercised unbound on a stub controller, same style as ``test_min_soc_floor.py``.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.omnibattery import ChargeDischargeController


class _Coord:
    def __init__(self, soc, capacity_kwh, min_soc=12, max_soc=95):
        self.min_soc = min_soc
        self.max_soc = max_soc
        self.data = {"battery_soc": soc, "battery_total_energy": capacity_kwh}


def _consumption(value):
    async def _f():
        return value
    return _f


def _states_get_that_blows_up_on_none(entity_id):
    # Mirrors the real crash site: HA's states.get() calls entity_id.lower()
    # unconditionally, so passing None reproduces the original AttributeError.
    if entity_id is None:
        raise AttributeError("'NoneType' object has no attribute 'lower'")
    return SimpleNamespace(state="unavailable")


def _ctrl_no_solar(*, consumption=2.0):
    return SimpleNamespace(
        predictive_charging_enabled=True,
        predictive_charging_overridden=False,
        coordinators=[_Coord(soc=50.0, capacity_kwh=10.0)],
        _predictive_safety_margin_kwh=0.0,
        _predictive_min_soc_floor=0.0,
        _predictive_min_soc_floor_enabled=False,
        _daily_consumption_history=[],
        solar_forecast_sensor=None,  # no solar panels: left unset, as the docs instruct
        hass=SimpleNamespace(states=SimpleNamespace(get=_states_get_that_blows_up_on_none)),
        _consumption_tracker=SimpleNamespace(get_dynamic_base_consumption=_consumption(consumption)),
    )


def _run(ctrl):
    return asyncio.run(ChargeDischargeController._should_activate_grid_charging(ctrl))


def test_unset_solar_sensor_does_not_crash():
    result = _run(_ctrl_no_solar())
    assert "Solar unavailable - conservative mode" in result["reason"]


def test_unset_solar_sensor_falls_back_to_conservative_mode():
    # 5 kWh usable (50% of 10kWh) vs 8 kWh consumption → deficit → charge.
    result = _run(_ctrl_no_solar(consumption=8.0))
    assert result["should_charge"] is True
    assert result["solar_forecast_kwh"] is None


def test_unset_solar_sensor_no_charge_when_usable_covers_consumption():
    result = _run(_ctrl_no_solar(consumption=1.0))
    assert result["should_charge"] is False


if __name__ == "__main__":
    test_unset_solar_sensor_does_not_crash()
    test_unset_solar_sensor_falls_back_to_conservative_mode()
    test_unset_solar_sensor_no_charge_when_usable_covers_consumption()
    print("ok")
