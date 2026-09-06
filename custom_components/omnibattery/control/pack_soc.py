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
