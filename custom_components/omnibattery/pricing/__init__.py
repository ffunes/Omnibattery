"""Dynamic pricing package.

Holds the price data model and pure price-math/parsing helpers extracted from
``ChargeDischargeController``. The runtime engine (``engine.PricingManager``) and
notifications live in submodules.
"""
from collections import namedtuple
from dataclasses import dataclass, field
from datetime import datetime

# Dynamic pricing data structures
PriceSlot = namedtuple("PriceSlot", ["start", "end", "price"])

SLOT_PURPOSE_DEFICIT = "deficit"
SLOT_PURPOSE_NEGATIVE_PRICE = "negative_price"
SLOT_PURPOSE_COMBINED = "combined"


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
    # Keep ``selected_slots`` as plain PriceSlot objects for backwards
    # compatibility.  Purpose metadata is keyed by the immutable PriceSlot so
    # normal deficit energy can never accidentally inherit an opportunistic SOC
    # target in a positive-price slot.
    slot_purposes: dict = field(default_factory=dict)
    schedule_type: str = SLOT_PURPOSE_DEFICIT
    deficit_charging_needed: bool | None = None
    negative_price_charging_needed: bool = False
    deficit_hours_needed: float = 0.0
    negative_price_hours_needed: float = 0.0
    negative_price_energy_kwh: float = 0.0
    slot_energy_targets_kwh: dict = field(default_factory=dict)
    slot_deadlines: dict = field(default_factory=dict)
    slot_plan_kinds: dict = field(default_factory=dict)
    chronological_planning_active: bool = False
    chronological_source: str | None = None
    solar_timeline_source: str | None = None
    earliest_depletion_at: datetime | None = None
    deadline_required_kwh: float = 0.0
    flexible_required_kwh: float = 0.0
    deadline_shortfall_kwh: float = 0.0
    total_shortfall_kwh: float = 0.0
    energy_deadlines: list = field(default_factory=list)
    chronological_plan_reason: str | None = None

    def __post_init__(self) -> None:
        """Fill purpose metadata for schedules created by older callers/tests."""
        if self.deficit_charging_needed is None:
            self.deficit_charging_needed = self.charging_needed
        if not self.slot_purposes:
            self.slot_purposes = {
                slot: SLOT_PURPOSE_DEFICIT for slot in self.selected_slots
            }

    def purpose_for(self, slot: PriceSlot) -> str:
        """Return why a selected price slot is present in the schedule."""
        return self.slot_purposes.get(slot, SLOT_PURPOSE_DEFICIT)


from .curtailment import BatterySnapshot, CurtailmentPlan, PreDischargeSlot


# Imported after PriceSlot is defined so ``calculations`` can resolve it from
# the partially-initialised package without a circular import.
from . import calculations  # noqa: E402,F401

__all__ = [
    "PriceSlot",
    "DynamicPricingSchedule",
    "SLOT_PURPOSE_DEFICIT",
    "SLOT_PURPOSE_NEGATIVE_PRICE",
    "SLOT_PURPOSE_COMBINED",
    "BatterySnapshot",
    "CurtailmentPlan",
    "PreDischargeSlot",
    "calculations",
]
