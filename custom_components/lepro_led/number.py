"""Speed and sensitivity numbers (unique IDs unchanged: <did>_speed / <did>_sensitivity)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode

from .entity import LeproEntity


async def async_setup_entry(hass, entry, async_add_entities):
    rt = entry.runtime_data
    ents = []
    for did in rt.devices:
        ents += [LeproSpeedNumber(rt, did), LeproSensitivityNumber(rt, did)]
    async_add_entities(ents)


class _Base(LeproEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER


class LeproSpeedNumber(_Base):
    _attr_translation_key = "speed"

    def __init__(self, rt, did):
        super().__init__(rt, did)
        self._attr_unique_id = f"{did}_speed"

    @property
    def native_value(self):
        return float(round(self.state_obj.speed))

    async def async_set_native_value(self, value: float) -> None:
        await self._rt.send(self._did, lambda s: s.command_speed(int(round(value))))


class LeproSensitivityNumber(_Base):
    """Effect sensitivity for d60 effects. The S1-5 has no device microphone; this is NOT a music-sync control."""

    _attr_translation_key = "sensitivity"

    def __init__(self, rt, did):
        super().__init__(rt, did)
        self._attr_unique_id = f"{did}_sensitivity"

    @property
    def native_value(self):
        return float(round(self.state_obj.sensitivity))

    async def async_set_native_value(self, value: float) -> None:
        await self._rt.send(self._did, lambda s: s.command_sensitivity(int(round(value))))
