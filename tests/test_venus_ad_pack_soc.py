"""Per-pack SOC on Venus A/D (issue #350).

A Venus A/D couples several battery packs and fills them in sequence, so the
aggregate SOC at 32104 can read 100% while a later pack is still empty. Each
pack publishes its own SOC on a stride-100 layout (34002, 34102, ...).

Two things are pinned here:

* the start-up probe that learns which of the six slots exist — an absent slot
  may either fail to answer or read a flat 0, and the probe must be right under
  both without knowing which the firmware does;
* the *additional* discharge floor: a pack at min_soc blocks discharge even
  when the aggregate is still above it. The symmetric swap (max(pack_soc)
  replacing the aggregate, which would extend discharge) is deliberately not
  implemented — see the plan's F3.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery import ChargeDischargeController
from custom_components.omnibattery.const import PACK_SOC_KEYS
from custom_components.omnibattery.drivers import MarstekModbusDriver
from custom_components.omnibattery.drivers.marstek import _PACK_PROBE_CYCLES

from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
    _BMS_CUTOFF_REQUIRED_CYCLES,
)

from tests.conftest import FakeCoordinator


# --- driver: pack discovery -------------------------------------------------


def _driver(reads: dict[int, int | None], *, version="vD"):
    """Driver over the real vD definitions, answering from a register map."""
    client = AsyncMock()
    client.async_write_register = AsyncMock(return_value=True)
    client.async_read_block = AsyncMock(return_value=None)

    async def _read(register, data_type="uint16", count=None, sensor_key=None):
        return reads.get(register)

    client.async_read_register = AsyncMock(side_effect=_read)
    return MarstekModbusDriver(
        "1.2.3.4", 502, version, client=client, ems_version=149,
    )


def _pack_register(n: int) -> int:
    return 34002 + 100 * (n - 1)


async def _probe(driver, cycles=_PACK_PROBE_CYCLES):
    """Run the coordinator's poll shape: aggregate first, then one call per pack."""
    for _ in range(cycles):
        await driver.read_telemetry(["battery_soc"])
        for key in PACK_SOC_KEYS:
            await driver.read_telemetry([key])


@pytest.mark.asyncio
async def test_absent_slots_that_fail_to_answer_are_dropped():
    # Variant 1 of the unknown: an empty slot raises a Modbus exception, so the
    # driver omits its key from the snapshot.
    reads = {32104: 80, _pack_register(1): 812, _pack_register(2): 795}
    driver = _driver(reads)
    await _probe(driver)

    assert driver._packs == {"battery_soc_pack_1", "battery_soc_pack_2"}
    keys = {k for g in driver.read_groups for k in g.keys}
    assert keys.intersection(PACK_SOC_KEYS) == driver._packs
    present = {d["key"] for d in driver.sensor_definitions}
    assert present.intersection(PACK_SOC_KEYS) == driver._packs


@pytest.mark.asyncio
async def test_absent_slots_that_answer_zero_are_dropped():
    # Variant 2: an empty slot answers 0. Only the aggregate separates that from
    # a pack that is genuinely flat, hence the "0 and aggregate > 5%" rule.
    reads = {32104: 80, _pack_register(1): 812, _pack_register(2): 795}
    reads.update({_pack_register(n): 0 for n in (3, 4, 5, 6)})
    driver = _driver(reads)
    await _probe(driver)

    assert driver._packs == {"battery_soc_pack_1", "battery_soc_pack_2"}


@pytest.mark.asyncio
async def test_flat_pack_on_an_empty_battery_is_kept():
    # The trap the plan calls out: discarding every slot that reads 0 loses a
    # pack that is really at 0%. With the aggregate down at 3% a 0 is real.
    reads = {32104: 3, _pack_register(1): 0, _pack_register(2): 0}
    driver = _driver(reads)
    await _probe(driver)

    assert driver._packs == {"battery_soc_pack_1", "battery_soc_pack_2"}


@pytest.mark.asyncio
async def test_one_transient_failure_does_not_hide_a_real_pack():
    reads = {32104: 80, _pack_register(1): 812, _pack_register(2): 795}
    driver = _driver(reads)

    del reads[_pack_register(2)]          # pack 2 misses the first cycle only
    await _probe(driver, cycles=1)
    reads[_pack_register(2)] = 795
    await _probe(driver, cycles=_PACK_PROBE_CYCLES - 1)

    assert "battery_soc_pack_2" in driver._packs


@pytest.mark.asyncio
async def test_pack_keys_poll_while_probing_then_only_the_found_ones():
    # They ship disabled by default, so without a dependency claim the
    # coordinator would skip them and the probe could never answer.
    reads = {32104: 80, _pack_register(1): 812}
    driver = _driver(reads)
    assert driver.control_dependency_keys.issuperset(PACK_SOC_KEYS)

    await _probe(driver)
    assert driver.control_dependency_keys.intersection(PACK_SOC_KEYS) == {
        "battery_soc_pack_1"
    }


@pytest.mark.asyncio
async def test_v3_has_no_pack_sensors():
    driver = _driver({37005: 80}, version="v3")
    assert driver.control_dependency_keys.isdisjoint(PACK_SOC_KEYS)
    assert not {d["key"] for d in driver.sensor_definitions}.intersection(PACK_SOC_KEYS)


# --- control: pack-aware charge ceiling and discharge floor -----------------
#
# Both verdicts read min(pack_soc), and the reason is that the hardware is not
# symmetric:
#   full  <=> the LEAST full pack reached the ceiling  -> min(pack_soc)
#   empty <=> the FIRST pack reached the floor         -> min(pack_soc)
# Charging walks on to the next pack when one fills, which is what the #350
# handovers showed, so the ceiling must not fire on the first pack to get there.
# Discharging does not walk on: a Venus D stops the whole battery at its first
# pack and strands the rest, so the floor must fire exactly there.


def _discharge_blocks(data, *, min_soc=10):
    """Run _refresh_battery_discharge_limit_blocks and report what it set."""
    coordinator = FakeCoordinator(min_soc=min_soc, data=data)
    set_blocks = []
    ctrl = SimpleNamespace(
        coordinators=[coordinator],
        _effective_discharge_min_soc=lambda c: (min_soc, "min_soc"),
        set_discharge_block=lambda *a, **kw: set_blocks.append((a, kw)),
        remove_discharge_block=lambda *a, **kw: None,
    )
    ChargeDischargeController._refresh_battery_discharge_limit_blocks(ctrl)
    return set_blocks


def _dischargeable(data, *, min_soc=10):
    coordinator = FakeCoordinator(min_soc=min_soc, data=data)
    ctrl = SimpleNamespace(
        coordinators=[coordinator],
        _non_responsive=SimpleNamespace(is_excluded=lambda c: False),
        _is_backup_function_active=lambda c: False,
        _is_manual_slot_owned=lambda c: False,
        is_discharge_blocked=lambda c: False,
    )
    got = ChargeDischargeController._get_available_batteries(ctrl, is_charging=False)
    return got == [coordinator]


def _chargeable(data, *, max_soc=100):
    coordinator = FakeCoordinator(max_soc=max_soc, data=data)
    coordinator.enable_charge_hysteresis = False
    ctrl = SimpleNamespace(
        coordinators=[coordinator],
        _non_responsive=SimpleNamespace(is_excluded=lambda c: False),
        _is_backup_function_active=lambda c: False,
        _is_manual_slot_owned=lambda c: False,
        is_charge_blocked=lambda c: False,
        get_charge_blockers=lambda c: {},
        _weekly_full_charge_unlocked=lambda: False,
        _effective_charge_max_soc=lambda c, w: (max_soc, "max_soc"),
        _should_charge_to_bms_cutoff=lambda c, m: False,
        _normal_balance_recal_override={},
        _weekly_charge_mgr=SimpleNamespace(is_battery_full=lambda c: False),
        _predictive_charge_target_soc=None,
    )
    got = ChargeDischargeController._get_available_batteries(ctrl, is_charging=True)
    return got == [coordinator]


# --- discharge: the first pack to empty decides -----------------------------


def test_one_pack_at_the_floor_ends_the_discharge():
    # A Venus D stops the whole battery there. The 50% in the other pack is
    # real but unreachable, and counting it commands into a stopped battery.
    data = {"battery_soc": 30, "battery_soc_pack_1": 50.0, "battery_soc_pack_2": 10.0}
    assert _discharge_blocks(data)
    assert not _dischargeable(data)


def test_a_battery_above_the_floor_on_every_pack_keeps_going():
    data = {"battery_soc": 30, "battery_soc_pack_1": 40.0, "battery_soc_pack_2": 12.0}
    assert not _discharge_blocks(data)
    assert _dischargeable(data)


def test_the_measured_six_pack_case():
    """4 September, cutoff 12%: aggregate 15%, three packs on the floor, and the
    device delivering nothing while the control layer commanded 938 W."""
    data = {
        "battery_soc": 15,
        "battery_soc_pack_1": 12.1, "battery_soc_pack_2": 12.0,
        "battery_soc_pack_3": 19.0, "battery_soc_pack_4": 12.0,
        "battery_soc_pack_5": 20.3, "battery_soc_pack_6": 19.3,
    }
    assert _discharge_blocks(data, min_soc=12)
    assert not _dischargeable(data, min_soc=12)


def test_packs_override_an_aggregate_already_at_the_floor():
    # Unchanged by the swap to min(): the aggregate is at the floor but every
    # pack is above it, so the battery keeps going.
    data = {"battery_soc": 10, "battery_soc_pack_1": 40.0, "battery_soc_pack_2": 12.0}
    assert not _discharge_blocks(data)
    assert _dischargeable(data)


def test_a_slot_written_off_by_the_probe_does_not_read_as_empty():
    """The stale zero. An absent slot answers a flat 0 for the probe's cycles;
    the driver then stops polling it, so the coordinator keeps that 0 for good.
    Harmless against the fullest pack, fatal against the first: a battery at
    80% would be excluded from discharge and never released, because the
    min-SOC latch only clears on a recovery the stale key can never show."""
    data = {
        "battery_soc": 80,
        "battery_soc_pack_1": 80.0, "battery_soc_pack_2": 80.0,
        "battery_soc_pack_3": None, "battery_soc_pack_4": None,
        "battery_soc_pack_5": None, "battery_soc_pack_6": None,
    }
    assert not _discharge_blocks(data)
    assert _dischargeable(data)


def test_out_of_range_pack_reading_is_ignored():
    # The addresses come from a third-party register map, so a garbage value
    # must not be able to move the floor in either direction.
    assert _discharge_blocks({"battery_soc": 9, "battery_soc_pack_1": 6553.5})


def test_no_pack_telemetry_behaves_exactly_as_before():
    assert not _discharge_blocks({"battery_soc": 30})
    assert _discharge_blocks({"battery_soc": 9})
    assert _dischargeable({"battery_soc": 30})
    assert not _dischargeable({"battery_soc": 9})


# --- charge: the least full pack decides ------------------------------------


def test_a_full_first_pack_does_not_end_the_charge():
    # #350 itself: the aggregate reads 100% as soon as the first coupled pack
    # fills, while later packs are still empty.
    assert _chargeable({"battery_soc": 100, "battery_soc_pack_1": 100.0,
                        "battery_soc_pack_2": 40.0})


def test_charge_stops_when_the_least_full_pack_reaches_the_ceiling():
    assert not _chargeable({"battery_soc": 100, "battery_soc_pack_1": 100.0,
                            "battery_soc_pack_2": 100.0})


def test_charge_respects_a_lower_ceiling_on_the_least_full_pack():
    assert not _chargeable({"battery_soc": 70, "battery_soc_pack_1": 90.0,
                            "battery_soc_pack_2": 80.0}, max_soc=80)
    assert _chargeable({"battery_soc": 70, "battery_soc_pack_1": 90.0,
                        "battery_soc_pack_2": 79.0}, max_soc=80)


def test_charge_without_pack_telemetry_is_unchanged():
    assert _chargeable({"battery_soc": 99})
    assert not _chargeable({"battery_soc": 100})


# --- charge: the BMS-cutoff arming gate (#350's actual 27 Aug trace) ---------


class _PackCoord:
    """Coordinator stand-in for the weekly manager (counter is name-keyed)."""

    def __init__(self, **data):
        self.name = "bat"
        self.battery_version = "vD"
        self.brand = None
        self.commanded_charge_power = 200
        self.data = {"battery_power": 0, "inverter_state": 1, **data}


def _weekly(coord):
    ctrl = SimpleNamespace(coordinators=[coord], weekly_full_charge_enabled=True)
    m = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    m._controller = ctrl
    m._bms_cutoff_counts = {}
    m._already_complete_logged = False
    m.is_active = lambda: True
    return m


def test_taper_zone_does_not_arm_while_a_pack_is_still_filling():
    # Literal reproduction of the 27 Aug trace in #350: the top cell sits at
    # 3.481 V because an earlier pack finished hours ago, while the least full
    # pack is at 89.9%. A lull in acceptance must not be read as a cutoff.
    coord = _PackCoord(
        battery_soc=94, max_cell_voltage=3.481,
        battery_soc_pack_1=100.0, battery_soc_pack_2=89.9,
    )
    weekly = _weekly(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES * 3):
        weekly.tick_bms_cutoff()
    assert weekly._bms_cutoff_counts.get("bat", 0) == 0
    assert weekly.is_battery_full(coord) is False


def test_taper_zone_still_arms_when_every_pack_is_at_the_top():
    coord = _PackCoord(
        battery_soc=94, max_cell_voltage=3.481,
        battery_soc_pack_1=100.0, battery_soc_pack_2=99.4,
    )
    weekly = _weekly(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        weekly.tick_bms_cutoff()
    assert weekly._bms_cutoff_counts["bat"] >= _BMS_CUTOFF_REQUIRED_CYCLES


def test_taper_zone_arming_unchanged_without_pack_telemetry():
    coord = _PackCoord(battery_soc=94, max_cell_voltage=3.481)
    weekly = _weekly(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        weekly.tick_bms_cutoff()
    assert weekly._bms_cutoff_counts["bat"] >= _BMS_CUTOFF_REQUIRED_CYCLES


def test_full_verdict_waits_for_the_least_full_pack():
    coord = _PackCoord(
        battery_soc=100, max_cell_voltage=3.40,
        battery_soc_pack_1=100.0, battery_soc_pack_2=62.0,
    )
    assert _weekly(coord).is_battery_full(coord) is False
    coord.data["battery_soc_pack_2"] = 100.0
    assert _weekly(coord).is_battery_full(coord) is True


@pytest.mark.asyncio
async def test_a_written_off_slot_is_purged_not_merely_dropped():
    """Stopping the polls is not enough: the zeros already stored stay unless
    the slot's key is overwritten once, on the cycle it is written off."""
    reads = {32104: 80, _pack_register(1): 800, _pack_register(2): 800}
    driver = _driver(reads)

    # Every slot answers during the probe; the absent ones answer a flat 0.
    for n in range(3, 7):
        reads[_pack_register(n)] = 0

    seen = []
    for _ in range(_PACK_PROBE_CYCLES):
        await driver.read_telemetry(["battery_soc"])
        for key in PACK_SOC_KEYS:
            seen.append(await driver.read_telemetry([key]))

    assert driver._packs == {"battery_soc_pack_1", "battery_soc_pack_2"}

    # Exactly one snapshot carries the purge, on the cycle the probe finished,
    # and it names every written-off slot.
    purges = [s for s in seen if s.get("battery_soc_pack_3", "missing") is None]
    assert len(purges) == 1
    for n in range(3, 7):
        assert purges[0][f"battery_soc_pack_{n}"] is None
    # Only the written-off slots are purged; a populated one is never nulled.
    assert not any(
        s.get("battery_soc_pack_1", "missing") is None
        or s.get("battery_soc_pack_2", "missing") is None
        for s in seen
    )
