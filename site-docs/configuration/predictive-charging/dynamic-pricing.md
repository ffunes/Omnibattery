# Predictive charging — Dynamic Pricing mode

Automatically selects the **cheapest hours of the day** to cover the calculated energy deficit.

## Compatible price integrations

- **Nordpool**
- **PVPC** (ESIOS REE, Spain)
- **CKW** (Switzerland)
- **EPEX Spot** (e.g. aWATTar)
- **ENTSO-e** (Transparency Platform)
- **Tibber** — no price sensor needed; the engine polls the `tibber.get_prices` service directly (see below)

!!! note "Tibber needs no sensor"
    Selecting **Tibber** as the price integration leaves the *Electricity price sensor* field unused — the engine calls the `tibber.get_prices` service (today's prices, plus tomorrow's after ~13:00), caches the slots and refreshes hourly. The official Tibber integration must be configured in HA.

## Configuration

| Field | Description |
|---|---|
| **Price integration type** | Nordpool / PVPC / CKW / EPEX Spot / ENTSO-e |
| **Electricity price sensor** | HA entity with the current price (and hourly forecast attributes) |
| **Max price threshold (€)** | (Optional) Price ceiling; does not charge even during "cheap" hours if the price exceeds this value. Also used as the discharge threshold when price-based discharge control is enabled |
| **Only discharge when price is above threshold** | (Optional) Price-gated discharge — see below |
| **Discharge price floor (€)** | (Optional) Separate floor for price-gated discharge — opens an idle band between the charge ceiling and this floor. Empty = reuse the max price threshold for both. See [Separate discharge price floor](#separate-discharge-price-floor) |
| **Solar forecast safety margin (kWh)** | (Optional) Extra energy buffer added to consumption forecast before deciding whether to charge (default 0 kWh) |
| **Predictive grid charge margin (%)** | (Optional) Tops up the grid-charge amount to hedge optimistic solar forecasts — e.g. a 2 kWh grid need at 50 % charges 3 kWh. Capped at the gap to max SOC (default 0 %) |

![Configuration form — Dynamic Pricing mode](../../assets/screenshots/configuration/predictive-charging/dynamic-pricing-form.png){ width="650"  style="display: block; margin: 0 auto;"}

## Daily evaluation (00:05)

At 00:05 the controller:

1. Calculates the energy deficit (battery + solar vs. expected consumption).
2. Fetches today's hourly prices from the configured integration.
3. Selects the cheapest hours needed to cover the deficit.
4. Calculates and stores the **daily average price** from the hourly price profile.
5. Schedules the charging slots for the day.

### Retry logic

If price data is unavailable at 00:05, the system retries every 15 minutes for the first hour.

### HA restart mid-day

If HA restarts after the 00:05 window without a prior evaluation, the controller runs an automatic evaluation at startup (after 15 seconds) considering only the remaining slots of the current day.

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

If discharge time slots are configured, **both conditions must be met** for the battery to discharge:

```
Discharge allowed = within_time_slot AND current_price > threshold
```

Outside the slot the battery never discharges. Inside the slot, it only discharges when the price is high enough.

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

![Diagnostic attributes of predictive_charging_active](../../assets/screenshots/configuration/predictive-charging/diagnostic-attributes.png){ width="650"  style="display: block; margin: 0 auto;"}
