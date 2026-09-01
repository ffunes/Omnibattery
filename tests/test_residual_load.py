"""Tests for the meter-side reconstruction and the three guards on it.

The situation these exist for: a 7 kW hybrid inverter running its own
self-consumption shares one meter with a 2.5 kW AC battery. The hybrid removes
the grid error before the PD loop ever sees one, so the loop correctly commands
nothing and the AC battery never runs -- the grid looks perfect while half the
storage sits idle. Every number here was measured on that installation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.omnibattery.control.residual_load import (
    apply_guards,
    feedforward_candidate_w,
    feedforward_w,
    grid_reading_w,
    guards_pending,
    primary_coordinator,
    residual_demand_w,
    surplus_blocks_discharge,
    uncovered_load_w,
)


def _battery(name, *, ac_power=None, battery_power=None, soc=50, available=True):
    data = {"battery_soc": soc}
    if ac_power is not None:
        data["ac_power"] = ac_power
    if battery_power is not None:
        data["battery_power"] = battery_power
    return SimpleNamespace(name=name, data=data, is_available=available)


def _controller(
    batteries,
    *,
    primary="",
    enabled=True,
    limit=2500,
    target=0.0,
    deadband=0,
):
    return SimpleNamespace(
        coordinators=batteries,
        primary_battery=primary,
        primary_feedforward_enabled=enabled,
        compute_active_target=lambda: target,
        deadband=deadband,
        previous_power=0.0,
        _surplus_guard_latched=False,
        _active_discharge_batteries=[],
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _eid: None)),
        _battery_power_limit=lambda coordinator, is_charging: limit,
    )


# ----------------------------------------------------------------------
# the reconstruction
#
# "grid + sum(ac_power)" removes each battery's own contribution from the meter
# reading. What is left is the load the batteries actually have to cover: house
# consumption less whatever PV is already supplying, arrived at without needing
# either figure.
# ----------------------------------------------------------------------
def test_after_dark_the_uncovered_load_is_the_house_load():
    controller = _controller([
        _battery("Marstek", ac_power=665),
        _battery("Huawei", ac_power=44),
    ])
    assert uncovered_load_w(controller, -40.0) == 669.0


def test_another_battery_charging_is_not_a_load_to_cover():
    """Its charging shows up at the meter, but nobody must chase it."""
    controller = _controller([
        _battery("Marstek", ac_power=0),
        _battery("Huawei", ac_power=-500),   # charging 500 W from the grid
    ])
    # Meter reads the 600 W house plus that 500 W. Only the 600 is the fleet's job.
    assert uncovered_load_w(controller, 1100.0) == 600.0


def test_pv_covering_the_house_shows_as_a_negative_load():
    """1188 W of PV over a 570 W house, the primary discharging 350 W while the
    other took in 920 W. The house load is the wrong quantity to feed forward:
    under sun the roof covers it, and commanding the primary to supply it anyway
    discharges one battery into the other."""
    controller = _controller([
        _battery("Marstek", battery_power=-350),
        _battery("Huawei", battery_power=920),
    ])
    assert uncovered_load_w(controller, 16.0) == pytest.approx(-554.0)
    # So the primary is asked for no discharge at all -- if anything, a charge.
    assert feedforward_w(controller, 16.0) >= 0


# --- defect: a silent battery understates the demand by exactly its output ---
def test_one_unreadable_battery_makes_the_whole_reconstruction_unusable():
    """The hardware holds its last command, so a battery off comms is still
    delivering: its output stays inside the meter reading while its term drops
    out of the sum. A partial reconstruction is not a weaker version of the
    quantity, it is a wrong one."""
    controller = _controller([
        _battery("Huawei", ac_power=800, available=False),   # still delivering
        _battery("Marstek", ac_power=200),
    ])
    assert uncovered_load_w(controller, -200.0) is None


def test_a_battery_reporting_no_power_at_all_is_unreadable_too():
    controller = _controller([_battery("Marstek")])
    assert uncovered_load_w(controller, 500.0) is None


def test_nothing_is_trimmed_while_the_fleet_cannot_be_reconstructed():
    """Which is the point of the None: the ceiling would otherwise trim the
    healthy battery down to a demand short by the silent one's output."""
    controller = _controller([
        _battery("Huawei", ac_power=800, available=False),
        _battery("Marstek", ac_power=900),
    ])
    assert apply_guards(controller, -900.0, 100.0) == -900.0


def test_a_missing_meter_reading_is_not_a_zero_load():
    assert uncovered_load_w(_controller([_battery("Marstek", ac_power=0)]), None) is None


# ----------------------------------------------------------------------
# the guards measure against the active target, never against zero
#
# curtailment_predischarge sets a negative override on purpose to build headroom
# before a negative-price window. Guards anchored at 0 veto it outright under
# sun and trim it to house load at night.
# ----------------------------------------------------------------------
def test_a_deliberate_predischarge_survives_the_ceiling_at_night():
    """2500 W of predischarge against a 787 W house. Anchored at zero the
    ceiling cut it to 787 W on the reference installation."""
    controller = _controller(
        [_battery("Marstek", ac_power=787)], target=-2500.0, enabled=False
    )
    assert uncovered_load_w(controller, 0.0) == 787.0
    assert residual_demand_w(controller, 0.0) == 3287.0
    assert apply_guards(controller, -2500.0, 0.0) == -2500.0

    # And the same command against a zero target is trimmed, as it should be.
    controller.compute_active_target = lambda: 0.0
    assert apply_guards(controller, -2500.0, 0.0) == -787.0


def test_a_deliberate_predischarge_survives_the_surplus_guard_under_sun():
    """A surplus plus an export target is still a demand on the batteries."""
    controller = _controller(
        [_battery("Marstek", ac_power=0)], target=-2500.0, enabled=False
    )
    # 1000 W of PV nobody is using: the raw uncovered load is negative...
    assert uncovered_load_w(controller, -1000.0) == -1000.0
    # ...but 1500 W of discharge is still wanted to reach the target.
    assert residual_demand_w(controller, -1000.0) == 1500.0
    assert surplus_blocks_discharge(controller, -1000.0) is False
    assert apply_guards(controller, -1500.0, -1000.0) == -1500.0

    # Anchored at zero instead, the same surplus vetoes the whole command.
    controller.compute_active_target = lambda: 0.0
    controller._surplus_guard_latched = False
    assert apply_guards(controller, -1500.0, -1000.0) == 0


# ----------------------------------------------------------------------
# the surplus guard
#
# 1391 W of PV over a 529 W house, one battery taking in 1110 W while the other
# gave up 205 W, and the meter at 3 W: 829 W of surplus making a round trip
# through two conversion losses while the deadband held the standing command.
# ----------------------------------------------------------------------
def _round_trip():
    return _controller([
        _battery("Marstek", battery_power=-205),   # discharging, commanded by us
        _battery("Huawei", battery_power=1110),    # charging, commanded by nobody
    ], enabled=False)


def test_a_discharge_into_a_surplus_is_refused():
    controller = _round_trip()
    assert uncovered_load_w(controller, 3.0) < 0
    assert apply_guards(controller, -165.0, 3.0) == 0


def test_charging_into_a_surplus_is_exactly_right():
    """The guard is one-directional: absorbing surplus is the point."""
    assert apply_guards(_round_trip(), 800.0, 3.0) == 800.0


def test_a_real_deficit_still_discharges():
    """After dark the guard has to keep out of the way."""
    controller = _controller([
        _battery("Marstek", ac_power=665),
        _battery("Huawei", ac_power=44),
    ], deadband=40, enabled=False)
    assert surplus_blocks_discharge(controller, -40.0) is False
    assert apply_guards(controller, -700.0, -40.0) == -700.0


def _drift(controller, series):
    """Walk a sequence of uncovered-load values past the guard.

    One battery, its own output the only thing between the grid and the house.
    """
    verdicts = []
    for uncovered in series:
        controller.coordinators = [_battery("Marstek", ac_power=uncovered)]
        verdicts.append(surplus_blocks_discharge(controller, 0.0))
    return verdicts


def test_noise_around_zero_does_not_toggle_the_guard():
    controller = _controller([], deadband=40, enabled=False)
    assert _drift(controller, [-30, 20, -60, 40, -90, 10]) == [False] * 6


def test_a_clear_surplus_engages_it_and_a_real_deficit_releases_it():
    controller = _controller([], deadband=40, enabled=False)
    #        clear surplus       drifting back up      real deficit
    series = [-800, -400, -120,  -60, -10, -30,        120, 300]
    assert _drift(controller, series) == [
        True, True, True,
        True, True, True,     # inside the band the verdict is held
        False, False,
    ]


def test_the_band_follows_the_configured_deadband():
    """Somebody who widened the deadband widened their idea of meter noise."""
    controller = _controller([], deadband=500, enabled=False)
    assert _drift(controller, [-300]) == [False]
    assert _drift(controller, [-800]) == [True]


def test_a_missing_reading_leaves_the_verdict_where_it_was():
    controller = _controller([_battery("Marstek", available=False)], enabled=False)
    controller._surplus_guard_latched = True
    assert surplus_blocks_discharge(controller, 0.0) is True
    controller._surplus_guard_latched = False
    assert surplus_blocks_discharge(controller, 0.0) is False


# ----------------------------------------------------------------------
# the discharge ceiling
#
# 21:39, no sun: the EMMA drew 211 W to charge the hybrid while the AC battery
# discharged 912 W against a 787 W house. The difference was chasing a load that
# was never the house's -- battery into battery, through two conversions, topped
# up from the grid by the other manager.
# ----------------------------------------------------------------------
def _at_2139(marstek_ac=912):
    return _controller([
        _battery("Marstek", ac_power=marstek_ac),   # discharging
        _battery("Huawei", ac_power=-211),          # charged by its own manager
    ], enabled=False)


def test_a_discharge_beyond_the_uncovered_load_is_trimmed():
    controller = _at_2139()
    # Meter: 787 house + 211 into the hybrid - 912 out of the Marstek.
    assert uncovered_load_w(controller, 86.0) == 787.0
    assert apply_guards(controller, -912.0, 86.0) == -787.0


def test_a_discharge_within_the_load_is_left_alone():
    controller = _at_2139(marstek_ac=600)
    assert apply_guards(controller, -600.0, 398.0) == -600.0


def test_the_ceiling_does_not_touch_charging():
    controller = _controller([_battery("Marstek", ac_power=0)], enabled=False)
    assert apply_guards(controller, 1500.0, 1500.0) == 1500.0


def test_a_surplus_is_left_to_the_guard_which_vetoes_rather_than_caps():
    controller = _controller([_battery("Marstek", ac_power=0)], deadband=40, enabled=False)
    assert apply_guards(controller, -400.0, -900.0) == 0


def test_noise_around_the_demand_does_not_clamp_every_cycle():
    controller = _controller([_battery("Marstek", ac_power=800)], deadband=20, enabled=False)
    # Uncovered 787, commanded 800: inside the deadband, so left as it is.
    assert apply_guards(controller, -800.0, -13.0) == -800.0
    controller.coordinators = [_battery("Marstek", ac_power=900)]
    controller._surplus_guard_latched = False
    assert apply_guards(controller, -900.0, -113.0) == -787.0


# ----------------------------------------------------------------------
# the deadband shortcut must ask the guards, not assume
#
# The grid reads on target *because* the other regulator is carrying the load,
# which is the situation these guards exist to change. One pending, derived from
# the pipeline itself, so a guard added there later cannot be swallowed.
# ----------------------------------------------------------------------
def test_the_quiet_meter_does_not_hide_a_discharge_above_the_ceiling():
    """The 21:39 case, and the one a per-guard pending forgot."""
    controller = _at_2139()
    controller.previous_power = -912.0
    assert guards_pending(controller, 86.0) is True

    controller.previous_power = -787.0
    assert guards_pending(controller, 86.0) is False


def test_the_quiet_meter_does_not_hide_a_standing_discharge_into_surplus():
    controller = _round_trip()
    controller.previous_power = -205.0
    assert guards_pending(controller, 3.0) is True

    controller.previous_power = 0.0
    assert guards_pending(controller, 3.0) is False


def test_the_quiet_meter_does_not_hide_an_unmet_feedforward_floor():
    controller = _controller([
        _battery("Huawei", battery_power=-800),
        _battery("Marstek", ac_power=0),
    ], primary="Marstek")
    assert guards_pending(controller, 0.0) is True

    controller.previous_power = -800.0
    assert guards_pending(controller, 0.0) is False


def test_a_small_shortfall_does_not_reopen_the_cycle():
    """Below the tolerance a correction is not worth a write."""
    controller = _controller([
        _battery("Huawei", battery_power=-850),
        _battery("Marstek", ac_power=0),
    ], primary="Marstek")
    controller.previous_power = -800.0
    assert guards_pending(controller, 0.0) is False


def test_the_switch_being_off_leaves_the_feedforward_out_of_the_pending():
    controller = _controller(
        [_battery("Huawei", battery_power=-800), _battery("Marstek", ac_power=0)],
        primary="Marstek",
        enabled=False,
    )
    assert guards_pending(controller, 0.0) is False


# ----------------------------------------------------------------------
# which battery is the primary
# ----------------------------------------------------------------------
def test_without_a_nomination_the_ordinary_choice_is_used():
    """Switching the feedforward on alone changes when a battery is asked, not
    which one: it addresses the battery the ladder would have picked anyway."""
    fuller = _battery("Marstek", ac_power=0, soc=70)
    emptier = _battery("Huawei", battery_power=-800, soc=30)
    controller = _controller([emptier, fuller], primary="")
    assert primary_coordinator(controller) is fuller
    assert feedforward_w(controller, 0.0) == -800.0


def test_the_automatic_choice_reads_the_ladder_key_not_the_raw_soc():
    """The ladder gives the running battery a 5 % edge so a pair sitting close
    together does not swap every cycle. Picking the primary on raw SOC would
    outrank that edge and bring the swapping straight back."""
    running = _battery("Huawei", battery_power=-800, soc=58)
    fuller = _battery("Marstek", ac_power=0, soc=60)
    controller = _controller([running, fuller], primary="")
    controller._active_discharge_batteries = [running]
    assert primary_coordinator(controller) is running


def test_the_edge_is_only_an_edge_and_a_clearly_fuller_battery_still_wins():
    running = _battery("Huawei", battery_power=-800, soc=58)
    fuller = _battery("Marstek", ac_power=0, soc=70)
    controller = _controller([running, fuller], primary="")
    controller._active_discharge_batteries = [running]
    assert primary_coordinator(controller) is fuller


def test_a_name_that_no_longer_matches_falls_back_rather_than_going_quiet():
    fuller = _battery("Marstek", ac_power=0, soc=70)
    controller = _controller([fuller], primary="Venus 9")
    assert primary_coordinator(controller) is fuller


def test_a_battery_in_manual_mode_is_never_the_primary_even_when_nominated():
    """The distribution leaves a manual battery out of the ladder, so a
    feedforward measured against its rating would be handed to the batteries
    that were left."""
    manual = _battery("Marstek", ac_power=0, soc=90)
    manual.battery_manual_mode_enabled = True
    automatic = _battery("Huawei", battery_power=-800, soc=40)
    controller = _controller([manual, automatic], primary="Marstek")
    assert primary_coordinator(controller) is automatic


def test_manual_mode_also_keeps_a_battery_out_of_the_automatic_choice():
    manual = _battery("Marstek", ac_power=0, soc=90)
    manual.battery_manual_mode_enabled = True
    automatic = _battery("Huawei", battery_power=-800, soc=40)
    controller = _controller([manual, automatic], primary="")
    assert primary_coordinator(controller) is automatic


def test_a_fleet_that_cannot_discharge_has_no_primary():
    controller = _controller([_battery("Marstek", ac_power=0)], limit=0)
    assert primary_coordinator(controller) is None
    assert feedforward_w(controller, 500.0) == 0.0


# ----------------------------------------------------------------------
# the feedforward itself
# ----------------------------------------------------------------------
def test_the_floor_never_exceeds_what_the_primary_can_deliver():
    controller = _controller([
        _battery("Huawei", battery_power=-4000),
        _battery("Marstek", ac_power=0),
    ], primary="Marstek", limit=2500)
    assert feedforward_w(controller, 0.0) == -2500.0


def test_only_the_shortfall_is_fed_forward():
    """Half-covered by the roof means half from the battery, not all of it."""
    controller = _controller([
        _battery("Marstek", ac_power=0),
        _battery("Huawei", ac_power=0),
    ], primary="Marstek")
    assert feedforward_w(controller, 600.0) == -600.0


def test_the_switch_gates_the_command_but_not_the_reading():
    """The figure has to be inspectable before it is acted on."""
    controller = _controller([
        _battery("Huawei", battery_power=-800),
        _battery("Marstek", ac_power=0),
    ], primary="Marstek", enabled=False)
    assert feedforward_candidate_w(controller, 0.0) == -800.0
    assert feedforward_w(controller, 0.0) == 0.0


def test_the_command_is_floored_but_a_larger_discharge_is_left_alone():
    controller = _controller([
        _battery("Huawei", battery_power=-800),
        _battery("Marstek", ac_power=0),
    ], primary="Marstek")
    # An idle loop is raised to cover the demand...
    assert apply_guards(controller, 0.0, 0.0) == -800.0
    # ...and so is a charge command, which would otherwise fight it.
    assert apply_guards(controller, 500.0, 0.0) == -800.0
    # It is a floor and not a target, so it never trims a larger discharge...
    from custom_components.omnibattery.control.residual_load import _apply_feedforward

    assert _apply_feedforward(controller, -2000.0, 0.0) == -2000.0
    # ...but the ceiling below it does, because 2000 W against an 800 W demand
    # is exactly the battery-into-battery discharge the ceiling exists for.
    assert apply_guards(controller, -2000.0, 0.0) == -800.0


# --- the surplus side -------------------------------------------------------
#
# 5834 W of sun, the hybrid at 83 % quietly absorbing all of it, and the battery
# meant to be filled first sitting at 17 %, untouched.
def _surplus_taken_by_the_other():
    controller = _controller([
        _battery("Marstek", ac_power=13),       # idle
        _battery("Huawei", ac_power=-5187),     # charging, commanded by nobody
    ], primary="Marstek")
    controller._battery_power_limit = lambda coordinator, is_charging: (
        2500 if coordinator.name == "Marstek" else 7000
    )
    return controller


def test_the_whole_surplus_is_offered_not_one_batterys_worth():
    """The floor is a system figure; the distribution shares it out after.
    Capped at the head battery's own rating it ends up with only its share of
    its own limit -- 2418 W of 2500 with 6.8 kW of surplus going past it."""
    controller = _surplus_taken_by_the_other()
    assert uncovered_load_w(controller, 67.0) == -5107.0
    assert feedforward_w(controller, 67.0) == 5107.0


def test_the_offer_never_exceeds_what_the_fleet_can_take():
    controller = _surplus_taken_by_the_other()
    controller._battery_power_limit = lambda coordinator, is_charging: (
        2500 if coordinator.name == "Marstek" else 0
    )
    assert feedforward_w(controller, 67.0) == 2500.0


def test_no_surplus_means_nothing_is_asked_for_however_much_room_there_is():
    """Both batteries have room; the sun does not. Asking anyway would move
    energy out of one and into the other through two conversions -- a battery
    behind the same meter is ordinary household load to another regulator, so a
    charge command is never refused."""
    controller = _controller([
        _battery("Marstek", ac_power=0),        # empty and idle
        _battery("Huawei", ac_power=250),       # discharging to cover the house
    ], primary="Marstek")
    assert uncovered_load_w(controller, 0.0) == 250.0
    assert feedforward_w(controller, 0.0) == -250.0


def test_only_the_uncovered_part_of_the_surplus_is_claimed():
    """The other battery's own absorption must not be counted as available."""
    controller = _controller([
        _battery("Marstek", ac_power=0),
        _battery("Huawei", ac_power=-1500),     # already absorbing 1500 W
    ], primary="Marstek")
    # Meter exports 2000 W on top of the 1500 W the other battery is taking.
    assert uncovered_load_w(controller, -2000.0) == -3500.0
    # So 3500 W may be claimed -- not the fleet's 5000 W of room.
    assert feedforward_w(controller, -2000.0) == 3500.0


def test_a_manual_battery_adds_no_room_to_the_surplus_offer():
    """It is outside the ladder, so its rating would be offered to batteries
    that never had it."""
    manual = _battery("Huawei", ac_power=0)
    manual.battery_manual_mode_enabled = True
    controller = _controller([_battery("Marstek", ac_power=0), manual], limit=2500)
    assert feedforward_w(controller, -6000.0) == 2500.0


# ----------------------------------------------------------------------
# the diagnostic reading
# ----------------------------------------------------------------------
def test_the_diagnostic_survives_a_cleared_cycle_reading():
    """previous_sensor is dropped whenever another manager takes the wheel. A
    max-SOC charge does exactly that, and a diagnostic that blanks out while
    something interesting is happening is no diagnostic."""
    controller = _controller([_battery("Marstek", ac_power=0)])
    controller.consumption_sensor = "sensor.grid"
    controller.meter_inverted = False
    controller.previous_sensor = None
    controller.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _eid: SimpleNamespace(state="-66.5"))
    )
    assert grid_reading_w(controller) == pytest.approx(-66.5)

    controller.previous_sensor = 12.0
    assert grid_reading_w(controller) == 12.0


def test_an_inverted_meter_is_honoured_by_the_diagnostic():
    controller = _controller([_battery("Marstek", ac_power=0)])
    controller.consumption_sensor = "sensor.grid"
    controller.meter_inverted = True
    controller.previous_sensor = None
    controller.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _eid: SimpleNamespace(state="500"))
    )
    assert grid_reading_w(controller) == -500.0


def test_an_unavailable_meter_reports_nothing_rather_than_zero():
    controller = _controller([_battery("Marstek", ac_power=0)])
    controller.consumption_sensor = "sensor.grid"
    controller.meter_inverted = False
    controller.previous_sensor = None
    controller.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _eid: SimpleNamespace(state="unavailable"))
    )
    assert grid_reading_w(controller) is None


# ----------------------------------------------------------------------
# the entities carry the figures whether or not the feature is on
# ----------------------------------------------------------------------
def test_the_diagnostics_refresh_themselves():
    """Their figures live on the controller and change every cycle. Unpolled, an
    entity's attributes freeze at the last state write -- for a switch that is
    the moment somebody toggled it."""
    import inspect

    from custom_components.omnibattery.select import ChargePrioritySelect
    from custom_components.omnibattery.switch import PrimaryFeedforwardSwitch

    for cls in (PrimaryFeedforwardSwitch, ChargePrioritySelect):
        assert getattr(cls, "_attr_should_poll", False) or (
            "_attr_should_poll = True" in inspect.getsource(cls.__init__)
        ), cls.__name__
        assert "extra_state_attributes" in inspect.getsource(cls), cls.__name__


def test_the_new_entities_are_named_in_every_language():
    import glob
    import json

    for path in ["custom_components/omnibattery/strings.json"] + sorted(
        glob.glob("custom_components/omnibattery/translations/*.json")
    ):
        entity = json.load(open(path, encoding="utf-8"))["entity"]
        for key in ("primary_battery", "charge_priority"):
            assert entity["select"][key]["name"], (path, key)
            # "automatic" is a state, not a battery name, so it needs one.
            assert entity["select"][key]["state"]["automatic"], (path, key)
        assert entity["switch"]["primary_feedforward"]["name"], path


def test_the_panel_offers_the_controls_in_every_language():
    """A setting nobody can find is a setting nobody uses, and a control missing
    from the section's items allowlist never renders at all."""
    import re

    panel = open(
        "custom_components/omnibattery/frontend/marstek-panel.js", encoding="utf-8"
    ).read()

    assert '{ key: "primary_battery", domain: "select"' in panel
    assert '{ key: "charge_priority", domain: "select"' in panel
    assert '{ key: "primary_feedforward", domain: "switch"' in panel
    for key in ("secPrimary", "itemPrimaryBattery", "itemPrimaryFeedforward",
                "itemChargePriority"):
        assert len(re.findall(r"\b%s:" % key, panel)) == 6, key
    for key in ("primary_battery", "primary_feedforward", "charge_priority"):
        assert len(re.findall(r'^    %s: "' % key, panel, re.M)) == 6, key
