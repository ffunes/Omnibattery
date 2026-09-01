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

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


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
    # will not sustain a non-zero charge/discharge. Marstek v2/v3 report 800 W (the
    # max_charge/discharge_power register floor); vA/vD/Zendure have no such floor
    # and report 0. The thermal derate clamps its non-zero output up to this value
    # so it never dribbles an unreliable sub-minimum command. Defaults to 0 (no floor).
    min_charge_power_w: int = 0
    min_discharge_power_w: int = 0


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


class BatteryDriver(ABC):
    """Abstract hardware driver for a single physical battery.

    One instance per battery (per coordinator). Owns its transport and connection
    state. All methods are async because every real transport (Modbus TCP, MQTT)
    is I/O bound.
    """

    # --- identity -----------------------------------------------------------

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
    def model_label(self) -> Optional[str]:
        """Human-readable model for display (panel chip / device page).

        Defaults to None; concrete drivers override (Marstek: "Venus <version>";
        Zendure: the report's ``product`` field). Not abstract so a driver with no
        model identity need not implement it.
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

    @property
    @abstractmethod
    def control_dependency_keys(self) -> frozenset:
        """Keys the coordinator must keep polling even when their entities are disabled.

        The control loop reads these from ``coordinator.data`` to drive set-points,
        power caps, and SOC cutoffs. Each driver returns only the keys relevant to
        its own telemetry model; the coordinator adds brand-agnostic keys separately.
        """
