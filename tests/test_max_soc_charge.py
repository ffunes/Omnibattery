"""Characterization tests for MaxSocChargeManager (top-of-charge management).

These pin the *current* behavior of the cluster extracted from
``ChargeDischargeController`` (the old ``_normal_balance_*`` methods) so the move
to ``max_soc_charge.py`` is proven cero-cambio-funcional. Despite the legacy
attribute names this is NOT active cell balancing — it manages the final stretch
of a normal 100% charge: power taper, charge pause/hysteresis at the top, SOC
recalibration on coulomb drift, and passive cell-delta measurement.

No hardware, no real Home Assistant. ``MaxSocChargeManager.__init__`` only stores
``hass``/``controller`` references, so it is built directly with a SimpleNamespace
hass and a stub controller. The latched state lives on the controller (the
manager reads/writes it via ``self._controller``), matching the production wiring
where switch.py / weekly_full_charge.py also touch those dicts.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.marstek_venus_energy_manager.const import (
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    NORMAL_BALANCE_CHARGE_POWER_W,
    NORMAL_BALANCE_RECAL_CUTOFF_CYCLES,
)
from custom_components.marstek_venus_energy_manager.max_soc_charge import (
    MaxSocChargeManager,
)


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------

class _Coord:
    """Coordinator stand-in. Identity-hashable (used as dict keys), unlike
    SimpleNamespace which defines __eq__ and is therefore unhashable."""

    def __init__(
        self,
        name="bat",
        *,
        data=None,
        max_soc=100,
        taper_enabled=True,
        active_balance_mode_enabled=False,
        max_charge_power=800,
    ):
        self.name = name
        self.data = {} if data is None else data
        self.max_soc = max_soc
        self.max_charge_power = max_charge_power
        self.active_balance_mode_enabled = active_balance_mode_enabled
        setattr(self, CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, taper_enabled)


def _controller(coords, **overrides):
    """Stub controller exposing only the state dicts/collaborators the manager
    reads. ``_blocks`` records charge-block sources per coordinator so tests can
    assert the ``normal_balance_pause`` block was set/removed."""
    blocks: dict = {}

    def _set_block(source, reason, details=None, coordinator=None):
        blocks.setdefault(coordinator, set()).add(source)

    def _remove_block(source, coordinator=None):
        blocks.get(coordinator, set()).discard(source)

    base = dict(
        coordinators=list(coords),
        _normal_balance_date=dt_util.now().date(),
        _normal_balance_charge_paused={},
        _normal_balance_voltage_tapered={},
        _normal_active_balance_phases={},
        _normal_balance_measure_started={},
        _normal_balance_last_delta_v={},
        _normal_balance_top_voltage_seen={},
        _normal_balance_pause_latch_soc={},
        _normal_balance_recal_override={},
        _normal_balance_recal_cutoff_count={},
        _normal_balance_recal_latched={},
        set_charge_block=_set_block,
        remove_charge_block=_remove_block,
        _is_active_balance_mode_running=lambda coordinator: False,
        _weekly_charge_mgr=object(),
        _weekly_full_charge_unlocked=lambda: False,
        _battery_power_limit=lambda coordinator, is_charging: coordinator.max_charge_power,
        _balance_monitor=None,
        _reset_active_balance_charge_resume_target=lambda coordinator: None,
    )
    base.update(overrides)
    ctrl = SimpleNamespace(**base)
    ctrl._blocks = blocks
    return ctrl


def _mgr(ctrl):
    return MaxSocChargeManager(SimpleNamespace(), ctrl)


def _paused(ctrl, coord):
    return "normal_balance_pause" in ctrl._blocks.get(coord, set())


# ----------------------------------------------------------------------
# _taper_enabled / _taper_applies
# ----------------------------------------------------------------------

def test_taper_enabled_reads_coordinator_flag():
    assert MaxSocChargeManager._taper_enabled(_Coord(taper_enabled=True)) is True
    assert MaxSocChargeManager._taper_enabled(_Coord(taper_enabled=False)) is False


def test_taper_enabled_defaults_true_when_attr_missing():
    bare = SimpleNamespace()  # no taper attr -> DEFAULT (True)
    assert MaxSocChargeManager._taper_enabled(bare) is True


def test_taper_applies_true_at_max_soc_100():
    c = _Coord(max_soc=100)
    assert _mgr(_controller([c]))._taper_applies(c) is True


def test_taper_applies_false_when_taper_disabled():
    c = _Coord(max_soc=100, taper_enabled=False)
    assert _mgr(_controller([c]))._taper_applies(c) is False


def test_taper_applies_false_when_active_balance_running():
    c = _Coord(max_soc=100)
    ctrl = _controller([c], _is_active_balance_mode_running=lambda coordinator: True)
    assert _mgr(ctrl)._taper_applies(c) is False


def test_taper_applies_false_when_coordinator_active_balance_enabled():
    c = _Coord(max_soc=100, active_balance_mode_enabled=True)
    assert _mgr(_controller([c]))._taper_applies(c) is False


def test_taper_applies_true_below_100_when_weekly_unlocked():
    c = _Coord(max_soc=80)
    ctrl = _controller([c], _weekly_full_charge_unlocked=lambda: True)
    assert _mgr(ctrl)._taper_applies(c) is True


def test_taper_applies_true_below_100_without_weekly_unlock():
    # #394: taper now engages purely on the option being enabled, regardless of
    # max_soc or weekly (scenario 4: taper ON, no weekly, max_soc < 100).
    c = _Coord(max_soc=80)
    assert _mgr(_controller([c]))._taper_applies(c) is True


# ----------------------------------------------------------------------
# _zone_active
# ----------------------------------------------------------------------

def test_zone_active_true_at_taper_voltage():
    c = _Coord(data={"max_cell_voltage": 3.50})  # >= 3.48 taper voltage
    assert _mgr(_controller([c]))._zone_active(c) is True


def test_zone_active_false_below_taper_voltage():
    c = _Coord(data={"max_cell_voltage": 3.40})
    assert _mgr(_controller([c]))._zone_active(c) is False


# ----------------------------------------------------------------------
# apply_charge_taper
# ----------------------------------------------------------------------

def test_apply_charge_taper_unchanged_when_not_applicable():
    c = _Coord(taper_enabled=False, data={"max_cell_voltage": 3.60})  # taper disabled
    assert _mgr(_controller([c])).apply_charge_taper(c, 800) == 800


def test_apply_charge_taper_caps_and_latches_at_taper_voltage():
    c = _Coord(data={"max_cell_voltage": 3.50})
    ctrl = _controller([c])
    m = _mgr(ctrl)
    assert m.apply_charge_taper(c, 800) == NORMAL_BALANCE_CHARGE_POWER_W
    assert ctrl._normal_balance_voltage_tapered.get(c) is True
    # Idempotent: still capped on a second call at the same voltage.
    assert m.apply_charge_taper(c, 800) == NORMAL_BALANCE_CHARGE_POWER_W


def test_apply_charge_taper_unlatches_when_dropping_out_of_zone():
    c = _Coord(data={"max_cell_voltage": 3.50})
    ctrl = _controller([c])
    m = _mgr(ctrl)
    m.apply_charge_taper(c, 800)  # latch
    c.data["max_cell_voltage"] = 3.40  # below taper zone
    assert m.apply_charge_taper(c, 800) == 800
    assert c not in ctrl._normal_balance_voltage_tapered


# ----------------------------------------------------------------------
# reset_if_new_day
# ----------------------------------------------------------------------

def test_reset_if_new_day_noop_same_day():
    c = _Coord()
    ctrl = _controller([c])
    ctrl._normal_balance_charge_paused[c] = True
    _mgr(ctrl).reset_if_new_day()
    assert ctrl._normal_balance_charge_paused == {c: True}


def test_reset_if_new_day_clears_state_and_blocks_on_rollover():
    c = _Coord()
    ctrl = _controller([c])
    ctrl._normal_balance_date = dt_util.now().date() - timedelta(days=1)
    ctrl._normal_balance_charge_paused[c] = True
    ctrl._normal_balance_pause_latch_soc[c] = 95.0
    ctrl._blocks.setdefault(c, set()).add("normal_balance_pause")

    _mgr(ctrl).reset_if_new_day()

    assert ctrl._normal_balance_date == dt_util.now().date()
    assert ctrl._normal_balance_charge_paused == {}
    assert ctrl._normal_balance_pause_latch_soc == {}
    assert not _paused(ctrl, c)


# ----------------------------------------------------------------------
# refresh_blocks
# ----------------------------------------------------------------------

def test_refresh_blocks_clears_when_taper_not_applicable():
    c = _Coord(taper_enabled=False, data={"max_cell_voltage": 3.60})
    ctrl = _controller([c])
    ctrl._normal_balance_charge_paused[c] = True
    ctrl._blocks.setdefault(c, set()).add("normal_balance_pause")

    _mgr(ctrl).refresh_blocks()

    assert c not in ctrl._normal_balance_charge_paused
    assert not _paused(ctrl, c)


def test_refresh_blocks_latches_pause_at_top_voltage():
    # SOC at 100 so the recal override does not keep charging (a low SOC at the
    # top voltage would instead trigger recalibration and skip the pause).
    c = _Coord(data={"max_cell_voltage": 3.60, "battery_soc": 100})
    ctrl = _controller([c])

    _mgr(ctrl).refresh_blocks()

    assert _paused(ctrl, c)
    assert ctrl._normal_balance_pause_latch_soc[c] == 100.0
    assert ctrl._normal_balance_top_voltage_seen.get(c) is True
    assert ctrl._normal_balance_charge_paused.get(c) is True


def test_refresh_blocks_pause_holds_until_soc_drop_margin():
    c = _Coord(data={"max_cell_voltage": 3.60, "battery_soc": 100})
    ctrl = _controller([c])
    m = _mgr(ctrl)
    m.refresh_blocks()  # latch at soc 100

    # Cell relaxed below the pause voltage but still in the taper zone; SOC has
    # only dropped 1% (resume margin is 3%) -> still paused.
    c.data.update(max_cell_voltage=3.50, battery_soc=99)
    m.refresh_blocks()
    assert _paused(ctrl, c)
    assert ctrl._normal_balance_pause_latch_soc[c] == 100.0


def test_refresh_blocks_pause_releases_after_soc_drop_margin():
    c = _Coord(data={"max_cell_voltage": 3.60, "battery_soc": 95})
    ctrl = _controller([c])
    m = _mgr(ctrl)
    m.refresh_blocks()  # latch at soc 95

    c.data.update(max_cell_voltage=3.50, battery_soc=91)  # 95 - 91 = 4 >= 3
    m.refresh_blocks()
    assert not _paused(ctrl, c)
    assert c not in ctrl._normal_balance_pause_latch_soc


def test_refresh_blocks_latches_on_bms_cutoff_signature_below_pause_voltage():
    # vmax below the 3.58 pause voltage but in the taper zone, charge collapsed
    # to <=10 W with the inverter in standby (raw state 1) -> BMS cut signature.
    c = _Coord(data={
        "max_cell_voltage": 3.50,
        "battery_soc": 100,
        "battery_power": 5,
        "inverter_state": 1,
    })
    ctrl = _controller([c])

    _mgr(ctrl).refresh_blocks()

    assert _paused(ctrl, c)
    assert ctrl._normal_balance_top_voltage_seen.get(c) is True


# ----------------------------------------------------------------------
# _compute_recal_override
# ----------------------------------------------------------------------

def _recal_coord(power=5, inv=1):
    return _Coord(data={"battery_power": power, "inverter_state": inv})


def test_recal_override_false_when_soc_at_threshold():
    c = _recal_coord()
    ctrl = _controller([c])
    ctrl._normal_balance_recal_cutoff_count[c] = 2
    assert _mgr(ctrl)._compute_recal_override(c, 3.55, 99) is False
    assert c not in ctrl._normal_balance_recal_cutoff_count  # counter cleared


def test_recal_override_true_keeps_charging_on_low_soc():
    c = _recal_coord(power=300, inv=0)  # actively charging, not a cutoff
    ctrl = _controller([c])
    assert _mgr(ctrl)._compute_recal_override(c, 3.55, 95) is True


def test_recal_override_latches_after_cutoff_cycles():
    c = _recal_coord(power=5, inv=1)  # cutoff signature every cycle
    ctrl = _controller([c])
    m = _mgr(ctrl)
    for _ in range(NORMAL_BALANCE_RECAL_CUTOFF_CYCLES - 1):
        assert m._compute_recal_override(c, 3.55, 95) is True
    # Nth consecutive cutoff latches recal and stops the override.
    assert m._compute_recal_override(c, 3.55, 95) is False
    assert ctrl._normal_balance_recal_latched.get(c) is True
    # Stays latched on subsequent calls.
    assert m._compute_recal_override(c, 3.55, 95) is False


def test_recal_override_cutoff_counter_resets_when_charge_resumes():
    c = _recal_coord(power=5, inv=1)
    ctrl = _controller([c])
    m = _mgr(ctrl)
    m._compute_recal_override(c, 3.55, 95)  # count -> 1
    c.data.update(battery_power=300, inverter_state=0)  # charge resumed
    assert m._compute_recal_override(c, 3.55, 95) is True
    assert c not in ctrl._normal_balance_recal_cutoff_count


# ----------------------------------------------------------------------
# get_status
# ----------------------------------------------------------------------

def test_get_status_reports_per_battery_diagnostics():
    c = _Coord(data={"max_cell_voltage": 3.60, "min_cell_voltage": 3.55,
                     "battery_soc": 95})
    empty = _Coord(name="empty", data={})  # skipped: no data
    ctrl = _controller([c, empty])
    ctrl._normal_balance_charge_paused[c] = True

    status = _mgr(ctrl).get_status()

    assert "empty" not in status
    s = status["bat"]
    assert s["enabled"] is True
    assert s["in_zone"] is True
    assert s["paused"] is True
    assert s["delta_V"] == 0.05
    assert s["charge_limit_w"] == 800


# ----------------------------------------------------------------------
# handle_measurement
# ----------------------------------------------------------------------

async def test_handle_measurement_enters_hold_and_takes_over():
    c = _Coord(data={"max_cell_voltage": 3.60, "min_cell_voltage": 3.55,
                     "battery_soc": 100})
    calls = []

    async def _set(coordinator, charge, discharge, **kw):
        calls.append((coordinator.name, charge, discharge, kw))

    ctrl = _controller([c], _set_battery_power=_set)

    took_over = await _mgr(ctrl).handle_measurement()

    assert took_over is True
    assert ctrl._normal_active_balance_phases[c] == "WAIT_MEASURE"
    assert len(calls) == 1
    name, charge, discharge, kw = calls[0]
    assert (charge, discharge) == (0, 0)
    assert "normal_balance_pause" in kw["ignore_charge_blockers"]


async def test_handle_measurement_records_delta_after_wait():
    c = _Coord(data={"max_cell_voltage": 3.60, "min_cell_voltage": 3.55,
                     "battery_soc": 100})

    class _Monitor:
        def __init__(self):
            self.calls = []

        async def async_record_top_balance_measurement(
            self, coordinator, vmax, vmin, soc, phase
        ):
            self.calls.append((coordinator.name, vmax, vmin, soc, phase))

    monitor = _Monitor()
    ctrl = _controller(
        [c],
        _set_battery_power=lambda *a, **k: _noop(),
        _balance_monitor=monitor,
    )
    # Pre-seed an in-flight measurement whose wait window has already elapsed.
    ctrl._normal_active_balance_phases[c] = "WAIT_MEASURE"
    ctrl._normal_balance_measure_started[c] = dt_util.utcnow() - timedelta(seconds=61)

    await _mgr(ctrl).handle_measurement()

    assert ctrl._normal_balance_last_delta_v[c] == 0.05
    assert ctrl._normal_active_balance_phases[c] == "MEASURED"
    assert len(monitor.calls) == 1
    assert monitor.calls[0][4] == "top_charge_3_55v"


async def test_handle_measurement_skips_during_soc_recalibration():
    c = _Coord(data={"max_cell_voltage": 3.60, "min_cell_voltage": 3.55,
                     "battery_soc": 100})
    calls = []

    async def _set(coordinator, charge, discharge, **kw):
        calls.append(coordinator.name)

    ctrl = _controller([c], _set_battery_power=_set)
    ctrl._normal_balance_recal_override[c] = True  # recal in progress

    took_over = await _mgr(ctrl).handle_measurement()

    assert took_over is False
    assert calls == []
    assert c not in ctrl._normal_active_balance_phases


async def _noop():
    return None
