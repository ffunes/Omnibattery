"""Observation-only battery driver backed by arbitrary Home Assistant entities.

Every other driver in this package owns a transport and can be told what to do.
This one owns neither. It exists for a battery that some *other* integration
already reads — a BYD stack behind a Kostal Plenticore, a vendor inverter with
its own HA integration — where Omnibattery can see the battery but cannot
command it.

Why a driver and not a sensor correction
----------------------------------------
The system aggregates derive household load as
``home = grid + sum(ac_power) + external_solar`` (``sensors/aggregate_sensors.py``,
``tracking/consumption_tracker.py``), and ``control/residual_load.py`` derives
"what the meter would read if every battery stopped" as ``grid + sum(ac_power)``.
A battery that is physically present but absent from ``controller.coordinators``
contributes no term to either, so its charging is billed to the house and its
discharging is subtracted from it — permanently, into the learned consumption
profile. The excluded-device machinery cannot stand in: it models a non-negative
draw (``infra/external_loads.py``), and a battery is bidirectional.

Putting it behind ``BatteryDriver`` is therefore the cheap way in: the shared
code already knows how to fold a coordinator's power into both formulas. What it
does *not* know is that this one must never be commanded, which is what
``DriverCapabilities.supports_power_setpoint=False`` tells it.

What it publishes, and what it deliberately does not
----------------------------------------------------
``ac_power`` is omitted on purpose. On a DC-coupled hybrid the inverter's AC
figure already has PV netted into it, so publishing it under that key counts the
array twice (the same trap the Huawei driver documents for its AC register).
With the key absent, the aggregates take their documented fallback
of ``-battery_power``, which is what a battery with no AC port of its own
actually contributes. ``has_mppt_pv`` must stay False for the same reason:
``residual_load._battery_ac_power_w`` returns None for an MPPT battery, and a
None there silently disables the second-regulator guards.

The one thing it can write
--------------------------
Some of these batteries expose a writable target or minimum SOC. That is not a
power setpoint and cannot stand in for one — it steers the vendor's own
regulator rather than replacing it — so it lives behind its own capability and
its own entity, never on the ``apply_setpoint`` path.

The optional mode entity is the other one: a select or switch of the source
integration, mapped to NOM and Manual. With it the battery declares
``supports_nom_mode`` and ``control/autonomous_mode.py`` drives it; without it
the battery is only observed.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional, Sequence

from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .base import (
    BatteryDriver,
    DriverCapabilities,
    DriverContext,
    DriverTraits,
    NativeOperatingMode,
    OperatingModeResult,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)
from .generic_ha_flow import GENERIC_HA_SETUP

_LOGGER = logging.getLogger(__name__)

# A source integration polls on its own schedule, and some vendor integrations
# are slow (Kostal Plenticore defaults to 30 s, others to a minute or more).
# This is a liveness heartbeat, not a freshness budget: it exists so the
# coordinator's failure ladder has something to fail on when the upstream
# integration dies and HA keeps serving its last states forever. Generous by
# design — too tight and a healthy slow poller flaps the battery offline.
DEFAULT_STALE_AFTER_S = 600.0

# A source that only reports changes says nothing about a battery at rest, all
# night long. Silence is only a loss of telemetry while the last power says the
# battery was doing something: a real measurement of that keeps moving.
# ponytail: a source that dies without its entities going unavailable while the
# battery rests is taken for a resting battery.
RESTING_POWER_W = 50.0

# Without these two the driver has no reason to exist: SOC without power fixes
# none of the measurement errors above, and power without SOC tells planning
# nothing about the battery's state.
_REQUIRED_KEYS: frozenset[str] = frozenset({"battery_soc", "battery_power"})

# The unit each key is published in; a source in kW or Wh is converted.
_UNITS = {
    "battery_power": (PowerConverter, UnitOfPower.WATT),
    "total_charging_energy": (EnergyConverter, UnitOfEnergy.KILO_WATT_HOUR),
    "total_discharging_energy": (EnergyConverter, UnitOfEnergy.KILO_WATT_HOUR),
}

# A vendor service that never answers must not hold a mode change forever, and
# a device behind a cloud shows the new mode some seconds after accepting it.
MODE_WRITE_TIMEOUT_S = 10.0
MODE_CONFIRM_TIMEOUT_S = 5.0

POWER_POSITIVE_CHARGE = "charge"
POWER_POSITIVE_DISCHARGE = "discharge"


SENSOR_DEFINITIONS: list[dict] = [
    {"name": "Battery SOC", "key": "battery_soc", "unit": "%",
     "device_class": "battery", "state_class": "measurement",
     "scale": 1, "precision": 1, "scan_interval": "high", "enabled_by_default": True},
    {"name": "Battery Power", "key": "battery_power", "unit": "W",
     "device_class": "power", "state_class": "measurement",
     "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"name": "Total Charging Energy", "key": "total_charging_energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total_increasing",
     "scale": 1, "precision": 3, "scan_interval": "low", "enabled_by_default": True},
    {"name": "Total Discharging Energy", "key": "total_discharging_energy", "unit": "kWh",
     "device_class": "energy", "state_class": "total_increasing",
     "scale": 1, "precision": 3, "scan_interval": "low", "enabled_by_default": True},
]

# Only created when a writable target-SOC entity was configured. min/max/step are
# refined from the upstream entity's own attributes on connect(), because the
# vendor decides them: the Kostal number accepts 5..100 in steps of 5, and an
# entity that shows a value its write path rejects is worse than no entity (#495).
TARGET_SOC_DEFINITION: dict = {
    "name": "Target SOC", "key": "target_soc", "unit": "%",
    "icon": "mdi:battery-lock", "min": 5, "max": 100, "step": 5,
    "scale": 1, "enabled_by_default": True, "scan_interval": "medium",
}


def mode_entity_options(hass: HomeAssistant, entity_id: Optional[str]) -> list[str]:
    """The states the mode entity can be put in; empty when they are not known."""
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return []
    if entity_id.split(".")[0] in ("select", "input_select"):
        return [str(option) for option in state.attributes.get("options") or []]
    return ["on", "off"]


class GenericHaEntityDriver(BatteryDriver):
    """Reads one battery from configured HA entities; sets none of its power."""

    BRAND = "generic_ha"
    SETUP = GENERIC_HA_SETUP
    LABEL = "Other battery via Home Assistant entities"
    # The device registry wants a vendor before any entity has been read, and
    # the honest answer at that point is where the readings come from rather
    # than a guess at whose battery it is. ``model_label`` refines it to the
    # actual source integration ("Kostal Plenticore") once the registry knows.
    MANUFACTURER = "Home Assistant"
    DEFAULT_MODEL = "Observed battery"
    TRAITS = DriverTraits(
        takes_power_setpoint=False,
        # Nothing is written at setup: every limit belongs to the vendor's own
        # regulator, which Omnibattery does not reach.
        device_owns_initial_config=True,
        # No voltage taper: a taper shapes the end of a charge this integration
        # never commands.
        supports_voltage_taper=False,
        # Addressed by a generated id, not an IP, so no address can drift and
        # MAC tracking has nothing to follow.
        ip_addressed=False,
        # There is no capacity register anywhere in sight; the user supplies it.
        capacity_field="required",
    )

    @classmethod
    def from_config(cls, battery_config, context: DriverContext) -> "GenericHaEntityDriver":
        """Build the driver from the entity ids the config flow stored."""
        return cls(
            context.hass,
            soc_entity=battery_config["ha_soc_entity"],
            power_entity=battery_config["ha_power_entity"],
            power_positive_is=battery_config.get(
                "ha_power_positive_is", POWER_POSITIVE_CHARGE
            ),
            charge_energy_entities=battery_config.get("ha_charge_energy_entities") or (),
            discharge_energy_entity=battery_config.get("ha_discharge_energy_entity"),
            target_soc_entity=battery_config.get("ha_target_soc_entity"),
            mode_entity=battery_config.get("ha_mode_entity"),
            mode_nom_option=battery_config.get("ha_mode_nom_option"),
            mode_manual_option=battery_config.get("ha_mode_manual_option"),
            stale_after_s=float(
                battery_config.get("ha_stale_after_s") or DEFAULT_STALE_AFTER_S
            ),
            label=context.name or cls.BRAND,
        )

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        soc_entity: str,
        power_entity: str,
        power_positive_is: str = POWER_POSITIVE_CHARGE,
        charge_energy_entities: Sequence[str] = (),
        discharge_energy_entity: Optional[str] = None,
        target_soc_entity: Optional[str] = None,
        mode_entity: Optional[str] = None,
        mode_nom_option: Optional[str] = None,
        mode_manual_option: Optional[str] = None,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        label: str = "generic_ha",
    ) -> None:
        self.hass = hass
        self._label = label
        self._soc_entity = soc_entity
        self._power_entity = power_entity
        self._power_sign = -1.0 if power_positive_is == POWER_POSITIVE_DISCHARGE else 1.0
        self._charge_energy_entities = tuple(e for e in charge_energy_entities if e)
        self._discharge_energy_entity = discharge_energy_entity or None
        self._target_soc_entity = target_soc_entity or None
        self._mode_entity = mode_entity or None
        # An incomplete mapping is no mapping: the battery is only observed.
        self._mode_options: dict[NativeOperatingMode, str] = {}
        if (
            self._mode_entity and mode_nom_option and mode_manual_option
            and mode_nom_option != mode_manual_option
        ):
            self._mode_options = {
                NativeOperatingMode.NOM: mode_nom_option,
                NativeOperatingMode.MANUAL: mode_manual_option,
            }
        self._stale_after_s = float(stale_after_s)

        self._connected = False
        self._shutting_down = False
        self._stalled = False
        self._manufacturer: Optional[str] = None

        self._capabilities = DriverCapabilities(
            # Nothing here is enforced by hardware Omnibattery can reach, and
            # nothing here is commanded by Omnibattery either. Both halves of the
            # SOC story belong to the vendor's regulator.
            hardware_soc_cutoff=False,
            has_force_mode=False,
            push_telemetry=True,          # source integration writes into hass.states
            telemetry_liveness_checked=True,
            # The envelope shared code may command. Zero is the honest answer and
            # a second line of defence: even if a call site forgets the capability
            # gate below, there is no power to allocate.
            max_charge_power_w=0,
            max_discharge_power_w=0,
            has_mppt_pv=False,
            has_solar_telemetry=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            # Lifetime counters come from the source integration when configured;
            # none of these devices resets one daily, so the entity layer derives
            # the daily figures from cumulative deltas.
            has_energy_counters=bool(self._charge_energy_entities or self._discharge_energy_entity),
            has_daily_energy_counters=False,
            # No capacity register anywhere in sight: the user supplies it and the
            # coordinator injects it as battery_total_energy.
            has_nominal_capacity=False,
            cycles_from_discharge_only=True,
            supports_power_setpoint=False,
            supports_nom_mode=bool(self._mode_options),
        )

        self._target_soc_definition = dict(TARGET_SOC_DEFINITION)
        self._rebuild_definitions()

    # --- definitions ---------------------------------------------------------

    def _rebuild_definitions(self) -> None:
        sensors = [dict(d) for d in SENSOR_DEFINITIONS]
        if not self._charge_energy_entities:
            sensors = [d for d in sensors if d["key"] != "total_charging_energy"]
        if not self._discharge_energy_entity:
            sensors = [d for d in sensors if d["key"] != "total_discharging_energy"]
        numbers = [dict(self._target_soc_definition)] if self._target_soc_entity else []

        self._definitions: dict[str, list[dict]] = {
            "sensor": sensors,
            "number": numbers,
            "select": [],
            "switch": [],
            "binary_sensor": [],
            "button": [],
            "all": sensors + numbers,
        }
        # One group: every value is a hass.states lookup, so there is nothing to
        # batch or pace. The coordinator still gates per key.
        self._read_groups = [
            ReadGroup(scan_interval="high", keys=tuple(d["key"] for d in self._definitions["all"]))
        ]

    @property
    def sensor_definitions(self) -> list[dict]:
        return self._definitions["sensor"]

    @property
    def number_definitions(self) -> list[dict]:
        return self._definitions["number"]

    @property
    def select_definitions(self) -> list[dict]:
        return self._definitions["select"]

    @property
    def switch_definitions(self) -> list[dict]:
        return self._definitions["switch"]

    @property
    def binary_sensor_definitions(self) -> list[dict]:
        return self._definitions["binary_sensor"]

    @property
    def button_definitions(self) -> list[dict]:
        return self._definitions["button"]

    @property
    def all_definitions(self) -> list[dict]:
        return self._definitions["all"]

    # --- identity / capabilities ---------------------------------------------

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def model_label(self) -> Optional[str]:
        """Name the integration the readings come from, not a guessed vendor.

        ``MANUFACTURER`` is fixed at class level and cannot know whose battery
        this is. What is both true and useful is where the data originates, so
        a BYD stack read through ``kostal_plenticore`` shows as "Kostal
        Plenticore (observed)". Falls back to ``DEFAULT_MODEL`` by returning
        None only if the registry has nothing at all.
        """
        if self._manufacturer is None:
            self._manufacturer = self._resolve_source_integration()
        return self._manufacturer

    def _resolve_source_integration(self) -> Optional[str]:
        """Title-case the platform that owns the SOC entity, or None if unknown."""
        try:
            entry = er.async_get(self.hass).async_get(self._soc_entity)
        except Exception:  # noqa: BLE001 - registry unavailable in tests/startup
            entry = None
        platform = getattr(entry, "platform", None)
        if not platform:
            return None
        return f"{platform.replace('_', ' ').title()} (observed)"

    # --- lifecycle -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @classmethod
    def resolve(cls, hass: HomeAssistant, entities: dict[str, str]) -> list[str]:
        """Return the required logical keys whose entity is missing or unreadable.

        Shared with the config flow so setup fails on the form with a named key
        rather than at runtime with an unavailable battery.
        """
        missing = []
        for key in sorted(_REQUIRED_KEYS):
            entity_id = entities.get(key)
            if not entity_id:
                missing.append(key)
                continue
            if _read_state(hass, entity_id, key) is None:
                missing.append(key)
        return missing

    async def connect(self) -> bool:
        """Validate the required entities exist and currently read as numbers."""
        missing = self.resolve(
            self.hass,
            {"battery_soc": self._soc_entity, "battery_power": self._power_entity},
        )
        if missing:
            if not self._shutting_down:
                _LOGGER.warning(
                    "%s: required entities missing or unreadable: %s",
                    self._label, ", ".join(missing),
                )
            self._connected = False
            return False
        self._refine_target_soc_definition()
        self._connected = True
        return True

    def _refine_target_soc_definition(self) -> None:
        """Adopt the upstream number's own min/max/step so the entity cannot lie."""
        if not self._target_soc_entity:
            return
        state = self.hass.states.get(self._target_soc_entity)
        attrs = getattr(state, "attributes", {}) or {}
        changed = False
        for attr in ("min", "max", "step"):
            value = _to_float(attrs.get(attr))
            if value is None:
                continue
            if attr == "step" and value <= 0:
                continue
            if self._target_soc_definition.get(attr) != value:
                self._target_soc_definition[attr] = value
                changed = True
        if changed:
            self._rebuild_definitions()

    async def close(self) -> None:
        self._connected = False

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = bool(value)

    # --- telemetry -----------------------------------------------------------

    @property
    def read_groups(self) -> list[ReadGroup]:
        return self._read_groups

    def _entity_for(self, key: str) -> Optional[str]:
        if key == "battery_soc":
            return self._soc_entity
        if key == "battery_power":
            return self._power_entity
        if key == "total_discharging_energy":
            return self._discharge_energy_entity
        if key == "target_soc":
            return self._target_soc_entity
        return None

    def _stall_age_s(self) -> Optional[float]:
        """Seconds since the freshest required entity last reported, or None.

        ``last_reported`` advances on every state write including a repeat, so
        this dates the upstream integration's last word rather than the last time
        a number moved. Returns None when no timestamp is available, so the guard
        fails open on a test double or an exotic source.
        """
        newest = None
        for entity_id in (self._soc_entity, self._power_entity):
            state = self.hass.states.get(entity_id)
            reported = getattr(state, "last_reported", None) if state else None
            if reported is not None and (newest is None or reported > newest):
                newest = reported
        if newest is None:
            return None
        return (dt_util.utcnow() - newest).total_seconds()

    def _is_stalled(self, power: float, age: Optional[float]) -> bool:
        """Whether the source has been silent too long for a battery doing ``power``."""
        return (
            age is not None and age > self._stale_after_s and abs(power) > RESTING_POWER_W
        )

    def telemetry_is_back(self) -> bool:
        """Whether a read would answer now: these are lookups in hass.states."""
        power = self._read_one("battery_power", self._power_entity)
        return (
            power is not None
            and self._read_one("battery_soc", self._soc_entity) is not None
            and not self._is_stalled(power, self._stall_age_s())
        )

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        """Return the configured entities' states as a snapshot.

        Unavailable, unknown or unparseable states are omitted rather than
        zeroed: zero is a valid battery power, and a fabricated one would be
        indistinguishable from a resting battery. Without both required readings
        the snapshot is empty.
        """
        power = self._read_one("battery_power", self._power_entity)
        if power is None or self._read_one("battery_soc", self._soc_entity) is None:
            # Half a battery is a lost battery: another entity still answering
            # (the target SOC may live elsewhere) must not keep it looking alive
            # on its last power.
            return {}
        age = self._stall_age_s()
        if self._is_stalled(power, age):
            if not self._stalled:
                self._stalled = True
                _LOGGER.warning(
                    "%s: no battery telemetry for %.0fs (limit %.0fs) — the source "
                    "integration looks stalled. Treating the cached states as "
                    "unusable rather than planning on them.",
                    self._label, age, self._stale_after_s,
                )
            return {}
        if self._stalled:
            self._stalled = False
            _LOGGER.info("%s: battery telemetry is flowing again", self._label)

        wanted = list(keys) if keys is not None else [d["key"] for d in self._definitions["all"]]
        snapshot: TelemetrySnapshot = {}
        for key in wanted:
            if key == "total_charging_energy":
                value = self._read_sum(key, self._charge_energy_entities)
            else:
                value = self._read_one(key, self._entity_for(key))
            if value is None:
                continue
            if key == "battery_power":
                # Omnibattery's convention is positive-charges. A source that
                # signs it the other way round (Kostal) is flipped here, once.
                value = value * self._power_sign
            snapshot[key] = value
        return snapshot

    def _read_one(self, key: str, entity_id: Optional[str]) -> Optional[float]:
        return _read_state(self.hass, entity_id, key) if entity_id else None

    def _read_sum(self, key: str, entity_ids: Sequence[str]) -> Optional[float]:
        """Sum several counters into one logical total; None unless all read.

        A partial sum would step backwards whenever one source blinks, and the
        coordinator guards ``total_increasing`` keys against exactly that by
        discarding the reading — so returning None is both honest and cheaper.
        """
        if not entity_ids:
            return None
        total = 0.0
        for entity_id in entity_ids:
            value = self._read_one(key, entity_id)
            if value is None:
                return None
            total += value
        return total

    # --- control -------------------------------------------------------------

    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        """Accept and discard a power command; this battery has no such input.

        Shared control is gated on ``supports_power_setpoint`` and should never
        reach here. It still answers coherently rather than raising, because a
        driver that blows up on an unexpected call takes the whole control cycle
        with it — and reports the idle it actually holds, not the command it was
        handed, so delivery checks compare against the truth.
        """
        _LOGGER.debug(
            "%s: ignoring %d W setpoint — this battery accepts no power command",
            self._label, net_power_w,
        )
        return SetpointResult(
            ok=True,
            net_power_w=0,
            confirmed=True,
            exact=False,
            battery_power_w=None,
            applied={},
        )

    async def write_control(self, key: str, value: int) -> bool:
        """Write the one control this battery has, if it has it."""
        if key != "target_soc" or not self._target_soc_entity:
            _LOGGER.debug("%s: no control mapping for key %r", self._label, key)
            return False
        target = self._clamp_target_soc(value)
        try:
            await self.hass.services.async_call(
                self._target_soc_entity.split(".")[0], "set_value",
                {"entity_id": self._target_soc_entity, "value": target},
                blocking=True,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - any failure is just a failed write
            if not self._shutting_down:
                _LOGGER.warning(
                    "%s: writing %s to %s failed: %s",
                    self._label, target, self._target_soc_entity, exc,
                )
            return False

    def _clamp_target_soc(self, value) -> float:
        """Snap a requested SOC onto the grid the upstream entity accepts."""
        definition = self._target_soc_definition
        low = float(definition.get("min", 0))
        high = float(definition.get("max", 100))
        step = float(definition.get("step", 1)) or 1.0
        requested = _to_float(value)
        if requested is None:
            requested = low
        snapped = low + round((requested - low) / step) * step
        snapped = max(low, min(high, snapped))
        return round(snapped, 3)

    def power_sample_time(self):
        """When the source last reported the power: it may poll every 30 s or more."""
        return getattr(self.hass.states.get(self._power_entity), "last_reported", None)

    def operating_mode_from_data(self, data: dict) -> Optional[NativeOperatingMode]:
        """The mode entity's live state; None when it is neither of the two."""
        state = self.hass.states.get(self._mode_entity) if self._mode_options else None
        if state is None:
            return None
        for mode, option in self._mode_options.items():
            if state.state == option:
                return mode
        return None

    async def apply_operating_mode(
        self, mode: NativeOperatingMode, *, read_back: bool = True
    ) -> OperatingModeResult:
        """Put the mode entity in the option mapped to ``mode``."""
        if not self._mode_options:
            return await super().apply_operating_mode(mode, read_back=read_back)
        option = self._mode_options[mode]
        domain = self._mode_entity.split(".")[0]
        data = {"entity_id": self._mode_entity}
        if domain in ("select", "input_select"):
            service = "select_option"
            data["option"] = option
        else:
            service = "turn_on" if option == "on" else "turn_off"
        failure = None
        try:
            async with asyncio.timeout(MODE_WRITE_TIMEOUT_S):
                await self.hass.services.async_call(domain, service, data, blocking=True)
        except TimeoutError:
            failure = "timeout"
        except Exception as exc:  # noqa: BLE001 - any failure is just a failed write
            failure = str(exc) or type(exc).__name__
        effective = self.operating_mode_from_data({})
        if failure is None and read_back:
            waited = 0.0
            while effective is not mode and waited < MODE_CONFIRM_TIMEOUT_S:
                await asyncio.sleep(0.25)
                waited += 0.25
                effective = self.operating_mode_from_data({})
        if failure is not None and not self._shutting_down:
            _LOGGER.warning(
                "%s: switching %s to %s failed: %s",
                self._label, self._mode_entity, option, failure,
            )
        return OperatingModeResult(
            ok=failure is None,
            confirmed=effective is mode,
            requested_mode=mode,
            effective_mode=effective,
            failure_reason=failure,
        )

    def net_power_from_data(self, data: dict) -> Optional[int]:
        """No command was ever issued, so there is no echo to reconstruct."""
        return None

    @property
    def control_dependency_keys(self) -> frozenset:
        """Keys the shared layers need even with their entities disabled.

        SOC and power feed the household-load balance and the residual-load
        reconstruction whether or not the user keeps the entities around, and
        those are the reason this driver exists.
        """
        return frozenset({"battery_soc", "battery_power"})

    def dynamic_discharge_limit_w(self, data: dict) -> Optional[int]:
        """Zero: nothing may be allocated to a battery that takes no commands."""
        return 0

    # --- coordinator-called hooks (not abstract on BatteryDriver) -------------

    async def apply_config(
        self,
        *,
        max_soc_pct: float,
        min_soc_pct: float,
        max_charge_power_w: int,
        max_discharge_power_w: int,
    ) -> bool:
        """Accept setup configuration without writing anything.

        Reporting success is deliberate, not sloppy: setup raises
        ``ConfigEntryNotReady`` on a False here, and there is no failure to
        report — the settings simply do not apply to a device Omnibattery does
        not configure. Sessy and Hoymiles answer the same way.
        """
        return True

    async def set_charge_cutoff(self, soc_pct: float) -> bool:
        """No hardware cutoff is reachable from here."""
        return False

    async def set_rs485_control(self, enable: bool) -> bool:
        """No external-control gate exists on an observed battery."""
        return False

    async def standby(self) -> bool:
        """Switch to Manual and confirm it; nothing to release without a mode."""
        if not self._mode_options:
            return True
        return (await self.apply_operating_mode(NativeOperatingMode.MANUAL)).confirmed


def _read_state(hass: HomeAssistant, entity_id: str, key: str) -> Optional[float]:
    """Read an entity as ``key``'s number in its published unit, or None.

    An entity without a unit is taken at face value; one whose unit does not
    measure the same thing (a percentage picked as power) is unreadable.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    value = _to_float(state.state)
    if value is None or key not in _UNITS:
        return value
    converter, target = _UNITS[key]
    unit = (getattr(state, "attributes", None) or {}).get("unit_of_measurement") or target
    try:
        return converter.convert(value, unit, target)
    except HomeAssistantError:
        return None


def _to_float(raw) -> Optional[float]:
    """Parse an HA state or attribute into a float, or None if it is not one."""
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):  # NaN / inf never reach the control layer
        return None
    return value
