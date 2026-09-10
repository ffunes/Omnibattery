"""Per-pack cell voltage and the worst-pack delta (issue #439).

Registers 37007/37008 are pack 1's max/min cell voltage, not the battery's
(#415). On a Venus A/D, which fills its packs in sequence and rotates the active
pack every 7-50 minutes, that made the "Cell Delta" health sensor a pack-1
reading wearing a whole-battery label.

Pinned here:

* the per-pack registers sit on the same stride-100 block as the SOC, at
  offsets +5/+6, and cost nothing until the owner enables them;
* an absent slot loses its cell entities along with its SOC, on the SOC probe's
  verdict rather than a second probe;
* the recorded delta becomes the *worst pack's* spread, never a max-minus-min
  taken across packs — those are independent BMSs and #415 measured 155 mV
  between two healthy ones;
* a battery that reports no per-pack voltage keeps the old reading exactly.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery.const import (
    PACK_MAX_CELL_KEYS,
    PACK_MIN_CELL_KEYS,
    PACK_SOC_KEYS,
)
from custom_components.omnibattery.const.registers_va import SENSOR_DEFINITIONS_VA
from custom_components.omnibattery.control.pack_soc import (
    pack_cell_deltas,
    worst_pack_delta,
)
from custom_components.omnibattery.drivers import MarstekModbusDriver
from custom_components.omnibattery.drivers.marstek import _PACK_PROBE_CYCLES


def _driver(reads: dict[int, int | None] | None = None):
    client = AsyncMock()
    client.async_write_register = AsyncMock(return_value=True)
    client.async_read_block = AsyncMock(return_value=None)

    async def _read(register, data_type="uint16", count=None, sensor_key=None):
        return (reads or {}).get(register)

    client.async_read_register = AsyncMock(side_effect=_read)
    return MarstekModbusDriver("1.2.3.4", 502, "vD", client=client, ems_version=149)


# --- register layout --------------------------------------------------------


def test_cell_registers_ride_the_soc_stride_and_ship_disabled():
    defs = {d["key"]: d for d in SENSOR_DEFINITIONS_VA}
    for n in range(1, 8):
        base = 34000 + 100 * (n - 1)
        # +2 is the SOC the stride is confirmed on (34602/pack 7, #415).
        assert defs[f"battery_soc_pack_{n}"]["register"] == base + 2
        vmax = defs[f"max_cell_voltage_pack_{n}"]
        vmin = defs[f"min_cell_voltage_pack_{n}"]
        assert (vmax["register"], vmin["register"]) == (base + 5, base + 6)
        # Off by default and on the slow schedule: a Venus A/D has one TCP slot,
        # so these must not cost a frame to anyone who has not asked for them.
        assert vmax["enabled_by_default"] is False
        assert vmin["enabled_by_default"] is False
        assert vmax["scan_interval"] == "low"
    # Pack 1's pair mirrors 37007/37008 — same source pointers in firmware v150.
    assert defs["max_cell_voltage"]["register"] == 37007
    assert defs["max_cell_voltage_pack_1"]["register"] == 34005


def test_cell_keys_stay_out_of_the_control_dependency_set():
    # Nothing in the control layer reads them, so unlike the pack SOCs they must
    # not be forced to keep polling while their entities are disabled.
    driver = _driver()
    assert driver.control_dependency_keys.isdisjoint(PACK_MAX_CELL_KEYS)
    assert driver.control_dependency_keys.isdisjoint(PACK_MIN_CELL_KEYS)


# --- slot probe -------------------------------------------------------------


@pytest.mark.asyncio
async def test_absent_slot_loses_its_cell_entities_with_its_soc():
    """Two packs present: slots 3-7 must drop all three of their keys."""
    driver = _driver({32104: 80, 34002: 812, 34102: 795})

    for _ in range(_PACK_PROBE_CYCLES):
        await driver.read_telemetry(["battery_soc"])
        for key in PACK_SOC_KEYS:
            await driver.read_telemetry([key])

    present = {d["key"] for d in driver.sensor_definitions}
    polled = {k for g in driver.read_groups for k in g.keys}
    for n in (1, 2):
        assert f"max_cell_voltage_pack_{n}" in present
        assert f"min_cell_voltage_pack_{n}" in present
    for n in range(3, 8):
        assert f"max_cell_voltage_pack_{n}" not in present
        assert f"min_cell_voltage_pack_{n}" not in present
        assert f"max_cell_voltage_pack_{n}" not in polled


# --- the delta --------------------------------------------------------------


def _coord(**data):
    return SimpleNamespace(data=data)


def test_worst_pack_wins_and_is_not_a_cross_pack_spread():
    # The #415 shape: pack 1 is tight, pack 2 holds the diverging cell. A
    # max-minus-min across packs would read 3.517 - 3.353 and blame the battery;
    # the answer is pack 2's own spread, and the pack number to go and look at.
    coord = _coord(
        max_cell_voltage_pack_1=3.363, min_cell_voltage_pack_1=3.353,
        max_cell_voltage_pack_2=3.517, min_cell_voltage_pack_2=3.320,
        max_cell_voltage_pack_3=3.340, min_cell_voltage_pack_3=3.338,
    )
    assert pack_cell_deltas(coord) == {1: 10.0, 2: 197.0, 3: 2.0}
    worst = worst_pack_delta(coord)
    assert worst["pack"] == 2
    assert worst["delta_mV"] == 197.0
    assert (worst["vmax_V"], worst["vmin_V"]) == (3.517, 3.32)
    # The stored triple must stay self-consistent, or history lies.
    assert round((worst["vmax_V"] - worst["vmin_V"]) * 1000, 1) == worst["delta_mV"]


def test_no_per_pack_telemetry_leaves_the_reading_alone():
    # Every model except Venus A/D, and every Venus A/D with the entities off.
    assert worst_pack_delta(_coord(max_cell_voltage=3.48, min_cell_voltage=3.31)) is None
    assert pack_cell_deltas(_coord()) == {}
    assert worst_pack_delta(SimpleNamespace(data=None)) is None


def test_implausible_readings_are_dropped_not_shown():
    # The +5/+6 offsets come from a third-party map and are not hardware-confirmed
    # the way the SOC's +2 is. A slot pointing at something that is not a cell
    # voltage must not surface as a health number.
    coord = _coord(
        max_cell_voltage_pack_1=3.340, min_cell_voltage_pack_1=3.330,
        max_cell_voltage_pack_2=0.0, min_cell_voltage_pack_2=0.0,
        max_cell_voltage_pack_3=65.0, min_cell_voltage_pack_3=3.3,
        # Inverted: a decode error, not a 300 mV imbalance.
        max_cell_voltage_pack_4=3.10, min_cell_voltage_pack_4=3.40,
    )
    assert pack_cell_deltas(coord) == {1: 10.0}
    assert worst_pack_delta(coord)["pack"] == 1


@pytest.mark.asyncio
async def test_recorded_measurement_follows_the_worst_pack():
    """The three call sites all pass 37007/37008; the monitor must override."""
    from custom_components.omnibattery.tracking.balance_monitor import BalanceMonitor

    monitor = BalanceMonitor.__new__(BalanceMonitor)
    monitor._data = {}
    monitor._sensor_groups = {}
    monitor._store = SimpleNamespace(async_save=AsyncMock())
    monitor._hass = SimpleNamespace()
    monitor._controller = SimpleNamespace()

    coord = SimpleNamespace(
        device_key="1.2.3.4_502",
        name="Venus D",
        data={
            "max_cell_voltage": 3.363, "min_cell_voltage": 3.353,
            "max_cell_voltage_pack_1": 3.363, "min_cell_voltage_pack_1": 3.353,
            "max_cell_voltage_pack_2": 3.517, "min_cell_voltage_pack_2": 3.320,
        },
    )
    # Caller hands over pack 1's registers, as max_soc_charge and weekly do.
    await monitor.async_record_top_balance_measurement(
        coord, 3.363, 3.353, 99.8, phase="top_charge_3_55v"
    )
    entry = monitor._data["1.2.3.4_502"]["readings"][-1]
    assert entry["delta_mV"] == 197.0
    assert entry["pack"] == 2
    assert entry["packs"] == {1: 10.0, 2: 197.0}
    assert (entry["vmax_V"], entry["vmin_V"]) == (3.517, 3.32)
