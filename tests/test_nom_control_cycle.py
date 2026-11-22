"""A NOM battery next to one we command, through the real control cycle."""
from __future__ import annotations

import pytest

import custom_components.omnibattery as integration
from custom_components.omnibattery import ChargeDischargeController as Controller
from tests.nom_house import House, regulate


# ------------------------------------------------------------- complement

def test_the_commanded_battery_covers_what_nom_does_not():
    """House 3000 W, NOM gives 1200 W: the rest is the commanded battery's."""
    house = House(house=3000, nom=regulate(1200)).run(60)
    assert house.command == pytest.approx(-1800, abs=60)
    assert abs(house.grid) < 60


def test_nom_covering_the_house_leaves_nothing_to_command():
    house = House(house=800, nom=regulate(1200)).run(60)
    assert all(abs(c) < 50 for c in house.commands), house.commands


def test_when_nom_gives_more_the_commanded_battery_gives_less():
    house = House(house=3000, nom=-500.0).run(60)
    assert house.command == pytest.approx(-2500, abs=60)
    house.nom = -1500.0
    house.run(60)
    assert house.command == pytest.approx(-1500, abs=60)


def test_the_rest_of_the_surplus_is_charged():
    """2000 W of surplus, NOM takes 800 W: 1200 W are left."""
    house = House(solar=2000, nom=800.0).run(60)
    assert house.command == pytest.approx(1200, abs=60)


# ------------------------------------------------------------- never feed it

def test_nom_filling_from_its_own_sun_with_the_meter_at_zero_starts_nothing():
    house = House(nom=2000.0, dc_coupled=True).run(60)
    assert all(c == 0 for c in house.commands), house.commands


def test_nom_charging_from_the_grid_is_not_fed_by_the_other_battery():
    house = House(nom=800.0).run(60)
    assert all(c >= 0 for c in house.commands), house.commands


def test_nom_charging_while_the_house_imports_only_the_house_is_covered():
    house = House(house=500, nom=800.0).run(60)
    assert house.command == pytest.approx(-500, abs=60)
    assert min(house.commands) > -600, house.commands


@pytest.mark.parametrize("kd", [0.0, 2.0])
def test_when_the_surplus_ends_the_charge_stops_without_becoming_a_discharge(
    kd, monkeypatch
):
    """The meter jumps from 0 to the whole NOM charge in one sample.

    Without the mixed-fleet guards, which go blind as soon as one battery of the
    fleet cannot be read: the feedback itself must not cross.
    """
    monkeypatch.setattr(integration, "apply_guards", lambda _c, power, _grid: power)
    monkeypatch.setattr(integration, "guards_pending", lambda _c, _grid: False)
    house = House(solar=2000, nom=800.0)
    house.run(60)
    house.controller.kd = kd
    assert house.command == pytest.approx(1200, abs=60)
    house.solar = 0.0
    house.commands.clear()
    house.run(40)
    assert all(c > -1 for c in house.commands), house.commands
    assert abs(house.command) < 50


@pytest.mark.parametrize("no_pd", [False, True])
def test_a_smaller_surplus_is_not_overshot_into_a_discharge(no_pd, monkeypatch):
    """900 W of sun left, NOM takes 800 W: 100 W to charge, nothing to discharge.

    The derivative (or, in No-PD, a battery still ramping up) must not carry the
    command past zero, where the only load is NOM's own charge.
    """
    monkeypatch.setattr(integration, "apply_guards", lambda _c, power, _grid: power)
    monkeypatch.setattr(integration, "guards_pending", lambda _c, _grid: False)
    house = House(solar=2000, nom=800.0).run(60)
    house.controller.kd = 3.0
    house.controller.no_pd_mode_enabled = no_pd
    if no_pd:
        house.controller._measured_battery_power = lambda: house.command - 600
    house.solar = 900.0
    house.commands.clear()
    house.run(2)
    assert house.commands and min(house.commands) >= 0, house.commands


def test_when_the_surplus_ends_the_house_is_still_covered():
    house = House(solar=2000, nom=800.0).run(60)
    house.solar, house.house = 0.0, 600.0
    house.commands.clear()
    house.run(60)
    assert house.command == pytest.approx(-600, abs=60)
    assert min(house.commands) > -700, house.commands


def test_two_uncontrolled_batteries_charging_are_added_up():
    """One NOM and one the user holds in manual."""
    house = House(house=400, nom=800.0)
    other = type(house.battery)("other", nom=False)
    other.data["battery_power"] = 500.0
    house.controller.coordinators.append(other)
    house.house += 500.0                    # its charge is on the meter too
    house.run(60)
    assert house.command == pytest.approx(-400, abs=60)


def test_an_excluded_device_with_nom_charging_starts_no_grid_charge():
    """The two corrections together must not read as an export.

    500 W on the meter = 1000 W of sun less NOM's 800 W and a 700 W excluded
    device: 200 W are free for the battery and no more.
    """
    house = House(house=700, solar=1000, nom=800.0)
    house.controller._external_loads.calculate_adjustment = lambda: 700.0
    house.run(60)
    assert 0 <= max(house.commands) <= 260, house.commands


# ------------------------------------------------------------- the import cap

def _capped(**kwargs):
    house = House(cap=3000.0, **kwargs)
    c = house.controller
    for name in ("_release_grid_import_cap", "_discharge_held_by_policy"):
        setattr(c, name, getattr(Controller, name).__get__(c, Controller))
    return house


def test_the_import_cap_counts_the_autonomous_charge():
    """4000 W on the real meter over a 3000 W cap, however NOM is seen elsewhere."""
    house = _capped(house=500, nom=3500.0, has_mode=False).run(60)
    assert house.grid <= 3000 + 60
    assert house.controller._capacity_protection_status["cap_requested_w"] == pytest.approx(
        1000, abs=60
    )


def test_below_the_cap_the_autonomous_charge_is_left_to_the_grid():
    house = _capped(house=500, nom=1500.0, has_mode=False).run(60)
    assert house.command == pytest.approx(-500, abs=60)


def test_a_reserve_that_holds_the_battery_still_protects_the_real_meter():
    """Peak shaving on: only what exceeds the cap on the physical meter."""
    house = _capped(house=500, nom=3500.0, has_mode=False)
    house.controller.capacity_protection_enabled = True
    house.controller.capacity_protection_soc_threshold = 90
    house.run(60)
    assert house.command == pytest.approx(-1000, abs=80)


# ------------------------------------------------------------- nothing changes

@pytest.mark.parametrize("case", [
    {"house": 1500}, {"solar": 1800}, {"house": 300, "solar": 900},
])
def test_without_an_autonomous_battery_the_loop_is_the_same(case, monkeypatch):
    """Same commands with and without the whole feature in the way."""
    plain = House(with_nom=False, **case).run(60)
    bypassed = House(with_nom=False, **case)
    bypassed.controller._autonomous_mode_mgr = None
    monkeypatch.setattr(Controller, "_without_uncommanded_charge", lambda self, grid, target: grid)
    bypassed.run(60)
    assert plain.commands == bypassed.commands
    assert plain.manager._states == {}


# ------------------------------------------------------------- telemetry lost

def _with_blockers(house):
    """The real blocker registry and its one NOM rule."""
    c = house.controller
    c._global_charge_blockers = {}
    c._battery_charge_blockers = {}
    for name in (
        "_set_operation_block", "_remove_operation_block", "_make_block_record",
        "set_discharge_block", "remove_discharge_block", "_block_registry",
        "is_discharge_blocked", "_capacity_protection_overrides_curtailment",
        "_stop_all_batteries_for_block", "get_discharge_blockers", "get_charge_blockers",
        "_serialize_blockers", "_operation_blockers_for_log",
    ):
        setattr(c, name, getattr(Controller, name).__get__(c, Controller))
    c._format_blockers_for_log = Controller._format_blockers_for_log
    c._refresh_operation_blockers = lambda: Controller._refresh_nom_telemetry_block(c)
    c._is_operation_allowed = lambda is_charging: is_charging or not c.is_discharge_blocked()
    return house


def test_nom_whose_power_is_lost_stops_the_discharges():
    """NOM cannot be put in Manual either: nobody knows what it is taking."""
    house = _with_blockers(House(house=1000, nom=0.0)).run(40)
    assert house.command == pytest.approx(-1000, abs=60)
    house.battery.driver.obeys = False
    house.battery.is_available = False
    house.run(10)
    assert house.command == 0
    assert "nom_telemetry" in house.controller._global_discharge_blockers
    assert house.manager.status(house.battery) == "degraded"


def test_the_charge_goes_on_and_shrinks_with_the_surplus_while_nom_is_blind():
    house = _with_blockers(House(solar=1500, nom=0.0))
    house.battery.driver.obeys = False
    house.battery.is_available = False
    house.run(60)
    assert house.command == pytest.approx(1500, abs=60)
    house.solar = 400.0
    house.run(40)
    assert house.command == pytest.approx(400, abs=60)


def test_a_blind_battery_confirmed_in_manual_blocks_nothing():
    house = _with_blockers(House(house=1000, nom=0.0)).run(40)
    house.battery.is_available = False
    house.run(60)                           # goes to Manual, and shows it
    assert house.battery.driver.mode == "manual"
    assert house.command == pytest.approx(-1000, abs=60)


def test_nom_comes_back_once_its_power_can_be_read():
    house = _with_blockers(House(house=1000, nom=regulate(300))).run(20)
    house.battery.is_available = False
    house.run(20)
    assert house.battery.driver.mode == "manual"
    house.battery.is_available = True
    house.run(60)
    assert house.battery.driver.mode == "nom"
    assert house.manager.status(house.battery) == "nom_active"


def test_an_observed_battery_without_a_mode_blocks_nothing():
    """Phase 1 stays as it was."""
    house = _with_blockers(House(house=1000, nom=0.0, has_mode=False))
    house.battery.is_available = False
    house.run(60)
    assert house.command == pytest.approx(-1000, abs=60)
    assert house.controller._global_discharge_blockers == {}


# ------------------------------------------------------------- grid charge

def test_a_grid_charge_does_not_start_before_nom_rests():
    """It would cover the charge from its own cells."""
    house = House(house=400, nom=regulate(1200)).run(20)
    assert house.battery.driver.mode == "nom"
    house.start_grid_charge()
    house.history.clear()
    house.run(120)
    assert house.command > 2000
    fed = [(command, nom) for _, command, nom in house.history if command > 50 and nom < -50]
    assert fed == [], "NOM discharged while the other battery charged"
    assert house.battery.driver.mode == "manual"
    assert house.issues == set()


def test_when_manual_fails_the_charge_starts_after_sixty_seconds_with_a_warning():
    """Waits, starts anyway, keeps trying, and the warning clears itself."""
    house = House(house=400, nom=regulate(1200)).run(20)
    house.battery.driver.obeys = False
    house.start_grid_charge()
    house.commands.clear()
    house.run(56)
    assert max(house.commands, default=0) <= 0, "started before the wait was over"
    assert house.controller.grid_charging_active, "the charge is not called off"
    house.run(40)
    assert house.command > 1000
    assert house.issues == {"nom_manual_unconfirmed"}
    assert house.manager.status(house.battery) == "degraded"
    assert house.manager.snapshot(house.battery)["effective_mode"] == "nom"

    tries = len(house.battery.driver.writes)
    house.run(90)
    assert len(house.battery.driver.writes) > tries
    assert house.command > 1000
    house.battery.driver.obeys = True
    house.run(70)
    assert house.battery.driver.mode == "manual"
    assert house.issues == set()
    assert house.manager.status(house.battery) == "nom_suspended_grid_charge"
    assert house.command > 1000


def test_normal_control_runs_while_the_charge_waits():
    """The command is not left running blind for a minute."""
    house = House(house=3000, nom=-1200.0)
    house.battery.driver.obeys = False
    house.run(40)
    house.start_grid_charge()
    house.house = 2000
    house.run(40)
    assert house.command == pytest.approx(-800, abs=80)


def test_nom_comes_back_after_the_charge():
    house = House(house=400, nom=regulate(1200)).run(20)
    house.start_grid_charge()
    house.run(60)
    house.stop_grid_charge()
    house.run(80)
    assert house.battery.driver.mode == "nom"
    assert abs(house.grid) < 80


def test_a_battery_without_a_mode_delays_no_charge():
    """Phase 1: its risk is documented, the charge starts at once."""
    house = House(house=400, nom=0.0, has_mode=False).run(10)
    house.start_grid_charge()
    house.run(6)
    assert house.command > 2000


def test_solar_surplus_charging_does_not_suspend_nom():
    house = House(solar=2500, nom=800.0).run(120)
    assert house.command == pytest.approx(1700, abs=80)
    assert house.battery.driver.mode == "nom" and house.battery.driver.writes == []


def test_a_running_excluded_device_stops_nom_and_the_other_battery_goes_on():
    """House 300 W plus a 2000 W device the batteries must not feed."""
    house = House(house=300, nom=regulate(1200))
    loads = house.controller._external_loads
    loads.calculate_adjustment = lambda: 0.0
    devices = [{"entity": "sensor.car", "active": False, "power_w": 0.0}]
    loads.autonomous_discharge_interlocks = lambda: devices
    house.run(60)
    house.house = 2300
    devices[0].update(active=True, power_w=2000.0)
    loads.calculate_adjustment = lambda: 2000.0
    house.run(4)
    house.history.clear()
    house.run(120)
    assert all(nom == 0 for _, _, nom in house.history)
    assert house.command == pytest.approx(-300, abs=60)
    house.house = 300
    devices[0].update(active=False, power_w=0.0)
    loads.calculate_adjustment = lambda: 0.0
    house.run(60)
    assert house.battery.driver.mode == "nom"
    assert house.battery.driver.writes == ["manual", "nom"]


def test_a_manual_charge_slot_waits_too():
    """It commands its battery directly, past the predictive handler."""
    house = House(house=400, nom=regulate(1200))
    c = house.controller
    applied = []

    async def _apply(net):
        applied.append((net, house.battery.driver.mode))
        house.marstek.data["ac_power"] = -net
        return type("R", (), {"ok": True, "failure_reason": None})()

    house.marstek.apply_power = _apply
    slot = {"mode": "manual", "power_override_enabled": True, "allow_charge": True}
    c.__dict__.update(
        _get_active_slot=lambda *_a: slot,
        _slot_battery_limits=lambda *_a: {"max_charge_power_w": 1500},
        _is_backup_function_active=lambda _c: False,
        get_charge_blockers=lambda _c: {}, get_discharge_blockers=lambda _c: {},
    )
    for name in ("_try_apply_manual_slot", "_slot_manual_direction_for"):
        setattr(c, name, getattr(Controller, name).__get__(c, Controller))
    house.run(10)
    assert applied and all(mode == "manual" for _, mode in applied), applied
    assert applied[0][0] == 1500


def test_changing_the_mode_disturbs_neither_the_other_battery_nor_the_controller():
    house = House(house=1000, nom=0.0).run(60)
    before, power = house.command, house.controller.previous_power
    house.commands.clear()
    house.battery.autonomous_requested_mode = "manual"
    house.run(6)
    assert house.battery.driver.mode == "manual"
    assert house.controller.previous_power == pytest.approx(power, abs=5)
    assert all(c == pytest.approx(before, abs=5) for c in house.commands), house.commands


def test_a_fixed_discharge_into_a_charge_we_did_not_foresee_is_stopped():
    """NOM discharges on its own schedule while the sun charges the other one."""
    house = House(solar=1500, nom=-400.0).run(60)
    assert house.battery.driver.mode == "manual"
    assert house.manager.last_cross_flow["charging"].keys() == {"battery"}
    assert house.command == pytest.approx(1500, abs=60), "only the sun's"


def test_only_autonomous_batteries_and_no_commandable_one():
    """The modes are still managed and nothing divides by zero."""
    house = House(house=1500, nom=regulate(800), with_marstek=False).run(60)
    assert house.commands == []
    assert house.manager.status(house.battery) == "nom_active"
    house.start_grid_charge()
    house.run(20)
    assert house.controller.grid_charging_active is False, "nothing to charge"
    house.controller.manual_mode_enabled = True
    house.controller._apply_software_manual_setpoints = _nothing
    house.controller._surplus_hold_mgr = house.controller._discharge_reserve_mgr = None
    house.controller._pricing_mgr.clear_curtailment_runtime = lambda _r: None
    house.run(10)
    assert house.battery.driver.mode == "manual"


async def _nothing(*_args, **_kwargs):
    return None


def test_a_lost_battery_is_not_still_charging_at_its_last_power():
    """Its last 800 W stay in the data; confirmed in Manual it takes nothing."""
    house = _with_blockers(House(house=1000, nom=800.0)).run(40)
    house.battery.is_available = False
    house.run(60)
    assert house.battery.driver.mode == "manual"
    assert house.command == pytest.approx(-1000, abs=60)


def test_a_load_step_under_a_solar_charge_is_not_paid_by_nom():
    """The kettle: NOM holds the meter at zero, so only its power shows the load."""
    house = House(solar=2000, nom=regulate(1200)).run(60)
    assert house.command > 700
    house.house = 1500
    house.run(60)
    fed = min(house.command, -house.history[-1][2])
    assert fed < 50, "NOM discharges into the other battery's charge"
    assert abs(house.grid) < 60
    assert house.battery.driver.writes == [] and house.manager.last_cross_flow is None


def test_no_charge_starts_on_an_export_nom_is_making():
    house = House(nom=-400.0).run(6)
    assert all(c == 0 for c in house.commands), house.commands


@pytest.mark.parametrize("target", [-1500.0, 800.0])
def test_importing_or_exporting_on_purpose_takes_nom_out(target):
    """It would absorb the sale, or discharge into what is being bought."""
    house = House(house=300, nom=regulate(1200)).run(40)
    house.controller._setpoint_overrides = {"high_price": (5, target)}
    house.run(80)
    assert house.battery.driver.mode == "manual"
    assert house.manager.status(house.battery) == "nom_suspended_grid_target"
    assert house.grid == pytest.approx(target, abs=60)
    house.controller._setpoint_overrides = {}
    house.run(80)
    assert house.battery.driver.mode == "nom"


def test_a_small_standing_target_and_the_reserve_leave_nom_alone():
    house = House(house=800, nom=regulate(1200))
    house.controller._setpoint_offsets = {"user_target": -30.0}
    house.controller._setpoint_overrides = {"capacity_protection": (10, 2500.0)}
    house.run(40)
    assert house.battery.driver.mode == "nom"


def test_a_nom_discharge_does_not_hide_the_charge_of_a_manual_battery():
    """Added up battery by battery: 800 W in and 500 W out are not 300 W in."""
    house = House(house=900, nom=-500.0)
    other = type(house.battery)("other", nom=False)
    other.data["battery_power"] = 800.0
    house.controller.coordinators.append(other)
    house.house += 800.0
    house.run(6)                            # before the detector ends it
    assert house.command == pytest.approx(-400, abs=60)
