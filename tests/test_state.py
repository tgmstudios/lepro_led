"""Pure device-state model: report parsing and command building."""
import pytest

from custom_components.lepro_led import protocol as p
from custom_components.lepro_led.state import DeviceState

RED = (255, 0, 0)
BLUE = (0, 0, 255)


def fresh():
    return DeviceState()


def test_report_updates_power_mode_brightness():
    s = fresh()
    assert s.apply_report({"d1": 1, "d2": 2, "d52": 1000})
    assert (s.on, s.mode, s.brightness) == (True, 2, 255)
    assert s.apply_report({"d1": 0})
    assert s.on is False
    assert s.brightness == 255  # unrelated state preserved


def test_report_without_known_keys_is_not_a_change():
    assert not fresh().apply_report({"foo": 1})


def test_d50_report_sets_colors_effect_speed():
    s = fresh()
    d50 = p.build_d50([RED] * 10 + [BLUE] * 15, "breath", 40)
    s.apply_report({"d50": d50, "d2": 2})
    assert s.colors == [RED] * 10 + [BLUE] * 15
    assert s.effect == "breath"
    assert abs(s.speed - 40) <= 1


def test_solid_report_keeps_previous_speed():
    s = fresh()
    s.speed = 77
    s.apply_report({"d50": p.build_d50([RED] * 25, "solid", 0), "d2": 2})
    assert s.speed == 77


def test_malformed_d50_preserves_state():
    s = fresh()
    s.apply_report({"d50": p.build_d50([RED] * 25, "gradient", 30), "d2": 2})
    before = (list(s.colors), s.effect, s.speed)
    s.apply_report({"d50": "N01:P1000", "d60": "xx"})
    assert (s.colors, s.effect, s.speed) == before


def test_d60_report_sets_sensitivity_and_effect_in_mode3():
    s = fresh()
    s.apply_report({"d2": 3, "d60": p.build_d60("wave_2", 80)})
    assert s.effect == "wave_2"
    assert abs(s.sensitivity - 80) <= 1


def test_mode3_without_parsable_effect_defaults_flash_but_keeps_d50_effect_for_mode2():
    s = fresh()
    s.apply_report({"d50": p.build_d50([RED] * 25, "circular", 10), "d2": 2})
    s.apply_report({"d2": 3})
    assert s.effect == "flash"
    s.apply_report({"d2": 2})
    assert s.effect == "circular"


def test_online_report():
    s = fresh()
    assert s.online is None
    s.apply_report({"online": 0})
    assert s.online is False
    s.apply_report({"online": 1})
    assert s.online is True
    s.apply_report({"online": "garbage"})
    assert s.online is True


def test_turn_on_plain_sends_only_power():
    assert fresh().command_turn_on() == {"d1": 1}


def test_turn_on_brightness_only_sends_power_and_d52():
    s = fresh()
    assert s.command_turn_on(brightness=255) == {"d1": 1, "d52": 1000}
    assert s.command_turn_on(brightness=1) == {"d1": 1, "d52": 100}


def test_turn_on_rgb_sets_all_segments_and_d50():
    s = fresh()
    cmd = s.command_turn_on(rgb=RED)
    assert cmd["d1"] == 1 and cmd["d2"] == 2
    assert cmd["d50"] == p.build_d50([RED] * 25, "solid", s.speed)
    assert "d52" not in cmd


def test_turn_on_effect_d60_uses_sensitivity():
    s = fresh()
    s.sensitivity = 100
    cmd = s.command_turn_on(effect="laser_4", brightness=128)
    assert cmd == {"d1": 1, "d2": 3, "d60": p.build_d60("laser_4", 100), "d52": p.ha_to_d52(128)}


def test_turn_on_unknown_effect_rejected():
    with pytest.raises(ValueError):
        fresh().command_turn_on(effect="disco")


def test_rgb_during_special_effect_switches_to_last_d50_effect():
    s = fresh()
    s.apply_report({"d50": p.build_d50([RED] * 25, "breath", 20), "d2": 2})
    s.apply_report({"d2": 3, "d60": p.build_d60("flash", 50)})
    cmd = s.command_turn_on(rgb=BLUE)
    assert cmd["d2"] == 2
    assert s.effect == "breath"


def test_segment_change_updates_single_segment():
    s = fresh()
    s.apply_report({"d50": p.build_d50([RED] * 25, "solid", 0), "d2": 2})
    cmd = s.command_segment(3, BLUE)
    colors = [RED] * 25
    colors[3] = BLUE
    assert cmd["d50"] == p.build_d50(colors, "solid", s.speed)
    with pytest.raises(ValueError):
        s.command_segment(25, BLUE)


def test_set_segments_even_blocks_and_validation():
    s = fresh()
    cmd = s.command_segments([RED, BLUE])
    assert s.colors == [RED] * 13 + [BLUE] * 12
    assert cmd["d50"] == p.build_d50(s.colors, "solid", s.speed)
    cmd = s.command_segments([RED] * 25)
    assert s.colors == [RED] * 25
    with pytest.raises(ValueError):
        s.command_segments([])
    with pytest.raises(ValueError):
        s.command_segments([RED] * 26)
    with pytest.raises(ValueError):
        s.command_segments([(1, 2, 999)])


def test_speed_change_only_sends_for_speed_bearing_effects():
    s = fresh()
    s.apply_report({"d50": p.build_d50([RED] * 25, "breath", 20), "d2": 2, "d1": 1})
    cmd = s.command_speed(60)
    assert cmd["d50"] == p.build_d50([RED] * 25, "breath", 60) and "d1" not in cmd
    s.apply_report({"d50": p.build_d50([RED] * 25, "solid", 0), "d2": 2})
    assert s.command_speed(10) is None and s.speed == 10  # stored, nothing on wire
    with pytest.raises(ValueError):
        s.command_speed(101)


def test_sensitivity_change_sends_only_for_d60_effects():
    s = fresh()
    s.apply_report({"d2": 3, "d60": p.build_d60("wave_1", 10), "d1": 1})
    assert s.command_sensitivity(90)["d60"] == p.build_d60("wave_1", 90)
    s.apply_report({"d50": p.build_d50([RED] * 25, "solid", 0), "d2": 2})
    assert s.command_sensitivity(20) is None and s.sensitivity == 20
    with pytest.raises(ValueError):
        s.command_sensitivity(-1)
