"""Shared admin authority and persisted settings for HA's native controls."""
from .session import Refused


async def set_setting(entity, key, value):
    user_id = entity._context.user_id
    user = await entity.hass.auth.async_get_user(user_id) if user_id else None
    if user is None or not user.is_active or not user.is_admin:
        raise Refused("NOT_AUTHORIZED")
    manager = entity.manager
    candidate = manager.config()
    candidate["settings"][key] = value
    await manager.update(manager.revision, candidate)


async def added(entity):
    def changed():
        entity.async_write_ha_state()
    entity.manager.listeners.add(changed)
    entity.async_on_remove(lambda: entity.manager.listeners.discard(changed))
