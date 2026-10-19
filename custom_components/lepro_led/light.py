"""Light platform: main strip light + 25 segment lights for S1-5."""
from __future__ import annotations

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)

from . import protocol as p
from .entity import LeproEntity


async def async_setup_entry(hass, entry, async_add_entities):
    rt = entry.runtime_data
    ents = []
    for did, ctx in rt.devices.items():
        if p.is_b_series(ctx.info.get("series")):
            ents.append(LeproBulbLight(rt, did))
            continue
        ents.append(LeproLedLight(rt, did))
        if "S1-5" in (ctx.info.get("series") or ""):
            ents.extend(LeproSegmentLight(rt, did, i) for i in range(p.SEGMENTS))
    async_add_entities(ents)


class LeproLedLight(LeproEntity, LightEntity):
    _attr_translation_key = "strip"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = list(p.ALL_EFFECTS)

    def __init__(self, rt, did):
        super().__init__(rt, did)
        self._attr_unique_id = did

    @property
    def is_on(self):
        return self.state_obj.on

    @property
    def brightness(self):
        return self.state_obj.brightness

    @property
    def rgb_color(self):
        return self.state_obj.rgb

    @property
    def effect(self):
        return self.state_obj.effect

    @property
    def extra_state_attributes(self):
        s = self.state_obj
        attrs = {"palette": [list(c) for c in s.palette], "effect_speed": round(s.speed), "direction": s.direction}
        # a cloud scene is only reported as active once the device itself reported its exact d50 string
        confirmed = "d50_str" not in self._rt.pending_confirmation(self._did)
        attrs["cloud_scene"] = s.cloud_scene if confirmed else None
        return attrs

    async def async_turn_on(self, **kwargs):
        await self._rt.send(
            self._did,
            lambda s: s.command_turn_on(
                brightness=kwargs.get(ATTR_BRIGHTNESS),
                rgb=tuple(kwargs[ATTR_RGB_COLOR]) if ATTR_RGB_COLOR in kwargs else None,
                effect=kwargs.get(ATTR_EFFECT),
            ),
        )

    async def async_turn_off(self, **kwargs):
        def off(s):
            s.on = False
            return {"d1": 0}

        await self._rt.send(self._did, off)


class LeproSegmentLight(LeproEntity, LightEntity):
    _attr_translation_key = "segment"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, rt, did, index):
        super().__init__(rt, did)
        self._index = index
        self._attr_unique_id = f"{did}_segment_{index + 1:02d}"
        self._attr_translation_placeholders = {"index": f"{index + 1:02d}"}
        self._attr_effect_list = list(p.ALL_EFFECTS)

    @property
    def is_on(self):
        return self.state_obj.on

    @property
    def brightness(self):
        return self.state_obj.brightness

    @property
    def effect(self):
        return self.state_obj.effect

    @property
    def rgb_color(self):
        return self.state_obj.colors[self._index]

    async def async_turn_on(self, **kwargs):
        def build(s):
            if ATTR_RGB_COLOR in kwargs:
                cmd = s.command_segment(self._index, tuple(kwargs[ATTR_RGB_COLOR]))
            else:
                cmd = s.command_turn_on()
            if ATTR_BRIGHTNESS in kwargs:
                s.brightness = int(kwargs[ATTR_BRIGHTNESS])
                cmd["d52"] = p.ha_to_d52(s.brightness)
            return cmd

        await self._rt.send(self._did, build)

    async def async_turn_off(self, **kwargs):
        # Legacy behaviour kept: a segment cannot be off alone, turning it off powers the strip off.
        def off(s):
            s.on = False
            return {"d1": 0}

        await self._rt.send(self._did, off)


class LeproBulbLight(LeproEntity, LightEntity):
    """B-series bulbs and SE1 (ported from upstream 1.4.x): RGB (d2=1/d5) and white/colour temperature (d2=0/d3/d4).

    No effects are offered: only the six strip DIY effects and the d60 effects are evidenced, and only for S1-5.
    """

    _attr_translation_key = "bulb"
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = p.B_KELVIN_MIN
    _attr_max_color_temp_kelvin = p.B_KELVIN_MAX

    def __init__(self, rt, did):
        super().__init__(rt, did)
        self._attr_unique_id = did

    @property
    def is_on(self):
        return self.state_obj.on

    @property
    def color_mode(self):
        return ColorMode.RGB if self.state_obj.mode == 1 else ColorMode.COLOR_TEMP

    @property
    def brightness(self):
        return self.state_obj.b_brightness_now()

    @property
    def rgb_color(self):
        return self.state_obj.b_rgb if self.state_obj.mode == 1 else None

    @property
    def color_temp_kelvin(self):
        s = self.state_obj
        return (s.b_kelvin or 4000) if s.mode != 1 else None

    async def async_turn_on(self, **kwargs):
        await self._rt.send(
            self._did,
            lambda s: s.command_b_turn_on(
                brightness=kwargs.get(ATTR_BRIGHTNESS),
                rgb=tuple(kwargs[ATTR_RGB_COLOR]) if ATTR_RGB_COLOR in kwargs else None,
                kelvin=kwargs.get(ATTR_COLOR_TEMP_KELVIN),
            ),
        )

    async def async_turn_off(self, **kwargs):
        def off(s):
            s.on = False
            return {"d1": 0}

        await self._rt.send(self._did, off)
