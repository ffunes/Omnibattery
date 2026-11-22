"""Price-data health: string-typed attributes and the Repairs issue.

A price sensor that stops delivering usable slots used to be invisible: every
per-entry parse failure is debug-level and the resulting status only shows up as
an attribute on the predictive-charging binary sensor, so price-aware charging
could stay silently inactive for weeks. These tests pin the two mechanisms that
surface it: the ``bad_format`` type check and the sustained-failure Repairs issue.

No hardware, no running Home Assistant: ``PricingManager`` only stores its
``hass``/``controller`` references, so a SimpleNamespace pair is enough.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.omnibattery.const import (
    PREDICTIVE_MODE_DYNAMIC_PRICING,
    PREDICTIVE_MODE_REALTIME_PRICE,
    PREDICTIVE_MODE_TIME_SLOT,
    PRICE_DATA_ISSUE_DELAY_S,
    PRICE_HEALTH_CHECK_INTERVAL_S,
    PRICE_INTEGRATION_EPEX,
)
from custom_components.omnibattery.infra.clock import local_now
from custom_components.omnibattery.pricing import engine as pricing_engine
from custom_components.omnibattery.pricing.engine import PricingManager


class _FakeIssueRegistry:
    """Records Repairs create/delete calls instead of touching a real registry."""

    def __init__(self):
        self.created: list[tuple] = []
        self.deleted: list[tuple] = []
        self.IssueSeverity = SimpleNamespace(WARNING="warning")

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.created.append((issue_id, kwargs))

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append(issue_id)


@pytest.fixture
def issues(monkeypatch):
    fake = _FakeIssueRegistry()
    monkeypatch.setattr(pricing_engine, "ir", fake)
    return fake


def _epex_entries():
    """Two hourly EPEX entries spanning now, as ISO strings."""
    start = local_now().replace(minute=0, second=0, microsecond=0)
    return [
        {
            "start_time": (start + timedelta(hours=i)).isoformat(),
            "end_time": (start + timedelta(hours=i + 1)).isoformat(),
            "price_per_kwh": 0.10 + i / 100,
        }
        for i in range(2)
    ]


def _manager(monkeypatch, data_attr):
    """PricingManager over an EPEX price sensor exposing ``data_attr``."""
    monkeypatch.setattr(
        pricing_engine, "resolve_official_nordpool_source", lambda *_args: None
    )
    state = SimpleNamespace(state="0.10", attributes={"data": data_attr})
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: state))
    ctrl = SimpleNamespace(
        hass=hass,
        config_entry=SimpleNamespace(entry_id="abc123"),
        price_sensor="sensor.dynamic_price",
        price_integration_type=PRICE_INTEGRATION_EPEX,
        _price_data_status="not_evaluated",
        _price_health_last_check=None,
        _price_data_bad_since=None,
        _price_data_issue_created=False,
        _price_data_issue_cleared=False,
        predictive_charging_enabled=True,
        predictive_charging_mode=PREDICTIVE_MODE_DYNAMIC_PRICING,
        charge_delay_enabled=False,
    )
    return PricingManager(hass, ctrl), ctrl


# ----------------------------------------------------------------------
# bad_format detection
# ----------------------------------------------------------------------

def test_list_attribute_parses_normally(monkeypatch):
    mgr, ctrl = _manager(monkeypatch, _epex_entries())

    slots = mgr._parse_price_data()

    assert slots
    assert ctrl._price_data_status.startswith("ok")


def test_stringified_attribute_is_reported_as_bad_format(monkeypatch):
    # What a template sensor produces when its rendered list holds datetime
    # objects: Home Assistant cannot literal_eval it, so the attribute stays a str.
    mgr, ctrl = _manager(monkeypatch, str(_epex_entries()))

    slots = mgr._parse_price_data()

    assert slots == []
    assert ctrl._price_data_status == "bad_format"


# ----------------------------------------------------------------------
# Repairs issue lifecycle
# ----------------------------------------------------------------------

def test_no_issue_before_the_failure_delay_elapses(monkeypatch, issues):
    mgr, ctrl = _manager(monkeypatch, str(_epex_entries()))
    ctrl._price_data_status = "bad_format"

    mgr._update_price_data_issue(1000.0)  # first failure: only starts the clock
    mgr._update_price_data_issue(1000.0 + PRICE_DATA_ISSUE_DELAY_S - 1)

    assert issues.created == []
    assert ctrl._price_data_bad_since == 1000.0


def test_sustained_failure_creates_one_issue(monkeypatch, issues):
    mgr, ctrl = _manager(monkeypatch, str(_epex_entries()))
    ctrl._price_data_status = "bad_format"

    mgr._update_price_data_issue(1000.0)
    mgr._update_price_data_issue(1000.0 + PRICE_DATA_ISSUE_DELAY_S)
    mgr._update_price_data_issue(1000.0 + PRICE_DATA_ISSUE_DELAY_S * 2)

    assert len(issues.created) == 1
    issue_id, kwargs = issues.created[0]
    assert issue_id == "price_data_unusable_abc123"
    assert kwargs["translation_key"] == "price_data_unusable"
    assert kwargs["translation_placeholders"]["sensor"] == "sensor.dynamic_price"
    assert kwargs["translation_placeholders"]["status"] == "bad_format"


def test_recovery_clears_the_issue(monkeypatch, issues):
    mgr, ctrl = _manager(monkeypatch, str(_epex_entries()))
    ctrl._price_data_status = "bad_format"
    mgr._update_price_data_issue(1000.0)
    mgr._update_price_data_issue(1000.0 + PRICE_DATA_ISSUE_DELAY_S)

    ctrl._price_data_status = "ok (24 slots)"
    mgr._update_price_data_issue(1000.0 + PRICE_DATA_ISSUE_DELAY_S + 60)

    assert issues.deleted == ["price_data_unusable_abc123"]
    assert ctrl._price_data_bad_since is None
    assert ctrl._price_data_issue_created is False


def test_healthy_prices_clear_a_stale_issue_once(monkeypatch, issues):
    # The issue is persistent, so it outlives the run that raised it while
    # _price_data_issue_created does not. A fresh, healthy run must still clear it,
    # but only once — not on every poll for the rest of the day.
    mgr, ctrl = _manager(monkeypatch, _epex_entries())
    ctrl._price_data_status = "ok (24 slots)"

    mgr._update_price_data_issue(1000.0)
    mgr._update_price_data_issue(2000.0)

    assert issues.created == []
    assert issues.deleted == ["price_data_unusable_abc123"]


def test_slots_that_all_lie_in_the_past_are_not_ok(monkeypatch):
    start = local_now().replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    stale = [
        {
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
            "price_per_kwh": 0.10,
        }
    ]
    mgr, ctrl = _manager(monkeypatch, stale)

    assert mgr._parse_price_data() == []
    assert ctrl._price_data_status == "no_future_slots"


# ----------------------------------------------------------------------
# Health-check throttle
# ----------------------------------------------------------------------

def test_health_check_is_throttled_to_its_interval(monkeypatch, issues):
    mgr, ctrl = _manager(monkeypatch, _epex_entries())
    parses = []
    monkeypatch.setattr(
        mgr, "_parse_price_data", lambda **kwargs: parses.append(kwargs) or []
    )
    clock = [5000.0]
    monkeypatch.setattr(pricing_engine, "monotonic", lambda: clock[0])

    mgr.maybe_check_price_data_health()
    clock[0] += PRICE_HEALTH_CHECK_INTERVAL_S - 1
    mgr.maybe_check_price_data_health()
    assert len(parses) == 1

    clock[0] += 2
    mgr.maybe_check_price_data_health()
    assert len(parses) == 2


def test_health_check_skips_and_clears_when_prices_are_not_used(monkeypatch, issues):
    # Turning predictive charging off (or switching to time-slot mode) must not
    # strand a persistent issue that nothing would ever clear again.
    mgr, ctrl = _manager(monkeypatch, _epex_entries())
    ctrl.predictive_charging_mode = PREDICTIVE_MODE_TIME_SLOT
    parses = []
    monkeypatch.setattr(mgr, "_parse_price_data", lambda **kwargs: parses.append(kwargs) or [])

    mgr.maybe_check_price_data_health()

    assert parses == []
    assert issues.deleted == ["price_data_unusable_abc123"]


def test_realtime_mode_only_checks_prices_when_the_charge_delay_needs_slots(monkeypatch):
    mgr, ctrl = _manager(monkeypatch, _epex_entries())
    ctrl.predictive_charging_mode = PREDICTIVE_MODE_REALTIME_PRICE

    # Real-time charging runs off the scalar current price: a slot-less sensor is
    # no defect, so nothing to report.
    assert mgr._prices_are_load_bearing() is False

    # ...unless the charge delay's price-aware release consumes the slots.
    ctrl.charge_delay_enabled = True
    assert mgr._prices_are_load_bearing() is True
