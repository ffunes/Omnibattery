"""Tests for MAC-based tracking of a battery's IP address.

Written without the ``hass`` fixture on purpose: ``pytest.ini`` disables the
Home Assistant plugin, and CI runs a bare ``pytest``, so a test that asked for
``hass`` would be skipped everywhere and prove nothing. The decision logic lives
in a pure module and the flow method is exercised against small stubs, so every
assertion here actually runs.
"""

from __future__ import annotations

import types

import pytest

from custom_components.omnibattery.infra.mac_tracking import (
    AMBIGUOUS_MAC,
    CONF_MAC,
    CONF_TRACK_MAC,
    ENDPOINT_CONFLICT,
    INVALID_MAC,
    NOT_IP_BASED,
    NO_MATCH,
    OK,
    STILL_REACHABLE,
    UNCHANGED,
    detect_mac,
    evaluate_lease,
    publishable_macs,
    is_ip_based,
    normalise_mac,
    tracking_enabled,
)

MAC_A = "dc:04:5a:14:6b:33"
MAC_B = "dc:04:5a:7d:de:c6"


def battery(
    host="192.168.1.181",
    port=502,
    slave_id=1,
    brand="marstek",
    track=True,
    mac=MAC_A,
    serial_port="",
):
    """Build a battery entry in the shape stored in ``entry.data["batteries"]``."""
    return {
        "name": "Battery",
        "host": host,
        "port": port,
        "slave_id": slave_id,
        "brand": brand,
        "serial_port": serial_port,
        CONF_TRACK_MAC: track,
        CONF_MAC: mac,
    }


# --- normalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "dc:04:5a:14:6b:33",
        "DC:04:5A:14:6B:33",
        "dc-04-5a-14-6b-33",
        "dc04.5a14.6b33",
        "DC045A146B33",
    ],
)
def test_normalise_mac_accepts_every_shape_routers_produce(raw):
    """Home Assistant delivers bare hex, humans type colons: both must match."""
    assert normalise_mac(raw) == MAC_A


@pytest.mark.parametrize("raw", ["", "not-a-mac", "dc:04:5a:14:6b", "zz:04:5a:14:6b:33", None, 42])
def test_normalise_mac_rejects_everything_else(raw):
    assert normalise_mac(raw) is None


# --- 1. disabled is a strict no-op -----------------------------------------


def test_disabled_tracking_ignores_the_lease():
    """The opt-in is off by default; an untouched install must not move."""
    batteries = [battery(track=False)]
    assert not tracking_enabled(batteries[0])
    verdict = evaluate_lease(batteries, MAC_A, "192.168.1.180")
    assert verdict.reason == NO_MATCH
    assert not verdict.should_update


def test_enabled_without_a_mac_is_also_a_no_op():
    """Ticking the box without a MAC leaves nothing to match on."""
    batteries = [battery(mac="")]
    assert not tracking_enabled(batteries[0])
    assert evaluate_lease(batteries, MAC_A, "192.168.1.180").reason == NO_MATCH


# --- 2. the nominal move ---------------------------------------------------


def test_new_address_for_a_tracked_battery_is_accepted():
    batteries = [battery(host="192.168.1.181")]
    verdict = evaluate_lease(batteries, MAC_A, "192.168.1.180")
    assert verdict.should_update
    assert verdict.reason == OK
    assert verdict.index == 0


def test_match_survives_a_differently_formatted_mac():
    """A MAC typed with colons must match the bare hex Home Assistant reports."""
    batteries = [battery(mac="DC-04-5A-14-6B-33")]
    assert evaluate_lease(batteries, "dc045a146b33", "192.168.1.180").should_update


def test_the_right_battery_is_picked_among_several():
    batteries = [battery(host="192.168.1.64", mac=MAC_B), battery(host="192.168.1.181", mac=MAC_A)]
    assert evaluate_lease(batteries, MAC_A, "192.168.1.180").index == 1


# --- 3. ambiguous MAC: shared gateway --------------------------------------


def test_one_mac_on_two_batteries_is_refused():
    """Batteries behind one Modbus gateway share the gateway's MAC.

    The MAC then belongs to the gateway rather than to either battery, so it
    cannot say which one moved. Abstaining is the only safe answer.
    """
    batteries = [
        battery(host="192.168.1.50", slave_id=1, mac=MAC_A),
        battery(host="192.168.1.50", slave_id=2, mac=MAC_A),
    ]
    verdict = evaluate_lease(batteries, MAC_A, "192.168.1.51")
    assert verdict.reason == AMBIGUOUS_MAC
    assert not verdict.should_update


# --- 4. endpoint already taken ---------------------------------------------


def test_moving_onto_another_batterys_endpoint_is_refused():
    """Two entries on one endpoint would send battery A's commands to B."""
    batteries = [
        battery(host="192.168.1.181", mac=MAC_A),
        battery(host="192.168.1.180", mac=MAC_B),
    ]
    verdict = evaluate_lease(batteries, MAC_A, "192.168.1.180")
    assert verdict.reason == ENDPOINT_CONFLICT
    assert not verdict.should_update


def test_same_address_but_a_different_slave_id_is_not_a_conflict():
    """A Modbus gateway legitimately hosts several batteries on one host:port."""
    batteries = [
        battery(host="192.168.1.181", slave_id=1, mac=MAC_A),
        battery(host="192.168.1.50", slave_id=2, mac=MAC_B),
    ]
    assert evaluate_lease(batteries, MAC_A, "192.168.1.50").should_update


# --- 5. a renewed lease for the current address ----------------------------


def test_a_lease_for_the_address_already_configured_changes_nothing():
    """Leases are renewed periodically; reloading on each renewal is pure cost."""
    batteries = [battery(host="192.168.1.181")]
    verdict = evaluate_lease(batteries, MAC_A, "192.168.1.181")
    assert verdict.reason == UNCHANGED
    assert not verdict.should_update


# --- 6. brands that have no IP to track ------------------------------------


@pytest.mark.parametrize("brand", ["esphome", "hoymiles"])
def test_non_ip_brands_are_left_alone(brand):
    """ESPHome and Hoymiles are addressed by device id, not by IP."""
    batteries = [battery(brand=brand, host="some-device-id")]
    assert not is_ip_based(batteries[0])
    assert evaluate_lease(batteries, MAC_A, "192.168.1.180").reason == NOT_IP_BASED


def test_a_serial_marstek_is_left_alone():
    """Serial stores a device path in host; there is no address to update."""
    batteries = [battery(host="/dev/ttyUSB0", serial_port="/dev/ttyUSB0")]
    assert not is_ip_based(batteries[0])
    assert evaluate_lease(batteries, MAC_A, "192.168.1.180").reason == NOT_IP_BASED


@pytest.mark.parametrize("brand", ["marstek", "zendure", "anker", "sessy", "indevolt"])
def test_the_ip_brands_are_tracked(brand):
    assert is_ip_based(battery(brand=brand))


# --- 7. a MAC that is not a MAC --------------------------------------------


def test_a_malformed_mac_on_the_lease_is_refused():
    assert evaluate_lease([battery()], "nonsense", "192.168.1.180").reason == INVALID_MAC


def test_an_empty_new_address_is_refused():
    assert evaluate_lease([battery()], MAC_A, "  ").reason == INVALID_MAC


# --- the two-addresses-at-once case ----------------------------------------


def test_a_battery_still_answering_is_not_moved():
    """A device can hold two addresses at once — measured on a Marstek unit.

    While the configured address still answers, a lease for a second address is
    extra information rather than a move. Switching would trade a working
    endpoint for an untested one, and could flap between the two.
    """
    batteries = [battery(host="192.168.1.181")]
    verdict = evaluate_lease(batteries, MAC_A, "192.168.1.180", is_reachable=lambda _i: True)
    assert verdict.reason == STILL_REACHABLE
    assert not verdict.should_update


def test_a_battery_that_has_gone_quiet_is_moved():
    batteries = [battery(host="192.168.1.181")]
    assert evaluate_lease(batteries, MAC_A, "192.168.1.180", is_reachable=lambda _i: False).should_update


# --- automatic MAC detection ------------------------------------------------


def _lease(ip, macaddress):
    return types.SimpleNamespace(ip=ip, macaddress=macaddress)


def test_detect_mac_finds_the_configured_host():
    discovered = [_lease("192.168.1.64", "dc045a7ddec6"), _lease("192.168.1.181", "dc045a146b33")]
    assert detect_mac(discovered, "192.168.1.181") == MAC_A


def test_detect_mac_returns_none_when_home_assistant_never_saw_the_device():
    """Normal on installs where Home Assistant is not on the batteries' network."""
    assert detect_mac([_lease("192.168.1.64", "dc045a7ddec6")], "192.168.1.181") is None


# --- what may be published to the device registry ---------------------------
# Home Assistant indexes devices by connections as well as by identifiers, so a
# MAC published twice merges two batteries into one device — at registration,
# long before any lease is evaluated.


def test_a_unique_mac_is_published():
    assert publishable_macs([battery(mac=MAC_A)]) == [MAC_A]


def test_a_mac_shared_by_two_batteries_is_published_for_neither():
    """The Modbus-gateway case: the MAC is the gateway's, not either battery's."""
    batteries = [
        battery(host="192.168.1.50", slave_id=1, mac=MAC_A),
        battery(host="192.168.1.50", slave_id=2, mac=MAC_A),
    ]
    assert publishable_macs(batteries) == [None, None]


def test_a_shared_mac_does_not_suppress_an_unrelated_battery():
    batteries = [
        battery(host="192.168.1.50", slave_id=1, mac=MAC_A),
        battery(host="192.168.1.50", slave_id=2, mac=MAC_A),
        battery(host="192.168.1.64", mac=MAC_B),
    ]
    assert publishable_macs(batteries) == [None, None, MAC_B]


def test_untracked_and_invalid_entries_publish_nothing():
    batteries = [battery(track=False, mac=MAC_A), battery(mac="nonsense"), battery(mac="")]
    assert publishable_macs(batteries) == [None, None, None]


def test_publication_is_blind_to_mac_formatting():
    """Two spellings of one MAC are still one MAC, and must both be withheld."""
    batteries = [battery(mac="DC-04-5A-14-6B-33"), battery(mac="dc045a146b33")]
    assert publishable_macs(batteries) == [None, None]


def test_a_shared_gateway_still_refuses_the_lease():
    """Belt and braces: the discovery guard holds even if a MAC did get published."""
    batteries = [
        battery(host="192.168.1.50", slave_id=1, mac=MAC_A),
        battery(host="192.168.1.50", slave_id=2, mac=MAC_A),
    ]
    assert evaluate_lease(batteries, MAC_A, "192.168.1.51").reason == AMBIGUOUS_MAC
