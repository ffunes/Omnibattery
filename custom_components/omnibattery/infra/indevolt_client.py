"""Flash-guarded, paced transport and telemetry scheduler for the Indevolt local API.

Two layers live here:

* :class:`IndevoltClient` sends one RPC at a time to one device. Every request,
  whoever makes it (driver, config-flow probe, a reloaded entry), passes the same
  per-device gate: at most one request in flight, and request starts at least one
  slot apart, measured start to start and never below :data:`MIN_SLOT_S`. The
  flash guard lives here too (see the FLASH WEAR block).
* :class:`IndevoltScheduler` owns all routine traffic for one driver. Per slot it
  sends the pending realtime write or, if there is none, one batched telemetry
  read: the control tier plus whichever slower tier is most overdue. Replies land
  in a timestamped cache that the driver serves to the coordinator, so however
  often Omnibattery polls, the device sees one request per slot.

Pacing follows the vendor's guidance (≥ 5 s between requests) and
measurements on a CMS-SF2000 (batches of up to 38 points and spacing down to 2 s
were clean). Batched reads contradict one vendor reply, so an incomplete batch
reply makes the scheduler fall back to smaller batches on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from contextvars import ContextVar
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import quote
from weakref import WeakKeyDictionary

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 8080

# Request pacing. SLOT_S is the profile constant (5 s first; a 3 s experiment
# follows the acceptance test). MIN_SLOT_S is a hard floor that no constructor
# argument, backoff state or caller can undercut.
SLOT_S = 5.0
MIN_SLOT_S = 2.0
REQUEST_TIMEOUT_S = 5.0
# Adaptive backoff: the interval doubles after a timeout, an error or a reply
# slower than LATENCY_SPIKE_S, up to MAX_BACKOFF_FACTOR, and halves again after
# HEALTHY_STREAK good requests in a row.
LATENCY_SPIKE_S = 2.5
MAX_BACKOFF_FACTOR = 4
HEALTHY_STREAK = 10
# A pending write takes the next slot, but never more than this many slots in a
# row, so a core that changes its setpoint every slot cannot starve the reads.
MAX_CONSECUTIVE_WRITES = 2
# Batch fallback: an incomplete reply halves the batch size; this many complete
# replies in a row double it again.
MAX_POINTS = 50
BATCH_RECOVER_AFTER = 30
UNSUPPORTED_RETRY_S = 3600.0
# A single-point verify read takes a whole slot, so at most one per this long.
VERIFY_SPACING_S = 30.0
MAX_RESPONSE_BYTES = 65536

# ─────────────────────────────────────────────────────────────────────────────
# ⚠ FLASH WEAR: read this before changing anything below.
#
# Indevolt R&D confirmed: only the realtime tuple 47015 (action, power, target
# SOC; Modbus 1150-1152) lives in RAM. EVERY other writable point is stored in
# flash/EEPROM, including the work mode (47005 / 4), the backup reserve
# (6505 / 1142), the feed-in and AC output limits (11010 / 1146, 1147) and grid
# charging (2618 / 1143). Flash survives a limited number of write cycles; a
# control loop that writes one of those points every few seconds wears out the
# battery's controller.
#
# Omnibattery therefore writes 47015 and nothing else, ever:
#   1. ``set_realtime`` and ``stop`` are the only write methods. There is no
#      generic set_data.
#   2. ``_submit`` re-checks every request just before it is sent: the endpoint
#      must be in ALLOWED_ENDPOINTS, and a SetData must target RAM_WRITE_POINTS.
#   3. tests/test_indevolt_no_flash_writes.py fails if either set changes.
#
# DO NOT add a point to RAM_WRITE_POINTS or an endpoint to ALLOWED_ENDPOINTS
# without written confirmation from Indevolt that the point is RAM-backed.
# ─────────────────────────────────────────────────────────────────────────────
RAM_WRITE_POINTS = frozenset({47015})
ALLOWED_ENDPOINTS = MappingProxyType(
    {
        "Sys.GetConfig": "GET",
        "Indevolt.GetData": "POST",
        "Indevolt.SetData": "POST",
    }
)
REALTIME_POINT = 47015
ACTION_STOP = 0
ACTION_CHARGE = 1
ACTION_DISCHARGE = 2
STOP_TUPLE = (ACTION_STOP, 0, 0)
# Documented range of the realtime power value (one unit up to 3600 W, up to
# 10800 W in parallel) and the API's hard minimum target SOC.
MAX_REALTIME_POWER_W = 10800
MIN_TARGET_SOC = 5


class IndevoltError(Exception):
    """A redacted transport failure. ``may_have_applied`` is honest for writes."""

    def __init__(self, message: str, *, may_have_applied: bool = False) -> None:
        super().__init__(message)
        self.may_have_applied = may_have_applied


class IndevoltAuthenticationError(IndevoltError):
    """The device asks for authentication: its local API is set to HTTP Digest.

    Omnibattery uses the plain HTTP setting of the local API.
    """


class IndevoltProtocolError(IndevoltError):
    """A reply did not match the documented RPC protocol."""


class IndevoltConnectionError(IndevoltError):
    """Network failure or timeout; a write may still have been executed."""


class IndevoltClosedError(IndevoltError):
    """The client or scheduler was closed."""


class IndevoltFlashWriteForbidden(RuntimeError):
    """A request would have written a flash-backed point. This is a bug.

    Deliberately not an :class:`IndevoltError`, so no transport error handler
    can swallow it.
    """


class IndevoltForbiddenEndpoint(RuntimeError):
    """A request named an endpoint outside ALLOWED_ENDPOINTS. This is a bug."""


# The wire: (method, url) -> (HTTP status, body bytes). The default implementation
# uses aiohttp; tests substitute a fake device with the same signature.
Transport = Callable[[str, str], Awaitable[tuple[int, bytes]]]


class _Gate:
    """Per-device pacing state shared by every client of that device."""

    __slots__ = ("lock", "last_start")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_start: float | None = None


# Keyed by event loop, then "host:port", so a reloaded entry or a config-flow
# probe shares the running driver's budget and never overlaps its request.
_GATES: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Gate]] = (
    WeakKeyDictionary()
)


# The gate the current task holds, so a request made inside ``exchange`` does
# not wait for its own slot. Per task, so another task can never ride along.
_HELD_GATE: ContextVar[_Gate | None] = ContextVar("indevolt_held_gate", default=None)


def _gate_for(endpoint: str) -> _Gate:
    gates = _GATES.setdefault(asyncio.get_running_loop(), {})
    return gates.setdefault(endpoint, _Gate())


def validate_host(host: str) -> str:
    """Accept a hostname or IP address, rejecting anything shaped like a URL."""
    if not isinstance(host, str):
        raise ValueError("Invalid Indevolt host")
    host = host.strip()
    if not host or any(c.isspace() for c in host) or any(c in host for c in "/\\@?#%"):
        raise ValueError("Invalid Indevolt host")
    return host


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON value")


def _parse_json(content: bytes) -> dict[str, Any]:
    try:
        data = json.loads(content, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise IndevoltProtocolError("Indevolt reply is not valid JSON") from None
    if not isinstance(data, dict):
        raise IndevoltProtocolError("Indevolt reply is not a JSON object")
    return data


class IndevoltClient:
    """One device, one request at a time, every request paced and flash-guarded."""

    def __init__(
        self,
        host: str,
        *,
        port: int = DEFAULT_PORT,
        slot_s: float = SLOT_S,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._host = validate_host(host)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid Indevolt port")
        self._port = port
        self._base = f"http://{self._host}:{port}/rpc/"
        self._endpoint_key = f"{self._host.lower()}:{port}"
        if not isinstance(slot_s, (int, float)) or not math.isfinite(slot_s):
            slot_s = SLOT_S
        self._slot_s = max(MIN_SLOT_S, float(slot_s))
        self._transport = transport
        self._session: aiohttp.ClientSession | None = None
        self._clock = clock
        self._sleep = sleep
        self._closed = False
        self.last_latency_s: float | None = None

    @property
    def slot_s(self) -> float:
        """Configured base spacing, already raised to the hard floor."""
        return self._slot_s

    @property
    def closed(self) -> bool:
        return self._closed

    # --- public requests ---------------------------------------------------

    async def get_config(self) -> dict[str, Any]:
        """Read the device identity (``Sys.GetConfig``)."""
        data = await self._submit("Sys.GetConfig", None)
        if not isinstance(data.get("device"), dict):
            raise IndevoltProtocolError("Indevolt configuration lacks device information")
        return data

    async def get_data(self, points: Sequence[int]) -> dict[str, Any]:
        """Read up to MAX_POINTS points. A reply may be partial; callers check."""
        return await self._submit("Indevolt.GetData", {"t": self._read_points(points)})

    async def set_realtime(self, action: int, power_w: int, target_soc: int) -> bool:
        """Write the realtime RAM tuple. True means the device accepted it.

        This and :meth:`stop` are the only write methods (see FLASH WEAR).
        """
        return await self._write_realtime(self.realtime_config(action, power_w, target_soc))

    async def stop(self) -> bool:
        """Write STOP ``[0, 0, 0]`` to the realtime tuple."""
        return await self.set_realtime(*STOP_TUPLE)

    async def exchange(
        self,
        pick: Callable[[], tuple[str, dict[str, Any] | None] | None],
        *,
        interval_s: float | None = None,
    ) -> tuple[tuple[str, dict[str, Any] | None], dict[str, Any]] | None:
        """Wait for the next slot, then send whatever ``pick`` chooses at that moment.

        Choosing inside the slot lets a write that arrived while waiting take
        this slot. ``interval_s`` lets the scheduler stretch the spacing
        (backoff); it can never shorten it below the configured slot.
        """
        async with self._slot(interval_s):
            request = pick()
            if request is None:
                return None
            endpoint, config = request
            return request, await self._submit(endpoint, config)

    async def close(self) -> None:
        self._closed = True
        if self._session is not None:
            await self._session.close()
            self._session = None

    # --- request building --------------------------------------------------

    @staticmethod
    def realtime_config(action: int, power_w: int, target_soc: int) -> dict[str, Any]:
        """Validate and build the SetData payload for the realtime tuple."""
        values = (action, power_w, target_soc)
        if any(type(v) is not int for v in values):
            raise ValueError("Indevolt realtime values must be integers")
        if values == STOP_TUPLE:
            return {"f": 16, "t": REALTIME_POINT, "v": list(STOP_TUPLE)}
        if (
            action not in (ACTION_CHARGE, ACTION_DISCHARGE)
            or not 0 <= power_w <= MAX_REALTIME_POWER_W
            or not MIN_TARGET_SOC <= target_soc <= 100
        ):
            raise ValueError("Unsupported Indevolt realtime command")
        return {"f": 16, "t": REALTIME_POINT, "v": list(values)}

    @staticmethod
    def _read_points(points: Sequence[int]) -> list[int]:
        if isinstance(points, (str, bytes)) or not 1 <= len(points) <= MAX_POINTS:
            raise ValueError("Indevolt reads take one to fifty points")
        result = []
        for point in points:
            if type(point) is not int or not 0 <= point <= 65535:
                raise ValueError("Invalid Indevolt read point")
            result.append(point)
        if len(set(result)) != len(result):
            raise ValueError("Duplicate Indevolt read point")
        return result

    async def _write_realtime(self, config: dict[str, Any]) -> bool:
        data = await self._submit("Indevolt.SetData", config)
        if data.get("result") is not True:
            raise IndevoltProtocolError(
                "Indevolt did not accept the realtime command", may_have_applied=True
            )
        return True

    # --- pacing --------------------------------------------------------------

    def _slot(self, interval_s: float | None = None) -> "_SlotContext":
        return _SlotContext(self, interval_s)

    def _spacing(self, interval_s: float | None) -> float:
        spacing = self._slot_s
        if interval_s is not None and math.isfinite(interval_s):
            spacing = max(spacing, float(interval_s))
        return max(MIN_SLOT_S, spacing)

    # --- the wire ------------------------------------------------------------

    async def _submit(self, endpoint: str, config: dict[str, Any] | None) -> dict[str, Any]:
        """The last stop before the network. Independent of every caller's checks."""
        # Flash guard, second line: re-validate the request itself, whatever
        # built it. See the FLASH WEAR block at the top of this module.
        if endpoint not in ALLOWED_ENDPOINTS:
            raise IndevoltForbiddenEndpoint(
                f"Refusing Indevolt endpoint {endpoint!r}: only "
                f"{sorted(ALLOWED_ENDPOINTS)} are allowed"
            )
        if endpoint == "Indevolt.SetData":
            point = config.get("t") if isinstance(config, dict) else None
            values = config.get("v") if isinstance(config, dict) else None
            if type(point) is not int or point not in RAM_WRITE_POINTS:
                raise IndevoltFlashWriteForbidden(
                    f"Refusing to write Indevolt point {point!r}: only "
                    f"{sorted(RAM_WRITE_POINTS)} is RAM-backed; every other point "
                    "is flash and wears out with each write"
                )
            if (
                set(config) != {"f", "t", "v"}
                or config["f"] != 16
                or not isinstance(values, list)
                or len(values) != 3
                or any(type(v) is not int for v in values)
            ):
                raise IndevoltFlashWriteForbidden(
                    "Refusing a malformed Indevolt SetData request"
                )
        elif endpoint == "Sys.GetConfig":
            if config is not None:
                raise IndevoltForbiddenEndpoint("Sys.GetConfig takes no parameters")
        elif not isinstance(config, dict) or set(config) != {"t"}:
            raise IndevoltForbiddenEndpoint("Indevolt.GetData takes only a point list")
        if self._closed:
            raise IndevoltClosedError("Indevolt client closed")
        if _HELD_GATE.get() is not _gate_for(self._endpoint_key):
            async with self._slot():
                return await self._send(endpoint, config)
        return await self._send(endpoint, config)

    async def _send(self, endpoint: str, config: dict[str, Any] | None) -> dict[str, Any]:
        write = endpoint == "Indevolt.SetData"
        url = self._base + endpoint
        if config is not None:
            url += "?config=" + quote(json.dumps(config, separators=(",", ":")), safe="")
        started = self._clock()
        try:
            status, content = await asyncio.wait_for(
                (self._transport or self._aiohttp_send)(ALLOWED_ENDPOINTS[endpoint], url),
                REQUEST_TIMEOUT_S,
            )
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise IndevoltConnectionError(
                f"Indevolt {endpoint} failed: {type(err).__name__}", may_have_applied=write
            ) from None
        finally:
            self.last_latency_s = self._clock() - started
        if status in (401, 403):
            raise IndevoltAuthenticationError(
                "Indevolt asks for authentication; set the local API to HTTP, not HTTP Digest"
            )
        if status != 200:
            raise IndevoltProtocolError(
                f"Indevolt {endpoint} returned HTTP {status}", may_have_applied=write
            )
        if len(content) > MAX_RESPONSE_BYTES:
            raise IndevoltProtocolError("Indevolt reply too large", may_have_applied=write)
        try:
            return _parse_json(content)
        except IndevoltProtocolError as err:
            err.may_have_applied = write
            raise

    async def _aiohttp_send(self, method: str, url: str) -> tuple[int, bytes]:
        if self._session is None or self._session.closed:
            # A fresh connection per request: the embedded server handles
            # keep-alive poorly, and the official library does the same.
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True, limit=1),
                trust_env=False,
            )
        async with self._session.request(
            method,
            # The URL is already percent-encoded; don't let yarl re-quote it.
            URL(url, encoded=True),
            headers={"Connection": "close", "Content-Type": "application/json"},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S, connect=3),
        ) as response:
            content = await response.content.read(MAX_RESPONSE_BYTES + 1)
            return response.status, content


class _SlotContext:
    """Hold the device gate and wait until the next request may start."""

    def __init__(self, client: IndevoltClient, interval_s: float | None) -> None:
        self._client = client
        self._interval_s = interval_s
        self._gate: _Gate | None = None
        self._token: Any = None

    async def __aenter__(self) -> None:
        client = self._client
        if client._closed:
            raise IndevoltClosedError("Indevolt client closed")
        self._gate = _gate_for(client._endpoint_key)
        await self._gate.lock.acquire()
        try:
            spacing = client._spacing(self._interval_s)
            while self._gate.last_start is not None:
                remaining = self._gate.last_start + spacing - client._clock()
                if remaining <= 0:
                    break
                await client._sleep(remaining)
            if client._closed:
                raise IndevoltClosedError("Indevolt client closed")
            # Start-to-start spacing: stamp the start before the request runs.
            self._gate.last_start = client._clock()
            self._token = _HELD_GATE.set(self._gate)
        except BaseException:
            self._gate.lock.release()
            raise

    async def __aexit__(self, *exc: object) -> None:
        _HELD_GATE.reset(self._token)
        assert self._gate is not None
        self._gate.lock.release()


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tier:
    """A set of points read together, at most once per ``interval_s``.

    The first tier is the control tier: it is read in every read slot. Every
    other tier rides along in the same request when it is due.
    """

    name: str
    points: tuple[int, ...]
    interval_s: float = 0.0


WRITE_ACCEPTED = "accepted"
WRITE_FAILED = "failed"


class IndevoltScheduler:
    """Owns all routine requests to one device: one per slot, writes first."""

    def __init__(
        self,
        client: IndevoltClient,
        tiers: Sequence[Tier],
        *,
        clock: Callable[[], float] = time.monotonic,
        on_telemetry: Callable[[frozenset[int]], None] | None = None,
        on_write: Callable[[tuple[int, int, int], str], None] | None = None,
        name: str = "Indevolt",
    ) -> None:
        if not tiers:
            raise ValueError("The scheduler needs at least the control tier")
        self._client = client
        self._tiers = list(tiers)
        self._tier_intervals = {tier.name: tier.interval_s for tier in tiers}
        self._clock = clock
        self._on_telemetry = on_telemetry
        self._on_write = on_write
        self._name = name
        self._cache: dict[int, tuple[Any, float]] = {}
        self._tier_done: dict[str, float] = {}
        self._tier_cursor: dict[str, int] = {}
        self._control_cursor = 0
        self._unsupported: dict[int, float] = {}
        # Ride-along points missing from a reply whose control points all came
        # back: read each alone once to tell "not supported" from "truncated".
        self._verify: list[int] = []
        self._verify_batch = MAX_POINTS
        self._last_verify: float | None = None
        self._pending: tuple[int, int, int] | None = None
        self._pending_force = False
        self._last_accepted: tuple[int, int, int] | None = None
        self._write_waiters: list[tuple[tuple[int, int, int], asyncio.Future[bool]]] = []
        self._consecutive_writes = 0
        self._max_batch = MAX_POINTS
        self._complete_streak = 0
        self._factor = 1
        self._healthy_streak = 0
        self._consecutive_failures = 0
        self._throttled_since: float | None = None
        self._auth_warned = False
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._first_read = asyncio.Event()
        self._current: tuple[str, Any] | None = None
        self.requests = 0
        self.timeouts = 0
        self.errors = 0
        self.writes = 0
        self.latencies_ms: deque[float] = deque(maxlen=200)

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._run(), name=f"{self._name} scheduler")

    async def wait_first_read(self, timeout: float) -> bool:
        """Wait until one control read has completed (used by connect)."""
        try:
            await asyncio.wait_for(self._first_read.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def close(self) -> None:
        self._closed = True
        self._fail_waiters(IndevoltClosedError("Indevolt scheduler closed"))
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # --- writes --------------------------------------------------------------

    def queue_write(self, values: tuple[int, int, int], *, force: bool = False) -> bool:
        """Queue the realtime tuple for the next slot. The newest tuple wins.

        Returns False when ``values`` is identical to the last tuple the device
        accepted; any different pending tuple is then dropped as well, because
        the device already holds what is wanted. ``force`` bypasses that check
        (used for the once-per-episode re-send).
        """
        IndevoltClient.realtime_config(*values)  # validate now, not in the slot
        if not force and values == self._last_accepted:
            self._pending = None
            self._pending_force = False
            return False
        self._pending = values
        self._pending_force = force
        return True

    def cancel_write(self) -> None:
        self._pending = None
        self._pending_force = False

    def forget_last_accepted(self) -> None:
        """The device state is unknown again (reconnect, mode change)."""
        self._last_accepted = None

    async def write_now(self, values: tuple[int, int, int], timeout: float) -> bool:
        """Queue ``values`` with force and wait until the device accepted it."""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._write_waiters.append((values, future))
        self.queue_write(values, force=True)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except (TimeoutError, IndevoltError):
            return False
        finally:
            self._write_waiters = [w for w in self._write_waiters if w[1] is not future]

    @property
    def pending_write(self) -> tuple[int, int, int] | None:
        return self._pending

    @property
    def last_accepted(self) -> tuple[int, int, int] | None:
        return self._last_accepted

    # --- telemetry cache -------------------------------------------------------

    def value(self, point: int, max_age_s: float | None = None) -> Any:
        """Cached value of ``point``, or None if missing or older than ``max_age_s``."""
        entry = self._cache.get(point)
        if entry is None:
            return None
        if max_age_s is not None and self._clock() - entry[1] > max_age_s:
            return None
        return entry[0]

    def age(self, point: int) -> float | None:
        entry = self._cache.get(point)
        return None if entry is None else self._clock() - entry[1]

    def set_tier_interval(self, name: str, interval_s: float) -> None:
        self._tier_intervals[name] = interval_s

    def expedite(self, name: str) -> None:
        """Make a tier due in the next read slot."""
        self._tier_done.pop(name, None)

    # --- pacing state ----------------------------------------------------------

    @property
    def interval_s(self) -> float:
        """Current spacing including backoff; never below the hard floor."""
        return max(MIN_SLOT_S, self._client.slot_s * self._factor)

    @property
    def control_max_age_s(self) -> float:
        """How old control telemetry may be before control must stop.

        Two worst-case control cycles (writes may take MAX_CONSECUTIVE_WRITES
        slots in a row, and a reduced batch size splits the control tier over
        several slots) plus one request timeout. Derived from the scheduler, so
        backoff never pauses control by itself.
        """
        control = len(self._tiers[0].points)
        chunks = math.ceil(control / self._max_batch)
        if control >= self._max_batch:
            chunks += 1  # the slot a due tier gets after each full pass
        cycle = self.interval_s * chunks * (MAX_CONSECUTIVE_WRITES + 1)
        return 2 * cycle + REQUEST_TIMEOUT_S

    @property
    def backoff_factor(self) -> int:
        return self._factor

    @property
    def throttled_for_s(self) -> float:
        if self._throttled_since is None:
            return 0.0
        return self._clock() - self._throttled_since

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def max_batch(self) -> int:
        return self._max_batch

    def stats(self) -> dict[str, Any]:
        latencies = sorted(self.latencies_ms)
        p95 = latencies[int(0.95 * (len(latencies) - 1))] if latencies else None
        return {
            "interval_s": self.interval_s,
            "backoff_factor": self._factor,
            "max_batch": self._max_batch,
            "requests": self.requests,
            "writes": self.writes,
            "timeouts": self.timeouts,
            "errors": self.errors,
            "latency_median_ms": latencies[len(latencies) // 2] if latencies else None,
            "latency_p95_ms": p95,
            "unsupported_points": sorted(self._unsupported),
        }

    # --- the loop --------------------------------------------------------------

    async def _run(self) -> None:
        while not self._closed:
            try:
                result = await self._client.exchange(self._pick, interval_s=self.interval_s)
            except asyncio.CancelledError:
                raise
            except IndevoltClosedError:
                return
            except IndevoltError as err:
                self._after_failure(err)
                continue
            except (IndevoltFlashWriteForbidden, IndevoltForbiddenEndpoint):
                # A guard tripped: a bug, never a device condition. Drop the
                # write so the loop cannot retry it, and surface the error.
                self._pending = None
                _LOGGER.exception("[%s] Indevolt request refused by the flash guard", self._name)
                continue
            except Exception:  # noqa: BLE001 - the loop must survive a decoding bug
                _LOGGER.exception("[%s] Indevolt scheduler error", self._name)
                self._after_failure(None)
                continue
            if result is not None:
                self._after_success(*result)

    def _pick(self) -> tuple[str, dict[str, Any] | None] | None:
        """Choose the request for this slot (called inside the slot)."""
        self._current = None
        if self._pending is not None and self._consecutive_writes < MAX_CONSECUTIVE_WRITES:
            values = self._pending
            self._pending = None
            self._pending_force = False
            self._consecutive_writes += 1
            self._current = ("write", values)
            return "Indevolt.SetData", IndevoltClient.realtime_config(*values)
        self._consecutive_writes = 0
        now = self._clock()
        if self._verify and (
            self._last_verify is None or now - self._last_verify >= VERIFY_SPACING_S
        ):
            self._last_verify = now
            points = [self._verify.pop(0)]
            self._current = ("verify", points)
        else:
            points = self._next_read()
            self._current = ("read", points)
        return "Indevolt.GetData", {"t": points}

    def _next_read(self) -> list[int]:
        now = self._clock()
        for point, since in list(self._unsupported.items()):
            if now - since >= UNSUPPORTED_RETRY_S:
                del self._unsupported[point]
        control = list(self._tiers[0].points)
        if len(control) < self._max_batch:
            return control + self._take_due(self._max_batch - len(control), control)
        # No room to ride along: walk the control tier in chunks, one per
        # slot, and give one slot to a due tier after each full pass.
        if self._control_cursor >= len(control):
            self._control_cursor = 0
            extra = self._take_due(self._max_batch, [])
            if extra:
                return extra
        start = self._control_cursor
        chunk = control[start:start + self._max_batch]
        self._control_cursor = start + len(chunk)
        return chunk

    def _take_due(self, room: int, already: list[int]) -> list[int]:
        """Fill ``room`` with due tiers, most overdue first, each round-robin."""
        now = self._clock()
        due = []
        for index, tier in enumerate(self._tiers[1:], start=1):
            interval = self._tier_intervals.get(tier.name, tier.interval_s)
            last = self._tier_done.get(tier.name)
            if last is None:
                due.append((math.inf, -index, tier))
            elif now - last >= interval:
                due.append(((now - last) / max(interval, 1e-9), -index, tier))
        taken: list[int] = []
        for _, _, tier in sorted(due, key=lambda item: (item[0], item[1]), reverse=True):
            space = room - len(taken)
            if space <= 0:
                break
            available = [
                p for p in tier.points
                if p not in self._unsupported and p not in already and p not in taken
            ]
            if not available:
                self._tier_done[tier.name] = now
                continue
            start = self._tier_cursor.get(tier.name, 0) % len(available)
            take = available[start:start + space]
            taken.extend(take)
            end = start + len(take)
            if end >= len(available):
                self._tier_cursor[tier.name] = 0
                self._tier_done[tier.name] = now
            else:
                self._tier_cursor[tier.name] = end
        return taken

    def _after_success(self, request: tuple[str, Any], data: dict[str, Any]) -> None:
        current = self._current
        self.requests += 1
        latency = self._client.last_latency_s or 0.0
        self.latencies_ms.append(round(latency * 1000, 1))
        self._record_health(latency <= LATENCY_SPIKE_S)
        if current is None:
            return
        kind, payload = current
        if kind == "write":
            self.writes += 1
            self._last_accepted = payload
            _LOGGER.debug(
                "[%s] Indevolt realtime %s accepted (%.0f ms)", self._name, payload, latency * 1000
            )
            self._resolve_waiters(payload, True)
            self._notify(self._on_write, payload, WRITE_ACCEPTED)
            return
        points: list[int] = payload
        now = self._clock()
        updated = set()
        for point in points:
            key = str(point)
            if key in data:
                self._cache[point] = (data[key], now)
                updated.add(point)
        missing = [p for p in points if p not in updated]
        _LOGGER.debug(
            "[%s] Indevolt read %d points in %.0f ms%s",
            self._name, len(points), latency * 1000,
            f", missing {missing}" if missing else "",
        )
        control = self._tiers[0].points
        if kind == "verify":
            if missing:
                self._mark_unsupported(points[0])
            else:
                # Present when asked alone: the batch was cut short.
                self._shrink(self._verify_batch, [points[0]])
        elif missing and len(points) > 1 and not any(p in control for p in missing):
            self._verify.extend(p for p in missing if p not in self._verify)
            self._verify_batch = len(points)
        elif missing:
            self._after_incomplete(points, missing)
        else:
            self._complete_streak += 1
            if self._max_batch < MAX_POINTS and self._complete_streak >= BATCH_RECOVER_AFTER:
                self._max_batch = min(MAX_POINTS, self._max_batch * 2)
                self._complete_streak = 0
                _LOGGER.info(
                    "[%s] Indevolt replies complete again; batch size back to %d",
                    self._name, self._max_batch,
                )
        if any(p in updated for p in self._tiers[0].points):
            self._first_read.set()
        if updated:
            self._notify(self._on_telemetry, frozenset(updated))

    def _after_incomplete(self, points: list[int], missing: list[int]) -> None:
        if len(points) > 1:
            self._shrink(len(points), missing)
        elif points[0] not in self._tiers[0].points:
            # Control points are always retried.
            self._mark_unsupported(points[0])

    def _shrink(self, batch: int, missing: list[int]) -> None:
        """Halve the batch size after a reply that was cut short."""
        self._complete_streak = 0
        new_size = max(1, batch // 2)
        if new_size < self._max_batch:
            self._max_batch = new_size
            _LOGGER.warning(
                "[%s] Indevolt reply was missing points %s; reading in batches of %d",
                self._name, missing, self._max_batch,
            )

    def _mark_unsupported(self, point: int) -> None:
        """A point that does not come back even alone is not on this model/firmware."""
        self._unsupported[point] = self._clock()
        _LOGGER.info("[%s] Indevolt point %d is not reported; skipping it", self._name, point)

    def _after_failure(self, err: IndevoltError | None) -> None:
        current = self._current
        self.requests += 1
        if isinstance(err, IndevoltConnectionError):
            self.timeouts += 1
        else:
            self.errors += 1
        self._record_health(False)
        if current is not None and current[0] == "write":
            # Outcome unknown: the device may or may not hold the tuple now.
            self._last_accepted = None
            self._resolve_waiters(current[1], False)
            self._notify(self._on_write, current[1], WRITE_FAILED)
        if isinstance(err, IndevoltAuthenticationError) and not self._auth_warned:
            self._auth_warned = True
            _LOGGER.warning(
                "[%s] Indevolt asks for authentication; set its local API to HTTP "
                "(not HTTP Digest) in the Indevolt app",
                self._name,
            )
        if not self._closed:
            _LOGGER.debug("[%s] Indevolt request failed: %s", self._name, err)
            # Let the driver re-check freshness without waiting for a poll.
            self._notify(self._on_telemetry, frozenset())

    def _record_health(self, healthy: bool) -> None:
        if healthy:
            self._consecutive_failures = 0
            self._healthy_streak += 1
            if self._factor > 1 and self._healthy_streak >= HEALTHY_STREAK:
                self._factor //= 2
                self._healthy_streak = 0
                if self._factor == 1:
                    self._throttled_since = None
            return
        self._consecutive_failures += 1
        self._healthy_streak = 0
        if self._factor < MAX_BACKOFF_FACTOR:
            self._factor *= 2
            _LOGGER.info(
                "[%s] Indevolt slow or failing; request spacing now %.0f s",
                self._name, self.interval_s,
            )
        if self._throttled_since is None:
            self._throttled_since = self._clock()

    def _notify(self, callback: Callable[..., None] | None, *args: Any) -> None:
        """Run a driver callback; its bugs must not count as device failures."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[%s] Indevolt driver callback failed", self._name)

    def _resolve_waiters(self, values: tuple[int, int, int], ok: bool) -> None:
        for wanted, future in self._write_waiters:
            if wanted == values and not future.done():
                future.set_result(ok)

    def _fail_waiters(self, error: IndevoltError) -> None:
        for _, future in self._write_waiters:
            if not future.done():
                future.set_exception(error)
