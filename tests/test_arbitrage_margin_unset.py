"""An unset Minimum Arbitrage Margin reads 0, so the panel shows its slider.

``unknown`` is what the panel drops as a leftover of a de-configured feature,
and every reader of the margin treats unset and 0 alike (off).
"""
from types import SimpleNamespace

from custom_components.omnibattery.number import MarstekArbitrageNumber


def test_an_unset_arbitrage_margin_reads_as_0_so_the_panel_shows_it():
    entry = SimpleNamespace(data={}, entry_id="entry")
    assert MarstekArbitrageNumber(SimpleNamespace(data={}), entry, "margin").native_value == 0.0
    entry.data = {"min_arbitrage_margin": 0.05}
    assert MarstekArbitrageNumber(SimpleNamespace(data={}), entry, "margin").native_value == 0.05
