"""Which SOC a coupled-pack battery is judged by (issue #350).

A Venus A/D couples several battery packs and fills them **in sequence**, so its
aggregate SOC is not the number either end of the charge should be decided on:
it can read the ceiling while the last pack is still half empty. Per-pack SOC
(``battery_soc_pack_1..6``) makes the real state visible, and the verdicts
become asymmetric:

* **full** when the *least* full pack reaches the ceiling — ``min(pack_soc)``;
* **empty** when the *first* pack reaches the floor — ``min(pack_soc)`` again.

The floor is not the mirror of the ceiling, because the hardware is not
symmetric. Charging walks on to the next pack when one fills, which is what the
#350 handovers showed. Discharging does not: a Venus D stops the whole battery
the moment its first pack reaches the cutoff, and leaves the charge in the
others where it is.

Measured on a six-pack Venus D, 4 September, with the cutoff at 12 %::

    pack 1  12.1 %    pack 4  12.0 %      aggregate      15 %
    pack 2  12.0 %    pack 5  20.3 %      device usable  3.4 %
    pack 3  19.0 %    pack 6  19.3 %      delivered      0 W

``max(pack_soc)`` reads 20.3 % there and says keep going, so the control layer
commanded ~938 W into a battery that had already stopped, for six hours, while
the house imported. ``min(pack_soc)`` reads 12.0 %, which is the cutoff, and is
the number the device itself acts on.

Both helpers fall back to the aggregate when the battery publishes no per-pack
telemetry, which is every model except Venus A/D and any Venus A/D slot that did
not answer the driver's probe. So a battery with one pack, or none exposed,
behaves exactly as it did before.
"""
from __future__ import annotations

# Pack telemetry keys are read by prefix rather than from a fixed list: the key
# shape is the contract, so a second brand exposing per-pack SOC the same way is
# picked up without touching the control layer.
_PACK_SOC_PREFIX = "battery_soc_pack_"


def pack_socs(coordinator) -> list[float]:
    """Return this battery's per-pack SOCs, empty when it publishes none.

    Values are bounded on read: the Venus A/D pack addresses come from a
    third-party register map, not from Marstek, and an out-of-range reading here
    would move a charge or discharge limit.
    """
    return [
        value
        for key, value in (getattr(coordinator, "data", None) or {}).items()
        if key.startswith(_PACK_SOC_PREFIX)
        and isinstance(value, (int, float))
        and 0 <= value <= 100
    ]


def soc_vs_ceiling(coordinator, aggregate):
    """SOC that decides whether to keep *charging*: the least full pack."""
    packs = pack_socs(coordinator)
    return min(packs) if packs else aggregate


def soc_vs_floor(coordinator, aggregate):
    """SOC that decides whether to keep *discharging*: the first pack to empty.

    Not the fullest one. The battery stops when any pack reaches the cutoff, so
    the charge left in the others is not available and must not be counted as
    though it were.
    """
    packs = pack_socs(coordinator)
    return min(packs) if packs else aggregate


# A pack this close to full has stopped taking charge, so from there the top
# cell reading is a fair proxy for the whole battery again. Same figure the
# BMS-cutoff detector has judged its taper clause on since #350.
_PACK_TOP_SOC = 99


def control_vmax(coordinator):
    """Max cell voltage that may decide for the *whole* battery (issue #415).

    Venus A/D publish register 37007 for pack 1 alone, not a fleet maximum:
    #415 measured 3.353 V there while another pack held a cell at 3.517 V, 155
    mV higher. On a battery that fills its packs in sequence a top cell reading
    therefore means "pack 1 has finished", not "the battery is at the top", and
    tapering or latching on it throttles a battery that is still half empty —
    measured as an hour at 196 W with the taper latched on a finished pack.

    A fleet maximum is not the fix either. Each pack balances on its own BMS, so
    one pack's outlier would drag the whole battery down: the #415 installation
    has a cell at 3.517 V at rest, 37 mV above the taper entry, which would hold
    the latch on permanently. What the reading needs is a qualifier, and it is
    the one the BMS-cutoff detector already applies (#350) — the top cell counts
    once the least full pack has caught up to it.

    Returns None while the packs still disagree, and the plain reading for every
    battery that publishes no per-pack SOC, which is every model except Venus
    A/D, so their behaviour is unchanged.
    """
    data = getattr(coordinator, "data", None) or {}
    try:
        vmax = float(data["max_cell_voltage"])
    except (KeyError, TypeError, ValueError):
        return None
    packs = pack_socs(coordinator)
    if packs and min(packs) < _PACK_TOP_SOC:
        return None
    return vmax
