"""Per-device state model and DP command builders (no Home Assistant imports)."""
from __future__ import annotations

import math

from . import protocol as p

WHITE = (255, 255, 255)
SPEED_EFFECTS = ("breath", "gradient", "clockwise", "counterclockwise", "circular")


def _check_rgb(c):
    if len(c) != 3 or any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 255 for v in c):
        raise ValueError(f"invalid RGB colour {c!r}")
    return tuple(c)


DIRECTIONAL = {"clockwise": "clockwise", "counterclockwise": "counterclockwise"}
SCENE_EFFECTS = p.D50_EFFECTS  # only the six verified DIY (d50) effects


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class DeviceState:
    def __init__(self) -> None:
        self.on: bool | None = None
        self.mode: int | None = None
        self.brightness: int | None = None  # HA scale 1..255
        self.colors: list = [WHITE] * p.SEGMENTS
        self.d50_effect: str = p.EFFECT_SOLID
        self.d60_effect: str | None = None
        self.speed: float = 50
        self.sensitivity: float = 50
        self.online: bool | None = None
        self.touched: set[str] = set()  # attribute names written by the last apply_report

    # ---- derived ---------------------------------------------------------
    @property
    def effect(self) -> str:
        if self.mode == 3:
            return self.d60_effect or "flash"
        return self.d50_effect

    @property
    def direction(self) -> str | None:
        """Rotation direction of the current d50 effect (verified types 4/3), else None."""
        return self.d50_effect if self.d50_effect in DIRECTIONAL and self.mode != 3 else None

    @property
    def palette(self) -> list:
        """Distinct colours of the d50 segment string, in order."""
        out: list = []
        for c in self.colors:
            if not out or out[-1] != c:
                out.append(c)
        return out

    @property
    def rgb(self):
        return self.colors[0]

    # ---- inbound ----------------------------------------------------------
    def apply_report(self, data: dict) -> bool:
        """Merge a device report. Returns True if any known key was present. Malformed values are ignored."""
        changed = False
        self.touched = touched = set()
        if "d1" in data and data["d1"] in (0, 1, True, False):
            self.on = bool(data["d1"])
            touched.add("on")
            changed = True
        if "d2" in data and isinstance(data["d2"], int) and not isinstance(data["d2"], bool):
            self.mode = data["d2"]
            touched.add("mode")
            changed = True
        if "d52" in data:
            b = p.d52_to_ha(data["d52"])
            if b is not None:
                self.brightness = b
                touched.add("brightness")
                changed = True
        if "d50" in data:
            parsed = p.parse_d50(data["d50"])
            if parsed is not None:
                self.colors = parsed.colors
                self.d50_effect = parsed.effect
                touched.update(("colors", "d50_effect"))
                if parsed.effect != "solid":  # solid carries no speed; keep the last one
                    self.speed = parsed.speed
                    touched.add("speed")
                changed = True
        if "d60" in data:
            parsed60 = p.parse_d60(data["d60"])
            if parsed60 is not None:
                self.sensitivity = parsed60[0]
                touched.add("sensitivity")
                if parsed60[1]:
                    self.d60_effect = parsed60[1]
                    touched.add("d60_effect")
                changed = True
        if "online" in data and data["online"] in (0, 1, True, False):
            self.online = bool(data["online"])
            touched.add("online")
            changed = True
        return changed

    # ---- outbound ---------------------------------------------------------
    def _d50_payload(self, power: bool) -> dict:
        cmd = {"d2": 2, "d50": p.build_d50(self.colors, self.d50_effect, self.speed)}
        if power:
            cmd = {"d1": 1, **cmd}
        self.mode = 2
        return cmd

    def _d60_payload(self) -> dict:
        self.mode = 3
        return {"d1": 1, "d2": 3, "d60": p.build_d60(self.d60_effect, self.sensitivity)}

    def command_turn_on(self, brightness=None, rgb=None, effect=None) -> dict:
        if effect is not None:
            norm = p.normalize_effect(effect)
            if norm is None:
                raise ValueError(f"unknown effect {effect!r}")
            effect = norm
        if rgb is not None:
            rgb = _check_rgb(rgb)
        if brightness is not None and not 1 <= int(brightness) <= 255:
            raise ValueError("brightness must be 1..255")

        if effect in p.D60_EFFECTS:
            self.d60_effect = effect
            cmd = self._d60_payload()
        elif effect in p.D50_EFFECTS or rgb is not None:
            if effect is not None:
                self.d50_effect = effect
            if rgb is not None:
                self.colors = [rgb] * p.SEGMENTS
            cmd = self._d50_payload(power=True)
        else:
            cmd = {"d1": 1}
        if brightness is not None:
            self.brightness = int(brightness)
            cmd["d52"] = p.ha_to_d52(self.brightness)
        self.on = True
        return cmd

    def command_segment(self, index: int, rgb) -> dict:
        if not 0 <= index < p.SEGMENTS:
            raise ValueError("segment index out of range")
        rgb = _check_rgb(rgb)
        colors = list(self.colors)
        colors[index] = rgb
        self.colors = colors
        self.on = True
        return self._d50_payload(power=True)

    def command_segments(self, colors) -> dict:
        n = len(colors)
        if not 1 <= n <= p.SEGMENTS:
            raise ValueError("provide 1..25 colours")
        cols = [_check_rgb(c) for c in colors]
        base, extra = divmod(p.SEGMENTS, n)
        out = []
        for i, c in enumerate(cols):  # leading blocks get the remainder
            out.extend([c] * (base + (1 if i < extra else 0)))
        self.colors = out
        self.on = True
        return self._d50_payload(power=True)

    def command_speed(self, speed: float):
        if not isinstance(speed, (int, float)) or not 0 <= speed <= 100:
            raise ValueError("speed must be 0..100")
        self.speed = speed
        if self.effect not in SPEED_EFFECTS or self.mode != 2:
            return None
        return self._d50_payload(power=False)

    def command_sensitivity(self, value: float):
        if not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ValueError("sensitivity must be 0..100")
        self.sensitivity = value
        if self.mode != 3 or self.d60_effect is None:
            return None
        return {"d2": 3, "d60": p.build_d60(self.d60_effect, value)}

    def command_scene(self, effect=None, colors=None, speed=None, brightness=None, brightness_pct=None, direction=None) -> dict:
        """Atomic scene: optional d50 effect/palette/speed/direction plus optional brightness in ONE command.

        All input is validated before any attribute is touched. Unprovided fields, sensitivity and the
        d60 music settings are preserved. While in music mode (d2=3) a d50 change needs an explicit
        ``effect`` (no silent fallback to an old palette); a brightness-only scene is still allowed.
        """
        if all(v is None for v in (effect, colors, speed, brightness, brightness_pct, direction)):
            raise ValueError("provide at least one of effect, colors, speed, brightness, brightness_pct, direction")
        if brightness is not None and brightness_pct is not None:
            raise ValueError("use either brightness or brightness_pct, not both")
        if effect is not None and effect not in SCENE_EFFECTS:
            raise ValueError(f"effect must be one of {', '.join(SCENE_EFFECTS)}")
        if direction is not None and direction not in DIRECTIONAL:
            raise ValueError("direction must be clockwise or counterclockwise")
        new_colors = None
        if colors is not None:
            if isinstance(colors, (str, bytes)) or not hasattr(colors, "__len__"):
                raise ValueError("colors must be a list of RGB colours")
            if not 1 <= len(colors) <= p.SEGMENTS:
                raise ValueError("provide 1..25 colours")
            cols = [_check_rgb(c) for c in colors]
            base, extra = divmod(p.SEGMENTS, len(cols))
            new_colors = []
            for i, c in enumerate(cols):
                new_colors.extend([c] * (base + (1 if i < extra else 0)))
        if speed is not None and (not _is_number(speed) or not 0 <= speed <= 100):
            raise ValueError("speed must be 0..100")
        bri = None
        if brightness is not None:
            if not isinstance(brightness, int) or isinstance(brightness, bool) or not 1 <= brightness <= 255:
                raise ValueError("brightness must be an integer 1..255")
            bri = brightness
        if brightness_pct is not None:
            if not _is_number(brightness_pct) or not 1 <= brightness_pct <= 100:
                raise ValueError("brightness_pct must be 1..100")
            bri = max(1, min(255, round(brightness_pct * 255 / 100)))

        d50_change = any(v is not None for v in (effect, colors, speed, direction))
        if self.mode == 3 and d50_change and effect is None:
            raise ValueError("device is in music mode; give an explicit effect to leave it")
        target = effect if effect is not None else self.d50_effect
        if direction is not None:
            if effect is None:
                if target not in DIRECTIONAL:
                    raise ValueError("direction only applies to clockwise/counterclockwise effects")
            elif effect not in DIRECTIONAL:
                raise ValueError("direction only applies to clockwise/counterclockwise effects")
            elif effect != direction:
                raise ValueError(f"direction {direction!r} conflicts with effect {effect!r}")
            target = direction

        cmd: dict = {}
        if d50_change:
            self.d50_effect = target
            if new_colors is not None:
                self.colors = new_colors
            if speed is not None:
                self.speed = speed
            cmd = self._d50_payload(power=True)
            self.on = True
        if bri is not None:
            self.brightness = bri
            cmd["d52"] = p.ha_to_d52(bri)
        return cmd
