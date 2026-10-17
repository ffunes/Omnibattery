"""A fake Indevolt battery and a virtual clock for the Indevolt tests.

``FakeIndevoltDevice`` is plugged into :class:`IndevoltClient` as its transport,
so every request crosses the real URL encoding and the client's guards before
the fake parses it like the device would. It models identity, the work mode,
the realtime tuple with its echo, a power ramp and SOC, and records every
request with its virtual timestamp.

``VirtualClock`` replaces ``time.monotonic`` and ``asyncio.sleep`` for the
client and scheduler. Time only moves when a test calls :meth:`VirtualClock.run`,
which wakes sleepers in deadline order, so a 20-minute scenario takes
milliseconds and every spacing is exact.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

# A real 38-point reply from a CMS-SF2000 (firmware V1.4.0E, self-consumption
# mode, charging from PV). Used as the fake's defaults.
SF2000_SAMPLE = {
    "114": 2400, "115": 2400, "142": 7.18, "606": "1002", "667": 0, "1664": 0,
    "1665": 0, "1666": 0, "1667": 0, "2101": 554, "2104": 1078.38, "2105": 0.28,
    "2107": 1451.22, "2108": 0, "2275": 580, "2278": 554, "2618": 1001, "6000": -487,
    "6001": 1001, "6002": 66, "6004": 6.13, "6005": 2.21, "6006": 1311.94,
    "6007": 1268.1, "6105": 15, "6107": 1000, "6108": 5, "6109": 0, "7101": 1,
    "9012": 41, "9030": 34, "9049": 36, "9068": 33, "11009": 2400, "11010": 2400,
    "11011": 2400, "11032": 2400, "11034": 0,
}

# The points read for parity with the official integration, from the same
# device (Realtime mode, discharging ~300 W): the
# Indevolt meter, PV, grid, remaining times and the inverter-unit battery's
# details. 11019 reports 9999 while it does not apply.
SF2000_PARITY_SAMPLE = {
    "11016": -7, "1501": 0, "1502": 0, "1505": 0, "11019": 9999, "11020": 374,
    "2600": 240.1, "2612": 49.99, "7120": 1000,
    "1600": 0.8, "1601": 0.8, "1602": 0.8, "1603": 0.9,
    "1632": 0, "1633": 0, "1634": 0, "1635": 0,
    "9000": 31, "9003": 178, "11042": 35, "9004": 6.55, "9013": 11.3, "9008": "022509270193",
}
# Per expansion battery: (soc, cycles, MOS temperature, voltage, current) points.
PACK_DETAIL_POINTS = (
    (9016, 9019, 9085, 9020, 19173), (9035, 9038, 9101, 9039, 19174),
    (9054, 9057, 9117, 9058, 19175), (9149, 9152, 9133, 9153, 19176),
    (9202, 9205, 9270, 9206, 19177),
)

# Writable points named in Indevolt's tables and the official library. Every one
# of them except 47015 is flash-backed. 1142, 1138 and 1147 are the user settings
# Omnibattery may write on the config channel, on a direct user action only.
KNOWN_FLASH_POINTS = (
    47005, 4, 6505, 1142, 11010, 1146, 1147, 2618, 1143, 2802, 8646, 8647,
    35001, 35002, 1138, 7265, 7266, 47016, 47017,
)


class VirtualClock:
    """Deterministic time for code that takes ``clock`` and ``sleep`` arguments."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.now + delay, next(self._seq), future))
        await future

    @staticmethod
    async def settle(rounds: int = 30) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def run(self, seconds: float) -> None:
        """Advance time by ``seconds``, letting every task run at each wake-up."""
        target = self.now + seconds
        await self.settle()
        while self._sleepers and self._sleepers[0][0] <= target:
            deadline, _, future = heapq.heappop(self._sleepers)
            self.now = max(self.now, deadline)
            if not future.done():
                future.set_result(None)
            await self.settle()
        self.now = target
        await self.settle()

    async def run_until(self, predicate, timeout: float, step: float = 0.5) -> bool:
        end = self.now + timeout
        while self.now < end:
            if predicate():
                return True
            await self.run(step)
        return predicate()


@dataclass
class Request:
    at: float
    method: str
    endpoint: str
    config: dict[str, Any] | None
    done_at: float | None = None  # when the reply (or the timeout) went back


@dataclass
class FakeIndevoltDevice:
    clock: VirtualClock
    device_type: str = "CMS-SF2000"
    serial: str = "3300000001"
    firmware: str = "V1.4.0E_R00D.0B2_M4801_0000003E"
    f_ver: str = "V140E.0B.003E"
    energy_mode: int = 4
    soc: float = 60.0
    capacity_kwh: float = 7.18
    native_min_soc: int = 15  # the reserve: read 6105, written via 1142
    limit_w: int = 2400  # hardware ceiling (114/115)
    charge_setting_w: int | None = None  # max AC input: read 11009, written via 1138
    discharge_setting_w: int | None = None  # max AC output: read 11011/11032, written via 1147
    feedin_w: int | None = None  # feed-in limit 11010 (never written by Omnibattery)
    # Config-channel writes are a test failure unless a test opts in here.
    allow_config_writes: bool = False
    reject_config: bool = False  # the device answers {result: false} to a setting
    config_apply_delay_s: float = 0.0  # a setting reads back only this long after the write
    _config_due: list = field(default_factory=list)
    ramp_w_per_s: float = 1000.0
    loss_w: float = 0.0
    status_family: int = 1000  # 1000-family echo (this firmware) or 0 (documented 0/1/2)
    packs: int = 3  # expansion packs; the sampled SF2000 has three
    absent_pack_style: str = "missing"  # absent packs: points "missing" or "empty" serials
    # realtime tuple as the device holds it
    action: int = 0
    rt_power: int = 0
    rt_target: int = 5
    power_w: float = 0.0  # Omnibattery sign: + charging
    action_state: int | None = None  # 0 once the device stopped at its target
    # A move commanded from standby starts (and the echo leaves standby) only
    # this long after the write; a real SF2000 takes 10–15 s.
    start_delay_s: float = 0.0
    _starting_until: float = 0.0
    # The echo (6107–6109) shows a written tuple only this long after the
    # write; a real SF2000 echoes an accepted STOP 9–19 s late.
    echo_lag_s: float = 0.0
    _tuples: list = field(default_factory=list)  # (written at, action, power, target)
    standby_events: list[float] = field(default_factory=list)
    # fault injection
    latency_s: float = 0.08
    timeouts: int = 0  # the next N requests time out
    batch_limit: int | None = None  # replies carry at most this many points
    missing_points: set[int] = field(default_factory=set)
    extra_points: dict[str, Any] = field(default_factory=dict)  # override or add point values
    requests: list[Request] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0
    _last: float | None = None

    # Every device created during a test; the autouse guard in conftest.py
    # checks each one for flash writes when the test ends.
    instances: ClassVar[list["FakeIndevoltDevice"]] = []

    def __post_init__(self) -> None:
        FakeIndevoltDevice.instances.append(self)
        self._tuples.append((float("-inf"), self.action, self.rt_power, self.rt_target))

    # --- physics ----------------------------------------------------------------

    def effective_action(self) -> int:
        """What the device does now: the written action, or 0 once it stopped."""
        if self.energy_mode != 4 or self.clock.now < self._starting_until:
            return 0
        return self.action if self.action_state is None else self.action_state

    def _target_reached(self, action: int) -> bool:
        if action == 1:
            return self.soc >= self.rt_target
        if action == 2:
            return self.soc <= max(self.rt_target, self.native_min_soc)
        return False

    def _advance(self) -> None:
        now = self.clock.now
        if self._last is None:
            self._last = now
        dt, self._last = now - self._last, now
        # Integrate in one-second steps so a target reached mid-interval stops.
        while True:
            action = self.effective_action()
            if self._target_reached(action):
                # The device drops to standby at its target; the tuple stays.
                self.action_state = action = 0
                self.standby_events.append(now)
            commanded = (
                float(self.rt_power) if action == 1
                else -float(self.rt_power) if action == 2
                else 0.0
            )
            if dt <= 0:
                break
            step = min(1.0, dt)
            dt -= step
            delta = commanded - self.power_w
            limit = self.ramp_w_per_s * step
            self.power_w += max(-limit, min(limit, delta))
            self.soc += self.power_w * step / 3600 / (self.capacity_kwh * 1000) * 100
            self.soc = max(0.0, min(100.0, self.soc))

    def set_mode(self, mode: int) -> None:
        """The user switches the working mode in the Indevolt app."""
        self._advance()
        self.energy_mode = mode
        if mode != 4:
            self.action, self.rt_power, self.action_state = 0, 0, None

    def jump_soc(self, soc: float) -> None:
        """A BMS recalibration moves SOC at once."""
        self._advance()
        self.soc = soc
        self._advance()

    @property
    def status(self) -> int:
        return self.status_family + self.effective_action()

    def echo(self) -> tuple[int, int, int]:
        """(6107, 6108, 6109) as read now: the tuple written echo_lag_s ago."""
        shown = [t for t in self._tuples if t[0] <= self.clock.now - self.echo_lag_s]
        if shown[-1] is self._tuples[-1]:
            return self.status, self.rt_target, self.rt_power
        _, action, power, target = shown[-1]
        return self.status_family + action, target, power

    def _apply_setting(self, point: int, value: int) -> None:
        if point == 1142:
            self.native_min_soc = value
        elif point == 1138:
            self.charge_setting_w = value
        else:
            self.discharge_setting_w = value

    def values(self) -> dict[str, Any]:
        self._advance()
        for due in [d for d in self._config_due if d[0] <= self.clock.now]:
            self._config_due.remove(due)
            self._apply_setting(due[1], due[2])
        data = {**SF2000_SAMPLE, **SF2000_PARITY_SAMPLE}
        battery_native = -round(self.power_w)  # + discharging
        ac = round(self.power_w + (self.loss_w if self.power_w > 0 else -self.loss_w if self.power_w < 0 else 0))
        data.update({
            "6000": battery_native,
            "6001": 1000 + (1 if self.power_w > 20 else 2 if self.power_w < -20 else 0),
            "6002": int(round(self.soc)),
            "2278": ac, "2275": ac, "667": 0,
            "2101": max(ac, 0), "2108": max(-ac, 0),
            "7101": self.energy_mode,
            "6105": self.native_min_soc,
            **dict(zip(("6107", "6108", "6109"), self.echo())),
            "11009": self._setting(self.charge_setting_w),
            "11011": self._setting(self.discharge_setting_w),
            "11032": self._setting(self.discharge_setting_w),
            "11010": self._setting(self.feedin_w),
            "114": self.limit_w, "115": self.limit_w,
            "142": self.capacity_kwh,
            "11005": 45,
        })
        pack_points = ((9032, 9030), (9051, 9049), (9070, 9068), (9165, 9163), (9218, 9216))
        for n, (serial, temp) in enumerate(pack_points, start=1):
            details = PACK_DETAIL_POINTS[n - 1]
            if n <= self.packs:
                data[str(serial)] = f"PACK{n}SN"
                data[str(temp)] = 30 + n
                data.update(zip(map(str, details), (30 + n, 170 + n, 30 + n, 6.5, 11.0 + n)))
            elif self.absent_pack_style == "empty":
                data[str(serial)] = ""
                data[str(temp)] = 0
                data.update((str(point), 0) for point in details)
            else:
                for point in (serial, temp, *details):
                    data.pop(str(point), None)
        data.update(self.extra_points)
        return data

    def _setting(self, value: int | None) -> int:
        return self.limit_w if value is None else value

    # --- transport -------------------------------------------------------------------

    @property
    def set_data_requests(self) -> list[Request]:
        return [r for r in self.requests if r.endpoint == "Indevolt.SetData"]

    @property
    def config_writes(self) -> list[tuple[int, list[int]]]:
        return [w for w in self.writes if w[0] != 47015]

    @property
    def writes(self) -> list[tuple[int, list[int]]]:
        return [(r.config["t"], r.config["v"]) for r in self.set_data_requests if r.config]

    def request_times(self) -> list[float]:
        return [r.at for r in self.requests]

    async def __call__(self, method: str, url: str) -> tuple[int, bytes]:
        parts = urlsplit(url)
        endpoint = parts.path.rsplit("/", 1)[-1]
        query = parse_qs(parts.query)
        config = json.loads(unquote(query["config"][0])) if "config" in query else None
        request = Request(self.clock.now, method, endpoint, config)
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            return await self._handle(method, endpoint, config)
        finally:
            self.in_flight -= 1
            request.done_at = self.clock.now

    async def _handle(self, method: str, endpoint: str, config: Any) -> tuple[int, bytes]:
        if self.timeouts > 0:
            self.timeouts -= 1
            await self.clock.sleep(5.0)
            raise TimeoutError
        await self.clock.sleep(self.latency_s)
        if endpoint == "Sys.GetConfig" and method == "GET":
            body = {"device": {"type": self.device_type, "sn": self.serial,
                               "fw": self.firmware, "f_ver": self.f_ver}}
        elif endpoint == "Indevolt.GetData" and method == "POST":
            values = self.values()
            points = [p for p in config["t"] if p not in self.missing_points]
            if self.batch_limit is not None and len(config["t"]) > self.batch_limit:
                points = points[: self.batch_limit]
            body = {str(p): values[str(p)] for p in points if str(p) in values}
        elif endpoint == "Indevolt.SetData" and method == "POST":
            self._advance()
            if config.get("t") == 47015:
                action, power, target = config["v"]
                if action != 0 and self.effective_action() == 0:
                    self._starting_until = self.clock.now + self.start_delay_s
                self.action, self.rt_power, self.rt_target = action, power, target
                self._tuples.append((self.clock.now, action, power, target))
                self.action_state = None
                self._advance()
            elif config.get("t") in (1142, 1138, 1147) and not self.reject_config:
                if self.config_apply_delay_s > 0:
                    self._config_due.append(
                        (self.clock.now + self.config_apply_delay_s, config["t"], config["v"][0]))
                else:
                    self._apply_setting(config["t"], config["v"][0])
            # Any other point would be written to flash on a real device. The
            # fake accepts it so the guard tests can prove it never gets here.
            body = {"result": not (self.reject_config and config.get("t") != 47015)}
        else:
            return 404, b""
        return 200, json.dumps(body).encode()


FLASH_WEAR_MESSAGE = (
    "FLASH WEAR: control and every other periodic or automatic path may only ever "
    "write the Indevolt realtime RAM tuple 47015. Indevolt R&D confirmed that every "
    "other writable point (work mode 47005/4, backup reserve, feed-in and output "
    "limits, grid charging, ...) is stored in flash, which wears out with every "
    "write. Only the user settings 1142/1138/1147 may be written, and only on a "
    "direct user action (a number entity or the options flow). A failure here "
    "means a code change opened a path to a flash write. DO NOT relax this test "
    "without written confirmation from Indevolt."
)

CONFIG_SETTING_POINTS = frozenset({1142, 1138, 1147})


def flash_writes(device: FakeIndevoltDevice) -> list[Request]:
    """SetData requests that reached the device for anything but 47015."""
    return [
        r for r in device.set_data_requests
        if not (isinstance(r.config, dict) and r.config.get("t") == 47015)
    ]


def unexpected_flash_writes(device: FakeIndevoltDevice) -> list[Request]:
    """Flash writes the test did not allow: every one, or any non-setting point."""
    return [
        r for r in flash_writes(device)
        if not (device.allow_config_writes and isinstance(r.config, dict)
                and r.config.get("t") in CONFIG_SETTING_POINTS)
    ]


class RecordingNotifier:
    """Stands in for IndevoltNotifier and records what the driver raised."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.suggest_slower: list[bool] = []

    def paused(self, reason: str, *, model: str | None = None) -> None:
        self.events.append(("paused", reason))

    def resumed(self) -> None:
        self.events.append(("resumed",))

    def firmware_outdated(self, firmware: str, minimum: str) -> None:
        self.events.append(("firmware", firmware, minimum))

    def setting_failed(self, key: str, value: int) -> None:
        self.events.append(("setting_failed", key, value))

    def flash_writes_paused(self, allowance: int) -> None:
        self.events.append(("flash_paused", allowance))

    def flash_writes_resumed(self) -> None:
        self.events.append(("flash_resumed",))

    def setting_ok(self, key: str) -> None:
        self.events.append(("setting_ok", key))

    def throttled(self, interval_s: float, *, suggest_slower: bool = False) -> None:
        self.events.append(("throttled", interval_s))
        self.suggest_slower.append(suggest_slower)

    def throttle_cleared(self) -> None:
        self.events.append(("throttle_cleared",))

    def count(self, *event) -> int:
        return sum(1 for e in self.events if e[: len(event)] == event)


async def connected_driver(clock: VirtualClock, device: FakeIndevoltDevice, *, own: bool = True,
                           host: str = "192.0.2.1", **kwargs):
    """Build an IndevoltDriver on ``device``, connect it and (optionally) wait until OWNED."""
    from custom_components.omnibattery.drivers.indevolt import ControlState, IndevoltDriver

    kwargs.setdefault("notifier", RecordingNotifier())
    driver = IndevoltDriver(host, transport=device, clock=clock, sleep=clock.sleep, **kwargs)
    task = asyncio.ensure_future(driver.connect())
    await clock.run_until(task.done, 30)
    assert task.result() is True
    if own:
        assert await clock.run_until(lambda: driver.control_state is ControlState.OWNED, 60)
    return driver
