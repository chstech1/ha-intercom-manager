"""Single removable PoC room status entity."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DOMAIN, Runtime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([IntercomM3PocSensor(entry.runtime_data)])


class IntercomM3PocSensor(SensorEntity):
    """Status only; backend selection is configuration, not hardware proof."""

    _attr_has_entity_name = False
    # Preserve the M3 entity id used by the explicit permission boundary.
    _attr_name = "Intercom M3 PoC Simulated Room"
    _attr_unique_id = f"{DOMAIN}_simulated_room"
    _attr_icon = "mdi:phone-in-talk"

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.add_status_listener(self._status_changed))

    @callback
    def _status_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        status = self._runtime.room_status()
        if status["busy"]:
            return "muted" if status["muted"] else "busy"
        available = status.get("available")
        return "available" if available is True else "unavailable" if available is False else "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "room_name": self._runtime.room_name,
            "backend": self._runtime.backend,
            **self._runtime.room_status(),
        }
