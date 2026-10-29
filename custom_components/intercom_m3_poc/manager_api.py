"""HA-native manager commands, sharing the legacy authenticated browser path."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import probatio
from homeassistant.components import websocket_api

from . import (_runtime, _manager_principal, _decode_id, _b64, _encode_generation,
               MEDIA_TICKET_PATH, _send_error)
from .session import Refused
from .manager_deployment import DeploymentError


def command(name, schema, handler):
    @websocket_api.websocket_command({probatio.Required("type"): "intercom_native/" + name, **schema})
    @websocket_api.async_response
    async def run(hass, connection, msg):
        try:
            principal = _manager_principal(hass, connection)
            runtime = _runtime(hass)
            manager = runtime.manager
            if manager is None:
                raise Refused("UNAVAILABLE")
            user = hass.auth.async_get_refresh_token(principal.refresh_token_id).user
            result = await handler(manager, user, principal, connection, msg)
        except Refused as err:
            _send_error(connection, msg, str(err))
            return
        except DeploymentError as err:
            _send_error(connection, msg, str(err))
            return
        except (ValueError, TypeError, KeyError):
            _send_error(connection, msg, "INVALID_REQUEST")
            return
        except OSError:
            _send_error(connection, msg, "STORAGE_UNAVAILABLE")
            return
        connection.send_result(msg["id"], result)
    return run


def admin(user):
    if not user.is_admin:
        raise Refused("NOT_AUTHORIZED")


async def list_rooms(manager, user, principal, connection, msg):
    reserved = {rid for call in manager.calls.values() for rid in call["room_ids"]}
    selected = [r for rid, r in manager.runtimes.items() if rid not in reserved and (user.is_admin or manager.permitted(user.id, rid))]
    await asyncio.gather(*(r.availability() for r in selected))
    return manager.snapshot(user.id, user.is_admin)


async def get_configuration(manager, user, principal, connection, msg):
    admin(user)
    return manager.config()


async def list_endpoint_profiles(manager, user, principal, connection, msg):
    admin(user)
    return {"endpoint_profiles": [{"endpoint_ref": key,
        "device_uuid": str(value.expected_uuid), "profile": getattr(value, "endpoint_profile", 3)}
        for key, value in manager.profiles.items()]}


async def update_configuration(manager, user, principal, connection, msg):
    admin(user)
    return await manager.update(msg["expected_revision"], msg["configuration"])


async def enroll_endpoint(manager, user, principal, connection, msg):
    admin(user)
    return await manager.enroll_endpoint(msg["expected_revision"], msg["endpoint"], msg.get("endpoint_ref"))


async def update_endpoint_profile(manager, user, principal, connection, msg):
    admin(user)
    return await manager.update_endpoint_profile(msg["expected_revision"], msg["endpoint_ref"], msg["endpoint_profile"])


async def remove_endpoint(manager, user, principal, connection, msg):
    admin(user)
    return await manager.remove_endpoint(msg["expected_revision"], msg["endpoint_ref"])


def _deployment(manager, user):
    admin(user)
    controller = _runtime(manager.hass).component.deployment
    if controller is None or controller.closed:
        raise Refused("DEPLOYMENT_UNAVAILABLE")
    return controller


async def deployment_status(manager, user, principal, connection, msg):
    return await _deployment(manager, user).refresh_approvals()


async def deployment_probe(manager, user, principal, connection, msg):
    return await _deployment(manager, user).probe(msg["expected_deployment_revision"], msg["device_id"])


async def deployment_prepare(manager, user, principal, connection, msg):
    return await _deployment(manager, user).prepare(
        msg["expected_deployment_revision"], msg["device_id"], msg["action"])


async def deployment_execute(manager, user, principal, connection, msg):
    return await _deployment(manager, user).execute(
        msg["expected_deployment_revision"], msg["device_id"], msg["operation_id"], msg["action"])


async def deployment_reconcile(manager, user, principal, connection, msg):
    return await _deployment(manager, user).reconcile(msg["expected_deployment_revision"], msg["device_id"])


async def history(manager, user, principal, connection, msg):
    return manager.history_page(user.id, user.is_admin, msg.get("offset", 0), msg.get("limit", 50))


async def delete_history(manager, user, principal, connection, msg):
    admin(user)
    return await manager.delete_history(msg["expected_revision"])


async def diagnostics(manager, user, principal, connection, msg):
    admin(user)
    from . import DOMAIN
    from homeassistant.loader import async_get_integration
    return {"revision": manager.revision,
        "versions": {"integration": (await async_get_integration(manager.hass, DOMAIN)).version},
        "capacity": {"max_calls": 4, "max_participants": 8,
            "active_calls": len(manager.calls) + sum(r.session.owner is not None for r in manager.runtimes.values())},
        "persistence": {"history_write_failures": manager.history_write_failures,
                        "listener_failures": manager.listener_failures,
                        "runtime_cleanup_failures": manager.runtime_cleanup_failures,
                        "runtime_cleanup_pending": len(set(manager.runtimes) - {r["room_id"] for r in manager.configuration["rooms"]})},
        "rooms": [{"room_id": rid, "backend": r.backend, "state": r.state, "reason": r.reason,
                   "metrics": deepcopy(r.last_metrics)} for rid, r in manager.runtimes.items()],
        "limitations": ["Acoustic acceptance requires physical tests", "ICM3 does not support manual answer or volume control"]}


async def start_call(manager, user, principal, connection, msg):
    targets = manager.resolve_targets(msg.get("target_room_ids"), msg.get("group_id"))
    if not isinstance(targets, list) or not targets or any(not isinstance(rid, str) for rid in targets):
        raise Refused("INVALID_PARTICIPANTS")
    source, answer_policy = msg.get("source", "browser"), msg.get("answer_policy", "drop_in")
    if source == "browser":
        if len(targets) != 1 or answer_policy != "drop_in":
            raise Refused("UNSUPPORTED_CALL")
        runtime = manager.runtimes.get(targets[0])
        if runtime is None:
            raise Refused("UNKNOWN_ROOM")
        owner = runtime.start(connection, principal, _decode_id(msg.get("browser_instance_id")), msg["mode"])
        await manager.wait_history_start(_b64(owner.call_id))
        await runtime.wait_for_availability_probe()
        return {"call_id": _b64(owner.call_id), "owner_generation": _encode_generation(owner.generation),
                "state": "PREPARING", "media_ticket_path": MEDIA_TICKET_PATH}
    result = manager.start_room_call(user.id, principal, [source, *targets], msg["mode"], answer_policy)
    await manager.wait_history_start(result["call_id"])
    return result


async def end_call(manager, user, principal, connection, msg):
    from . import _decode_generation
    call_id = msg["call_id"]
    if msg["type"].endswith("leave_call") and msg.get("participant_id"):
        return manager.participant_control(user.id, call_id, msg["participant_id"], leave=True)
    if call_id in manager.calls or call_id in manager.terminal_calls:
        return await manager.end_call(user.id, call_id, user.is_admin, principal)
    runtime = _runtime(manager.hass, call_id=_decode_id(call_id))
    owner = runtime.session.owner
    if owner is None:
        if runtime.terminal_owner is None or runtime.terminal_owner[0] != principal:
            raise Refused("NOT_OWNER")
        return {"stopped": True}
    if not user.is_admin and owner.principal != principal:
        raise Refused("NOT_OWNER")
    if not await runtime.revoke("ADMIN_STOPPED" if user.is_admin else "OWNER_STOPPED"):
        raise Refused("TEARDOWN_TIMEOUT")
    return {"stopped": True}


async def unsupported(manager, user, principal, connection, msg):
    raise Refused("UNSUPPORTED_OPERATION")


async def answer_call(manager, user, principal, connection, msg):
    return await manager.answer_call(user.id, msg["call_id"], msg["participant_id"], decline=msg["type"].endswith("decline_call"))


async def participant_tx(manager, user, principal, connection, msg):
    return manager.participant_control(user.id, msg["call_id"], msg["participant_id"], enabled=msg["enabled"])


async def invite_participant(manager, user, principal, connection, msg):
    return manager.invite_participant(user.id, msg["call_id"], msg["participant_id"], user.is_admin)


async def list_announcement_sources(manager, user, principal, connection, msg):
    if not user.is_admin and not any(g["user_id"] == user.id and "announce" in g["actions"] for g in manager.configuration["permissions"]):
        raise Refused("NOT_AUTHORIZED")
    from .manager_announcements import sources
    return await sources(manager)


async def start_announcement(manager, user, principal, connection, msg):
    targets = manager.resolve_targets(msg.get("target_room_ids"), msg.get("group_id"))
    source = msg["source"]
    if isinstance(source, dict) and source.get("kind") == "live":
        result = manager.start_live_announcement(connection, principal, targets, source)
        if result["call_id"] is not None:
            await manager.wait_history_start(result["call_id"])
        return result
    from .manager_announcements import start
    return await start(manager, user.id, principal, targets, source)


async def stop_announcement(manager, user, principal, connection, msg):
    call = manager.calls.get(msg["call_id"])
    if call is not None and call["type"] != "announcement":
        raise Refused("WRONG_CALL_TYPE")
    return await manager.end_call(user.id, msg["call_id"], user.is_admin, principal)


async def subscribe_manager(hass, connection, msg):
    try:
        principal = _manager_principal(hass, connection)
        manager = _runtime(hass).manager
        if manager is None:
            raise Refused("UNAVAILABLE")
    except Refused as err:
        _send_error(connection, msg, str(err))
        return
    def changed():
        try:
            if manager.closed:
                raise Refused("UNAVAILABLE")
            # Recheck each delivery; a retained subscription grants no ongoing authority.
            token = hass.auth.async_get_refresh_token(principal.refresh_token_id)
            user = getattr(token, "user", None)
            from . import ENTITY_ID, POLICY_CONTROL
            if (user is None or not user.is_active or user.id != principal.user_id
                    or not user.permissions.check_entity(ENTITY_ID, POLICY_CONTROL)
                    or not user.is_admin and not any(g["user_id"] == user.id for g in manager.configuration["permissions"])):
                raise Refused("NOT_AUTHORIZED")
            connection.send_message(websocket_api.event_message(msg["id"], manager.snapshot(user.id, user.is_admin)))
        except Exception:
            unsubscribe()
    def unsubscribe():
        manager.listeners.discard(changed)
        connection.subscriptions.pop(msg["id"], None)
    manager.listeners.add(changed)
    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    changed()


subscribe_manager = websocket_api.websocket_command({probatio.Required("type"): "intercom_native/subscribe_manager"})(websocket_api.async_response(subscribe_manager))
REV = {probatio.Required("expected_revision"): int}
DEPLOY_REV = {probatio.Required("expected_deployment_revision"): int,
              probatio.Required("device_id"): str}
DEPLOY_ACTION = probatio.In(("install", "rollback_previous", "restore_stock", "remove"))
CALL = {probatio.Required("call_id"): str}
COMMANDS = (
    command("list_rooms", {}, list_rooms), subscribe_manager,
    command("get_configuration", {}, get_configuration),
    command("list_endpoint_profiles", {}, list_endpoint_profiles),
    command("deployment_status", {}, deployment_status),
    command("deployment_probe", DEPLOY_REV, deployment_probe),
    command("deployment_prepare", {**DEPLOY_REV, probatio.Required("action"): DEPLOY_ACTION}, deployment_prepare),
    command("deployment_execute", {**DEPLOY_REV, probatio.Required("operation_id"): str,
        probatio.Required("action"): DEPLOY_ACTION}, deployment_execute),
    command("deployment_reconcile", DEPLOY_REV, deployment_reconcile),
    command("update_configuration", {**REV, probatio.Required("configuration"): dict}, update_configuration),
    command("enroll_endpoint", {**REV, probatio.Required("endpoint"): dict, probatio.Optional("endpoint_ref"): str}, enroll_endpoint),
    command("update_endpoint_profile", {**REV, probatio.Required("endpoint_ref"): str,
        probatio.Required("endpoint_profile"): int}, update_endpoint_profile),
    command("remove_endpoint", {**REV, probatio.Required("endpoint_ref"): str}, remove_endpoint),
    command("history", {probatio.Optional("offset"): int, probatio.Optional("limit"): int}, history),
    command("delete_history", REV, delete_history), command("diagnostics", {}, diagnostics),
    command("start_call", {probatio.Optional("source"): str, probatio.Optional("target_room_ids"): list,
        probatio.Optional("group_id"): str,
        probatio.Required("mode"): str, probatio.Optional("answer_policy"): str,
        probatio.Optional("browser_instance_id"): str}, start_call),
    command("end_call", CALL, end_call), command("leave_call", {**CALL, probatio.Optional("participant_id"): str}, end_call),
    command("answer_call", {**CALL, probatio.Required("participant_id"): str}, answer_call),
    command("decline_call", {**CALL, probatio.Required("participant_id"): str}, answer_call),
    command("invite_participant", {**CALL, probatio.Required("participant_id"): str}, invite_participant),
    command("set_participant_tx_enabled", {**CALL, probatio.Required("participant_id"): str,
        probatio.Required("enabled"): bool}, participant_tx),
    command("list_announcement_sources", {}, list_announcement_sources),
    command("start_announcement", {probatio.Optional("target_room_ids"): list,
        probatio.Optional("group_id"): str,
        probatio.Required("source"): dict}, start_announcement), command("stop_announcement", CALL, stop_announcement),
)


def register_services(hass):
    """HA actions use trusted HA contexts; absent user identity grants nothing."""
    from homeassistant.core import SupportsResponse
    from . import DOMAIN, ENTITY_ID, POLICY_CONTROL
    from .session import Principal

    async def service(call):
        manager = _runtime(hass).manager
        user_id = call.context.user_id
        principal_class = "ha_service"
        if not user_id:
            if call.service == "room_call":
                action, rooms = "call", [call.data["source"], *call.data["target_room_ids"]]
            elif call.service == "announce":
                action, rooms = "announce", manager.resolve_targets(call.data.get("target_room_ids"), call.data.get("group_id"))
                if call.data.get("source", {}).get("kind") == "live":
                    raise Refused("FOREGROUND_BROWSER_REQUIRED")
            elif call.service == "hangup":
                active = manager.calls.get(call.data["call_id"])
                if active is None:
                    raise Refused("UNKNOWN_CALL")
                action, rooms = "hangup", active["room_ids"]
            else:
                raise Refused("NOT_AUTHORIZED")
            binding = manager.automation_binding(call.context, action, rooms)
            user_id, principal_class = binding["user_id"], "automation"
        user = await hass.auth.async_get_user(user_id) if user_id else None
        if user is None or not user.is_active or not user.permissions.check_entity(ENTITY_ID, POLICY_CONTROL):
            raise Refused("NOT_AUTHORIZED")
        if principal_class == "automation" and not user.is_admin:
            raise Refused("NOT_AUTHORIZED")
        principal = Principal(user.id, "", call.context.id, principal_class, "ha_service")
        if call.service == "room_call":
            result = manager.start_room_call(user.id, principal,
                [call.data["source"], *call.data["target_room_ids"]],
                call.data.get("mode", "full_duplex"), call.data.get("answer_policy", "drop_in"))
            await manager.wait_history_start(result["call_id"])
            return result
        if call.service == "hangup":
            return await manager.end_call(user.id, call.data["call_id"], user.is_admin)
        if call.service == "announce":
            from .manager_announcements import start
            targets = manager.resolve_targets(call.data.get("target_room_ids"), call.data.get("group_id"))
            return await start(manager, user.id, principal, targets, call.data["source"])
        raise Refused("UNSUPPORTED_OPERATION")

    hass.services.async_register(DOMAIN, "room_call", service, schema=probatio.Schema({
        probatio.Required("source"): str, probatio.Required("target_room_ids"): [str],
        probatio.Optional("mode", default="full_duplex"): str,
        probatio.Optional("answer_policy", default="drop_in"): str}), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "hangup", service, schema=probatio.Schema({
        probatio.Required("call_id"): str}), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "announce", service, schema=probatio.Schema({
        probatio.Optional("target_room_ids"): [str], probatio.Optional("group_id"): str,
        probatio.Required("source"): dict}), supports_response=SupportsResponse.OPTIONAL)
