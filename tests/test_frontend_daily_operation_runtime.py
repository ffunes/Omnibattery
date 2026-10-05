"""Execute the shipped timeline without replacing its state or rendering methods.

Node provides DOM primitives only. State decoding, delay/action decisions, cell
accessibility, all 144 timeline cells, SVG paths and navigation run unchanged.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_components import omnibattery

PANEL = Path(omnibattery.__file__).parent / "frontend/marstek-panel.js"
HARNESS = r"""
(async () => {
const fs = require('node:fs');
const vm = require('node:vm');
let Panel;
const context = vm.createContext({
  HTMLElement: class {},
  customElements: { get() {}, define(name, cls) { Panel = cls; } },
});
const runtime = new vm.SourceTextModule(fs.readFileSync(process.argv[1], 'utf8'), { context });
await runtime.link(() => { throw new Error('Unexpected external import'); });
await runtime.evaluate();
const scenario = JSON.parse(fs.readFileSync(0, 'utf8'));
const panel = Object.create(Panel.prototype);
const entityId = 'sensor.test_daily_operation_timeline';
const attrs = { local_date: '2026-10-05', current_index: 8, current_progress: 0.5, ...scenario.attrs };
panel._panelConfig = {};
panel._hass = {
  states: scenario.missingEntity ? {} : {
    [entityId]: { entity_id: entityId, state: scenario.state || 'ready', attributes: attrs },
  },
  entities: scenario.missingEntity ? {} : {
    [entityId]: { entity_id: entityId, platform: 'omnibattery', translation_key: 'daily_operation_timeline' },
  },
  devices: {}, config: { time_zone: 'UTC' },
  locale: { language: 'en', time_zone: 'server', time_format: '24' },
};
const node = () => ({
  style: {}, attributes: {}, toggles: {}, children: {}, className: '',
  hidden: false, textContent: '', innerHTML: '',
  setAttribute(key, value) { this.attributes[key] = value; },
  removeAttribute(key) { delete this.attributes[key]; },
  querySelector(selector) { return this.children[selector] ||= node(); },
  get classList() { return { toggle: (key, value) => { this.toggles[key] = !!value; } }; },
});
const ref = Object.fromEntries(
  'card badge notice hourlyBalanceLegend yAxis socAxis viewport stage tooltip previous next nowMarker nowText'
    .split(' ').map(key => [key, node()])
);
ref.viewport.clientWidth = 480;
ref.viewport.scrollWidth = ref.stage.scrollWidth = 1440;
ref.viewport.scrollLeft = 0;
ref.scrollState = { manual: false, initialized: false, programmaticUntil: 0 };
ref.cells = Array.from({ length: 144 }, node);
ref.paths = Object.fromEntries(
  'solarActual solarForecast consumptionActual consumptionForecast socActual socForecast'
    .split(' ').map(key => [key, node()])
);
panel._r = { dailyOperation: ref };
const snapshot = panel._dailyOperationState();
const indexes = scenario.indexes || [7, 8, 9, 96];
if (scenario.first === 'timeline') panel._patchDailyOperationTimeline();
if (snapshot && scenario.first === 'cell') panel._dailyOperationUpdateCell(snapshot, 8);
const items = snapshot ? indexes.map(index => panel._dailyOperationItem(snapshot, index)) : [];
// Exercise the cell method directly and then the real complete timeline patch.
if (snapshot) for (const index of indexes) panel._dailyOperationUpdateCell(snapshot, index);
if (scenario.first !== 'timeline') panel._patchDailyOperationTimeline();
process.stdout.write(JSON.stringify({
  snapshot: snapshot ? { hasValues: snapshot.hasValues, unavailable: snapshot.unavailable, currentIndex: snapshot.currentIndex } : null,
  items,
  cells: indexes.map(index => ({
    className: ref.cells[index].className, toggles: ref.cells[index].toggles,
    aria: ref.cells[index].attributes['aria-label'] || null,
    delayHidden: ref.cells[index].querySelector('.daily-op-delay-mark').hidden,
    setpointHidden: ref.cells[index].querySelector('.daily-op-setpoint-mark').hidden,
  })),
  patchedCells: ref.cells.filter(cell => Object.hasOwn(cell.attributes, 'aria-label')).length,
  cardHidden: ref.card.hidden, notice: ref.notice.textContent, noticeHidden: ref.notice.hidden,
  paths: Object.fromEntries(Object.entries(ref.paths).map(([key, path]) => [key, path.attributes.d || ''])),
  navigation: { previousDisabled: ref.previous.disabled, nextDisabled: ref.next.disabled, centered: ref.scrollState.initialized },
}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def _run(attrs=None, **scenario):
    node = shutil.which("node")
    assert node, "Node is required to execute the shipped frontend"
    result = subprocess.run(
        [node, "--experimental-vm-modules", "-e", HARNESS, str(PANEL)],
        input=json.dumps({"attrs": attrs or {}, **scenario}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _series(**values):
    """Sparse real payload arrays, including valid zero and absent samples."""
    return [values.get(str(index)) for index in range(96)]


@pytest.mark.parametrize("first", ["item", "cell", "timeline"])
def test_null_delay_is_safe_from_each_runtime_entry_point(first):
    result = _run(
        {"delay": None, "actual_action_mask": _series(**{"8": 2})},
        first=first,
    )
    assert result["items"][1]["actions"] == ["grid"]
    assert result["items"][1]["delay"] is False
    assert result["cells"][1]["delayHidden"] is True
    assert result["patchedCells"] == 144


@pytest.mark.parametrize("delay", [None, {}, {"enabled": False}, {"state": "idle"}])
def test_missing_or_idle_delay_keeps_actual_current_action(delay):
    result = _run(
        {
            "delay": delay,
            "actual_action_mask": _series(**{"8": 4}),
            "actual_source": _series(**{"8": "observed"}),
            "solar_actual_kwh": _series(**{"8": 0}),
        }
    )
    current = result["items"][1]
    assert current["actions"] == ["discharge"]
    assert current["delay"] is False
    assert current["solarActual"] == 0
    assert result["cells"][1]["className"] == "daily-op-cell daily-op-base-discharge"
    assert result["cells"][1]["delayHidden"] is True
    assert "Observed: Battery discharge" in result["cells"][1]["aria"]
    assert result["patchedCells"] == 144
    assert result["navigation"]["centered"] is True


@pytest.mark.parametrize(
    "attrs",
    [
        {},
        {"delay": None},
        {"actual_action_mask": [0] * 96, "planned_action_mask": [0] * 96},
    ],
)
def test_empty_or_zero_masks_do_not_fabricate_timeline_actions(attrs):
    result = _run(attrs)
    assert result["snapshot"]["hasValues"] is False
    assert result["notice"] == "No daily operation data"
    assert result["patchedCells"] == 144
    assert all(
        item["actions"] == [] and item["solarActual"] is None
        for item in result["items"]
    )
    assert all(
        cell["className"] == "daily-op-cell daily-op-base-neutral"
        for cell in result["cells"]
    )
    assert all(
        "No action" in cell["aria"] and "0.000 kWh" not in cell["aria"]
        for cell in result["cells"]
    )
    assert all(path == "" for path in result["paths"].values())


def test_missing_entity_hides_card_without_synthesizing_data():
    result = _run(missingEntity=True)
    assert result["snapshot"] is None
    assert result["cardHidden"] is True
    assert result["patchedCells"] == 0


def test_known_zero_energy_is_evidence_without_an_action():
    result = _run({"solar_actual_kwh": _series(**{"7": 0, "8": 0})})
    assert result["snapshot"]["hasValues"] is True
    assert result["noticeHidden"] is True
    assert result["items"][1]["solarActual"] == 0
    assert result["items"][1]["actions"] == []
    assert "Solar (Real): 0.000 kWh" in result["cells"][1]["aria"]
    assert result["paths"]["solarActual"].startswith("M")
    # The observed current point is the hand-off, not invented future energy.
    assert result["paths"]["solarForecast"].count("M") == 1
    assert "L" not in result["paths"]["solarForecast"]
    assert result["items"][2]["solarForecast"] is None


@pytest.mark.parametrize(
    "state",
    [
        "waiting for solar",
        "waiting for forecast",
        "waiting_for_solar",
        "waiting",
        "blocked",
        "Delayed until 03:00",
    ],
)
def test_waiting_and_delayed_states_only_mark_current_cell(state):
    result = _run({"delay": {"state": state, "enabled": True}})
    assert [item["delay"] for item in result["items"]] == [False, True, False, False]
    assert [cell["delayHidden"] for cell in result["cells"]] == [
        True,
        False,
        True,
        True,
    ]
    assert result["cells"][1]["toggles"]["daily-op-delay"] is True
    assert all(item["actions"] == [] for item in result["items"])


def test_status_alias_and_unlock_time_remain_visible():
    result = _run({"delay": {"status": "Delayed", "estimated_unlock_time": "03:00"}})
    assert result["items"][1]["delay"] is True
    assert result["items"][1]["delayUntil"] == "03:00"
    assert result["cells"][1]["delayHidden"] is False


@pytest.mark.parametrize("enabled", [False, "off", 0])
def test_disabled_delay_suppresses_waiting_and_interval_marks(enabled):
    result = _run(
        {
            "delay": {"state": "waiting for solar", "enabled": enabled},
            "delay_until": _series(**{"7": "02:00", "8": "03:00", "9": "04:00"}),
        }
    )
    assert all(item["delay"] is False for item in result["items"])
    assert all(cell["delayHidden"] is True for cell in result["cells"])


@pytest.mark.parametrize(
    "delay",
    [
        {"state": "waiting for solar", "weekly_full_charge_bypasses_delay": True},
        {"state": "Skipped - full charge day"},
    ],
)
def test_weekly_bypass_suppresses_delay_and_setpoint_marks(delay):
    result = _run(
        {
            "delay": delay,
            "setpoint": {"state": "charging_to_setpoint"},
            "actual_context_mask": _series(**{"8": 1}),
            "planned_context_mask": _series(**{"9": 1}),
            "delay_until": _series(**{"8": "03:00", "9": "04:00"}),
        }
    )
    assert all(
        item["delay"] is False
        and item["delayUntil"] is None
        and item["setpoint"] is False
        for item in result["items"]
    )
    assert all(
        cell["delayHidden"] is True and cell["setpointHidden"] is True
        for cell in result["cells"]
    )


def test_interval_delay_is_preserved_without_a_top_level_delay_payload():
    result = _run({"delay_until": _series(**{"7": "02:00", "9": "04:00"})})
    assert [item["delay"] for item in result["items"]] == [True, False, True, False]
    assert result["items"][0]["delayUntil"] == "02:00"
    assert result["items"][2]["delayUntil"] == "04:00"


def test_real_current_and_forecast_keep_observed_and_planned_actions_separate():
    result = _run(
        {
            "actual_action_mask": _series(**{"7": 4, "8": 2}),
            "planned_action_mask": _series(**{"7": 1, "8": 4, "9": 1}),
            "actual_source": _series(**{"7": "observed", "8": "observed"}),
        }
    )
    assert [item["status"] for item in result["items"]] == [
        "real",
        "current",
        "forecast",
        "forecast",
    ]
    assert [item["actions"] for item in result["items"]] == [
        ["discharge"],
        ["grid"],
        ["solar"],
        [],
    ]
    assert "Observed: Battery discharge" in result["cells"][0]["aria"]
    assert "Observed: Grid charge" in result["cells"][1]["aria"]
    assert "Plan: Solar charge" in result["cells"][2]["aria"]


def test_current_forecast_never_becomes_an_observed_action():
    result = _run({"planned_action_mask": _series(**{"8": 2, "9": 2})})
    assert result["items"][1]["actions"] == []
    assert "No action" in result["cells"][1]["aria"]
    assert result["cells"][1]["className"] == "daily-op-cell daily-op-base-neutral"
    assert result["items"][2]["actions"] == ["grid"]
    assert "Plan: Grid charge" in result["cells"][2]["aria"]
