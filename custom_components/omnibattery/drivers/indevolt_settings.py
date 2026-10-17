"""Indevolt user settings on the config channel, kept in sync both ways.

Three Indevolt settings have an Omnibattery counterpart: the reserve (min SOC),
the max AC input (max charge power) and the max AC output (max discharge power).
They live in the battery's flash, so they are written only on a direct user
action (see FLASH WEAR in ``infra/indevolt_client.py``):

* **Omnibattery -> device:** a number entity or an options-flow save requests a
  value. After a debounce the value is compared with what the device holds and
  written only if it differs, within a yearly allowance shared by the three
  settings. Every flash write is logged at INFO.
* **Device -> Omnibattery:** a value changed elsewhere (the Indevolt app, the
  official integration) is adopted and stored in the battery's config, without
  writing anything.

Nothing here runs on a timer or from the control loop, and nothing is written at
startup. An options-flow save reloads the entry; ``apply_config`` then tells a
user change apart from a stale config by comparing it with the last value both
sides agreed on (the ``indevolt_synced_*`` keys).

Max SOC has no Indevolt setting; the core enforces it in software.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..infra.indevolt_client import CONFIG_WRITE_POINTS, IndevoltClient, IndevoltError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Setting:
    key: str  # logical key: coordinator data and number entity
    config_key: str  # the battery config key Omnibattery keeps
    read_point: int
    write_point: int  # a CONFIG_WRITE_POINTS member


MIN_SOC = Setting("discharging_cutoff_capacity", "min_soc", 6105, 1142)
MAX_CHARGE = Setting("max_charge_power", "max_charge_power", 11009, 1138)
MAX_DISCHARGE = Setting("max_discharge_power", "max_discharge_power", 11011, 1147)
SETTINGS = (MIN_SOC, MAX_CHARGE, MAX_DISCHARGE)
BY_KEY = {s.key: s for s in SETTINGS}
# apply_config keyword -> setting (max_soc_pct has no device setting).
APPLY_CONFIG_SETTINGS = {
    "min_soc_pct": MIN_SOC,
    "max_charge_power_w": MAX_CHARGE,
    "max_discharge_power_w": MAX_DISCHARGE,
}
assert all(s.write_point in CONFIG_WRITE_POINTS for s in SETTINGS)

# Sliders send one value when released; a few quick adjustments or an
# automation firing in a burst collapse to one write: the newest value wins
# once nothing changed for this long.
DEBOUNCE_S = 3.0
# Flash wear guard. The three settings count together (they most likely share
# one flash block), and every write that may have reached the device counts,
# also one that failed: a battery can store a value and still not answer. Once
# this many writes fall within a rolling year, writing stays off until the user
# turns it back on in Repairs, which starts a new allowance; changes made on the
# device are still adopted. Without a person stepping in, Omnibattery therefore
# writes the battery's flash at most 100 times a year, whatever goes wrong. A
# person changing settings stays far below it; an automation that changes them
# every day reaches it after 100 days, and the Repairs issue says why.
YEARLY_ALLOWANCE = 100
ALLOWANCE_WINDOW_S = 365 * 86400.0
# The guard lives in the battery config, with wall-clock times, so neither a
# restart nor a reload loop resets it: {"times": [...], "total": n, "paused": b}.
FLASH_WRITES_KEY = "indevolt_flash_writes"
# The Repairs issue that turns writing back on (raised by the notifier).
FLASH_WRITES_ISSUE = "indevolt_flash_writes_paused"
# A written value is shown until the device reports it back, at most this long.
CONFIRM_TIMEOUT_S = 600.0
SYNCED_PREFIX = "indevolt_synced_"


class SettingsNotifier(Protocol):
    def setting_failed(self, key: str, value: int) -> None: ...
    def setting_ok(self, key: str) -> None: ...
    def flash_writes_paused(self, allowance: int) -> None: ...
    def flash_writes_resumed(self) -> None: ...


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


class IndevoltSettings:
    """Two-way sync of the Indevolt user settings for one battery."""

    def __init__(
        self,
        *,
        client: Callable[[], IndevoltClient | None],
        read: Callable[[int], float | None],
        expedite: Callable[[], None],
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[Any]],
        persist: Callable[[str, Any], None] | None = None,
        stored: Callable[[str], Any] | None = None,
        notifier: SettingsNotifier | None = None,
        name: str = "Indevolt",
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._read = read
        self._expedite = expedite
        self._clock = clock
        # ``clock`` may be monotonic, which restarts with Home Assistant; the
        # yearly allowance is persisted, so it needs wall-clock time.
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._persist = persist
        self._stored = stored
        self._notifier = notifier
        self._name = name
        self._pending: dict[str, tuple[int, str, float]] = {}  # key -> (value, origin, changed at)
        self._written: dict[str, tuple[int, float]] = {}  # accepted, not yet read back
        # Last value both sides agreed on, per setting. Loaded on first use: the
        # config entry is attached to the coordinator after the driver is built.
        self._synced_store: dict[str, int | None] | None = None
        self._synced_at_start: dict[str, int | None] = {}
        # The flash wear guard (see YEARLY_ALLOWANCE), loaded on first use too.
        self._guard_loaded = False
        self._write_times: list[float] = []  # wall clock, within the window
        self._writes_total = 0
        self._paused = False
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    # --- reading --------------------------------------------------------------------

    def device_value(self, setting: Setting) -> int | None:
        value = self._read(setting.read_point)
        return None if value is None else int(round(value))

    def published(self, key: str) -> int | None:
        """The value Omnibattery should show and use: a requested or just-written
        value until the device reports it, otherwise the device's own."""
        if key in self._pending:
            return self._pending[key][0]
        if key in self._written:
            return self._written[key][0]
        return self.device_value(BY_KEY[key])

    @property
    def flash_writes(self) -> int:
        """Every write that may have reached the battery's flash, ever (the sensor)."""
        self._load_guard()
        return self._writes_total

    def writes_this_year(self) -> int:
        self._load_guard()
        self._prune()
        return len(self._write_times)

    def resume_flash_writes(self) -> None:
        """The user turned writing back on in Repairs: a new allowance starts."""
        self._load_guard()
        if not self._paused:
            return
        _LOGGER.info("[%s] Indevolt setting writes turned back on; a new allowance of %d starts",
                     self._name, YEARLY_ALLOWANCE)
        self._paused = False
        self._write_times.clear()
        self._save_guard()
        if self._notifier is not None:
            self._notifier.flash_writes_resumed()

    # --- user intent ------------------------------------------------------------------

    def request(self, key: str, value: int, *, origin: str) -> bool:
        """A direct user action asks for ``value``. True: it will be applied."""
        setting = BY_KEY.get(key)
        if setting is None or self._closed:
            return False
        low, high = CONFIG_WRITE_POINTS[setting.write_point]
        if type(value) is not int or not low <= value <= high:
            _LOGGER.warning("[%s] Indevolt %s %r is outside %d-%d; not written",
                            self._name, key, value, low, high)
            return False
        if not self._flash_writes_allowed():
            return False
        self._pending[key] = (value, origin, self._clock())
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())
        return True

    def apply_config(self, values: dict[str, Any]) -> None:
        """Called at setup with the battery config. Writes only a user change.

        A value that differs from the last one both sides agreed on was changed
        in the options flow (which reloads the entry), so it is user intent. An
        equal value means nothing changed on Omnibattery's side: the device stays
        the source of truth and is adopted on the next read. Without an agreed
        value (first start) nothing is written either.
        """
        for kwarg, setting in APPLY_CONFIG_SETTINGS.items():
            # The agreed value as it was at startup: the first read may already
            # have adopted a device change before setup calls this.
            configured = values.get(kwarg)
            synced = self._load_synced_at_start(setting.key)
            if configured is None or synced is None:
                continue
            try:
                configured = int(round(float(configured)))
            except (TypeError, ValueError):
                continue
            if configured != synced:
                _LOGGER.info("[%s] Indevolt %s changed in the options to %d (was %d)",
                             self._name, setting.key, configured, synced)
                self.request(setting.key, configured, origin="options flow")

    # --- device -> Omnibattery ---------------------------------------------------------

    def on_telemetry(self) -> None:
        """Confirm written values and adopt values changed outside Omnibattery."""
        self._load_guard()  # raises the Repairs issue again after a restart
        now = self._clock()
        for setting in SETTINGS:
            key = setting.key
            device = self.device_value(setting)
            if device is None:
                continue
            if key in self._written:
                value, at = self._written[key]
                if device == value:
                    del self._written[key]
                    self._agree(setting, device)
                elif now - at > CONFIRM_TIMEOUT_S:
                    del self._written[key]
                    _LOGGER.warning(
                        "[%s] Indevolt accepted %s = %d but still reports %d; adopting the "
                        "device value", self._name, key, value, device)
                    self._fail(setting, value)
                continue
            if key in self._pending:
                continue
            if device != self._synced[key]:
                if self._synced[key] is not None:
                    _LOGGER.info("[%s] Indevolt %s changed on the device: %d -> %d; adopted",
                                 self._name, key, self._synced[key], device)
                self._agree(setting, device)
            else:
                self._restore_config(setting)

    # --- writing ------------------------------------------------------------------------

    async def _run(self) -> None:
        while self._pending and not self._closed:
            due = min(changed for _, _, changed in self._pending.values()) + DEBOUNCE_S
            if self._clock() < due:
                await self._sleep(due - self._clock())
                continue
            now = self._clock()
            for key, (value, origin, changed) in list(self._pending.items()):
                if now - changed < DEBOUNCE_S:
                    continue
                await self._write(BY_KEY[key], value, origin)
                # A newer request for this key arrived while writing: keep it.
                if self._pending.get(key, (None, None, changed))[2] == changed:
                    self._pending.pop(key, None)

    async def _write(self, setting: Setting, value: int, origin: str) -> None:
        # While an earlier write is unconfirmed, the cached reading may predate
        # it (the battery applies a setting a moment after accepting it), so the
        # battery most likely holds the written value, not the cached one.
        unconfirmed = self._written.pop(setting.key, None)
        device = unconfirmed[0] if unconfirmed is not None else self.device_value(setting)
        if device == value:
            _LOGGER.debug("[%s] Indevolt %s already %d; no write", self._name, setting.key, value)
            if unconfirmed is not None:
                self._written[setting.key] = unconfirmed  # still to be confirmed
            self._agree(setting, value)
            return
        if not self._flash_writes_allowed():
            return
        client = self._client()
        if client is None:
            self._fail(setting, value)
            return
        # Counted before sending: a write that errors may still have reached flash.
        self._count_write()
        try:
            await client.set_config(setting.write_point, value)
        except IndevoltError as err:
            _LOGGER.warning("[%s] Indevolt %s = %d not written: %s", self._name, setting.key, value, err)
            self._fail(setting, value)
            return
        _LOGGER.info(
            "[%s] Indevolt flash write (%s): %s %s -> %d, point %d, %d/%d this year",
            self._name, origin, setting.key, "?" if device is None else device, value,
            setting.write_point, self.writes_this_year(), YEARLY_ALLOWANCE,
        )
        self._written[setting.key] = (value, self._clock())
        self._agree(setting, value)
        self._expedite()

    # --- bookkeeping ----------------------------------------------------------------------

    def _agree(self, setting: Setting, value: int) -> None:
        """Both sides hold ``value``: remember it and store it in the battery config."""
        if self._notifier is not None:
            self._notifier.setting_ok(setting.key)
        if self._synced[setting.key] == value:
            return
        self._synced[setting.key] = value
        if self._persist is not None:
            self._persist(setting.config_key, value)
            self._persist(SYNCED_PREFIX + setting.config_key, value)

    def _restore_config(self, setting: Setting) -> None:
        """Put the agreed value back into the battery config.

        A number entity stores the requested value right after asking for it,
        also when the request was refused (writes paused) or the write failed. Left
        there, the next setup would take it for an options-flow change and
        write it after all.
        """
        synced = self._synced[setting.key]
        if synced is None or self._persist is None:
            return
        if self._stored_int(setting.config_key) not in (None, synced):
            self._persist(setting.config_key, synced)

    def _fail(self, setting: Setting, value: int) -> None:
        if self._notifier is not None:
            self._notifier.setting_failed(setting.key, value)

    # --- flash wear guard ------------------------------------------------------------------

    def _flash_writes_allowed(self) -> bool:
        """False once the yearly allowance is used up; then writing stays off."""
        self._load_guard()
        if not self._paused:
            self._prune()
            if len(self._write_times) < YEARLY_ALLOWANCE:
                return True
            self._paused = True
            self._save_guard()
            _LOGGER.warning(
                "[%s] Indevolt settings written %d times in the past year; flash writes "
                "stay off until they are turned back on in Repairs",
                self._name, YEARLY_ALLOWANCE,
            )
        else:
            _LOGGER.warning("[%s] Indevolt flash writes are off (see Repairs); not written",
                            self._name)
        if self._notifier is not None:
            self._notifier.flash_writes_paused(YEARLY_ALLOWANCE)
        return False

    def _count_write(self) -> None:
        self._write_times.append(self._wall_clock())
        self._writes_total += 1
        self._save_guard()

    def _prune(self) -> None:
        now = self._wall_clock()
        # A time ahead of the clock (the clock was set back) stays: it errs on
        # the side of fewer writes.
        self._write_times = [t for t in self._write_times if now - t < ALLOWANCE_WINDOW_S]

    def _load_guard(self) -> None:
        if self._guard_loaded:
            return
        self._guard_loaded = True
        stored = self._stored(FLASH_WRITES_KEY) if self._stored is not None else None
        if not isinstance(stored, Mapping):
            return
        times = stored.get("times")
        if isinstance(times, (list, tuple)):
            self._write_times = [
                float(t) for t in times
                if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
            ]
        total = stored.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            self._writes_total = max(total, len(self._write_times))
        else:
            self._writes_total = len(self._write_times)
        self._paused = stored.get("paused") is True
        if self._paused:
            _LOGGER.warning("[%s] Indevolt flash writes are off until turned back on in Repairs",
                            self._name)
            if self._notifier is not None:
                self._notifier.flash_writes_paused(YEARLY_ALLOWANCE)

    def _save_guard(self) -> None:
        if self._persist is None:
            return
        # A new dict every time: the config entry only saves a changed value.
        self._persist(FLASH_WRITES_KEY, {
            "times": [int(t) for t in self._write_times],
            "total": self._writes_total,
            "paused": self._paused,
        })

    @property
    def _synced(self) -> dict[str, int | None]:
        if self._synced_store is None:
            self._synced_store = {s.key: self._stored_int(SYNCED_PREFIX + s.config_key)
                                  for s in SETTINGS}
            self._synced_at_start = dict(self._synced_store)
        return self._synced_store

    def _load_synced_at_start(self, key: str) -> int | None:
        self._synced  # noqa: B018 - loads the store on first use
        return self._synced_at_start.get(key)

    def _stored_int(self, key: str) -> int | None:
        value = self._stored(key) if self._stored is not None else None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(round(number)) if math.isfinite(number) else None

    async def close(self) -> None:
        self._closed = True
        self._pending.clear()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
