"""Versioned manager metadata and shared admission. No media or credentials in history."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
import secrets
import time
import uuid
from zoneinfo import ZoneInfo

from homeassistant.helpers.storage import Store
from homeassistant.core import callback

from .session import Refused

DEFAULT_SETTINGS = {
    "silence_enabled": True, "silence_seconds": 60,
    "silence_call_types": ["room", "conference"], "history_enabled": True,
    "history_retention_days": 30,
    "automation_bindings": [],
}
DEFAULT_QUIET = {"enabled": False, "days": list(range(7)), "start": "22:00", "end": "07:00"}
DEFAULT_VOLUMES = {"initial": None, "maximum": None, "chime": None,
                   "announcement": None, "calibrated": False}
CONFIG_KEYS = {"rooms", "groups", "permissions", "settings"}
ROOM_KEYS = {"room_id", "name", "area_id", "aliases", "backend", "endpoint_ref",
             "dnd", "quiet_hours", "favorite_room_id", "volumes"}
LOCAL_DEFAULTS = {"allowed_destination_ids": None, "local_user_id": None, "ha_device_id": None,
                  "assist_pipeline_id": None, "stt_entity_id": None}
SAFE_CODE = re.compile(r"^[A-Z0-9_]{1,40}$")


def text(value, maximum=64):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum or not value.isprintable():
        raise Refused("INVALID_CONFIGURATION")
    return value.strip()


def identity(value):
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise Refused("INVALID_CONFIGURATION") from None
    if parsed != value:
        raise Refused("INVALID_CONFIGURATION")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise Refused("INVALID_CONFIGURATION")
    return value


def boolean(value):
    if type(value) is not bool:
        raise Refused("INVALID_CONFIGURATION")
    return value


def room_record(room_id, name, backend, endpoint_ref=None):
    return {"room_id": room_id, "name": name, "backend": backend,
            "endpoint_ref": endpoint_ref, "area_id": None, "aliases": [], "dnd": False,
            "quiet_hours": deepcopy(DEFAULT_QUIET), "favorite_room_id": None,
            "volumes": deepcopy(DEFAULT_VOLUMES), **deepcopy(LOCAL_DEFAULTS)}


def validate_configuration(value, profiles):
    if not isinstance(value, dict) or set(value) - (CONFIG_KEYS | {"revision"}) or not CONFIG_KEYS <= value.keys():
        raise Refused("INVALID_CONFIGURATION")
    result = deepcopy(value)
    result.pop("revision", None)
    rooms, groups, grants = result["rooms"], result["groups"], result["permissions"]
    if not isinstance(rooms, list) or len(rooms) > 64 or not isinstance(groups, list) or len(groups) > 64 or not isinstance(grants, list) or len(grants) > 256:
        raise Refused("INVALID_CONFIGURATION")
    ids, aliases, refs = set(), set(), set()
    for room in rooms:
        if not isinstance(room, dict) or set(room) - (ROOM_KEYS | LOCAL_DEFAULTS.keys()) or not ROOM_KEYS <= room.keys():
            raise Refused("INVALID_CONFIGURATION")
        for key, default in LOCAL_DEFAULTS.items():
            room.setdefault(key, deepcopy(default))
        room_id = identity(room["room_id"])
        if room_id in ids:
            raise Refused("DUPLICATE_ROOM")
        ids.add(room_id)
        room["name"] = text(room["name"])
        if room["area_id"] is not None:
            room["area_id"] = text(room["area_id"])
        names = room["aliases"]
        if not isinstance(names, list) or len(names) > 16:
            raise Refused("INVALID_CONFIGURATION")
        for alias in [room["name"], *names]:
            normalized = text(alias).casefold()
            if normalized in aliases:
                raise Refused("AMBIGUOUS_ALIAS")
            aliases.add(normalized)
        if room["backend"] not in ("simulation", "real_endpoint"):
            raise Refused("INVALID_CONFIGURATION")
        if room["backend"] == "real_endpoint":
            ref = room["endpoint_ref"]
            if not isinstance(ref, str) or ref not in profiles:
                raise Refused("UNKNOWN_ENDPOINT_PROFILE")
            if ref in refs:
                raise Refused("DUPLICATE_ENDPOINT")
            refs.add(ref)
        elif room["endpoint_ref"] is not None:
            raise Refused("INVALID_CONFIGURATION")
        boolean(room["dnd"])
        quiet = room["quiet_hours"]
        if not isinstance(quiet, dict) or set(quiet) != set(DEFAULT_QUIET):
            raise Refused("INVALID_CONFIGURATION")
        boolean(quiet["enabled"])
        if not isinstance(quiet["days"], list) or len(set(quiet["days"])) != len(quiet["days"]):
            raise Refused("INVALID_CONFIGURATION")
        for day in quiet["days"]:
            integer(day, 0, 6)
        for key in ("start", "end"):
            if not isinstance(quiet[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", quiet[key]):
                raise Refused("INVALID_CONFIGURATION")
        volumes = room["volumes"]
        if not isinstance(volumes, dict) or set(volumes) != set(DEFAULT_VOLUMES):
            raise Refused("INVALID_CONFIGURATION")
        if volumes != DEFAULT_VOLUMES:
            if room["backend"] != "real_endpoint" or getattr(profiles[room["endpoint_ref"]], "endpoint_profile", 3) != 4:
                raise Refused("VOLUME_UNSUPPORTED")
            if volumes["calibrated"] is not True:
                raise Refused("CALIBRATION_REQUIRED")
            for key in ("initial", "maximum", "chime", "announcement"):
                integer(volumes[key], 5, 100)
            if volumes["initial"] > volumes["maximum"] or volumes["announcement"] > volumes["maximum"]:
                raise Refused("INVALID_VOLUME")
    for room in rooms:
        if room["favorite_room_id"] is not None and (room["favorite_room_id"] not in ids or room["favorite_room_id"] == room["room_id"]):
            raise Refused("UNKNOWN_ROOM")
        destinations = room["allowed_destination_ids"]
        if destinations is not None and (not isinstance(destinations, list) or len(set(destinations)) != len(destinations) or not set(destinations) <= ids or room["room_id"] in destinations):
            raise Refused("INVALID_DIRECTIONAL_POLICY")
        for key in ("local_user_id", "ha_device_id", "assist_pipeline_id", "stt_entity_id"):
            if room[key] is not None:
                room[key] = text(room[key], 128)
    group_ids = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"group_id", "name", "room_ids"}:
            raise Refused("INVALID_CONFIGURATION")
        group_id = identity(group["group_id"])
        if group_id in group_ids:
            raise Refused("DUPLICATE_GROUP")
        group_ids.add(group_id)
        group["name"] = text(group["name"])
        members = group["room_ids"]
        if not isinstance(members, list) or not members or len(members) != len(set(members)) or not set(members) <= ids:
            raise Refused("UNKNOWN_ROOM")
    users = set()
    for grant in grants:
        if not isinstance(grant, dict) or set(grant) != {"user_id", "room_ids", "actions"}:
            raise Refused("INVALID_CONFIGURATION")
        user = text(grant["user_id"], 128)
        if user in users:
            raise Refused("DUPLICATE_PERMISSION")
        users.add(user)
        if not isinstance(grant["room_ids"], list) or len(set(grant["room_ids"])) != len(grant["room_ids"]) or not set(grant["room_ids"]) <= ids:
            raise Refused("UNKNOWN_ROOM")
        if not isinstance(grant["actions"], list) or len(set(grant["actions"])) != len(grant["actions"]) or not set(grant["actions"]) <= {"call", "history", "announce"}:
            raise Refused("INVALID_CONFIGURATION")
    settings = result["settings"]
    if isinstance(settings, dict):
        settings.setdefault("automation_bindings", [])
    if not isinstance(settings, dict) or set(settings) != set(DEFAULT_SETTINGS):
        raise Refused("INVALID_CONFIGURATION")
    boolean(settings["silence_enabled"])
    boolean(settings["history_enabled"])
    integer(settings["silence_seconds"], 5, 3600)
    integer(settings["history_retention_days"], 1, 365)
    if not isinstance(settings["silence_call_types"], list) or len(set(settings["silence_call_types"])) != len(settings["silence_call_types"]) or not set(settings["silence_call_types"]) <= {"room", "conference", "browser"}:
        raise Refused("INVALID_CONFIGURATION")
    bindings = settings["automation_bindings"]
    if not isinstance(bindings, list) or len(bindings) > 128:
        raise Refused("INVALID_AUTOMATION_BINDING")
    automation_ids = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {"automation_entity_id", "user_id", "room_ids", "actions"}:
            raise Refused("INVALID_AUTOMATION_BINDING")
        automation_id = text(binding["automation_entity_id"], 128)
        if not re.fullmatch(r"automation\.[a-z0-9_]+", automation_id) or automation_id in automation_ids:
            raise Refused("INVALID_AUTOMATION_BINDING")
        automation_ids.add(automation_id)
        text(binding["user_id"], 128)
        if not isinstance(binding["room_ids"], list) or not binding["room_ids"] or len(set(binding["room_ids"])) != len(binding["room_ids"]) or not set(binding["room_ids"]) <= ids:
            raise Refused("INVALID_AUTOMATION_BINDING")
        if not isinstance(binding["actions"], list) or not binding["actions"] or len(set(binding["actions"])) != len(binding["actions"]) or not set(binding["actions"]) <= {"call", "announce", "hangup"}:
            raise Refused("INVALID_AUTOMATION_BINDING")
    return result


def effective_dnd(room, now):
    if room["dnd"]:
        return True
    quiet = room["quiet_hours"]
    if not quiet["enabled"]:
        return False
    minute = now.hour * 60 + now.minute
    start = int(quiet["start"][:2]) * 60 + int(quiet["start"][3:])
    end = int(quiet["end"][:2]) * 60 + int(quiet["end"][3:])
    if start < end:
        return now.weekday() in quiet["days"] and start <= minute < end
    # An overnight interval belongs to its start day, including the after-midnight part.
    return ((now.weekday() in quiet["days"] and minute >= start)
            or ((now.weekday() - 1) % 7 in quiet["days"] and minute < end))


class Manager:
    """One HA entry, durable metadata, serialized configuration, volatile call authority."""

    def __init__(self, hass, entry, legacy):
        self.hass, self.entry, self.legacy = hass, entry, legacy
        self.store = Store(hass, 1, f"intercom_m3_poc.manager.{entry.entry_id}")
        self.lock = asyncio.Lock()
        self.profiles = {}
        self.profile_values = {}
        self.configuration = {}
        self.revision = 0
        self.history = []
        self.runtimes = {}
        self.live_runtimes = {}
        self.calls = {}
        self.terminal_calls = {}
        self.listeners = set()
        self.tasks = set()
        self.closed = False
        self.endpoint_pool = None
        self.pool_tasks = {}
        self.automation_contexts = {}
        self.automation_unsub = None
        self.volume_state = {}
        self.volume_tasks = {}
        self.history_write_failures = 0
        self.history_starts = {}
        self.listener_failures = 0
        self.runtime_cleanup_failures = 0
        self.cleanup_task = None

    async def load(self, device_id):
        from .endpoint import EndpointConfig
        saved = await self.store.async_load()
        profiles, profile_values = {}, {}
        if saved is None:
            backup = Store(self.hass, 1, f"intercom_m3_poc.manager.{self.entry.entry_id}.migration_backup")
            if await backup.async_load() is not None:
                raise Refused("MISSING_MANAGER_STORAGE")
            empty = self.entry.data.get("manager_bootstrap") == "empty"
            values = {**self.entry.data, **self.entry.options}
            backend = values.get("backend", "simulation")
            if not empty and backend == "real_endpoint":
                fields = ("endpoint_host", "endpoint_port", "expected_endpoint_uuid", "expected_endpoint_uri",
                    "expected_endpoint_spki_sha256", "endpoint_ca_path", "endpoint_client_cert_path", "endpoint_client_key_path", "endpoint_profile")
                profile_values["legacy"] = {key: values[key] for key in fields if key in values}
                profiles["legacy"] = await self.hass.async_add_executor_job(EndpointConfig.from_mapping, profile_values["legacy"])
            rooms = [] if empty else [room_record(device_id, values.get("room_name", self.legacy.room_name),
                backend, "legacy" if profiles else None)]
            configuration = {"rooms": rooms,
                "groups": [], "permissions": [{"user_id": user_id, "room_ids": [] if empty else [device_id],
                    "actions": ["call", "history"]} for user_id in sorted(self.legacy.allowed_users)],
                "settings": deepcopy(DEFAULT_SETTINGS)}
            configuration = validate_configuration(configuration, profiles)
            revision, history = 1, []
            await backup.async_save({"entry_id": self.entry.entry_id,
                "data": dict(self.entry.data), "options": dict(self.entry.options),
                "created_at": time.time()})
        else:
            if not isinstance(saved, dict) or type(saved.get("schema")) is not int or saved["schema"] not in (1, 2):
                raise Refused("INVALID_STORAGE")
            revision = integer(saved.get("revision"), 1, 2**53 - 1)
            profile_values = deepcopy(saved.get("endpoint_profiles"))
            if not isinstance(profile_values, dict):
                raise Refused("INVALID_STORAGE")
            for profile_id, values in profile_values.items():
                text(profile_id)
                if not isinstance(values, dict):
                    raise Refused("INVALID_STORAGE")
                profiles[profile_id] = await self.hass.async_add_executor_job(EndpointConfig.from_mapping, values)
            configuration = validate_configuration(saved.get("configuration"), profiles)
            history = deepcopy(saved.get("history", []))
            if not isinstance(history, list):
                raise Refused("INVALID_STORAGE")
            history = history[-10000:]
        previous_history = deepcopy(history)
        detected = time.time()
        for summary in history:
            if isinstance(summary, dict) and summary.get("outcome") == "unfinished":
                summary.update(outcome="interrupted", reason="PROCESS_RESTART", ended_at=None,
                    duration_seconds=None, timing="unknown", interrupted_detected_at=detected)
        # Stage all authority until migration/recovery is durable; no runtime opens on save failure.
        self.history = history
        history = self.trim_history(configuration["settings"]["history_retention_days"], apply=False)
        self.history = []
        payload = {"schema": 2, "revision": revision, "configuration": configuration,
            "endpoint_profiles": profile_values, "history": history}
        if saved is None or saved["schema"] == 1 or history != previous_history:
            await self.store.async_save(payload)
        self.configuration, self.revision, self.history = configuration, revision, history
        self.profiles, self.profile_values = profiles, profile_values
        self.legacy.manager = self
        if any(room["room_id"] == device_id for room in configuration["rooms"]):
            self.runtimes[device_id] = self.legacy
        else:
            self.legacy.room_name, self.legacy.backend, self.legacy.endpoint_config = "Intercom Manager", "real_endpoint", None
        await self.reconcile()
        from .manager_assist import async_setup_assist
        await async_setup_assist(self.hass, self)
        self.automation_unsub = self.hass.bus.async_listen("automation_triggered", self.automation_triggered)
        self.spawn(self.policy_watchdog())

    def payload(self, configuration=None, revision=None, history=None):
        return {"schema": 2, "revision": self.revision if revision is None else revision,
                "configuration": self.configuration if configuration is None else configuration,
                "history": self.history if history is None else history,
                "endpoint_profiles": self.profile_values}

    def config(self):
        return {"revision": self.revision, **deepcopy(self.configuration)}

    def room(self, room_id):
        room = next((r for r in self.configuration["rooms"] if r["room_id"] == room_id), None)
        if room is None:
            raise Refused("UNKNOWN_ROOM")
        return room

    def permitted(self, user_id, room_id, action="call"):
        return any(g["user_id"] == user_id and room_id in g["room_ids"] and action in g["actions"]
                   for g in self.configuration["permissions"])

    def require(self, user_id, room_ids, action="call"):
        if not room_ids or any(not self.permitted(user_id, room_id, action) for room_id in room_ids):
            raise Refused("NOT_AUTHORIZED")

    def dnd(self, room_id):
        zone = ZoneInfo(self.hass.config.time_zone)
        return effective_dnd(self.room(room_id), datetime.now(zone))

    async def reconcile(self):
        from . import Runtime
        if self.endpoint_pool is None:
            from .endpoint import EndpointPool
            self.endpoint_pool = EndpointPool(source_handler=self.handle_local_request,
                                              wake_handler=self.handle_local_wake)
        ids = {r["room_id"] for r in self.configuration["rooms"]}
        failed = False
        for room_id in list(self.runtimes):
            if room_id not in ids:
                self.volume_state.pop(room_id, None)
                try:
                    volume_task = self.volume_tasks.pop(room_id, None)
                    if volume_task is not None:
                        volume_task.cancel()
                        await asyncio.gather(volume_task, return_exceptions=True)
                    await self.remove_pool_room(room_id)
                    if room_id in self.endpoint_pool.entries or room_id in self.endpoint_pool.configurations:
                        raise Refused("RUNTIME_CLEANUP_PENDING")
                    removed = self.runtimes[room_id]
                    if removed is self.legacy:
                        if not await removed.revoke("ROOM_REMOVED"):
                            raise Refused("TEARDOWN_TIMEOUT")
                        for ws in tuple(getattr(removed, "sockets", ())):
                            if not ws.closed:
                                await removed._close_socket(ws, "ROOM_REMOVED")
                        if any(not ws.closed for ws in getattr(removed, "sockets", ())):
                            raise Refused("TEARDOWN_TIMEOUT")
                        # Keep the entry's auth/entity adapter after removing its first room.
                        removed.room_name, removed.backend, removed.endpoint_config = "Intercom Manager", "real_endpoint", None
                        removed.availability_value, removed.availability_checked = None, 0
                    else:
                        await removed.close()
                except Exception:
                    failed = True  # Retain exact runtime ownership for the next cleanup attempt.
                    continue
                self.runtimes.pop(room_id, None)
        for room in self.configuration["rooms"]:
            runtime = self.runtimes.get(room["room_id"])
            config = self.profiles.get(room["endpoint_ref"])
            if runtime is None:
                runtime = Runtime(self.hass, self.entry, self.legacy.component,
                    self.legacy.allowed_users, self.legacy.internal_origin, self.legacy.nabu_origin)
                self.runtimes[room["room_id"]] = runtime
            runtime.manager, runtime.device_id = self, room["room_id"]
            runtime.room_name, runtime.backend, runtime.endpoint_config = room["name"], room["backend"], config
            runtime.status_listeners.add(self.notify)
            runtime.allowed_users = {g["user_id"] for g in self.configuration["permissions"]
                                     if room["room_id"] in g["room_ids"] and "call" in g["actions"]}
            if config is not None and getattr(config, "endpoint_profile", 3) == 4:
                if self.endpoint_pool.configurations.get(room["room_id"]) != config:
                    await self.remove_pool_room(room["room_id"])
                    self.schedule_pool_open(room["room_id"], config)
            elif room["room_id"] in self.endpoint_pool.configurations or room["room_id"] in self.pool_tasks:
                await self.remove_pool_room(room["room_id"])
        if failed:
            raise Refused("RUNTIME_CLEANUP_FAILED")

    async def remove_pool_room(self, room_id):
        task, owner = self.pool_tasks.get(room_id), asyncio.current_task()
        self.pool_tasks[room_id] = owner  # Keep watchdog admission closed through yielding transport cleanup.
        try:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await self.endpoint_pool.remove(room_id)
        finally:
            if self.pool_tasks.get(room_id) is owner:
                self.pool_tasks.pop(room_id, None)

    def schedule_pool_open(self, room_id, config):
        room = next((r for r in self.configuration["rooms"] if r["room_id"] == room_id), None)
        if (self.closed or room is None or self.profiles.get(room["endpoint_ref"]) != config
                or self.deployment_reserved(config.host)):
            return
        if room_id in self.pool_tasks and not self.pool_tasks[room_id].done():
            return
        async def open_idle():
            try:
                await self.endpoint_pool.open(room_id, config)
            except Exception:
                pass  # Availability remains false/unknown; next bounded retry uses a fresh authenticated stream.
            finally:
                if self.pool_tasks.get(room_id) is asyncio.current_task():
                    self.pool_tasks.pop(room_id, None)
                self.notify()
        self.pool_tasks[room_id] = self.spawn(open_idle())

    def resolve_targets(self, room_ids=None, group_id=None):
        if group_id is not None:
            if room_ids is not None:
                raise Refused("INVALID_PARTICIPANTS")
            group = next((g for g in self.configuration["groups"] if g["group_id"] == group_id), None)
            if group is None:
                raise Refused("UNKNOWN_GROUP")
            return list(group["room_ids"])
        return room_ids

    def destination_allowed(self, source, target):
        destinations = self.room(source)["allowed_destination_ids"]
        return source != target and (destinations is None or target in destinations)

    @callback
    def automation_triggered(self, event):
        component = self.hass.data.get("automation")
        automation_id = event.data.get("entity_id")
        entity = component.get_entity(automation_id) if component and hasattr(component, "get_entity") else None
        if entity is None or getattr(entity, "_context", None) is not event.context:
            return  # A caller-supplied event/entity/context identifier grants no authority.
        if not any(b["automation_entity_id"] == automation_id for b in self.configuration["settings"]["automation_bindings"]):
            return
        now = asyncio.get_running_loop().time()
        self.automation_contexts = {key: value for key, value in self.automation_contexts.items() if now - value[2] <= 3600}
        if len(self.automation_contexts) >= 128:
            return
        self.automation_contexts[event.context.id] = (event.context, automation_id, now)

    def automation_binding(self, context, action, room_ids):
        item = self.automation_contexts.get(context.id)
        if item is None or item[0] is not context or asyncio.get_running_loop().time() - item[2] > 3600:
            raise Refused("AUTOMATION_AUTHORITY_REQUIRED")
        binding = next((b for b in self.configuration["settings"]["automation_bindings"]
                        if b["automation_entity_id"] == item[1]), None)
        if binding is None or action not in binding["actions"] or not set(room_ids) <= set(binding["room_ids"]):
            raise Refused("AUTOMATION_NOT_AUTHORIZED")
        return binding

    def automation_current(self, principal, action, room_ids):
        item = self.automation_contexts.get(principal.control_connection_id)
        if item is None:
            return False
        try:
            binding = self.automation_binding(item[0], action, room_ids)
        except Refused:
            return False
        return binding["user_id"] == principal.user_id

    async def handle_local_request(self, peer_room_id, nonce, target_room_id, connection, boot):
        if self.closed or not self.endpoint_pool.matches(peer_room_id, connection, boot):
            raise Refused("STALE_SOURCE")
        room = self.room(peer_room_id)
        if room["favorite_room_id"] != target_room_id or not room["local_user_id"]:
            raise Refused("SOURCE_NOT_CONFIGURED")
        from .session import Principal
        principal = Principal(room["local_user_id"], "", f"{connection.hex()}:{boot.hex()}", "local_endpoint", "local_endpoint")
        result = self.start_room_call(room["local_user_id"], principal, [peer_room_id, target_room_id], "full_duplex", "ring")
        await self.wait_history_start(result["call_id"])

    async def handle_local_wake(self, endpoint, event):
        controller = getattr(self, "assist", None)
        if controller is None:
            raise Refused("ASSIST_UNAVAILABLE")
        handler = getattr(controller, "async_endpoint_wake", None)
        if not callable(handler):
            raise Refused("ASSIST_UNAVAILABLE")
        await handler(endpoint, event)

    def assist_local_stt(self, entity_id):
        from homeassistant.helpers import entity_registry as er
        import ipaddress
        entity = er.async_get(self.hass).async_get(entity_id)
        if entity is None or entity.domain != "stt" or entity.platform != "wyoming" or not entity.config_entry_id:
            return False
        entry = self.hass.config_entries.async_get_entry(entity.config_entry_id)
        try:
            host = ipaddress.ip_address(entry.data.get("host", "")) if entry else None
        except ValueError:
            return False
        return host is not None and not host.is_unspecified and (host.is_loopback or host.is_private)

    def assist_binding(self, device_id, pipeline_id):
        from .manager_assist import SourceBinding
        from .session import Principal
        from homeassistant.helpers import device_registry as dr
        matches = [r for r in self.configuration["rooms"] if r["ha_device_id"] == device_id
                   and r["assist_pipeline_id"] == pipeline_id and r["local_user_id"] and r["stt_entity_id"]]
        if len(matches) != 1:
            raise Refused("NO_ROOM_AUTHORITY")
        room = matches[0]
        device = dr.async_get(self.hass).async_get(device_id)
        runtime = self.runtimes[room["room_id"]]
        config = runtime.endpoint_config
        if device is None or config is None or getattr(config, "endpoint_profile", 3) != 4:
            raise Refused("NO_ROOM_AUTHORITY")
        # The administrator's selected native HA device must resolve to the same enrolled LAN host.
        if not any((entry := self.hass.config_entries.async_get_entry(entry_id)) is not None
                   and entry.data.get("host") == config.host for entry_id in device.config_entries):
            raise Refused("SOURCE_DEVICE_MISMATCH")
        try:
            from homeassistant.components.assist_pipeline import pipeline
            selected = pipeline.async_get_pipeline(self.hass, pipeline_id)
        except (KeyError, ValueError):
            raise Refused("PIPELINE_UNAVAILABLE") from None
        if selected.stt_engine != room["stt_entity_id"] or not self.assist_local_stt(room["stt_entity_id"]):
            raise Refused("LOCAL_STT_REQUIRED")
        from homeassistant.components import conversation
        from homeassistant.components.conversation.const import HOME_ASSISTANT_AGENT
        from homeassistant.components.conversation.default_agent import DefaultAgent
        if (selected.conversation_engine != HOME_ASSISTANT_AGENT or not isinstance(
                conversation.async_get_agent(self.hass, selected.conversation_engine), DefaultAgent)):
            raise Refused("LOCAL_CONVERSATION_REQUIRED")
        if selected.tts_engine:
            from .manager_announcements import local_tts_engine
            if not local_tts_engine(self.hass, selected.tts_engine):
                raise Refused("LOCAL_TTS_REQUIRED")
        entry = self.endpoint_pool.entries.get(room["room_id"]) if self.endpoint_pool else None
        if not entry or entry[0].closed:
            raise Refused("STALE_SOURCE")
        client = entry[0]
        principal = Principal(room["local_user_id"], "", f"{client.connection.hex()}:{client.boot.hex()}",
                              "local_endpoint", "local_endpoint")
        return SourceBinding(room["room_id"], device_id, pipeline_id, room["local_user_id"], principal,
                             room["stt_entity_id"])

    def assist_binding_for_endpoint(self, endpoint):
        room_id = next((rid for rid, entry in self.endpoint_pool.entries.items() if entry[0] is endpoint), None) if self.endpoint_pool else None
        if room_id is None:
            raise Refused("STALE_SOURCE")
        room = self.room(room_id)
        return self.assist_binding(room["ha_device_id"], room["assist_pipeline_id"])

    def assist_endpoint_current(self, endpoint, room_id, call_id, generation):
        now = asyncio.get_running_loop().time()
        if (self.closed or not self.endpoint_pool or not self.endpoint_pool.matches(room_id, endpoint.connection, endpoint.boot)
                or endpoint.room_mic_muted or endpoint.status_health & 32 or not endpoint.clock.fresh(now)):
            return False
        entry = self.endpoint_pool.entries.get(room_id)
        if not entry or entry[0] is not endpoint:
            return False
        if call_id is None:
            return not endpoint.started and endpoint.owner == 0 and generation == 0
        return self.assist_call_current(room_id, call_id, generation)

    def assist_call_current(self, room_id, call_id, generation):
        call = self.calls.get(call_id)
        entry = self.endpoint_pool.entries.get(room_id) if self.endpoint_pool else None
        if (not call or not entry or room_id not in call["room_ids"] or call["stop"].is_set()
                or call.get("generation") != generation):
            return False
        endpoint = entry[0]
        from . import _decode_id
        now = asyncio.get_running_loop().time()
        ringing_healthy = (not endpoint.held and endpoint.started and not endpoint.answered
            and endpoint.answer_policy == "ring" and endpoint.ring_seen
            and endpoint.last_status_received > 0 and now - endpoint.last_status_received <= .5
            and not endpoint.status_health & 0xe0)
        return ((endpoint.call, endpoint.owner) == (_decode_id(call_id), generation)
                and not endpoint.room_mic_muted and not endpoint.status_health & 32
                and endpoint.clock.fresh(now) and self.endpoint_pool.matches(room_id, endpoint.connection, endpoint.boot)
                and (endpoint.hold_healthy(now) if endpoint.held else ringing_healthy or endpoint.ready(now)))

    async def async_assist_hold(self, room_id, call_id, generation, enabled):
        call = self.calls.get(call_id)
        entry = self.endpoint_pool.entries.get(room_id) if self.endpoint_pool else None
        if not call or not entry or call.get("generation") != generation:
            return False
        endpoint = entry[0]
        from . import _decode_id
        if endpoint.call != _decode_id(call_id) or endpoint.owner != generation:
            return False
        if enabled:
            if not endpoint.held:
                return False
            await asyncio.wait_for(endpoint.command_ready.wait(), 1)
            if not endpoint.held or not endpoint.hold_ready(asyncio.get_running_loop().time()):
                return False
            call["participant_hold"][room_id] = True
        else:
            if endpoint.held:
                old_generation, token = endpoint.mode_generation, endpoint.command_token
                await endpoint.resume_command()
                deadline = asyncio.get_running_loop().time() + .8
                while endpoint.held and not endpoint.closed and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(.01)
                if (endpoint.held or endpoint.closed or endpoint.mode_generation != old_generation + 1
                        or endpoint.command_token != 0 or endpoint.call != _decode_id(call_id)
                        or endpoint.owner != generation or self.endpoint_pool.entries.get(room_id, (None,))[0] is not endpoint):
                    self.remove_policy_rooms(call, [room_id])
                    return False
            call["participant_hold"][room_id] = False
        self.notify()
        return True

    async def async_assist_action(self, room_id, call_id, generation, action):
        if not self.assist_call_current(room_id, call_id, generation):
            raise Refused("STALE_CALL")
        call = self.calls[call_id]
        user_id = self.room(room_id)["local_user_id"]
        if action == "hang_up":
            return self.participant_control(user_id, call_id, room_id, leave=True)
        if action in ("answer", "decline"):
            return await self.answer_call(user_id, call_id, room_id, action == "decline")
        raise Refused("UNRECOGNIZED_COMMAND")

    async def update(self, expected, configuration):
        if self.closed:
            raise Refused("UNAVAILABLE")
        candidate = validate_configuration(configuration, self.profiles)
        async with self.lock:
            if self.closed:
                raise Refused("UNAVAILABLE")
            if type(expected) is not int or expected != self.revision:
                raise Refused("CONFIGURATION_CONFLICT")
            if self.revision >= 2**53 - 1:
                raise Refused("CONFIGURATION_REVISION_EXHAUSTED")
            # Store before success; the old active configuration remains intact on failure.
            history = self.trim_history(candidate["settings"]["history_retention_days"], apply=False)
            await self.store.async_save(self.payload(candidate, self.revision + 1, history))
            previous = self.configuration
            self.configuration, self.revision = candidate, self.revision + 1
            self.history = history
            await self.configuration_changed(previous)
        self.notify()
        return {"revision": self.revision}

    async def configuration_changed(self, previous):
        """Apply the existing owner/call policy after a durable configuration publication."""
        for room_id, runtime in list(self.runtimes.items()):
            owner = runtime.session.owner
            candidate_room = next((r for r in self.configuration["rooms"] if r["room_id"] == room_id), None)
            previous_room = next((r for r in previous["rooms"] if r["room_id"] == room_id), None)
            if owner and (candidate_room is None or previous_room is None or self.dnd(room_id)
                    or any(candidate_room[key] != previous_room[key] for key in ("backend", "endpoint_ref"))
                    or not self.permitted(owner.principal.user_id, room_id)):
                runtime.revoke_now("CONFIGURATION_CHANGED")
        for call_id, call in list(self.calls.items()):
            action = "announce" if call["type"] == "announcement" else "call"
            if any(not self.permitted(call["user_id"], rid, action) for rid in call["room_ids"]):
                call["stop"].set()  # Initiating-authority revocation ends the whole affected call.
            elif call["type"] in ("room", "conference") and not self.call_directions_allowed(call):
                call["stop"].set()
            else:
                invalid = [rid for rid in call["room_ids"] if not any(r["room_id"] == rid for r in self.configuration["rooms"])
                    or self.dnd(rid)]
                self.remove_policy_rooms(call, invalid)
            if call["stop"].is_set() and call_id in self.live_runtimes:
                self.live_runtimes[call_id].revoke_now("CONFIGURATION_CHANGED")
        await self.reconcile()

    async def remove_endpoint(self, expected, endpoint_ref):
        async with self.lock:
            if self.closed:
                raise Refused("UNAVAILABLE")
            if type(expected) is not int or expected != self.revision:
                raise Refused("CONFIGURATION_CONFLICT")
            if self.revision >= 2**53 - 1:
                raise Refused("CONFIGURATION_REVISION_EXHAUSTED")
            if not isinstance(endpoint_ref, str) or endpoint_ref not in self.profiles:
                raise Refused("UNKNOWN_ENDPOINT_PROFILE")
            candidate = deepcopy(self.configuration)
            removed = {r["room_id"] for r in candidate["rooms"] if r["endpoint_ref"] == endpoint_ref}
            candidate["rooms"] = [r for r in candidate["rooms"] if r["room_id"] not in removed]
            for room in candidate["rooms"]:
                if room["favorite_room_id"] in removed:
                    room["favorite_room_id"] = None
                if room["allowed_destination_ids"] is not None:
                    room["allowed_destination_ids"] = [rid for rid in room["allowed_destination_ids"] if rid not in removed]
            for item in [*candidate["groups"], *candidate["permissions"], *candidate["settings"]["automation_bindings"]]:
                item["room_ids"] = [rid for rid in item["room_ids"] if rid not in removed]
            candidate["groups"] = [g for g in candidate["groups"] if g["room_ids"]]
            candidate["settings"]["automation_bindings"] = [b for b in candidate["settings"]["automation_bindings"] if b["room_ids"]]
            profiles = {key: value for key, value in self.profiles.items() if key != endpoint_ref}
            values = {key: deepcopy(value) for key, value in self.profile_values.items() if key != endpoint_ref}
            candidate = validate_configuration(candidate, profiles)
            payload = self.payload(candidate, self.revision + 1)
            payload["endpoint_profiles"] = values
            await self.store.async_save(payload)
            previous = self.configuration
            targets = [self.runtimes[rid] for rid in removed if rid in self.runtimes]
            affected_calls = {key for key, call in self.calls.items() if removed.intersection(call["room_ids"])}
            targets.extend(self.live_runtimes[key] for key in affected_calls if key in self.live_runtimes)
            self.configuration, self.profiles, self.profile_values = candidate, profiles, values
            self.revision += 1
            cleanup = "complete"
            try:
                await self.configuration_changed(previous)
            except Exception:
                self.runtime_cleanup_failures += 1
                cleanup = "failed"
            if cleanup == "complete" and (any(rid in self.runtimes or rid in self.pool_tasks or rid in self.volume_tasks
                    or rid in self.endpoint_pool.entries or rid in self.endpoint_pool.configurations for rid in removed)
                    or any(key in self.calls or key in self.live_runtimes for key in affected_calls)
                    or any(not task.done() for runtime in targets for task in
                           (*getattr(runtime, "media_handlers", ()), *getattr(runtime, "owned_tasks", ())))):
                cleanup = "pending"
        self.notify()
        return {"revision": self.revision, "endpoint_ref": endpoint_ref, "roster_removed": True,
            "runtime_cleanup": cleanup, "device_trust_reset": False, "external_credentials_erased": False}

    def notify(self):
        for listener in tuple(self.listeners):
            try:
                listener()
            except Exception:
                self.listeners.discard(listener)
                self.listener_failures += 1

    def remove_policy_rooms(self, call, room_ids):
        if not room_ids:
            return
        if call.get("mode") == "speaker_only" and call["initial_room_ids"][0] in room_ids:
            call["stop"].set()
            return
        remaining = [rid for rid in call["room_ids"] if rid not in room_ids]
        if call["type"] == "conference" and len(remaining) >= 2:
            call["removed_room_ids"].update(room_ids)
            call["room_ids"] = remaining
        else:
            call["stop"].set()

    def call_directions_allowed(self, call):
        rooms = call["room_ids"]
        known = {r["room_id"] for r in self.configuration["rooms"]}
        if not set(rooms) <= known:
            return False
        if call.get("mode") == "speaker_only" and call["initial_room_ids"][0] not in rooms:
            return False
        sources = rooms if call.get("mode", "full_duplex") == "full_duplex" else call["initial_room_ids"][:1]
        return all(self.destination_allowed(source, target) for source in sources for target in rooms if source != target)

    def spawn(self, coro):
        task = self.hass.async_create_background_task(coro, "intercom manager")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def schedule_record(self, item):
        async def save():
            try:
                await self.record(item)
            except Exception:
                # Call cleanup remains effective even if history persistence fails.
                pass  # record() counted the failed durable write.
        self.spawn(save())

    async def policy_watchdog(self):
        last_pool_check = 0
        purge_task = None
        while not self.closed:
            await asyncio.sleep(.5)
            known = {r["room_id"] for r in self.configuration["rooms"]}
            for rid, runtime in self.runtimes.items():
                owner = runtime.session.owner
                if owner and (rid not in known or self.dnd(rid) or not self.permitted(owner.principal.user_id, rid)):
                    runtime.revoke_now("POLICY_REVOKED")
            for call in list(self.calls.values()):
                known = {r["room_id"] for r in self.configuration["rooms"]}
                self.remove_policy_rooms(call, [rid for rid in call["room_ids"] if rid not in known or self.dnd(rid)])
            now = asyncio.get_running_loop().time()
            if now - last_pool_check >= 5:
                last_pool_check = now
                if purge_task is None or purge_task.done():
                    purge_task = self.spawn(self.purge_history())
                if (set(self.runtimes) - known and (self.cleanup_task is None or self.cleanup_task.done())):
                    async def cleanup():
                        async with self.lock:
                            if not self.closed:
                                try:
                                    await self.reconcile()
                                except Exception:
                                    self.runtime_cleanup_failures += 1
                        self.notify()
                    self.cleanup_task = self.spawn(cleanup())
                occupied = {rid for call in self.calls.values() for rid in call["room_ids"]}
                for rid, runtime in self.runtimes.items():
                    config = runtime.endpoint_config
                    if config is not None and getattr(config, "endpoint_profile", 3) == 4 and not runtime.session.owner and rid not in occupied:
                        entry = self.endpoint_pool.entries.get(rid)
                        if not entry or entry[0].closed or any(task.done() for task in entry[1]):
                            self.schedule_pool_open(rid, config)
            for rid, entry in list(self.endpoint_pool.entries.items()):
                client = entry[0]
                if self.deployment_reserved(client.config.host):
                    continue
                if not client.started or client.closed or rid not in known or rid not in self.runtimes or self.room(rid)["volumes"] == DEFAULT_VOLUMES:
                    continue
                epoch = (client.connection, client.boot, client.call, client.owner)
                observed = getattr(client, "volume_applied", None)
                current = self.volume_state.get(rid)
                if observed and (current is None or current["epoch"] != epoch):
                    generation, level, maximum, chime, revision = observed
                    if generation == client.mode_generation:
                        self.volume_state[rid] = {"level": level, "maximum": maximum, "chime": chime,
                            "revision": revision, "epoch": epoch, "calibrated": self.room(rid)["volumes"]["calibrated"]}
                        current = self.volume_state[rid]
                        self.notify()
                if (current and current["epoch"] == epoch and current["revision"] == self.revision) or rid in self.volume_tasks:
                    continue
                async def apply(rid=rid, client=client):
                    try:
                        announcement = any(c["type"] == "announcement" and rid in c["room_ids"] for c in self.calls.values())
                        await self.apply_room_volume(rid, client, announcement=announcement)
                    except Exception:
                        runtime = self.runtimes.get(rid)
                        if runtime is not None:
                            runtime.revoke_now("VOLUME_APPLY_FAILED")
                            for call in self.calls.values():
                                if rid in call["room_ids"]:
                                    self.remove_policy_rooms(call, [rid])
                    finally:
                        if self.volume_tasks.get(rid) is asyncio.current_task():
                            self.volume_tasks.pop(rid, None)
                self.volume_tasks[rid] = self.spawn(apply())

    async def enroll_endpoint(self, expected, values, endpoint_ref=None):
        from .endpoint import EndpointConfig
        if not isinstance(values, dict):
            raise Refused("INVALID_CONFIGURATION")
        fields = {"endpoint_host", "endpoint_port", "expected_endpoint_uuid", "expected_endpoint_uri",
                  "expected_endpoint_spki_sha256", "endpoint_ca_path", "endpoint_client_cert_path", "endpoint_client_key_path", "endpoint_profile"}
        if set(values) - fields:
            raise Refused("INVALID_CONFIGURATION")
        config = await self.hass.async_add_executor_job(EndpointConfig.from_mapping, values)
        async with self.lock:
            if self.closed:
                raise Refused("UNAVAILABLE")
            if type(expected) is not int or expected != self.revision:
                raise Refused("CONFIGURATION_CONFLICT")
            if self.revision >= 2**53 - 1:
                raise Refused("CONFIGURATION_REVISION_EXHAUSTED")
            if endpoint_ref is not None and endpoint_ref not in self.profiles:
                raise Refused("UNKNOWN_ENDPOINT_PROFILE")
            if endpoint_ref is not None and self.profiles[endpoint_ref].expected_uuid != config.expected_uuid:
                raise Refused("ENDPOINT_IDENTITY_CHANGED")
            if any(key != endpoint_ref and c.expected_uuid == config.expected_uuid for key, c in self.profiles.items()):
                raise Refused("DUPLICATE_ENDPOINT")
            profile_id = endpoint_ref or str(uuid.uuid4())
            payload = self.payload(revision=self.revision + 1)
            payload["endpoint_profiles"] = {**self.profile_values, profile_id: deepcopy(values)}
            await self.store.async_save(payload)
            self.profile_values[profile_id], self.profiles[profile_id] = deepcopy(values), config
            self.revision += 1
            for rid, runtime in self.runtimes.items():
                if self.room(rid)["endpoint_ref"] == profile_id:
                    runtime.revoke_now("ENDPOINT_CONFIGURATION_CHANGED")
                    for call in self.calls.values():
                        if rid in call["room_ids"]:
                            self.remove_policy_rooms(call, [rid])
            await self.reconcile()
        self.notify()
        return {"revision": self.revision, "endpoint_ref": profile_id}

    async def update_endpoint_profile(self, expected, endpoint_ref, endpoint_profile):
        if type(endpoint_profile) is not int or endpoint_profile not in (3, 4) or endpoint_ref not in self.profile_values:
            raise Refused("INVALID_ENDPOINT_PROFILE")
        values = {**self.profile_values[endpoint_ref], "endpoint_profile": endpoint_profile}
        return await self.enroll_endpoint(expected, values, endpoint_ref)

    async def apply_room_volume(self, room_id, client, *, announcement=False):
        runtime, revision = self.runtimes.get(room_id), self.revision
        volumes = self.room(room_id)["volumes"]
        if volumes == DEFAULT_VOLUMES:
            return None
        if not volumes["calibrated"] or not getattr(client, "capabilities", 0) & 256:
            raise Refused("VOLUME_UNSUPPORTED")
        previous = self.volume_state.get(room_id)
        epoch = (client.connection, client.boot, client.call, client.owner)
        level = volumes["announcement"] if announcement else volumes["initial"]
        if previous is not None and previous["epoch"] == epoch:
            level = min(previous["level"], volumes["maximum"])
        applied = await client.set_volume(level, volumes["maximum"], volumes["chime"], revision)
        if (self.closed or revision != self.revision or self.runtimes.get(room_id) is not runtime
                or not any(room["room_id"] == room_id for room in self.configuration["rooms"])):
            raise Refused("CONFIGURATION_CHANGED")
        self.volume_state[room_id] = {**applied, "epoch": epoch, "calibrated": volumes["calibrated"]}
        self.notify()
        return {key: value for key, value in self.volume_state[room_id].items() if key != "epoch"}

    def room_volume_config(self, room_id, *, announcement=False):
        volumes = self.room(room_id)["volumes"]
        if volumes == DEFAULT_VOLUMES:
            return None
        return {"level": volumes["announcement"] if announcement else volumes["initial"],
                "maximum": volumes["maximum"], "chime": volumes["chime"], "revision": self.revision}

    def start_room_call(self, user_id, principal, room_ids, mode, answer_policy="drop_in"):
        if not isinstance(room_ids, list) or not 2 <= len(room_ids) <= 4 or len(set(room_ids)) != len(room_ids):
            raise Refused("INVALID_PARTICIPANTS")
        if mode not in ("full_duplex", "speaker_only"):
            raise Refused("UNSUPPORTED_MODE")
        if answer_policy not in ("drop_in", "ring"):
            raise Refused("INVALID_ANSWER_POLICY")
        self.require(user_id, room_ids)
        self.reserve(room_ids)
        endpoints = {}
        for rid in room_ids:
            runtime = self.runtimes[rid]
            if runtime.endpoint_config is None:
                raise Refused("REAL_ENDPOINT_REQUIRED")
            endpoints[rid] = runtime.endpoint_config
        if answer_policy == "ring" and any(getattr(config, "endpoint_profile", 3) != 4 for config in endpoints.values()):
            raise Refused("MANUAL_ANSWER_UNSUPPORTED")
        sources = room_ids if mode == "full_duplex" else room_ids[:1]
        if any(not self.destination_allowed(source, target) for source in sources for target in room_ids if source != target):
            raise Refused("DIRECTIONAL_POLICY_DENIED")
        from . import _b64
        call_id = secrets.token_bytes(16)
        call = {"call_id": _b64(call_id), "type": "room" if len(room_ids) == 2 else "conference",
                "room_ids": list(room_ids), "state": "PREPARING", "reason": "STARTED",
                "started_at": time.time(), "user_id": user_id, "principal": principal,
                "stop": asyncio.Event(), "metrics": {}, "participant_tx_enabled": {},
                "participant_hold": {}, "removed_room_ids": set(), "initial_room_ids": list(room_ids),
                "answer_policy": answer_policy, "generation": secrets.randbits(64) or 1,
                "invitations": {}, "invitation_states": {}, "mode": mode}
        self.calls[call["call_id"]] = call
        self.begin_history(call)
        self.spawn(self._run_call(call, call_id, endpoints, mode))
        self.notify()
        return {"call_id": call["call_id"], "state": call["state"]}

    async def _run_call(self, call, call_id, endpoints, mode):
        reason = "ENDED"
        async def authorize():
            if self.closed or call["stop"].is_set() or self.calls.get(call["call_id"]) is not call:
                return False
            p = call["principal"]
            try:
                # A userless HA service is never an authenticated caller.
                if p.ingress_class in ("ha_service", "local_endpoint", "automation"):
                    user = await self.hass.auth.async_get_user(p.user_id)
                else:
                    token = self.hass.auth.async_get_refresh_token(p.refresh_token_id)
                    user = getattr(token, "user", None)
                if user is None or not user.is_active or user.id != call["user_id"]:
                    return False
                from . import ENTITY_ID, POLICY_CONTROL
                if not user.permissions.check_entity(ENTITY_ID, POLICY_CONTROL):
                    return False
                self.require(user.id, call["room_ids"])
                if not self.call_directions_allowed(call):
                    return False
                if p.ingress_class == "automation" and (not user.is_admin or not self.automation_current(p, "call", call["room_ids"])):
                    return False
                if p.ingress_class == "local_endpoint" and call["initial_room_ids"][0] in call["room_ids"]:
                    connection, boot = (bytes.fromhex(value) for value in p.control_connection_id.split(":"))
                    if not self.endpoint_pool.matches(call["initial_room_ids"][0], connection, boot):
                        return False
            except Refused:
                return False
            return not any(self.dnd(rid) for rid in call["room_ids"])

        async def on_state(state, state_reason, metrics):
            if state == "CONNECTED":
                state = "ACTIVE_FULL" if mode == "full_duplex" else "ACTIVE_SPEAKER_ONLY"
            elif state == "DEGRADED":
                state = "CONNECTING"
            state_reason = state_reason or "MEDIA_PROGRESS"
            if state not in {"PREPARING", "CONNECTING", "RINGING", "ACTIVE_FULL", "ACTIVE_SPEAKER_ONLY", "HELD", "TERMINATED"} or not isinstance(state_reason, str) or not SAFE_CODE.fullmatch(state_reason):
                raise Refused("INVALID_MEDIA_STATE")
            call["state"], call["reason"] = state, state_reason
            call["last_report"] = asyncio.get_running_loop().time()
            call["metrics"] = {key: value for key, value in metrics.items() if isinstance(value, (bool, int, float))}
            failed = metrics.get("failed_rooms", {})
            if isinstance(failed, dict):
                call["removed_room_ids"].update(rid for rid in failed if rid in call["initial_room_ids"])
                call["room_ids"] = [rid for rid in call["initial_room_ids"] if rid not in call["removed_room_ids"]]
            invitation_states = metrics.get("invitations", {})
            if isinstance(invitation_states, dict):
                for rid, value in invitation_states.items():
                    if rid in call["invitations"] and isinstance(value, dict) and value.get("state") in {"RINGING", "CONNECTING", "ACTIVE", "FAILED"}:
                        call["invitation_states"][rid] = {"state": value["state"], "reason": value.get("reason", "INVITATION_PROGRESS")}
                        if value["state"] == "FAILED":
                            call["invitations"].pop(rid, None)
                            call["removed_room_ids"].add(rid)
                            if rid in call["room_ids"]:
                                call["room_ids"].remove(rid)
            self.notify()

        def settings():
            return {**self.configuration["settings"], "call_type": call["type"],
                    "silence_timeout_seconds": self.configuration["settings"]["silence_seconds"],
                    "source_room_id": call["initial_room_ids"][0],
                    "endpoint_pool": getattr(self, "endpoint_pool", None),
                    "answer_policy": call["answer_policy"],
                    "invitations": dict(call["invitations"]),
                    "volume_configs": {rid: value for rid in call["initial_room_ids"]
                        if rid in self.runtimes and (value := self.room_volume_config(rid)) is not None},
                    "participants": {rid: {"tx_enabled": call["participant_tx_enabled"].get(rid, True),
                        "held": call["participant_hold"].get(rid, False), "removed": rid in call["removed_room_ids"]}
                        for rid in call["initial_room_ids"]},
                    "participant_tx_enabled": dict(call["participant_tx_enabled"]),
                    "participant_hold": dict(call["participant_hold"])}
        try:
            await self.wait_history_start(call["call_id"])
            from .manager_audio import run_room_call
            await run_room_call(call_id, call["generation"], endpoints, mode,
                                call["stop"], authorize, on_state, settings)
            reason = call["reason"] if call["state"] == "TERMINATED" else "ENDED"
        except asyncio.CancelledError:
            reason = "INTERRUPTED"
            raise
        except Exception as err:
            candidate = str(err)
            reason = candidate if SAFE_CODE.fullmatch(candidate) else "MEDIA_ERROR"
        finally:
            call["state"], call["reason"] = "TERMINATED", reason
            self.calls.pop(call["call_id"], None)
            self.remember_terminal(call)
            self.notify()
            ended = time.time()
            await self.record({k: call[k] for k in ("call_id", "type", "started_at")} |
                {"room_ids": call["initial_room_ids"]} |
                {"ended_at": ended, "duration_seconds": max(0, ended - call["started_at"]),
                 "outcome": "ended", "reason": reason})

    def remember_terminal(self, call):
        now = asyncio.get_running_loop().time()
        self.terminal_calls = {key: value for key, value in self.terminal_calls.items() if now - value["ended"] <= 300}
        self.terminal_calls[call["call_id"]] = {"user_id": call["user_id"], "ended": now,
            "principal": call["principal"] if call.get("browser_source") else None}
        while len(self.terminal_calls) > 100:
            self.terminal_calls.pop(next(iter(self.terminal_calls)))

    async def end_call(self, user_id, call_id, admin=False, principal=None):
        call = self.calls.get(call_id)
        if call is None:
            previous = self.terminal_calls.get(call_id)
            if previous is None or asyncio.get_running_loop().time() - previous["ended"] > 300:
                raise Refused("UNKNOWN_CALL")
            if not admin and (previous["user_id"] != user_id or previous["principal"] is not None and previous["principal"] != principal):
                raise Refused("NOT_AUTHORIZED")
            return {"stopped": True}
        if not admin and call["user_id"] != user_id:
            raise Refused("NOT_AUTHORIZED")
        if call.get("browser_source") and not admin and call["principal"] != principal:
            raise Refused("NOT_OWNER")
        call["stop"].set()
        runtime = self.live_runtimes.get(call_id)
        if runtime is not None:
            runtime.revoke_now("OWNER_STOPPED")
        return {"stopped": True}

    async def answer_call(self, user_id, call_id, participant_id, decline=False):
        call = self.calls.get(call_id)
        if call is None or participant_id not in call["room_ids"] or call["type"] == "announcement":
            raise Refused("STALE_CALL")
        self.require(user_id, [participant_id])
        if self.room(participant_id)["local_user_id"] != user_id and not (
                user_id == call["user_id"] and participant_id == call["initial_room_ids"][0]):
            raise Refused("PARTICIPANT_AUTHORITY_REQUIRED")
        # The initiating authority cannot remotely accept a different resident's microphone.
        if user_id == call["user_id"] and participant_id != call["initial_room_ids"][0]:
            raise Refused("RECEIVER_AUTHORITY_REQUIRED")
        from . import _decode_id
        entry = self.endpoint_pool.entries.get(participant_id) if self.endpoint_pool else None
        if entry is None:
            raise Refused("UNAVAILABLE")
        client = entry[0]
        method = self.endpoint_pool.decline if decline else self.endpoint_pool.answer
        await method(participant_id, _decode_id(call_id), call["generation"], client.mode_generation)
        return {"declined" if decline else "accepted": True}

    def participant_control(self, user_id, call_id, participant_id, *, enabled=None, leave=False):
        call = self.calls.get(call_id)
        if call is None or participant_id not in call["room_ids"] or call["type"] not in ("room", "conference"):
            raise Refused("STALE_CALL")
        self.require(user_id, [participant_id])
        if self.room(participant_id)["local_user_id"] != user_id:
            raise Refused("PARTICIPANT_AUTHORITY_REQUIRED")
        if leave:
            if len(call["room_ids"]) <= 2:
                call["stop"].set()
            else:
                call["removed_room_ids"].add(participant_id)
                call["room_ids"].remove(participant_id)
        else:
            if type(enabled) is not bool:
                raise Refused("INVALID_REQUEST")
            call["participant_tx_enabled"][participant_id] = enabled
        self.notify()
        return {"left": True} if leave else {"enabled": enabled}

    def invite_participant(self, user_id, call_id, participant_id, admin=False):
        call = self.calls.get(call_id)
        if call is None or call["type"] not in ("room", "conference") or call["state"] not in ("ACTIVE_FULL", "ACTIVE_SPEAKER_ONLY"):
            raise Refused("STALE_CALL")
        if not admin and call["user_id"] != user_id:
            raise Refused("NOT_AUTHORIZED")
        self.require(call["user_id"], [*call["room_ids"], participant_id])
        if participant_id in call["room_ids"] or participant_id in call["initial_room_ids"]:
            raise Refused("DUPLICATE_PARTICIPANT")
        if len(call["room_ids"]) >= 4:
            raise Refused("CAPACITY_REACHED")
        self.reserve([participant_id])
        config = self.runtimes[participant_id].endpoint_config
        if config is None or getattr(config, "endpoint_profile", 3) != 4:
            raise Refused("MANUAL_ANSWER_UNSUPPORTED")
        if any(not self.destination_allowed(source, target) for source in [*call["room_ids"], participant_id]
               for target in [*call["room_ids"], participant_id] if source != target):
            raise Refused("DIRECTIONAL_POLICY_DENIED")
        call["invitations"][participant_id] = config
        call["invitation_states"][participant_id] = {"state": "RINGING", "reason": "INVITED"}
        call["initial_room_ids"].append(participant_id)
        call["room_ids"].append(participant_id)
        call["type"] = "conference"
        self.notify()
        return {"call_id": call_id, "participant_id": participant_id, "state": "RINGING"}

    def start_live_announcement(self, connection, principal, room_ids, source):
        if not isinstance(source, dict) or set(source) != {"kind", "browser_instance_id"}:
            raise Refused("INVALID_SOURCE")
        if not isinstance(room_ids, list) or not room_ids or len(room_ids) > 7 or len(set(room_ids)) != len(room_ids):
            raise Refused("INVALID_PARTICIPANTS")
        from . import Runtime, _decode_id, _b64, _encode_generation, MEDIA_TICKET_PATH
        browser_id = _decode_id(source["browser_instance_id"])
        results, accepted = [], []
        for rid in room_ids:
            try:
                self.require(principal.user_id, [rid], "announce")
                self.reserve([rid], browser=True)
                if self.runtimes[rid].endpoint_config is None:
                    raise Refused("REAL_ENDPOINT_REQUIRED")
            except Refused as err:
                results.append({"room_id": rid, "status": "skipped", "reason": str(err)})
            else:
                results.append({"room_id": rid, "status": "accepted", "reason": "ACCEPTED"})
                accepted.append(rid)
        if not accepted:
            return {"call_id": None, "state": "TERMINATED", "results": results}
        self.reserve(accepted, browser=True)
        runtime = Runtime(self.hass, self.entry, self.legacy.component, {principal.user_id},
                          self.legacy.internal_origin, self.legacy.nabu_origin)
        runtime.backend = "real_endpoint"
        runtime.room_name, runtime.device_id = "Live announcement", "announcement_" + secrets.token_hex(8)
        owner = runtime.start(connection, principal, browser_id, "speaker_only")
        call_id = _b64(owner.call_id)
        runtime.live_announcement_id = call_id
        call = {"call_id": call_id, "type": "announcement", "room_ids": accepted,
            "state": "PREPARING", "reason": "STARTED", "started_at": time.time(),
            "user_id": principal.user_id, "principal": principal, "stop": asyncio.Event(),
            "metrics": {}, "results": results, "browser_source": True}
        self.calls[call_id], self.live_runtimes[call_id] = call, runtime
        self.begin_history(call)
        def changed():
            if runtime.session.owner is None:
                call["stop"].set()
                # A source that never attached still needs a bounded reservation finalizer.
                if not runtime.media_handlers and call_id in self.calls:
                    self.spawn(self.finish_live(call_id, "SOURCE_ENDED"))
        runtime.status_listeners.add(changed)
        self.notify()
        return {"call_id": call_id, "owner_generation": _encode_generation(owner.generation),
                "state": "PREPARING", "media_ticket_path": MEDIA_TICKET_PATH, "results": results}

    async def finish_live(self, call_id, reason):
        call = self.calls.pop(call_id, None)
        runtime = self.live_runtimes.pop(call_id, None)
        if call is None:
            return
        self.remember_terminal(call)
        call["stop"].set()
        if runtime is not None:
            await runtime.close()
        ended = time.time()
        await self.record({k: call[k] for k in ("call_id", "type", "room_ids", "started_at")} |
            {"ended_at": ended, "duration_seconds": max(0, ended - call["started_at"]),
             "outcome": "ended", "reason": reason, "results": call["results"]})
        self.notify()

    async def run_live_media(self, ws, owner, runtime):
        call_id = runtime.live_announcement_id
        call = self.calls.get(call_id)
        if call is None:
            raise Refused("STALE_CALL")
        endpoints = {rid: self.runtimes[rid].endpoint_config for rid in call["room_ids"]}
        async def authorize():
            if self.closed or call["stop"].is_set() or not await runtime.authorize_owner(owner):
                return False
            return all(self.permitted(owner.principal.user_id, rid, "announce") and not self.dnd(rid)
                       for rid in call["room_ids"])
        async def on_state(state, reason, metrics):
            if state not in {"PREPARING", "CONNECTING", "ACTIVE_SPEAKER_ONLY", "TERMINATED"}:
                raise Refused("INVALID_MEDIA_STATE")
            reason = reason or "MEDIA_PROGRESS"
            if not SAFE_CODE.fullmatch(reason):
                raise Refused("INVALID_MEDIA_STATE")
            call["state"], call["reason"] = state, reason
            call["last_report"] = asyncio.get_running_loop().time()
            outcomes = metrics.get("destination_results", {})
            if isinstance(outcomes, dict):
                for result in call["results"]:
                    value = outcomes.get(result["room_id"])
                    if isinstance(value, dict) and value.get("status") in {"accepted", "active", "completed", "failed", "skipped"} and isinstance(value.get("reason"), str) and SAFE_CODE.fullmatch(value["reason"]):
                        result.update(status=value["status"], reason=value["reason"])
            from . import SAFE_METRICS
            call["metrics"] = {key: value for key, value in metrics.items() if isinstance(value, (bool, int, float))}
            safe_metrics = {key: value for key, value in call["metrics"].items() if key in SAFE_METRICS}
            await runtime.media_state(owner, state, reason, safe_metrics)
            self.notify()
        reason = "MEDIA_ENDED"
        try:
            await self.wait_history_start(call_id)
            from .manager_live import run_live_announcement
            volume_configs = {rid: value for rid in call["room_ids"]
                if (value := self.room_volume_config(rid, announcement=True)) is not None}
            await run_live_announcement(ws, owner, endpoints, authorize, on_state,
                lambda: runtime.current_owner(owner), call["stop"], self.endpoint_pool,
                **({"volume_configs": volume_configs} if volume_configs else {}))
            reason = call["reason"]
        finally:
            await self.finish_live(call_id, reason)

    def deployment_reserved(self, host):
        controller = getattr(getattr(self.legacy, "component", None), "deployment", None)
        return bool(controller is not None and controller.has_reserved_host(host))

    def deployment_target_busy(self, host):
        targets = {rid for rid, runtime in self.runtimes.items()
                   if runtime.endpoint_config is not None and runtime.endpoint_config.host == host}
        return (self.closed or any(targets.intersection(call["room_ids"]) for call in self.calls.values())
                or any(self.runtimes[rid].session.owner is not None or self.runtimes[rid]._media_busy()
                       for rid in targets))

    async def deployment_quiesce_target(self, host):
        if self.deployment_target_busy(host):
            raise Refused("BUSY")
        targets = [(rid, runtime) for rid, runtime in self.runtimes.items()
                   if runtime.endpoint_config is not None and runtime.endpoint_config.host == host]
        for rid, runtime in targets:
            for task in (runtime.availability_task, self.volume_tasks.pop(rid, None)):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            runtime.availability_value, runtime.availability_checked = False, 0
            if self.endpoint_pool is not None:
                await self.remove_pool_room(rid)
                if rid in self.endpoint_pool.entries or rid in self.endpoint_pool.configurations:
                    raise Refused("RUNTIME_CLEANUP_PENDING")
        self.notify()

    async def deployment_installed(self, device_id, values):
        """Enroll only the controller's verified, server-owned committed identity."""
        if self.deployment_target_busy(values["endpoint_host"]):
            raise Refused("BUSY")
        existing = next((key for key, profile in self.profiles.items()
                         if str(profile.expected_uuid) == device_id), None)
        if existing is not None and self.profile_values.get(existing) == values:
            saved = {"endpoint_ref": existing}
        else:
            saved = await self.enroll_endpoint(self.revision, values, existing)
        if any(room["room_id"] == device_id for room in self.configuration["rooms"]):
            return
        controller = self.legacy.component.deployment
        row = next((row for row in controller.status()["devices"] if row["device_id"] == device_id), None)
        if row is None:
            raise Refused("UNKNOWN_DEPLOYMENT_DEVICE")
        configuration = deepcopy(self.configuration)
        configuration["rooms"].append(room_record(device_id, row["label"], "real_endpoint", saved["endpoint_ref"]))
        for user_id in sorted(self.legacy.allowed_users):
            grant = next((grant for grant in configuration["permissions"]
                          if grant["user_id"] == user_id and set(grant["actions"]) == {"call", "history"}), None)
            if grant is not None and device_id not in grant["room_ids"]:
                grant["room_ids"].append(device_id)
        await self.update(self.revision, configuration)

    def reserve(self, room_ids, browser=False):
        if self.closed:
            raise Refused("UNAVAILABLE")
        active = len(self.calls) + sum(r.session.owner is not None for r in self.runtimes.values())
        participants = sum(len(c["room_ids"]) + int(c.get("browser_source", False)) for c in self.calls.values()) + sum(2 for r in self.runtimes.values() if r.session.owner)
        if active >= 4 or participants + len(room_ids) + int(browser) > 8:
            raise Refused("CAPACITY_REACHED")
        reserved = {rid for call in self.calls.values() for rid in call["room_ids"]}
        for rid in room_ids:
            runtime = self.runtimes.get(rid)
            if runtime is None:
                raise Refused("UNKNOWN_ROOM")
            if runtime.endpoint_config is not None and self.deployment_reserved(runtime.endpoint_config.host):
                raise Refused("DEVICE_UPDATING")
            if rid in reserved or runtime.session.owner or runtime._media_busy():
                raise Refused("BUSY")
            if self.dnd(rid):
                raise Refused("DND")

    def snapshot(self, user_id, admin=False):
        visible = {r["room_id"] for r in self.configuration["rooms"] if admin or self.permitted(user_id, r["room_id"])}
        rooms = []
        for room in self.configuration["rooms"]:
            rid = room["room_id"]
            if rid not in visible:
                continue
            runtime = self.runtimes[rid]
            status = runtime.room_status()
            call = next((c for c in self.calls.values() if rid in c["room_ids"]), None)
            pool_entry = self.endpoint_pool.entries.get(rid) if self.endpoint_pool else None
            client = pool_entry[0] if pool_entry else None
            applied = self.volume_state.get(rid)
            if applied and (not client or applied["epoch"] != (client.connection, client.boot, client.call, client.owner)):
                applied = None
            if call is not None:
                status["available"] = True if (call["state"].startswith("ACTIVE_") and
                    asyncio.get_running_loop().time() - call.get("last_report", 0) <= 1.5
                    and call.get("invitation_states", {}).get(rid, {"state": "ACTIVE"})["state"] == "ACTIVE") else None
            rooms.append({"room_id": rid, "device_id": rid, "name": room["name"],
                **status, "available": status.get("available"), "dnd": self.dnd(rid),
                "can_call": self.permitted(user_id, rid), "can_announce": self.permitted(user_id, rid, "announce"),
                "busy": status["busy"] or call is not None,
                "activity": call["state"] if call else runtime.state if status["busy"] else "IDLE",
                "last_report_age_ms": max(0, int((asyncio.get_running_loop().time() - call["last_report"]) * 1000)) if call and call.get("last_report") else runtime.report_age_ms(),
                "volume": {"supported": bool(client and client.capabilities & 256),
                    "calibrated": room["volumes"]["calibrated"],
                    "applied": {key: value for key, value in applied.items() if key != "epoch"} if applied else None},
                "supported_modes": ["full_duplex", "speaker_only"]})
        calls = []
        for call in self.calls.values():
            # Exclude foreign participants/identity unless the viewer is part of this call.
            own_rooms = [rid for rid in call["room_ids"] if call["type"] in ("room", "conference")
                         and rid in visible and self.room(rid)["local_user_id"] == user_id]
            if not admin and call["user_id"] != user_id and not own_rooms:
                continue
            calls.append({k: deepcopy(call[k]) for k in ("call_id", "type", "room_ids", "state", "started_at", "reason")})
            calls[-1]["room_ids"] = [rid for rid in call["room_ids"] if rid in visible]
            calls[-1].update(can_end=admin or call["user_id"] == user_id, can_leave=bool(own_rooms),
                participant_count=len(call["room_ids"]) + int(call.get("browser_source", False)),
                participants=[{"room_id": rid, "can_leave": True, "can_mute": True,
                    "can_answer": (call.get("answer_policy") == "ring" and call["state"] == "RINGING"
                        or call.get("invitation_states", {}).get(rid, {}).get("state") == "RINGING")
                        and (call["user_id"] != user_id or rid == call.get("initial_room_ids", [None])[0]),
                    "can_decline": (call.get("answer_policy") == "ring" and call["state"] == "RINGING"
                        or call.get("invitation_states", {}).get(rid, {}).get("state") == "RINGING")
                        and (call["user_id"] != user_id or rid == call.get("initial_room_ids", [None])[0]),
                    "tx_enabled": call.get("participant_tx_enabled", {}).get(rid, True)} for rid in own_rooms])
            can_invite = (admin or call["user_id"] == user_id) and call["type"] in ("room", "conference") and len(call["room_ids"]) < 4 and call["state"].startswith("ACTIVE_")
            targets = []
            if can_invite:
                for rid, selected in self.runtimes.items():
                    if rid in call["initial_room_ids"] or rid not in visible or not self.permitted(call["user_id"], rid):
                        continue
                    if selected.endpoint_config is None or getattr(selected.endpoint_config, "endpoint_profile", 3) != 4:
                        continue
                    if all(self.destination_allowed(source, target) for source in [*call["room_ids"], rid]
                           for target in [*call["room_ids"], rid] if source != target):
                        try:
                            self.reserve([rid])
                        except Refused:
                            continue
                        targets.append(rid)
            calls[-1].update(can_invite=bool(targets), invite_targets=targets)
            if call["type"] == "announcement":
                calls[-1]["results"] = [deepcopy(result) for result in call.get("results", []) if admin or result["room_id"] in visible]
        for rid, runtime in self.runtimes.items():
            owner = runtime.session.owner
            if owner and (admin or owner.principal.user_id == user_id):
                from . import _b64, _encode_generation
                calls.append({"call_id": _b64(owner.call_id), "type": "browser", "room_ids": [rid],
                    "state": runtime.state, "reason": runtime.reason,
                    "owner_generation": _encode_generation(owner.generation)})
                calls[-1].update(can_end=True, can_leave=True)
        public_settings = deepcopy(self.configuration["settings"]) if admin else {
            key: deepcopy(self.configuration["settings"][key]) for key in (
                "silence_enabled", "silence_seconds", "silence_call_types", "history_enabled", "history_retention_days")}
        return {"revision": self.revision, "rooms": rooms, "calls": calls,
            "groups": [deepcopy(g) for g in self.configuration["groups"] if set(g["room_ids"]) <= visible],
            "settings": public_settings, "is_admin": admin,
            "capabilities": {"browser_call": True, "room_call": True, "manual_answer": any(
                r.endpoint_config is not None and getattr(r.endpoint_config, "endpoint_profile", 3) == 4 for r in self.runtimes.values()),
                "announcements": True, "live_announcements": True, "conference": True, "silence_detection": True,
                "local_assist": False, "volume": any(entry[0].capabilities & 256 for entry in self.endpoint_pool.entries.values()) if self.endpoint_pool else False}}

    def trim_history(self, retention_days=None, *, apply=True):
        if retention_days is None:
            retention_days = self.configuration["settings"]["history_retention_days"]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).timestamp()
        retained = [h for h in self.history if isinstance(h, dict)
            and isinstance(stamp := h.get("ended_at") if h.get("ended_at") is not None else h.get("started_at"), (int, float))
            and stamp >= cutoff]
        unfinished = [h for h in retained if h.get("outcome") == "unfinished"][-10000:]
        completed = [h for h in retained if h.get("outcome") != "unfinished"]
        available = 10000 - len(unfinished)
        # Current calls retain their crash marker when many short calls consume the record cap.
        result = (completed[-available:] if available else []) + unfinished
        if apply:
            self.history = result
        return result

    async def purge_history(self):
        async with self.lock:
            if self.closed:
                return
            history = self.trim_history(apply=False)
            if history == self.history:
                return
            try:
                await self.store.async_save(self.payload(history=history))
            except Exception:
                self.history_write_failures += 1
                return  # Keep the disk diff for retry on the next maintenance cycle.
            self.history = history

    def begin_history(self, call):
        """Start a minimal durable summary; admission responses/media await this task."""
        if not self.configuration["settings"]["history_enabled"]:
            return
        call_id = call["call_id"]
        if call_id in self.history_starts:
            return
        item = {"call_id": call_id, "type": call["type"],
            "room_ids": list(call.get("initial_room_ids", call["room_ids"])),
            "started_at": call["started_at"], "ended_at": None, "duration_seconds": None,
            "outcome": "unfinished", "reason": "IN_PROGRESS", "timing": "in_progress"}
        async def save():
            async with self.lock:
                before = self.history
                self.history = [h for h in self.history if h.get("call_id") != call_id] + [item]
                self.trim_history()
                candidate, self.history = self.history, before
                try:
                    await self.store.async_save(self.payload(history=candidate))
                except Exception:
                    self.history_write_failures += 1
                    raise
                self.history = candidate
        self.history_starts[call_id] = self.spawn(save())
        from . import _b64
        active = set(self.calls) | {_b64(runtime.session.owner.call_id) for runtime in self.runtimes.values() if runtime.session.owner}
        finished = [key for key, task in self.history_starts.items() if task.done() and key not in active]
        for key in finished[:-100]:
            self.history_starts.pop(key, None)

    async def wait_history_start(self, call_id):
        task = self.history_starts.get(call_id)
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except (Exception, asyncio.CancelledError) as err:
            call = self.calls.get(call_id)
            if call is not None:
                call["stop"].set()
            runtime = self.live_runtimes.get(call_id)
            if runtime is not None:
                runtime.revoke_now("HISTORY_UNAVAILABLE")
            from . import _b64
            for runtime in self.runtimes.values():
                owner = runtime.session.owner
                if owner is not None and _b64(owner.call_id) == call_id:
                    runtime.revoke_now("HISTORY_UNAVAILABLE")
            if isinstance(err, asyncio.CancelledError):
                raise
            raise Refused("HISTORY_UNAVAILABLE") from None

    async def record(self, item):
        call_id = item["call_id"]
        started = self.history_starts.get(call_id)
        if started is not None and started is not asyncio.current_task() and not started.done():
            await asyncio.gather(asyncio.shield(started), return_exceptions=True)
        async with self.lock:
            if self.configuration["settings"]["history_enabled"] or any(h.get("call_id") == call_id for h in self.history):
                before = self.history
                self.history = [h for h in self.history if h.get("call_id") != call_id] + [deepcopy(item)]
                self.trim_history()
                candidate, self.history = self.history, before
                try:
                    await self.store.async_save(self.payload(history=candidate))
                except Exception:
                    self.history_write_failures += 1
                    raise
                self.history = candidate

    def history_page(self, user_id, admin, offset=0, limit=50):
        integer(offset, 0, 10000)
        integer(limit, 1, 100)
        items = [deepcopy(h) for h in reversed(self.trim_history(apply=False)) if h.get("outcome") != "unfinished"
                 and (admin or all(self.permitted(user_id, rid, "history") for rid in h.get("room_ids", [])))]
        return {"items": items[offset:offset + limit], "total": len(items),
                "next_offset": offset + limit if offset + limit < len(items) else None}

    async def delete_history(self, expected):
        async with self.lock:
            if self.closed:
                raise Refused("UNAVAILABLE")
            if type(expected) is not int or expected != self.revision:
                raise Refused("CONFIGURATION_CONFLICT")
            count = len(self.history)
            await self.store.async_save(self.payload(history=[]))
            self.history.clear()
        return {"deleted": count}

    async def close(self):
        self.closed = True
        # Finish an already-saving administrative write before runtime teardown.
        # Queued writes recheck closed while holding this same lock.
        async with self.lock:
            pass
        self.notify()
        if self.automation_unsub is not None:
            self.automation_unsub()
            self.automation_unsub = None
        self.automation_contexts.clear()
        self.terminal_calls.clear()
        from .manager_assist import async_unload_assist
        await async_unload_assist(self.hass, self)
        for call in list(self.calls.values()):
            call["stop"].set()
        for call_id in list(self.live_runtimes):
            await self.finish_live(call_id, "ENTRY_UNLOADED")
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks, timeout=2)
            # A Store save may be running in an executor; never cancel its waiter.
            # Queued writers cannot enter until cancellation is issued under the lock.
            async with self.lock:
                for task in pending:
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for runtime in self.runtimes.values():
            await runtime.close()
        if not any(runtime is self.legacy for runtime in self.runtimes.values()):
            await self.legacy.close()
        for runtime in list(self.live_runtimes.values()):
            await runtime.close()
        if self.endpoint_pool is not None:
            await self.endpoint_pool.close()
        # Runtime.close can enqueue final metadata after the earlier task snapshot.
        # Producers are now closed; drain those saves before a reload uses this Store.
        while self.tasks:
            tasks = tuple(self.tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self.tasks.difference_update(tasks)
        self.history_starts.clear()
        self.listeners.clear()
