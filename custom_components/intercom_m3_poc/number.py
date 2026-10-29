"""Persisted household speech-silence interval."""
from homeassistant.components.number import NumberEntity, NumberMode
from .manager_entity import added, set_setting


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([SilenceSeconds(entry.runtime_data.manager, entry.entry_id)])


class SilenceSeconds(NumberEntity):
    _attr_name = "Intercom silence interval"
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = 5
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(self, manager, entry_id):
        self.manager = manager
        self._attr_unique_id = f"{entry_id}_silence_seconds"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        await added(self)

    @property
    def native_value(self):
        return self.manager.configuration["settings"]["silence_seconds"]

    async def async_set_native_value(self, value):
        if isinstance(value, bool) or int(value) != value:
            raise ValueError("silence interval must be an integer")
        await set_setting(self, "silence_seconds", int(value))
