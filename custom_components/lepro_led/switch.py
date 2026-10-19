"""Power-only switch (ported from upstream 1.3.x; unique id <did>_power)."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .entity import LeproEntity


async def async_setup_entry(hass, entry, async_add_entities):
    rt = entry.runtime_data
    async_add_entities(LeproPowerSwitch(rt, did) for did in rt.devices)


class LeproPowerSwitch(LeproEntity, SwitchEntity):
    """Plain on/off. Sends only d1 so colour, effect and brightness are left untouched."""

    _attr_translation_key = "power"

    def __init__(self, rt, did):
        super().__init__(rt, did)
        self._attr_unique_id = f"{did}_power"

    @property
    def is_on(self):
        return self.state_obj.on

    async def async_turn_on(self, **kwargs):
        def on(s):
            s.on = True
            return {"d1": 1}

        await self._rt.send(self._did, on)

    async def async_turn_off(self, **kwargs):
        def off(s):
            s.on = False
            return {"d1": 0}

        await self._rt.send(self._did, off)
