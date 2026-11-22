"""Brand-agnostic battery driver contract.

This is the seam between the control layer (coordinator + ChargeDischargeController)
and the hardware. It is deliberately *semantic*, not register-shaped: the only
operations it exposes are "give me a telemetry snapshot" and "deliver this net
power". A Modbus/register battery (Marstek) and an MQTT/property battery (Zendure)
can both sit behind it because neither register addresses nor MQTT topics appear
in the contract.

Two model differences the contract reconciles:

* **Poll vs push.** Marstek is polled every ~1.5 s; Zendure pushes telemetry over
  MQTT. :meth:`BatteryDriver.read_telemetry` is a *pull* of the latest known
  state — a push-based driver caches the last message and returns it, so the
  coordinator's poll loop is unchanged.
* **Control semantics.** Marstek wants ``force_mode`` + separate charge/discharge
  set-point registers; Zendure wants an input/output limit. The control loop
  speaks a single signed *net power* (+charge / -discharge) via
  :meth:`BatteryDriver.apply_setpoint`; each driver translates to its own wire
  format internally.

Nothing imports this module yet — it defines the target contract. It is filled
in incrementally; see ``docs/plans/driver_abstraction.md`` for the phase plan.
"""

from __future__ import annotations

import enum
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional, Sequence

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .setup import DriverSetup

_LOGGER = logging.getLogger(__name__)


# Optional telemetry key a driver may publish alongside ``battery_power``.
#
# ``battery_power`` is cell-side (+charge / -discharge). On a battery whose PV or
# an off-grid microinverter feeds the same DC bus, the cells can read "charging"
# while the AC port is delivering the commanded discharge, and can absorb a
# commanded charge straight from PV with nothing crossing the AC port at all
# (issue #399). A driver that can measure the exchange at the device's own AC
# port publishes it under this key, in the same sign convention as
# ``battery_power``. Drivers that cannot measure it simply omit the key and the
# control layer keeps judging delivery from the cells alone.
DELIVERED_AC_POWER_KEY = "ac_delivered_power"

# Cell-side (DC) battery power, same sign convention as ``battery_power``, for a
# driver whose ``battery_power`` is really an AC-side figure (Hoymiles, #625) and
# that can measure the DC side separately (voltage x current). The efficiency
# sensor uses it as the DC plane when present.
BATTERY_DC_POWER_KEY = "battery_dc_power"

# --- canonical keys ----------------------------------------------------------
# Logical keys the platforms treat specially. They are the vocabulary the first
# drivers (Marstek registers) gave to the entities, and a driver that wants the
# behaviour publishes its control or telemetry under the same key. The values
# are entity ``unique_id`` suffixes and translation keys: they must not change.

#: Switch that turns the device's external (RS485/Modbus) control mode on or off.
#: Its on/off goes through ``set_rs485_control`` and a readback, not a plain write.
KEY_RS485_CONTROL_MODE = "rs485_control_mode"
#: Select with the options ``None`` / ``Charge`` / ``Discharge``. The chosen option
#: is remembered as the manual force mode so the control loop can re-assert it.
KEY_FORCE_MODE = "force_mode"
#: Number of the manual charge setpoint in watts (the register-backed twin of the
#: software manual setpoint). Its slider is capped to the configured charge limit.
KEY_SET_CHARGE_POWER = "set_charge_power"
#: Number of the manual discharge setpoint in watts; see ``KEY_SET_CHARGE_POWER``.
KEY_SET_DISCHARGE_POWER = "set_discharge_power"
#: Number holding the maximum charge power the device accepts, in watts. Where the
#: device only has a hardware selector (``hardware_power_cap_choices``) the user's
#: software limit is shown and written instead.
KEY_MAX_CHARGE_POWER = "max_charge_power"
#: Number holding the maximum discharge power; see ``KEY_MAX_CHARGE_POWER``. A
#: device that publishes this limit under another key declares the alias with the
#: ``canonical_key`` field of its number definition.
KEY_MAX_DISCHARGE_POWER = "max_discharge_power"
#: Number of the device's own charge cut-off SOC in percent; writing it moves the
#: coordinator's maximum SOC.
KEY_CHARGING_CUTOFF_CAPACITY = "charging_cutoff_capacity"
#: Number of the device's own discharge cut-off SOC in percent; writing it moves
#: the coordinator's minimum SOC.
KEY_DISCHARGING_CUTOFF_CAPACITY = "discharging_cutoff_capacity"
#: Sensor of the state of charge in percent. The platform attaches the model label
#: and the aggregate-solar and DC-PV flags to it for the panel.
KEY_BATTERY_SOC = "battery_soc"
#: Sensor of the lifetime discharged energy in kWh; the platform corrects it with
#: the backup-discharge energy the integration measured.
KEY_TOTAL_DISCHARGING_ENERGY = "total_discharging_energy"

#: Field of an entity definition that names the canonical key the entity stands
#: for when its own ``key`` (which fixes the ``unique_id``) differs from it. Absent
#: means the entity's key is already the canonical one.
CANONICAL_KEY_FIELD = "canonical_key"


def canonical_key(definition: Mapping) -> str:
    """The canonical key an entity definition stands for (its own key by default)."""
    return definition.get(CANONICAL_KEY_FIELD, definition["key"])


# Telemetry keys that directly govern control safety, in the canonical
# vocabulary every driver imitates. During non-responsive recovery a successful
# read of unrelated groups must not hide a frozen group that carries one of these
# (issue #26): the coordinator counts the failures of those groups apart. A key a
# driver does not publish simply never matches, so the default is the same set for
# every driver; ``BatteryDriver.critical_control_keys`` is where a driver narrows
# or widens it.
CRITICAL_CONTROL_KEYS = frozenset({
    "ac_power",
    "battery_power",
    "battery_soc",
    KEY_FORCE_MODE,
    "inverter_state",
    KEY_SET_CHARGE_POWER,
    KEY_SET_DISCHARGE_POWER,
})


class AcSource(enum.Enum):
    """Where ``BatteryDriver.ac_contribution_w`` may read a battery's AC contribution.

    REGISTER: the device's own AC-side reading, ``ac_register_power_w`` (only the
    drivers that publish one override it). PORT: ``DELIVERED_AC_POWER_KEY``. CELL:
    ``battery_power``, which on a battery with PV on its own DC bus is not what
    crosses the AC port. A source that reads None is skipped.
    """

    REGISTER = "register"
    PORT = "port"
    CELL = "cell"


# The five places that need the AC contribution disagree on which source comes
# first and on which they accept (plan section 8, T7.4; pinned by
# tests/test_ac_contribution_characterization.py). Each order below reproduces one
# of them exactly. Diferencia conservada, decidir: the only reachable divergence
# between drivers is Anker with independent PV, which publishes both a register
# (the negated pack power) and the AC port (pack minus PV), and the two orders
# read different ones.
#: Controller (``_coordinator_delivered_power``) and phase limiter: register first.
AC_REGISTER_FIRST = (AcSource.REGISTER, AcSource.PORT, AcSource.CELL)
#: Consumption tracker: the AC port first.
AC_PORT_FIRST = (AcSource.PORT, AcSource.REGISTER, AcSource.CELL)
#: Residual-load reconstruction on a battery with connected MPPT PV: the cell power
#: is refused there, so a battery with neither AC source reads as unreadable.
AC_PORT_FIRST_NO_CELL = (AcSource.PORT, AcSource.REGISTER)
#: Daily-operation diary: only the device's own AC-side reading, no fallback.
AC_REGISTER_ONLY = (AcSource.REGISTER,)
#: Phase limiter when it has no controller to ask: no AC port.
AC_REGISTER_THEN_CELL = (AcSource.REGISTER, AcSource.CELL)


class InverterStatus(enum.Enum):
    """Canonical operating state of a battery's inverter.

    The vocabulary behind the ``inverter_state`` telemetry key, in the numeric
    codes the key carries. ``BatteryDriver.inverter_status`` turns whatever a
    driver publishes into one of these (or ``None`` when it cannot say), and the
    control layer compares members, never raw codes.
    """

    SLEEP = 0
    STANDBY = 1
    CHARGE = 2
    DISCHARGE = 3
    BACKUP = 4  # grid down: the backup port is fed from the cells
    OTA_UPGRADE = 5
    BYPASS = 6  # grid passed through to the backup port; the cells supply nothing


@dataclass(frozen=True)
class DriverAlarm:
    """One active alarm or fault, already decoded by the driver.

    ``kind`` is ``"fault"`` or ``"alarm"``; ``code`` identifies the condition
    within its kind (stable across reads: the notifier tells new conditions from
    standing ones by ``(kind, code)``). ``description`` is the human text, or
    ``None`` for a condition the driver cannot name: it still counts as active
    but adds no label.
    """

    kind: Literal["fault", "alarm"]
    code: int
    description: Optional[str] = None


@dataclass(frozen=True)
class DriverContext:
    """What the coordinator hands a driver's ``from_config`` besides the stored config.

    The power limits arrive already normalised by the coordinator; ``persist`` and
    ``stored`` are bound to this battery's entry in the config entry.
    """

    hass: "HomeAssistant"
    name: str
    device_key: str
    max_charge_power_w: int
    max_discharge_power_w: int
    persist: Callable[[str, Any], None]
    stored: Callable[[str], Any]


def stored_battery_value(coordinator: Any, key: str) -> Any:
    """Read one key of this coordinator's battery config (None when unknown)."""
    entry = getattr(coordinator, "_config_entry", None)
    data = getattr(entry, "data", None)
    # Home Assistant hands out entry data as a read-only MappingProxyType, not a dict.
    if not isinstance(data, Mapping):
        return None
    for battery in data.get("batteries", []):
        if not isinstance(battery, Mapping):
            continue
        if (
            battery.get("host") == coordinator.host
            and battery.get("port") == coordinator.port
            and battery.get("slave_id", 1) == getattr(coordinator, "slave_id", 1)
        ):
            return battery.get(key)
    return None


@dataclass(frozen=True)
class CellVoltageProfile:
    """Per-cell voltage levels the top-of-charge logic acts on (a cell chemistry).

    The core compares the highest cell voltage a driver publishes
    (``max_cell_voltage``) against these levels; a driver that publishes no cell
    voltage declares no profile (``DriverCapabilities.cell_voltage_profile``
    is None) and the core applies none.
    """

    # The charge taper starts at this voltage (cell >= taper_entry_v).
    taper_entry_v: float
    # The taper latch releases only below this voltage (hysteresis under entry).
    taper_exit_v: float
    # Top-cell point at which charging is paused / the BMS-owned hold begins.
    pause_v: float
    # After a first BMS cutoff above pause_v, the cell must relax to this voltage
    # (cell <= recal_retry_v) before the one-shot retry is allowed.
    recal_retry_v: float


# LiFePO4 per-cell levels. They describe the chemistry, not a brand: any driver
# whose cells are LFP and that publishes the highest cell voltage may declare it.
LFP_CELL_VOLTAGE_PROFILE = CellVoltageProfile(
    taper_entry_v=3.48,
    taper_exit_v=3.44,
    pause_v=3.60,
    recal_retry_v=3.57,
)


@dataclass(frozen=True)
class BalanceBaseline:
    """How the balance monitor judges a cell-voltage spread, in millivolts.

    Cells can leave the factory with a sizeable top-of-charge spread that is
    normal, not a fault. The three thresholds are absolute raw-delta values placed
    above it, so a fresh battery reads green; ``offset_mv`` is subtracted only
    where a reading is judged against the stored history (the rising-trend gate
    and the status of a stored reading).
    """

    # Factory top-of-charge spread, subtracted before judging stored readings.
    offset_mv: float
    # Raw (or, for stored readings, offset-corrected) spread at which the status
    # turns yellow, orange and red.
    yellow_mv: float
    orange_mv: float
    red_mv: float


# The baseline of every driver today: the figures measured on Marstek cells
# (commonly ~170-180 mV at 3.55 V, where the LiFePO4 curve is steep). A driver
# whose cells ship with another spread declares its own; ``None`` = no baseline,
# the monitor then records the reading without judging it.
DEFAULT_BALANCE_BASELINE = BalanceBaseline(
    offset_mv=180, yellow_mv=200, orange_mv=230, red_mv=250
)


@dataclass(frozen=True)
class DriverTraits:
    """Class-level traits the shared code needs before a driver is connected.

    Unlike :class:`DriverCapabilities` (per instance, refined after ``connect``),
    these are fixed per brand, or derived from the stored battery config by the
    brand's own :meth:`BatteryDriver.traits_for`.
    """

    # The 100 % charge voltage taper switch exists (needs per-cell voltages).
    supports_voltage_taper: bool = True
    # The coordinator keeps the stored taper flag even without the switch.
    # Freezes a known defect (plan section 8): Sessy's weekly-charge planning
    # assumes a 200 W tail from a taper it does not have. Remove with that fix.
    keeps_stored_taper_flag: bool = False
    # Fixed power envelope: legacy configured limits are clamped to the device's.
    fixed_power_envelope: bool = False
    # The battery has a DC PV input that can be left unconnected (Marstek vA/vD).
    optional_dc_pv_input: bool = False
    # False for a battery that is only read: its stored power limits are not
    # watts the fleet can be told to move.
    takes_power_setpoint: bool = True
    # Initial SOC/power config is not written at setup: the device holds it.
    device_owns_initial_config: bool = False
    # Addressed by IP, so its address can drift.
    ip_addressed: bool = False
    # The BMS owns the top-of-charge cutoff (coupled-pack Marstek vA/vD): software
    # must not treat a refused charge near 100 % as a fault. Marstek derives it
    # from its stored version in its own ``traits_for``.
    bms_owns_top_cutoff: bool = False
    # The battery couples several packs and fills them in sequence (coupled-pack
    # Marstek vA/vD), so its aggregate SOC and cell-voltage reading describe one
    # pack and the control layer judges the top of charge on the per-pack SOC
    # keys (``battery_soc_pack_*``). Derived like ``bms_owns_top_cutoff``.
    sequential_pack_fill: bool = False
    # A top-of-charge cutoff can be told from idle without ``inverter_state``
    # (Zendure's local API does not publish it); the commanded setpoint and
    # SOC >= 99 stand in for it.
    cutoff_without_inverter_state: bool = False
    # Decimals of the aggregate SOC; None keeps the sensor definition's own.
    # Marstek derives it from its stored version in its own ``traits_for``.
    soc_decimals: Optional[int] = None
    # The device's max-power setting is only a hardware ceiling chosen from
    # fixed options (Marstek v2/v3: 800 W or 2500 W), so the user's own limit
    # must survive polling.
    hardware_power_cap_choices: bool = False
    # The config flow's battery-capacity field: Sessy cannot report one
    # ("required"), Zendure and Hoymiles can ("optional"), the rest have none.
    capacity_field: Literal["required", "optional", "none"] = "none"
    # Offered range of the minimum-SOC control in the config flow: (low, high,
    # default). Defaults are the Marstek v2 form's.
    min_soc_range: tuple[int, int, int] = (12, 30, 12)
    # Low bound of the maximum-SOC control (its high bound and default are 100).
    # Marstek v3/vA/vD enforce SOC in software, so theirs is lower.
    max_soc_low: int = 80


@dataclass(frozen=True)
class DriverCapabilities:
    """Static, brand/model-specific traits the control layer branches on.

    Replaces the ``if self.battery_version in ("v3", "vA", "vD")`` checks that are
    currently scattered through the coordinator and control loop. A driver reports
    its capabilities once; callers consult them instead of hard-coding versions.
    """

    # True if the hardware enforces SOC charge/discharge cut-offs itself. When
    # False the control layer must enforce min/max SOC in software (Marstek v3/vA/vD
    # have no cut-off registers; v2 does).
    hardware_soc_cutoff: bool

    # True if the hardware supports a distinct force/charge/discharge mode command
    # (Marstek force_mode). A driver that only takes a signed power limit reports
    # False and ignores the ``mode`` hint in apply_setpoint.
    has_force_mode: bool

    # Telemetry arrives by push (MQTT) rather than poll (Modbus). The coordinator
    # uses this to decide whether read_telemetry is a live read or a cache read.
    push_telemetry: bool

    # Inclusive power envelope the hardware accepts, in watts (per battery unit).
    max_charge_power_w: int
    max_discharge_power_w: int

    # True if the driver exposes individual DC-coupled MPPT channels. The control
    # layer uses this to decide whether the unit contributes per-channel solar
    # production and needs the MPPT-aware calculations. Drivers that expose only
    # an aggregate PV value keep this False and set has_solar_telemetry instead.
    has_mppt_pv: bool

    # True if the hardware exposes alarm/fault status registers (Marstek v2 only).
    # Gates the system alarm sensor.
    has_alarm_registers: bool

    # True if external (RS485/Modbus) control mode can be toggled on this hardware.
    has_rs485_control: bool

    # True if the hardware reports cumulative energy counters. When False the
    # integration synthesises charge/discharge energy by integrating power.
    # Defaults True so existing register-backed drivers need no change.
    has_energy_counters: bool = True

    # True if the hardware also reports its nominal battery capacity. Drivers
    # such as Sessy expose lifetime energy counters but require the user to
    # supply this value for stored-energy and equivalent-cycle calculations.
    has_nominal_capacity: bool = True

    # True when equivalent cycles are defined from cumulative discharged energy
    # alone. Sessy exposes that lifetime counter, while the existing drivers
    # retain their throughput-based calculation for backward compatibility.
    cycles_from_discharge_only: bool = False

    # True if the hardware also reports counters that reset every day. Some
    # devices (Anker) expose only lifetime charge/discharge totals; for those the
    # entity layer derives daily values from the cumulative counter deltas.
    # Defaults True for backward compatibility with the Marstek register maps.
    has_daily_energy_counters: bool = True

    # True when ``solar_power`` is an independent aggregate DC/PV source.
    # This is separate from has_mppt_pv because some devices (Anker Solarbank 4)
    # report the combined PV input but do not expose one telemetry key per MPPT.
    # A PV-looking value derived from the battery's own AC/P1 calculation must
    # leave this False so it is never counted as additional production.
    has_solar_telemetry: bool = False

    # True if a setpoint readback reliably reflects the just-written command on the
    # confirmation cycle. Register batteries (Marstek) echo the written value at
    # once. A driver whose device applies writes with latency (Zendure: the HTTP
    # report echoes the previous limit for ~2 s, so an in-flight PD setpoint change
    # reads back "not yet applied" even though the write was accepted) reports
    # False, so the control layer logs an unconfirmed first attempt at debug rather
    # than warning — the retry still confirms it. Defaults True.
    setpoint_confirm_reliable: bool = True

    # True if the reported delivered power tracks the real AC output closely
    # enough to anchor control on. Anker reports 0 W or lagging values for
    # seconds after a setpoint change (#522), so the PD skips the measured-power
    # anchors that have no real limit behind them (the timed anti-windup
    # fallback and the feedforward deadbeat). Defaults True.
    delivered_power_reliable: bool = True

    # True if the AC port and the cell terminal are measured independently, so
    # their power difference is the real conversion loss. The efficiency sensor
    # integrates the two planes on PV units only when this holds. Zendure reports
    # identical AC and cell power while discharging, which reads as 100% (#556),
    # so it keeps the cumulative charge/discharge counters. Defaults True.
    independent_power_planes: bool = True

    # False if the lifetime charge/discharge counters are battery-side (DC to DC),
    # so their ratio is the cell efficiency and not the AC round trip (Anker,
    # #706). Such a ratio must not feed the arbitrage efficiency. Defaults True.
    lifetime_counters_ac: bool = True

    # Approximate physical response time (seconds) after issuing a setpoint. The
    # zero-cross guard uses it to avoid commanding the opposite direction while
    # the previous command is still taking effect. Readback freshness is declared
    # separately below because a device may begin responding quickly while its
    # telemetry takes several more seconds to settle.
    actuator_latency_s: float = 0.5

    # Worst-case time (seconds) before post-command telemetry can be trusted as a
    # settled readback. ``None`` preserves the historical behaviour by falling
    # back to ``actuator_latency_s``. Drivers with distinct actuation and telemetry
    # timing should report both values so hot-path ACK checks can be skipped
    # without changing the physical-response model.
    readback_latency_s: Optional[float] = None

    # Time (seconds) after a direction change during which zero delivered power is
    # expected while the device engages. ``None`` uses the controller default.
    # This is separate from actuator_latency_s because a device may switch
    # direction quickly once running but still need a long standby-to-running
    # safety transition (Sessy takes up to 60 seconds for that transition).
    engage_grace_s: Optional[float] = None

    # Minimum reliable operating power (watts, per unit) below which the hardware
    # will not sustain a non-zero charge/discharge. Every Marstek model reports 0
    # (its setpoint registers accept any power); Anker reports a real floor.
    # The thermal derate clamps its non-zero output up to this value
    # so it never dribbles an unreliable sub-minimum command. Defaults to 0 (no floor).
    min_charge_power_w: int = 0
    min_discharge_power_w: int = 0

    # True when a push driver's read_telemetry still proves the source is live
    # (it drops values whose age says the upstream feed has stalled) rather than
    # replaying a cache. Only meaningful alongside push_telemetry: it tells the
    # coordinator the post-reconnect probe read is worth running, so a reconnect
    # that did not actually restore telemetry cannot clear the failure counter
    # and starve the back-off. Defaults False (a plain cache read proves nothing).
    telemetry_liveness_checked: bool = False

    # Inclusive SOC window (percent) each cutoff control may offer. The defaults
    # are the Venus D's hardware floors, which is what the software-limit entity
    # hard-coded before these existed; a driver whose hardware means something
    # else reports its own. A control that offers a value the driver's write path
    # rejects is worse than no control: the entity shows the new number while the
    # hardware backstop never moves (#495).
    charge_cutoff_range: tuple[float, float] = (50.0, 100.0)
    discharge_cutoff_range: tuple[float, float] = (12.0, 50.0)

    # Per-cell voltage levels the top-of-charge logic applies to the driver's
    # ``max_cell_voltage``. None = the driver publishes no cell voltage (or the
    # levels do not fit its chemistry): the core applies none. Kept per instance
    # in the capabilities, not in DriverTraits, because the Marstek driver derives
    # it from the telemetry its model publishes, like ``has_mppt_pv``.
    cell_voltage_profile: Optional[CellVoltageProfile] = None

    # Telemetry keys of the per-MPPT-channel PV power (W) the driver publishes,
    # in channel order. () = no channel keys. Separate from ``has_mppt_pv`` (the
    # hardware flag the installation can switch off) and from the aggregate
    # ``solar_power`` key: a model may publish channel keys without having
    # ``has_mppt_pv``, and the core only ever sums the keys listed here.
    mppt_power_keys: tuple[str, ...] = ()

    # How the balance monitor judges the driver's cell-voltage spread. None = no
    # baseline: the reading is recorded but not judged and raises no alert.
    balance_baseline: Optional[BalanceBaseline] = DEFAULT_BALANCE_BASELINE

    # False when the device accepts no signed power command at all. Every driver
    # that owns a transport translates ``apply_setpoint`` into some wire format;
    # a battery Home Assistant can only *observe* (a foreign inverter's storage,
    # read through that integration's entities) has nothing to translate it into.
    #
    # This is not "the user switched control off" (``rs485_user_disabled``) and
    # not "the device refuses right now" (Huawei's PV gate). It is a permanent
    # property of the device, and it is load-bearing in two directions: shared
    # control must never select, allocate to or write to the battery, and
    # predictive charging must never count its empty room as grid-fillable
    # headroom. Its *telemetry* still counts -- that is the whole point of
    # configuring it. Read it through ``can_take_setpoint`` below, never raw.
    supports_power_setpoint: bool = True

    # True when the device cannot be given watts but can be switched between its
    # own zero-on-the-meter regulation (NOM) and standing still (Manual). Only
    # meaningful with ``supports_power_setpoint=False``. Read it through
    # ``supports_nom_mode`` below.
    supports_nom_mode: bool = False


class NativeOperatingMode(enum.StrEnum):
    """The two native modes of a battery that regulates on its own."""

    NOM = "nom"        # keeps its own meter at 0 W
    MANUAL = "manual"  # regulation off, the battery rests


@dataclass(frozen=True)
class OperatingModeResult:
    """Outcome of one native mode change.

    ``ok``: the device accepted the write. ``confirmed``: it was also seen in the
    requested mode; False is normal right after the write on a slow device.
    """

    ok: bool
    confirmed: bool
    requested_mode: NativeOperatingMode
    effective_mode: Optional[NativeOperatingMode] = None
    failure_reason: Optional[str] = None


def supports_nom_mode(coordinator) -> bool:
    """Whether this battery can be switched between NOM and Manual."""
    capabilities = getattr(coordinator, "capabilities", None)
    return getattr(capabilities, "supports_nom_mode", False) is True


def can_take_setpoint(coordinator) -> bool:
    """Whether shared code may command this battery's power at all.

    One definition for a question asked from the control loop, the entity
    platforms, the planner and the aggregates alike. Defaults True so every
    existing driver, coordinator and test double keeps its behaviour: only a
    driver that declares otherwise is treated as observe-only.
    """
    capabilities = getattr(coordinator, "capabilities", None)
    return bool(getattr(capabilities, "supports_power_setpoint", True))


def is_observed(coordinator) -> bool:
    """Negation of :func:`can_take_setpoint`, for readability at filter sites.

    ``if is_observed(c): continue`` says what the skip is for; the double
    negative of the positive form does not.
    """
    return not can_take_setpoint(coordinator)


def mppt_power_keys(coordinator) -> tuple[str, ...]:
    """The MPPT channel keys this battery's driver publishes (``()`` if none)."""
    capabilities = getattr(coordinator, "capabilities", None)
    return tuple(getattr(capabilities, "mppt_power_keys", ()))


def has_connected_mppt_pv(coordinator) -> bool:
    """Return whether an MPPT-capable battery has panels connected.

    ``has_mppt_pv`` remains a hardware capability. Marstek Venus A/D users can
    explicitly declare that their physical MPPT inputs are unused; coordinators
    and test doubles without that installation setting retain the historical
    capability-based behaviour.
    """
    capabilities = getattr(coordinator, "capabilities", None)
    return bool(
        getattr(capabilities, "has_mppt_pv", False)
        and getattr(coordinator, "dc_pv_connected", True)
    )


def has_solar_production_source(controller) -> bool:
    """True when production is actually measured, not merely accumulated.

    The daily accumulator is rolled over at midnight whether or not any
    source feeds it, so its date says nothing about whether a reading ever
    arrives. Without a source it stays at 0.0 all day.
    """
    if getattr(controller, "solar_production_sensor", None):
        return True
    return any(
        has_connected_mppt_pv(coordinator)
        or getattr(
            getattr(coordinator, "capabilities", None),
            "has_solar_telemetry",
            False,
        )
        for coordinator in getattr(controller, "coordinators", None) or ()
    )


@dataclass(frozen=True)
class SetpointResult:
    """Outcome of an :meth:`BatteryDriver.apply_setpoint` call.

    ``net_power_w`` is the commanded signed power that was actually applied
    (+charge / -discharge), clamped to the driver's envelope. ``confirmed`` is
    True only when the driver read the command back from the hardware and it
    matched; a write-only fast path returns the optimistic command with
    ``confirmed=False``.
    """

    ok: bool
    net_power_w: int
    confirmed: bool
    # Brief machine-readable reason when ``ok`` is False (e.g. "write_failed",
    # "not_connected", "feedback_timeout"). None on success.
    failure_reason: Optional[str] = None
    # True when a confirmed readback echoed the written set-points exactly;
    # False when confirmation relied on the driver's echo tolerance (the battery
    # is still ramping / the bridge lagged the write). Only meaningful when
    # ``confirmed`` is True — the control layer uses it to defer the exact
    # settled-state verification to the poll comparison.
    exact: bool = True
    # Measured delivered power (signed W, +charge / -discharge) read back from the
    # hardware on a confirmation cycle; None on the write-only fast path
    # (``read_back=False``). Universal telemetry the control layer uses for
    # non-delivery detection — independent of any register/property layout.
    battery_power_w: Optional[int] = None
    # Brand-native state echo for the coordinator's telemetry cache
    # (``coordinator.data``). The coordinator merges this verbatim so it need not
    # know the keys: Marstek returns ``force_mode`` + ``set_charge_power`` +
    # ``set_discharge_power`` (plus ``battery_power`` on a readback cycle). None
    # only when the command failed before anything was applied.
    applied: Optional[dict] = None


# Telemetry is a flat mapping of logical sensor key -> decoded value, exactly the
# shape the coordinator already stores in ``coordinator.data`` today (e.g.
# {"battery_soc": 47, "battery_power": -612, ...}). Kept as a plain dict so the
# existing sensor/aggregate layers need no change.
TelemetrySnapshot = dict


@dataclass(frozen=True)
class ReadGroup:
    """A schedulable unit of telemetry the coordinator polls as one request.

    The driver groups its telemetry keys so the coordinator can schedule, gate and
    lock *per group* without knowing the register layout: a Modbus driver collapses
    a contiguous register span into one block group (read in a single request) and
    exposes every other key as its own singleton group; a push driver can expose a
    single group of everything it caches. ``scan_interval`` is the poll-cadence
    name the coordinator maps to seconds (None means the group is misconfigured and
    is skipped with a warning); ``keys`` are the logical telemetry keys read
    together — passed verbatim to :meth:`BatteryDriver.read_telemetry` — and double
    as the group's stable identity for per-group poll scheduling.
    """

    scan_interval: Optional[str]
    keys: tuple[str, ...]


@dataclass(frozen=True)
class ReconcileState:
    """What the coordinator owns and a driver's ``reconcile`` may need to read.

    The driver never holds a reference to the coordinator; the coordinator hands
    this snapshot in on every ``reconcile`` call. ``attributes`` holds the
    current value of each coordinator attribute the driver mirrors (the names
    in ``BatteryDriver.coordinator_attributes``; None when unset).
    """

    device_max_charge_power: int
    device_max_discharge_power: int
    configured_max_charge_power: int
    configured_max_discharge_power: int
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DriverReconciliation:
    """What a driver asks the coordinator to apply after ``reconcile``.

    ``None`` on every optional field means "leave it alone". The coordinator's
    ``_apply_reconciliation`` applies it in this order: ``attributes``,
    ``drop_keys``, ``definitions_changed``, the power limits, ``max_soc`` /
    ``min_soc``, then ``persist`` in insertion order, then ``message``.
    """

    device_max_charge_power: Optional[int] = None
    device_max_discharge_power: Optional[int] = None
    configured_max_charge_power: Optional[int] = None
    configured_max_discharge_power: Optional[int] = None
    max_soc: Optional[int] = None
    min_soc: Optional[int] = None
    # Coordinator attributes to reassign (``ems_version``, ``zendure_model``...).
    attributes: dict[str, Any] = field(default_factory=dict)
    # Per-battery config keys to persist, in order.
    persist: dict[str, Any] = field(default_factory=dict)
    # Keys to remove from the coordinator's ``data``.
    drop_keys: tuple[str, ...] = ()
    # The coordinator rebuilds its key -> definition index from the driver.
    definitions_changed: bool = False
    # Pre-formatted info line the coordinator logs (prefixed with its name).
    message: Optional[str] = None


class BatteryDriver(ABC):
    """Abstract hardware driver for a single physical battery.

    One instance per battery (per coordinator). Owns its transport and connection
    state. All methods are async because every real transport (Modbus TCP, MQTT)
    is I/O bound.
    """

    # --- identity -----------------------------------------------------------

    # Class-level identity, read by ``drivers.registry`` and the device registry.
    # A subclass sets all of them; the defaults only keep a bare driver usable.
    BRAND: str = ""             # stable id stored as ``brand`` in the config entry
    LABEL: str = ""             # label in the config flow's brand selector
    MANUFACTURER: str = ""      # device registry manufacturer
    DEFAULT_MODEL: str = ""     # device registry model when ``model_label`` is None
    # Config keys of this brand to redact from diagnostics. The generic keys
    # live here; ``registry.redact_keys()`` unions every registered class.
    REDACT_KEYS: frozenset[str] = frozenset({"host", "serial", "ip_address", "mac"})

    TRAITS: DriverTraits = DriverTraits()
    # Config flow steps of this brand. ``None`` = no steps are generated for the
    # brand; a ``DriverSetup`` gets its steps generated by ``config_flow.py``.
    SETUP: "DriverSetup | None" = None

    @classmethod
    def traits_for(cls, battery_config: Mapping) -> DriverTraits:
        """Traits of this brand for one stored battery config.

        The brand's fixed ``TRAITS``; a brand whose traits follow its own stored
        keys (Marstek's version and serial port) overrides this.
        """
        return cls.TRAITS

    @classmethod
    def model_version(cls, battery_config: Mapping, name: str | None = None) -> str | None:
        """Hardware version a brand distinguishes in its stored config; None if none."""
        return None

    @classmethod
    @abstractmethod
    def from_config(
        cls, battery_config: Mapping, context: DriverContext
    ) -> "BatteryDriver":
        """Build this brand's driver from one battery's stored config.

        Reads exactly the config keys the brand owns, with their defaults;
        ``context`` carries what the coordinator owns (see :class:`DriverContext`).
        """

    @classmethod
    def link_key(cls, battery_config: Mapping) -> tuple:
        """Identity of the physical link a battery sits on.

        Batteries with the same key are set up one after another (they share one
        connection slot); different keys connect concurrently. A brand with a
        serial link of its own (Marstek) overrides this.
        """
        return (battery_config.get("host"), battery_config.get("port"), None)

    @classmethod
    def slot_power_ceiling(cls, battery_config: Mapping) -> Optional[int]:
        """Hardware power ceiling of the time-slot forms; None = the configured max."""
        return None

    def coordinator_attributes(self) -> dict[str, Any]:
        """Brand-owned config values the coordinator mirrors as public attributes."""
        return {}

    def reconcile(
        self,
        data: dict,
        *,
        reason: Literal["init", "connect", "read_group", "poll"],
        state: Optional[ReconcileState] = None,
    ) -> Optional[DriverReconciliation]:
        """Align coordinator-owned state with what this brand has detected.

        Called by the coordinator at four moments, named by ``reason``: ``init``
        (end of the constructor's limit setup), ``connect`` (after a successful
        connect or fresh reconnect), ``read_group`` (after each telemetry group
        read) and ``poll`` (after each poll's data merge). ``data`` is the
        coordinator's current telemetry; ``state`` the coordinator-owned values
        in :class:`ReconcileState`. Returns what to apply, or None (default).
        """
        return None

    @property
    @abstractmethod
    def capabilities(self) -> DriverCapabilities:
        """Static traits of this battery (see :class:`DriverCapabilities`)."""

    @property
    def dc_coupled(self) -> bool:
        """Whether photovoltaic input reaches the battery without an AC stage.

        A hybrid inverter charges its battery straight from the strings; an AC
        battery has to take the same energy through an inverter and back again,
        and pays a conversion each way. The difference only matters when there is
        not enough sun for every battery: then the scarce kilowatt-hours are
        worth putting where the least of them is lost (see
        ``control/charge_order.py``).

        Defaults to False, which is what an AC battery is.
        """
        return False

    @property
    def allows_aggregate_solar(self) -> bool:
        """Whether the panel may read this battery's aggregate ``solar_power``.

        The panel fills a battery's PV from the single aggregate value only when
        it is a genuine PV source. A driver whose aggregate value is derived
        from the battery's own AC measurement returns False. Defaults to True.
        """
        return True

    @property
    def model_label(self) -> Optional[str]:
        """Human-readable model for display (panel chip / device page).

        Defaults to None; concrete drivers override (Marstek: "Venus <version>";
        Zendure: the report's ``product`` field). Not abstract so a driver with no
        model identity need not implement it.
        """
        return None

    @property
    def configuration_url(self) -> Optional[str]:
        """Web page of the device, shown as "Visit" on its device page."""
        return None

    def software_version(self, data: dict) -> Optional[str]:
        """Firmware line for the device page, read from the coordinator's data.

        None (the default) writes no firmware line. A method rather than a
        property because the value lives in the polled telemetry, not the driver.
        """
        return None

    @property
    def serial(self) -> Optional[str]:
        """Stable hardware serial, or None if the transport exposes none.

        Used to key the synthetic-energy backup so a deleted-and-re-added battery
        reclaims its lifetime totals (host/port can change with DHCP; the serial
        does not). Defaults to None; only drivers that read one override it.
        """
        return None

    # --- connection lifecycle ----------------------------------------------

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Whether the driver currently holds a live link to the hardware."""

    @abstractmethod
    async def connect(self) -> bool:
        """Establish the link. Return True on success. Idempotent / re-callable."""

    @abstractmethod
    async def close(self) -> None:
        """Tear the link down and release any single-slot resource (e.g. the v3
        TCP slot). Safe to call when already closed."""

    @abstractmethod
    def set_shutting_down(self, value: bool) -> None:
        """Suppress error logging during integration unload / HA shutdown."""

    # --- telemetry (read) ---------------------------------------------------

    @property
    @abstractmethod
    def read_groups(self) -> list[ReadGroup]:
        """Telemetry keys grouped into schedulable poll units (see :class:`ReadGroup`).

        The coordinator iterates these to schedule, gate and lock per group rather
        than branching on register layout. A polled Modbus driver returns one group
        per contiguous register block (read in a single request) plus a singleton
        group per remaining key; a push driver may return a single group of its
        cached state.
        """

    @abstractmethod
    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        """Return the latest decoded telemetry as a logical-key -> value mapping.

        ``keys`` optionally restricts the read to the given logical keys (used by
        the coordinator to honour per-sensor poll intervals and skip disabled
        entities); None means "everything this driver knows". A polled driver
        reads the hardware now; a push driver returns its cached last state.
        Missing/failed values are omitted rather than set to None.
        """

    @property
    def balance_dependency_keys(self) -> frozenset[str]:
        """Telemetry the cell-balance monitor needs beyond max/min cell voltage.

        The coordinator keeps these polling even when their entities are
        disabled, so a driver may expose a finer-grained breakdown as opt-in
        entities without the balance reading depending on the user enabling them.
        Empty for drivers whose max/min cell voltage already describes the whole
        battery.
        """
        return frozenset()

    @property
    def supplemental_discharge_dependency_keys(self) -> frozenset[str]:
        """Telemetry required to derive discharge omitted by hardware counters.

        The coordinator treats this as a semantic driver hook: an empty set means
        the hardware's cumulative discharge counter is complete. Drivers whose
        counter omits a discharge path return the native telemetry keys needed to
        identify and measure that path.
        """
        return frozenset()

    def supplemental_discharge_power_w(
        self,
        data: dict,
        updated_keys: frozenset[str],
    ) -> Optional[float]:
        """Return a fresh, normalized discharge sample missing from the counter.

        ``None`` means no relevant sample was refreshed in this poll. ``0`` means
        a relevant sample was refreshed but no supplemental discharge is active.
        A positive value is integrated by the shared energy layer. Drivers own
        all native key, state and model interpretation so callers never branch on
        a brand or firmware version.
        """
        return None

    # --- control (write) ----------------------------------------------------

    @abstractmethod
    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        """Command a signed net power: +charge / -discharge, 0 = idle/hold.

        The driver translates to its own wire format (Marstek: force_mode +
        charge/discharge registers; Zendure: input/output limit). ``mode_hint``
        is an optional control-layer intent ("charge"/"discharge"/"idle") that
        drivers with an explicit mode command may use; sign of ``net_power_w`` is
        authoritative. ``read_back=False`` skips confirmation to cut bus traffic
        (result carries ``confirmed=False``).
        """

    @abstractmethod
    async def write_control(self, key: str, value: int) -> bool:
        """Command a single logical control to a wire value.

        Generic entity-write path for the user-facing number/select/switch/button
        entities: the entity names a logical control *key* (e.g. ``force_mode``,
        ``rs485_control_mode``, a select option's underlying value) and supplies the
        already-encoded wire value; the driver resolves the key to its own wire
        detail (Marstek: the register address). This keeps the platform code
        register-free so a non-Modbus brand whose definitions carry no "register"
        does not break here. Returns True if the write was accepted, False if this
        driver has no control for the key or the write failed.
        """

    def dynamic_discharge_limit_w(self, data: dict) -> Optional[int]:
        """Live discharge ceiling below the static envelope, or None if there is none.

        The static envelope in :class:`DriverCapabilities` describes what the
        battery can do in isolation. A DC-coupled hybrid breaks that assumption:
        its battery and its PV strings share one inverter, so the power actually
        available for discharge is whatever the inverter's AC rating has left
        over after PV. That headroom changes with the sun, which a value fixed at
        setup time cannot express.

        Returning a live value lets the load-sharing logic allocate against real
        headroom rather than a nameplate figure it can never reach. ``data`` is
        the coordinator's telemetry cache; return None when the inputs are
        missing so the caller keeps the static limit rather than guessing.

        Drivers for AC batteries with their own inverter have no such coupling
        and inherit this default.
        """
        return None

    def dynamic_charge_limit_w(self, data: dict) -> Optional[int]:
        """Live charge ceiling below the static envelope, or None if there is none.

        The charge-side counterpart of :meth:`dynamic_discharge_limit_w`. A
        driver returns 0 while it cannot take a charge command at all (an
        Indevolt outside Realtime mode), so battery selection skips it and the
        other batteries take the load, instead of the controller allocating
        power the battery refuses until the non-responsive tracker excludes it.
        Near full it may return what the battery takes while its BMS tapers the
        charge (an Indevolt in its constant-voltage phase), so the controller
        commands that instead of a power the battery reports as not delivered.

        Defaults to None: the static limit stands, so other drivers are
        unaffected. See docs/indevolt-core-changes.md.
        """
        return None

    async def async_fix_issue(self, issue_id: str) -> bool:
        """Fix a Repairs issue this driver raised; True when it did.

        Called by the fix flow in ``repairs.py`` when the user submits a fixable
        issue whose data names this battery. The Indevolt driver uses it to turn
        setting writes back on after its yearly flash allowance stopped them.

        Defaults to False: other drivers raise no fixable issues, and the flow
        then keeps the issue. See docs/indevolt-core-changes.md.
        """
        return False

    @abstractmethod
    async def apply_config(
        self,
        *,
        max_soc_pct: float,
        min_soc_pct: float,
        max_charge_power_w: int,
        max_discharge_power_w: int,
    ) -> bool:
        """Write the one-time per-battery configuration (SOC window, power caps).

        Called once from setup, under the coordinator lock. The driver decides
        which of the values the hardware can hold; return True when nothing that
        was attempted failed.
        """

    @abstractmethod
    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        """Write the hardware charge ceiling (weekly full charge raises and restores it).

        Return False when the hardware has no such setting or the write failed.
        """

    @abstractmethod
    async def standby(self) -> bool:
        """Idle the battery for teardown (integration unload); True on success."""

    async def set_rs485_control(self, enable: bool) -> bool:
        """Enable/disable external (RS485) control mode; False when there is none.

        Defaults to False: only drivers whose ``capabilities.has_rs485_control``
        is True have a mode to toggle.
        """
        return False

    async def get_rs485_control(self) -> Optional[bool]:
        """Read the RS485 control mode back; None when unknown or not applicable."""
        return None

    @property
    def pack_field_specs(self) -> Sequence[dict]:
        """Numeric per-pack sensor fields; the sensor platform builds one entity
        per (pack, field). Empty for drivers that do not expose individual packs."""
        return ()

    def obsolete_entity_suffixes(self) -> Iterable[str]:
        """Entity key suffixes that no longer belong to this battery.

        Read by the sensor platform after the model is detected; it removes the
        registry entries ``<prefix>_<suffix>`` of this battery. Empty by default.
        The prefix is ``device_key``, plus the legacy ``host`` and ``name``
        prefixes when ``OBSOLETE_ENTITY_LEGACY_PREFIXES`` is set.
        """
        return ()

    # Entities of this brand were once keyed on host or name instead of
    # device_key, so obsolete ones are looked up under those prefixes too.
    OBSOLETE_ENTITY_LEGACY_PREFIXES: bool = False

    # --- native operating mode (only with ``supports_nom_mode``) ------------

    async def apply_operating_mode(
        self, mode: NativeOperatingMode, *, read_back: bool = True
    ) -> OperatingModeResult:
        """Switch the device's native mode.

        ``read_back`` waits a few seconds for the device to show the mode. The
        autonomous mode manager passes False and confirms on later cycles.
        """
        return OperatingModeResult(
            ok=False, confirmed=False, requested_mode=mode,
            failure_reason="unsupported_operating_mode",
        )

    def operating_mode_from_data(self, data: dict) -> Optional[NativeOperatingMode]:
        """The native mode the device is in now, or None when it is not known."""
        return None

    def telemetry_is_back(self) -> bool:
        """Whether a suspended battery is known to answer again, without asking it.

        Polling a battery that failed repeatedly is suspended for two minutes to
        spare it. A driver that can tell for free that the readings are back
        returns True and the suspension ends at once.
        """
        return False

    def power_sample_time(self) -> Any:
        """Identity of the power reading in use, when the source stamps it.

        Two equal answers are one reading seen twice. None: every poll is a
        reading of its own.
        """
        return None

    # --- entity definitions (read by the coordinator and the platforms) ----

    @property
    def sensor_definitions(self) -> list[dict]:
        """Sensor entity definitions. Empty unless the driver provides some."""
        return []

    @property
    def number_definitions(self) -> list[dict]:
        """Number entity definitions."""
        return []

    @property
    def select_definitions(self) -> list[dict]:
        """Select entity definitions."""
        return []

    @property
    def switch_definitions(self) -> list[dict]:
        """Switch entity definitions."""
        return []

    @property
    def binary_sensor_definitions(self) -> list[dict]:
        """Binary-sensor entity definitions."""
        return []

    @property
    def button_definitions(self) -> list[dict]:
        """Button entity definitions."""
        return []

    @property
    def all_definitions(self) -> list[dict]:
        """The polled union of the definitions above (the driver decides what
        that includes; buttons are not polled)."""
        return []

    @abstractmethod
    def net_power_from_data(self, data: dict) -> Optional[int]:
        """Derive the current commanded net power from coordinator telemetry cache.

        Returns the signed net power (+ charge / - discharge / 0 idle) last echoed
        into ``coordinator.data``, or None if the required keys are absent. None
        tells the skip-if-unchanged logic to fall through to a real write rather
        than incorrectly skipping it. Each driver reads its own brand-native keys
        (Marstek: force_mode + set_charge/discharge_power; Zendure: ac_mode +
        input/output_limit).
        """

    def inverter_status(self, data: dict) -> Optional[InverterStatus]:
        """The inverter's operating state in the canonical vocabulary.

        Reads ``inverter_state`` from the telemetry cache as the numeric codes of
        ``InverterStatus``. Returns None when the driver publishes no state, a
        state that is not a code (a text label) or a code outside the vocabulary:
        None means "cannot tell", never "idle". A driver whose telemetry uses
        another encoding overrides this.
        """
        raw = data.get("inverter_state")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        try:
            return InverterStatus(raw)
        except ValueError:
            return None

    def on_backup_power(self, data: dict) -> bool:
        """Whether a grid outage has the battery feeding its backup output.

        While it is True the battery is left out of automatic control. False
        unless the driver reports the outage itself (only Huawei does).
        """
        return False

    def active_alarms(self, data: dict) -> list[DriverAlarm]:
        """The alarms and faults currently active, decoded from ``data``.

        Faults first, then alarms, each in ascending ``code`` order: the order the
        system alarm sensor lists them and the notifier prints them. Defaults to
        none; a driver whose telemetry carries alarm or fault status overrides it.
        """
        return []

    def is_self_managed(self, data: dict) -> bool:
        """Whether the device is running its own routine and ignores setpoints.

        True while the device calibrates its SOC on its own: the core takes it
        out of automatic control until it finishes. Defaults to False; a driver
        whose telemetry reports such a state overrides this.
        """
        return False

    @property
    def manual_control_by_registers(self) -> bool:
        """Whether manual mode drives the hardware through force-mode entities.

        True when the driver publishes the ``force_mode`` select and the
        ``set_charge_power`` / ``set_discharge_power`` numbers: manual charge and
        discharge are then written straight to the device. False means the core
        asserts the manual setpoint through ``apply_setpoint`` every cycle.
        Defaults to ``capabilities.has_force_mode``; a driver that has the
        capability but publishes no such entities overrides this.
        """
        return self.capabilities.has_force_mode

    @property
    def critical_control_keys(self) -> frozenset:
        """Telemetry keys whose read groups count as safety-critical (issue #26).

        Defaults to ``CRITICAL_CONTROL_KEYS``, the canonical vocabulary; keys the
        driver does not publish never match.
        """
        return CRITICAL_CONTROL_KEYS

    def forcible_power_from_data(self, data: dict) -> float:
        """Signed power (+ charge / - discharge) the telemetry says is forced, else 0.

        Decodes the canonical ``force_mode`` codes (1 charge, 2 discharge) with
        ``set_charge_power`` / ``set_discharge_power``. Used as a last resort when
        ``net_power_from_data`` cannot tell; a negative setpoint counts as 0.
        """
        try:
            force_mode = int(round(float(data.get("force_mode"))))
        except (TypeError, ValueError):
            force_mode = 0
        if force_mode == 1:
            value = data.get("set_charge_power", 0)
            sign = 1
        elif force_mode == 2:
            value = data.get("set_discharge_power", 0)
            sign = -1
        else:
            return 0.0
        try:
            return sign * max(0.0, float(value or 0))
        except (TypeError, ValueError):
            return 0.0

    def device_max_soc(self, data: dict) -> Optional[float]:
        """The maximum SOC the device itself keeps, or None if it does not publish one.

        None means the device stores no ceiling of its own, so the integration's
        configured value is the only one. A driver whose device holds its own
        Target SOC overrides this.
        """
        return None

    def ac_register_power_w(self, data: dict) -> Optional[float]:
        """The device's own AC-side reading in +charge / -discharge watts, or None.

        None means the driver publishes no such reading (the default). A driver
        whose telemetry carries one overrides this and converts its sign; it
        raises ``TypeError``/``ValueError`` on a value that is not a number, like
        the other sources.
        """
        return None

    def ac_contribution_w(
        self, data: dict, order: Sequence[AcSource] = AC_REGISTER_FIRST
    ) -> Optional[float]:
        """What the battery puts on (-) or takes from (+) the AC bus, in watts.

        The first source in ``order`` that reads something wins; None when none
        does. Which sources and in which order is the caller's explicit choice:
        the sites that need it disagree and each keeps its own (see the
        ``AC_*`` orders above, "diferencia conservada, decidir (plan section 8,
        T7.4)"). A value that is not a number raises ``TypeError`` or
        ``ValueError`` rather than falling through to the next source.
        """
        for source in order:
            if source is AcSource.REGISTER:
                value = self.ac_register_power_w(data)
            elif source is AcSource.PORT:
                value = data.get(DELIVERED_AC_POWER_KEY)
            else:
                value = data.get("battery_power")
            if value is not None:
                return float(value)
        return None

    @property
    @abstractmethod
    def control_dependency_keys(self) -> frozenset:
        """Keys the coordinator must keep polling even when their entities are disabled.

        The control loop reads these from ``coordinator.data`` to drive set-points,
        power caps, and SOC cutoffs. Each driver returns only the keys relevant to
        its own telemetry model; the coordinator adds brand-agnostic keys separately.
        """
