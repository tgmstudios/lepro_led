"""Cloud scene selector: lists the strip's server-provided saved scenes and applies the chosen one."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import LeproEntity


async def async_setup_entry(hass, entry, async_add_entities):
    rt = entry.runtime_data
    async_add_entities(LeproCloudSceneSelect(rt, did) for did in rt.devices)


class LeproCloudSceneSelect(LeproEntity, SelectEntity):
    """Options are whatever the cloud catalog returned (possibly none); nothing is invented."""

    _attr_translation_key = "cloud_scene"

    def __init__(self, rt, did):
        super().__init__(rt, did)
        self._attr_unique_id = f"{did}_cloud_scene"

    @property
    def options(self) -> list[str]:
        return [sc.label for sc in self._rt.scenes[self._did].values()]

    @property
    def current_option(self) -> str | None:
        # confirmed only: published but not yet reported by the device shows no selection
        if "d50_str" in self._rt.pending_confirmation(self._did):
            return None
        label = self.state_obj.cloud_scene
        return label if label in self.options else None

    @property
    def extra_state_attributes(self):
        return {"scene_ids": {sc.label: sc.scene_id for sc in self._rt.scenes[self._did].values()}}

    async def async_select_option(self, option: str) -> None:
        scene = next((sc for sc in self._rt.scenes[self._did].values() if sc.label == option), None)
        if scene is None:
            raise HomeAssistantError("unknown cloud scene")
        await self._rt.send(self._did, lambda s: s.command_cloud_scene(scene.d50))
