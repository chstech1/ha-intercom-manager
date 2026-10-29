"""Bounded speaker-only playback of approved local files and verified local TTS."""
from __future__ import annotations

import asyncio
import io
import ipaddress
from pathlib import Path
import re
import secrets
import time
import wave

from .endpoint import EndpointEnded, SAMPLES, run_endpoint
from .session import Refused

FILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}\.wav$")
MAX_SECONDS = 60
MAX_PCM = MAX_SECONDS * 16000 * 2


def file_root(hass):
    return Path(hass.config.path("intercom_m3_poc", "announcements")).resolve()


def read_file(hass, file_id):
    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
        raise Refused("INVALID_SOURCE")
    root = file_root(hass)
    path = (root / file_id).resolve()
    if path.parent != root or not path.is_file() or path.stat().st_size > MAX_PCM + 4096:
        raise Refused("INVALID_SOURCE")
    try:
        with wave.open(str(path), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, 16000, "NONE") or source.getnframes() > 16000 * MAX_SECONDS:
                raise Refused("UNSUPPORTED_AUDIO_FORMAT")
            pcm = source.readframes(source.getnframes())
    except (OSError, EOFError, wave.Error):
        raise Refused("INVALID_SOURCE") from None
    if not pcm:
        raise Refused("EMPTY_SOURCE")
    return pcm


def local_tts_engine(hass, engine_id):
    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)
    entity = registry.async_get(engine_id)
    if entity is None or entity.domain != "tts" or entity.platform != "wyoming" or not entity.config_entry_id:
        return False
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    if entry is None:
        return False
    try:
        host = ipaddress.ip_address(entry.data.get("host", ""))
    except ValueError:
        return False
    return not host.is_unspecified and (host.is_loopback or host.is_private)


async def sources(manager):
    from homeassistant.helpers import entity_registry as er
    def files():
        root = file_root(manager.hass)
        if not root.is_dir():
            return []
        return [{"file_id": path.name, "name": path.stem.replace("_", " ")}
                for path in sorted(root.iterdir())[:256]
                if FILE_ID.fullmatch(path.name) and path.is_file() and path.resolve().parent == root]
    approved = await manager.hass.async_add_executor_job(files)
    engines = [{"engine_id": e.entity_id, "name": e.name or e.original_name or e.entity_id}
               for e in er.async_get(manager.hass).entities.values()
               if local_tts_engine(manager.hass, e.entity_id)]
    return {"files": approved, "tts_engines": engines, "live_supported": True}


async def prepare(manager, source):
    if not isinstance(source, dict):
        raise Refused("INVALID_SOURCE")
    if source.get("kind") == "file" and set(source) == {"kind", "file_id"}:
        return await manager.hass.async_add_executor_job(read_file, manager.hass, source["file_id"])
    if source.get("kind") != "tts" or set(source) != {"kind", "engine_id", "message"}:
        raise Refused("UNSUPPORTED_SOURCE")
    message, engine = source["message"], source["engine_id"]
    if not isinstance(message, str) or not 1 <= len(message) <= 500 or not message.isprintable() or not isinstance(engine, str) or not local_tts_engine(manager.hass, engine):
        raise Refused("LOCAL_TTS_REQUIRED")
    from homeassistant.components import tts
    media_id = tts.generate_media_source_id(manager.hass, message, engine=engine, cache=False)
    async def bounded_tts():
        # Same native stream as async_get_media_source_audio, with the size ceiling enforced during collection.
        parsed = tts.parse_media_source_id(media_id)
        stream = manager.hass.data[tts.DATA_TTS_MANAGER].async_create_result_stream(**parsed["options"])
        stream.async_set_message(parsed["message"])
        chunks = stream.async_stream_result()
        output = bytearray()
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes) or len(output) + len(chunk) > 4 * 1024 * 1024:
                    raise Refused("SOURCE_TOO_LARGE")
                output.extend(chunk)
        finally:
            close = getattr(chunks, "aclose", None)
            if close:
                await close()
        if not output:
            raise Refused("EMPTY_SOURCE")
        return bytes(output)
    data = await asyncio.wait_for(bounded_tts(), 20)
    process = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-i", "pipe:0", "-t", str(MAX_SECONDS), "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        pcm, _ = await asyncio.wait_for(process.communicate(data), 15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode or not pcm or len(pcm) > MAX_PCM:
        raise Refused("TTS_AUDIO_FAILED")
    return pcm


async def start(manager, user_id, principal, room_ids, source):
    if not isinstance(room_ids, list) or not room_ids or len(room_ids) > 8 or any(not isinstance(rid, str) for rid in room_ids) or len(set(room_ids)) != len(room_ids):
        raise Refused("INVALID_PARTICIPANTS")
    pcm = await prepare(manager, source)
    accepted, results = [], []
    for rid in room_ids:
        try:
            manager.require(user_id, [rid], "announce")
            manager.reserve([rid])
            runtime = manager.runtimes[rid]
            if runtime.endpoint_config is None:
                raise Refused("REAL_ENDPOINT_REQUIRED")
        except Refused as err:
            results.append({"room_id": rid, "status": "skipped", "reason": str(err)})
        else:
            accepted.append(rid)
            results.append({"room_id": rid, "status": "accepted", "reason": "ACCEPTED"})
    if not accepted:
        return {"call_id": None, "state": "TERMINATED", "results": results}
    manager.reserve(accepted)
    from . import _b64
    raw_id = secrets.token_bytes(16)
    call = {"call_id": _b64(raw_id), "type": "announcement", "room_ids": accepted,
        "state": "PREPARING", "reason": "STARTED", "started_at": time.time(),
        "user_id": user_id, "principal": principal, "stop": asyncio.Event(), "metrics": {},
        "results": results}
    manager.calls[call["call_id"]] = call
    if hasattr(manager, "begin_history"):
        manager.begin_history(call)
    manager.spawn(run(manager, call, raw_id, pcm))
    manager.notify()
    if hasattr(manager, "wait_history_start"):
        await manager.wait_history_start(call["call_id"])
    return {"call_id": call["call_id"], "state": call["state"], "results": results}


async def run(manager, call, raw_id, pcm):
    loop = asyncio.get_running_loop()
    generation = secrets.randbits(64) or 1
    async def authorized(rid):
        principal = call["principal"]
        if manager.closed or call["stop"].is_set() or not manager.permitted(call["user_id"], rid, "announce") or manager.dnd(rid):
            return False
        if principal.ingress_class in ("ha_service", "automation"):
            user = await manager.hass.auth.async_get_user(principal.user_id)
        else:
            token = manager.hass.auth.async_get_refresh_token(principal.refresh_token_id)
            user = getattr(token, "user", None)
        from . import ENTITY_ID, POLICY_CONTROL
        return (user is not None and user.is_active and user.id == principal.user_id
                and user.permissions.check_entity(ENTITY_ID, POLICY_CONTROL)
                and (principal.ingress_class != "automation" or user.is_admin and manager.automation_current(principal, "announce", [rid])))

    async def destination(rid):
        result = next(item for item in call["results"] if item["room_id"] == rid)
        try:
            async def bridge(client):
                await client.start()
                deadline = loop.time() + 8
                silence = bytes(SAMPLES * 2)
                while not client.ready(loop.time()):
                    if not await authorized(rid):
                        raise EndpointEnded("AUTH_REVOKED")
                    if loop.time() >= deadline:
                        raise EndpointEnded("SETUP_TIMEOUT")
                    client.send_audio(int(loop.time() * 1000000), 0, silence)
                    await asyncio.sleep(.032)
                call["state"], call["reason"] = "ACTIVE_SPEAKER_ONLY", "ANNOUNCEMENT"
                call["last_report"] = loop.time()
                manager.notify()
                scheduled = loop.time()
                for offset in range(0, len(pcm), SAMPLES * 2):
                    if not await authorized(rid):
                        raise EndpointEnded("AUTH_REVOKED")
                    if not client.ready(loop.time()):
                        raise EndpointEnded("MEDIA_NO_PROGRESS")
                    frame = pcm[offset:offset + SAMPLES * 2].ljust(SAMPLES * 2, b"\0")
                    client.send_audio(int(loop.time() * 1000000), 0, frame)
                    call["last_report"] = loop.time()
                    scheduled += .032
                    await asyncio.sleep(max(0, scheduled - loop.time()))
                await asyncio.sleep(.15)
                raise EndpointEnded("ANNOUNCEMENT_COMPLETE")
            config = manager.runtimes[rid].endpoint_config
            kwargs = {"pool": manager.endpoint_pool} if getattr(manager, "endpoint_pool", None) is not None else {}
            if hasattr(manager, "room_volume_config"):
                volume = manager.room_volume_config(rid, announcement=True)
                if volume is not None:
                    kwargs["volume_config"] = volume
            await run_endpoint(config, raw_id, generation, "speaker_only", 0, bridge,
                               lambda: authorized(rid), **kwargs)
        except EndpointEnded as err:
            result["reason"] = str(err) if re.fullmatch(r"[A-Z0-9_]{1,40}", str(err)) else "ENDPOINT_ERROR"
            result["status"] = "completed" if result["reason"] == "ANNOUNCEMENT_COMPLETE" else "failed"
        except asyncio.CancelledError:
            result.update(status="failed", reason="INTERRUPTED")
            raise
        except Exception:
            result.update(status="failed", reason="ENDPOINT_ERROR")
        finally:
            manager.notify()
    try:
        if hasattr(manager, "wait_history_start"):
            await manager.wait_history_start(call["call_id"])
        await asyncio.gather(*(destination(rid) for rid in call["room_ids"]))
    finally:
        call["state"] = "TERMINATED"
        manager.calls.pop(call["call_id"], None)
        if hasattr(manager, "remember_terminal"):
            manager.remember_terminal(call)
        manager.notify()
        ended = time.time()
        await manager.record({k: call[k] for k in ("call_id", "type", "room_ids", "started_at")} |
            {"ended_at": ended, "duration_seconds": max(0, ended - call["started_at"]),
             "outcome": "completed" if all(r["status"] == "completed" for r in call["results"] if r["room_id"] in call["room_ids"]) else "failed",
             "reason": "ANNOUNCEMENT_ENDED", "results": call["results"]})
