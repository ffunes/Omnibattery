"""One model of the battery, per quarter (2.0.0 optimizer design, D2).

Energy ``e`` is the usable stored kWh above the discharge floor, 0..capacity.
A single efficiency, the configured round trip, split evenly: eff = sqrt(rte).
  charging   (e1 > e): AC in  = (e1 - e) / eff, solar surplus first, the rest
                       from the grid;
  discharging (e1 < e): AC out = (e - e1) * eff, the house first, the rest exported.
Limits per quarter: AC in <= charge kWh, out <= discharge kWh, grid import while
charging <= ceiling kWh, export <= export limit (solar beyond it is curtailed).

A grid charge quota executes as the controller does: a SOC target per battery,
charged while the whole-percent reading is below it, so the stored energy
rounds up to the next 1 % of the fleet. Exact for one battery and for a fleet
at equal SOC; with unequal SOCs each battery rounds from its own point and the
error stays within 1 % of the fleet. The rest of the quarter the battery is
back to normal: it covers the house and stores the surplus.

All functions take floats or numpy arrays for ``e``/``e1``.
"""
from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class BatteryModel:
    capacity_kwh: float  # whole fleet, 0-100 % SOC
    round_trip_efficiency: float
    floor_kwh: float = 0.0  # stored kWh at the discharge floor
    usable_kwh: float | None = None  # floor..max; default capacity - floor

    @property
    def eff(self) -> float:
        return self.round_trip_efficiency ** 0.5

    @property
    def cap(self) -> float:
        if self.usable_kwh is not None:
            return self.usable_kwh
        return self.capacity_kwh - self.floor_kwh

    def executed_quota(self, e, quota):
        """Stored kWh a quota really adds. The target is the whole-percent
        reading plus the quota, and the charge stops at the first whole percent
        at or above it: from a fractional SOC it can store less than the quota
        or up to 1 % more. Assumes the reading is the SOC rounded down."""
        step = self.capacity_kwh / 100.0
        start = self.floor_kwh + np.asarray(e, dtype=float)
        reading = np.floor(start / step + 1e-6) * step
        rounded = np.ceil((reading + quota) / step - 1e-6) * step - start
        room = np.maximum(self.cap - np.asarray(e, dtype=float), 0.0)
        return np.where(quota > TOL, np.minimum(np.maximum(rounded, 0.0), room), 0.0)

    def normal_step(self, q: Quarter, e, quota: float = 0.0):
        """(energy after, cost) of the quarter: the quota first, at the charge
        rate the ceiling leaves, the grid carrying the house meanwhile; then
        normal behaviour (cover the house, store the surplus). The two phases
        are priced apart: a charge and a discharge in one quarter do not net."""
        e = np.asarray(e, dtype=float)
        net = q.house_kwh - q.solar_kwh
        rate = min(q.charge_kwh, max(0.0, q.ceiling_kwh - max(0.0, net))) * self.eff
        add = np.minimum(self.executed_quota(e, quota), rate)
        busy = np.where(add > 0, add / rate, 0.0) if rate > 0 else np.zeros_like(add)
        e1 = e + add
        grid_busy = add / self.eff + net * busy  # the charge, plus the house meanwhile
        rest = net * (1 - busy)
        if net < 0:
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
        """Cost of going from e to e1 in the quarter; BIG if not allowed.

        Permissions: ``sell`` battery energy may be exported; ``hold`` the
        battery may leave the house to the grid; ``solar`` surplus may be
        exported instead of stored. ``forced`` prices a move without judging it
        (the normal behaviour and an executed quota).
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
            bad |= (from_grid > TOL) & (not q.grid_charge_allowed)
            bad |= from_grid > max(0.0, q.ceiling_kwh - max(0.0, net)) + 1e-6
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
