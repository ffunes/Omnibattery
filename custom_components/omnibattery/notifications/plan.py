"""Pure formatters for predictive-charging / dynamic-pricing notifications.

Extracted verbatim from ``ChargeDischargeController`` (module-8 PR2). Each
function builds a ``(title, message)`` pair from already-gathered data; the
controller keeps the thin ``_send_*`` wrappers that read its config and push the
``persistent_notification`` to Home Assistant. No side effects here.
"""
from __future__ import annotations

from datetime import datetime
import math
import re

from .gateway import EXECUTION, PLAN_CHARGE, PLAN_INFO
from .texts import day_name, translate


def _forecast_notification_context(decision: dict, lang: str = "en") -> dict:
    """Describe the energy horizon and forecast source consistently."""
    scope = str(decision.get("consumption_scope") or "daily")
    is_remaining = scope.startswith("remaining")
    avg_consumption = float(decision.get("avg_consumption_kwh", 0) or 0)
    remaining_consumption = decision.get("remaining_consumption_kwh")
    consumption_kwh = (
        float(remaining_consumption)
        if is_remaining and remaining_consumption is not None
        else avg_consumption
    )
    consumption_source = decision.get("consumption_forecast_source")
    days = int(decision.get("days_in_history", 0) or 0)
    profile_days = int(decision.get("profile_days", 0) or 0)

    horizon_end = decision.get("energy_horizon_end")
    # A planning horizon extended over tomorrow (#710) can end at 00:00 too,
    # but that is tomorrow's midnight, not tonight's (#721).
    covers_tomorrow = isinstance(horizon_end, datetime) and bool(
        decision.get("planning_horizon_extended")
    )
    ends_at_midnight = isinstance(horizon_end, datetime) and (
        horizon_end.hour == 0 and horizon_end.minute == 0
    )
    horizon_extended = covers_tomorrow or (
        isinstance(horizon_end, datetime) and not ends_at_midnight
    )
    if covers_tomorrow:
        horizon_label = (
            translate(lang, "plan.horizon.until_tomorrow_midnight")
            if ends_at_midnight
            else translate(
                lang,
                "plan.horizon.until_day_time",
                day=day_name(lang, horizon_end.weekday()),
                time=horizon_end.strftime("%H:%M"),
            )
        )
        horizon_phrase = (
            translate(lang, "plan.phrase.tomorrow")
            if ends_at_midnight
            else translate(lang, "plan.phrase.tomorrow_until", horizon=horizon_label)
        )
    elif horizon_extended:
        horizon_label = translate(
            lang, "plan.horizon.until_sunrise", time=horizon_end.strftime("%H:%M")
        )
        horizon_phrase = translate(lang, "plan.phrase.tonight", horizon=horizon_label)
    else:
        horizon_label = translate(lang, "plan.horizon.until_midnight")
        horizon_phrase = translate(lang, "plan.phrase.today")
    overnight_kwh_raw = decision.get("overnight_consumption_kwh")
    overnight_kwh = float(overnight_kwh_raw) if overnight_kwh_raw is not None else None

    if is_remaining:
        uses_profile = scope == "remaining_profile" or consumption_source == "profile"
        if uses_profile:
            basis = translate(lang, "plan.basis.profile")
        elif scope == "remaining_fallback":
            basis = translate(lang, "plan.basis.temporary_curve")
        elif days > 0:
            basis = translate(lang, "plan.basis.remaining_average", days=days)
        else:
            basis = translate(lang, "plan.basis.remaining_default")
        if not uses_profile and decision.get("consumption_accumulator_ready"):
            basis = translate(lang, "plan.basis.adjusted", basis=basis)
    else:
        if scope == "daily_profile" or consumption_source == "profile":
            sample_count = profile_days or days
            if sample_count > 0:
                basis = translate(lang, "plan.basis.profile_days", days=sample_count)
            else:
                basis = translate(lang, "plan.basis.profile")
        elif days > 0:
            basis = translate(lang, "plan.basis.average", days=days)
        else:
            basis = translate(lang, "plan.basis.daily_default")

    # On a horizon covering tomorrow the solar figure is today's remainder
    # plus tomorrow's forecast; the table shows the two parts.
    solar_split = None
    remaining_solar = decision.get("remaining_solar_kwh")
    tomorrow_solar = decision.get("tomorrow_solar_kwh")
    if (
        is_remaining
        and covers_tomorrow
        and remaining_solar is not None
        and tomorrow_solar is not None
    ):
        solar_split = (float(remaining_solar), float(tomorrow_solar))
    # The plan counts on the sun minus the safety margin; the table shows both.
    try:
        solar_total = decision.get("solar_forecast_kwh")
        solar_total = None if solar_total is None else float(solar_total)
        solar_margin = float(decision.get("solar_safety_margin_kwh") or 0.0)
    except (TypeError, ValueError):
        solar_total, solar_margin = None, 0.0
    return {
        "solar_total_kwh": solar_total,
        "solar_margin_share": (
            solar_margin / solar_total if solar_total and solar_margin > 0 else 0.0
        ),
        "solar_split": solar_split,
        "covers_tomorrow": covers_tomorrow,
        # Where the leg after midnight ends, unless that is midnight itself.
        "horizon_end_clock": (
            horizon_end.strftime("%H:%M")
            if horizon_extended and not ends_at_midnight
            else None
        ),
        "horizon_phrase": horizon_phrase,
        "is_remaining": is_remaining,
        "consumption_kwh": consumption_kwh,
        "consumption_basis": basis,
        "horizon_extended": horizon_extended,
        "horizon_label": horizon_label,
        "overnight_kwh": overnight_kwh,
    }


def _build_forecast_lines(
    context: dict, decision: dict, lang: str = "en", *extra
) -> str:
    """The energy balance as one table: sun, house, battery, then ``extra``
    ``(name, kWh)`` rows such as the grid charge (``plan.balance.<name>``).

    A horizon past midnight gets a column per leg plus the total; the leg
    after midnight is part of the total, never on top of it.
    """
    solar_split = context.get("solar_split")
    solar_total = context.get("solar_total_kwh")
    share = context.get("solar_margin_share") or 0.0
    consumption = context["consumption_kwh"]
    overnight = context.get("overnight_kwh") or 0.0
    legs = solar_split is not None or overnight > 0

    def _row(label, total, today=None, later=None):
        cells = [today, later, total] if legs else [total]
        if total is None:
            cells[-1] = "—"
        return f"| {label} | " + " | ".join(
            cell if isinstance(cell, str) else "" if cell is None else f"{cell:.2f}"
            for cell in cells
        ) + " |"

    columns = [translate(lang, "plan.balance.today")]
    if legs:
        columns += [
            translate(
                lang,
                "plan.balance.tomorrow" if context.get("covers_tomorrow") else "plan.balance.night",
            )
            + (f" → {clock}" if (clock := context.get("horizon_end_clock")) else ""),
            translate(lang, "plan.balance.total"),
        ]
    solar_parts = solar_split or (None, None)
    rows = [
        "| kWh | " + " | ".join(columns) + " |",
        "|---" * (len(columns) + 1) + "|",
        _row(translate(lang, "plan.balance.solar"), solar_total, *solar_parts),
    ]
    if share > 0:
        rows.append(_row(
            translate(lang, "plan.balance.solar_margin", pct=share * 100.0),
            solar_total * (1.0 - share),
            *(None if part is None else part * (1.0 - share) for part in solar_parts),
        ))
    rows.append(
        _row(
            translate(lang, "plan.balance.consumption"),
            consumption,
            max(0.0, consumption - overnight),
            overnight,
        )
        if overnight > 0
        else _row(translate(lang, "plan.balance.consumption"), consumption)
    )
    rows.append(_row(
        translate(lang, "plan.balance.battery", soc=float(decision.get("avg_soc") or 0.0)),
        float(decision.get("usable_energy_kwh") or 0.0),
    ))
    rows += [
        _row(translate(lang, f"plan.balance.{name}"), float(kwh or 0.0)) for name, kwh in extra
    ]
    # A blank line closes the table, or the text joins its last row.
    return (
        "\n".join(rows)
        + "\n\n"
        + translate(lang, "plan.line.basis", basis=context["consumption_basis"])
        + "\n"
    )


def _horizon_text(context: dict, *, with_for: bool = False, lang: str = "en") -> str:
    """Describe the remaining horizon consistently for 'not needed' messages."""
    if not context["is_remaining"]:
        return translate(lang, "plan.horizon_text.today")
    if with_for:
        return translate(lang, "plan.horizon_text.for", phrase=context["horizon_phrase"])
    return context["horizon_phrase"]


def _max_charge_power_str(
    max_contracted_power, max_charge_capacity=None, *, lang: str = "en"
) -> str:
    """The grid-charge ceiling and which limit sets it (the lower one)."""
    if max_charge_capacity is not None and max_charge_capacity < max_contracted_power:
        return translate(lang, "plan.power.batteries", power=max_charge_capacity)
    return translate(lang, "plan.power.contracted", power=max_contracted_power)


def _discharge_limit(decision_data: dict) -> tuple[float, float] | None:
    """Discharge limit (W) the planner capped the battery drain at, if known,
    and the house load above it (kWh): that comes from the grid, so the
    planned grid charge leaves it out.
    """
    try:
        limit = float(decision_data.get("battery_discharge_limit_w"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(limit):
        return None
    try:
        carried = float(decision_data.get("grid_carried_load_kwh") or 0.0)
    except (TypeError, ValueError):
        carried = 0.0
    return limit, carried


def _limits_table(rows, lang: str = "en") -> str:
    """``(name, value)`` power limits as a table (``plan.power.<name>``)."""
    if not rows:
        return ""
    return "\n".join(
        [f"| {translate(lang, 'plan.power.header')} | |", "|---|---|"]
        + [f"| {translate(lang, f'plan.power.{name}')} | {value} |" for name, value in rows]
    )


def format_predictive_notification_message(
    decision_data: dict,
    is_daily_evaluation: bool = False,
    *,
    max_contracted_power,
    max_charge_capacity,
    charging_time_slot,
    now: datetime,
    lang: str = "en",
) -> tuple[str, str, str]:
    """Format notification title and message from decision data.

    Args:
        decision_data: Dict from _should_activate_grid_charging() with energy balance data
        is_daily_evaluation: True when called from daily evaluation in automation_slots mode

    Returns:
        tuple: (title, message, category)
    """
    from datetime import time as dt_time

    should_charge = decision_data["should_charge"]
    solar_forecast = decision_data["solar_forecast_kwh"]
    total_available = decision_data["total_available_kwh"]
    energy_deficit = decision_data["energy_deficit_kwh"]
    context = _forecast_notification_context(decision_data, lang)

    power_str = _max_charge_power_str(max_contracted_power, max_charge_capacity, lang=lang)

    # Safe mode: no solar forecast
    if solar_forecast is None:
        title = translate(lang, "plan.safe.title")
        message = (
            translate(lang, "plan.safe.no_forecast")
            + "\n\n"
            + _build_forecast_lines(context, decision_data, lang)
            + "\n"
            + translate(lang, "plan.safe.not_activated")
        )
        return (title, message, PLAN_INFO)

    if decision_data.get("chronological_deferred"):
        shortfall = float(decision_data.get("deadline_shortfall_kwh", 0.0) or 0.0)
        title = translate(lang, "plan.deferred.title")
        message = (
            translate(lang, "plan.deferred.no_quota")
            + "\n\n"
            + _build_forecast_lines(context, decision_data, lang, ("grid_need", energy_deficit))
            + (
                translate(lang, "plan.deferred.shortfall", kwh=shortfall)
                if shortfall > 0
                else translate(lang, "plan.deferred.reserved")
            )
            + "\n"
        )
        return (title, message, PLAN_INFO)

    # Sufficient energy — no charging needed
    if not should_charge:
        title = translate(lang, "plan.not_required.title")
        horizon_text = _horizon_text(context, lang=lang)
        message = (
            translate(lang, "plan.sufficient", horizon=horizon_text)
            + "\n\n"
            + _build_forecast_lines(context, decision_data, lang, ("available", total_available))
            + "\n"
            + translate(
                lang,
                "plan.no_more_charging" if context["is_remaining"] else "plan.no_charging",
            )
        )
        return (title, message, PLAN_INFO)

    # Charging needed
    try:
        start_time = dt_time.fromisoformat(charging_time_slot["start_time"])
        end_time = dt_time.fromisoformat(charging_time_slot["end_time"])
        slot_str = f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}"
    except Exception:
        slot_str = None

    if is_daily_evaluation:
        title = translate(lang, "plan.expected_today.title")
        category = PLAN_CHARGE
        timing_line = translate(lang, "plan.timing.when_cheap") + "\n"
    else:
        title = translate(lang, "plan.started.title")
        category = EXECUTION
        timing_line = (
            translate(lang, "plan.timing.until", time=end_time.strftime("%H:%M")) + "\n"
            if slot_str else translate(lang, "plan.timing.now") + "\n"
        )

    planned_grid_charge = decision_data.get("planned_grid_charge_kwh")
    solar_surplus = decision_data.get("solar_surplus_kwh")
    if decision_data.get("floor_active"):
        # Charge is driven by the guaranteed-minimum-SOC floor, not the daily
        # energy balance. The floor charge happens whenever SOC is low (often
        # overnight, before solar ramps), so the day's solar surplus can't be
        # credited against it — the whole deficit comes from the grid.
        charge_rows = [("grid_floor", energy_deficit)]
    elif planned_grid_charge is not None and solar_surplus is not None:
        # The predictive SOC target is based on planned_grid_charge_kwh.  Do not
        # report the complementary headroom calculation here: it can differ
        # from the actual grid target when a grid-charge margin is configured.
        # Solar surplus is a whole-day energy-balance estimate, not a promise
        # that the remainder of today's battery headroom will be solar-charged.
        charge_rows = [("grid_charge", planned_grid_charge), ("solar_surplus", solar_surplus)]
    else:
        charge_rows = [("deficit", energy_deficit)]

    message = (
        translate(lang, "plan.charge.needed")
        + "\n\n"
        + _build_forecast_lines(context, decision_data, lang, *charge_rows)
        + "\n"
        # A blank line before the table so markdown renders it as one.
        + f"{timing_line}\n"
        + _limits_table([("charge", power_str)], lang)
    )

    return (title, message, category)


def _blocks(rows):
    """Back-to-back ``(start, end, value, *sums)`` rows with one value, as one
    row with each of ``sums`` added up (None stays None)."""
    out = []
    for start, end, value, *sums in sorted(rows, key=lambda row: row[0]):
        if out and out[-1][1] == start and out[-1][2] == value:
            first, _end, _value, *totals = out[-1]
            out[-1] = (first, end, value, *(
                None if total is None else total + (add or 0.0)
                for total, add in zip(totals, sums)
            ))
        else:
            out.append((start, end, value, *sums))
    return out


def _day_prefix(start, now, lang: str = "en") -> str:
    """``""`` when ``start`` is today, else ``Wed 25 · `` (``start`` already in ``now``'s zone)."""
    if start.date() == now.date():
        return ""
    return translate(
        lang, "plan.day_prefix", day=day_name(lang, start.weekday()), date=start.day
    )


def _clock(at, now, lang: str = "en") -> str:
    """``HH:MM``; ``Wed 25 · HH:MM`` when ``at`` is not today."""
    if at.tzinfo is not None and now.tzinfo is not None:
        at = at.astimezone(now.tzinfo)
    return f"{_day_prefix(at, now, lang)}{at.strftime('%H:%M')}"


def _span(start, end, now, lang: str = "en") -> str:
    """``HH:MM–HH:MM``; ``Wed 25 · HH:MM–HH:MM`` when ``start`` is not today."""
    if start.tzinfo is not None and now.tzinfo is not None:
        start = start.astimezone(now.tzinfo)
        end = end.astimezone(now.tzinfo)
    return f"{_day_prefix(start, now, lang)}{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def _quarter_rows(orders, kind):
    """The plan's ``(start, end, price, kWh)`` quarters of one kind."""
    return [
        (a, b, price, kwh)
        for a, b, k, price, kwh in (orders or {}).get("quarters") or ()
        if k == kind
    ]


def _charge_span(slot, charges):
    """The part of ``slot`` the plan charges in: its first charge quarter's
    start to its last one's end; the whole slot without quarter data."""
    inside = [(a, b) for a, b, _p, _k in charges if slot.start <= a < slot.end]
    if not inside:
        return slot.start, slot.end
    return min(a for a, _b in inside), max(b for _a, b in inside)


def _bought(rows):
    """``(start, end, price, kWh)`` rows the house buys from the grid, with
    their cost (price x kWh) as a fifth value."""
    return [
        (a, b, price, kwh, None if kwh is None else price * kwh)
        for a, b, price, kwh in rows
    ]


def _table(groups, now, lang, unit) -> list:
    """``{icon: (start, end, price, kWh|None, cost|None) rows}`` as one
    markdown table in time order, lines; each row opens with its group's icon
    (``""`` for none). Several rows with a cost close with a total: the
    energy, its average price and the cost."""
    cost_unit = unit.split("/")[0]  # "€/kWh" → "€", "CHF" → "CHF"

    # Back-to-back rows at one price join inside a group only: a charge and
    # the hold after it at the same price stay two rows.
    blocks = sorted(
        ((*block, icon) for icon, rows in groups.items() for block in _blocks(rows)),
        key=lambda block: block[0],
    )
    # A column no row has a value for (energy, cost) is left out.
    keep = [True, True] + [
        any(block[i] is not None for block in blocks) for i in (3, 4)
    ]

    def _cells(cells):
        return "| " + " | ".join(c for c, kept in zip(cells, keep) if kept) + " |"

    def _row(span, price, kwh, cost):
        return _cells([
            span,
            "—" if price is None else translate(lang, "plan.table.price", price=price, unit=unit),
            "—" if kwh is None else translate(lang, "plan.table.kwh", kwh=kwh),
            "—" if cost is None else translate(lang, "plan.table.cost", cost=cost, cost_unit=cost_unit),
        ])

    header = [c.strip() for c in translate(lang, "plan.table.header").strip("|").split("|")]
    lines = [_cells(header), "|---" * sum(keep) + "|"] + [
        _row(f"{icon} {_span(a, b, now, lang)}".strip(), price, kwh, cost)
        for a, b, price, kwh, cost, icon in blocks
    ]
    if len(blocks) > 1 and any(block[4] is not None for block in blocks):
        known = [(p, kwh) for _a, _b, p, kwh, _c, _i in blocks if kwh is not None]
        energy = sum(kwh for _p, kwh in known)
        lines.append(_row(
            translate(lang, "plan.table.total"),
            sum(p * kwh for p, kwh in known) / energy if energy > 0 else None,
            energy if known else None,
            sum(block[4] for block in blocks if block[4] is not None),
        ))
    return lines


def _plan_order_lines(
    orders, now, lang: str = "en", unit: str = "€/kWh", charges=(), note: str = "",
    saving=None,
) -> str:
    """The optimizer's orders, as executed (published). What is bought from
    the grid is one table in time order: the charge (``charges``, rows ready
    for ``_table``), the holds and the battery empty; ``note`` follows it.
    Last, what the plan saves (``saving``) against the battery with no plan,
    when it rounds to a cent or more."""
    orders = orders or {}
    lines = []

    def _power_table(key, by_slot):
        # The label, then span and power; blank lines so markdown renders it.
        span_header = translate(lang, "plan.table.header").strip("|").split("|")[0].strip()
        lines.extend([
            translate(lang, f"plan.orders.{key}"),
            "",
            f"| {span_header} | {translate(lang, 'plan.table.power')} |",
            "|---|---|",
            *(
                f"| {_span(a, b, now, lang)} | {kw:.1f} kW |"
                for a, b, kw, _k in _blocks(
                    (s.start, s.end, round(float(kw), 1), None) for s, kw in by_slot.items()
                )
            ),
            "",
        ])

    # The holds as a table, by quarter when the plan gave them (a slot that
    # charges and then holds is held only after its charge).
    holds = _bought(_quarter_rows(orders, "hold") if orders.get("quarters") else [
        (s.start, s.end, s.price, None) for s in orders.get("holds") or ()
    ])
    # The battery empty: the house buys from the grid. One row per price, as
    # one average over a night hides which part of it is the dear one.
    bought = [
        (icon, key, rows)
        for icon, key, rows in (
            ("🔌", "charge", list(charges)),
            ("🔒", "holds", holds),
            ("🪫", "empty", _bought(_quarter_rows(orders, "empty"))),
        )
        if rows
    ]
    if bought:
        # The table under its title, then what each icon means; blank lines
        # around the table, or markdown does not render it as one.
        lines += [translate(lang, "plan.orders.title"), ""]
        lines += _table({icon: rows for icon, _key, rows in bought}, now, lang, unit) + [""]
        lines.append(translate(lang, "plan.orders.legend"))
        lines += [f"{icon} {translate(lang, f'plan.orders.{key}')}" for icon, key, _rows in bought]
        lines.append("")
        if note:
            lines += [note, ""]
    exports = orders.get("exports") or ()
    if exports:
        spans = _blocks((s.start, s.end, None, None) for s in exports)
        lines.append(
            translate(
                lang,
                "plan.orders.exports",
                items=", ".join(_span(a, b, now, lang) for a, b, _v, _k in spans),
            )
        )
    sales = orders.get("sales") or {}
    if sales:
        _power_table("sales", sales)
    room = orders.get("room") or {}
    if room:
        _power_table("room", room)
    if orders.get("solar_delay"):
        lines.append(translate(lang, "plan.orders.solar_delay"))
    if saving is not None and round(saving, 2) > 0:
        lines.append(
            translate(lang, "plan.savings", saving=saving, cost_unit=unit.split("/")[0])
        )
    return "".join(line + "\n" for line in lines)


def _no_slots_explanation(
    decision_data: dict,
    unit: str,
    arbitrage_binding: bool,
    now: datetime,
    lang: str = "en",
) -> tuple[str, str]:
    """Headline and hint for a deficit that ended with no selected slot.

    The chronological planner records why it allocated nothing; only a
    rejection by price is fixed by the price sensor or the threshold, so the
    generic hint is kept for that case (and when no plan reason exists).
    """
    reason = decision_data.get("chronological_plan_reason")
    if reason == "awaiting_prices":
        waiting = float(decision_data.get("awaiting_prices_kwh", 0.0) or 0.0)
        return (
            translate(lang, "plan.noslots.awaiting_prices.headline"),
            translate(lang, "plan.noslots.awaiting_prices.hint", kwh=waiting),
        )
    if reason == "cheaper_to_import":
        imported = float(decision_data.get("cheaper_to_import_kwh", 0.0) or 0.0)
        return (
            translate(lang, "plan.noslots.cheaper_to_import.headline"),
            translate(lang, "plan.noslots.cheaper_to_import.hint", kwh=imported),
        )
    if reason in ("deadline_capacity_shortfall", "insufficient_slot_capacity"):
        return (
            translate(lang, "plan.noslots.capacity.headline"),
            translate(lang, "plan.noslots.capacity.hint"),
        )
    if reason == "insufficient_power":
        return (
            translate(lang, "plan.noslots.no_power.headline"),
            translate(lang, "plan.noslots.no_power.hint"),
        )
    if reason == "ok":
        return (
            translate(lang, "plan.noslots.no_room.headline"),
            translate(lang, "plan.noslots.no_room.hint"),
        )
    if reason == "no_eligible_slots":
        return (
            translate(lang, "plan.noslots.no_eligible.headline"),
            translate(lang, "plan.noslots.no_eligible.hint"),
        )
    hint = translate(
        lang, "plan.noslots.hint.arbitrage" if arbitrage_binding else "plan.noslots.hint.price"
    )
    rejected = decision_data.get("cheapest_rejected_slot")
    if reason == "price_threshold" and isinstance(rejected, dict):
        try:
            start = _clock(datetime.fromisoformat(str(rejected["start"])), now, lang)
            price = float(rejected["price"])
            ceiling = rejected.get("ceiling")
            if ceiling is not None:
                hint = translate(
                    lang,
                    "plan.noslots.cheapest_ceiling",
                    start=start,
                    price=price,
                    unit=unit,
                    ceiling=float(ceiling),
                    hint=hint,
                )
            else:
                hint = translate(
                    lang,
                    "plan.noslots.cheapest",
                    start=start,
                    price=price,
                    unit=unit,
                    hint=hint,
                )
        except (KeyError, TypeError, ValueError):
            pass
    return (translate(lang, "plan.noslots.default.headline"), hint)


def format_dynamic_pricing_notification(
    decision_data: dict,
    schedule,
    *,
    unit: str,
    max_price_threshold,
    discharge_price_threshold,
    arbitrage_ceiling=None,
    max_contracted_power,
    max_charge_capacity,
    plan_orders=None,
    arbitrage_margin=None,
    plan_saving=None,
    now: datetime,
    lang: str = "en",
) -> tuple[str, str, str]:
    """Format dynamic pricing evaluation notification.

    With the 2.0.0 optimizer (``planner == "optimizer"``) the old planner's
    words (deficit, deadlines, flexible) mean nothing: it lists the charge in
    blocks and the plan's other orders (``plan_orders``: the published holds,
    exports, sales and solar delay), and what it saves (``plan_saving``)."""
    optimizer = decision_data.get("planner") == "optimizer"
    orders_lines = (
        _plan_order_lines(plan_orders, now, lang, unit, saving=plan_saving) if optimizer else ""
    )
    avg_soc = decision_data.get("avg_soc", 0)
    usable_energy = decision_data.get("usable_energy_kwh", 0)
    energy_deficit = decision_data.get("energy_deficit_kwh", 0)
    planned_grid_charge = decision_data.get("planned_grid_charge_kwh", energy_deficit)
    context = _forecast_notification_context(decision_data, lang)

    def _balance(*rows) -> str:
        return _build_forecast_lines(context, decision_data, lang, *rows)

    battery_line = translate(lang, "plan.battery", soc=avg_soc, usable=usable_energy)
    # The arbitrage ceiling is only the binding constraint when it undercuts the
    # static one; otherwise it is reported for information but decided nothing.
    arbitrage_binding = arbitrage_ceiling is not None and (
        max_price_threshold is None or arbitrage_ceiling < max_price_threshold
    )

    _price_parts = []
    if max_price_threshold is not None:
        _price_parts.append(
            translate(lang, "plan.price.charge", price=max_price_threshold, unit=unit)
        )
    if discharge_price_threshold is not None:
        _price_parts.append(
            translate(lang, "plan.price.discharge", price=discharge_price_threshold, unit=unit)
        )
    if arbitrage_ceiling is not None:
        _price_parts.append(
            translate(
                lang,
                "plan.price.arbitrage" if arbitrage_binding else "plan.price.arbitrage_not_binding",
                price=arbitrage_ceiling,
                unit=unit,
            )
        )
    price_config_line = (
        translate(lang, "plan.price.thresholds", parts=" | ".join(_price_parts))
        if _price_parts
        else ""
    )

    def _footer(charge_power: bool = False, discharge: bool = False) -> str:
        """Thresholds and power limits as the closing block, after a blank line."""
        rows = []
        note = ""
        if charge_power:
            rows.append((
                "charge",
                _max_charge_power_str(max_contracted_power, max_charge_capacity, lang=lang),
            ))
        limit = _discharge_limit(decision_data) if discharge else None
        if limit:
            rows.append(("discharge", f"{limit[0]:g}W"))
            if limit[1] >= 0.005:
                note = translate(lang, "plan.power.carried", carried=limit[1])
        # Blank lines around the table, or markdown joins the text to it.
        parts = [p for p in (price_config_line, _limits_table(rows, lang), note) if p]
        return "\n\n" + "\n\n".join(parts) if parts else ""

    if schedule is None or not schedule.selected_slots:
        if not decision_data.get("should_charge", False):
            title = translate(lang, "plan.pricing.not_needed.title")
            category = PLAN_INFO
            horizon_text = _horizon_text(context, lang=lang)
            message = (
                translate(lang, "plan.sufficient", horizon=horizon_text)
                + "\n\n"
                + _balance(("available", decision_data.get("total_available_kwh", 0)))
                + "\n"
                + f"{orders_lines}"
                + translate(
                    lang,
                    "plan.no_more_charging" if context["is_remaining"] else "plan.no_charging",
                )
                + _footer()
            )
        elif decision_data.get("deferred_to_next_day"):
            title = translate(lang, "plan.pricing.deferred.title")
            category = PLAN_INFO
            message = (
                translate(lang, "plan.pricing.deferred.headline")
                + "\n\n"
                + _balance(("deficit", energy_deficit))
                + f"{orders_lines}\n"
                + translate(lang, "plan.pricing.deferred.footer")
                + f"{_footer()}"
            )
        else:
            headline, hint = _no_slots_explanation(
                decision_data, unit, arbitrage_binding, now, lang
            )
            plan_reason = decision_data.get("chronological_plan_reason")
            category = PLAN_INFO
            title = translate(
                lang,
                "plan.pricing.import.title"
                if plan_reason == "cheaper_to_import"
                else "plan.pricing.awaiting.title"
                if plan_reason == "awaiting_prices"
                else "plan.pricing.no_slots.title",
            )
            if optimizer and plan_reason == "cheaper_to_import":
                # Not a fault: the plan weighed it. A margin of 0 means
                # losses only, so it is named only when one is set.
                imported = float(decision_data.get("cheaper_to_import_kwh", 0.0) or 0.0)
                storing = (
                    translate(
                        lang,
                        "plan.pricing.import.storing_margin",
                        margin=float(arbitrage_margin),
                        unit=unit,
                    )
                    if arbitrage_margin and float(arbitrage_margin) > 0
                    else translate(lang, "plan.pricing.import.storing")
                )
                message = (
                    translate(lang, "plan.pricing.import.headline")
                    + "\n\n"
                    + _balance(("import_left", imported))
                    + f"{orders_lines}\n"
                    + storing
                    + f"{_footer()}"
                )
            else:
                message = (
                    translate(lang, "plan.pricing.warning", headline=headline)
                    + "\n\n"
                    + _balance(("deficit", energy_deficit))
                    + f"{orders_lines}\n"
                    + f"{hint}"
                    + f"{_footer()}"
                )
    else:
        hours_needed = schedule.hours_needed
        n_slots = len(schedule.selected_slots)
        schedule_type = getattr(schedule, "schedule_type", "deficit")
        category = PLAN_CHARGE
        charges = _quarter_rows(plan_orders, "charge") if optimizer else []
        spans = {s: _charge_span(s, charges) for s in schedule.selected_slots}
        if charges:
            # The time the plan really charges, not the slots it charges in.
            hours_needed = sum((b - a).total_seconds() for a, b in spans.values()) / 3600.0
        slots_label = (
            translate(lang, "plan.pricing.slots", count=n_slots)
            if n_slots != int(hours_needed) and not charges
            else ""
        )
        hours_label = (
            translate(lang, "plan.pricing.hours_slots", hours=round(hours_needed, 2), slots=slots_label)
            if slots_label
            else translate(lang, "plan.pricing.hours", hours=round(hours_needed, 2))
        )
        title = translate(lang, "plan.pricing.selected.title", hours=hours_label)

        cost_unit = unit.split("/")[0]  # "€/kWh" → "€", "CHF" → "CHF"
        slot_lines = "\n".join(
            translate(
                lang,
                "plan.slot.line",
                span=_span(s.start, s.end, now, lang),
                price=s.price,
                unit=unit,
                extras=(
                    (
                        translate(
                            lang,
                            "plan.slot.kwh",
                            kwh=float(schedule.slot_energy_targets_kwh[s]),
                        )
                        if s in getattr(schedule, "slot_energy_targets_kwh", {})
                        else ""
                    )
                    + (
                        translate(
                            lang,
                            "plan.slot.before",
                            time=_clock(schedule.slot_deadlines[s], now, lang),
                        )
                        if getattr(schedule, "slot_deadlines", {}).get(s) is not None
                        else (
                            translate(lang, "plan.slot.flexible")
                            if s in getattr(schedule, "slot_energy_targets_kwh", {})
                            else translate(
                                lang, "plan.slot.purpose", purpose=schedule.purpose_for(s)
                            )
                            if hasattr(schedule, "purpose_for")
                            else ""
                        )
                    )
                    + (
                        translate(
                            lang, "plan.slot.purpose_extra", purpose=schedule.purpose_for(s)
                        )
                        if hasattr(schedule, "purpose_for")
                        and schedule.purpose_for(s) in {"negative_price", "combined"}
                        else ""
                    )
                ),
            )
            for s in sorted(schedule.selected_slots, key=lambda s: s.start)
        )
        table_costs = False
        # The old planner's charge keeps its tagged list (deadline, flexible).
        if optimizer or schedule_type == "negative_price" or not schedule.charging_needed:
            # The optimizer books quarters: one line per run at one price.
            targets = getattr(schedule, "slot_energy_targets_kwh", {}) or {}
            # Each slot's share of the estimated cost (it includes the charging
            # losses), by the energy that cost pays for.
            paid = getattr(schedule, "slot_cost_energy_kwh", None) or targets
            paid = {s: float(paid[s]) for s in schedule.selected_slots if s in paid}
            weight = sum(s.price * kwh for s, kwh in paid.items())
            # ponytail: prices of both signs that cancel out leave no shares.
            share = float(schedule.estimated_cost) / weight if abs(weight) > 1e-9 else None
            # The table then carries the price and the cost: no text lines.
            table_costs = share is not None
            # A blank line before the table so markdown renders it as one.
            charge_rows = [
                (
                    *spans[s],
                    s.price,
                    None if s not in targets else float(targets[s]),
                    None if share is None or s not in paid else s.price * paid[s] * share,
                )
                for s in schedule.selected_slots
            ]
            slot_lines = "\n" + "\n".join(_table({"": charge_rows}, now, lang, unit))
        # The table carries the price and the cost, with a total for several rows.
        cost_lines = "" if table_costs else (
            "\n\n"
            + translate(lang, "plan.pricing.average_price", price=schedule.average_price, unit=unit)
            + "\n"
            + translate(
                lang, "plan.pricing.estimated_cost", cost=schedule.estimated_cost, cost_unit=cost_unit
            )
        )
        if schedule_type == "negative_price":
            title = translate(lang, "plan.pricing.negative.title", hours=hours_label)
            opportunity_energy = float(
                getattr(schedule, "negative_price_energy_kwh", 0.0) or 0.0
            )
            message = (
                battery_line
                + "\n"
                + translate(lang, "plan.pricing.negative.opportunistic", kwh=opportunity_energy)
                + "\n"
                + translate(lang, "plan.pricing.negative.no_deficit")
                + "\n\n"
                + translate(lang, "plan.pricing.negative.header")
                + f"\n{slot_lines}"
                + cost_lines
                + _footer(charge_power=True)
            )
        elif not schedule.charging_needed:
            title = translate(lang, "plan.pricing.info.title", hours=hours_label)
            category = PLAN_INFO
            horizon_text = _horizon_text(context, with_for=True, lang=lang)
            message = (
                translate(lang, "plan.pricing.info.no_charging", horizon=horizon_text)
                + "\n\n"
                + _balance(("available", decision_data.get("total_available_kwh", 0)))
                + "\n"
                + f"{orders_lines}"
                + translate(lang, "plan.pricing.info.header")
                + f"\n{slot_lines}\n\n"
                + ("" if table_costs else translate(lang, "plan.pricing.average_price", price=schedule.average_price, unit=unit) + "\n")
                + translate(lang, "plan.pricing.info.footer")
                + f"{_footer()}"
            )
        else:
            if decision_data.get("floor_active"):
                # The floor booked all or part of it; the price only picked when.
                title = translate(lang, "plan.pricing.floor.title", hours=hours_label)
            opportunity_line = ""
            if schedule_type == "combined":
                opportunity_line = (
                    translate(
                        lang,
                        "plan.pricing.opportunity",
                        kwh=float(getattr(schedule, "negative_price_energy_kwh", 0.0) or 0.0),
                    )
                    + "\n"
                )
            chronological_lines = ""
            if getattr(schedule, "chronological_planning_active", False):
                deadline = float(getattr(schedule, "deadline_required_kwh", 0.0) or 0.0)
                flexible = float(getattr(schedule, "flexible_required_kwh", 0.0) or 0.0)
                chronological_lines = (
                    translate(lang, "plan.pricing.deadline", kwh=deadline)
                    + "\n"
                    + translate(lang, "plan.pricing.flexible", kwh=flexible)
                    + "\n"
                )
                deferred = float(decision_data.get("deferred_grid_charge_kwh", 0.0) or 0.0)
                if deferred > 0.005:
                    chronological_lines += (
                        translate(lang, "plan.pricing.tomorrow", kwh=deferred) + "\n"
                    )
                imported = float(getattr(schedule, "cheaper_to_import_kwh", 0.0) or 0.0)
                if imported > 0:
                    chronological_lines += (
                        translate(lang, "plan.pricing.import_left", kwh=imported) + "\n"
                    )
                shortfall = float(getattr(schedule, "deadline_shortfall_kwh", 0.0) or 0.0)
                if shortfall > 0:
                    chronological_lines += (
                        translate(
                            lang,
                            "plan.pricing.cannot_deliver",
                            kwh=shortfall,
                            reason=getattr(
                                schedule, "chronological_plan_reason", "insufficient capacity"
                            ),
                        )
                        + "\n"
                    )
            selected_label = translate(
                lang,
                "plan.pricing.selected_deadline"
                if getattr(schedule, "chronological_planning_active", False)
                else "plan.pricing.selected_hours",
            )
            forecast_lines = _balance(("deficit", energy_deficit))
            charge_lines = (
                f"{opportunity_line}"
                + translate(
                    lang,
                    "plan.pricing.grid_charge_hours",
                    kwh=planned_grid_charge,
                    hours=hours_needed,
                )
                + "\n\n"
                + f"{chronological_lines}"
            )
            if optimizer:
                deferred = float(decision_data.get("deferred_grid_charge_kwh", 0.0) or 0.0)
                # The planned grid charge is a row of the balance table.
                forecast_lines = _balance(("grid_charge", planned_grid_charge))
                charge_lines = (
                    (
                        translate(lang, "plan.pricing.tomorrow", kwh=deferred) + "\n"
                        if deferred > 0.005
                        else ""
                    )
                    + "\n"
                )
            if optimizer:
                # One table, in time order: the charge, the holds and the
                # battery empty; the cost text right after it.
                slots_block = _plan_order_lines(
                    plan_orders, now, lang, unit, charges=charge_rows,
                    note=cost_lines.strip(), saving=plan_saving,
                ).rstrip()
            else:
                slots_block = (
                    translate(lang, "plan.pricing.slots_header", label=selected_label)
                    + f"\n{slot_lines}"
                    + cost_lines
                )
            message = (
                f"{forecast_lines}"
                + f"{charge_lines}"
                + slots_block
                + _footer(charge_power=True, discharge=True)
            )

    # A table closes with a blank line; the text after it may add its own.
    return (title, re.sub(r"\n{3,}", "\n\n", message), category)


def format_slot_start_notification(
    slot,
    schedule,
    *,
    unit: str,
    max_contracted_power,
    now: datetime,
    lang: str = "en",
) -> tuple[str, str, str]:
    """Format the 'cheap pricing slot started' notification.

    ``schedule`` must be non-None (the caller guards on it).
    """
    remaining_slots = [
        s for s in schedule.selected_slots if s.start > slot.start
    ]
    if remaining_slots:
        next_line = translate(
            lang,
            "slot_start.next",
            time=_clock(remaining_slots[0].start, now, lang),
            count=len(remaining_slots),
        )
    else:
        next_line = translate(lang, "slot_start.last")

    title = translate(lang, "slot_start.title", price=slot.price, unit=unit)
    message = (
        translate(lang, "slot_start.charging", power=max_contracted_power)
        + "\n"
        + translate(lang, "slot_start.slot", span=_span(slot.start, slot.end, now, lang))
        + "\n"
        + next_line
    )
    return (title, message, EXECUTION)


def format_dp_pre_slot_reevaluation_notification(
    slot,
    decision: dict,
    *,
    unit: str,
    now: datetime,
    lang: str = "en",
) -> tuple[str, str, str]:
    """Format the pre-slot re-evaluation 'charging still needed' notification."""
    energy_deficit = decision.get("energy_deficit_kwh", 0)
    context = _forecast_notification_context(decision, lang)

    title = translate(lang, "pre_slot.title", time=_clock(slot.start, now, lang))
    message = (
        _build_forecast_lines(context, decision, lang, ("deficit", energy_deficit))
        + "\n"
        + translate(
            lang,
            "pre_slot.slot",
            span=_span(slot.start, slot.end, now, lang),
            price=slot.price,
            unit=unit,
        )
        + "\n"
        + translate(lang, "pre_slot.activate", time=_clock(slot.start, now, lang))
    )
    return (title, message, EXECUTION)
