"""The deferred daily Dynamic Pricing evaluation is retried all day.

Prices missing at 00:05 were retried only inside hour 0 (and the 4th retry
could never match its minute), so a provider publishing at 01:10 left the
day without a schedule.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from custom_components.omnibattery.pricing.engine import PricingManager

TODAY = date(2026, 11, 9)


def _manager(count=1, last=None, evaluated=None):
    controller = SimpleNamespace(
        _dynamic_pricing_evaluated_date=evaluated,
        _dp_eval_retry_count=count,
        _dp_eval_retry_at=last,
    )
    return PricingManager(SimpleNamespace(), controller)


def test_no_retry_without_a_deferred_evaluation():
    assert _manager(count=0)._dp_eval_retry_due(datetime(2026, 11, 9, 2, 0)) is False


def test_no_retry_once_the_day_is_evaluated():
    manager = _manager(evaluated=TODAY, last=datetime(2026, 11, 9, 0, 5))
    assert manager._dp_eval_retry_due(datetime(2026, 11, 9, 3, 0)) is False


def test_first_hour_retries_every_15_minutes():
    manager = _manager(last=datetime(2026, 11, 9, 0, 5))
    assert manager._dp_eval_retry_due(datetime(2026, 11, 9, 0, 19)) is False
    assert manager._dp_eval_retry_due(datetime(2026, 11, 9, 0, 20)) is True


def test_retries_continue_after_the_first_hour():
    manager = _manager(count=4, last=datetime(2026, 11, 9, 0, 50))
    assert manager._dp_eval_retry_due(datetime(2026, 11, 9, 1, 10)) is False
    assert manager._dp_eval_retry_due(datetime(2026, 11, 9, 1, 20)) is True
    manager = _manager(count=12, last=datetime(2026, 11, 9, 13, 0))
    assert manager._dp_eval_retry_due(datetime(2026, 11, 9, 13, 30)) is True


def test_unstamped_or_stale_count_retries_now():
    assert _manager(last=None)._dp_eval_retry_due(datetime(2026, 11, 9, 0, 6)) is True
    stale = _manager(last=datetime(2026, 11, 8, 23, 0))
    assert stale._dp_eval_retry_due(datetime(2026, 11, 9, 0, 6)) is True
