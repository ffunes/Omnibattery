# Predictive charging — Dynamic Pricing mode

Automatically selects the **cheapest hours of the day** to cover the calculated energy deficit.

## Compatible price integrations

- **Nord Pool** — both the official Home Assistant integration and the HACS integration
- **PVPC** (ESIOS REE, Spain)
- **CKW** (Switzerland)
- **EPEX Spot** (e.g. aWATTar)
- **ENTSO-e** (Transparency Platform)
- **Tibber** — no price sensor needed; the engine polls the `tibber.get_prices` service directly (see below)

!!! note "Tibber needs no sensor"
    Selecting **Tibber** as the price integration leaves the *Electricity price sensor* field unused — the engine calls the `tibber.get_prices` service (today's prices, plus tomorrow's after ~13:00), caches the slots and refreshes hourly. The official Tibber integration must be configured in HA.

!!! note "Official Nord Pool and HACS are selected the same way"
    Select **Nordpool** and choose a price entity from the provider. A HACS sensor continues to be read from its `raw_today` / `raw_tomorrow` attributes. If that sensor has `price_in_cents: true`, Omnibattery automatically converts its slots and current price to major currency/kWh, so thresholds must still be entered in €/kWh (or the corresponding major currency), not cents. For an entity from Home Assistant's official Nord Pool integration, Omnibattery automatically resolves its market area, calls `nordpool.get_prices_for_date` for today, converts the returned currency/MWh values to currency/kWh, and refreshes the cache hourly. No separate provider option or template sensor is needed.

## Configuration

| Field | Description |
|---|---|
| **Price integration type** | Nordpool / PVPC / CKW / EPEX Spot / ENTSO-e / Tibber |
| **Electricity price sensor** | HA price entity. For Nord Pool, select either an official-integration entity or the existing HACS sensor; unused for Tibber |
| **Max price threshold (€)** | (Optional) Price ceiling; does not charge even during "cheap" hours if the price exceeds this value. Also used as the discharge threshold when price-based discharge control is enabled |
| **Only discharge when price is above threshold** | (Optional) Price-gated discharge — see below |
| **Discharge price floor (€)** | (Optional) Separate floor for price-gated discharge — opens an idle band between the charge ceiling and this floor. Empty = reuse the max price threshold for both. See [Separate discharge price floor](#separate-discharge-price-floor) |
| **Solar forecast safety margin (kWh)** | (Optional) Extra energy buffer added to consumption forecast before deciding whether to charge (default 0 kWh) |
| **Predictive grid charge margin (%)** | (Optional) Tops up the grid-charge amount to hedge optimistic solar forecasts — e.g. a 2 kWh grid need at 50 % charges 3 kWh. Capped at the gap to max SOC (default 0 %) |
| **Negative-price opportunistic charging** | (Optional, default off) Charge in qualifying negative import-price slots even when the normal forecast has no deficit |

![Configuration form — Dynamic Pricing mode](../../assets/screenshots/configuration/predictive-charging/dynamic-pricing-form.png){ width="650"  style="display: block; margin: 0 auto;"}

## Daily evaluation (00:05)

At 00:05 the controller:

1. Calculates the energy deficit and projects consumption, solar and usable battery energy in 15-minute intervals until midnight.
2. Fetches today's hourly prices from the configured integration.
3. Detects when cumulative energy would reach the minimum SOC and reserves the cheapest eligible slots that can deliver each requirement before its deadline.
4. Calculates and stores the **daily average price** from the hourly price profile.
5. Assigns an energy quota to each slot; only energy without an early deadline remains freely optimized across the day.

“Cheapest” therefore means cheapest among slots able to meet a requirement in time. A later slot never counts as coverage for energy already needed earlier. A partial or impossible plan remains executable, but reports the kWh shortfall and whether price filtering or physical slot capacity caused it. Quotas are targets rather than guarantees: contracted power, battery headroom, phase limits, temperature, ownership and other runtime protections remain authoritative.

### Retry logic

If price data is unavailable at 00:05, the system retries every 15 minutes for the first hour.

### HA restart mid-day

If HA restarts after the 00:05 window without a prior evaluation, the controller runs an automatic evaluation at startup (after 15 seconds). It considers the remaining slots of the current day and, when the provider has already published them, the next **12 hours** so a restart does not leave the next overnight window without a plan.

## Automatic re-evaluation during the day

The 00:05 plan is not immutable. Dynamic Pricing adapts it as the real day develops:

- **One hour before each selected future slot**, the energy balance is checked again. A slot is silently skipped when the battery and expected solar now cover the need. If a deficit remains, a persistent notification confirms that the slot will be used. Back-to-back slots are not re-evaluated while the previous slot is still charging.
- **Late afternoon / evening**, the controller performs one additional recharge assessment. When solar start was detected, it runs approximately **1.5 hours before the estimated end of production**; if no start was detected, it uses a safe fallback at **16:00**. It projects the remaining household consumption until midnight, subtracts usable battery energy and remaining solar, and adds only the cheap future slots needed to cover a material deficit (at least **0.3 kWh**). This is a safety top-up, so it is not blocked by the optional arbitrage-margin gate.
- **After a 30-point SOC drop**, it performs the same late-day deficit assessment immediately instead of waiting for the evening trigger. The comparison is against the average battery SOC recorded at the last Dynamic Pricing evaluation; only drops of at least 30 percentage points trigger it, the reference is reset after reevaluation, and an SOC rise never triggers it.

These reevaluations keep existing charge limits, SOC floors, time-slot ownership, manual mode, backup and availability protections authoritative. The daily reference and once-per-day evening guard reset at midnight.

### Re-evaluate Dynamic Pricing button

When Dynamic Pricing is enabled, the system device exposes **Re-evaluate Dynamic Pricing** (`button.*_reevaluate_dynamic_pricing`) in the dashboard and in Home Assistant. Pressing it immediately rebuilds the schedule with the latest price and solar data, using the same extended horizon as the startup catch-up path (end of today or **now + 12 hours**, whichever is later).

This button is useful after changing a price threshold, forecast or runtime option. It is deliberately not a full multi-day planner: pressing it in the afternoon does not reserve tomorrow afternoon's energy against today's deficit. Tomorrow's normal plan is built at 00:05 once that day's balance is known.

---

## Negative-price opportunistic charging

This **opt-in Dynamic Pricing feature** is intended for installations with or without solar. When enabled, Omnibattery independently finds hourly or 15-minute slots whose normalized **import price is negative**. It calculates the battery energy needed to reach each battery's configured maximum SOC and selects the most-negative individual slots first. A solar forecast sensor is not required.

The calendar records why each interval was selected: `deficit`, `negative_price`, or `combined`. A positive-price deficit slot therefore keeps the normal deficit-based SOC target; it cannot consume energy that is pending only for an opportunity. In a qualifying combined slot, the higher of the deficit and opportunistic targets applies. Each battery uses its own configured maximum SOC as the opportunistic ceiling.

Charging stops as soon as the battery's configured maximum SOC is reached, and remaining opportunity-only slots are removed. A pure opportunity also stops if the live price becomes unavailable or is no longer negative. Contracted power, per-battery and system charge limits, user blockers, manual ownership, backup, availability and all existing safety controls remain authoritative.

The negative import-price condition is deliberately separate from the **Negative injection threshold** below. The former detects when importing energy is attractive; the latter identifies solar anti-curtailment risk. Outside a solar-risk window, a negative-price slot can charge toward the configured maximum SOC as before. Inside a risk window it is not rejected automatically: it can use only the headroom left after the solar reserve:

```
opportunistic space = current free space − remaining solar reserve
```

The opportunity never consumes the solar reserve. If actual solar is lower than forecast, the remaining reserve falls progressively and more grid charging becomes available; if actual solar is higher, the opportunity is reduced or stopped. Contracted power, SOC limits, minimum reserves, manual ownership and all other safety blockers still apply. A charge required to guarantee minimum SOC remains the safety exception. Missing solar data puts the anti-curtailment planner in fail-safe mode but does not cancel an otherwise valid import-price opportunity.

The runtime switch is available in the Omnibattery System controls, so automations can enable the feature without reopening the options flow.

---

## Smart Pre-discharge / Anti-curtailment

This is an **opt-in subfunction of Dynamic Pricing**. It does not control a PV inverter. When enabled, Omnibattery reuses the normalized 15-minute or hourly price slots and the existing solar model to find future slots where:

- the price is at or below **Negative injection threshold** (default `0 €/kWh`), and
- forecast solar surplus would exceed household consumption.

The planner first calculates the headroom needed to absorb the forecast solar surplus. Before the first risk window it selects the most valuable (highest-price) eligible blocks for pre-discharge, stopping at the configured SOC floors, reserves, power limits and existing blockers. The same **Solar forecast safety margin** is used by predictive charging when deciding whether the solar forecast is sufficient. Slots are grouped into approximately one-hour blocks to avoid chatter. Consumption is distributed uniformly from the existing daily-history estimate when no more detailed model is available.

The live controls are available only when Predictive Grid Charging uses Dynamic Pricing:

| Control | Meaning |
|---|---|
| **Smart Pre-discharge** | Runtime opt-in switch; default off |
| **Negative injection threshold** | Inclusive price threshold for a risk slot |
| **Pre-discharge reserve SOC** | Additional SOC floor; `0` uses existing floors |
| **Pre-discharge export mode** | **Self-consumption only**, **Automatic**, or **Custom limit** |
| **Custom deliberate-export limit (W)** | Shown for **Custom limit**; caps deliberate export to the grid during pre-discharge. This is an export limit, not total battery discharge power |
| **Solar forecast safety margin** | Extra buffer in kWh used by predictive charging and anti-curtailment |

The three export modes are:

- **Self-consumption only**: no deliberate grid export; equivalent to `0 W`.
- **Automatic**: calculates only the export power needed to create the required headroom; it does not always use the maximum available discharge power.
- **Custom limit**: deliberately exports up to the configured W limit. The value describes deliberate grid export, not total battery discharge power.

Existing configurations remain compatible: legacy `0` maps to **Self-consumption only**, while a positive legacy value maps to **Custom limit**. During a risk window, the controller clamps the net grid target to zero: the battery may cover domestic consumption, but it will not deliberately export to the grid. The feature never bypasses minimum or guaranteed-minimum SOC, user time-slot ownership, manual control, backup mode, unavailable/non-responsive batteries, or capacity protection. Missing prices, forecast, SOC, capacity or a valid grid meter are fail-safe conditions: any smart override and blocker are cleared. The plan is rebuilt after restart, at the normal daily evaluations, when the feature is enabled, after a material change in available battery headroom, and by the existing **Re-evaluate Dynamic Pricing** button. Parameter changes invalidate the old plan; use that button to apply them immediately instead of waiting for the next evaluation. Plans are not persisted.

The single binary sensor for this feature, `curtailment_status`, reports the current state, reason, next risk window, risk slots, required/current headroom, planned discharge, shortfall, per-battery targets, selected discharge slots and active export target. It also exposes automation-oriented attributes:

- `protected_window_active`: the negative-injection window is active.
- `headroom_deficit_kwh`: headroom still missing to absorb the forecast.
- `inverter_curtailment_required`: `true` only when the protected window is active and headroom is missing; `false` when a valid plan needs no inverter limit; `null` while the plan is fail-safe or unavailable.
- The downloaded diagnostics include `solar_reserve_remaining_kwh`, `current_free_space_kwh`, and `opportunistic_space_available_kwh`. The latter is never negative and follows `current free space − remaining solar reserve`.
- `charge_limit_reason` and `charge_limit_reasons` identify why opportunistic grid charging is limited, including active charge blockers and exhaustion of the solar reserve. The `export` diagnostic reports the selected mode and, when present, the deliberate-export limit in W.

`active_export_target_w` is the battery's pre-discharge export target, not a universal PV-inverter command. An automation should apply an inverter-specific limit and restore normal operation only after the status no longer requires curtailment.

---

## Price-aware solar surplus absorption

This is an **opt-in subfunction of Dynamic Pricing**. Storing PV surplus is not free: on a dynamic contract the energy that goes into the battery forfeits that slot's feed-in revenue, and feed-in prices swing widely across the day. Absorbing the morning surplus at a high feed-in price and exporting the midday surplus at a low one is the expensive way round.

When enabled, Omnibattery plans *when* to take the day's charge instead of absorbing whatever appears:

1. It derives the energy the battery still needs today from the remaining household consumption, the energy already stored above the floor, the **Solar forecast safety margin** and the space left in the battery.
2. It distributes the remaining solar forecast and consumption over the future price slots, capped by the batteries' charge power, to estimate the surplus each slot can offer.
3. It selects the **cheapest export hours** that together cover the target, accumulating energy rather than hours, so a cheap sunrise slot with no surplus is not mistaken for a cheap midday slot with several kWh.
4. Outside those windows it registers the `surplus_price_hold` charge blocker. No battery is then available in the charge direction, the PD command clamps to `0 W`, and the surplus flows to the grid. Discharge is untouched, so self-consumption from the battery continues.

The hold releases whenever holding could cost energy rather than save money:

| Release reason | Meaning |
|---|---|
| `absorption_window` | The current slot is one of the selected cheap windows |
| `past_deadline` | Solar production is expected to have ended |
| `target_met` | The battery already covers the rest of the day |
| `shortfall_risk` | The remaining cheap windows can no longer cover the target |
| `no_material_saving` | The best remaining slot is not cheaper than the **Surplus hold minimum saving** |

The target is recalculated from live SOC every control cycle, so a cheap window that under-delivers raises the target while the remaining windows shrink; the hold then drops for the rest of the day rather than ending short. The plan itself is rebuilt every few minutes and at the normal daily, pre-slot and evening re-evaluations. Plans are not persisted.

Missing prices, a missing solar forecast, a deadline in the past, unusable SOC or capacity, and non-finite values are all fail-safe conditions that release the hold. So are the features that own charging in their own right: charge delay, a scheduled cheap grid slot, negative-price opportunistic charging, smart pre-discharge, the weekly full charge, peak shaving, EV pause, manual mode or manual time-slot ownership, and any battery sitting on its SOC floor.

| Control | Meaning |
|---|---|
| **Price-aware solar surplus absorption** | Opt-in; default off |
| **Surplus hold minimum saving** | How much cheaper the best remaining slot must be before surplus is held back. Prevents toggling on negligible differences |
| **Export/feed-in price sensor** | Optional. Leave empty to use the import price curve, which is correct when export is credited at the import price |
| **Export price integration** | Attribute layout of that sensor. Leave empty to reuse the import integration. Tibber is unavailable here: it is service-based and its cache belongs to the import curve |

The binary sensor `surplus_price_hold_status` reports whether surplus is being held back, the reason, the day's target, the energy the remaining selected windows can still absorb, the deadline, the next release time, the selected slots with their export prices, and which curve the prices came from. The **Integration Status** sensor reports `surplus_price_hold` while the hold is active.

The export price sensor is read through the same parsers as the import sensor, but it is deliberately isolated from the import health check: a flaky export sensor never raises the import-price repair issue. The **Negative injection threshold** used by smart pre-discharge continues to read the import curve.

---

## Price-based discharge control

The **"Only discharge when price is above threshold"** option adds an extra condition to discharge behaviour.

When active, **every controller cycle (event-driven)** checks whether the current price allows discharge:

```
If current_price > threshold:
    → Discharge allowed (PD controller operates normally)
If current_price <= threshold:
    → Discharge BLOCKED (battery holds)
```

The threshold is resolved as follows:

1. If **Max price threshold** is configured, that value is used.
2. If **Max price threshold** is empty, the daily average price is used.

The daily average price is calculated automatically during the 00:05 evaluation from the hourly price profile. The goal is to preserve battery energy for the most expensive hours of the day. If no fixed threshold is configured and the daily average is not available yet, discharge control does not act.

### Separate discharge price floor

By default a single threshold gates both ends: the battery grid-charges only **below** the max price threshold and discharges only **above** it. The optional **Discharge price floor** decouples the two by setting a lower discharge floor, opening an **idle band** between them:

```
price ≥ max price threshold     → discharge allowed
discharge floor < price < ceiling → idle (no grid charge, no discharge)
price ≤ discharge price floor    → discharge BLOCKED
```

In the idle band the battery neither grid-charges nor discharges — but **solar-surplus charging still works**. This avoids cycling the battery for the marginal price difference around the average. The floor must be **at or above** the charge ceiling (it is validated on save); leave it empty to reuse the max price threshold for both (the single-threshold behaviour above).

Both thresholds are also exposed as live `number` entities (**Max Price Threshold** and **Discharge Price Floor**) so automations can rewrite them without entering the options flow.

### Minimum arbitrage margin

A fixed charge ceiling answers "is this price low?" but not "is it low *enough*". Those come apart in winter, when a flat price curve can sit entirely below the ceiling while offering no spread to trade against. Charging then runs the battery through a cycle that the round-trip losses eat.

The optional **Minimum Arbitrage Margin** makes the ceiling move with the day instead. At each evaluation the engine takes the most expensive hours still ahead, as many as it plans to charge for, and requires:

```
expected_discharge_price × round_trip_efficiency − slot_price ≥ margin
```

Slots that fail are dropped. If none survive, the day is skipped entirely.

The margin is **empty by default**, which leaves slot selection exactly as it was. Setting it back to `0` disables it again. When set, it applies *on top of* the max price threshold, and whichever ceiling is stricter wins.

The gate runs on the 00:05 evaluation only. The evening recharge after a poor solar day is a deficit-driven safety top-up rather than an arbitrage trade, and by then the remaining horizon holds no expensive hours to price against, so applying the gate there would block every recharge it exists to perform.

**Round-Trip Efficiency** (default `0.85`) is the AC-to-AC ratio used to value a stored kWh. Lower values tighten the gate. Note this is the *marginal* ratio (extra kWh out per extra kWh in), not the gross figure you get by dividing lifetime discharge by lifetime charge, which also carries standby drain. Standby is paid whether or not you cycle, so folding it in here would refuse profitable charges.

Both are exposed as live `number` entities, and the evaluation notification reports the resulting ceiling so a skipped night is traceable.

### Interaction with time slots

If time slots are configured to restrict discharge, **both conditions must be met** for the battery to discharge:

```
Discharge allowed = within_discharge_time_slot AND current_price > threshold
```

Outside a slot that allows discharge, the battery never discharges. Inside one, it only discharges when the price is high enough.

### Effect on the PD controller

When discharge is blocked by price, the controller completely freezes its state (power to 0, no derivative term update), the same as during a time slot restriction. The battery resumes smoothly as soon as the price exceeds the active threshold again.

---

## Diagnostic attributes

The `predictive_charging_active` binary sensor exposes:

| Attribute | Description |
|---|---|
| `charging_needed` | Whether charging is needed according to the balance |
| `selected_hours` | Selected hours with individual prices |
| `average_price` | Average price of the selected hours |
| `estimated_cost` | Estimated charging cost |
| `evaluation_timestamp` | When the last evaluation was performed |
| `price_data_status` | Price sensor status (`ok (N slots)`, `sensor_unavailable`, `no_slots`, `not_evaluated`) |
| `chronological_planning_active` | Whether deadline-aware planning produced the active schedule |
| `chronological_source` / `solar_timeline_source` | Consumption and solar curve sources |
| `earliest_projected_depletion` | First projected minimum-SOC crossing without grid charge |
| `deadline_required_kwh` / `flexible_required_kwh` | Energy reserved before deadlines and energy optimized freely by price |
| `deadline_shortfall_kwh` / `total_shortfall_kwh` | Urgent and total energy that eligible slots cannot deliver |
| `energy_deadlines` | Cumulative energy requirements and local ISO deadlines |
| `slot_energy_targets_kwh` / `slot_deadlines` | Per-slot quotas and their deadlines, serialized with local timestamps |

![Diagnostic attributes of predictive_charging_active](../../assets/screenshots/configuration/predictive-charging/diagnostic-attributes.png){ width="650"  style="display: block; margin: 0 auto;"}

The dynamic calendar consumes the same dated solar timeline as Time Slot. A
provider curve has priority over a mature learned profile, and an invalid
candidate falls back atomically to the next source. The learned profile is
applied automatically once mature; until then the sinusoidal curve is used.
