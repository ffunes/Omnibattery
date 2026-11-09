"""Which SOC a coupled-pack battery is judged by (issue #350).

A Venus A/D couples several battery packs and fills them **in sequence**, so its
aggregate SOC is not the number either end of the charge should be decided on:
it can read the ceiling while the last pack is still half empty. Per-pack SOC
(``battery_soc_pack_1..7``) makes the real state visible, and the verdicts
become asymmetric:

* **full** when the *least* full pack reaches the ceiling — ``min(pack_soc)``;
* **empty** when the *aggregate* reaches the floor — no per-pack reading at all.

The floor is not the mirror of the ceiling, because the hardware is not
symmetric. Charging walks on to the next pack when one fills, which is what the
#350 handovers showed. Discharging walks on too, but the BMS rotates packs on
its own per-pack threshold and stops the battery when the *total* reaches the
cutoff (issue #545: BMS 109, 116 and 118, Venus A and D). Neither pack extreme
tracks that: ``min(pack_soc)`` stopped a Venus A at 20 % with a pack at 29.9 %,
and ``max(pack_soc)`` kept commanding a battery that had gone to Bypass for five
hours. Per-pack SOC is not even reliable while a pack is active — a BMS 116
pack read 12.0 % throughout ten minutes of delivering 300 W.

A battery that stops before its aggregate floor (#408: a v150 Venus D at 15 %)
is caught by what it delivers, not by its pack readings: see the low-SOC
refusal latch in ``_check_non_delivery``.

``soc_vs_ceiling`` falls back to the aggregate when the battery publishes no
per-pack telemetry, which is every model except Venus A/D and any Venus A/D slot
that did not answer the driver's probe.
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


# Per-pack cell voltage keys are read by suffix for the same reason the SOC is
# read by prefix: the key shape is the contract.
_PACK_VMAX_PREFIX = "max_cell_voltage_pack_"
_PACK_VMIN_PREFIX = "min_cell_voltage_pack_"


def pack_cell_voltages(coordinator) -> dict[int, tuple[float, float]]:
    """Return ``{pack number: (vmax, vmin)}`` for every pack reporting both.

    Empty for every battery that publishes no per-pack cell voltage, which is
    every model except Venus A/D, and any Venus A/D slot the probe wrote off. The
    entities ship disabled but the reads do not depend on that (see
    ``balance_dependency_keys``), so a multi-pack owner gets the per-pack delta
    without opting in.
    """
    data = getattr(coordinator, "data", None) or {}

    def _slot_values(prefix):
        out = {}
        for key, value in data.items():
            if not key.startswith(prefix) or not isinstance(value, (int, float)):
                continue
            try:
                out[int(key[len(prefix):])] = float(value)
            except ValueError:
                continue
        return out

    vmax = _slot_values(_PACK_VMAX_PREFIX)
    vmin = _slot_values(_PACK_VMIN_PREFIX)
    # A pack is trusted exactly as far as a Venus E's 37007/37008 are: if the
    # registers answer, the reading stands. The only pairs thrown out are the two
    # that cannot be a measurement at all — a zero, and a max below its min.
    return {
        n: (vmax[n], vmin[n])
        for n in sorted(vmax.keys() & vmin.keys())
        if vmax[n] >= vmin[n] > 0
    }


def pack_cell_deltas(coordinator) -> dict[int, float]:
    """Return ``{pack number: cell delta in mV}``, rounded for display."""
    return {
        n: round((high - low) * 1000, 1)
        for n, (high, low) in pack_cell_voltages(coordinator).items()
    }


def worst_pack_delta(coordinator) -> dict | None:
    """The pack with the widest internal cell spread, or ``None`` if none report.

    Returned as the fields a balance reading stores, so a caller folds it in
    whole and ``delta_mV`` never disagrees with ``vmax_V``/``vmin_V``.

    This is deliberately not a fleet max-minus-min. Each pack balances on its own
    BMS, so a spread taken *across* packs measures how far apart two independent
    BMSs sit, which no cell imbalance follows from and nothing can act on — #415
    measured a resting 155 mV between packs on a healthy battery. The widest
    single pack is a real imbalance inside one BMS, and it names the pack to go
    and look at.
    """
    voltages = pack_cell_voltages(coordinator)
    if not voltages:
        return None
    pack = max(voltages, key=lambda n: voltages[n][0] - voltages[n][1])
    high, low = voltages[pack]
    return {
        "pack": pack,
        "delta_mV": round((high - low) * 1000, 1),
        "vmax_V": round(high, 4),
        "vmin_V": round(low, 4),
    }


def soc_vs_ceiling(coordinator, aggregate):
    """SOC that decides whether to keep *charging*: the least full pack."""
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

    What the qualifier does not buy is a fleet maximum, and it is worth saying
    why no one should add one later. #415 measured register 32111, the active
    pack index: a Venus A/D charges exactly one pack at a time and rotates every
    7–50 minutes, so 37007 reports the pack under load only while slot 1 happens
    to be the active one, and otherwise under-reads it by ~60 mV. Even qualified,
    a cell diverging in another pack stays invisible — 3.363 V on pack 1 against
    3.517 V on pack 2 with every pack at 99.8–100 %. Reading each pack's own
    34005 would close that, and it is deliberately not done: the same charge
    showed the hardware tapering itself 2467 → 1349 → 541 → 0 W with the
    integration allowing 2500 W throughout, peaking at 3.574 V without a cutoff.
    The BMS protects the cell; this feature only has to stop *us* from throttling
    a battery that is still filling.

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
