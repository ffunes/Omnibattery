"""The contiguous-block table is derived, not hand-maintained (follows #361).

Block reads were introduced with a table written by hand, which caught the
three spans somebody looked at and left the rest reading one register per
frame. The same adjacency rule applied to every polled register finds more:
on a Venus D the four MPPT powers sit at 30037..30040 and were four separate
requests every two seconds.

What is pinned here is the rule, not a particular register map:

* only registers that are already adjacent are grouped, never across a gap,
  so an unmapped address can never be pulled into a block — the property the
  hand-written tables were careful about and the reason a block cannot start
  failing as a whole;
* members of one block share a scan interval, because a block is scheduled
  and fetched as one unit;
* every span the hand-written table declared is still covered, so the change
  can only add batching, never take it away;
* a lone register stays a single read rather than becoming a one-member block.
"""
from __future__ import annotations

import pytest

from custom_components.omnibattery.const import (
    REGISTER_BLOCKS_V2,
    REGISTER_BLOCKS_V3,
    REGISTER_BLOCKS_VA_PACK_CELLS,
)
from custom_components.omnibattery.drivers.marstek import (
    _MAX_BLOCK_REGISTERS,
    _derive_register_blocks,
    _load_definitions,
    _register_width,
)


def _definitions(version: str) -> list[dict]:
    """Exactly what the driver derives from, disabled entities included.

    Polling does not follow entity enablement - the per-pack registers are a
    disabled-by-default entity each and are read all the same - so filtering
    here would test a table the driver never builds.
    """
    return _load_definitions(version)["all"]


def _vd_definitions() -> list[dict]:
    return _definitions("vD")


def test_members_are_adjacent_and_never_span_a_gap() -> None:
    """The safety property the hand-written tables were built on."""
    for block in _derive_register_blocks(_vd_definitions()):
        covered: set[int] = set()
        for member in block["members"]:
            start = block["start"] + member["offset"]
            covered.update(range(start, start + member["count"]))
        expected = set(range(block["start"], block["start"] + block["count"]))
        assert covered == expected, f"block at {block['start']} spans a gap: {block}"


def test_members_share_a_scan_interval() -> None:
    by_key = {d["key"]: d for d in _vd_definitions()}
    for block in _derive_register_blocks(_vd_definitions()):
        intervals = {by_key[m["key"]].get("scan_interval") for m in block["members"]}
        assert intervals == {block["scan_interval"]}


def test_blocks_stay_within_the_modbus_limit() -> None:
    for block in _derive_register_blocks(_vd_definitions()):
        assert 2 <= block["count"] <= _MAX_BLOCK_REGISTERS


def test_no_single_member_blocks() -> None:
    """A one-member block reads like the per-register path it would replace."""
    for block in _derive_register_blocks(_vd_definitions()):
        assert len(block["members"]) >= 2


def test_every_key_appears_at_most_once() -> None:
    seen: set[str] = set()
    for block in _derive_register_blocks(_vd_definitions()):
        for member in block["members"]:
            assert member["key"] not in seen, f"{member['key']} is in two blocks"
            seen.add(member["key"])


@pytest.mark.parametrize(
    ("hand_written", "definitions"),
    [
        (REGISTER_BLOCKS_V3, _vd_definitions()),
        (REGISTER_BLOCKS_V2, _definitions("v2")),
        (REGISTER_BLOCKS_VA_PACK_CELLS, _definitions("vA")),
    ],
)
def test_derivation_covers_what_was_written_by_hand(hand_written, definitions) -> None:
    """Whatever the tables declared must still be fetched in one request."""
    derived = _derive_register_blocks(definitions)
    for block in hand_written:
        wanted = {member["key"] for member in block["members"]}
        assert any(
            wanted <= {m["key"] for m in candidate["members"]} for candidate in derived
        ), f"span at {block['start']} is no longer batched"


def test_venus_d_batches_the_mppt_powers_and_the_energy_counters() -> None:
    """The two spans the hand-written table missed, named so a change is visible."""
    derived = {b["start"]: b for b in _derive_register_blocks(_vd_definitions())}

    mppt = derived.get(30037)
    assert mppt is not None, "the four MPPT powers are no longer batched"
    assert [m["key"] for m in mppt["members"]] == [
        "mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power",
    ]
    assert mppt["scan_interval"] == "high"

    energy = derived.get(33000)
    assert energy is not None, "the lifetime energy counters are no longer batched"
    assert {m["key"] for m in energy["members"]} == {
        "total_charging_energy", "total_discharging_energy",
    }
    # Two 32-bit counters, so four registers rather than two.
    assert energy["count"] == 4


def test_register_width_follows_the_client_default() -> None:
    """Widths must match what MarstekModbusClient would have read per key."""
    assert _register_width({"data_type": "uint16"}) == 1
    assert _register_width({"data_type": "int32"}) == 2
    assert _register_width({"data_type": "uint32"}) == 2
    assert _register_width({"data_type": "uint16", "count": 5}) == 5


def test_definitions_without_a_register_are_ignored() -> None:
    derived = _derive_register_blocks(
        [
            {"key": "a", "register": 10, "scan_interval": "high"},
            {"key": "computed", "scan_interval": "high"},
            {"key": "b", "register": 11, "scan_interval": "high"},
        ]
    )
    assert len(derived) == 1
    assert [m["key"] for m in derived[0]["members"]] == ["a", "b"]


def test_a_gap_of_one_register_still_splits() -> None:
    derived = _derive_register_blocks(
        [
            {"key": "a", "register": 10, "scan_interval": "high"},
            {"key": "b", "register": 12, "scan_interval": "high"},
        ]
    )
    assert derived == []


def test_the_same_registers_at_different_intervals_do_not_merge() -> None:
    derived = _derive_register_blocks(
        [
            {"key": "fast", "register": 10, "scan_interval": "high"},
            {"key": "slow", "register": 11, "scan_interval": "low"},
        ]
    )
    assert derived == []


def test_only_va_and_vd_get_the_per_pack_block() -> None:
    """#439: a v3 shares the entity map but not the 34000 registers.

    The hand-written table had to be told this - the pack blocks were appended
    for vA/vD only, because a group is built unconditionally and a v3 would
    have spent a failing read on it every cycle. Derivation settles it by
    itself, and this is the test that says so.
    """
    v3 = _derive_register_blocks(_definitions("v3"))
    assert [b for b in v3 if 34000 <= b["start"] < 35000] == []
