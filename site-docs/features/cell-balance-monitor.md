# Cell balance monitor

Tracks the voltage spread between the highest and lowest cell at the top of a full charge. The reading is used to show whether the battery pack is staying balanced over time and to trigger imbalance alerts when the spread becomes high.

## Why this is needed on LFP batteries

Marstek Venus batteries use LFP cells. LFP is very stable and long-lived, but its voltage curve is almost flat through most of the usable SOC range. Around the middle of the charge, two cells can have noticeably different SOC while still reporting almost the same voltage. That makes mid-SOC voltage readings poor indicators of cell balance.

The useful balance window is near the top of charge. Above roughly 3.45 V per cell, the LFP voltage curve rises much faster, so differences between cells become visible. That is also the area where the battery BMS is expected to perform passive balancing by bleeding the highest cells.

In practice, the Marstek BMS does not always balance the cells well by itself. If the pack reaches 100% quickly and then immediately returns to normal operation, weak balancing can leave one cell consistently higher than the rest. This integration therefore does two things:

- it slows the final part of a 100% charge so the BMS has time to work in the top-balance window;
- it measures imbalance only at a repeatable top-voltage point, instead of using noisy mid-SOC readings.

## The LFP charge curve in detail

LFP (LiFePO4) chemistry has a charge/discharge curve that is fundamentally different from Li-ion NMC or NCA. Understanding it is what justifies every voltage threshold this integration uses.

A typical 3.2 V nominal LFP cell behaves like this during a constant-current charge:

| SOC range | Cell voltage range | Slope |
|---|---|---|
| 0 – 10 % | 2.50 V → 3.20 V | Steep entry knee |
| 10 – 90 % | 3.20 V → 3.30 V | Almost flat — about 1 mV per % SOC |
| 90 – 97 % | 3.30 V → 3.45 V | Mild rise begins |
| 97 – 99 % | 3.45 V → 3.55 V | Knee — voltage starts climbing sharply |
| 99 – 100 % | 3.55 V → 3.65 V | Steep top knee — full-charge cliff |

That long flat plateau is the reason LFP voltage tells you almost nothing about state of charge in the middle of the curve. Two cells that look identical at 3.28 V can in reality have 5 – 10 % SOC of difference between them, which is huge.

The plateau also means **the BMS cannot do meaningful passive balancing in the middle of the curve**. Passive balancing works by bleeding current off the highest cell through a resistor. To even detect which cell is "highest", the BMS needs the spread between cells to rise above measurement noise. On the plateau, all cells read essentially the same number, so the BMS has nothing to act on.

Only when the pack enters the upper knee (above roughly 3.45 V) do cell voltages spread apart enough for the BMS to identify the leader. A 10 mV spread on the plateau might correspond to a 5 % SOC difference, but the same 10 mV spread above 3.50 V represents a tiny SOC delta — which is exactly what you want at the end of charge.

So balancing on LFP is only effective in a narrow window: roughly the last 1 – 3 % of charge, above 3.45 V. Outside that window the BMS is essentially blind to imbalance, and any time the pack spends below the knee is time the cells are *not* getting balanced.

## Availability

The cell balance monitor is always active. There is no separate configuration option because the readings are useful battery health data and do not change normal operation by themselves.

There is one integrated control that decides when the battery is taken to the top-voltage measurement window:

- **100% charge voltage taper**: per battery option. When the charge target is 100%, the integration slows the final charge and records a top-voltage balance reading.

The weekly full charge feature can temporarily set the battery max SOC to 100%. Once it does that, the same 100% charge voltage taper rules are used.

Venus A/D batteries with coupled packs are the exception to the top-voltage
stop: they still reduce charge to 200 W from 3.48 V, but reaching 3.60 V does
not stop the integration or start the 60-second measurement. The reduced
charge continues until the BMS confirms its cutoff, so the first full pack
cannot prevent the remaining coupled packs from filling.

Other Venus E/v2/v3 packs can also be stopped by their BMS just below 3.60 V.
When that cutoff is confirmed while the battery is still in the taper zone,
the cutoff itself becomes the measurement trigger; the integration stops
charging, waits 60 seconds, and records the settled cell delta.

For active recovery of a pack with a persistent imbalance, use the optional [Marstek active-balance blueprint](../blueprints.md#active-cell-balancing-for-one-marstek-battery). The blueprint is an external Home Assistant automation: it takes one battery through **Battery Manual Mode**, discovers the standard entities from the selected Omnibattery device (with manual ID overrides for renamed entities), and leaves manual ownership asserted if cleanup cannot be confirmed.

## 100% charge voltage taper

This path is used whenever the **100% charge voltage taper** option is enabled for a battery. It is voltage-driven: it engages once `max_cell_voltage` reaches the thresholds below, regardless of the configured `max_soc`. In practice that happens when:

- the user configured that battery with `max_soc = 100`, or
- the weekly full charge temporarily raised the battery to 100%, or
- a high `max_soc` below 100% still lets the cells reach 3.48 V.

The weekly full charge does not use a different balance profile. It only changes the target SOC to 100%; voltage thresholds, charge power and measurement logic are the same.

### Charge profile

```mermaid
flowchart TD
    A([100% charge voltage taper start]) --> B(Charge with configured charge limit)
    B --> C{max_cell_voltage < 3.48 V}
    C -->|Yes| A
    C -->|No| D([Limit charging])
    D --> E(Charge with 200 W)
    E --> F{max_cell_voltage < 3.60 V}
    F -->|Yes| G{BMS cutoff confirmed?}
    G -->|No| D
    G -->|Yes, Venus E/v2/v3| H([Stop charge and wait 60s])
    H --> I("Record cell_delta = (cell_Vmax - cell_Vmin) * 1000")
    G -->|Yes, Venus A/D| N([Continue at 200 W / retry path])
    F -->|No, Venus E| J([Stop charge and latch])
    J --> K(Stay stopped until charge hysteresis releases)
    J --> L(Wait 60s)
    L --> M("Record cell_delta = (cell_Vmax - cell_Vmin) * 1000")
    F -->|No, Venus A/D| N
    N --> O{BMS cutoff confirmed?}
    O -->|No| N
    O -->|Yes| P([Stop charge and wait 60s])
    P --> Q("Record cell_delta = (cell_Vmax - cell_Vmin) * 1000")
```
    
| Condition for one battery | Action |
|---|---|
| `max_cell_voltage` below 3.48 V | Normal configured charge limit |
| `max_cell_voltage` at or above 3.48 V | Limit charge to 200 W |
| Confirmed BMS cutoff below 3.60 V in the taper zone | Stop charge, wait 60 s without charging, then record the delta |
| `max_cell_voltage` reaches 3.60 V on Venus E | The configured charge hysteresis takes ownership of the stop/recharge threshold |
| `max_cell_voltage` reaches 3.60 V on Venus A/D | Keep charging at 200 W until the BMS cutoff; do not apply the integration stop |
| After the 60 s wait on Venus E | Record `delta_mV = (Vmax - Vmin) * 1000` |
| After the confirmed BMS cutoff on Venus A/D | Wait 60 s without charging, then record `delta_mV = (Vmax - Vmin) * 1000` |

Starting the taper is voltage based: SOC is deliberately not used to decide when tapering begins, because SOC can be less reliable near the top than the cell-voltage registers.

On Venus E, reaching 3.60 V lets the configured charge hysteresis prevent
recharging until its SOC threshold is crossed. If the BMS cuts first below
3.60 V, the same 60-second diagnostic starts after the debounced cutoff. The
measurement still runs as a best-effort diagnostic; if it did not finish
before weekly completion, the pending post-cutoff measurement is allowed to
finish before any completion fallback is used. Venus A/D skip this integration
hold and measurement before the BMS cutoff; once the final cutoff is confirmed,
they wait 60 seconds without charging and record the delta-V measurement once.

In a multi-battery system, this is evaluated per battery. One battery can be limited by the taper while another continues charging normally.

### SOC recalibration on a stuck top voltage (Venus E)

Some Venus E packs reach the 3.60 V top-voltage threshold while the BMS still reports a SOC well below full (for example 60–70%). That gap can mean the BMS coulomb counter has drifted, but reaching the voltage threshold does not prove that the reported SOC is wrong.

When this happens, holding at 3.60 V never gives the BMS a chance to finish its own top-of-charge sequence. So instead of pausing, the integration keeps charging at the 200 W tapered power until the BMS itself cuts off, *attempting* to make it recalibrate SOC.

This is a best-effort attempt, not a guaranteed fix. Whether a top-of-curve cutoff actually resets the reported SOC depends on the BMS firmware: some packs snap to 100% on an over-voltage cutoff, others do not. The integration only creates the conditions for a recalibration — it cannot force the BMS to apply one.

The override triggers automatically whenever **all** of these are true:

- the 100% voltage taper is active (so `max_cell_voltage` is in the top zone), and
- `max_cell_voltage` has reached the 3.60 V top-voltage threshold, and
- the BMS still reports SOC below 99%.

It is self-limiting:

- charging continues at 200 W only (the gentle taper power), not full power;
- a BMS cutoff is detected when battery power collapses to ≤ 10 W and the inverter reports Standby for 5 consecutive cycles (~10 s). If that first cutoff happened above 3.60 V while SOC is still below 100%, the battery waits for the cell to relax to 3.57 V and makes one 200 W retry; when the BMS cuts again, the override latches off permanently;
- if SOC reaches 100% during the wait or retry, no further attempt is made;
- if the SOC reads 99% or more before the first cutoff, the initial condition no longer matches, so the override does not fire;
- the latch only re-arms after the battery leaves the top zone (`max_cell_voltage` below 3.48 V), so a later full charge can recalibrate again if needed.

Reaching the 3.60 V threshold normally only happens on a 100% charge, so this rarely affects daily cycling at a lower `max_soc`. It does **not** run during the [weekly full charge](weekly-full-charge.md) — there the normal charge hysteresis is suppressed and the BMS cutoff alone ends the cycle (see that page). Venus A/D use the BMS-owned path on every tapered 100% charge, not this SOC-recalibration retry. The optional active-balance blueprint takes ownership through Battery Manual Mode, so the normal controller naturally excludes that battery while the automation is running.

!!! note "Cell imbalance"
    The override does not check the cell spread first. On a badly imbalanced pack the highest cell can hit the BMS over-voltage cutoff before the pack is full, so the recalibration is correct but balancing is left to later cycles. The BMS still protects each cell individually.

## Optional active-balance blueprint

The [Marstek active-balance blueprint](../blueprints.md#active-cell-balancing-for-one-marstek-battery) is the supported recovery path when passive balancing during normal or weekly charging is not enough. It is deliberately outside the integration's automatic control loop and must be configured once per battery.

Its default profile is: configured maximum charge power until `max_cell_voltage >= 3.49 V`, regulated charge at 95 W until 3.60 V, a 60-second rest measurement, 200 W discharge retries toward 3.49 V until `delta_V <= 0.03 V`, and a final 200 W discharge to 3.48 V. If the BMS rejects a new charge leg, the blueprint waits 10 seconds and requires three approximately-zero-power samples. When rejection still occurs inside the upper window, it first rests for 60 seconds and publishes the settled delta; it then lowers the retry target by 0.01 V, down to 3.40 V, and continues with adaptive discharge. Rejections below the upper window are not stored as formal measurements.

The automation validates every resolved entity and voltage/power relationship before writing. It sets both setpoints to 0 W before changing the force mode, temporarily writes 100% SOC, and converges every cancellation, restart or error through the same cleanup. It restores the configured SOC maximum and turns Battery Manual Mode off only after idle and SOC writes are confirmed; otherwise the switch remains ON as a safety hold.

## Why these voltage thresholds

Every voltage cutoff used by the 100 % taper and the optional active-balance blueprint was picked against the LFP curve described above. None of these numbers are arbitrary.

| Threshold | Where it is used | Why this value |
|---|---|---|
| **3.45 V** | Reference for the start of the upper knee | This is roughly where the LFP curve leaves the plateau. Below this, balancing decisions cannot be trusted because cell voltages are too close together to distinguish. |
| **3.48 V** | Trigger for tapering normal charge to 200 W | A little above the knee. The small margin confirms the pack is genuinely in the balance window — and not just on a brief voltage bounce caused by a load step — before reducing power. |
| **3.49 V** | Blueprint discharge floor between retries; switch-over from coarse to regulated charge | Sits just inside the balance window. Stopping the discharge here keeps the pack in the zone where the BMS can still see and bleed the high cell. Going lower would push the pack off the knee and waste the time already spent balancing. |
| **3.60 V** | Top measurement point; stop charge and wait 60 s before reading the delta | High enough to let supported BMS firmware reach its native top-charge behaviour while retaining about 50 mV of nominal headroom below the usual 3.65 V LFP ceiling. The battery BMS remains the final cutoff and may stop charge earlier. |
| **3.48 V (again)** | End-of-cycle discharge floor — the 200 W final discharge in the blueprint stops here | The same threshold used to enter the taper is reused to leave the balance window. Stopping at 3.48 V brings the pack just off the upper knee without dropping it back onto the deep plateau. Sitting at 3.55 – 3.60 V for long periods accelerates calendar ageing, so the automation deliberately bleeds the pack down to the lower edge of the window before releasing control. |
| **3.40 V** | Lower bound for the blueprint retry voltage when charge rejection is detected | The automation gives each new charge leg 10 s to engage and, if charge power has not yet been observed, then requires 3 consecutive ~0 W cycles before declaring rejection. It drops the retry voltage by 0.01 V, but never below 3.40 V. Going further down exits the balance window entirely and forces a long, wasteful re-climb up the curve. |
| **0.03 V (30 mV)** | Blueprint completion threshold | Considered "balanced enough" for an LFP pack at the top of the knee. Pushing for tighter values (10 mV or less) is rarely productive because passive balancing currents are tiny — see the next section. |
| **0.05 V (50 mV)** | Green / yellow status boundary | A pack reading below 50 mV at the top is considered healthy. This is more conservative than typical LFP vendor specs (often 80 – 100 mV) because the measurement is taken in the balance window, where differences between cells are exaggerated. |

The normal taper uses 200 W so the cell voltage remains excited enough to advance through the top zone without returning to full power. The optional blueprint uses a gentler 95 W charge leg. Measurements are always taken at **rest** after charge and discharge stop for 60 seconds, so neither charge power contaminates the recorded delta.

## Why this takes so long

Cell balancing is **not** a fast process — and Marstek Venus packs are no exception. There are two reasons.

**1. Passive balancing current is small.** A typical LFP BMS bleeds the highest cell through a balance resistor at somewhere between 30 mA and 150 mA. The Marstek Venus packs sit at the low end of that range. For a 100 Ah cell, a 50 mA bleed removes only about 0.05 % SOC per hour from the high cell. Equalising even small SOC differences between cells therefore requires many hours of continuous time in the balance window.

**2. The balance window itself is narrow.** The BMS can only bleed when the pack is above ~3.45 V *and* the highest cell is detectably above the rest. As soon as charging stops or the pack drops back below the knee, balancing stops. A normal charge cycle that hits 100 % and immediately returns to discharge spends only minutes in the useful window — far too little for any visible effect.

The practical consequence:

> **Reducing the top-of-charge cell delta by roughly 5 mV typically takes around 24 hours of cumulative time at the top of the balance window.**

That figure is consistent both with the bleed-current arithmetic above and with observations on real Venus packs. Bigger imbalances (50 mV or more) can take **multiple days** of repeated top-balance sessions before the delta starts dropping consistently. Packs that have been left chronically unbalanced for months may take a week or more to recover.

This is also why the active-balance blueprint does not have a "fast" path:

- the 95 W charge cap above 3.48 V is set so the pack stays in the knee long enough for the BMS to make progress, rather than ramming through it in seconds;
- the 200 W discharge between retries brings the pack back down to the retry voltage without dropping out of the window;
- the automation is allowed to run indefinitely, because anything short of "many hours" is unlikely to move the needle.

If the goal is to restore a noticeably unbalanced pack, import the blueprint, create one automation for that battery and **leave it running overnight (or longer) before checking the result**. Watching the cell delta in real time and expecting movement within minutes will only cause frustration.

## How imbalance is measured

The only reading that feeds the balance status, alerts and trend is the explicit top-window measurement:

1. the battery enters the taper zone at `max_cell_voltage >= 3.48 V`;
2. it either reaches `max_cell_voltage >= 3.60 V`, or the BMS cutoff is confirmed while the cell remains below that point;
3. charge is stopped;
4. the integration waits 60 seconds;
5. it records the spread between `max_cell_voltage` and `min_cell_voltage`.

Older OCV-style readings, opportunistic readings and long passive-hold readings are no longer used. Measuring after a settled top-voltage or BMS-cutoff event keeps readings comparable while supporting packs whose BMS stops just below 3.60 V.

## Thresholds

| Status | Delta range | Meaning |
|---|---|---|
| Green | < 50 mV | Good balance |
| Yellow | 50-99 mV | Minor imbalance; monitor over time |
| Orange | 100-149 mV | Moderate imbalance |
| Red | >= 150 mV | High imbalance |

Thresholds are fixed and apply equally to all supported LFP packs.

## Notifications

The integration sends Home Assistant persistent notifications for these events:

| Event | Notification title |
|---|---|
| Orange or red top-voltage reading | Cell imbalance - `{battery name}` |
| Red on 2 or more consecutive full charges | Possible degraded cell - `{battery name}` |
| Rising trend with average above 75 mV | Rising imbalance trend - `{battery name}` |

## Sensor entities

Five sensor entities are created per battery when the feature is enabled:

| Entity | Description | Unit |
|---|---|---|
| `sensor.*_cell_delta` | Voltage spread between max and min cell | mV |
| `sensor.*_balance_status` | Balance result: `green` / `yellow` / `orange` / `red` | - |
| `sensor.*_delta_trend` | Trend over recent readings: `rising` / `stable` / `falling` | - |
| `sensor.*_last_balance_read` | Timestamp of the last reading | timestamp |
| `sensor.*_delta_avg_4w` | Rolling average of the last 4 readings | mV |

Values are restored from persistent storage after a Home Assistant restart so sensors show the last known state immediately on startup.

## Diagnostics

The **Integration Status** sensor exposes a `normal_balance_protection` attribute with per-battery details:

| Attribute | Meaning |
|---|---|
| `enabled` | Whether 100% voltage taper is enabled for that battery |
| `in_zone` | Whether `max_cell_voltage` is in the top-balance window |
| `max_cell_voltage` / `min_cell_voltage` | Current cell voltage extremes |
| `delta_V` | Current voltage spread in volts |
| `voltage_taper_latched` | Whether the 200 W normal-charge taper is currently active |
| `bms_cutoff_charge_active` | Whether Venus A/D are being kept charge-eligible until their BMS cutoff |
| `bms_cutoff_measurement` | Post-cutoff measurement state after a confirmed BMS cutoff: `pending` or `done` |
| `soc_recal_active` | Whether the charge is being kept past 3.60 V to attempt recalibration of a low reported SOC |
| `soc_recal_bms_cutoff` | Whether the BMS cutoff has been reached during recalibration (override latched off) |
| `soc_recal_retry_pending` | Whether the battery is waiting for 3.57 V before the one-shot retry |
| `soc_recal_retry_active` | Whether the one-shot 200 W retry is in progress |
| `soc_recal_first_cutoff_voltage` | Highest voltage observed during the first BMS cutoff |
| `charge_limit_w` | Effective per-battery charge limit before allocation |

The blueprint's phase, retry voltage and cleanup result are reported in its persistent notifications; they are not integration status attributes. Each settled rest measurement is also recorded in the existing `Cell Delta` history with `source: blueprint`, using the integration's own coordinator telemetry.

!!! info
    Cell voltage registers (`max_cell_voltage`, `min_cell_voltage`) are read from all supported battery versions (v2, v3, vA, vD).
