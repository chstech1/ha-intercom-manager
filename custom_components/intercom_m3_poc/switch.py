"""Persisted household speech-silence control."""
from homeassistant.components.switch import SwitchEntity
from .manager_entity import added, set_setting


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([SilenceSwitch(entry.runtime_data.manager, entry.entry_id)])


class SilenceSwitch(SwitchEntity):
    _attr_name = "Intercom silence disconnection"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, manager, entry_id):
        self.manager = manager
        self._attr_unique_id = f"{entry_id}_silence_enabled"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        await added(self)

    @property
    def is_on(self):
        return self.manager.configuration["settings"]["silence_enabled"]

    async def async_turn_on(self, **kwargs):
        await set_setting(self, "silence_enabled", True)

    async def async_turn_off(self, **kwargs):
        await set_setting(self, "silence_enabled", False)
