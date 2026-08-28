"""Tests for the primary battery and its house-load feedforward.

The situation this exists for: a hybrid inverter running its own
self-consumption shares the meter. It removes the grid error before the PD loop
sees one, so the loop commands nothing, and the second battery never runs — the
grid looks perfect while half the storage sits idle.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery import (
    _apply_primary_feedforward,
    _apply_surplus_guard,
    _surplus_guard_pending,
    _uncovered_load_w,
    _measured_house_load_w,
    _primary_coordinator,
    _primary_feedforward_candidate_w,
    _primary_feedforward_pending,
    _primary_feedforward_w,
)


def _battery(name, *, ac_power=None, battery_power=None, soc=50, available=True):
    data = {"battery_soc": soc}
    if ac_power is not None:
        data["ac_power"] = ac_power
    if battery_power is not None:
        data["battery_power"] = battery_power
    return SimpleNamespace(name=name, data=data, is_available=available)


def _controller(batteries, *, primary="", enabled=True, limit=2500, grid_sensor=None):
    return SimpleNamespace(
        coordinators=batteries,
        primary_battery=primary,
        primary_feedforward_enabled=enabled,
        solar_production_sensor=grid_sensor,
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _eid: None)),
        previous_power=0.0,
        _battery_power_limit=lambda coordinator, is_charging: limit,
    )


# ----------------------------------------------------------------------
# house load
# ----------------------------------------------------------------------
def test_the_house_load_counts_power_this_controller_never_commanded():
    """The whole point: a battery someone else is driving still feeds the house.

    On the reference installation the grid sits at 0 W while a hybrid inverter
    discharges 800 W under its own energy manager. A commanded-power estimate
    would call that a 0 W house; the measured balance sees the 800 W.
    """
    controller = _controller([
        _battery("Huawei", battery_power=-800),   # +charge / −discharge
        _battery("Marstek", ac_power=0),
    ])
    assert _measured_house_load_w(controller, 0.0) == 800.0


def test_a_battery_that_dropped_out_is_left_out():
    """Its last reading is frozen, and the meter already shows its load."""
    controller = _controller([
        _battery("Huawei", battery_power=-800, available=False),
        _battery("Marstek", ac_power=200),
    ])
    assert _measured_house_load_w(controller, 600.0) == 800.0


def test_no_readable_battery_means_no_answer_rather_than_zero():
    controller = _controller([_battery("Marstek", available=False)])
    assert _measured_house_load_w(controller, 500.0) is None


def test_export_does_not_produce_a_negative_house():
    controller = _controller([_battery("Marstek", ac_power=1000)])
    assert _measured_house_load_w(controller, -3000.0) == 0.0


# ----------------------------------------------------------------------
# nominating the primary
# ----------------------------------------------------------------------
def test_without_a_nomination_the_ordinary_choice_is_used():
    """Switching the feedforward on alone changes when a battery is asked,
    not which one — it addresses the battery the discharge ordering would have
    picked anyway, the fullest."""
    fuller = _battery("Marstek", ac_power=0, soc=70)
    emptier = _battery("Huawei", battery_power=-800, soc=30)
    controller = _controller([emptier, fuller], primary="")
    assert _primary_coordinator(controller) is fuller
    assert _primary_feedforward_w(controller, 0.0) == 800.0


def test_a_name_that_no_longer_matches_falls_back_rather_than_stopping():
    """A battery can be renamed or removed; the feature should not go quiet."""
    fuller = _battery("Marstek", ac_power=0, soc=70)
    controller = _controller([fuller], primary="Venus 9")
    assert _primary_coordinator(controller) is fuller


def test_a_fleet_that_cannot_discharge_has_no_primary():
    controller = _controller([_battery("Marstek", ac_power=0)], primary="", limit=0)
    assert _primary_coordinator(controller) is None
    assert _primary_feedforward_w(controller, 500.0) == 0.0


def test_the_switch_gates_the_command_but_not_the_reading():
    """The figure has to be inspectable before it is acted on."""
    controller = _controller(
        [_battery("Huawei", battery_power=-800), _battery("Marstek", ac_power=0)],
        primary="Marstek",
        enabled=False,
    )
    assert _primary_feedforward_candidate_w(controller, 0.0) == 800.0
    assert _primary_feedforward_w(controller, 0.0) == 0.0


def test_a_battery_that_cannot_discharge_gets_no_feedforward():
    """min SOC, discharge disallowed, a driver limit of zero — all the same here."""
    controller = _controller(
        [_battery("Marstek", ac_power=0)], primary="Marstek", limit=0
    )
    assert _primary_feedforward_w(controller, 500.0) == 0.0


def test_the_feedforward_never_exceeds_what_the_battery_can_deliver():
    controller = _controller(
        [_battery("Huawei", battery_power=-4000), _battery("Marstek", ac_power=0)],
        primary="Marstek",
        limit=2500,
    )
    assert _primary_feedforward_w(controller, 0.0) == 2500.0


# ----------------------------------------------------------------------
# acting on it
# ----------------------------------------------------------------------
def test_the_command_is_floored_at_the_house_load():
    controller = _controller(
        [_battery("Huawei", battery_power=-800), _battery("Marstek", ac_power=0)],
        primary="Marstek",
    )
    # Discharge is negative. An idle loop is raised to cover the house...
    assert _apply_primary_feedforward(controller, 0, 0.0) == -800.0
    # ...and so is a charge command, which would otherwise fight it.
    assert _apply_primary_feedforward(controller, 500, 0.0) == -800.0


def test_a_larger_discharge_is_left_alone():
    """It is a floor, not a target: the PD may always ask for more."""
    controller = _controller(
        [_battery("Huawei", battery_power=-800), _battery("Marstek", ac_power=0)],
        primary="Marstek",
    )
    assert _apply_primary_feedforward(controller, -2000, 0.0) == -2000


def test_the_deadband_shortcut_is_skipped_while_the_floor_is_unmet():
    """A grid on target is exactly the state this feature exists to change.

    It is on target *because* the other regulator carried the load, so the
    deadband's reasoning — nothing to correct — does not apply.
    """
    controller = _controller(
        [_battery("Huawei", battery_power=-800), _battery("Marstek", ac_power=0)],
        primary="Marstek",
    )
    assert _primary_feedforward_pending(controller, 0.0) is True

    # Once the primary carries it, the shortcut is welcome again.
    controller.previous_power = -800.0
    assert _primary_feedforward_pending(controller, 0.0) is False


def test_a_small_shortfall_does_not_reopen_the_cycle():
    """Below the tolerance a correction is not worth a write."""
    controller = _controller(
        [_battery("Huawei", battery_power=-850), _battery("Marstek", ac_power=0)],
        primary="Marstek",
    )
    controller.previous_power = -800.0
    assert _primary_feedforward_pending(controller, 0.0) is False


def test_the_switch_being_off_leaves_the_deadband_untouched():
    controller = _controller(
        [_battery("Huawei", battery_power=-800)], primary="Huawei", enabled=False
    )
    assert _primary_feedforward_pending(controller, 0.0) is False


# ----------------------------------------------------------------------
# discharge order
# ----------------------------------------------------------------------
def _selector(batteries, primary=""):
    from custom_components.omnibattery.control.power_distribution import PowerDistribution

    controller = _controller(batteries, primary=primary)
    controller._active_charge_batteries = []
    controller._active_discharge_batteries = []
    selector = PowerDistribution.__new__(PowerDistribution)
    selector._controller = controller
    selector._is_battery_manual_owned = lambda _c: False
    return selector


def test_discharge_normally_starts_with_the_fullest():
    marstek = _battery("Marstek", soc=55)
    huawei = _battery("Huawei", soc=40)
    selector = _selector([huawei, marstek])
    order = selector._ordered_batteries_for_operation([huawei, marstek], False)
    assert [b.name for b in order] == ["Marstek", "Huawei"]


def test_a_nominated_primary_goes_first_even_when_emptier():
    marstek = _battery("Marstek", soc=20)
    huawei = _battery("Huawei", soc=90)
    selector = _selector([huawei, marstek], primary="Marstek")
    order = selector._ordered_batteries_for_operation([huawei, marstek], False)
    assert [b.name for b in order] == ["Marstek", "Huawei"]


def test_charging_still_fills_the_emptiest_first():
    """Otherwise the primary would be driven to both extremes and never level out."""
    marstek = _battery("Marstek", soc=80)
    huawei = _battery("Huawei", soc=30)
    selector = _selector([huawei, marstek], primary="Marstek")
    order = selector._ordered_batteries_for_operation([huawei, marstek], True)
    assert [b.name for b in order] == ["Huawei", "Marstek"]


def test_both_new_entities_are_named_in_every_language():
    import glob
    import json

    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        entity = json.load(open(path, encoding="utf-8"))["entity"]
        assert entity["select"]["primary_battery"]["name"], path
        # The "automatic" option is a state, not a battery name, so it needs one.
        assert entity["select"]["primary_battery"]["state"]["automatic"], path
        assert entity["switch"]["primary_feedforward"]["name"], path


def test_the_panel_offers_both_controls_in_every_language():
    """A setting nobody can find is a setting nobody uses."""
    import re

    panel = open(
        "custom_components/omnibattery/frontend/marstek-panel.js", encoding="utf-8"
    ).read()

    assert '{ key: "primary_battery", domain: "select"' in panel
    assert '{ key: "primary_feedforward", domain: "switch"' in panel
    # One label set and one help text per language block.
    for key in ("secPrimary", "itemPrimaryBattery", "itemPrimaryFeedforward"):
        assert len(re.findall(r"\b%s:" % key, panel)) == 6, key
    for key in ("primary_battery", "primary_feedforward"):
        assert len(re.findall(r"^    %s: \"" % key, panel, re.M)) == 6, key


# ----------------------------------------------------------------------
# what the batteries actually have to cover
#
# The house load is the wrong thing to feed forward. Under sun the roof covers
# it, and commanding the primary to supply it anyway discharges one battery into
# the other. Observed on the reference installation: 1188 W of PV against a
# 570 W house, the primary discharging 350 W while the other took in 920 W.
# ----------------------------------------------------------------------
def _solar_controller(pv_w, batteries, primary="Marstek", enabled=True, limit=2500):
    """A controller whose solar sensor reports pv_w."""
    controller = _controller(batteries, primary=primary, enabled=enabled, limit=limit)
    controller.solar_production_sensor = "sensor.pv"
    controller.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _eid: SimpleNamespace(state=str(pv_w)))
    )
    return controller


def test_pv_covering_the_house_leaves_the_primary_alone():
    """The exact case that was wrong: sun to spare, and it still discharged."""
    controller = _solar_controller(1188, [
        _battery("Marstek", battery_power=-350),  # was discharging 350 W
        _battery("Huawei", battery_power=920),    # while this one charged 920 W
    ])
    # Grid near zero: the roof is carrying the house and charging the other unit.
    assert _uncovered_load_w(controller, 16.0) == pytest.approx(-554.0)
    assert _primary_feedforward_w(controller, 16.0) == 0.0


def test_only_the_shortfall_is_fed_forward():
    """Half-covered by the roof means half from the battery, not all of it."""
    controller = _solar_controller(400, [
        _battery("Marstek", ac_power=0),
        _battery("Huawei", ac_power=0),
    ])
    # House 1000 W, roof 400 W: the meter shows the 600 W nobody covers.
    assert _uncovered_load_w(controller, 600.0) == 600.0
    assert _primary_feedforward_w(controller, 600.0) == 600.0


def test_another_battery_charging_is_not_a_load_to_cover():
    """Its charging shows up at the meter, but the primary must not chase it."""
    controller = _solar_controller(0, [
        _battery("Marstek", ac_power=0),
        _battery("Huawei", ac_power=-500),   # charging 500 W from the grid
    ])
    # Meter reads house 600 plus that 500. Only the 600 is the primary's job.
    assert _uncovered_load_w(controller, 1100.0) == 600.0
    assert _primary_feedforward_w(controller, 1100.0) == 600.0


def test_after_dark_it_is_the_house_load_again():
    """With no sun the two quantities coincide, which is the night case."""
    controller = _solar_controller(0, [
        _battery("Marstek", ac_power=665),
        _battery("Huawei", ac_power=44),
    ])
    assert _uncovered_load_w(controller, -40.0) == 669.0
    assert _primary_feedforward_w(controller, -40.0) == 669.0


def test_no_readable_battery_means_no_feedforward():
    controller = _solar_controller(0, [_battery("Marstek", available=False)])
    assert _uncovered_load_w(controller, 500.0) is None
    assert _primary_feedforward_w(controller, 500.0) == 0.0


# ----------------------------------------------------------------------
# never discharge into a surplus
#
# Independent of the feedforward, and the reason the reference installation
# discharged one battery while the other charged: the PD loop controls on the
# raw meter, and with a second regulator on it the two cancel out. The meter
# reads zero, the deadband holds the standing command, and 829 W of surplus
# takes a round trip through two conversion losses.
# ----------------------------------------------------------------------
def _surplus_case(enabled=False):
    """1391 W of PV over a 529 W house; the other battery takes the surplus."""
    return _solar_controller(1391, [
        _battery("Marstek", battery_power=-205),   # discharging, commanded by us
        _battery("Huawei", battery_power=1110),    # charging, commanded by nobody
    ], enabled=enabled)


def test_a_discharge_into_surplus_is_refused():
    controller = _surplus_case()
    assert _uncovered_load_w(controller, 3.0) < 0
    assert _apply_surplus_guard(controller, -165, 3.0) == 0


def test_charging_into_a_surplus_is_exactly_right():
    """The guard is one-directional: absorbing surplus is the point."""
    controller = _surplus_case()
    assert _apply_surplus_guard(controller, 800, 3.0) == 800


def test_the_quiet_meter_does_not_hide_the_standing_discharge():
    """The grid reads zero only because the two batteries cancel out."""
    controller = _surplus_case()
    controller.previous_power = -165.0
    assert _surplus_guard_pending(controller, 3.0) is True

    # Once the discharge is withdrawn there is nothing left to correct.
    controller.previous_power = 0.0
    assert _surplus_guard_pending(controller, 3.0) is False


def test_a_real_deficit_still_discharges():
    """After dark the guard must keep out of the way."""
    controller = _solar_controller(0, [
        _battery("Marstek", ac_power=665),
        _battery("Huawei", ac_power=44),
    ])
    assert _apply_surplus_guard(controller, -700, -40.0) == -700
    controller.previous_power = -700.0
    assert _surplus_guard_pending(controller, -40.0) is False


def test_grid_charging_is_not_caught_by_the_guard():
    """Charging from the grid at night is a decision, not an accident."""
    controller = _solar_controller(0, [_battery("Marstek", ac_power=-1000)])
    # Meter shows the house plus the charging; the guard only ever vetoes
    # discharge, so a deliberate charge passes untouched.
    assert _apply_surplus_guard(controller, 1000, 1600.0) == 1000


def test_without_a_readable_battery_the_guard_stays_out():
    controller = _solar_controller(2000, [_battery("Marstek", available=False)])
    assert _apply_surplus_guard(controller, -500, -1500.0) == -500


# ----------------------------------------------------------------------
# the guard must not follow the clouds
#
# A bare sign test toggles the battery in step with the light: PV wanders across
# the house load, the uncovered figure crosses zero, and discharge starts and
# stops every cycle. The guard engages only on a clear surplus and releases the
# moment the load turns positive.
# ----------------------------------------------------------------------
def _drift(controller, series, grid_w=0.0):
    """Walk a sequence of uncovered-load values past the guard."""
    from custom_components.omnibattery import _surplus_blocks_discharge

    verdicts = []
    for uncovered in series:
        # One battery, its own output the only thing between grid and house.
        controller.coordinators = [_battery("Marstek", ac_power=uncovered - grid_w)]
        verdicts.append(_surplus_blocks_discharge(controller, grid_w))
    return verdicts


def test_noise_around_zero_does_not_toggle_the_guard():
    controller = _solar_controller(0, [], primary="Marstek")
    controller.deadband = 40
    controller._surplus_guard_latched = False
    # Drifting between a small surplus and a small deficit: never clear enough.
    assert _drift(controller, [-30, 20, -60, 40, -90, 10]) == [False] * 6


def test_a_clear_surplus_engages_it_and_a_real_deficit_releases_it():
    controller = _solar_controller(0, [], primary="Marstek")
    controller.deadband = 40
    controller._surplus_guard_latched = False
    #        clear surplus        drifting back up          real deficit
    series = [-800, -400, -120,   -60, -10, -30,            120, 300]
    assert _drift(controller, series) == [
        True, True, True,
        True, True, True,     # inside the band the verdict is held
        False, False,
    ]


def test_a_passing_cloud_hands_the_house_back_at_once():
    """Release has no band: waiting would import while the battery sat idle."""
    controller = _solar_controller(0, [], primary="Marstek")
    controller.deadband = 40
    controller._surplus_guard_latched = True
    assert _drift(controller, [1])[0] is False


def test_a_missing_reading_leaves_the_verdict_where_it_was():
    from custom_components.omnibattery import _surplus_blocks_discharge

    controller = _solar_controller(0, [_battery("Marstek", available=False)])
    controller._surplus_guard_latched = True
    assert _surplus_blocks_discharge(controller, 0.0) is True
    controller._surplus_guard_latched = False
    assert _surplus_blocks_discharge(controller, 0.0) is False


def test_the_band_follows_the_configured_deadband():
    """A user who widened the deadband widened their idea of meter noise."""
    controller = _solar_controller(0, [], primary="Marstek")
    controller.deadband = 500
    controller._surplus_guard_latched = False
    assert _drift(controller, [-300]) == [False]      # inside a wide deadband
    assert _drift(controller, [-800]) == [True]


def test_the_diagnostic_survives_a_cleared_cycle_reading():
    """previous_sensor is dropped whenever another manager takes the wheel.

    A max-SOC charge does exactly that, and a diagnostic that blanks out while
    something interesting is happening is no diagnostic — it read null on the
    reference installation at the very moment the surplus was being absorbed.
    """
    from custom_components.omnibattery import _grid_reading_w

    controller = _controller([_battery("Marstek", ac_power=0)])
    controller.consumption_sensor = "sensor.grid"
    controller.meter_inverted = False
    controller.previous_sensor = None
    controller.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _eid: SimpleNamespace(state="-66.5"))
    )
    assert _grid_reading_w(controller) == pytest.approx(-66.5)

    # The cycle's own reading still wins when it has one.
    controller.previous_sensor = 12.0
    assert _grid_reading_w(controller) == 12.0


def test_an_inverted_meter_is_honoured_by_the_diagnostic():
    from custom_components.omnibattery import _grid_reading_w

    controller = _controller([_battery("Marstek", ac_power=0)])
    controller.consumption_sensor = "sensor.grid"
    controller.meter_inverted = True
    controller.previous_sensor = None
    controller.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _eid: SimpleNamespace(state="500"))
    )
    assert _grid_reading_w(controller) == -500.0


def test_an_unavailable_meter_reports_nothing_rather_than_zero():
    from custom_components.omnibattery import _grid_reading_w

    controller = _controller([_battery("Marstek", ac_power=0)])
    controller.consumption_sensor = "sensor.grid"
    controller.meter_inverted = False
    controller.previous_sensor = None
    controller.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _eid: SimpleNamespace(state="unavailable"))
    )
    assert _grid_reading_w(controller) is None


# ----------------------------------------------------------------------
# the surplus side
#
# The mirror of the load side, needed for the same reason. A second regulator on
# the meter takes the surplus into its own battery, the grid reads zero, and
# this controller correctly commands nothing on what it can see. The battery
# meant to be filled first then stays empty: observed at 5834 W of sun with the
# hybrid at 83 % and the other at 17 %, untouched, order and switch both right.
# ----------------------------------------------------------------------
def _surplus_taken_by_the_other():
    """Sun to spare, and the hybrid quietly absorbing all of it."""
    from custom_components.omnibattery import _charge_feedforward_w

    controller = _solar_controller(5834, [
        _battery("Marstek", ac_power=13),      # idle
        _battery("Huawei", ac_power=-5187),    # charging, commanded by nobody
    ])
    controller.charge_priority = "Marstek"
    controller._scarce_solar_latched = False
    controller._active_charge_batteries = []
    controller._battery_power_limit = lambda coordinator, is_charging: (
        2500 if coordinator.name == "Marstek" else 7000
    )
    return controller, _charge_feedforward_w


def test_the_whole_surplus_is_offered_not_one_batterys_worth():
    """The floor is a system figure; the distribution shares it out after.

    Capped at the head battery's own rating, that battery ends up with only its
    share of its own limit — 2418 W of 2500 on the reference installation, while
    6.8 kW of surplus went past it.
    """
    controller, feedforward = _surplus_taken_by_the_other()
    assert _uncovered_load_w(controller, 67.0) < 0
    assert feedforward(controller, 67.0) == 5107.0


def test_the_offer_never_exceeds_what_the_fleet_can_take():
    controller, feedforward = _surplus_taken_by_the_other()
    controller._battery_power_limit = lambda coordinator, is_charging: (
        2500 if coordinator.name == "Marstek" else 0
    )
    assert feedforward(controller, 67.0) == 2500.0


def test_a_small_surplus_is_not_rounded_up_to_the_limit():
    controller, feedforward = _surplus_taken_by_the_other()
    controller.coordinators = [
        _battery("Marstek", ac_power=0),
        _battery("Huawei", ac_power=-800),
    ]
    assert feedforward(controller, 0.0) == 800.0


def test_a_deficit_offers_nothing_to_absorb():
    controller, feedforward = _surplus_taken_by_the_other()
    controller.coordinators = [_battery("Marstek", ac_power=665)]
    assert feedforward(controller, -40.0) == 0.0


def test_the_command_is_floored_at_the_surplus():
    controller, _ = _surplus_taken_by_the_other()
    assert _apply_primary_feedforward(controller, 0, 67.0) == 5107.0
    # A larger charge request is left alone; it is a floor, not a target.
    assert _apply_primary_feedforward(controller, 6000, 67.0) == 6000


def test_the_quiet_meter_does_not_hide_an_unclaimed_surplus():
    controller, _ = _surplus_taken_by_the_other()
    controller.previous_power = 0.0
    assert _primary_feedforward_pending(controller, 67.0) is True

    controller.previous_power = 5107.0
    assert _primary_feedforward_pending(controller, 67.0) is False


def test_the_switch_gates_the_surplus_side_too():
    controller, feedforward = _surplus_taken_by_the_other()
    controller.primary_feedforward_enabled = False
    assert feedforward(controller, 67.0) == 0.0
    assert _primary_feedforward_pending(controller, 67.0) is False


def test_a_battery_that_cannot_charge_adds_no_room():
    controller, feedforward = _surplus_taken_by_the_other()
    controller._battery_power_limit = lambda coordinator, is_charging: (
        0 if coordinator.name == "Marstek" else 3000
    )
    assert feedforward(controller, 67.0) == 3000.0


def test_the_diagnostics_refresh_themselves():
    """Their figures live on the controller and change every cycle.

    Unpolled, an entity's attributes freeze at the last state write — for a
    switch that is the moment someone toggled it. Observed reporting a 3647 W
    surplus at dusk with 68 W of sun, four hours after it was switched on.
    """
    import inspect

    from custom_components.omnibattery.select import ChargePrioritySelect
    from custom_components.omnibattery.switch import PrimaryFeedforwardSwitch

    for cls in (PrimaryFeedforwardSwitch, ChargePrioritySelect):
        source = inspect.getsource(cls.__init__)
        assert "_attr_should_poll = True" in source, cls.__name__
        assert "extra_state_attributes" in inspect.getsource(cls), cls.__name__


# ----------------------------------------------------------------------
# never pump one battery into the other
#
# A battery behind the same meter is ordinary household load to another
# regulator on it, so a charge command is never refused — whatever is asked for
# gets covered, from the sun if it is there and from the other battery if it is
# not. The cap at the *uncovered* surplus is what keeps that from happening,
# and it is easy to mistake for a mere device limit.
# ----------------------------------------------------------------------
def test_no_surplus_means_nothing_is_asked_for_however_much_room_there_is():
    """Both batteries have room; the sun does not. Asking anyway would move
    energy out of one and into the other through two conversions."""
    from custom_components.omnibattery import _charge_feedforward_w

    controller = _solar_controller(300, [
        _battery("Marstek", ac_power=0),       # empty and idle
        _battery("Huawei", ac_power=250),      # discharging to cover the house
    ])
    controller.charge_priority = "Marstek"
    controller._scarce_solar_latched = False
    controller._active_charge_batteries = []
    controller._battery_power_limit = lambda coordinator, is_charging: 2500
    # Meter at zero, house covered by the other battery: no surplus exists.
    assert _uncovered_load_w(controller, 0.0) == 250.0
    assert _charge_feedforward_w(controller, 0.0) == 0.0


def test_only_the_uncovered_part_is_claimed_not_the_whole_house():
    """The other battery's own output must not be counted as available."""
    from custom_components.omnibattery import _charge_feedforward_w

    controller = _solar_controller(4000, [
        _battery("Marstek", ac_power=0),
        _battery("Huawei", ac_power=-1500),    # already absorbing 1500 W
    ])
    controller.charge_priority = "Marstek"
    controller._scarce_solar_latched = False
    controller._active_charge_batteries = []
    controller._battery_power_limit = lambda coordinator, is_charging: 2500
    # Meter exports 2000 W on top of the 1500 W the other battery is taking.
    assert _uncovered_load_w(controller, -2000.0) == -3500.0
    # So 3500 W may be claimed — the sum, not the fleet's 5000 W of room.
    assert _charge_feedforward_w(controller, -2000.0) == 3500.0


def test_the_claim_never_exceeds_the_surplus_even_with_room_to_spare():
    from custom_components.omnibattery import _charge_feedforward_w

    controller = _solar_controller(1000, [
        _battery("Marstek", ac_power=0),
        _battery("Huawei", ac_power=0),
    ])
    controller.charge_priority = "Marstek"
    controller._scarce_solar_latched = False
    controller._active_charge_batteries = []
    controller._battery_power_limit = lambda coordinator, is_charging: 7000
    assert _charge_feedforward_w(controller, -400.0) == 400.0


# ----------------------------------------------------------------------
# discharge ceiling
# ----------------------------------------------------------------------
# The feedforward already refuses to *chase* another battery's charging, but
# the PD loop underneath it works from the raw meter, where that charging is
# indistinguishable from house load. Measured on the reference installation at
# 21:39 with no sun: the EMMA drew 211 W into the hybrid while the Marstek
# discharged 912 W against a 787 W house. The excess was battery-to-battery,
# through two conversions, topped up from the grid by the other manager.

def _ceiling(new_power, grid_w, batteries, deadband=0):
    from custom_components.omnibattery import _apply_discharge_ceiling

    controller = _controller(batteries)
    controller.deadband = deadband
    return _apply_discharge_ceiling(controller, new_power, grid_w)


def test_a_discharge_beyond_the_uncovered_load_is_trimmed():
    """The night this was found: 912 W commanded against a 787 W house."""
    batteries = [
        _battery("Marstek", ac_power=912),     # discharging
        _battery("Huawei", ac_power=-211),     # charged by its own manager
    ]
    # Meter: 787 house + 211 into the hybrid - 912 from the Marstek.
    assert _ceiling(-912.0, 86.0, batteries) == -787.0


def test_a_discharge_within_the_load_is_left_alone():
    batteries = [
        _battery("Marstek", ac_power=600),
        _battery("Huawei", ac_power=-211),
    ]
    # Uncovered is still 787; asking for 600 of it is nobody's business to trim.
    assert _ceiling(-600.0, 398.0, batteries) == -600.0


def test_the_ceiling_does_not_touch_charging():
    batteries = [_battery("Marstek", ac_power=0)]
    assert _ceiling(1500.0, 1500.0, batteries) == 1500.0


def test_a_surplus_is_left_to_the_guard():
    """It vetoes rather than caps, and two hands on the same command is worse."""
    batteries = [_battery("Marstek", ac_power=0)]
    assert _ceiling(-400.0, -900.0, batteries) == -400.0


def test_without_a_readable_battery_nothing_is_trimmed():
    batteries = [_battery("Marstek", ac_power=None)]
    assert _ceiling(-900.0, 500.0, batteries) == -900.0


def test_noise_around_the_demand_does_not_clamp_every_cycle():
    batteries = [_battery("Marstek", ac_power=800)]
    # Uncovered 787, commanded 800: inside the deadband, so left as it is.
    assert _ceiling(-800.0, -13.0, batteries, deadband=20) == -800.0
    # Well past it, so trimmed.
    assert _ceiling(-900.0, -113.0, batteries, deadband=20) == -687.0
