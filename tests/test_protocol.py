"""Golden-vector tests for the pure wire codecs."""
import json
import pathlib

import pytest

from custom_components.lepro_led import protocol as p

GOLD = json.loads((pathlib.Path(__file__).parent / "fixtures/legacy_golden.json").read_text())


@pytest.mark.parametrize("speed,code", GOLD["speed"])
def test_speed_encode_matches_legacy(speed, code):
    assert p.speed_to_hex(speed) == code


@pytest.mark.parametrize("speed,code", GOLD["speed"])
def test_speed_decode_roundtrip(speed, code):
    assert round(p.hex_to_speed(code)) == speed


@pytest.mark.parametrize("case", GOLD["d50"], ids=lambda c: f"{c['pattern']}-{c['effect']}-{c['speed']}")
def test_d50_build_matches_legacy_bytes(case):
    cols = [tuple(c) for c in case["colors"]]
    assert p.build_d50(cols, case["effect"], case["speed"]) == case["d50"]


@pytest.mark.parametrize("case", GOLD["d50"], ids=lambda c: f"{c['pattern']}-{c['effect']}-{c['speed']}")
def test_d50_parse_roundtrip(case):
    st = p.parse_d50(case["d50"])
    assert st.colors == [tuple(c) for c in case["colors"]]
    assert st.effect == case["effect"]
    if case["effect"] not in ("solid",) and case["speed"] > 0:
        assert abs(st.speed - case["speed"]) <= 1


def test_d50_parse_malformed_returns_none():
    assert p.parse_d50("garbage") is None
    assert p.parse_d50("N01:P10001FF0000F21000") is None  # truncated lengths
    assert p.parse_d50("") is None
    assert p.parse_d50(None) is None


def test_d50_build_requires_25_colors():
    with pytest.raises(ValueError):
        p.build_d50([(1, 2, 3)] * 24, "solid", 50)
    with pytest.raises(ValueError):
        p.build_d50([(1, 2, 300)] * 25, "solid", 50)
    with pytest.raises(ValueError):
        p.build_d50([(1, 2, 3)] * 25, "wave_1", 50)


def test_d60_known_vectors():
    assert p.build_d60("flash", 0) == "2000064000000"
    assert p.build_d60("laser_4", 100) == "2080064630000"
    assert p.build_d60("wave_2", 50) == "2020064320000"  # 50*0x63/100 = 49.5 -> 0x32 (legacy round)


def test_d60_parse():
    assert p.parse_d60("2000064320000") == (51, "flash")
    assert p.parse_d60("2080064630000") == (100, "laser_4")
    assert p.parse_d60("9990064320000") == (51, None)
    assert p.parse_d60("short") is None
    assert p.parse_d60(None) is None


def test_d60_build_validates():
    with pytest.raises(ValueError):
        p.build_d60("solid", 10)
    with pytest.raises(ValueError):
        p.build_d60("flash", 101)
    with pytest.raises(ValueError):
        p.build_d60("flash", -1)


def test_brightness_range_is_app_range_100_1000():
    # APK SetRequestBuilder.dp52RgbIcMaxBrightness is @IntRange(100..1000)
    assert p.ha_to_d52(1) == 100
    assert p.ha_to_d52(255) == 1000
    assert all(100 <= p.ha_to_d52(b) <= 1000 for b in range(1, 256))
    assert p.ha_to_d52(0) == 100  # never emits out-of-range 0; zero means turn_off at entity level
    assert p.ha_to_d52(999) == 1000


def test_brightness_decode_and_legacy_out_of_range():
    assert p.d52_to_ha(1000) == 255
    assert p.d52_to_ha(100) == 1
    assert p.d52_to_ha(50) == 1  # legacy report below app range clamps
    assert p.d52_to_ha(5000) == 255
    assert p.d52_to_ha("abc") is None
    assert p.d52_to_ha(None) is None


def test_brightness_roundtrip_monotonic():
    prev = 0
    for b in range(1, 256):
        d = p.ha_to_d52(b)
        assert d >= prev
        prev = d
        assert abs(p.d52_to_ha(d) - b) <= 1


def test_b_series_codecs_match_upstream_app_captures():
    assert p.build_d5((255, 0, 0), 255) == "000003E803E8"
    assert p.build_d5((0, 255, 0), 255) == "007803E803E8"
    assert p.build_d5((0, 0, 255), 255) == "00F003E803E8"
    assert p.parse_d5("007803E803E8") == ((0, 255, 0), 255)
    assert p.parse_d5("bad") is None and p.parse_d5(123) is None
    assert [p.ha_to_d3(x) for x in (255,)] == [1000]
    assert p.d4_to_kelvin(0) == 2700 and p.d4_to_kelvin(1000) == 6500 and p.kelvin_to_d4(4600) == 500
    assert p.is_b_series("SE1") and p.is_b_series("BC1") and not p.is_b_series("S1-5") and not p.is_b_series(None)
