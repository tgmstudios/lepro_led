"""Cloud-reported connectivity (new unique ID suffix: <did>_online)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import LeproEntity


async def async_setup_entry(hass, entry, async_add_entities):
    rt = entry.runtime_data
    async_add_entities(LeproOnlineSensor(rt, did) for did in rt.devices)


class LeproOnlineSensor(LeproEntity, BinarySensorEntity):
    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, rt, did):
        super().__init__(rt, did)
        self._attr_unique_id = f"{did}_online"

    @property
    def available(self) -> bool:
        return self._rt.transport is not None and self._rt.transport.connected

    @property
    def is_on(self):
        return self.state_obj.online
