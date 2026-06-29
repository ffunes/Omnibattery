"""Top-of-charge management for a normal 100% target (MaxSocChargeManager).

Despite the legacy ``_normal_balance_*`` attribute names this module does NOT
drive active cell balancing. It manages the final stretch of a normal max-SOC
(100%) charge while active balance mode is not running:

- Charge-power taper near the top cell voltage (CV-like ramp-down).
- Charge pause/hysteresis once the top voltage (or a BMS-cutoff signature) is
  reached, latched until SOC drops by the resume margin — prevents pinning the
  cell at the top voltage and the top-of-charge ping-pong on the weak v3 MCU.
- SOC recalibration: keep charging past the pause when the BMS reports a low SOC
  at full cell voltage (coulomb-counter drift) until the BMS itself cuts off.
- Passive cell-delta measurement at the top, reported to the balance monitor.

The latched state (the ``_normal_balance_*`` dicts and
``_normal_active_balance_phases``) stays on the controller because switch.py,
weekly_full_charge.py and the main control loop read and mutate it; this module
reads/writes it by reference via ``self._controller``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from ..const import (
    CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
    NORMAL_BALANCE_CHARGE_POWER_W,
    NORMAL_BALANCE_MEASURE_WAIT_SECONDS,
    NORMAL_BALANCE_PAUSE_CELL_VOLTAGE,
    NORMAL_BALANCE_RECAL_CUTOFF_CYCLES,
    NORMAL_BALANCE_RECAL_CUTOFF_POWER_W,
    NORMAL_BALANCE_RECAL_INVERTER_STANDBY,
    NORMAL_BALANCE_RECAL_SOC_THRESHOLD,
    NORMAL_BALANCE_RESUME_SOC_DROP,
    NORMAL_BALANCE_TAPER_CELL_VOLTAGE,
    NORMAL_BALANCE_TAPER_EXIT_CELL_VOLTAGE,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class MaxSocChargeManager:
    """Top-of-charge taper, pause, SOC recalibration and cell-delta measurement."""

    def __init__(self, hass: "HomeAssistant", controller: Any) -> None:
        self._hass = hass
        self._controller = controller

    def reset_if_new_day(self) -> None:
        """Reset top-of-charge latched state at the local day boundary."""
        c = self._controller
        today = dt_util.now().date()
        if today == c._normal_balance_date:
            return

        c._normal_balance_date = today
        c._normal_balance_charge_paused.clear()
        c._normal_balance_voltage_tapered.clear()
        c._normal_active_balance_phases.clear()
        c._normal_balance_measure_started.clear()
        c._normal_balance_last_delta_v.clear()
        c._normal_balance_top_voltage_seen.clear()
        c._normal_balance_pause_latch_soc.clear()
        c._normal_balance_recal_override.clear()
        c._normal_balance_recal_cutoff_count.clear()
        c._normal_balance_recal_latched.clear()
        for coordinator in c.coordinators:
            c.remove_charge_block("normal_balance_pause", coordinator=coordinator)

    @staticmethod
    def _cell_delta_v(data: dict) -> float | None:
        """Return current max-min cell delta in V when both voltages are known."""
        vmax = data.get("max_cell_voltage")
        vmin = data.get("min_cell_voltage")
        if vmax is None or vmin is None:
            return None
        try:
            return round(float(vmax) - float(vmin), 4)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _taper_enabled(coordinator) -> bool:
        """Return True when this battery uses full-charge voltage tapering."""
        return bool(
            getattr(
                coordinator,
                CONF_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
                DEFAULT_FULL_CHARGE_VOLTAGE_TAPER_ENABLED,
            )
        )

    def _taper_applies(self, coordinator) -> bool:
        """Return True when taper is enabled for this coordinator, excluding active balance ownership."""
        c = self._controller
        if not self._taper_enabled(coordinator):
            return False
        if c._is_active_balance_mode_running(coordinator):
            return False
        if getattr(coordinator, "active_balance_mode_enabled", False):
            return False
        return True

    def _zone_active(self, coordinator) -> bool:
        """Return True when the battery is in the normal top-balancing zone."""
        if not self._taper_applies(coordinator):
            return False

        data = coordinator.data or {}
        vmax = data.get("max_cell_voltage")
        try:
            if vmax is not None and float(vmax) >= NORMAL_BALANCE_TAPER_CELL_VOLTAGE:
                return True
        except (TypeError, ValueError):
            return False
        return False

    def _compute_recal_override(self, coordinator, vmax_f: float, soc) -> bool:
        """Decide whether to keep charging past the tapper pause to recalibrate SOC.

        Called while the max cell is in the top taper zone (down to the taper
        voltage, so the BMS-cutoff counter keeps advancing as the cell relaxes
        after a cut). A low reported SOC at full cell voltage means the BMS
        coulomb counter has drifted, so
        keep charging (at the tapered power) until the BMS itself cuts off, then
        latch off so the SOC can recalibrate to 100%. The latch clears when the
        battery leaves the top zone (see refresh_blocks).
        """
        c = self._controller
        if soc is None or soc >= NORMAL_BALANCE_RECAL_SOC_THRESHOLD:
            c._normal_balance_recal_cutoff_count.pop(coordinator, None)
            return False
        if c._normal_balance_recal_latched.get(coordinator):
            return False

        data = coordinator.data or {}
        power = data.get("battery_power")
        inv = data.get("inverter_state")
        try:
            cutoff = (
                power is not None
                and inv is not None
                and float(power) <= NORMAL_BALANCE_RECAL_CUTOFF_POWER_W
                and int(inv) == NORMAL_BALANCE_RECAL_INVERTER_STANDBY
            )
        except (TypeError, ValueError):
            cutoff = False

        if cutoff:
            count = c._normal_balance_recal_cutoff_count.get(coordinator, 0) + 1
            c._normal_balance_recal_cutoff_count[coordinator] = count
            if count >= NORMAL_BALANCE_RECAL_CUTOFF_CYCLES:
                c._normal_balance_recal_latched[coordinator] = True
                c._normal_balance_recal_cutoff_count.pop(coordinator, None)
                _LOGGER.info(
                    "%s: BMS cutoff during SOC recalibration at vmax=%.3f V, SOC=%s%% — "
                    "holding; SOC should recalibrate to 100%%",
                    coordinator.name, vmax_f, soc,
                )
                return False
        else:
            c._normal_balance_recal_cutoff_count.pop(coordinator, None)
        return True

    def _clear_recal_state(self, coordinator) -> None:
        """Drop all SOC-recalibration state for a battery (session ended)."""
        c = self._controller
        c._normal_balance_recal_override.pop(coordinator, None)
        c._normal_balance_recal_cutoff_count.pop(coordinator, None)
        c._normal_balance_recal_latched.pop(coordinator, None)

    def refresh_blocks(self) -> None:
        """Update normal high-SOC charge protection blockers.

        The normal mode does not force charging. It only stops charge while the
        max cell is at the 100% top voltage; SOC hysteresis decides when future
        charging is allowed.
        """
        c = self._controller
        self.reset_if_new_day()

        for coordinator in c.coordinators:
            data = coordinator.data or {}
            if not self._taper_applies(coordinator):
                c._normal_balance_charge_paused.pop(coordinator, None)
                c._normal_balance_voltage_tapered.pop(coordinator, None)
                c._normal_balance_pause_latch_soc.pop(coordinator, None)
                self._clear_recal_state(coordinator)
                c.remove_charge_block("normal_balance_pause", coordinator=coordinator)
                continue

            if not data:
                c._normal_balance_charge_paused.pop(coordinator, None)
                c._normal_balance_voltage_tapered.pop(coordinator, None)
                c._normal_balance_recal_override.pop(coordinator, None)
                c.remove_charge_block("normal_balance_pause", coordinator=coordinator)
                continue

            in_zone = self._zone_active(coordinator)
            vmax_raw = (coordinator.data or {}).get("max_cell_voltage")
            try:
                vmax_now = float(vmax_raw) if vmax_raw is not None else None
            except (TypeError, ValueError):
                vmax_now = None
            # Hysteresis: only clear the taper latch once the cell has dropped to the
            # exit threshold (below entry), not the moment it slips under 3.48 V at
            # low charge power. This prevents 1250 W ↔ 95 W oscillation.
            if not in_zone and (vmax_now is None or vmax_now < NORMAL_BALANCE_TAPER_EXIT_CELL_VOLTAGE):
                c._normal_balance_voltage_tapered.pop(coordinator, None)
            if not in_zone:
                # Battery has dropped out of the top zone: end any recal session so
                # a later full charge can recalibrate again.
                self._clear_recal_state(coordinator)

            vmax = data.get("max_cell_voltage")
            current_soc = data.get("battery_soc")
            try:
                vmax_f = float(vmax) if vmax is not None else None
            except (TypeError, ValueError):
                vmax_f = None
            try:
                soc_f = float(current_soc) if current_soc is not None else None
            except (TypeError, ValueError):
                soc_f = None
            weekly_active = hasattr(c, "_weekly_charge_mgr") and c._weekly_full_charge_unlocked()

            if vmax_f is not None:
                if in_zone and vmax_f >= NORMAL_BALANCE_TAPER_CELL_VOLTAGE:
                    c._normal_balance_voltage_tapered[coordinator] = True
                # Latch the pause the first time the top voltage is reached this
                # charge session, recording the SOC at that moment. The taper then
                # stops charging and stays stopped — it must NOT re-trickle when
                # the cell relaxes, which would pin the cell at the top voltage.
                #
                # Also latch on the BMS-cutoff signature (charge collapsed to ~0 W
                # with the inverter in standby while still in the top zone). The
                # cell relaxes below the pause voltage within a poll or two of the
                # cut, so a 2 s poll may never observe vmax >= pause and the latch
                # would otherwise never arm — the controller keeps re-commanding
                # charge and the BMS cuts again, an endless top-of-charge ping-pong.
                power = data.get("battery_power")
                inv = data.get("inverter_state")
                try:
                    bms_cut_signature = (
                        in_zone
                        and power is not None
                        and inv is not None
                        and float(power) <= NORMAL_BALANCE_RECAL_CUTOFF_POWER_W
                        and int(inv) == NORMAL_BALANCE_RECAL_INVERTER_STANDBY
                    )
                except (TypeError, ValueError):
                    bms_cut_signature = False
                if not weekly_active and in_zone and (
                    vmax_f >= NORMAL_BALANCE_PAUSE_CELL_VOLTAGE or bms_cut_signature
                ):
                    c._normal_balance_top_voltage_seen[coordinator] = True
                    if coordinator not in c._normal_balance_pause_latch_soc:
                        c._normal_balance_pause_latch_soc[coordinator] = (
                            soc_f if soc_f is not None else 100.0
                        )

            # The pause is latched: charge stays stopped until SOC has dropped by
            # the resume margin (the battery was actually discharged), not merely
            # until the cell voltage relaxes. Then the latch clears so a later
            # top-up tapers again.
            # During weekly charge, skip pause/latch entirely — BMS cutoff is the only exit.
            paused = False
            override = False
            if not weekly_active:
                latch_soc = c._normal_balance_pause_latch_soc.get(coordinator)
                paused = latch_soc is not None
                if (
                    paused
                    and soc_f is not None
                    and soc_f <= latch_soc - NORMAL_BALANCE_RESUME_SOC_DROP
                ):
                    c._normal_balance_pause_latch_soc.pop(coordinator, None)
                    paused = False

                # SOC recalibration: while in the top zone, if the BMS reports a low
                # SOC keep charging until the BMS cuts off (recalibrates). The window
                # extends down to the taper voltage, not just the pause voltage: the
                # cell relaxes below 3.58 V within a poll or two of the BMS cut, so
                # gating the cutoff counter on vmax >= pause would freeze it before it
                # reaches the required consecutive cycles and recal would never latch.
                if (
                    paused
                    and vmax_f is not None
                    and vmax_f >= NORMAL_BALANCE_TAPER_CELL_VOLTAGE
                ):
                    override = self._compute_recal_override(
                        coordinator, vmax_f, current_soc
                    )
                    if override:
                        paused = False
            c._normal_balance_recal_override[coordinator] = override

            if paused:
                c._normal_balance_charge_paused[coordinator] = True
                c.set_charge_block(
                    "normal_balance_pause",
                    "cell_voltage_pause",
                    {
                        "battery": coordinator.name,
                        "max_cell_voltage": vmax,
                        "delta_V": self._cell_delta_v(data),
                    },
                    coordinator=coordinator,
                )
            else:
                c._normal_balance_charge_paused.pop(coordinator, None)
                c.remove_charge_block("normal_balance_pause", coordinator=coordinator)

    def apply_charge_taper(self, coordinator, limit: int) -> int:
        """Cap the per-battery charge limit to the taper power once near the top."""
        c = self._controller
        if not self._taper_applies(coordinator):
            return limit

        data = coordinator.data or {}
        max_cell_voltage = data.get("max_cell_voltage")
        voltage_tapered = c._normal_balance_voltage_tapered
        voltage_taper_latched = voltage_tapered.get(coordinator, False)
        if max_cell_voltage is not None:
            try:
                max_cell_voltage_f = float(max_cell_voltage)
                if max_cell_voltage_f >= NORMAL_BALANCE_TAPER_CELL_VOLTAGE:
                    voltage_taper_latched = True
                    voltage_tapered[coordinator] = True
                elif max_cell_voltage_f < NORMAL_BALANCE_TAPER_EXIT_CELL_VOLTAGE:
                    voltage_tapered.pop(coordinator, None)
                    voltage_taper_latched = False
                if voltage_taper_latched:
                    limit = min(limit, NORMAL_BALANCE_CHARGE_POWER_W)
            except (TypeError, ValueError):
                pass

        return limit

    def get_status(self) -> dict:
        """Return top-of-charge diagnostics for the integration status sensor."""
        c = self._controller
        status = {}
        for coordinator in c.coordinators:
            data = coordinator.data or {}
            if not data:
                continue
            status[coordinator.name] = {
                "enabled": self._taper_enabled(coordinator),
                "in_zone": self._zone_active(coordinator),
                "paused": c._normal_balance_charge_paused.get(coordinator, False),
                "max_cell_voltage": data.get("max_cell_voltage"),
                "min_cell_voltage": data.get("min_cell_voltage"),
                "delta_V": self._cell_delta_v(data),
                "voltage_taper_latched": c._normal_balance_voltage_tapered.get(
                    coordinator, False
                ),
                "pause_latched_soc": c._normal_balance_pause_latch_soc.get(coordinator),
                "active_balance_phase": c._normal_active_balance_phases.get(coordinator),
                "soc_recal_active": c._normal_balance_recal_override.get(coordinator, False),
                "soc_recal_bms_cutoff": c._normal_balance_recal_latched.get(coordinator, False),
                "charge_limit_w": c._battery_power_limit(coordinator, True),
            }
        return status

    async def handle_measurement(self) -> bool:
        """Measure cell delta after any 100% target reaches top voltage."""
        c = self._controller
        active_details = {}
        took_over = False
        active_coordinators: set = set()

        # During an active weekly full charge the taper must drive the battery
        # all the way to the real BMS cutoff. The 60 s measurement hold (0 W
        # while vmax >= pause voltage) would otherwise cap the cell at 3.58 V and
        # stall an imbalanced pack there forever — it relaxes below 3.58 V at 0 W,
        # resumes, climbs back, and ping-pongs without ever reaching the cutoff.
        # The delta-V diagnostic is still captured once at completion.
        weekly_active = (
            hasattr(c, "_weekly_charge_mgr") and c._weekly_full_charge_unlocked()
        )

        for coordinator in c.coordinators:
            if coordinator.data is None or not self._taper_applies(coordinator):
                continue
            if c._is_active_balance_mode_running(coordinator):
                continue
            if weekly_active:
                # Let the weekly taper charge to the BMS cutoff; don't hold/measure.
                c._normal_active_balance_phases.pop(coordinator, None)
                c._normal_balance_measure_started.pop(coordinator, None)
                continue
            if c._normal_balance_recal_override.get(coordinator):
                # SOC recalibration in progress: let PD keep charging to the BMS
                # cutoff instead of holding/measuring at the top voltage.
                c._normal_active_balance_phases.pop(coordinator, None)
                c._normal_balance_measure_started.pop(coordinator, None)
                continue
            if coordinator in c._normal_active_balance_phases:
                active_coordinators.add(coordinator)
                continue
            try:
                vmax = float(coordinator.data.get("max_cell_voltage"))
            except (TypeError, ValueError):
                continue
            if vmax >= NORMAL_BALANCE_PAUSE_CELL_VOLTAGE:
                c._normal_active_balance_phases[coordinator] = "WAIT_MEASURE"
                c._normal_balance_measure_started[coordinator] = dt_util.utcnow()
                active_coordinators.add(coordinator)

        for coordinator in list(c._normal_active_balance_phases):
            if coordinator not in active_coordinators:
                c._normal_active_balance_phases.pop(coordinator, None)
                c._normal_balance_measure_started.pop(coordinator, None)
                c._reset_active_balance_charge_resume_target(coordinator)

        for coordinator in active_coordinators:
            data = coordinator.data or {}
            try:
                vmax = float(data.get("max_cell_voltage"))
                vmin = float(data.get("min_cell_voltage"))
            except (TypeError, ValueError):
                c._normal_active_balance_phases.pop(coordinator, None)
                c._normal_balance_measure_started.pop(coordinator, None)
                c._reset_active_balance_charge_resume_target(coordinator)
                continue

            phase = c._normal_active_balance_phases.get(coordinator, "WAIT_MEASURE")
            charge_power = 0
            discharge_power = 0
            delta_v = round(vmax - vmin, 4)
            if phase == "WAIT_MEASURE":
                started = c._normal_balance_measure_started.setdefault(
                    coordinator,
                    dt_util.utcnow(),
                )
                if (dt_util.utcnow() - started).total_seconds() >= NORMAL_BALANCE_MEASURE_WAIT_SECONDS:
                    c._normal_balance_last_delta_v[coordinator] = delta_v
                    phase = "MEASURED"
                    c._normal_active_balance_phases[coordinator] = phase
                    if c._balance_monitor is not None:
                        await c._balance_monitor.async_record_top_balance_measurement(
                            coordinator,
                            vmax,
                            vmin,
                            data.get("battery_soc"),
                            phase="top_charge_3_55v",
                        )
                    _LOGGER.info(
                        "%s: normal 100%% balance measurement delta=%.4f V at vmax=%.3f V",
                        coordinator.name,
                        delta_v,
                        vmax,
                    )
            if phase == "MEASURED" and vmax < NORMAL_BALANCE_PAUSE_CELL_VOLTAGE:
                c._normal_active_balance_phases.pop(coordinator, None)
                c._normal_balance_measure_started.pop(coordinator, None)
                c._reset_active_balance_charge_resume_target(coordinator)
                await c._set_battery_power(coordinator, 0, 0)
                continue

            details = {
                "phase": phase.lower(),
                "max_cell_voltage": round(vmax, 3),
                "min_cell_voltage": round(vmin, 3),
                "delta_V": delta_v,
                "charge_w": charge_power,
                "discharge_w": discharge_power,
            }
            active_details[coordinator.name] = details
            took_over = True

            await c._set_battery_power(
                coordinator,
                charge_power,
                discharge_power,
                ignore_charge_blockers={
                    "charge_delay",
                    "time_slot_charge",
                    "max_soc",
                    "charge_hysteresis",
                    "normal_balance_pause",
                },
                ignore_discharge_blockers={
                    "time_slot_discharge",
                    "price_discharge",
                    "min_soc",
                },
            )

        if active_details:
            _LOGGER.debug("Normal max-SOC active balancing: %s", active_details)
        return took_over
