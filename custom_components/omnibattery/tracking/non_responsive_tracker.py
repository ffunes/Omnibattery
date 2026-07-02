"""Non-responsive battery tracking for Marstek Venus.

Excludes batteries that ACK power commands but fail to deliver power
(e.g. firmware glitch, BMS lockout). Excluded batteries are retried after
a flat cooldown — the goal is to recover a battery as soon as possible, so
the penalty never grows across episodes.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class NonResponsiveTracker:
    """Track per-battery non-delivery events and gate exclusion via cooldown."""

    def __init__(
        self,
        fail_threshold: int = 3,
        cooldown_min: int = 5,
    ) -> None:
        self._fail_threshold = fail_threshold
        # Flat retry cooldown — never grows across episodes. Recovering the
        # battery fast matters more than penalising a chronically flaky one; a
        # still-dead battery just re-excludes on its next failed cycle.
        self._cooldown_min = cooldown_min
        # coordinator -> {"fail_count": int, "excluded_at": datetime|None}
        self.batteries: dict[Any, dict] = {}

    @property
    def cooldown_min(self) -> int:
        """Flat exclusion cooldown in minutes (read by the diagnostic sensor)."""
        return self._cooldown_min

    def is_excluded(self, coordinator) -> bool:
        """Return True if the battery is currently in non-responsive cooldown.

        When the cooldown expires, the battery is allowed one retry window:
        fail_count is reset so it re-enters the pool. The wake-grace budget also
        resets, so the next failure episode gets its own one-shot
        wake-before-exclude round.
        """
        info = self.batteries.get(coordinator)
        if not info or info["excluded_at"] is None:
            return False
        elapsed_min = (dt_util.utcnow() - info["excluded_at"]).total_seconds() / 60
        if elapsed_min >= self._cooldown_min:
            _LOGGER.info(
                "[%s] Non-responsive cooldown expired (%d min) - retrying battery",
                coordinator.name, self._cooldown_min,
            )
            info["excluded_at"] = None
            info["fail_count"] = 0
            info["wake_used"] = False
            return False
        return True

    def _record_fail(
        self, coordinator, reason: str, detail: str,
        *, retry_attempted: bool = False, allow_wake_grace: bool = False,
    ) -> str | None:
        """Increment the fail counter for a battery and exclude after threshold.

        ``reason`` is a stable category string surfaced on the diagnostic sensor:
        ``non_delivery`` (ACK ok, awake, ~0 W out), ``standby_no_delivery`` (ACK ok
        but the inverter sits in standby), ``modbus_write_failed`` /
        ``driver_exception`` (unexpected exception from driver), ``feedback_timeout`` (write
        accepted but the readback never followed) or ``ack_mismatch`` (readback did
        not match the command).

        When ``allow_wake_grace`` is set and the threshold is crossed for the
        first time this episode, the battery is NOT excluded yet: the fail
        counter is reset instead, so the very next cycle re-tries it in the pool
        (with a fresh reconnect/re-assert already attempted by the caller) rather
        than paying a 5-minute cooldown for a fault the wake may have already
        fixed. Only a second consecutive threshold-cross in the same episode
        actually excludes.

        Returns ``"wake"`` on the grace round, ``"excluded"`` on the call that
        excludes the battery, or ``None`` otherwise.
        """
        info = self.batteries.setdefault(
            coordinator,
            {"fail_count": 0, "excluded_at": None, "wake_used": False},
        )
        info["fail_count"] += 1
        info["reason"] = reason
        info["retry_attempted"] = retry_attempted
        _LOGGER.debug(
            "[%s] %s (fail %d/%d, reason=%s, retry=%s)",
            coordinator.name, detail, info["fail_count"], self._fail_threshold,
            reason, retry_attempted,
        )
        if info["fail_count"] >= self._fail_threshold and info["excluded_at"] is None:
            if allow_wake_grace and not info["wake_used"]:
                info["wake_used"] = True
                info["fail_count"] = 0
                _LOGGER.info(
                    "[%s] Non-responsive after %d consecutive cycles (reason=%s) — "
                    "attempting a wake nudge before excluding (grace round).",
                    coordinator.name, self._fail_threshold, reason,
                )
                return "wake"
            info["excluded_at"] = dt_util.utcnow()
            _LOGGER.warning(
                "[%s] Non-responsive after %d consecutive cycles (reason=%s, "
                "retry_attempted=%s) — %s. Excluding from pool for %d minutes.",
                coordinator.name, self._fail_threshold, reason,
                retry_attempted, detail, self._cooldown_min,
            )
            return "excluded"
        return None

    def record_non_delivery(
        self, coordinator, commanded: float, actual: float,
        *, reason: str = "non_delivery", retry_attempted: bool = False,
    ) -> str | None:
        """Record a cycle where the battery ACK'd but delivered ~0 W.

        Returns ``"wake"`` on the grace round (caller should attempt a wake
        nudge and leave the battery in the pool), ``"excluded"`` on the call
        that excludes the battery for real, or ``None`` otherwise.
        """
        return self._record_fail(
            coordinator, reason,
            f"ACK ok but not delivering power: commanded={int(commanded)}W, actual={int(actual)}W",
            retry_attempted=retry_attempted,
            allow_wake_grace=True,
        )

    def record_comm_failure(
        self, coordinator, reason: str, *, retry_attempted: bool = True,
    ) -> bool:
        """Record a cycle where the write or its feedback failed at the Modbus level.

        Returns True on the call that just excluded the battery. No wake grace
        here — comm failures aren't fixed by the discharge wake nudge.
        """
        return self._record_fail(
            coordinator, reason, "Power write/feedback did not complete",
            retry_attempted=retry_attempted,
        ) == "excluded"

    def set_wake_attempted(self, coordinator, value: bool) -> None:
        """Flag whether a wake nudge was sent at the moment of exclusion."""
        info = self.batteries.get(coordinator)
        if info:
            info["wake_attempted"] = value

    def clear(self, coordinator) -> None:
        """Mark a battery as healthy (delivering power) and reset its exclusion state."""
        info = self.batteries.get(coordinator)
        if info:
            was_excluded = info["excluded_at"] is not None
            info["fail_count"] = 0
            info["excluded_at"] = None
            info["reason"] = None
            info["retry_attempted"] = False
            info["wake_attempted"] = False
            info["wake_used"] = False
            if was_excluded:
                _LOGGER.info(
                    "[%s] Battery is delivering power again - returned to pool",
                    coordinator.name,
                )

    def excluded_names(self) -> list[str]:
        """Return names of batteries currently excluded due to non-responsive behavior."""
        now = dt_util.utcnow()
        return [
            c.name
            for c, info in self.batteries.items()
            if info.get("excluded_at") is not None
            and (now - info["excluded_at"]).total_seconds() / 60 < self._cooldown_min
        ]
