"""Per-cycle predictive state messages: INFO on a change, DEBUG while repeating."""

import logging
from types import SimpleNamespace

from custom_components.omnibattery.pricing.engine import PricingManager

ENGINE_LOGGER = "custom_components.omnibattery.pricing.engine"


def test_log_state_once_info_only_on_transition(caplog):
    caplog.set_level(logging.DEBUG, logger=ENGINE_LOGGER)
    manager = PricingManager(SimpleNamespace(), SimpleNamespace())

    def cycle(key=None):
        manager._roll_state_log()
        if key is not None:
            manager._log_state_once(key, "state %s", key)

    cycle("a")
    cycle("a")
    cycle("b")
    cycle()  # state left: nothing logged this cycle
    cycle("b")

    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "state a"),
        (logging.DEBUG, "state a"),
        (logging.INFO, "state b"),
        (logging.INFO, "state b"),
    ]
