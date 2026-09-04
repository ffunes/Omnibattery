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

from custom_components.omnibattery.const import (
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    NORMAL_BALANCE_CHARGE_POWER_W,
    NORMAL_BALANCE_PAUSE_CELL_VOLTAGE,
    NORMAL_BALANCE_RECAL_CUTOFF_CYCLES,
    NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE,
)
from custom_components.omnibattery.control.max_soc_charge import (
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
        battery_version="v2",
        max_soc=100,
        taper_enabled=True,
        max_charge_power=800,
        commanded_charge_power=0,
    ):
        self.name = name
        self.data = {} if data is None else data
        self.battery_version = battery_version
        self.max_soc = max_soc
        self.max_charge_power = max_charge_power
        self.commanded_charge_power = commanded_charge_power
        setattr(self, CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED, taper_enabled)


def _controller(coords, **overrides):
    """Stub controller exposing only the state dicts/collaborators the manager
    reads. ``_blocks`` records charge-block sources per coordinator so tests can
tracks the manager's runtime state."""
    base = dict(
        coordinators=list(coords),
        _normal_balance_date=dt_util.now().date(),
        _normal_balance_voltage_tapered={},
        _normal_balance_bms_cutoff_active={},
        _normal_balance_bms_cutoff_retry_pending={},
        _normal_balance_bms_cutoff_retry_active={},
        _normal_balance_bms_cutoff_retry_accept_count={},
        _normal_balance_bms_cutoff_measurement={},
        _normal_balance_phases={},
        _normal_balance_measure_started={},
        _normal_balance_last_delta_v={},
        _normal_balance_recal_override={},
        _normal_balance_recal_cutoff_count={},
        _normal_balance_recal_latched={},
        _normal_balance_recal_retry_pending={},
        _normal_balance_recal_retry_active={},
        _normal_balance_recal_first_cutoff_voltage={},
        _weekly_charge_mgr=object(),
        _weekly_full_charge_unlocked=lambda: False,
        _battery_power_limit=lambda coordinator, is_charging: coordinator.max_charge_power,
        _balance_monitor=None,
    )
    base.update(overrides)
    ctrl = SimpleNamespace(**base)
    return ctrl


def _mgr(ctrl):
    return MaxSocChargeManager(SimpleNamespace(), ctrl)


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
    c.data["max_cell_voltage"] = 3.40  # below exit threshold (3.44 V)
    assert m.apply_charge_taper(c, 800) == 800
    assert c not in ctrl._normal_balance_voltage_tapered


def test_apply_charge_taper_stays_latched_in_hysteresis_band():
    # Cell relaxes to 3.46 V (below 3.48 entry but above 3.44 exit) — taper must hold.
    c = _Coord(data={"max_cell_voltage": 3.50})
    ctrl = _controller([c])
    m = _mgr(ctrl)
    m.apply_charge_taper(c, 800)  # latch at 3.50 V
    c.data["max_cell_voltage"] = 3.46  # in hysteresis band
    assert m.apply_charge_taper(c, 800) == NORMAL_BALANCE_CHARGE_POWER_W
    assert ctrl._normal_balance_voltage_tapered.get(c) is True


def test_venus_ad_keeps_the_200_w_taper_at_the_top_voltage():
    for version in ("vA", "vD"):
        c = _Coord(
            battery_version=version,
            data={"max_cell_voltage": NORMAL_BALANCE_PAUSE_CELL_VOLTAGE},
        )
        assert _mgr(_controller([c])).apply_charge_taper(c, 800) == NORMAL_BALANCE_CHARGE_POWER_W


# ----------------------------------------------------------------------
# reset_if_new_day
# ----------------------------------------------------------------------

def test_reset_if_new_day_noop_same_day():
    c = _Coord()
    ctrl = _controller([c])
    ctrl._normal_balance_voltage_tapered[c] = True
    _mgr(ctrl).reset_if_new_day()
    assert ctrl._normal_balance_voltage_tapered == {c: True}


def test_reset_if_new_day_clears_taper_state_on_rollover():
    c = _Coord()
    ctrl = _controller([c])
    ctrl._normal_balance_date = dt_util.now().date() - timedelta(days=1)
    ctrl._normal_balance_voltage_tapered[c] = True

    _mgr(ctrl).reset_if_new_day()

    assert ctrl._normal_balance_date == dt_util.now().date()
    assert ctrl._normal_balance_voltage_tapered == {}


def test_reset_if_new_day_preserves_unfinished_venus_ad_bms_charge():
    c = _Coord(battery_version="vA")
    ctrl = _controller([c])
    ctrl._normal_balance_date = dt_util.now().date() - timedelta(days=1)
    ctrl._normal_balance_bms_cutoff_active[c] = True

    _mgr(ctrl).reset_if_new_day()

    assert ctrl._normal_balance_bms_cutoff_active[c] is True


# ----------------------------------------------------------------------
# refresh_blocks
# ----------------------------------------------------------------------

def test_refresh_blocks_clears_taper_state_when_not_applicable():
    c = _Coord(taper_enabled=False, data={"max_cell_voltage": 3.60})
    ctrl = _controller([c])
    ctrl._normal_balance_voltage_tapered[c] = True

    _mgr(ctrl).refresh_blocks()

    assert c not in ctrl._normal_balance_voltage_tapered


def test_refresh_blocks_does_not_add_a_second_top_charge_block():
    c = _Coord(data={"max_cell_voltage": 3.60, "battery_soc": 100})
    ctrl = _controller([c])

    _mgr(ctrl).refresh_blocks()

    assert ctrl._normal_balance_recal_override[c] is False


def test_refresh_blocks_starts_recalibration_at_top_voltage_on_low_soc():
    c = _Coord(
        data={
            "max_cell_voltage": 3.60,
            "battery_soc": 95,
            "battery_power": 200,
            "inverter_state": 0,
        },
        commanded_charge_power=200,
    )
    ctrl = _controller([c])

    _mgr(ctrl).refresh_blocks()

    assert ctrl._normal_balance_recal_override[c] is True


def test_refresh_blocks_queues_measurement_when_bms_cuts_below_pause_voltage():
    c = _Coord(
        data={
            "max_cell_voltage": 3.58,
            "min_cell_voltage": 3.54,
            "battery_soc": 99,
            "battery_power": 0,
            "inverter_state": 1,
        },
        commanded_charge_power=200,
    )
    ctrl = _controller(
        [c],
        _weekly_charge_mgr=SimpleNamespace(
            is_bms_cutoff_confirmed=lambda _coordinator: True,
        ),
    )

    _mgr(ctrl).refresh_blocks()

    assert (
        ctrl._normal_balance_bms_cutoff_measurement[c]
        == MaxSocChargeManager._BMS_CUTOFF_MEASUREMENT_PENDING
    )


def test_venus_ad_latches_bms_owned_charge_past_voltage_relaxation():
    c = _Coord(
        battery_version="vA",
        data={"max_cell_voltage": NORMAL_BALANCE_PAUSE_CELL_VOLTAGE, "battery_soc": 100},
    )
    ctrl = _controller(
        [c],
        _weekly_charge_mgr=SimpleNamespace(
            is_bms_cutoff_confirmed=lambda _coordinator: False,
        ),
    )
    manager = _mgr(ctrl)

    manager.refresh_blocks()
    assert ctrl._normal_balance_bms_cutoff_active[c] is True
    assert manager.should_charge_to_bms_cutoff(c, 100) is True

    # The cell can relax below 3.60 V while the other coupled packs continue
    # filling; the BMS-owned latch must survive that relaxation.
    c.data["max_cell_voltage"] = 3.57
    manager.refresh_blocks()
    assert ctrl._normal_balance_bms_cutoff_active[c] is True
    assert manager.should_charge_to_bms_cutoff(c, 100) is True


def test_venus_ad_bms_confirmation_releases_top_charge_latch():
    c = _Coord(
        battery_version="vD",
        data={"max_cell_voltage": NORMAL_BALANCE_PAUSE_CELL_VOLTAGE, "battery_soc": 98},
    )
    cutoff_confirmed = {"value": False}
    ctrl = _controller(
        [c],
        _weekly_charge_mgr=SimpleNamespace(
            is_bms_cutoff_confirmed=lambda _coordinator: cutoff_confirmed["value"],
            reset_bms_cutoff_confirmation=lambda _coordinator: cutoff_confirmed.__setitem__(
                "value", False
            ),
        ),
    )
    manager = _mgr(ctrl)

    manager.refresh_blocks()
    assert ctrl._normal_balance_bms_cutoff_active[c] is True

    cutoff_confirmed["value"] = True
    manager.refresh_blocks()
    assert ctrl._normal_balance_bms_cutoff_retry_pending[c] is True
    assert c not in ctrl._normal_balance_bms_cutoff_measurement

    c.data["max_cell_voltage"] = NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE
    manager.refresh_blocks()
    assert c not in ctrl._normal_balance_bms_cutoff_retry_pending
    assert ctrl._normal_balance_bms_cutoff_retry_active[c] is True
    assert manager.should_charge_to_bms_cutoff(c, 100) is True

    cutoff_confirmed["value"] = True
    manager.refresh_blocks()
    assert c not in ctrl._normal_balance_bms_cutoff_retry_active
    assert (
        ctrl._normal_balance_bms_cutoff_measurement[c]
        == manager._BMS_CUTOFF_MEASUREMENT_PENDING
    )
    assert manager.should_charge_to_bms_cutoff(c, 100) is False


def test_venus_ad_first_cutoff_waits_for_relaxation_then_opens_one_retry():
    c = _Coord(
        battery_version="vA",
        data={
            "max_cell_voltage": NORMAL_BALANCE_PAUSE_CELL_VOLTAGE,
            "battery_soc": 94,
            "battery_power": 0,
        },
        commanded_charge_power=200,
    )
    confirmed = {"value": True}
    reset_calls = []
    ctrl = _controller(
        [c],
        _weekly_charge_mgr=SimpleNamespace(
            is_bms_cutoff_confirmed=lambda _coordinator: confirmed["value"],
            reset_bms_cutoff_confirmation=lambda coordinator: (
                reset_calls.append(coordinator), confirmed.__setitem__("value", False)
            ),
        ),
    )
    manager = _mgr(ctrl)

    assert manager.prepare_bms_cutoff_retry(c) == manager._BMS_CUTOFF_RETRY_PENDING
    assert ctrl._normal_balance_bms_cutoff_retry_pending[c] is True
    assert manager.should_charge_to_bms_cutoff(c, 100) is False

    c.data["max_cell_voltage"] = NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE
    assert manager.prepare_bms_cutoff_retry(c) == manager._BMS_CUTOFF_RETRY_ACTIVE
    assert c not in ctrl._normal_balance_bms_cutoff_retry_pending
    assert ctrl._normal_balance_bms_cutoff_retry_active[c] is True
    assert reset_calls == [c]
    assert manager.should_charge_to_bms_cutoff(c, 100) is True


def test_venus_ad_retry_requires_a_second_confirmed_cutoff():
    c = _Coord(
        battery_version="vD",
        data={
            "max_cell_voltage": NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE,
            "battery_soc": 94,
            "battery_power": 200,
        },
        commanded_charge_power=200,
    )
    confirmed = {"value": False}
    ctrl = _controller(
        [c],
        _weekly_charge_mgr=SimpleNamespace(
            is_bms_cutoff_confirmed=lambda _coordinator: confirmed["value"],
            reset_bms_cutoff_confirmation=lambda _coordinator: None,
        ),
    )
    ctrl._normal_balance_bms_cutoff_retry_active[c] = True
    manager = _mgr(ctrl)

    assert manager.prepare_bms_cutoff_retry(c) == manager._BMS_CUTOFF_RETRY_ACTIVE

    confirmed["value"] = True
    c.data["battery_power"] = 0
    assert manager.prepare_bms_cutoff_retry(c) is None
    assert c not in ctrl._normal_balance_bms_cutoff_retry_active
    assert manager.should_charge_to_bms_cutoff(c, 100) is False


def test_venus_ad_retry_rearms_only_after_sustained_accepted_charge():
    c = _Coord(
        battery_version="vD",
        data={
            "max_cell_voltage": NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE,
            "battery_soc": 94,
            "battery_power": 200,
        },
        commanded_charge_power=200,
    )
    confirmed = {"value": False}
    reset_calls = []
    ctrl = _controller(
        [c],
        _weekly_charge_mgr=SimpleNamespace(
            is_bms_cutoff_confirmed=lambda _coordinator: confirmed["value"],
            reset_bms_cutoff_confirmation=lambda coordinator: (
                reset_calls.append(coordinator), confirmed.__setitem__("value", False)
            ),
        ),
    )
    ctrl._normal_balance_bms_cutoff_retry_active[c] = True
    manager = _mgr(ctrl)

    # Query methods may run multiple times per tick; they must not advance the
    # acceptance debounce.
    for _ in range(NORMAL_BALANCE_RECAL_CUTOFF_CYCLES * 2):
        assert manager.prepare_bms_cutoff_retry(c) == manager._BMS_CUTOFF_RETRY_ACTIVE
    assert ctrl._normal_balance_bms_cutoff_retry_accept_count == {}

    for _ in range(NORMAL_BALANCE_RECAL_CUTOFF_CYCLES - 1):
        manager.tick_bms_cutoff_retry_acceptance()
    assert ctrl._normal_balance_bms_cutoff_retry_active[c] is True

    manager.tick_bms_cutoff_retry_acceptance()
    assert c not in ctrl._normal_balance_bms_cutoff_retry_active
    assert ctrl._normal_balance_bms_cutoff_retry_accept_count == {}
    assert reset_calls == [c]

    # The next confirmed refusal is now provisional again, ready for another
    # coupled-pack handover instead of being treated as the final cutoff.
    confirmed["value"] = True
    c.data.update(battery_power=0, max_cell_voltage=NORMAL_BALANCE_PAUSE_CELL_VOLTAGE)
    assert manager.prepare_bms_cutoff_retry(c) == manager._BMS_CUTOFF_RETRY_PENDING


def test_venus_ad_brief_retry_acceptance_does_not_rearm_final_cutoff():
    c = _Coord(
        battery_version="vA",
        data={
            "max_cell_voltage": NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE,
            "battery_soc": 94,
            "battery_power": 200,
        },
        commanded_charge_power=200,
    )
    confirmed = {"value": False}
    ctrl = _controller(
        [c],
        _weekly_charge_mgr=SimpleNamespace(
            is_bms_cutoff_confirmed=lambda _coordinator: confirmed["value"],
            reset_bms_cutoff_confirmation=lambda _coordinator: None,
        ),
    )
    ctrl._normal_balance_bms_cutoff_retry_active[c] = True
    manager = _mgr(ctrl)

    for _ in range(NORMAL_BALANCE_RECAL_CUTOFF_CYCLES - 1):
        manager.tick_bms_cutoff_retry_acceptance()
    assert ctrl._normal_balance_bms_cutoff_retry_active[c] is True

    # A renewed refusal clears the partial acceptance streak. Once the shared
    # cutoff detector confirms it, the original retry is final.
    c.data["battery_power"] = 0
    manager.tick_bms_cutoff_retry_acceptance()
    confirmed["value"] = True
    assert manager.prepare_bms_cutoff_retry(c) is None
    assert c not in ctrl._normal_balance_bms_cutoff_retry_active


# ----------------------------------------------------------------------
# _compute_recal_override
# ----------------------------------------------------------------------

def _recal_coord(power=5, inv=1, commanded=200):
    return _Coord(
        data={"battery_power": power, "inverter_state": inv},
        commanded_charge_power=commanded,
    )


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


def test_high_voltage_cutoff_arms_one_retry_after_relaxation():
    c = _recal_coord(power=5, inv=1, commanded=200)
    ctrl = _controller([c])
    m = _mgr(ctrl)

    for cycle in range(NORMAL_BALANCE_RECAL_CUTOFF_CYCLES):
        result = m._compute_recal_override(c, 3.64, 95)
        if cycle < NORMAL_BALANCE_RECAL_CUTOFF_CYCLES - 1:
            assert result is True
        else:
            assert result is False

    assert ctrl._normal_balance_recal_latched[c] is True
    assert ctrl._normal_balance_recal_retry_pending[c] is True
    assert ctrl._normal_balance_recal_retry_active.get(c, False) is False

    # The battery is idle while the top cell relaxes; the retry must not start early.
    c.commanded_charge_power = 0
    assert m._compute_recal_override(c, 3.59, 95) is False
    assert ctrl._normal_balance_recal_retry_pending[c] is True

    # At 3.57 V the one-shot 200 W retry becomes active.
    assert m._compute_recal_override(c, NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE, 95) is True
    assert ctrl._normal_balance_recal_retry_pending.get(c, False) is False
    assert ctrl._normal_balance_recal_retry_active[c] is True


def test_recal_retry_stops_after_its_second_bms_cutoff():
    c = _recal_coord(power=5, inv=1, commanded=200)
    ctrl = _controller([c])
    m = _mgr(ctrl)

    for _ in range(NORMAL_BALANCE_RECAL_CUTOFF_CYCLES):
        m._compute_recal_override(c, 3.64, 95)

    c.commanded_charge_power = 0  # waiting for relaxation
    assert m._compute_recal_override(c, NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE, 95) is True
    c.commanded_charge_power = 200  # the retry is now being commanded

    for _ in range(NORMAL_BALANCE_RECAL_CUTOFF_CYCLES - 1):
        assert m._compute_recal_override(c, NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE, 95) is True
    assert m._compute_recal_override(c, NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE, 95) is False

    assert ctrl._normal_balance_recal_retry_pending.get(c, False) is False
    assert ctrl._normal_balance_recal_retry_active.get(c, False) is False
    assert ctrl._normal_balance_recal_latched[c] is True
    # The latched first cutoff prevents a third attempt in the same top session.
    assert m._compute_recal_override(c, NORMAL_BALANCE_RECAL_RETRY_CELL_VOLTAGE, 95) is False


def test_recal_override_cutoff_counter_resets_when_charge_resumes():
    c = _recal_coord(power=5, inv=1)
    ctrl = _controller([c])
    m = _mgr(ctrl)
    m._compute_recal_override(c, 3.55, 95)  # count -> 1
    c.data.update(battery_power=300, inverter_state=0)  # charge resumed
    assert m._compute_recal_override(c, 3.55, 95) is True
    assert c not in ctrl._normal_balance_recal_cutoff_count


def test_recal_override_never_latches_on_idle_battery():
    c = _recal_coord(power=0, inv=1, commanded=0)
    ctrl = _controller([c])
    m = _mgr(ctrl)

    for _ in range(NORMAL_BALANCE_RECAL_CUTOFF_CYCLES * 2):
        assert m._compute_recal_override(c, 3.55, 92) is True

    assert c not in ctrl._normal_balance_recal_latched
    assert ctrl._normal_balance_recal_cutoff_count.get(c, 0) == 0


def test_recal_override_counter_freezes_while_idle():
    c = _recal_coord(power=5, inv=1)
    ctrl = _controller([c])
    m = _mgr(ctrl)
    for _ in range(NORMAL_BALANCE_RECAL_CUTOFF_CYCLES - 1):
        m._compute_recal_override(c, 3.55, 95)
    assert ctrl._normal_balance_recal_cutoff_count[c] == NORMAL_BALANCE_RECAL_CUTOFF_CYCLES - 1

    c.commanded_charge_power = 0
    assert m._compute_recal_override(c, 3.55, 95) is True
    assert ctrl._normal_balance_recal_cutoff_count[c] == NORMAL_BALANCE_RECAL_CUTOFF_CYCLES - 1

    c.commanded_charge_power = 200
    assert m._compute_recal_override(c, 3.55, 95) is False
    assert ctrl._normal_balance_recal_latched.get(c) is True


# ----------------------------------------------------------------------
# get_status
# ----------------------------------------------------------------------

def test_get_status_reports_per_battery_diagnostics():
    c = _Coord(data={"max_cell_voltage": 3.60, "min_cell_voltage": 3.55,
                     "battery_soc": 95})
    empty = _Coord(name="empty", data={})  # skipped: no data
    ctrl = _controller([c, empty])

    status = _mgr(ctrl).get_status()

    assert "empty" not in status
    s = status["bat"]
    assert s["enabled"] is True
    assert s["in_zone"] is True
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
    assert ctrl._normal_balance_phases[c] == "WAIT_MEASURE"
    assert len(calls) == 1
    name, charge, discharge, kw = calls[0]
    assert (charge, discharge) == (0, 0)


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
    ctrl._normal_balance_phases[c] = "WAIT_MEASURE"
    ctrl._normal_balance_measure_started[c] = dt_util.utcnow() - timedelta(seconds=61)

    await _mgr(ctrl).handle_measurement()

    assert ctrl._normal_balance_last_delta_v[c] == 0.05
    assert ctrl._normal_balance_phases[c] == "MEASURED"
    assert len(monitor.calls) == 1
    assert monitor.calls[0][4] == "top_charge_3_55v"


async def test_handle_measurement_records_bms_cutoff_delta_below_pause_voltage():
    c = _Coord(
        data={"max_cell_voltage": 3.58, "min_cell_voltage": 3.54,
              "battery_soc": 99}
    )

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
    ctrl._normal_balance_bms_cutoff_measurement[c] = "pending"
    ctrl._normal_balance_phases[c] = "WAIT_MEASURE"
    ctrl._normal_balance_measure_started[c] = dt_util.utcnow() - timedelta(seconds=61)

    await _mgr(ctrl).handle_measurement()

    assert ctrl._normal_balance_bms_cutoff_measurement[c] == "done"
    assert ctrl._normal_balance_last_delta_v[c] == 0.04
    assert monitor.calls[0][4] == "top_charge_bms_cutoff"


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
    assert c not in ctrl._normal_balance_phases


async def test_handle_measurement_skips_during_weekly_full_charge():
    """Weekly full charge owns the taper to the BMS cutoff — no 3.60 V hold."""
    c = _Coord(data={"max_cell_voltage": 3.60, "min_cell_voltage": 3.55,
                     "battery_soc": 94})
    calls = []

    async def _set(coordinator, charge, discharge, **kw):
        calls.append(coordinator.name)

    ctrl = _controller(
        [c], _set_battery_power=_set, _weekly_full_charge_unlocked=lambda: True
    )

    took_over = await _mgr(ctrl).handle_measurement()

    assert took_over is False
    assert calls == []
    assert c not in ctrl._normal_balance_phases


async def test_handle_measurement_skips_top_voltage_hold_for_venus_ad():
    c = _Coord(
        battery_version="vA",
        data={"max_cell_voltage": NORMAL_BALANCE_PAUSE_CELL_VOLTAGE, "min_cell_voltage": 3.55,
              "battery_soc": 100},
    )
    calls = []

    async def _set(coordinator, charge, discharge, **kw):
        calls.append((coordinator.name, charge, discharge))

    ctrl = _controller([c], _set_battery_power=_set)

    took_over = await _mgr(ctrl).handle_measurement()

    assert took_over is False
    assert calls == []
    assert c not in ctrl._normal_balance_phases


async def test_handle_measurement_waits_after_venus_ad_bms_cutoff():
    c = _Coord(
        battery_version="vA",
        data={"max_cell_voltage": 3.57, "min_cell_voltage": 3.54, "battery_soc": 100},
    )
    calls = []

    async def _set(coordinator, charge, discharge, **kw):
        calls.append((coordinator.name, charge, discharge))

    ctrl = _controller([c], _set_battery_power=_set)
    ctrl._normal_balance_bms_cutoff_measurement[c] = "pending"

    took_over = await _mgr(ctrl).handle_measurement()

    assert took_over is True
    assert ctrl._normal_balance_phases[c] == "WAIT_MEASURE"
    assert calls == [("bat", 0, 0)]


async def test_handle_measurement_records_venus_ad_post_cutoff_delta():
    c = _Coord(
        battery_version="vD",
        data={"max_cell_voltage": 3.57, "min_cell_voltage": 3.54, "battery_soc": 100},
    )

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
    ctrl._normal_balance_bms_cutoff_measurement[c] = "pending"
    ctrl._normal_balance_phases[c] = "WAIT_MEASURE"
    ctrl._normal_balance_measure_started[c] = dt_util.utcnow() - timedelta(seconds=61)

    await _mgr(ctrl).handle_measurement()

    assert ctrl._normal_balance_bms_cutoff_measurement[c] == "done"
    assert ctrl._normal_balance_last_delta_v[c] == 0.03
    assert monitor.calls[0][4] == "top_charge_bms_cutoff"
    assert _mgr(ctrl).should_charge_to_bms_cutoff(c, 100) is False


async def _noop():
    return None
