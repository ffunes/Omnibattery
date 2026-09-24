# Schedule when batteries may operate

Time slots let you permit charging or discharging only during chosen periods. A slot can also limit state of charge (SOC) or power, or force one fixed power when you deliberately choose manual operation.

## Do I need it?

**Use it if** you want a recurring rule such as preventing discharge overnight, allowing charge only during selected hours, or applying a lower power or state-of-charge limit during part of the day.

**You do not need it if** the batteries may follow home consumption at all times. Predictive charging has its own tariff schedule and does not require these operating slots.

## Before you start

- Decide which direction the slot should permit: charge, discharge, or both.
- Choose the days, start and end time, and target battery.
- For normal automatic control, use **PD** operation mode. Proportional–derivative (PD) control still follows grid demand inside the slot.
- Use **Manual** only when you want one exact charge or discharge power and understand that the slot temporarily takes that battery out of automatic allocation.

## How to enable it

1. Open **Settings → Devices & services → Omnibattery → Configure → Time slots** and enable **Configure time slots**.
2. Set **Start time**, **End time**, **Days of the week**, and **Target battery**. The end must be later than the start on the same day.
3. Select **Allow charge**, **Allow discharge**, or both, then leave **Operation mode** at **PD** for automatic control.
4. Optional: enable **Override slot SOC max/min** or **Override slot max charge/discharge power** and complete the per-battery detail form.
5. Save the slot and add another only when a different period or battery needs a separate rule.

![Configure a time slot](../assets/screenshots/configuration/time-slot-form.png){ width="600" style="display: block; margin: 0 auto;"}

## What you will see

Every saved slot creates a **Time Slot N** switch on the Omnibattery system device. Turn it off to pause that slot without deleting its times and limits; turn it on to restore the saved rule. The switch state persists across restarts.

During an active PD slot, the battery continues responding to grid and household demand within the permitted directions and any slot limits. During an active Manual slot, the selected battery is forced to the configured charge or discharge power unless a safety blocker intervenes.

## If it does not work

| Symptom | Likely cause | What to check |
|---|---|---|
| The battery operates outside the slot | No enabled slot permits that direction, so that direction remains unrestricted | Add or enable at least one slot with **Allow charge** or **Allow discharge** for the target battery |
| The slot never becomes active | Its switch is off, today is not selected, or its target battery no longer exists | Check **Time Slot N**, the selected days, and **Target battery** |
| The form rejects the period | The end is not later than the start, or another slot overlaps for the same battery | Use a same-day period and remove the overlap |
| A power or SOC field does not appear | Its override checkbox is off | Enable the matching override and continue to the detail form |
| Manual mode does not force power | Power override is missing, both directions are selected, or a safety rule blocks the command | Configure one direction and its power, then check minimum/maximum SOC and EV pause status |

??? "Advanced details"
    **Direction rules**

    Charge and discharge are evaluated independently for each battery. If no enabled applicable slot has **Allow charge**, charging is unrestricted by time slots. Once any applicable slot allows charge, charging is permitted only inside a matching enabled slot. The same rule applies separately to discharge. Minimum and maximum SOC, EV pauses, manual battery ownership, and other safety rules still apply.

    This keeps migrated no-discharge schedules working as discharge-permission windows. Older charge-applicability settings migrate to **Allow charge**.

    **Overrides and limits**

    Omnibattery accepts up to 8 slots. In the detail form, minimum SOC ranges from 12–30%, maximum SOC from 80–100%, and power starts at 100 W and ends at each battery's hardware maximum in 50 W steps. In PD mode, power values cap automatic control. In Manual mode, the selected value becomes the exact requested power.

    A valid Manual slot needs power override enabled and exactly one direction selected. It directly commands the battery for that control cycle and removes it from PD allocation. Battery Manual Mode ownership, minimum and maximum SOC, and EV pause remain authoritative. Manual time-slot commands can bypass the [three-phase automatic envelope](three-phase.md).

    Slots aimed at different physical batteries may cover the same period. Slots that target the same battery, including an all-batteries scope, cannot overlap. A saved target for a battery removed from the integration becomes inert until you edit or remove the slot.

    For diagnostics, the **Predictive Charging Active** binary sensor can expose `active_slot_per_battery` with the current slot definition and `manual_slot_owned` with batteries controlled by a Manual slot.
