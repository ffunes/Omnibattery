"""Characterization of what the core does with ``data["backup_function"]`` (T7.2, #40).

Pins, for the eight drivers, the value each one publishes under ``backup_function``
and what the two shared callers do with it, so the ``backup_enabled()`` query can
replace ``_backup_switch_enabled`` without changing a result.

The core reads the key as Marstek's switch: 0 is armed, 1 is off. Published values:

* marstek    the raw register, 0 or 1;
* esphome    maps the firmware select back to the wire value, 0 or 1;
* huawei     publishes a text: "Off-grid", "Ready" or "Disabled";
* anker, hoymiles, indevolt, sessy, zendure  never publish the key.

``None`` ("not read yet") is added for every driver. The two callers:
``ChargeDischargeController._is_backup_function_active`` and the shutdown writes
of ``async_unload_entry`` (which skip a battery that is feeding the backup port).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

import custom_components.omnibattery as integration
from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import DOMAIN
from tests.test_driver_construction_characterization import (
    BRAND_CONFIGS,
    _build_coordinator,
)

ABSENT = object()  # the driver does not publish the key

# brand -> every value the driver can leave under ``backup_function``
PUBLISHED = {
    "marstek": [0, 1, None, ABSENT],
    "esphome": [0, 1, None, ABSENT],
    "huawei": ["Off-grid", "Ready", "Disabled", None, ABSENT],
    "anker": [None, ABSENT],
    "hoymiles": [None, ABSENT],
    "indevolt": [None, ABSENT],
    "sessy": [None, ABSENT],
    "zendure": [None, ABSENT],
}
CASES = [(b, v) for b, values in PUBLISHED.items() for v in values]
IDS = [f"{b}-{'absent' if v is ABSENT else v!r}" for b, v in CASES]


def _coordinator(brand):
    """The real coordinator (and its real driver) for the brand.

    Must run inside a test coroutine: the Marstek driver creates a Modbus client
    that needs a running loop.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(DataUpdateCoordinator, "__init__", lambda *_a, **_k: None)
        return _build_coordinator(BRAND_CONFIGS[brand])


def _outage(value):
    """Armed and in a grid outage: the data below carries the backup state (4),
    which Huawei ignores in favour of its own off-grid bit."""
    return value == "Off-grid" if isinstance(value, str) else _armed(value)


def _armed(value):
    """What the core made of the value before the query existed."""
    if value is ABSENT or value is None:
        return False
    if isinstance(value, str):
        return value in ("Off-grid", "Ready")
    return value == 0


def _data(value, **rest):
    data = dict(rest)
    if value is not ABSENT:
        data["backup_function"] = value
    return data


class _Hashable(SimpleNamespace):
    """A coordinator stand-in usable as a dict key (the controller keys by it)."""

    __hash__ = object.__hash__


# ---------------------------------------------------------------------------
# Caller 1: _is_backup_function_active
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("brand", "value"), CASES, ids=IDS)
async def test_backup_function_active(brand, value):
    coord = _Hashable(
        name="BAT1", driver=_coordinator(brand).driver,
        data=_data(value, ac_offgrid_power=200, inverter_state=4),
    )
    ctrl = SimpleNamespace(
        _backup_cooldown_until={coord: dt_util.utcnow()},
    )
    active = ChargeDischargeController._is_backup_function_active(ctrl, coord)
    assert active is _outage(value)
    if not _armed(value):
        # A switch that is not armed also clears a lingering cooldown.
        assert coord not in ctrl._backup_cooldown_until


# ---------------------------------------------------------------------------
# Caller 2: shutdown writes in async_unload_entry
# ---------------------------------------------------------------------------

async def _noop(*_args, **_kwargs):
    return None


@pytest.mark.parametrize(("brand", "value"), CASES, ids=IDS)
async def test_shutdown_skips_an_armed_backup(brand, value, monkeypatch):
    monkeypatch.setattr(integration.asyncio, "sleep", _noop)
    standby_calls = []

    async def standby():
        standby_calls.append(1)
        return True

    coord = SimpleNamespace(
        name="BAT1", driver=_coordinator(brand).driver, _is_connected=True,
        data=_data(value, ac_offgrid_power=200, inverter_state=4),
        capabilities=SimpleNamespace(has_rs485_control=False),
        set_shutting_down=lambda _flag: None, standby=standby, disconnect=_noop,
    )
    entry = SimpleNamespace(entry_id="entry1")

    async def unload_platforms(_entry, _platforms):
        return True

    hass = SimpleNamespace(
        data={DOMAIN: {"entry1": {"coordinators": [coord]}}},
        config_entries=SimpleNamespace(
            async_unload_platforms=unload_platforms,
            async_entries=lambda _domain: [entry, SimpleNamespace(entry_id="entry2")],
        ),
    )
    assert await integration.async_unload_entry(hass, entry) is True
    assert standby_calls == ([] if _outage(value) else [1])
