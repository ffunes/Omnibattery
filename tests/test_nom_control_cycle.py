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
