"""Regression tests for WeeklyFullChargeManager BMS-cutoff detection.

Pins the fix for the "full charge stops at the top voltage" bug: a battery that is merely
idle (≤10 W + Standby in the taper zone but NOT commanded to charge) must not be
mistaken for a real top-of-charge BMS cutoff. Only a battery we actually commanded
to charge yet refuses counts; once confirmed, the latch must survive the charge
exclusion that follows it.

No hardware, no real Home Assistant. ``Store(hass, ...)`` only stores references at
construction, so a SimpleNamespace hass is enough; ``is_active`` is overridden to
isolate the cutoff counter from day/feature gating. The detector runs on a fake
clock (``bms_cutoff_clock`` in conftest), one control cycle per tick.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    DISCHARGE_ENGAGE_GRACE_S,
    NORMAL_BALANCE_TAPER_CELL_VOLTAGE,
)
from custom_components.omnibattery.control.weekly_full_charge import (
    WeeklyFullChargeManager,
    _BMS_CUTOFF_CONFIRM_S,
    _BMS_CUTOFF_REQUIRED_CYCLES,
)
from custom_components.omnibattery.control.max_soc_charge import MaxSocChargeManager

_IN_ZONE = NORMAL_BALANCE_TAPER_CELL_VOLTAGE + 0.05  # cell above taper entry
_STANDBY = 1
_CHARGING = 2
# Meter samples plus the 2s safety tick: about 1.5 control cycles a second.
_FAST_CYCLE_S = 2 / 3


@pytest.fixture(autouse=True)
def clock(bms_cutoff_clock):
    return bms_cutoff_clock


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
    # A commanded battery has been charging since long before the test starts.
    m._bms_charge_commanded_at = {coord.name: 0.0}
    m._already_complete_logged = False
    m.is_active = lambda: True  # weekly active; bypass day/feature gating
    return m


def _run_for(m, clock, seconds):
    """Tick the detector for ``seconds`` at the clock's step."""
    for _ in range(round(seconds / clock.step)):
        m.tick_bms_cutoff()


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
    """A battery taking the charge it was offered for N cycles is not full → reset."""
    coord = _Coord("bat", soc=94, power=0, commanded=200)
    m = _mgr(coord)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES - 1):
        m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] == _BMS_CUTOFF_REQUIRED_CYCLES - 1
    # Now it accepts charge; one sample is not enough to reset.
    coord.data["battery_power"] = 150
    m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] == _BMS_CUTOFF_REQUIRED_CYCLES - 1
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES - 1):
        m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] == 0
    assert m.is_battery_full(coord) is False


def test_flapping_bms_confirms_cutoff():
    """v3 at 88% with every cell full flaps Charge ↔ Standby every few seconds.
    Brief acceptances must not erase the refusals, or the cutoff never confirms."""
    coord = _Coord("bat", soc=88, power=0, commanded=200)
    m = _mgr(coord)
    m.is_active = lambda: False
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        coord.data.update(battery_power=0, inverter_state=_STANDBY)
        m.tick_bms_cutoff()
        coord.data.update(battery_power=150, inverter_state=2)
        m.tick_bms_cutoff()
    assert m.is_battery_full(coord) is True


def test_restart_from_standby_is_not_judged_within_the_engage_grace(clock):
    """A battery restarted from standby at 99% still reads 0 W + Standby while it
    engages, and its readings can predate the command. No refusal counts before
    the engage grace has passed."""
    clock.step = _FAST_CYCLE_S
    coord = _Coord("bat", soc=99, power=0, commanded=0)
    m = _mgr(coord)
    _run_for(m, clock, 60)  # idle in standby, not asked to charge

    coord.commanded_charge_power = 800
    m.tick_bms_cutoff()
    coord.commanded_charge_power = 1600
    _run_for(m, clock, DISCHARGE_ENGAGE_GRACE_S - 2)
    assert m._bms_cutoff_counts.get("bat", 0) == 0
    assert m.is_battery_full(coord) is False

    # It starts charging: never a cutoff.
    coord.data.update(battery_power=1200, inverter_state=_CHARGING)
    _run_for(m, clock, 60)
    assert m.is_battery_full(coord) is False


def test_battery_that_never_starts_is_confirmed_after_the_grace(clock):
    """A battery asked to charge that keeps refusing is confirmed once the engage
    grace and the confirmation window have passed."""
    clock.step = _FAST_CYCLE_S
    coord = _Coord("bat", soc=99, power=0, commanded=0)
    m = _mgr(coord)
    m.tick_bms_cutoff()

    coord.commanded_charge_power = 1600
    _run_for(m, clock, DISCHARGE_ENGAGE_GRACE_S + _BMS_CUTOFF_CONFIRM_S - 2)
    assert m.is_battery_full(coord) is False
    _run_for(m, clock, 4)
    assert m.is_battery_full(coord) is True


def test_cutoff_of_a_charging_battery_is_confirmed_on_time_not_cycles(clock):
    """The control cycle also runs on the 2s safety tick, so the required number
    of cycles can pass in about 3s. A battery that was charging and stops is
    confirmed once the refusal has lasted the confirmation window."""
    clock.step = _FAST_CYCLE_S
    coord = _Coord("bat", soc=99, power=900, commanded=1600, inv=_CHARGING)
    m = _mgr(coord)
    _run_for(m, clock, 60)

    coord.data.update(battery_power=0, inverter_state=_STANDBY)
    for _ in range(_BMS_CUTOFF_REQUIRED_CYCLES):
        m.tick_bms_cutoff()
    assert m.is_battery_full(coord) is False
    _run_for(m, clock, _BMS_CUTOFF_CONFIRM_S)
    assert m.is_battery_full(coord) is True


def test_engage_grace_follows_the_driver_capability(clock):
    """A driver that reports a longer engage grace gets all of it."""
    coord = _Coord("bat", soc=99, power=0, commanded=0)
    coord.capabilities = SimpleNamespace(engage_grace_s=65.0)
    m = _mgr(coord)
    m.tick_bms_cutoff()

    coord.commanded_charge_power = 1600
    _run_for(m, clock, 60)
    assert m._bms_cutoff_counts.get("bat", 0) == 0
    _run_for(m, clock, 20)
    assert m.is_battery_full(coord) is True


def test_a_new_charge_command_gets_a_new_engage_grace(clock):
    """Dropping the charge command freezes the count; the next command starts a
    new engage grace in which the count stays frozen too."""
    coord = _Coord("bat", soc=99, power=0, commanded=1600)
    m = _mgr(coord)
    m.tick_bms_cutoff()
    m.tick_bms_cutoff()
    assert m._bms_cutoff_counts["bat"] == 2

    coord.commanded_charge_power = 0
    _run_for(m, clock, 300)
    assert m._bms_cutoff_counts["bat"] == 2

    coord.commanded_charge_power = 800
    _run_for(m, clock, DISCHARGE_ENGAGE_GRACE_S - 5)
    assert m._bms_cutoff_counts["bat"] == 2
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
    weekly._bms_charge_commanded_at = {"bat": 0.0}
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
    weekly._bms_charge_commanded_at = {"bat": 0.0}
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
