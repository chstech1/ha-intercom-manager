"""Removable Milestone 5/6 room proof of concept.

The HTTP and WebSocket endpoints are process-lifetime registrations because Home
Assistant exposes no corresponding unregister API.  They resolve this stable
component state and remain inert whenever the config entry is unloaded.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import math
from pathlib import Path
import re
import struct
from typing import Any

from aiohttp import WSMsgType, web
import probatio

from homeassistant.auth import EVENT_USER_REMOVED, EVENT_USER_UPDATED
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import (
    KEY_HASS_REFRESH_TOKEN_ID,
    KEY_HASS_USER,
    KEY_HASS,
    HomeAssistantView,
    StaticPathConfig,
    current_request,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from .session import Principal, Refused, Session, admit_request, encode_frame, decode_frame, ATTACH
from .endpoint import EndpointClient, EndpointConfig, EndpointEnded


DOMAIN = "intercom_m3_poc"
_LOGGER = logging.getLogger(__name__)
DATA_COMPONENT = f"{DOMAIN}_component"
ENTITY_ID = "sensor.intercom_m3_poc_simulated_room"
DEVICE_ID = "00000000-0000-4000-8000-000000000003"
MEDIA_TICKET_PATH = "/api/intercom_native/media-ticket"
MEDIA_WS_PATH = "/api/intercom_native/ws"
STATIC_PATH = "/intercom_m3_poc"
PANEL_PATH = "intercom-m3-poc"
PLATFORMS = (Platform.SENSOR, Platform.SWITCH, Platform.NUMBER)
ATTACH_OK = struct.Struct("<16sQBB")
CONTROL_CLEANUP = f"{DOMAIN}_owner"
MAX_PREAUTH = 16
ALLOWED_STATES = {"PREPARING", "CONNECTING", "ACTIVE_FULL", "ACTIVE_SPEAKER_ONLY", "TERMINATED"}
SAFE_REASON = re.compile(r"^[A-Z0-9_]{1,40}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")
SAFE_GENERATION = re.compile(r"^[0-9a-f]{16}$")
SAFE_METRICS = {
    "uplink_frames",
    "downlink_frames",
    "stale_drops",
    "queue_drops",
    "simulation",
    "backend",
    "endpoint_sink_frames",
    "endpoint_capture_frames",
    "room_mic_muted",
    "endpoint_audio_prior_age_max_ms",
    "endpoint_audio_transit_age_max_ms",
    "endpoint_clock_uncertainty_ms",
    "endpoint_audio_stale_drops",
    "browser_uplink_age_max_ms",
    "endpoint_tx_queue_hold_max_ms",
    "browser_downlink_queue_hold_max_ms",
    "browser_downlink_age_max_ms",
    "browser_clock_rejected_samples",
    "browser_clock_rejected_uncertainty_max_ms",
    "endpoint_clock_rejected_samples",
    "endpoint_clock_rejected_uncertainty_max_ms",
}

CONF_ALLOWED_USERS = "allowed_user_ids"
CONF_INTERNAL_ORIGIN = "internal_origin"
CONF_NABU_ORIGIN = "nabu_origin"
CONF_BACKEND = "backend"
CONF_ROOM_NAME = "room_name"
BACKEND_SIMULATION = "simulation"
BACKEND_REAL = "real_endpoint"
DEFAULT_ROOM_NAME = "Intercom Room"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_id(value: Any) -> bytes:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise Refused("INVALID_IDENTITY")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        raise Refused("INVALID_IDENTITY") from None
    if len(raw) != 16 or _b64(raw) != value:
        raise Refused("INVALID_IDENTITY")
    return raw


def _encode_generation(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 0xFFFFFFFFFFFFFFFF:
        raise Refused("INVALID_GENERATION")
    return f"{value:016x}"


def _decode_generation(value: Any) -> int:
    if not isinstance(value, str) or not SAFE_GENERATION.fullmatch(value):
        raise Refused("INVALID_GENERATION")
    generation = int(value, 16)
    if generation == 0:
        raise Refused("INVALID_GENERATION")
    return generation


def _cloud_request() -> bool:
    try:
        from hass_nabucasa.remote import is_cloud_request

        return bool(is_cloud_request.get())
    except ImportError:
        return False


def _send_error(connection, msg: dict[str, Any], reason: str) -> None:
    connection.send_error(msg["id"], reason.lower(), reason)


@dataclass
class ComponentState:
    """Process-lifetime endpoint state."""

    runtime: Runtime | None = None
    manager: Any = None
    deployment: Any = None
    registration_failed: bool = False
    preauth_count: int = 0

    @callback
    def acquire_preauth(self) -> bool:
        if self.preauth_count >= MAX_PREAUTH:
            return False
        self.preauth_count += 1
        return True

    @callback
    def release_preauth(self) -> None:
        self.preauth_count = max(0, self.preauth_count - 1)


@dataclass
class Runtime:
    """Authority and lifecycle for the single selected room backend."""

    hass: HomeAssistant
    entry: ConfigEntry
    component: ComponentState
    allowed_users: set[str]
    internal_origin: str | None
    nabu_origin: str | None
    session: Session = field(default_factory=Session)
    accepting: bool = True
    state: str = "TERMINATED"
    reason: str = "IDLE"
    control_connection: Any = None
    media_ws: web.WebSocketResponse | None = None
    lease_task: asyncio.Task | None = None
    owned_tasks: set[asyncio.Task] = field(default_factory=set)
    media_handlers: set[asyncio.Task] = field(default_factory=set)
    sockets: set[web.WebSocketResponse] = field(default_factory=set)
    revoke_unsub: Callable[[], None] | None = None
    user_unsubs: list[Callable[[], None]] = field(default_factory=list)
    subscribers: dict[int, tuple[Any, int]] = field(default_factory=dict)
    panel_registered: bool = False
    terminal_owner: tuple[Principal, bytes, int] | None = None
    backend: str = BACKEND_SIMULATION
    endpoint_config: EndpointConfig | None = None
    room_name: str = DEFAULT_ROOM_NAME
    room_mic_muted: bool | None = None
    status_listeners: set[Callable[[], None]] = field(default_factory=set)
    availability_value: bool | None = None
    availability_checked: float = 0.0
    availability_task: asyncio.Task | None = None
    availability_expiry_task: asyncio.Task | None = None
    manager: Any = None
    device_id: str = DEVICE_ID
    last_media_report: float = 0.0
    last_metrics: dict = field(default_factory=dict)
    call_started_at: float = 0.0

    @property
    def control_cleanup_key(self):
        return CONTROL_CLEANUP if self.device_id == DEVICE_ID else f"{CONTROL_CLEANUP}:{self.device_id}"

    @classmethod
    async def create(
        cls, hass: HomeAssistant, entry: ConfigEntry, component: ComponentState
    ) -> Runtime:
        values = {**entry.data, **entry.options}
        backend = values.get(CONF_BACKEND, BACKEND_SIMULATION)
        runtime = cls(
            hass=hass,
            entry=entry,
            component=component,
            allowed_users=set(entry.data[CONF_ALLOWED_USERS]),
            internal_origin=entry.data.get(CONF_INTERNAL_ORIGIN) or None,
            nabu_origin=entry.data.get(CONF_NABU_ORIGIN) or None,
            backend=backend,
            room_name=values.get(CONF_ROOM_NAME, DEFAULT_ROOM_NAME),
        )
        runtime.user_unsubs = [
            hass.bus.async_listen(EVENT_USER_UPDATED, runtime._user_changed),
            hass.bus.async_listen(EVENT_USER_REMOVED, runtime._user_changed),
        ]
        return runtime

    def room_status(self) -> dict[str, Any]:
        """Return current facts without probing or inferring endpoint presence."""
        owner = self.session.owner
        status: dict[str, Any] = {
            "room_name": self.room_name,
            "busy": owner is not None or self._media_busy(),
            "muted": self.room_mic_muted if owner is not None else None,
        }
        if self.backend == BACKEND_SIMULATION:
            status["available"] = True
        elif self.state.startswith("ACTIVE_"):
            status["available"] = True if self.last_media_report > 0 and self.session.clock() - self.last_media_report <= 1.5 else None
        elif owner is None and self.availability_value is not None:
            status["available"] = self.availability_value
        return status

    def report_age_ms(self):
        stamp = self.last_media_report if self.session.owner else self.availability_checked
        return max(0, int((self.session.clock() - stamp) * 1000)) if stamp else None

    async def availability(self) -> bool | None:
        """Check the idle authenticated listener without starting a call."""
        deployment = self.component.deployment
        if (deployment is not None and self.endpoint_config is not None
                and deployment.has_reserved_host(self.endpoint_config.host)):
            self.availability_value, self.availability_checked = False, 0
            return False
        if self.backend == BACKEND_SIMULATION:
            return True
        if self.state.startswith("ACTIVE_"):
            return self.room_status().get("available")
        if (self.manager is not None and self.endpoint_config is not None
                and getattr(self.endpoint_config, "endpoint_profile", 3) == 4):
            pool = self.manager.endpoint_pool
            entry = pool.entries.get(self.device_id) if pool else None
            value = bool(entry and not entry[0].closed and not any(task.done() for task in entry[1])
                         and entry[0].clock.fresh(self.session.clock()))
            self.availability_value, self.availability_checked = value, self.session.clock()
            return value
        if (self.session.owner is not None or self._media_busy()
                or self.endpoint_config is None):
            return None
        now = asyncio.get_running_loop().time()
        if self.availability_checked and now - self.availability_checked < 5:
            return self.availability_value
        task = self.availability_task
        if task is None or task.done():
            task = self._track(
                self.hass.async_create_background_task(
                    self._probe_availability(), f"{DOMAIN} availability"))
            self.availability_task = task
        await asyncio.shield(task)
        if self.availability_task is task:
            self.availability_task = None
        return self.availability_value

    async def _probe_availability(self) -> None:
        client = EndpointClient(
            self.endpoint_config, bytes(16), 1, "speaker_only")
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = False
        try:
            await asyncio.wait_for(client.connect(), 2.5)
            result = True
        except EndpointEnded as err:
            candidate = err.args[0] if err.args else "ENDPOINT_ERROR"
            code = (candidate if isinstance(candidate, str)
                    and SAFE_REASON.fullmatch(candidate) else "ENDPOINT_ERROR")
            _LOGGER.warning(
                "Endpoint availability probe failed: type=ENDPOINT code=%s errno=0 elapsed_ms=%d",
                code, max(0, int((loop.time() - started) * 1_000)))
        except OSError as err:
            error_number = err.errno if isinstance(err.errno, int) else 0
            _LOGGER.warning(
                "Endpoint availability probe failed: type=OS code=OS_ERROR errno=%d elapsed_ms=%d",
                error_number, max(0, int((loop.time() - started) * 1_000)))
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Endpoint availability probe failed: type=TIMEOUT code=TIMEOUT errno=0 elapsed_ms=%d",
                max(0, int((loop.time() - started) * 1_000)))
        except ValueError:
            _LOGGER.warning(
                "Endpoint availability probe failed: type=VALUE code=VALUE_ERROR errno=0 elapsed_ms=%d",
                max(0, int((loop.time() - started) * 1_000)))
        finally:
            await client.close()
        self.availability_value = result
        self.availability_checked = asyncio.get_running_loop().time()
        self.availability_task = None
        self._notify_status()
        checked = self.availability_checked
        self.availability_expiry_task = self._track(
            self.hass.async_create_background_task(
                self._expire_availability(checked), f"{DOMAIN} availability expiry"))

    async def _expire_availability(self, checked: float) -> None:
        await asyncio.sleep(5)
        if self.availability_checked == checked and self.session.owner is None:
            self.availability_value = None
            self.availability_checked = 0.0
            self._notify_status()

    async def wait_for_availability_probe(self) -> None:
        task = self.availability_task
        if task is not None:
            await asyncio.shield(task)

    @callback
    def add_status_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.status_listeners.add(listener)

        @callback
        def unsubscribe() -> None:
            self.status_listeners.discard(listener)

        return unsubscribe

    @callback
    def _notify_status(self) -> None:
        for listener in tuple(self.status_listeners):
            listener()

    def _ingress(self, request) -> tuple[str, str]:
        return admit_request(
            request,
            internal_origin=self.internal_origin,
            nabu_origin=self.nabu_origin,
            cloud_context=_cloud_request(),
        )

    def _token_user(self, user_id: str, token_id: str):
        token = self.hass.auth.async_get_refresh_token(token_id)
        user = getattr(token, "user", None)
        if (
            token is None
            or user is None
            or user.id != user_id
            or not user.is_active
            or user.id not in self.allowed_users
            or not user.permissions.check_entity(ENTITY_ID, POLICY_CONTROL)
        ):
            raise Refused("NOT_AUTHORIZED")
        return user

    @callback
    def _expire_preparing(self) -> None:
        owner = self.session.owner
        if (
            owner is not None
            and not owner.attached
            and self.session.clock() >= getattr(owner, "attachment_deadline", 0)
        ):
            self.revoke_now("ATTACH_TIMEOUT")

    def principal_from_connection(self, connection) -> Principal:
        if not self.accepting:
            raise Refused("UNAVAILABLE")
        self._expire_preparing()
        request = current_request.get()
        if request is None:
            raise Refused("SECURE_INGRESS_REQUIRED")
        user_id = connection.user.id
        token_id = connection.refresh_token_id
        if not token_id:
            raise Refused("NOT_AUTHORIZED")
        self._token_user(user_id, token_id)
        ingress, origin = self._ingress(request)
        return Principal(user_id, token_id, f"{id(connection):x}", ingress, origin)

    def principal_from_request(self, request) -> Principal:
        if not self.accepting:
            raise Refused("UNAVAILABLE")
        self._expire_preparing()
        user = request.get(KEY_HASS_USER)
        token_id = request.get(KEY_HASS_REFRESH_TOKEN_ID)
        if user is None or not token_id:
            raise Refused("NOT_AUTHORIZED")
        self._token_user(user.id, token_id)
        ingress, origin = self._ingress(request)
        owner = self.session.owner
        if (
            owner is None
            or owner.principal.user_id != user.id
            or owner.principal.refresh_token_id != token_id
            or (owner.principal.ingress_class, owner.principal.origin)
            != (ingress, origin)
        ):
            raise Refused("NOT_OWNER")
        return owner.principal

    def current_owner(self, owner) -> bool:
        return self.accepting and self.session.owner is owner and owner.attached

    def _media_busy(self) -> bool:
        """Return whether a media handler still owns teardown work."""
        return any(not task.done() for task in self.media_handlers)

    @callback
    def _track(self, task: asyncio.Task) -> asyncio.Task:
        self.owned_tasks.add(task)
        task.add_done_callback(self.owned_tasks.discard)
        return task

    async def _close_socket(self, ws: web.WebSocketResponse, reason: str) -> None:
        if ws.closed:
            return
        try:
            await asyncio.wait_for(
                ws.close(code=1008, message=reason.encode("ascii")), 0.5
            )
        except (asyncio.TimeoutError, ConnectionError):
            pass

    @callback
    def _schedule_socket_close(self, ws: web.WebSocketResponse, reason: str) -> None:
        if ws.closed:
            return
        task = self.hass.async_create_background_task(
            self._close_socket(ws, reason), f"{DOMAIN} close media"
        )
        self._track(task)

    async def authorize_owner(self, owner) -> bool:
        self._expire_preparing()
        if self.manager is not None and self.manager.closed:
            self.revoke_now("ENTRY_UNLOADED")
            return False
        if self.manager is not None:
            try:
                await self.manager.wait_history_start(_b64(owner.call_id))
            except Refused:
                return False
        try:
            self._token_user(owner.principal.user_id, owner.principal.refresh_token_id)
        except Refused:
            await self.revoke("AUTHORITY_REVOKED")
            return False
        return self.accepting and self.session.owner is owner

    @callback
    def _user_changed(self, event: Event) -> None:
        if self.manager is not None:
            for runtime in self.manager.runtimes.values():
                owner = runtime.session.owner
                if owner is not None and event.data.get("user_id") == owner.principal.user_id:
                    runtime.revoke_now("USER_CHANGED")
            for call in self.manager.calls.values():
                if call["user_id"] == event.data.get("user_id"):
                    call["stop"].set()
            self.manager.notify()
            return
        owner = self.session.owner
        if owner is not None and event.data.get("user_id") == owner.principal.user_id:
            self.revoke_now("USER_CHANGED")

    @callback
    def _token_revoked(self) -> None:
        self.revoke_unsub = None
        self.revoke_now("TOKEN_REVOKED")

    @callback
    def _control_closed(self) -> None:
        self.revoke_now("CONTROL_CLOSED")

    @callback
    def revoke_now(self, reason: str) -> None:
        owner = self.session.owner
        if owner is None:
            return
        self.terminal_owner = (owner.principal, owner.call_id, owner.generation)
        if self.manager is not None:
            import time
            ended = time.time()
            item = {"call_id": _b64(owner.call_id), "type": "browser",
                    "room_ids": [self.device_id], "started_at": self.call_started_at,
                    "ended_at": ended, "duration_seconds": max(0, ended - self.call_started_at),
                    "outcome": "ended", "reason": reason}
            self.manager.schedule_record(item)
        self.session.revoke()
        self.state = "TERMINATED"
        self.reason = reason
        self.room_mic_muted = None
        if self.lease_task is not None:
            if self.lease_task is not asyncio.current_task():
                self.lease_task.cancel()
            self.lease_task = None
        connection = self.control_connection
        self.control_connection = None
        if connection is not None and reason != "CONTROL_CLOSED":
            connection.subscriptions.pop(self.control_cleanup_key, None)
        unsubscribe = self.revoke_unsub
        self.revoke_unsub = None
        if unsubscribe is not None and reason != "TOKEN_REVOKED":
            unsubscribe()
        self._emit(owner, "TERMINATED", reason, {})
        self._notify_status()
        if reason != "CONTROL_CLOSED":
            for subscriber, subscription_id in tuple(self.subscribers.values()):
                subscriber.subscriptions.pop(subscription_id, None)
        self.subscribers.clear()
        ws = self.media_ws
        self.media_ws = None
        if ws is not None and not ws.closed and reason != "ENTRY_UNLOADED":
            self._schedule_socket_close(ws, reason)

    async def wait_for_media_teardown(self) -> bool:
        """Wait boundedly for another media handler to release its transport."""
        current = asyncio.current_task()
        handlers = [
            task for task in self.media_handlers
            if task is not current and not task.done()
        ]
        if handlers:
            _, pending = await asyncio.wait(handlers, timeout=2)
            return not pending
        await asyncio.sleep(0)
        return True

    async def revoke(self, reason: str) -> bool:
        self.revoke_now(reason)
        return await self.wait_for_media_teardown()

    async def _lease(self, owner) -> None:
        try:
            while self.session.owner is owner:
                await asyncio.sleep(1)
                if not await self.authorize_owner(owner):
                    return
                if not owner.attached and self.session.clock() >= owner.attachment_deadline:
                    self.revoke_now("ATTACH_TIMEOUT")
                    return
        except asyncio.CancelledError:
            pass

    def start(self, connection, principal: Principal, browser_id: bytes, mode: str):
        if mode not in ("full_duplex", "speaker_only"):
            raise Refused("UNSUPPORTED_MODE")
        if self._media_busy():
            raise Refused("BUSY")
        if (self.component.deployment is not None and self.endpoint_config is not None
                and self.component.deployment.has_reserved_host(self.endpoint_config.host)):
            raise Refused("DEVICE_UPDATING")
        if self.manager is not None:
            self.manager.require(principal.user_id, [self.device_id])
            self.manager.reserve([self.device_id], browser=True)
        owner = self.session.start(principal, browser_id)
        owner.mode = mode
        owner.caller_tx_enabled = True
        owner.mode_generation = 0
        owner.attachment_deadline = self.session.clock() + 3
        self.control_connection = connection
        connection.subscriptions[self.control_cleanup_key] = self._control_closed
        self.revoke_unsub = self.hass.auth.async_register_revoke_token_callback(
            principal.refresh_token_id, self._token_revoked
        )
        self.state = "PREPARING"
        import time
        self.call_started_at = time.time()
        if self.manager is not None:
            self.manager.begin_history({"call_id": _b64(owner.call_id), "type": "browser",
                "room_ids": [self.device_id], "started_at": self.call_started_at})
        self.last_media_report = 0.0
        self.last_metrics = {}
        self.reason = "STARTED"
        self.room_mic_muted = None
        self.availability_value = None
        self.availability_checked = 0.0
        if self.availability_expiry_task is not None:
            self.availability_expiry_task.cancel()
            self.availability_expiry_task = None
        self.lease_task = self._track(
            self.hass.async_create_background_task(
                self._lease(owner), f"{DOMAIN} authority lease"
            )
        )
        self._notify_status()
        return owner

    def _emit(
        self, owner, state: str, reason: str, metrics: dict[str, Any]
    ) -> None:
        event = {
            "call_id": _b64(owner.call_id),
            "owner_generation": _encode_generation(owner.generation),
            "mode_generation": getattr(owner, "mode_generation", 0),
            "state": state,
            "mode": getattr(owner, "mode", "full_duplex"),
            "reason": reason,
            "metrics": metrics,
        }
        for connection, subscription_id in tuple(self.subscribers.values()):
            try:
                connection.send_message(websocket_api.event_message(subscription_id, event))
            except Exception:  # A stale UI subscriber carries no authority.
                self.subscribers.pop(id(connection), None)

    async def media_state(
        self, owner, state: str, reason: str, metrics: dict[str, Any]
    ) -> None:
        if (
            state not in ALLOWED_STATES
            or not isinstance(reason, str)
            or not SAFE_REASON.fullmatch(reason)
        ):
            await self.revoke("INVALID_MEDIA_STATE")
            return
        safe_metrics = {
            key: value
            for key, value in metrics.items()
            if key in SAFE_METRICS
            and isinstance(value, (bool, int, float))
            and not isinstance(value, complex)
            and (not isinstance(value, float) or math.isfinite(value))
        }
        if self.session.owner is owner:
            self.last_media_report = self.session.clock()
            self.last_metrics = safe_metrics
            self.state = state
            self.reason = reason
            if "room_mic_muted" in safe_metrics:
                self.room_mic_muted = bool(safe_metrics["room_mic_muted"])
            self._emit(owner, state, reason, safe_metrics)
            self._notify_status()

    async def close(self) -> None:
        self.accepting = False
        sockets = tuple(self.sockets)
        try:
            await self.revoke("ENTRY_UNLOADED")
            current = asyncio.current_task()
            socket_closes = [
                self._close_socket(ws, "ENTRY_UNLOADED")
                for ws in tuple(self.sockets)
                if not ws.closed
            ]
            if socket_closes:
                await asyncio.gather(*socket_closes, return_exceptions=True)
            handlers = [task for task in self.media_handlers if task is not current and not task.done()]
            if handlers:
                done, pending = await asyncio.wait(handlers, timeout=0.5)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            tasks = [task for task in self.owned_tasks if task is not current and not task.done()]
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=0.5)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            for unsubscribe in self.user_unsubs:
                unsubscribe()
            self.user_unsubs.clear()
            self.media_handlers.clear()
            self.owned_tasks.clear()
            self.status_listeners.clear()
        finally:
            # A handler's finally block or cancellation must not orphan an unclosed socket.
            self.sockets.update(ws for ws in sockets if not ws.closed)
            self.sockets.difference_update(ws for ws in tuple(self.sockets) if ws.closed)
        if self.sockets:
            raise Refused("RUNTIME_CLEANUP_FAILED")


def _runtime(hass: HomeAssistant, device_id=None, call_id=None) -> Runtime:
    component = hass.data.get(DATA_COMPONENT)
    runtime = component.runtime if isinstance(component, ComponentState) else None
    if runtime is None or not runtime.accepting:
        raise Refused("UNAVAILABLE")
    manager = component.manager
    if manager is not None:
        if manager.closed:
            raise Refused("UNAVAILABLE")
        if device_id is not None:
            runtime = manager.runtimes.get(device_id)
        elif call_id is not None:
            runtime = next((r for r in [*manager.runtimes.values(), *manager.live_runtimes.values()] if
                (r.session.owner is not None and r.session.owner.call_id == call_id)
                or (r.terminal_owner is not None and r.terminal_owner[1] == call_id)), None)
        if runtime is None or not runtime.accepting:
            raise Refused("UNKNOWN_DEVICE" if device_id else "NOT_OWNER")
    return runtime


def _message_runtime(hass, msg):
    return _runtime(hass, call_id=_decode_id(msg["call_id"]))


def _manager_principal(hass, connection):
    runtime = _runtime(hass)
    request = current_request.get()
    if request is None or not connection.refresh_token_id:
        raise Refused("SECURE_INGRESS_REQUIRED")
    token = hass.auth.async_get_refresh_token(connection.refresh_token_id)
    user = getattr(token, "user", None)
    manager = runtime.manager
    if (user is None or not user.is_active or user.id != connection.user.id
            or not user.permissions.check_entity(ENTITY_ID, POLICY_CONTROL)
            or (not user.is_admin and (manager is None or not any(
                grant["user_id"] == user.id for grant in manager.configuration["permissions"])))):
        raise Refused("NOT_AUTHORIZED")
    ingress, origin = runtime._ingress(request)
    return Principal(user.id, connection.refresh_token_id, f"{id(connection):x}", ingress, origin)


def _owner_message(runtime: Runtime, connection, msg: dict[str, Any]):
    principal = runtime.principal_from_connection(connection)
    return runtime.session.require_owner(
        principal, _decode_id(msg["call_id"]), _decode_generation(msg["owner_generation"])
    )


@websocket_api.websocket_command({probatio.Required("type"): "intercom_native/list_devices"})
@websocket_api.async_response
async def ws_list_devices(hass, connection, msg) -> None:
    try:
        runtime = _runtime(hass)
        if runtime.manager is not None:
            _manager_principal(hass, connection)
        else:
            runtime.principal_from_connection(connection)
    except Refused as err:
        _send_error(connection, msg, str(err))
        return
    if runtime.manager is not None:
        manager = runtime.manager
        principal = _manager_principal(hass, connection)
        for selected in manager.runtimes.values():
            if manager.permitted(principal.user_id, selected.device_id):
                await selected.availability()
        snapshot = manager.snapshot(principal.user_id, False)
        connection.send_result(msg["id"], {"devices": [
            {**room, "display_name": room["name"], "ready": room["available"] is True,
             "maintenance": False} for room in snapshot["rooms"]]})
        return
    await runtime.availability()
    room_status = runtime.room_status()
    device = {
        "device_id": DEVICE_ID,
        "display_name": runtime.room_name,
        "room_name": runtime.room_name,
        "ready": room_status.get("available") is True,
        "busy": room_status["busy"],
        "dnd": False,
        "muted": room_status["muted"],
        "maintenance": False,
        "supported_modes": ["full_duplex", "speaker_only"],
    }
    if "available" in room_status:
        device["available"] = room_status["available"]
    connection.send_result(
        msg["id"],
        {
            "devices": [
                device
            ]
        },
    )


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "intercom_native/start",
        probatio.Required("device_id"): str,
        probatio.Required("browser_instance_id"): str,
        probatio.Required("mode"): str,
    }
)
@websocket_api.async_response
async def ws_start(hass, connection, msg) -> None:
    try:
        runtime = _runtime(hass, device_id=msg["device_id"])
        if msg["device_id"] != runtime.device_id:
            raise Refused("UNKNOWN_DEVICE")
        principal = runtime.principal_from_connection(connection)
        owner = runtime.start(
            connection, principal, _decode_id(msg["browser_instance_id"]), msg["mode"]
        )
        if runtime.manager is not None:
            await runtime.manager.wait_history_start(_b64(owner.call_id))
        await runtime.wait_for_availability_probe()
    except Refused as err:
        _send_error(connection, msg, str(err))
        return
    connection.send_result(
        msg["id"],
        {
            "call_id": _b64(owner.call_id),
            "owner_generation": _encode_generation(owner.generation),
            "state": "PREPARING",
            "media_ticket_path": MEDIA_TICKET_PATH,
        },
    )


OWNER_SCHEMA = {
    probatio.Required("call_id"): str,
    probatio.Required("owner_generation"): str,
}


@websocket_api.websocket_command(
    {probatio.Required("type"): "intercom_native/stop", **OWNER_SCHEMA}
)
@websocket_api.async_response
async def ws_stop(hass, connection, msg) -> None:
    try:
        runtime = _message_runtime(hass, msg)
        principal = runtime.principal_from_connection(connection)
        call_id = _decode_id(msg["call_id"])
        generation = _decode_generation(msg["owner_generation"])
        if runtime.session.owner is None:
            if runtime.terminal_owner != (principal, call_id, generation):
                raise Refused("NOT_OWNER")
            teardown_complete = await runtime.wait_for_media_teardown()
        else:
            runtime.session.require_owner(principal, call_id, generation)
            teardown_complete = await runtime.revoke("OWNER_STOPPED")
        if not teardown_complete:
            raise Refused("TEARDOWN_TIMEOUT")
    except Refused as err:
        _send_error(connection, msg, str(err))
        return
    connection.send_result(msg["id"], {"stopped": True})


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "intercom_native/admin_stop",
        probatio.Required("device_id"): str,
        probatio.Required("observed_call_id"): str,
    }
)
@websocket_api.async_response
async def ws_admin_stop(hass, connection, msg) -> None:
    try:
        runtime = _runtime(hass, device_id=msg["device_id"])
        principal = runtime.principal_from_connection(connection)
        user = runtime._token_user(principal.user_id, principal.refresh_token_id)
        if not user.is_admin:
            raise Refused("NOT_AUTHORIZED")
        if msg["device_id"] != runtime.device_id:
            raise Refused("UNKNOWN_DEVICE")
        owner = runtime.session.owner
        if owner is None or owner.call_id != _decode_id(msg["observed_call_id"]):
            raise Refused("STALE_CALL")
        if not await runtime.revoke("ADMIN_STOPPED"):
            raise Refused("TEARDOWN_TIMEOUT")
    except Refused as err:
        _send_error(connection, msg, str(err))
        return
    connection.send_result(msg["id"], {"stopped": True})


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "intercom_native/set_caller_tx_enabled",
        **OWNER_SCHEMA,
        probatio.Required("enabled"): bool,
    }
)
@websocket_api.async_response
async def ws_set_caller_tx_enabled(hass, connection, msg) -> None:
    try:
        runtime = _message_runtime(hass, msg)
        owner = _owner_message(runtime, connection, msg)
        owner.caller_tx_enabled = msg["enabled"]
        runtime._emit(owner, runtime.state, "CALLER_TX_CHANGED", {})
    except Refused as err:
        _send_error(connection, msg, str(err))
        return
    connection.send_result(msg["id"], {"enabled": msg["enabled"]})


@websocket_api.websocket_command(
    {
        probatio.Required("type"): "intercom_native/request_duplex",
        **OWNER_SCHEMA,
        probatio.Required("expected_mode_generation"): int,
    }
)
@websocket_api.async_response
async def ws_request_duplex(hass, connection, msg) -> None:
    try:
        runtime = _message_runtime(hass, msg)
        owner = _owner_message(runtime, connection, msg)
        if msg["expected_mode_generation"] != owner.mode_generation:
            raise Refused("STALE_MODE")
    except Refused as err:
        _send_error(connection, msg, str(err))
        return
    _send_error(connection, msg, "UNSUPPORTED_MODE")


@websocket_api.websocket_command(
    {probatio.Required("type"): "intercom_native/subscribe_call", **OWNER_SCHEMA}
)
@websocket_api.async_response
async def ws_subscribe_call(hass, connection, msg) -> None:
    try:
        runtime = _message_runtime(hass, msg)
        owner = _owner_message(runtime, connection, msg)
    except Refused as err:
        _send_error(connection, msg, str(err))
        return

    @callback
    def unsubscribe() -> None:
        runtime.subscribers.pop(id(connection), None)

    old = runtime.subscribers.pop(id(connection), None)
    if old is not None:
        connection.subscriptions.pop(old[1], None)
    runtime.subscribers[id(connection)] = (connection, msg["id"])
    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    runtime._emit(owner, runtime.state, runtime.reason, {})


COMMANDS = (
    ws_list_devices,
    ws_start,
    ws_stop,
    ws_admin_stop,
    ws_set_caller_tx_enabled,
    ws_request_duplex,
    ws_subscribe_call,
)

from .manager_api import COMMANDS as MANAGER_COMMANDS
COMMANDS += MANAGER_COMMANDS


class MediaTicketView(HomeAssistantView):
    url = MEDIA_TICKET_PATH
    name = f"api:{DOMAIN}:media_ticket"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        try:
            if request.query_string:
                raise Refused("INVALID_REQUEST")
            if request.content_length is not None and request.content_length > 1024:
                raise Refused("INVALID_REQUEST")

            async def read_body() -> bytes:
                body = bytearray()
                while True:
                    chunk = await request.content.read(min(1025 - len(body), 1024))
                    if not chunk:
                        return bytes(body)
                    body.extend(chunk)
                    if len(body) > 1024:
                        raise Refused("INVALID_REQUEST")

            body = await asyncio.wait_for(read_body(), 3)

            def no_duplicates(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise Refused("INVALID_REQUEST")
                    result[key] = value
                return result

            data = json.loads(body, object_pairs_hook=no_duplicates)
            if not isinstance(data, dict) or set(data) != {"call_id", "browser_instance_id"}:
                raise Refused("INVALID_REQUEST")
            runtime = _runtime(request.app[KEY_HASS], call_id=_decode_id(data["call_id"]))
            principal = runtime.principal_from_request(request)
            ticket = runtime.session.issue_ticket(
                principal,
                _decode_id(data["call_id"]),
                _decode_id(data["browser_instance_id"]),
            )
        except (
            Refused,
            asyncio.TimeoutError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            RecursionError,
        ):
            return web.json_response(
                {"error": "request_refused"}, status=400, headers=headers
            )
        return web.json_response({"ticket": ticket, "expires_in": 30}, headers=headers)


class MediaWebSocketView(HomeAssistantView):
    url = MEDIA_WS_PATH
    name = f"api:{DOMAIN}:media_ws"
    requires_auth = False

    async def get(self, request: web.Request) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        component = hass.data.get(DATA_COMPONENT)
        if not isinstance(component, ComponentState) or not component.acquire_preauth():
            raise web.HTTPServiceUnavailable()
        attached = False
        ws = None
        runtime = None
        owner = None
        try:
            if request.query_string:
                raise Refused("INVALID_REQUEST")
            runtime = _runtime(hass)
            handler = asyncio.current_task()
            ingress = runtime._ingress(request)
            ws = web.WebSocketResponse(
                # aiohttp rejects messages whose size is >= this value.
                compress=False,
                max_msg_size=1047,
                writer_limit=3 * 1046,
            )
            await ws.prepare(request)
            if request.transport is not None:
                request.transport.set_write_buffer_limits(high=3 * 1046, low=1046)
            message = await asyncio.wait_for(ws.receive(), 3)
            if (
                message.type is not WSMsgType.BINARY
                or not isinstance(message.data, bytes)
                or len(message.data) > 512
            ):
                raise Refused("INVALID_ATTACHMENT")
            kind, payload = decode_frame(message.data)
            if kind != 1:
                raise Refused("INVALID_ATTACHMENT")
            runtime = _runtime(hass, call_id=ATTACH.unpack(payload)[0])
            if handler is not None:
                runtime.media_handlers.add(handler)
            runtime.sockets.add(ws)
            runtime._expire_preparing()
            owner = runtime.session.attach(message.data, ingress)
            if not await runtime.authorize_owner(owner) or runtime.media_ws is not None:
                raise Refused("NOT_AUTHORIZED")
            runtime.media_ws = ws
            attached = True
            component.release_preauth()
            path = 2 if owner.principal.ingress_class == "nabu_remote" else 1
            await ws.send_bytes(
                encode_frame(2, ATTACH_OK.pack(owner.call_id, owner.generation,
                                               1 if runtime.backend == BACKEND_SIMULATION else 2, path))
            )
            from .media import run_media
            if getattr(runtime, "live_announcement_id", None) is not None:
                await component.manager.run_live_media(ws, owner, runtime)
            else:
                await run_media(
                ws,
                owner,
                owner.mode,
                lambda: runtime.authorize_owner(owner),
                lambda state, reason, metrics: runtime.media_state(
                    owner, state, reason, metrics
                ),
                lambda: runtime.current_owner(owner),
                    runtime.endpoint_config,
                    runtime.manager.endpoint_pool if runtime.manager is not None else None,
                    runtime.manager.room_volume_config(runtime.device_id) if runtime.manager is not None else None,
                )
        except (Refused, asyncio.TimeoutError):
            if ws is not None and not ws.closed:
                await runtime._close_socket(ws, "REQUEST_REFUSED")
        finally:
            if not attached:
                component.release_preauth()
            if runtime is not None and owner is not None and runtime.session.owner is owner:
                await runtime.revoke("MEDIA_ENDED")
            if runtime is not None and runtime.media_ws is ws:
                runtime.media_ws = None
            if runtime is not None:
                if ws is None or ws.closed:
                    runtime.sockets.discard(ws)
                handler = asyncio.current_task()
                if handler is not None:
                    runtime.media_handlers.discard(handler)
        return ws if ws is not None else web.Response(status=400)


def _http_collision(hass: HomeAssistant, path: str) -> bool:
    for resource in hass.http.app.router.resources():
        info = resource.get_info()
        registered = info.get("path") or info.get("prefix")
        if isinstance(registered, str) and (
            registered == path
            or registered.startswith(path.rstrip("/") + "/")
            or ("prefix" in info and path.startswith(registered.rstrip("/") + "/"))
        ):
            return True
    return False


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the process-lifetime adapter exactly once."""
    existing = hass.data.get(DATA_COMPONENT)
    if isinstance(existing, ComponentState):
        return not existing.registration_failed
    component = ComponentState()
    hass.data[DATA_COMPONENT] = component
    try:
        command_map = hass.data.get(websocket_api.DOMAIN, {})
        if any(handler._ws_command in command_map for handler in COMMANDS):
            raise RuntimeError("intercom_m3_poc WebSocket command collision")
        for path in (MEDIA_TICKET_PATH, MEDIA_WS_PATH, STATIC_PATH):
            if _http_collision(hass, path):
                raise RuntimeError(f"intercom_m3_poc route collision: {path}")
        hass.http.register_view(MediaTicketView())
        hass.http.register_view(MediaWebSocketView())
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    STATIC_PATH, str(Path(__file__).parent / "frontend"), False
                )
            ]
        )
        for handler in COMMANDS:
            websocket_api.async_register_command(hass, handler)
        from .manager_api import register_services
        register_services(hass)
    except Exception as err:
        _LOGGER.error("PoC endpoint setup failed: %s", type(err).__name__)
        component.registration_failed = True
        return False
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    component = hass.data.get(DATA_COMPONENT)
    registry_entry = er.async_get(hass).async_get(ENTITY_ID)
    entity_collision = (
        (registry_entry is not None and registry_entry.config_entry_id != entry.entry_id)
        or (registry_entry is None and hass.states.get(ENTITY_ID) is not None)
    )
    if (
        not isinstance(component, ComponentState)
        or component.registration_failed
        or component.runtime is not None
        or entity_collision
        or frontend.async_panel_exists(hass, PANEL_PATH)
    ):
        return False
    runtime = await Runtime.create(hass, entry, component)
    from .manager import Manager
    manager = Manager(hass, entry, runtime)
    try:
        await manager.load(DEVICE_ID)
    except Exception as err:
        _LOGGER.error("Manager metadata load failed: %s", str(err) if isinstance(err, Refused) else type(err).__name__)
        await manager.close()
        return False
    component.manager = manager
    entry.runtime_data = runtime
    from .manager_deployment import DeploymentManager, DeploymentError
    from .deployment_release import COMPATIBILITY_SHA256, BUNDLE_MANIFEST_SHA256
    deployment = DeploymentManager(hass, entry.entry_id,
        Path(hass.config.path("intercom_m3_poc_deployment", entry.entry_id)),
        Path(__file__).parent / "firmware" if COMPATIBILITY_SHA256 and BUNDLE_MANIFEST_SHA256 else None,
        COMPATIBILITY_SHA256, BUNDLE_MANIFEST_SHA256)
    try:
        await deployment.load()
    except Exception as err:
        _LOGGER.error("Speaker deployment unavailable: %s",
            str(err) if isinstance(err, DeploymentError) else type(err).__name__)
        await deployment.close()
    else:
        component.deployment = deployment
        deployment.bind_runtime(manager.deployment_target_busy,
            manager.deployment_quiesce_target, manager.deployment_installed)
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_PATH,
            webcomponent_name="intercom-manager-panel",
            sidebar_title="Intercom Manager",
            sidebar_icon="mdi:phone-in-talk",
            module_url=f"{STATIC_PATH}/manager.js?v={(await async_get_integration(hass, DOMAIN)).version}",
            require_admin=False,
            config={"device_id": DEVICE_ID,
                    "room_name": runtime.room_name,
                    "simulation": runtime.backend == BACKEND_SIMULATION,
                    "backend": runtime.backend},
        )
        runtime.panel_registered = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        component.runtime = runtime
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    except Exception as err:
        _LOGGER.error("PoC entry setup failed: %s", type(err).__name__)
        if component.deployment is not None:
            await component.deployment.close()
            component.deployment = None
        await manager.close()
        component.manager = None
        if runtime.panel_registered:
            frontend.async_remove_panel(hass, PANEL_PATH)
        component.runtime = None
        return False
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply a backend selection by reloading the isolated entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime: Runtime = entry.runtime_data
    component = hass.data.get(DATA_COMPONENT)
    if isinstance(component, ComponentState) and component.deployment is not None:
        await component.deployment.close()
        component.deployment = None
    if isinstance(component, ComponentState) and component.manager is not None:
        await component.manager.close()
        component.manager = None
    else:
        await runtime.close()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if runtime.panel_registered:
        frontend.async_remove_panel(hass, PANEL_PATH)
        runtime.panel_registered = False
    component = hass.data.get(DATA_COMPONENT)
    if isinstance(component, ComponentState) and component.runtime is runtime:
        component.runtime = None
    return unloaded
