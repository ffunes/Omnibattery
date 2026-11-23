"""#942: a Zendure 4000 Mix Pro's MPPT inputs count as connected DC PV.

The coordinator forces ``dc_pv_connected`` False for brands whose traits have
no optional DC PV input, so the hardware capability alone must decide there.
"""

from types import SimpleNamespace

from custom_components.omnibattery.drivers.base import has_connected_mppt_pv
from custom_components.omnibattery.drivers.registry import traits_for_brand


def _coordinator(brand, config, has_mppt_pv, dc_pv_connected):
    return SimpleNamespace(
        traits=traits_for_brand(brand, config),
        capabilities=SimpleNamespace(has_mppt_pv=has_mppt_pv),
        dc_pv_connected=dc_pv_connected,
    )


def test_zendure_mix_pro_mppt_counts_as_connected():
    coord = _coordinator("zendure", {"zendure_model": "4000mix_pro"}, True, False)
    assert has_connected_mppt_pv(coord) is True


def test_zendure_without_mppt_is_not_connected():
    coord = _coordinator("zendure", {}, False, False)
    assert has_connected_mppt_pv(coord) is False


def test_marstek_venus_a_setting_still_applies():
    config = {"battery_version": "vA"}
    assert has_connected_mppt_pv(_coordinator("marstek", config, True, False)) is False
    assert has_connected_mppt_pv(_coordinator("marstek", config, True, True)) is True


def test_doubles_without_traits_keep_historical_behaviour():
    caps = SimpleNamespace(has_mppt_pv=True)
    assert has_connected_mppt_pv(SimpleNamespace(capabilities=caps)) is True
    assert has_connected_mppt_pv(
        SimpleNamespace(capabilities=caps, dc_pv_connected=False)
    ) is False
