"""Pure device-state model: report parsing and command building."""
import copy

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


# ---- command_scene ---------------------------------------------------------
GREEN = (0, 255, 0)


def _scene_state(effect="solid", speed=30, mode=2):
    s = fresh()
    s.apply_report({"d50": p.build_d50([RED] * 25, effect, speed), "d2": mode, "d1": 1, "d52": 500, "d60": p.build_d60("wave_1", 40)})
    return s


def test_scene_full_command_is_single_atomic_payload():
    s = _scene_state()
    cmd = s.command_scene(effect="gradient", colors=[RED, BLUE], speed=70, brightness=128)
    cols = [RED] * 13 + [BLUE] * 12
    assert cmd == {"d1": 1, "d2": 2, "d50": p.build_d50(cols, "gradient", 70), "d52": p.ha_to_d52(128)}
    assert (s.d50_effect, s.colors, s.speed, s.brightness) == ("gradient", cols, 70, 128)


def test_scene_preserves_unprovided_fields_and_sensitivity():
    s = _scene_state("breath", 20)
    sens, d60 = s.sensitivity, s.d60_effect
    cmd = s.command_scene(colors=[GREEN])
    assert cmd["d50"] == p.build_d50([GREEN] * 25, "breath", 20)
    assert (s.sensitivity, s.d60_effect, s.brightness) == (sens, d60, s.brightness) and "d52" not in cmd
    cmd = s.command_scene(speed=90)
    assert cmd["d50"] == p.build_d50([GREEN] * 25, "breath", 90)


def test_scene_brightness_only_sends_only_d52_and_pct_converted():
    s = _scene_state()
    assert s.command_scene(brightness_pct=100) == {"d52": p.ha_to_d52(255)}
    assert s.command_scene(brightness_pct=1) == {"d52": p.ha_to_d52(3)}
    assert s.mode == 2 and s.d50_effect == "solid"


def test_scene_direction_inversion_and_conflicts():
    s = _scene_state("clockwise", 40)
    assert s.direction == "clockwise"
    cmd = s.command_scene(direction="counterclockwise")
    assert cmd["d50"] == p.build_d50([RED] * 25, "counterclockwise", 40) and s.direction == "counterclockwise"
    assert s.command_scene(effect="clockwise", direction="clockwise")["d50"].endswith(p.build_d50([RED] * 25, "clockwise", 40)[-20:])
    with pytest.raises(ValueError):
        s.command_scene(effect="clockwise", direction="counterclockwise")
    with pytest.raises(ValueError):
        s.command_scene(effect="gradient", direction="clockwise")
    g = _scene_state("gradient")
    with pytest.raises(ValueError):
        g.command_scene(direction="clockwise")


def test_scene_music_mode_rejects_implicit_d50_but_allows_effect_and_brightness():
    s = _scene_state(mode=3)
    before = dict(vars(s))
    for kw in ({"colors": [RED]}, {"speed": 10}):
        with pytest.raises(ValueError, match="music"):
            s.command_scene(**kw)
    assert vars(s) == before
    assert s.command_scene(brightness=10) == {"d52": p.ha_to_d52(10)} and s.mode == 3
    cmd = s.command_scene(effect="circular")
    assert cmd["d2"] == 2 and s.mode == 2


@pytest.mark.parametrize("kw", [
    {}, {"effect": "flash"}, {"effect": "wave_1"}, {"effect": "Solid"}, {"effect": 3},
    {"colors": []}, {"colors": [RED] * 26}, {"colors": "red"}, {"colors": [(1, 2)]}, {"colors": [(1, 2, 256)]},
    {"colors": [(True, 0, 0)]}, {"colors": [(1.5, 0, 0)]}, {"colors": [(-1, 0, 0)]},
    {"speed": -1}, {"speed": 101}, {"speed": True}, {"speed": float("nan")}, {"speed": float("inf")}, {"speed": "5"},
    {"brightness": 0}, {"brightness": 256}, {"brightness": True}, {"brightness": 1.5}, {"brightness": float("nan")},
    {"brightness_pct": 0}, {"brightness_pct": 101}, {"brightness_pct": float("nan")}, {"brightness_pct": True},
    {"brightness": 5, "brightness_pct": 5}, {"direction": "left"},
    {"effect": "gradient", "colors": [RED], "speed": 5, "brightness": 999},  # late failure must not leave partial mutation
])
def test_scene_invalid_input_rejected_before_mutation(kw):
    s = _scene_state()
    before = copy.deepcopy(vars(s))
    with pytest.raises(ValueError):
        s.command_scene(**kw)
    assert vars(s) == before


def test_b_series_reports_and_commands_do_not_touch_strip_fields():
    s = DeviceState()
    before = (s.colors, s.d50_effect, s.d50_str)
    assert s.apply_report({"d2": 0, "d3": 500, "d4": 500})
    assert s.b_brightness == 128 and s.b_kelvin == 4600
    assert s.apply_report({"d2": 1, "d5": "000003E80190"})
    assert s.b_rgb == (255, 0, 0) and s.b_rgb_brightness == p.d3_to_ha(400)
    s.apply_report({"d3": "x", "d4": True, "d5": "zz"})  # malformed ignored
    assert (s.colors, s.d50_effect, s.d50_str) == before
    assert s.command_b_turn_on(brightness=100) == {"d1": 1, "d2": 1, "d5": "000003E80190"[:8] + f"{p.ha_to_d3(100):04X}"}
    assert s.command_b_turn_on(rgb=(250, 250, 250)) ["d2"] == 0  # white-like -> white mode
    with pytest.raises(ValueError):
        s.command_b_turn_on(brightness=0)
