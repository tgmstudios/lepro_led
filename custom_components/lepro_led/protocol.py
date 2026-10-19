"""Pure wire codecs for Lepro S1-5 style RGBIC strips (no Home Assistant imports).

Every format here is either (a) byte-identical to what the pre-existing
integration already sent (golden-tested against commit da95907) or (b) a range
taken from the app's own model classes (d52 100..1000, d5 4-hex HSV 0..1000).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

SEGMENTS = 25

EFFECT_SOLID = "solid"
D50_EFFECTS = ("solid", "breath", "gradient", "clockwise", "counterclockwise", "circular")
D60_PREFIX = {
    "flash": "2000064",
    "wave_1": "2010064",
    "wave_2": "2020064",
    "wave_3": "2030064",
    "wave_4": "2040064",
    "laser_1": "2050064",
    "laser_2": "2060064",
    "laser_3": "2070064",
    "laser_4": "2080064",
}
D60_EFFECTS = tuple(D60_PREFIX)
ALL_EFFECTS = D50_EFFECTS + D60_EFFECTS

D52_MIN, D52_MAX = 100, 1000  # APK SetRequestBuilder.dp52RgbIcMaxBrightness @IntRange
_D60_MAX_HEX = 0x63


def normalize_effect(name: str | None) -> str | None:
    if name is None:
        return None
    return name if name in ALL_EFFECTS else None


# ---- speed -------------------------------------------------------------
def speed_to_hex(speed: float) -> str:
    if speed <= 0:
        return "1000"
    raw = int(round(-117.41 * math.log(speed + 1) + 597.75))
    return f"0{raw:03X}"


def hex_to_speed(code: str) -> float:
    code = code.upper()
    if code == "1000":
        return 0.0
    raw = int(code[1:], 16)
    return float(max(0, min(100, math.exp((raw - 597.75) / -117.41) - 1)))


# ---- d50 ---------------------------------------------------------------
def _effect_tail(effect: str, speed: float) -> str:
    sp = speed_to_hex(speed)
    if effect == "solid":
        return "000640000E1"
    if effect == "breath":
        return "000640000E4" + sp + "0000" + sp + "1664"
    if effect == "gradient":
        return "100640000E3" + sp + "C2O6" + sp
    if effect == "clockwise":
        return "00164" + sp + "E1"
    if effect == "counterclockwise":
        return "00264" + sp + "E1"
    if effect == "circular":
        return "100640000E1" + "C2O6" + sp
    raise ValueError(f"not a d50 effect: {effect!r}")


def build_d50(colors, effect: str, speed: float) -> str:
    if effect not in D50_EFFECTS:
        raise ValueError(f"not a d50 effect: {effect!r}")
    if len(colors) != SEGMENTS:
        raise ValueError(f"need exactly {SEGMENTS} segment colours, got {len(colors)}")
    for c in colors:
        if len(c) != 3 or any(not isinstance(v, int) or not 0 <= v <= 255 for v in c):
            raise ValueError(f"invalid RGB colour {c!r}")
    groups: list[list] = []
    for col in colors:
        if groups and groups[-1][0] == tuple(col):
            groups[-1][1] += 1
        else:
            groups.append([tuple(col), 1])
    n = len(groups)
    cols = "".join(f"{r:02X}{g:02X}{b:02X}" for (r, g, b), _ in groups)
    lens = "".join(f"{cnt:04X}" for _, cnt in groups)
    return f"N01:P1000{n}{cols}F21000{n}{lens}U3V3{_effect_tail(effect, speed)};"


@dataclass
class D50State:
    colors: list
    effect: str
    speed: float


def parse_d50(d50) -> D50State | None:
    """Parse a d50 string. Returns None on malformed/partial input (callers keep prior state)."""
    if not isinstance(d50, str) or not d50:
        return None
    colors = None
    p_idx = d50.find("P1000")
    if p_idx == -1:
        m = re.search(r"P10001([0-9A-F]{6})", d50)
        if not m:
            return None
        h = m.group(1)
        colors = [(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))] * SEGMENTS
    else:
        f_idx = d50.find("F21000", p_idx)
        if f_idx == -1:
            return None
        block = d50[p_idx + 5 : f_idx]
        n = k = None
        for kk in range(1, 4):
            if len(block) >= kk and block[:kk].isdigit():
                cand = int(block[:kk])
                if cand > 0 and len(block) - kk == 6 * cand:
                    n, k = cand, kk
                    break
        if n is None:
            return None
        try:
            hexcols = block[k:]
            rgb = [
                (int(hexcols[i * 6 : i * 6 + 2], 16), int(hexcols[i * 6 + 2 : i * 6 + 4], 16), int(hexcols[i * 6 + 4 : i * 6 + 6], 16))
                for i in range(n)
            ]
            lens_hex = d50[f_idx + 6 + k : f_idx + 6 + k + n * 4]
            if len(lens_hex) < n * 4:
                return None
            lens = [int(lens_hex[i * 4 : i * 4 + 4], 16) for i in range(n)]
        except ValueError:
            return None
        segs = []
        for col, cnt in zip(rgb, lens):
            segs.extend([col] * cnt)
        if not segs:
            return None
        segs = (segs + [segs[-1]] * SEGMENTS)[:SEGMENTS]
        colors = segs

    effect, speed = EFFECT_SOLID, 50.0
    if "000640000E1" in d50:
        effect = "solid"
    elif m := re.search(r"000640000E4([0-9A-F]{4})0000[0-9A-F]{4}1664", d50):
        effect, speed = "breath", hex_to_speed(m.group(1))
    elif m := re.search(r"100640000E3([0-9A-F]{4})C2O6[0-9A-F]{4}", d50):
        effect, speed = "gradient", hex_to_speed(m.group(1))
    elif m := re.search(r"00164([0-9A-F]{4})E1", d50):
        effect, speed = "clockwise", hex_to_speed(m.group(1))
    elif m := re.search(r"00264([0-9A-F]{4})E1", d50):
        effect, speed = "counterclockwise", hex_to_speed(m.group(1))
    elif m := re.search(r"100640000E1C2O6([0-9A-F]{4})", d50):
        effect, speed = "circular", hex_to_speed(m.group(1))
    return D50State(colors, effect, speed)


# ---- d60 ---------------------------------------------------------------
def build_d60(effect: str, sensitivity: int) -> str:
    if effect not in D60_PREFIX:
        raise ValueError(f"not a d60 effect: {effect!r}")
    if not isinstance(sensitivity, (int, float)) or not 0 <= sensitivity <= 100:
        raise ValueError("sensitivity must be 0..100")
    hv = max(0, min(_D60_MAX_HEX, int(round(sensitivity * _D60_MAX_HEX / 100))))
    return f"{D60_PREFIX[effect]}{hv:02X}0000"


def parse_d60(d60):
    """Return (sensitivity_percent, effect_or_None) or None if malformed."""
    if not isinstance(d60, str) or len(d60) < 9:
        return None
    try:
        val = int(d60[7:9], 16)
    except ValueError:
        return None
    pct = max(0, min(100, int(round(val * 100 / _D60_MAX_HEX))))
    rev = {v: k for k, v in D60_PREFIX.items()}
    return pct, rev.get(d60[0:7])


# ---- d52 brightness (app range 100..1000) -----------------------------------
def ha_to_d52(ha: int) -> int:
    ha = max(1, min(255, int(ha)))
    return D52_MIN + int(round((ha - 1) * (D52_MAX - D52_MIN) / 254))


def d52_to_ha(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    v = max(D52_MIN, min(D52_MAX, v))
    return 1 + int(round((v - D52_MIN) * 254 / (D52_MAX - D52_MIN)))
