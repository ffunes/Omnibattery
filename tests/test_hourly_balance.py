"""Regression tests for HourlyBalanceManager._push_sensors.

Pins the fix for #355: disabling the net-balance entity in the HA UI leaves it
unattached (``hass is None``). ``async_write_ha_state()`` then raises
``RuntimeError`` and, called from inside the PD control cycle, stalls the loop
so the integration status is stuck on "initialising". ``_push_sensors`` must
skip unattached sensors and still push the attached ones.

No hardware, no real Home Assistant: the manager is built with ``__new__`` so
only ``_sensors`` exists, which is all ``_push_sensors`` touches.
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.marstek_venus_energy_manager.hourly_balance import (
    HourlyBalanceManager,
)


def _stub_sensor(hass):
    """A sensor that records writes and mimics HA's RuntimeError when detached."""
    calls = {"writes": 0}

    def async_write_ha_state():
        if hass is None:
            raise RuntimeError("Attribute hass is None")
        calls["writes"] += 1

    return SimpleNamespace(hass=hass, async_write_ha_state=async_write_ha_state, calls=calls)


def _mgr(sensors):
    mgr = HourlyBalanceManager.__new__(HourlyBalanceManager)
    mgr._sensors = sensors
    return mgr


def test_push_skips_detached_sensor():
    detached = _stub_sensor(hass=None)
    mgr = _mgr([detached])

    mgr._push_sensors()  # must not raise

    assert detached.calls["writes"] == 0


def test_push_writes_attached_sensor():
    attached = _stub_sensor(hass=object())
    mgr = _mgr([attached])

    mgr._push_sensors()

    assert attached.calls["writes"] == 1


def test_push_attached_unaffected_by_detached():
    detached = _stub_sensor(hass=None)
    attached = _stub_sensor(hass=object())
    mgr = _mgr([detached, attached])

    mgr._push_sensors()

    assert detached.calls["writes"] == 0
    assert attached.calls["writes"] == 1
