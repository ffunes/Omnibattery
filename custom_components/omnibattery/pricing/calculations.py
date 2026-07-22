"""Pure price parsing and slot-selection helpers.

Extracted verbatim from ``ChargeDischargeController`` (module-8 PR1). These are
side-effect-free functions: price-sensor attribute parsers (one per supported
integration) plus the cheap-slot selection / charging-hours math. The controller
keeps thin delegating wrappers so its internal call sites are unchanged.
"""
import logging
import math
from datetime import datetime, timedelta

from . import PriceSlot
from ..const import CHARGE_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY

_LOGGER = logging.getLogger(__name__)


def normalize_nordpool_hacs_price(value, attrs: dict) -> float:
    """Return a HACS Nordpool price in major currency per kWh.

    HACS applies its ``price_in_cents`` and ``unit`` configuration to the
    sensor state and to every ``raw_today`` / ``raw_tomorrow`` value. Internal
    Omnibattery thresholds use major currency/kWh, so undo those display-scale
    transformations at the provider boundary.
    """
    price = float(value)
    if attrs.get("price_in_cents") is True:
        price /= 100.0

    energy_unit = str(attrs.get("unit") or "kWh").lower()
    if energy_unit == "mwh":
        price /= 1000.0
    elif energy_unit == "wh":
        price *= 1000.0
    return price


def parse_nordpool_prices(attrs: dict) -> list:
    """Parse Nordpool / Energi Data Service price attributes.

    Expected format in raw_today / raw_tomorrow:
        [{"start": datetime, "end": datetime, "value": float}, ...]
    HACS display scaling (cents and MWh/Wh) is normalized to major
    currency/kWh. Returns list[PriceSlot] in local time.
    """
    from homeassistant.util import dt as dt_util

    slots = []
    for key in ("raw_today", "raw_tomorrow"):
        entries = attrs.get(key) or []
        for entry in entries:
            try:
                start = entry.get("start")
                end = entry.get("end")
                value = entry.get("value")
                if start is None or end is None or value is None:
                    continue
                # Convert to local datetime if timezone-aware
                if hasattr(start, "tzinfo") and start.tzinfo is not None:
                    start = dt_util.as_local(start).replace(tzinfo=None)
                if hasattr(end, "tzinfo") and end.tzinfo is not None:
                    end = dt_util.as_local(end).replace(tzinfo=None)
                slots.append(
                    PriceSlot(
                        start=start,
                        end=end,
                        price=normalize_nordpool_hacs_price(value, attrs),
                    )
                )
            except Exception as exc:
                _LOGGER.debug("Dynamic pricing: failed to parse Nordpool entry %s: %s", entry, exc)
    return slots


def parse_nordpool_service_prices(response: dict, area: str | None = None) -> list:
    """Parse ``nordpool.get_prices_for_date`` response data.

    The official Home Assistant integration returns one list per market area:
        {"SE3": [{"start": ISO8601, "end": ISO8601, "price": 320.5}, ...]}
    Service prices use currency/MWh, so they are divided by 1000 to match the
    official current-price sensor and Omnibattery thresholds (currency/kWh).
    """
    from datetime import datetime as _dt

    from homeassistant.util import dt as dt_util

    if not response:
        return []

    selected_area = area if area in response else next(iter(response), None)
    if selected_area is None:
        return []
    if area is not None and selected_area != area:
        _LOGGER.warning(
            "Dynamic pricing: Nord Pool response did not contain area %s; using %s",
            area,
            selected_area,
        )

    slots = []
    for entry in response.get(selected_area) or []:
        try:
            start = entry.get("start")
            end = entry.get("end")
            price_val = entry.get("price")
            if start is None or end is None or price_val is None:
                continue
            if isinstance(start, str):
                start = _dt.fromisoformat(start)
            if isinstance(end, str):
                end = _dt.fromisoformat(end)
            if hasattr(start, "tzinfo") and start.tzinfo is not None:
                start = dt_util.as_local(start).replace(tzinfo=None)
            if hasattr(end, "tzinfo") and end.tzinfo is not None:
                end = dt_util.as_local(end).replace(tzinfo=None)
            slots.append(
                PriceSlot(start=start, end=end, price=float(price_val) / 1000.0)
            )
        except Exception as exc:
            _LOGGER.debug(
                "Dynamic pricing: failed to parse official Nord Pool entry %s: %s",
                entry,
                exc,
            )
    return slots


def parse_pvpc_prices(attrs: dict) -> list:
    """Parse PVPC (ESIOS REE, Spain) price attributes.

    Expected format: "price_00h", "price_01h", ..., "price_23h" (float, €/kWh).
    PVPC publishes next-day prices around 20:00; at 00:05 the attributes
    reflect the current day's prices (already in effect).
    Returns list[PriceSlot] for today in local time.
    """
    from datetime import date as _date, time as _time

    slots = []
    target_date = _date.today()
    for hour in range(24):
        attr_name = f"price_{hour:02d}h"
        price_val = attrs.get(attr_name)
        if price_val is None:
            continue
        try:
            price = float(price_val)
        except (ValueError, TypeError):
            _LOGGER.debug("Dynamic pricing: failed to parse PVPC attribute %s=%s", attr_name, price_val)
            continue
        start = datetime.combine(target_date, _time(hour=hour, minute=0))
        end = start + timedelta(hours=1)
        slots.append(PriceSlot(start=start, end=end, price=price))
    return slots


def parse_ckw_prices(attrs: dict) -> list:
    """Parse CKW (Switzerland) price attributes.

    Expected format in 'prices':
        [{"start": "2026-03-27T00:00+01:00", "end": "2026-03-27T00:15+01:00", "price": 0.2402}, ...]
    96 slots per day (15-minute intervals). Prices in CHF/kWh.
    Returns list[PriceSlot] in local time.
    """
    from homeassistant.util import dt as dt_util
    from datetime import datetime as _dt

    slots = []
    entries = attrs.get("prices") or []
    for entry in entries:
        try:
            start = entry.get("start")
            end = entry.get("end")
            price_val = entry.get("price")
            if price_val is None:
                # Some CKW-derived sensors expose the total price under a
                # generic/value-style key. Values are expected in CHF/kWh.
                price_val = entry.get("value", entry.get("integrated"))
            if start is None or end is None or price_val is None:
                continue
            # Parse ISO 8601 string timestamps if needed
            if isinstance(start, str):
                start = _dt.fromisoformat(start)
            if isinstance(end, str):
                end = _dt.fromisoformat(end)
            # Convert to local naive datetime
            if hasattr(start, "tzinfo") and start.tzinfo is not None:
                start = dt_util.as_local(start).replace(tzinfo=None)
            if hasattr(end, "tzinfo") and end.tzinfo is not None:
                end = dt_util.as_local(end).replace(tzinfo=None)
            slots.append(PriceSlot(start=start, end=end, price=float(price_val)))
        except Exception as exc:
            _LOGGER.debug("Dynamic pricing: failed to parse CKW entry %s: %s", entry, exc)
    return slots


def parse_epex_prices(attrs: dict) -> list:
    """Parse EPEX Spot (e.g. aWATTar) price attributes.

    Expected format in 'data':
        [{"start_time": "2026-05-22T00:00:00+02:00",
          "end_time":   "2026-05-22T01:00:00+02:00",
          "price_per_kwh": 0.14977}, ...]
    Hourly slots in EUR/kWh, typically covering today and tomorrow
    once published.
    Returns list[PriceSlot] in local naive time.
    """
    from homeassistant.util import dt as dt_util
    from datetime import datetime as _dt

    slots = []
    entries = attrs.get("data") or []
    for entry in entries:
        try:
            start = entry.get("start_time")
            end = entry.get("end_time")
            price_val = entry.get("price_per_kwh")
            if start is None or end is None or price_val is None:
                continue
            if isinstance(start, str):
                start = _dt.fromisoformat(start)
            if isinstance(end, str):
                end = _dt.fromisoformat(end)
            if hasattr(start, "tzinfo") and start.tzinfo is not None:
                start = dt_util.as_local(start).replace(tzinfo=None)
            if hasattr(end, "tzinfo") and end.tzinfo is not None:
                end = dt_util.as_local(end).replace(tzinfo=None)
            slots.append(PriceSlot(start=start, end=end, price=float(price_val)))
        except Exception as exc:
            _LOGGER.debug("Dynamic pricing: failed to parse EPEX entry %s: %s", entry, exc)
    return slots


def parse_entsoe_prices(attrs: dict) -> list:
    """Parse ENTSO-e Transparency Platform prices (HA jaapp integration).

    Expected attributes:
        prices_today / prices_tomorrow:
            [{"time": "2026-05-13 00:00:00+02:00", "price": 0.26027}, ...]
    Slots may be hourly or 15-minute. Each slot's end is inferred from the
    next slot's start time; the last slot inherits the previous delta and
    falls back to 60 minutes when only one entry exists.
    Returns list[PriceSlot] in local naive time, in chronological order.
    """
    from homeassistant.util import dt as dt_util
    from datetime import datetime as _dt, timedelta as _td

    raw = []
    for key in ("prices_today", "prices_tomorrow"):
        entries = attrs.get(key) or []
        for entry in entries:
            try:
                start = entry.get("time")
                price_val = entry.get("price")
                if start is None or price_val is None:
                    continue
                if isinstance(start, str):
                    start = _dt.fromisoformat(start)
                if hasattr(start, "tzinfo") and start.tzinfo is not None:
                    start = dt_util.as_local(start).replace(tzinfo=None)
                raw.append((start, float(price_val)))
            except Exception as exc:
                _LOGGER.debug("Dynamic pricing: failed to parse ENTSO-e entry %s: %s", entry, exc)

    if not raw:
        return []

    raw.sort(key=lambda x: x[0])
    slots = []
    for i, (start, price) in enumerate(raw):
        if i + 1 < len(raw):
            end = raw[i + 1][0]
        elif i > 0:
            end = start + (raw[i][0] - raw[i - 1][0])
        else:
            end = start + _td(hours=1)
        slots.append(PriceSlot(start=start, end=end, price=price))
    return slots


def parse_tibber_prices(prices_by_home: dict) -> list:
    """Parse the response of the ``tibber.get_prices`` service.

    Unlike the other providers, Tibber prices are not exposed as sensor
    attributes — they come from the ``tibber.get_prices`` service response:
        {"<home name>": [{"start_time": "2026-06-11T00:00:00.000+02:00",
                          "price": 0.3561}, ...]}
    Slots are typically 15-minute. Each slot's end is inferred from the next
    slot's start; the last slot inherits the previous delta (falling back to
    15 minutes). Tomorrow's slots are appended by Tibber after ~13:00.
    Returns list[PriceSlot] in local naive time, chronologically sorted.
    """
    from homeassistant.util import dt as dt_util
    from datetime import datetime as _dt, timedelta as _td

    if not prices_by_home:
        return []

    # Single home is the common case; pick it. With multiple homes Tibber keys
    # each by name and there is no configured selection, so use the first and
    # warn rather than merging conflicting timelines.
    home_names = list(prices_by_home.keys())
    if len(home_names) > 1:
        _LOGGER.warning(
            "Dynamic pricing: Tibber returned %d homes %s — using '%s'",
            len(home_names), home_names, home_names[0],
        )
    entries = prices_by_home.get(home_names[0]) or []

    raw = []
    for entry in entries:
        try:
            start = entry.get("start_time")
            price_val = entry.get("price")
            if start is None or price_val is None:
                continue
            if isinstance(start, str):
                start = _dt.fromisoformat(start)
            if hasattr(start, "tzinfo") and start.tzinfo is not None:
                start = dt_util.as_local(start).replace(tzinfo=None)
            raw.append((start, float(price_val)))
        except Exception as exc:
            _LOGGER.debug("Dynamic pricing: failed to parse Tibber entry %s: %s", entry, exc)

    if not raw:
        return []

    raw.sort(key=lambda x: x[0])
    slots = []
    for i, (start, price) in enumerate(raw):
        if i + 1 < len(raw):
            end = raw[i + 1][0]
        elif i > 0:
            end = start + (raw[i][0] - raw[i - 1][0])
        else:
            end = start + _td(minutes=15)
        slots.append(PriceSlot(start=start, end=end, price=price))
    return slots


def calculate_charging_hours_needed(deficit_kwh: float, max_contracted_power: float, max_charge_capacity: float) -> float:
    """Calculate how many hours of charging are needed to cover deficit.

    Uses the effective charge power: min(ICP limit, total battery charge capacity).
    If ICP > battery capacity, the batteries are the bottleneck and using ICP alone
    would underestimate the number of hours needed.
    """
    effective_power_kw = min(max_contracted_power, max_charge_capacity) / 1000.0
    if effective_power_kw <= 0:
        return 1.0  # Fallback: at least 1 hour if no power info available
    hours = deficit_kwh / (effective_power_kw * CHARGE_EFFICIENCY)
    return math.ceil(hours * 2) / 2  # Round up to nearest 0.5h


def calculate_planned_grid_charge_kwh(
    deficit_kwh: float,
    battery_headroom_kwh: float,
    margin_pct: float = 0.0,
) -> float:
    """Return the grid energy to schedule, capped by battery headroom.

    The predictive deficit describes how much energy the household is missing,
    but a small or nearly-full battery may not be able to store all of it. The
    optional forecast margin inflates the requested energy before the physical
    headroom cap is applied.
    """
    deficit = max(0.0, deficit_kwh)
    headroom = max(0.0, battery_headroom_kwh)
    margin_factor = 1.0 + max(0.0, margin_pct) / 100.0
    return min(headroom, deficit * margin_factor)


def select_cheapest_blocks(slots: list, hours_needed: float, slot_duration_h: float) -> list:
    """Select cheapest slots using a block strategy for sub-hourly granularity.

    Groups consecutive slots into 1-hour blocks (e.g. 4 × 15-min slots).
    Selects the cheapest block first, then the next cheapest, etc.
    Any remainder hours (e.g. 0.5h) use the cheapest consecutive sub-block
    of the appropriate size from the remaining slots.

    Args:
        slots: list[PriceSlot] already filtered (future + threshold)
        hours_needed: fractional hours of charging needed
        slot_duration_h: duration of each slot in hours (e.g. 0.25 for 15-min)

    Returns:
        Sorted (by start time) list of selected PriceSlot
    """
    block_size = max(1, round(1.0 / slot_duration_h))  # 4 for 15-min slots
    sorted_slots = sorted(slots, key=lambda s: s.start)
    n = len(sorted_slots)

    full_blocks_needed = int(hours_needed)
    remainder_slots_needed = round((hours_needed - full_blocks_needed) / slot_duration_h)

    def find_cheapest_window(available: list, window_size: int):
        """Return indices (into sorted_slots) of the cheapest time-consecutive window."""
        best_avg = float("inf")
        best_window = None
        for i in range(len(available) - window_size + 1):
            candidate = available[i:i + window_size]
            # Verify slots are time-consecutive (gap <= 1 min tolerance)
            consecutive = all(
                abs((sorted_slots[candidate[j + 1]].start - sorted_slots[candidate[j]].end).total_seconds()) < 60
                for j in range(len(candidate) - 1)
            )
            if not consecutive:
                continue
            avg = sum(sorted_slots[idx].price for idx in candidate) / window_size
            # Prefer lower price; break ties by earlier start time
            if avg < best_avg or (avg == best_avg and best_window is not None and
                    sorted_slots[candidate[0]].start < sorted_slots[best_window[0]].start):
                best_avg = avg
                best_window = list(candidate)
        return best_window

    available = list(range(n))
    selected_indices = []

    # Select full 1-hour blocks
    for block_num in range(full_blocks_needed):
        window = find_cheapest_window(available, block_size)
        if window is None:
            _LOGGER.warning(
                "Dynamic pricing: no consecutive block of %d slots available for block %d/%d, "
                "falling back to cheapest individual slots",
                block_size, block_num + 1, full_blocks_needed
            )
            # Fall back: pick cheapest individual available slots for this block
            by_price = sorted(available, key=lambda i: sorted_slots[i].price)
            take = min(block_size, len(by_price))
            window = by_price[:take]

        selected_indices.extend(window)
        for idx in window:
            available.remove(idx)

    # Select partial block (remainder)
    if remainder_slots_needed > 0 and available:
        window = find_cheapest_window(available, remainder_slots_needed)
        if window is None:
            _LOGGER.warning(
                "Dynamic pricing: no consecutive window of %d slots for remainder, "
                "falling back to cheapest individual slots",
                remainder_slots_needed
            )
            by_price = sorted(available, key=lambda i: sorted_slots[i].price)
            window = by_price[:remainder_slots_needed]
        selected_indices.extend(window)

    hours_accumulated = len(selected_indices) * slot_duration_h
    if hours_accumulated < hours_needed:
        _LOGGER.warning(
            "Dynamic pricing: only %.1fh selected in blocks, needed %.1fh "
            "(threshold may be too low or not enough consecutive slots)",
            hours_accumulated, hours_needed
        )

    _LOGGER.info(
        "Dynamic pricing (block strategy): %d blocks × %d slots + %d remainder slots selected "
        "(%.1fh total, slot_duration=%.2fh)",
        full_blocks_needed, block_size, remainder_slots_needed,
        hours_accumulated, slot_duration_h
    )
    return sorted([sorted_slots[i] for i in selected_indices], key=lambda s: s.start)


def expected_discharge_price(slots: list, hours_needed: float) -> float | None:
    """Average price of the most expensive ``hours_needed`` of remaining slots.

    This is the value a stored kWh is expected to realise: energy charged now
    displaces grid import during the priciest part of the remaining horizon.

    Symmetry assumption: the discharge window is taken to be as long as the
    charge window. That is an approximation, since discharge rate follows
    household load rather than charge power, but the resulting gate is
    insensitive to it: both sides of the comparison move together when the
    window widens.

    Must be called with the *unfiltered* future slots. The expensive hours are
    by definition above the charge ceiling, so computing this after the
    ``max_price_threshold`` filter would discard exactly the slots it needs.

    Returns None when there is not enough price data to form an estimate.
    """
    if not slots or hours_needed <= 0:
        return None

    by_price = sorted(slots, key=lambda s: s.price, reverse=True)
    total_price = 0.0
    hours = 0.0
    for slot in by_price:
        duration = (slot.end - slot.start).total_seconds() / 3600.0
        if duration <= 0:
            continue
        total_price += slot.price * duration
        hours += duration
        if hours >= hours_needed:
            break

    if hours <= 0:
        return None
    return total_price / hours


def arbitrage_ceiling(
    slots: list,
    hours_needed: float,
    min_arbitrage_margin,
    round_trip_efficiency: float = DEFAULT_ROUND_TRIP_EFFICIENCY,
) -> float | None:
    """Highest slot price that still clears ``min_arbitrage_margin`` per kWh.

    Returns None when the gate is disabled or when there is no price data to
    estimate the discharge value, in which case the caller falls back to the
    static ceiling alone.

    A margin of zero counts as disabled. A NumberEntity cannot be cleared back
    to "unset" from the UI, so zero has to mean off, otherwise anyone who tried
    the feature would be stuck with a permanently active gate.
    """
    if min_arbitrage_margin is None or min_arbitrage_margin <= 0:
        return None

    expected = expected_discharge_price(slots, hours_needed)
    if expected is None:
        _LOGGER.warning(
            "Dynamic pricing: arbitrage margin enabled but no price data to "
            "estimate discharge value, skipping the margin check"
        )
        return None

    ceiling = expected * round_trip_efficiency - min_arbitrage_margin
    _LOGGER.info(
        "Dynamic pricing: arbitrage ceiling %.4f "
        "(expected discharge %.4f x efficiency %.2f - margin %.4f)",
        ceiling, expected, round_trip_efficiency, min_arbitrage_margin,
    )
    return ceiling


def effective_charge_ceiling(
    slots: list,
    hours_needed: float,
    max_price_threshold,
    min_arbitrage_margin=None,
    round_trip_efficiency: float = DEFAULT_ROUND_TRIP_EFFICIENCY,
    now=None,
) -> tuple[float | None, float | None]:
    """Return ``(effective_ceiling, arbitrage_ceiling)``.

    ``effective_ceiling`` is the stricter of the static and arbitrage ceilings
    and is what callers pass to ``select_cheapest_hours``.

    ``arbitrage_ceiling`` is returned separately for reporting, and is None when
    the gate is off or could not be estimated. Note the two are equal only when
    the arbitrage ceiling is the stricter one; when the static ceiling binds,
    ``arbitrage_ceiling`` is a value that did not constrain anything. Callers
    that explain a decision to the user must compare the two rather than assume
    the arbitrage value was applied.

    ``slots`` must be the unfiltered horizon: the expensive hours the arbitrage
    estimate needs are by definition above the static ceiling.
    """
    if now is None:
        now = datetime.now()
    future_slots = [s for s in slots if s.end > now]

    margin_ceiling = arbitrage_ceiling(
        future_slots, hours_needed, min_arbitrage_margin, round_trip_efficiency
    )
    if margin_ceiling is None:
        return max_price_threshold, None
    if max_price_threshold is None or margin_ceiling < max_price_threshold:
        return margin_ceiling, margin_ceiling
    return max_price_threshold, margin_ceiling


def select_cheapest_hours(
    slots: list,
    hours_needed: float,
    max_price_threshold,
    now=None,
) -> list:
    """Filter slots by max_price_threshold, sort by price, return cheapest N.

    For sub-hourly granularity (e.g. 15-min slots) dispatches to
    select_cheapest_blocks to avoid scattered fragmented charging windows.

    Args:
        slots: list[PriceSlot] available in next 24h
        hours_needed: fractional hours of charging needed
        max_price_threshold: optional price ceiling (None disables filtering);
            pass the result of ``effective_charge_ceiling`` to apply the
            arbitrage gate as well
        now: evaluation instant, defaults to the wall clock

    Returns:
        Sorted (by start time) list of selected PriceSlot
    """
    if now is None:
        now = datetime.now()

    # Remove past slots
    future_slots = [s for s in slots if s.end > now]

    # Apply price threshold filter
    if max_price_threshold is not None:
        future_slots = [s for s in future_slots if s.price <= max_price_threshold]
        _LOGGER.info(
            "Dynamic pricing: %d slots after price threshold filter (max=%.3f)",
            len(future_slots), max_price_threshold
        )

    if not future_slots:
        _LOGGER.warning("Dynamic pricing: no slots available after filtering")
        return []

    # Dispatch to block strategy for sub-hourly granularity
    slot_duration_h = (future_slots[0].end - future_slots[0].start).total_seconds() / 3600.0
    if slot_duration_h < 0.9:
        return select_cheapest_blocks(future_slots, hours_needed, slot_duration_h)

    # Hourly slots: sort by price, accumulate until hours_needed is met
    sorted_slots = sorted(future_slots, key=lambda s: (s.price, s.start))

    selected = []
    hours_accumulated = 0.0
    for slot in sorted_slots:
        slot_duration = (slot.end - slot.start).total_seconds() / 3600.0
        selected.append(slot)
        hours_accumulated += slot_duration
        if hours_accumulated >= hours_needed:
            break

    if hours_accumulated < hours_needed:
        _LOGGER.warning(
            "Dynamic pricing: only %.1fh available, needed %.1fh (threshold may be too low)",
            hours_accumulated, hours_needed
        )

    # Return sorted by start time for chronological execution
    return sorted(selected, key=lambda s: s.start)
