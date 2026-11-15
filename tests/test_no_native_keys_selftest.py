"""Self-test of the native-key guardian: it must catch what it promises to."""

from __future__ import annotations

from collections import Counter

from tests.test_no_native_keys import diff_against_allowlist, scan_python_source

SAMPLE = '''
"""Docstring mentioning soc_set and inverter_state == 4."""
from .const import ALARM_BIT_DESCRIPTIONS, FAULT_BIT_DESCRIPTIONS as F

def bypass(value):
    return value == INVERTER_STATE_AC_BYPASS

def backup(value):
    return value in ("Off-grid", "Ready") or value == 0

def f(self, coordinator, data, entry):
    """Docstring about soc_status."""
    # comment about fault_status
    a = self.data.get("min_soc")
    b = coordinator.data["min_soc"]
    c = "min_soc" in self.data
    d = data.get("min_soc")
    k = data.get("soc_set")
    inv = coordinator.data.get("inverter_state")
    e = inv == 6
    g = inv in (1, 4)
    h = coordinator.data.get("inverter_state") >= 2
    bypass(coordinator.data.get("inverter_state"))
    value = coordinator.data.get("backup_function")
    backup(value)
    j = value == "Off-grid"
    return ALARM_BIT_DESCRIPTIONS
'''

CLEAN = '''
"""Docstring mentioning soc_set and inverter_state == 4."""

def f(self, battery_config, entry, user_input, merged):
    """Docstring about soc_status."""
    # comment about fault_status, inverter_state == 4
    a = battery_config.get("min_soc")
    b = entry.data.get("min_soc")
    c = self.config_entry.data["min_soc"]
    d = user_input["min_soc"]
    e = merged["min_soc"]
    self.data["min_soc"] = 5
    inv = self.data.get("inverter_state")
    ok = inv is not None and inv != None
    if inv is None:
        return other == 4
    _LOGGER.info("soc_set %s inverter_state %s", "soc_status", self.data.get("alarm_status"))
    _LOGGER.debug("x", extra="fault_status")
    return power == 4 and inv.real
'''


def _scan(src=SAMPLE, rel="x.py"):
    return Counter(scan_python_source(src, rel))


def test_detects_every_kind():
    found = _scan()
    kinds = Counter(line.split(":", 3)[2] for line in found.elements())
    assert kinds["key"] == 1
    assert kinds["data_key"] == 4
    assert kinds["state_code"] == 4
    assert kinds["backup_code"] == 3
    assert kinds["alarm_table"] == 3


def test_detects_each_rule_precisely():
    found = _scan()
    expected = {
        "x.py:f:key:soc_set",
        "x.py:f:data_key:self.data.get('min_soc')",
        "x.py:f:data_key:coordinator.data['min_soc']",
        "x.py:f:data_key:'min_soc' in self.data",
        "x.py:f:data_key:data.get('min_soc')",
        "x.py:bypass:state_code:value == INVERTER_STATE_AC_BYPASS",
        "x.py:f:state_code:inv == 6",
        "x.py:f:state_code:inv in (1, 4)",
        "x.py:f:state_code:coordinator.data.get('inverter_state') >= 2",
        "x.py:backup:backup_code:value == 0",
        "x.py:backup:backup_code:value in ('Off-grid', 'Ready')",
        "x.py:f:backup_code:value == 'Off-grid'",
        "x.py:<module>:alarm_table:ALARM_BIT_DESCRIPTIONS",
        "x.py:<module>:alarm_table:FAULT_BIT_DESCRIPTIONS",
        "x.py:f:alarm_table:ALARM_BIT_DESCRIPTIONS",
    }
    assert expected <= set(found)


def test_ignores_docstrings_comments_logs_config_and_non_compares():
    assert _scan(CLEAN) == Counter()


def test_battery_config_min_soc_is_not_a_finding():
    src = 'def f(battery_config):\n    return battery_config.get("min_soc"), battery_config["min_soc"]\n'
    assert _scan(src) == Counter()


def test_permanent_exceptions_are_rules():
    src = 'def f(d):\n    return d.get("soc_set")\n'
    assert _scan(src, "domain_migration.py") == Counter()
    init = 'def _async_migrate_x(d):\n    return d.get("soc_set")\n'
    assert _scan(init, "__init__.py") == Counter()
    assert _scan(init, "other.py")


def test_multiset_diff_reports_new_and_obsolete():
    new, obsolete = diff_against_allowlist(["a", "a", "b"], ["a", "c"])
    assert new == ["a", "b"]
    assert obsolete == ["c"]
