"""Refactor golden: record/check every ChronologicalEvaluationRequest of the suite.

Loaded from ``tests/conftest.py`` only when ``OMNI_GOLDEN`` is ``record`` or
``check``.  It wraps ``PricingManager.evaluate_chronological_projection`` and
serializes each request it receives, keyed ``<test nodeid>#<call index>``.
``record`` writes ``tests/golden/plan_requests.json``; ``check`` compares the
run against it and fails the session on any difference.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path

GOLDEN_PATH = Path(__file__).parent / "golden" / "plan_requests.json"

# Tests whose requests differ between two identical runs (non-deterministic).
NONDETERMINISTIC: set[str] = set()

_calls: dict[str, object] = {}
_state = {"nodeid": None, "index": 0}


def _canon(value):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, enum.Enum):
        return _canon(value.value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(round(value, 9))
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "__type__": type(value).__name__,
            **{f.name: _canon(getattr(value, f.name)) for f in dataclasses.fields(value)},
        }
    if isinstance(value, Mapping):
        return {
            str(k): _canon(v)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (set, frozenset)):
        return sorted((_canon(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return f"<{type(value).__name__}>{value!r}"


def pytest_runtest_setup(item) -> None:
    _state["nodeid"] = item.nodeid
    _state["index"] = 0


def pytest_configure(config) -> None:
    from custom_components.omnibattery.pricing.engine import PricingManager

    original = PricingManager.evaluate_chronological_projection

    def wrapper(request):
        nodeid = _state["nodeid"]
        if nodeid is not None and nodeid not in NONDETERMINISTIC:
            key = f"{nodeid}#{_state['index']}"
            _state["index"] += 1
            _calls[key] = _canon(request)
        return original(request)

    PricingManager.evaluate_chronological_projection = staticmethod(wrapper)


def _diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if k not in a:
                yield f"{p}: missing in golden"
            elif k not in b:
                yield f"{p}: missing in run"
            else:
                yield from _diff(a[k], b[k], p)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            yield from _diff(x, y, f"{path}[{i}]")
    elif a != b:
        yield f"{path}: golden={str(a)[:120]} run={str(b)[:120]}"


def pytest_sessionfinish(session, exitstatus) -> None:
    mode = os.environ.get("OMNI_GOLDEN")
    if mode == "record":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(
            json.dumps(_calls, sort_keys=True, indent=0) + "\n", encoding="utf-8"
        )
        print(f"\n[golden] recorded {len(_calls)} requests -> {GOLDEN_PATH}")
    elif mode == "check":
        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        diffs = list(_diff(golden, json.loads(json.dumps(_calls))))
        if diffs:
            print(f"\n[golden] {len(diffs)} DIFFERENCES (first 20):")
            for d in diffs[:20]:
                print("  ", d)
            session.exitstatus = 1
        else:
            print(f"\n[golden] OK: {len(_calls)} requests identical")
