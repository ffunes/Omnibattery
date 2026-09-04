"""Bounded and lifecycle-aware access to Home Assistant Recorder.

Recorder history is an optional learning input.  It must never become part of
the control path, and a config-entry reload must be able to invalidate work
which is still waiting for an executor result.  This module owns the small
amount of coordination needed by the profile trackers:

* only one backfill job is allowed to query Recorder at a time;
* every query is for one local calendar day (23, 24, or 25 real hours);
* the returned state list is detached from Recorder's mapping immediately;
* a generation token prevents late executor results from being applied;
* task, query and duration counters stay bounded for diagnostics.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from time import monotonic
from typing import Any

_LOGGER = logging.getLogger(__name__)


class BackfillInvalidated(RuntimeError):
    """Raised internally when a result belongs to an old entry generation."""


@dataclass(frozen=True)
class BackfillToken:
    """Immutable ownership token captured by one background backfill job."""

    coordinator: "RecorderBackfillCoordinator"
    generation: int

    def is_valid(self) -> bool:
        """Return whether this job may still apply results."""
        return self.coordinator.is_token_valid(self)


def local_day_bounds(
    local_date: date,
    timezone: Any,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return the absolute bounds for one local day, capped for the current day.

    ``datetime`` subtraction on a zone-aware pair is deliberately not used to
    construct the endpoint.  Combining each wall-clock midnight separately
    preserves the real 23/25-hour length on DST transitions.
    """
    start = datetime.combine(local_date, time.min, tzinfo=timezone)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=timezone)
    if now is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone)
        else:
            now = now.astimezone(timezone)
        if now.date() == local_date:
            end = min(end, now)
    return start, end


class RecorderBackfillCoordinator:
    """Serialize Recorder jobs and expose cancellation-safe query helpers."""

    def __init__(self, hass: Any, config_entry: Any) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self._job_lock = asyncio.Lock()
        self._query_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._generation = 1
        self._stopping = False

        self._job_status = "idle"
        self._current_job: str | None = None
        self._current_entity: str | None = None
        self._current_block: str | None = None
        self._last_error: str | None = None
        self._last_duration_s = 0.0
        self._max_duration_s = 0.0
        self._queries = 0
        self._blocks = 0
        self._skipped_blocks = 0
        self._failed_blocks = 0
        self._states = 0
        self._active_queries = 0
        self._max_active_queries = 0
        self._last_query_range_s = 0.0
        self._max_query_range_s = 0.0

    @property
    def generation(self) -> int:
        """Return the current entry-owned generation."""
        return self._generation

    @property
    def stopping(self) -> bool:
        return self._stopping

    def is_token_valid(self, token: BackfillToken | None) -> bool:
        """Check a token without touching any profile or Store state."""
        return bool(
            token is not None
            and not self._stopping
            and token.coordinator is self
            and token.generation == self._generation
        )

    def new_token(self) -> BackfillToken:
        """Return a token for a small caller-owned query."""
        return BackfillToken(self, self._generation)

    def _create_task(self, coroutine: Awaitable[Any], name: str) -> asyncio.Task | None:
        """Create a task owned by the ConfigEntry when the HA API is present."""
        create = getattr(self.config_entry, "async_create_background_task", None)
        task = None
        if callable(create):
            try:
                task = create(self.hass, coroutine, name)
            except TypeError:
                # Small test doubles and older HA versions used the two-argument
                # spelling.  Close the first coroutine only if neither call can
                # accept it; the normal HA path never enters this branch.
                try:
                    task = create(coroutine, name)
                except TypeError:
                    close = getattr(coroutine, "close", None)
                    if callable(close):
                        close()
                    raise
        else:
            create = getattr(self.hass, "async_create_task", None)
            if callable(create):
                try:
                    task = create(coroutine, name=name)
                except TypeError:
                    task = create(coroutine)
            else:
                try:
                    task = asyncio.get_running_loop().create_task(coroutine, name=name)
                except TypeError:
                    task = asyncio.get_running_loop().create_task(coroutine)

        if task is None:
            # ConfigEntry owns the task even when an older implementation does
            # not return it.  It cannot be awaited here, but unload still invokes
            # ``async_cancel`` and invalidates its generation before any result
            # can be applied.
            return None
        if isinstance(task, asyncio.Task):
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return task

    def submit(
        self,
        name: str,
        worker: Callable[[BackfillToken], Awaitable[Any]],
    ) -> asyncio.Task | None:
        """Queue one serial backfill worker and return its owned task."""
        if self._stopping:
            return None
        token = BackfillToken(self, self._generation)

        async def run() -> Any:
            started = monotonic()
            try:
                async with self._job_lock:
                    if not token.is_valid():
                        raise BackfillInvalidated
                    self._job_status = "running"
                    self._current_job = name
                    self._last_error = None
                    result = await worker(token)
                    if not token.is_valid():
                        raise BackfillInvalidated
                    return result
            except BackfillInvalidated:
                self._job_status = "cancelled"
                return False
            except asyncio.CancelledError:
                self._job_status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - learning is best effort
                self._job_status = "failed"
                self._last_error = f"{name}: {exc}"
                _LOGGER.warning("Recorder backfill %s failed: %s", name, exc)
                return False
            finally:
                duration = max(0.0, monotonic() - started)
                self._last_duration_s = duration
                self._max_duration_s = max(self._max_duration_s, duration)
                self._current_job = None
                self._current_entity = None
                self._current_block = None
                if self._job_status == "running":
                    self._job_status = "complete"

        return self._create_task(run(), f"omnibattery_recorder_{name}")

    async def async_query(
        self,
        token: BackfillToken,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        block: str | None = None,
        include_start_time_state: bool | None = None,
    ) -> list[Any] | None:
        """Query one bounded Recorder range and return only its state list.

        ``None`` means the token was invalidated.  An empty list is a valid
        query result and is intentionally distinct from cancellation.
        """
        if not token.is_valid():
            return None
        start_ts = start.timestamp()
        end_ts = end.timestamp()
        if end_ts <= start_ts:
            return []

        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:
            self._last_error = "recorder unavailable"
            return []

        self._current_entity = entity_id
        self._current_block = block
        self._blocks += 1
        self._queries += 1
        range_s = max(0.0, end_ts - start_ts)
        self._last_query_range_s = range_s
        self._max_query_range_s = max(self._max_query_range_s, range_s)
        try:
            recorder = get_instance(self.hass)
            async with self._query_lock:
                if not token.is_valid():
                    return None
                self._active_queries += 1
                self._max_active_queries = max(
                    self._max_active_queries, self._active_queries
                )
                executor_job = None
                try:
                    if include_start_time_state is None:
                        executor_job = recorder.async_add_executor_job(
                            history.state_changes_during_period,
                            self.hass,
                            start,
                            end,
                            entity_id,
                        )
                    else:
                        query = partial(
                            history.state_changes_during_period,
                            self.hass,
                            start,
                            end,
                            entity_id,
                            include_start_time_state=include_start_time_state,
                        )
                        executor_job = recorder.async_add_executor_job(query)
                    try:
                        # Cancelling the asyncio wrapper does not necessarily
                        # stop the executor thread. Keep _query_lock held until
                        # that thread has returned, so a reload cannot overlap
                        # it with a new Recorder query.
                        states_map = await asyncio.shield(executor_job)
                    except asyncio.CancelledError:
                        try:
                            await asyncio.shield(executor_job)
                        except Exception:  # noqa: BLE001 - cancellation wins
                            pass
                        raise
                finally:
                    self._active_queries = max(0, self._active_queries - 1)
            if not token.is_valid():
                return None
            if not isinstance(states_map, Mapping):
                return []
            # Detach the list from the Recorder result before yielding.  The
            # caller processes it immediately and deletes its reference before
            # requesting the next local day.
            states = list(states_map.get(entity_id, ()) or ())
            del states_map
            self._states += len(states)
            await asyncio.sleep(0)
            return states if token.is_valid() else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a single block is retryable
            self._failed_blocks += 1
            self._last_error = f"{entity_id}: {exc}"
            _LOGGER.debug("Recorder query failed for %s: %s", entity_id, exc)
            return []
        finally:
            self._current_entity = None
            self._current_block = None

    def note_skipped(self) -> None:
        """Count a complete coverage block without logging it."""
        self._skipped_blocks += 1

    async def async_cancel(self) -> None:
        """Invalidate all jobs and await tasks known to this coordinator."""
        self._stopping = True
        self._generation += 1
        self._job_status = "cancelled"
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._current_job = None
        self._current_entity = None
        self._current_block = None

    def diagnostics(self) -> dict[str, Any]:
        """Return bounded Recorder/backfill counters for integration diagnostics."""
        return {
            "state": self._job_status,
            "profile": self._current_job,
            "entity": self._current_entity,
            "block": self._current_block,
            "last_error": self._last_error,
            "blocks_queried": self._blocks,
            "blocks_skipped": self._skipped_blocks,
            "blocks_failed": self._failed_blocks,
            "states_processed": self._states,
            "duration_last_s": round(self._last_duration_s, 3),
            "duration_max_s": round(self._max_duration_s, 3),
            "generation": self._generation,
            "active_queries": self._active_queries,
            "max_active_queries": self._max_active_queries,
            "query_range_last_s": round(self._last_query_range_s, 3),
            "query_range_max_s": round(self._max_query_range_s, 3),
        }


__all__ = [
    "BackfillInvalidated",
    "BackfillToken",
    "RecorderBackfillCoordinator",
    "local_day_bounds",
]
