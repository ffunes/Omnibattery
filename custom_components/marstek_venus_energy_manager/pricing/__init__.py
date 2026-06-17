"""Dynamic pricing package.

Holds the price data model and pure price-math/parsing helpers extracted from
``ChargeDischargeController``. The runtime engine (``engine.PricingManager``) and
notifications live in submodules.
"""
from collections import namedtuple
from dataclasses import dataclass
from datetime import datetime

# Dynamic pricing data structures
PriceSlot = namedtuple("PriceSlot", ["start", "end", "price"])


@dataclass
class DynamicPricingSchedule:
    """Stores the result of a dynamic pricing evaluation."""
    hours_needed: float
    selected_slots: list  # list[PriceSlot]
    average_price: float
    estimated_cost: float
    total_available_slots: int
    evaluation_time: datetime
    energy_deficit_kwh: float
    charging_needed: bool = True


# Imported after PriceSlot is defined so ``calculations`` can resolve it from
# the partially-initialised package without a circular import.
from . import calculations  # noqa: E402,F401

__all__ = ["PriceSlot", "DynamicPricingSchedule", "calculations"]
