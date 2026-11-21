"""One model of the battery, per quarter (2.0.0 optimizer design, D2).

Energy ``e`` is the usable stored kWh above the discharge floor, 0..capacity.
A single efficiency, the configured round trip, split evenly: eff = sqrt(rte).
  charging   (e1 > e): AC in  = (e1 - e) / eff, solar surplus first, the rest
                       from the grid;
  discharging (e1 < e): AC out = (e - e1) * eff, the house first, the rest exported.
Limits per quarter: AC in <= charge kWh, out <= discharge kWh, grid import while
charging <= ceiling kWh, export <= export limit (solar beyond it is curtailed).

Grid charging happens only as the controller does it: a slot quota becomes a
SOC target per battery, set once when the slot starts, charged while the
whole-percent reading is below it. The stored energy rounds to whole percents
of the fleet: exact for one battery and for a fleet at equal SOC and max_soc
whatever the capacities; otherwise each battery rounds from its own reading and
target, within 2 % of the fleet (measured max 1.37, tests/test_battery_model.py). Once the target is reached the slot
is complete and the battery is back to normal: it covers the house and stores
the surplus.

All functions take floats or numpy arrays for ``e``/``e1``.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

TOL = 1e-9
BIG = 1e9  # cost of an infeasible move


@dataclass(frozen=True)
class Quarter:
    """Inputs of one forecast interval."""

    house_kwh: float
    solar_kwh: float
    import_price: float
    export_price: float
    grid_charge_allowed: bool
    charge_kwh: float  # AC charge power x hours
    discharge_kwh: float  # AC discharge power x hours
    ceiling_kwh: float = BIG  # grid import ceiling x hours
    export_limit_kwh: float | None = None
    hours: float = 0.25  # the interval's length (the bands are powers)


@dataclass(frozen=True)
class BatteryModel:
    capacity_kwh: float  # whole fleet, 0-100 % SOC
    round_trip_efficiency: float
    floor_kwh: float = 0.0  # stored kWh at the discharge floor
    usable_kwh: float | None = None  # floor..max; default capacity - floor
    # Resolution of the SOC reading, % (whole percent on most drivers; Huawei
    # reads tenths; ESPHome whatever its sensor publishes).
    soc_resolution_pct: float = 1.0
    # The charge curve of a weekly full charge (D-6, D-7): ((stored kWh, AC
    # kW), ...) ascending; from each edge up the fleet charges at that power at
    # most, grid and sun alike (a battery in its 100 % taper, or full while the
    # others go on).
    bands: tuple = ()

    @property
    def eff(self) -> float:
        return self.round_trip_efficiency ** 0.5

    @property
    def cap(self) -> float:
        if self.usable_kwh is not None:
            return self.usable_kwh
        return self.capacity_kwh - self.floor_kwh

    def slot_target(self, e, quota):
        """Stored energy a slot's quota charges up to, set once when the slot
        starts: the whole-percent reading plus the quota, rounded up to the next
        whole percent (the charge stops at the first reading at or above it).
        From a fractional SOC it can store less than the quota or up to 1 %
        more. Assumes the reading is the SOC rounded down."""
        step = self.capacity_kwh * self.soc_resolution_pct / 100.0
        e = np.asarray(e, dtype=float)
        reading = np.floor((self.floor_kwh + e) / step + 1e-6) * step
        target = np.ceil((reading + quota) / step - 1e-6) * step - self.floor_kwh
        return np.where(quota > TOL, np.minimum(target, self.cap), e)

    def executed_quota(self, e, quota):
        """Stored kWh a quota adds when the slot has the time to charge it."""
        return np.maximum(self.slot_target(e, quota) - np.asarray(e, dtype=float), 0.0)

    def charge_rate(self, q: Quarter) -> float:
        """Stored kWh a slot charge adds in the quarter at most: the power the
        grid ceiling leaves after the house, through the charge efficiency."""
        net = q.house_kwh - q.solar_kwh
        return min(q.charge_kwh, max(0.0, q.ceiling_kwh - max(0.0, net))) * self.eff

    def charge_hours(self, e: float, upto: float, power_kw: float) -> float:
        """Hours a charge takes from ``e`` to ``upto`` at ``power_kw`` AC, at
        most each band's power from its edge up (the house and the sun aside)."""
        segments = [(-math.inf, power_kw)] + [(edge, min(power_kw, kw)) for edge, kw in self.bands]
        hours = 0.0
        for k, (low, kw) in enumerate(segments):
            high = segments[k + 1][0] if k + 1 < len(segments) else math.inf
            need = min(upto, high) - max(e, low)
            if need <= TOL:
                continue
            if kw <= 0:
                return math.inf
            hours += need / (kw * self.eff)
        return hours

    def fill(self, e, upto, rate, q: Quarter, frac=1.0):
        """(stored kWh, fraction of the quarter used) charging from ``e`` toward
        ``upto`` for at most ``frac`` of the quarter: ``rate`` stored kWh per
        quarter, at most each band's power from its edge up."""
        e = np.asarray(e, dtype=float)
        upto = np.asarray(upto, dtype=float)
        segments = [(-np.inf, rate)] + [
            (edge, min(rate, kw * q.hours * self.eff)) for edge, kw in self.bands
        ]
        frac = np.asarray(frac, dtype=float)
        left = np.broadcast_to(frac, np.broadcast_shapes(e.shape, frac.shape)).copy()
        now = np.broadcast_to(e, left.shape).copy()
        stored = np.zeros_like(left)
        for k, (low, r) in enumerate(segments):
            high = segments[k + 1][0] if k + 1 < len(segments) else np.inf
            need = np.where(now < high, np.maximum(np.minimum(upto, high) - np.maximum(now, low), 0.0), 0.0)
            t = np.minimum(need / r, left) if r > 0 else np.zeros_like(need)
            got = t * r
            stored += got
            now = now + got
            # Out of time (or of power) inside this segment: none left above it.
            left = np.where((need > 0) & (got < need - TOL), 0.0, left - t)
        return stored, frac - left

    def charge_phase(self, q: Quarter, e, target):
        """(stored kWh, busy fraction) of a slot charge toward ``target`` in the
        quarter: at the rate the ceiling leaves, slower above the bands' edges."""
        e = np.asarray(e, dtype=float)
        rate = self.charge_rate(q)
        if not self.bands:
            add = np.minimum(np.maximum(np.asarray(target, dtype=float) - e, 0.0), rate)
            busy = np.where(add > 0, add / rate, 0.0) if rate > 0 else np.zeros_like(add)
            return add, busy
        return self.fill(e, target, rate, q)

    def normal_step(self, q: Quarter, e, target=None):
        """(energy after, cost) of the quarter. With an open slot (``target``
        from ``slot_target``, passed until the slot first reaches it: the
        controller then completes the slot) the battery first charges toward
        it at the rate the ceiling leaves, the grid carrying the house
        meanwhile; then normal behaviour (cover the house, store the surplus).
        The two phases are priced apart: they do not net."""
        e = np.asarray(e, dtype=float)
        net = q.house_kwh - q.solar_kwh
        if target is None or not q.grid_charge_allowed:
            add = busy = np.zeros_like(e)
        else:
            add, busy = self.charge_phase(q, e, target)
        e1 = e + add
        grid_busy = add / self.eff + net * busy  # the charge, plus the house meanwhile
        rest = net * (1 - busy)
        if net < 0 and self.bands:
            stored, _t = self.fill(e1, self.cap, min(-net, q.charge_kwh) * self.eff, q, 1 - busy)
            stored_ac = stored / self.eff
            e2 = e1 + stored
            grid_rest = rest + stored_ac
        elif net < 0:
            stored_ac = np.minimum(-rest, q.charge_kwh * (1 - busy))
            stored_ac = np.minimum(stored_ac, np.maximum(self.cap - e1, 0.0) / self.eff)
            e2 = e1 + stored_ac * self.eff
            grid_rest = rest + stored_ac
        else:
            out = np.minimum(np.minimum(e1 * self.eff, rest), q.discharge_kwh)
            e2 = np.maximum(0.0, e1 - out / self.eff)
            grid_rest = rest - out
        imp = np.maximum(grid_busy, 0.0) + np.maximum(grid_rest, 0.0)
        exp = np.maximum(-grid_busy, 0.0) + np.maximum(-grid_rest, 0.0)
        if q.export_limit_kwh is not None:
            exp = np.minimum(exp, q.export_limit_kwh)  # the rest of the solar is curtailed
        return e2, imp * q.import_price - exp * q.export_price

    def cost(self, q: Quarter, e, e1, *, sell=True, hold=True, solar=True, forced=False, margin=0.0):
        """Cost of a free move from e to e1 in the quarter; BIG if not allowed.

        Free moves never charge from the grid: the controller does that only
        through a slot quota, which is ``normal_step``. Permissions: ``sell``
        battery energy may be exported; ``hold`` the battery may leave the house
        to the grid; ``solar`` surplus may be exported instead of stored.
        ``forced`` prices a move without judging it.
        """
        e = np.asarray(e, dtype=float)
        e1 = np.asarray(e1, dtype=float)
        d = e1 - e
        a_in = np.maximum(d, 0.0) / self.eff
        out = np.maximum(-d, 0.0) * self.eff
        net = q.house_kwh - q.solar_kwh
        grid = net + a_in - out
        surplus = max(0.0, -net)
        from_grid = np.maximum(0.0, a_in - surplus)
        batt_exp = np.maximum(0.0, out - max(0.0, net))
        bad = (a_in > q.charge_kwh + 1e-6) | (out > q.discharge_kwh + 1e-6)
        if not forced:
            bad |= from_grid > TOL
            if not sell:
                bad |= batt_exp > 1e-6
            if not hold:
                need = np.minimum(np.minimum(e * self.eff, max(0.0, net)), q.discharge_kwh)
                bad |= (d <= 1e-6) & (out < need - 1e-6)
            if not solar and surplus > 0:
                must = np.minimum(self.cap - e, min(surplus, q.charge_kwh) * self.eff)
                bad |= d < must - 1e-6
        imp = np.maximum(grid, 0.0)
        exp = np.maximum(-grid, 0.0)
        if q.export_limit_kwh is not None:
            bad |= batt_exp > q.export_limit_kwh + 1e-6
            exp = np.minimum(exp, q.export_limit_kwh)  # the rest of the solar is curtailed
        c = imp * q.import_price - exp * q.export_price + margin * from_grid
        return np.where(bad, BIG, c)
