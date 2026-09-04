"""Regression tests for WeeklyFullChargeManager BMS-cutoff detection.

Pins the fix for the "full charge stops at the top voltage" bug: a battery that is merely
idle (≤10 W + Standby in the taper zone but NOT commanded to charge) must not be
mistaken for a real top-of-charge BMS cutoff. Only a battery we actually commanded
to charge yet refuses counts; once confirmed, the latch must survive the charge
exclusion that follows it.

No hardware, no real Home Assistant. ``Store(hass, ...)`` only stores references at
construction, so a SimpleNamespace hass is enough; ``is_active`` is overridden to
isolate the cutoff counter from day/feature gating.
"""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.omnibattery.const import NORMAL_BALANCE_TAPER_CELL_VOLTAGE
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
    _BMS_CUTOFF_REQUIRED_CYCLES,
)
from custom_components.omnibattery.control.max_soc_charge import MaxSocChargeManager

_IN_ZONE = NORMAL_BALANCE_TAPER_CELL_VOLTAGE + 0.05  # cell above taper entry
_STANDBY = 1


class _Coord:
    """Identity-hashable coordinator stand-in (name-keyed in the counter dict)."""

    def __init__(self, name, *, soc, power, commanded, vmax=_IN_ZONE, inv=_STANDBY,
                 battery_version="v2", brand=None):
        self.name = name
        self.battery_version = battery_version
        self.brand = brand
        self.commanded_charge_power = commanded
        self.data = {
            "battery_soc": soc,
            "battery_power": power,
            "inverter_state": inv,
            "max_cell_voltage": vmax,
        }


def _mgr(coord, *, top_charge_manager=None):
    """Build a manager without its Store (only the cutoff-counter state matters)."""
    ctrl = SimpleNamespace(coordinators=[coord], weekly_full_charge_enabled=True)
    if top_charge_manager is not None:
        ctrl._max_soc_mgr = top_charge_manager
    m = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    m._controller = ctrl
    m._bms_cutoff_counts = {}
    m._already_complete_logged = False
    m.is_active = lambda: True  # weekly active; bypass day/feature gating
    return m


def test_idle_battery_never_confirms_cutoff():
    """Idle in the taper zone (not commanded) must NOT accumulate cutoff cycles."""
    coord = _Coord("bat", soc=94, power=0, commanded=0)
    m = _mgr(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES * 3):
        m.tick_bms_cutoff()
    assert m._bms_cutoff_counts.get("bat", 0) == 0
    assert m.is_battery_full(coord) is False


def test_commanded_refusal_confirms_cutoff():
    """Commanded to charge but refusing (≤10 W + Standby) confirms after N cycles."""
    coord = _Coord("bat", soc=94, power=0, commanded=200)
    m = _mgr(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] >= _BMS_CUTOFF_REQUIRED_CYCLES
    assert m.is_battery_full(coord) is True


def test_zendure_commanded_refusal_confirms_without_inverter_state():
    """Zendure has no inverter_state, but its active command still gates a cutoff."""
    coord = _Coord(
        "zendure",
        soc=99,
        power=0,
        commanded=200,
        inv=None,
        brand="zendure",
    )
    m = _mgr(coord)

    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        m.tick_bms_cutoff()

    assert m._bms_cutoff_counts["zendure"] >= _BMS_CUTOFF_REQUIRED_CYCLES
    assert m.is_battery_full(coord) is True


def test_confirmed_cutoff_latches_when_battery_goes_idle():
    """Once confirmed, dropping the charge command must freeze (not reset) the count."""
    coord = _Coord("bat", soc=94, power=0, commanded=200)
    m = _mgr(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        m.tick_bms_cutoff()
    assert m.is_battery_full(coord) is True
    # Battery is now excluded → no longer commanded. Must stay full, not un-latch.
    coord.commanded_charge_power = 0
    coord.data["battery_power"] = 0
    for _ in range(10):
        m.tick_bms_cutoff()
    assert m.is_battery_full(coord) is True


def test_cutoff_below_99_confirms_without_weekly_charge():
    """v2 BMS cutting off at 98% (cells in taper zone) must confirm outside weekly
    charge — otherwise charge hysteresis never latches. Regression for the
    'stopped at 98%, hysteresis inactive' report."""
    coord = _Coord("bat", soc=98, power=0, commanded=200)
    m = _mgr(coord)
    m.is_active = lambda: False  # NOT in weekly full charge
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        m.tick_bms_cutoff()
    assert m.is_battery_full(coord) is True


def test_accepting_charge_resets_counter():
    """A battery taking the charge it was offered is not full → counter resets."""
    coord = _Coord("bat", soc=94, power=0, commanded=200)
    m = _mgr(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES - 1):
        m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] == _BMS_CUTOFF_REQUIRED_CYCLES - 1
    # Now it accepts charge.
    coord.data["battery_power"] = 150
    m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] == 0
    assert m.is_battery_full(coord) is False


def test_venus_ad_100_soc_waits_for_bms_cutoff_when_top_charge_path_is_active():
    coord = _Coord("bat", soc=100, power=200, commanded=200, vmax=3.60,
                   battery_version="vA")
    top_charge_manager = SimpleNamespace(
        should_charge_to_bms_cutoff=lambda _coord, _max_soc: True,
    )
    m = _mgr(coord, top_charge_manager=top_charge_manager)

    assert m.is_battery_full(coord) is False


def test_bms_cutoff_confirmation_is_read_without_soc_interpretation():
    coord = _Coord("bat", soc=100, power=0, commanded=200, vmax=3.60,
                   battery_version="vD")
    m = _mgr(coord)

    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        m.tick_bms_cutoff()

    assert m.is_bms_cutoff_confirmed(coord) is True


def test_venus_ad_first_cutoff_does_not_complete_until_retry_is_refused():
    coord = _Coord(
        "bat",
        soc=94,
        power=0,
        commanded=200,
        vmax=3.60,
        battery_version="vA",
    )
    ctrl = SimpleNamespace(
        coordinators=[coord],
        weekly_full_charge_enabled=True,
        _normal_balance_bms_cutoff_active={},
        _normal_balance_bms_cutoff_retry_pending={},
        _normal_balance_bms_cutoff_retry_active={},
        _normal_balance_bms_cutoff_retry_accept_count={},
        _normal_balance_bms_cutoff_measurement={},
        _normal_balance_date=None,
    )
    weekly = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    weekly._controller = ctrl
    weekly._bms_cutoff_counts = {"bat": _BMS_CUTOFF_REQUIRED_CYCLES}
    weekly._already_complete_logged = False
    weekly.is_active = lambda: True
    ctrl._weekly_charge_mgr = weekly
    ctrl._max_soc_mgr = MaxSocChargeManager(SimpleNamespace(), ctrl)

    # The first five-cycle refusal is provisional and must not complete weekly
    # charge or be exposed as a final full signal.
    assert weekly.is_battery_full(coord) is False
    assert ctrl._normal_balance_bms_cutoff_retry_pending[coord] is True

    # Relaxation opens exactly one retry window and clears the first counter.
    coord.data["max_cell_voltage"] = 3.57
    assert weekly.is_battery_full(coord) is False
    assert ctrl._normal_balance_bms_cutoff_retry_active[coord] is True
    assert weekly._bms_cutoff_counts == {}

    # The retry is accepted, then a later five-cycle refusal is final.
    coord.data["battery_power"] = 200
    weekly.tick_bms_cutoff()
    coord.data["battery_power"] = 0
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        weekly.tick_bms_cutoff()
    assert weekly.is_battery_full(coord) is True
    assert coord not in ctrl._normal_balance_bms_cutoff_retry_active


def test_venus_ad_rearms_three_sequential_pack_handovers_then_finishes():
    """Each sustained handover reopens one retry; the final refusal does not."""
    handover_vmax = NORMAL_BALANCE_TAPER_CELL_VOLTAGE + 0.001
    coord = _Coord(
        "bat",
        soc=93,
        power=0,
        commanded=200,
        vmax=handover_vmax,
        battery_version="vD",
    )
    ctrl = SimpleNamespace(
        coordinators=[coord],
        weekly_full_charge_enabled=True,
        _normal_balance_bms_cutoff_active={},
        _normal_balance_bms_cutoff_retry_pending={},
        _normal_balance_bms_cutoff_retry_active={},
        _normal_balance_bms_cutoff_retry_accept_count={},
        _normal_balance_bms_cutoff_measurement={},
        _normal_balance_date=None,
    )
    weekly = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    weekly._controller = ctrl
    weekly._bms_cutoff_counts = {}
    weekly._already_complete_logged = False
    weekly.is_active = lambda: True
    ctrl._weekly_charge_mgr = weekly
    ctrl._max_soc_mgr = MaxSocChargeManager(SimpleNamespace(), ctrl)

    for _ in range(3):
        # Reproduce #350: one completed pack keeps the aggregate maximum only
        # 1 mV inside the taper zone while the inverter briefly refuses charge.
        # It is already below the relaxation threshold, so the provisional
        # 200 W retry opens immediately.
        coord.data.update(battery_power=0, max_cell_voltage=handover_vmax)
        for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
            weekly.tick_bms_cutoff()
        assert weekly.is_battery_full(coord) is False
        assert coord not in ctrl._normal_balance_bms_cutoff_retry_pending
        assert ctrl._normal_balance_bms_cutoff_retry_active[coord] is True

        # This pack accepts charge continuously, proving the refusal was a
        # handover and rearming the same provisional path for the next pack.
        coord.data["battery_power"] = 200
        for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
            weekly.tick_bms_cutoff()
        assert coord not in ctrl._normal_balance_bms_cutoff_retry_active
        assert weekly._bms_cutoff_counts.get(coord.name, 0) == 0

    # The next retry gets no sustained accepted charge. Its confirmed refusal
    # remains final, preserving the anti-infinite-retry safety.
    coord.data.update(battery_power=0, max_cell_voltage=handover_vmax)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        weekly.tick_bms_cutoff()
    assert weekly.is_battery_full(coord) is False
    assert ctrl._normal_balance_bms_cutoff_retry_active[coord] is True
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        weekly.tick_bms_cutoff()
    assert weekly.is_battery_full(coord) is True
    assert coord not in ctrl._normal_balance_bms_cutoff_retry_active


async def test_weekly_completion_queues_post_cutoff_measurement_below_pause_voltage():
    """A weekly v2 cutoff at 3.58 V gets the same settled measurement as vA/vD."""
    coord = _Coord(
        "bat",
        soc=98,
        power=0,
        commanded=0,
        vmax=3.58,
        battery_version="v2",
    )
    coord.enable_charge_hysteresis = False
    ctrl = SimpleNamespace(
        coordinators=[coord],
        _normal_balance_bms_cutoff_measurement={},
        _normal_balance_voltage_tapered={coord: True},
        _normal_balance_last_delta_v={},
        _balance_monitor=None,
        _weekly_charge_status={},
        weekly_full_charge_complete=False,
        _weekly_charge_saved_max_soc={},
        _weekly_charge_needs_restore=False,
    )
    weekly = WeeklyFullChargeManager.__new__(WeeklyFullChargeManager)
    weekly._controller = ctrl
    weekly._bms_cutoff_counts = {
        "bat": _BMS_CUTOFF_REQUIRED_CYCLES,
    }
    weekly._cutoff_applied_names = set()

    async def _restore(_reason):
        return True

    async def _save_state():
        return None

    weekly._restore_hardware_cutoffs = _restore
    weekly.save_state = _save_state

    await weekly._complete_weekly_charge("all_batteries_full")

    assert (
        ctrl._normal_balance_bms_cutoff_measurement[coord]
        == MaxSocChargeManager._BMS_CUTOFF_MEASUREMENT_PENDING
    )
    assert ctrl._normal_balance_last_delta_v == {}
