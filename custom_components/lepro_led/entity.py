from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .runtime import LeproRuntime


def device_info(ctx) -> dict:
    info = ctx.info
    out = {
        "identifiers": {(DOMAIN, ctx.did)},
        "name": info.get("name") or f"Lepro {ctx.did}",
        "manufacturer": "Lepro",
        "model": info.get("series", "Lepro LED"),
    }
    if info.get("fwVersion"):
        out["sw_version"] = str(info["fwVersion"])
    if info.get("hwVersion"):
        out["hw_version"] = str(info["hwVersion"])
    return out


class LeproEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: LeproRuntime, did: str):
        self._rt = runtime
        self._did = did
        self._attr_device_info = device_info(runtime.devices[did])

    @property
    def state_obj(self):
        return self._rt.devices[self._did].state

    @property
    def available(self) -> bool:
        return self._rt.available(self._did)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._rt.add_listener(self._did, self._on_update))

    def _on_update(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()
