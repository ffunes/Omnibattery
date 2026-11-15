"""Self-test of the brand-branch guardian: it must catch what it promises to."""

from __future__ import annotations

from collections import Counter

from tests.test_no_brand_branches import (
    diff_against_allowlist,
    scan_js_source,
    scan_python_source,
)

SAMPLE = '''
"""Module docstring mentioning Zendure and marstek."""
from ..drivers.zendure import X
from ..drivers.base import BatteryDriver
from ..infra.modbus_client import Y

def f(c, battery_version, x):
    """Docstring about Zendure."""
    # comment about zendure
    a = x.brand == "foo"
    b = getattr(c, "brand", None) == "zendure"
    d = battery_version in ("vA", "vD")
    e = MAX_POWER_BY_VERSION[battery_version]
    g = REGISTER_MAP.get(battery_version, None)
    h = "Zendure"
    is_anker = False
    _LOGGER.info("zendure %s", x)
    return "lunar emma"
'''


def _scan(src=SAMPLE, rel="x.py"):
    return Counter(scan_python_source(src, rel))


def test_detects_every_kind():
    found = _scan()
    kinds = {line.split(":", 3)[2] for line in found}
    assert kinds == {"literal", "identifier", "compare", "lookup", "import"}
    assert found["x.py:f:compare:x.brand == 'foo'"] == 1
    assert found["x.py:f:compare:getattr(c, 'brand', None) == 'zendure'"] == 1
    assert found["x.py:f:compare:battery_version in ('vA', 'vD')"] == 1
    assert found["x.py:f:lookup:MAX_POWER_BY_VERSION[battery_version]"] == 1
    assert found["x.py:f:lookup:REGISTER_MAP.get(battery_version, None)"] == 1
    assert found["x.py:f:literal:zendure"] == 2  # "Zendure" + the compare's literal
    assert found["x.py:f:literal:emma"] == 1  # "lunar" must not match
    assert found["x.py:f:identifier:is_anker"] == 1
    assert found["x.py:<module>:import:drivers.zendure"] == 1
    assert found["x.py:<module>:import:infra.modbus_client"] == 1
    assert not any("drivers.base" in line for line in found)


def test_ignores_docstring_comment_and_log_calls():
    src = '''
"""Zendure docstring."""
def f(x):
    """Marstek docstring."""
    # zendure comment
    _LOGGER.info("zendure %s", x)
    self.log.warning("huawei", brand=x.brand == "anker")
'''
    assert not _scan(src)


def test_permanent_exceptions_are_rules():
    assert not scan_python_source('x = "marstek_venus_system_foo"', "sensor.py")
    assert not scan_python_source("class MarstekVenusNumber: pass", "number.py")
    assert not scan_python_source('x = brand == "zendure"', "domain_migration.py")
    mig = "async def async_migrate_entry(h):\n    return h.brand == 'zendure'\n"
    assert not scan_python_source(mig, "__init__.py")
    assert scan_python_source(mig, "other.py")


def test_js_scan_strips_comments():
    js = "// zendure\n/* anker\n */ const a = 'Solarbank'; const u = 'http://x'; // venus"
    assert scan_js_source(js, "frontend/p.js") == ["frontend/p.js:<js>:js:solarbank"]


def test_multiset_diff_reports_new_and_obsolete():
    new, obsolete = diff_against_allowlist(["a", "a", "b"], ["a", "c"])
    assert new == ["a", "b"] and obsolete == ["c"]
