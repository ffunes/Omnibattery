"""Per-pack cell voltage and the worst-pack delta (issue #439).

Registers 37007/37008 are pack 1's max/min cell voltage, not the battery's
(#415). On a Venus A/D, which fills its packs in sequence and rotates the active
pack every 7-50 minutes, that made the "Cell Delta" health sensor a pack-1
reading wearing a whole-battery label.

Pinned here:

* the per-pack registers sit on the same stride-100 block as the SOC, at
  offsets +5/+6, and are block-read one frame per pack;
* a pack whose cell registers never answer leaves the poll schedule after three
  tries, and takes nothing else with it;
* they poll with their entities disabled, so the delta is right for everyone
  and nobody has to opt in to a correct health number;
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
        # Off by default as entities — fourteen diagnostic rows per battery is
        # clutter — and on the slow schedule, because a Venus A/D has one TCP slot.
        assert vmax["enabled_by_default"] is False
        assert vmin["enabled_by_default"] is False
        assert vmax["scan_interval"] == "low"
    # Pack 1's pair mirrors 37007/37008 — same source pointers in firmware v150.
    assert defs["max_cell_voltage"]["register"] == 37007
    assert defs["max_cell_voltage_pack_1"]["register"] == 34005


def test_cell_keys_poll_while_their_entities_are_disabled():
    # The whole point of the fix is a delta nobody has to opt into. The entities
    # ship off; the reads must not, or the balance monitor is back to pack 1.
    driver = _driver()
    assert driver.balance_dependency_keys.issuperset(PACK_MAX_CELL_KEYS)
    assert driver.balance_dependency_keys.issuperset(PACK_MIN_CELL_KEYS)
    # They are balance dependencies, not control ones: nothing in the control
    # layer reads them, and #415 is explicit that it must stay that way.
    assert driver.control_dependency_keys.isdisjoint(PACK_MAX_CELL_KEYS)
    assert driver.control_dependency_keys.isdisjoint(PACK_MIN_CELL_KEYS)


def test_each_pack_pair_costs_one_frame_not_two():
    # The objection to reading these at all is the single TCP slot, so the
    # adjacency has to be used: 34005/34006 are one block read per pack.
    driver = _driver()
    blocks = {b["start"]: b for b in driver._register_blocks}
    for n in range(1, 8):
        block = blocks[34005 + 100 * (n - 1)]
        assert block["count"] == 2
        assert block["scan_interval"] == "low"
        assert [m["key"] for m in block["members"]] == [
            f"max_cell_voltage_pack_{n}", f"min_cell_voltage_pack_{n}",
        ]
    groups = {g.keys for g in driver.read_groups}
    assert ("max_cell_voltage_pack_1", "min_cell_voltage_pack_1") in groups


def test_a_v3_never_gets_the_pack_cell_blocks():
    # v3 shares the entity map but has no 34000-block, and a block group is built
    # unconditionally — so this would be a failing read every cycle on the model
    # with the least headroom to spare.
    client = AsyncMock()
    client.async_read_register = AsyncMock(return_value=None)
    client.async_read_block = AsyncMock(return_value=None)
    v3 = MarstekModbusDriver("1.2.3.4", 502, "v3", client=client, ems_version=149)
    assert all(b["start"] != 34005 for b in v3._register_blocks)
    assert v3.balance_dependency_keys == frozenset()


# --- slot probe -------------------------------------------------------------


async def _probe(driver):
    """Run the coordinator's poll shape until the start-up probe has settled."""
    for _ in range(_PACK_PROBE_CYCLES):
        await driver.read_telemetry(["battery_soc"])
        for key in PACK_SOC_KEYS + PACK_MAX_CELL_KEYS + PACK_MIN_CELL_KEYS:
            await driver.read_telemetry([key])


@pytest.mark.asyncio
async def test_absent_slot_loses_its_cell_registers_with_its_soc():
    """Two packs present: slots 3-7 must drop all three of their keys."""
    driver = _driver({32104: 80, 34002: 812, 34102: 795,
                      34005: 3340, 34006: 3330, 34105: 3352, 34106: 3348})
    await _probe(driver)

    present = {d["key"] for d in driver.sensor_definitions}
    polled = {k for g in driver.read_groups for k in g.keys}
    for n in (1, 2):
        assert f"max_cell_voltage_pack_{n}" in present
        assert f"min_cell_voltage_pack_{n}" in present
    for n in range(3, 8):
        assert f"max_cell_voltage_pack_{n}" not in present
        assert f"max_cell_voltage_pack_{n}" not in polled
    assert driver.balance_dependency_keys == {
        "max_cell_voltage_pack_1", "min_cell_voltage_pack_1",
        "max_cell_voltage_pack_2", "min_cell_voltage_pack_2",
    }


@pytest.mark.asyncio
async def test_a_pack_whose_cell_registers_never_answer_stops_being_asked():
    """The whole objection to this feature is the single TCP slot.

    If the extrapolated +5/+6 offsets are not there on some firmware, the reads
    must leave the schedule after three tries instead of costing a frame per pack
    per cycle forever — and the SOC, which the charge ceiling and discharge floor
    stand on, must survive that untouched.
    """
    driver = _driver({32104: 80, 34002: 812, 34102: 795})
    await _probe(driver)

    polled = {k for g in driver.read_groups for k in g.keys}
    assert polled.isdisjoint(PACK_MAX_CELL_KEYS)
    assert polled.isdisjoint(PACK_MIN_CELL_KEYS)
    assert driver.balance_dependency_keys == frozenset()
    # The SOC verdict is unaffected.
    assert polled.intersection(PACK_SOC_KEYS) == {
        "battery_soc_pack_1", "battery_soc_pack_2",
    }
    assert driver.control_dependency_keys.issuperset(
        {"battery_soc_pack_1", "battery_soc_pack_2"}
    )


@pytest.mark.asyncio
async def test_the_soc_write_off_does_not_wait_on_the_cell_probe():
    """Cell registers are a diagnostic; the SOC verdict must not hang on them.

    The coordinator only polls what its entities and dependencies ask for, so a
    build where the cell keys are never requested must still settle the SOC.
    """
    driver = _driver({32104: 80, 34002: 812})
    for _ in range(_PACK_PROBE_CYCLES):
        await driver.read_telemetry(["battery_soc"])
        for key in PACK_SOC_KEYS:
            await driver.read_telemetry([key])

    polled = {k for g in driver.read_groups for k in g.keys}
    assert polled.intersection(PACK_SOC_KEYS) == {"battery_soc_pack_1"}


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
    # Every model except Venus A/D, and any Venus A/D slot that never answered.
    assert worst_pack_delta(_coord(max_cell_voltage=3.48, min_cell_voltage=3.31)) is None
    assert pack_cell_deltas(_coord()) == {}
    assert worst_pack_delta(SimpleNamespace(data=None)) is None


def test_only_the_two_impossible_pairs_are_dropped():
    # A pack is trusted exactly as far as a Venus E's 37007/37008 are. The two
    # things thrown out are the ones that cannot be a measurement at all: a zero,
    # and a max below its min. Nothing else is second-guessed.
    coord = _coord(
        max_cell_voltage_pack_1=3.340, min_cell_voltage_pack_1=3.330,
        max_cell_voltage_pack_2=0.0, min_cell_voltage_pack_2=0.0,
        # Inverted: a decode error, not a 300 mV imbalance.
        max_cell_voltage_pack_3=3.10, min_cell_voltage_pack_3=3.40,
        # Wide but real: #415 measured 197 mV inside one pack, and the status
        # thresholds go to 250 mV, so this must reach the sensor, not be filtered.
        max_cell_voltage_pack_4=3.517, min_cell_voltage_pack_4=3.320,
    )
    assert pack_cell_deltas(coord) == {1: 10.0, 4: 197.0}
    assert worst_pack_delta(coord)["pack"] == 4


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
