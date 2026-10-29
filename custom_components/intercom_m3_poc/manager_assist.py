"""Local Assist authority and STT-only commands; no client can mint a wake event.

Trusted endpoint/pipeline hooks are required. Missing hooks keep this feature closed.
No audio, recognized text, principal or request nonce is written to storage/logs.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from dataclasses import dataclass, replace
import re
import time

from homeassistant.helpers import intent
from homeassistant.const import __version__ as HA_VERSION

from .session import Refused

INTENTS = {"IntercomLocalDropIn": "drop_in", "IntercomLocalCall": "ring",
           "IntercomLocalTalk": "ring", "IntercomLocalInvalidCall": None}
COMMANDS = {"answer": "answer", "answer call": "answer", "decline": "decline",
            "decline call": "decline", "hang up": "hang_up", "hangup": "hang_up",
            "end call": "hang_up"}
SOURCE_AGE = 2.0
REQUEST_AGE = 15.0
HOLD_SECONDS = 15.0


@dataclass(frozen=True)
class SourceBinding:
    room_id: str
    device_id: str
    pipeline_id: str
    user_id: str
    principal: object
    stt_entity_id: str


@dataclass(frozen=True)
class SourceEvent:
    request_id: bytes
    sequence: int
    received_at: float
    call_id: str | None = None
    generation: int = 0
    mode_generation: int = 0


@dataclass(frozen=True)
class _Request:
    endpoint: object
    event: SourceEvent
    binding: SourceBinding
    context: object
    revision: int
    boot: object = None
    connection: object = None
    native_task: asyncio.Task | None = None
    native_entity: object = None


def normalized(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or not value.isprintable():
        raise Refused("INVALID_ASSIST_REQUEST")
    return " ".join(value.casefold().split()).rstrip(".!?")


def parse_call(value):
    """Bounded English grammar for fake-STT and trusted idle pipeline adapters."""
    value = normalized(value)
    match = re.fullmatch(r"(drop in(?: on)?|call|i want to talk to) (.{1,64})", value)
    if match is None:
        raise Refused("UNRECOGNIZED_COMMAND")
    return match[2], "drop_in" if match[1].startswith("drop in") else "ring"


class LocalAssist:
    def __init__(self, hass, manager):
        self.hass, self.manager = hass, manager
        self.pending = {}
        self.seen = {}  # one authenticated session high-water mark per enrolled room
        self.tasks = set()
        self.handlers = []
        self.closed = False
        self.supported_core = HA_VERSION == "2026.10.1"

    def _hook(self, name):
        if not self.supported_core:
            raise Refused("ASSIST_CORE_UNSUPPORTED")
        hook = getattr(self.manager, name, None)
        if not callable(hook) or self.closed or self.manager.closed:
            raise Refused("ASSIST_UNAVAILABLE")
        return hook

    def _binding(self, device_id, pipeline_id):
        binding = self._hook("assist_binding")(device_id, pipeline_id)
        if (not isinstance(binding, SourceBinding) or binding.device_id != device_id
                or binding.pipeline_id != pipeline_id or not binding.user_id
                or binding.principal is None):
            raise Refused("NO_ROOM_AUTHORITY")
        self.manager.room(binding.room_id)
        self.manager.require(binding.user_id, [binding.room_id])
        return binding

    def confirm_source(self, endpoint, event, context, device_id, pipeline_id):
        """ONLY the authenticated endpoint reader may call this; not an HA action.

        Reader validates ICM4 frame, exact local wake provenance, replay envelope,
        boot/connection, clock and mute before invoking. The server-owned Context
        object must be passed unchanged into that pipeline's intent dispatch.
        """
        now = time.monotonic()
        if (not isinstance(event, SourceEvent) or type(event.sequence) is not int
                or not 0 < event.sequence < 2**64 or not isinstance(event.request_id, bytes)
                or len(event.request_id) != 16 or event.request_id == bytes(16)
                or type(event.received_at) not in (int, float)
                or not 0 <= now - event.received_at <= SOURCE_AGE
                or context is None or event.sequence != getattr(endpoint, "wake_sequence", getattr(endpoint, "rx_sequence", None))
                or type(event.generation) is not int
                or type(event.mode_generation) is not int or not 0 <= event.mode_generation < 2**32
                or event.mode_generation != getattr(endpoint, "mode_generation", 0)
                or (event.call_id is None and event.generation != 0)
                or (event.call_id is not None and (not isinstance(event.call_id, str)
                                                  or not 0 < event.generation < 2**64))):
            raise Refused("INVALID_SOURCE_EVENT")
        binding = self._binding(device_id, pipeline_id)
        if not self._hook("assist_endpoint_current")(
                endpoint, binding.room_id, event.call_id, event.generation):
            raise Refused("INVALID_SOURCE_EVENT")
        session = (endpoint, getattr(endpoint, "boot", None), getattr(endpoint, "connection", None))
        rooms = {room["room_id"] for room in self.manager.configuration["rooms"]}
        self.seen = {room: item for room, item in self.seen.items() if room in rooms}
        prior = self.seen.get(binding.room_id)
        if prior and prior[0] == session and (event.sequence <= prior[1] or event.request_id == prior[2]):
            raise Refused("SOURCE_REPLAY")
        self.seen[binding.room_id] = (session, event.sequence, event.request_id)
        self.pending = {key: item for key, item in self.pending.items()
                        if now - item.event.received_at <= REQUEST_AGE
                        and item.binding.room_id != binding.room_id}
        if len(self.pending) >= 64:
            raise Refused("ASSIST_CAPACITY")
        self.pending[id(context)] = _Request(endpoint, event, binding, context, self.manager.revision,
            getattr(endpoint,"boot",None),getattr(endpoint,"connection",None))

    def _consume(self, context, device_id):
        request = self.pending.get(id(context))
        if request is None or request.context is not context or request.binding.device_id != device_id:
            raise Refused("NO_LOCAL_CONFIRMATION")
        if request.native_task is not None and asyncio.current_task() is not request.native_task:
            raise Refused("NO_NATIVE_PIPELINE_CONFIRMATION")
        del self.pending[id(context)]
        self._validate(request)
        return request

    def _validate(self, request, mode_generation=None):
        event, binding = request.event, request.binding
        expected_mode = event.mode_generation if mode_generation is None else mode_generation
        if (self.manager.revision != request.revision
                or not 0 <= time.monotonic() - event.received_at <= REQUEST_AGE
                or self._binding(binding.device_id, binding.pipeline_id) != binding
                or request.boot != getattr(request.endpoint,"boot",None)
                or request.connection != getattr(request.endpoint,"connection",None)
                or (request.native_task is not None and (
                    request.native_task.done() or request.native_entity._pipeline_task is not request.native_task
                    or request.native_entity.registry_entry.device_id != binding.device_id
                    or request.native_entity._resolve_pipeline() != binding.pipeline_id))
                or expected_mode != getattr(request.endpoint, "mode_generation", 0)
                or not self._hook("assist_endpoint_current")(
                    request.endpoint, binding.room_id, event.call_id, event.generation)):
            raise Refused("STALE_SOURCE_EVENT")

    def call(self, context, device_id, destination, answer_policy, satellite_id=None):
        candidate = self.pending.get(id(context))
        if candidate and candidate.native_entity and candidate.native_entity.entity_id != satellite_id:
            raise Refused("NO_NATIVE_PIPELINE_CONFIRMATION")
        request = self._consume(context, device_id)
        if request.event.call_id is not None:
            raise Refused("CALL_ALREADY_ACTIVE")
        if answer_policy not in ("drop_in", "ring"):
            raise Refused("INVALID_ASSIST_REQUEST")
        alias = normalized(destination)
        # Keep both interpretations: "The Kitchen" and optional article +
        # "Kitchen" can be different configured rooms and must stay ambiguous.
        aliases = {alias,alias[4:]} if alias.startswith("the ") else {alias}
        binding = request.binding
        matches = [room["room_id"] for room in self.manager.configuration["rooms"]
                   if self.manager.permitted(binding.user_id, room["room_id"])
                   and aliases & {normalized(name) for name in [room["name"], *room["aliases"]]}]
        if len(matches) != 1:
            raise Refused("AMBIGUOUS_ROOM" if matches else "UNKNOWN_ROOM")
        if matches[0] == binding.room_id:
            raise Refused("SAME_ROOM")
        self.manager.require(binding.user_id, [binding.room_id, matches[0]])
        return self.manager.start_room_call(binding.user_id, binding.principal,
            [binding.room_id, matches[0]], "full_duplex", answer_policy)

    async def _async_bind_idle_native(self, endpoint, event, binding, expected):
        """Pinned Core early binding; never inspect coroutine frames or patch HA."""
        from homeassistant.components import assist_satellite
        from homeassistant.components.assist_satellite.entity import AssistSatelliteEntity
        component = self.hass.data.get(assist_satellite.DATA_COMPONENT)
        natives = [entity for entity in component.entities if isinstance(entity,AssistSatelliteEntity)
            and entity.registry_entry is not None and entity.registry_entry.platform == "esphome"
            and entity.registry_entry.device_id == binding.device_id
            and entity._resolve_pipeline() == binding.pipeline_id] if component else []
        if len(natives) != 1:
            raise Refused("NATIVE_PIPELINE_UNAVAILABLE")
        native = natives[0]
        previous = getattr(native,"_pipeline_task",None)
        wake_wall = time.time() - (time.monotonic()-event["received_at"])
        sentinel = object()
        self.confirm_source(endpoint,SourceEvent(expected,event["sequence"],event["received_at"]),
            sentinel,binding.device_id,binding.pipeline_id)
        request = self.pending[id(sentinel)]
        waiting = asyncio.current_task()
        self.tasks.add(waiting)
        try:
            async with asyncio.timeout_at(event["received_at"]+SOURCE_AGE):
                while True:
                    self._validate(request)
                    task = getattr(native,"_pipeline_task",None)
                    context = getattr(native,"_context",None)
                    created = getattr(native,"_context_set",None)
                    if (isinstance(task,asyncio.Task) and task is not previous and not task.done()
                            and context is not None and context.user_id is None
                            and type(created) in (int,float) and wake_wall <= created <= wake_wall+SOURCE_AGE):
                        bound = replace(request,context=context,native_task=task,native_entity=native)
                        self.pending.pop(id(sentinel),None)
                        self.pending[id(context)] = bound
                        def forget(*_):
                            if self.pending.get(id(context)) is bound:
                                self.pending.pop(id(context),None)
                        task.add_done_callback(forget)
                        asyncio.get_running_loop().call_at(event["received_at"]+REQUEST_AGE,forget)
                        return {"confirmed":True}
                    await asyncio.sleep(.01)
        finally:
            self.tasks.discard(waiting)
            self.pending.pop(id(sentinel),None)

    async def async_endpoint_wake(self, endpoint, event):
        """Direct EndpointPool.wake_handler(client, eventdict) for active calls.

        The pool owns client identity. A HA registry/admin binding maps this exact
        client to its device/pipeline/STT entity; callback accepts no caller IDs.
        Idle native-pipeline bridging uses confirm_source with its actual Context.
        """
        binding = self._hook("assist_binding_for_endpoint")(endpoint)
        if not isinstance(binding, SourceBinding):
            raise Refused("NO_ROOM_AUTHORITY")
        if (not isinstance(event, dict) or set(event) != {
                "request_id", "sequence", "received_at", "call_id", "generation", "token"}
                or type(event["token"]) is not int or not 0 < event["token"] < 2**64):
            raise Refused("INVALID_SOURCE_EVENT")
        token = event["token"]
        expected = hashlib.sha256(endpoint.boot + endpoint.connection
                                  + struct.pack("<Q", token)).digest()[:16]
        if (event["request_id"] != expected or event["sequence"] != endpoint.wake_sequence
                or event["received_at"] != endpoint.wake_received_at
                or event["generation"] != endpoint.mode_generation):
            raise Refused("INVALID_SOURCE_EVENT")
        if event["call_id"] is None:
            if (token != endpoint.last_wake_token or endpoint.call != bytes(16)
                    or endpoint.owner or endpoint.started or endpoint.held or event["generation"]):
                raise Refused("INVALID_SOURCE_EVENT")
            return await self._async_bind_idle_native(endpoint,event,binding,expected)
        if token != endpoint.command_token or not endpoint.held:
            raise Refused("INVALID_SOURCE_EVENT")
        from homeassistant.core import Context
        import uuid
        if event["call_id"] != str(uuid.UUID(bytes=endpoint.call)):
            raise Refused("STALE_CALL")
        # Type19 precedes the separate authenticated flush ACK. Admit only after
        # that ACK; the transport reader keeps running while this callback waits.
        await asyncio.wait_for(endpoint.command_ready.wait(), 1)
        context = Context(user_id=binding.user_id)
        call_id = base64.urlsafe_b64encode(endpoint.call).rstrip(b"=").decode("ascii")
        source = SourceEvent(expected, event["sequence"], event["received_at"],
            call_id, endpoint.owner, endpoint.mode_generation)
        self.confirm_source(endpoint, source, context, binding.device_id, binding.pipeline_id)
        async def command_pcm():
            from homeassistant.components.assist_pipeline.vad import AudioBuffer, VoiceCommandSegmenter
            from pymicro_vad import MicroVad
            vad = MicroVad()
            segmenter = VoiceCommandSegmenter(timeout_seconds=10)
            leftover = AudioBuffer(320)
            await endpoint.command_ready.wait()  # actual firmware flush ACK
            while endpoint.held and endpoint.command_token == token:
                generated, prior, pcm = await endpoint.command_capture.get()
                age = max(0, int(time.monotonic()*1_000_000)-generated) + prior
                if age >= 2_000_000:
                    raise Refused("COMMAND_AUDIO_EXPIRED")
                yield pcm
                if not segmenter.process_with_vad(pcm, 160, vad.Process10ms, leftover):
                    if segmenter.timed_out:
                        raise Refused("COMMAND_SPEECH_TIMEOUT")
                    return
        return await self.async_command(context, binding.device_id, command_pcm())

    async def async_command(self, context, device_id, stream):
        """Run local STT directly, never HA conversation or general intents.

        stream is a volatile async PCM iterator from the separate command channel.
        The endpoint hold ACK must precede its first iteration. Mute/authority and
        exact generation are rechecked for each chunk and again before action.
        """
        request = self._consume(context, device_id)
        event, binding = request.event, request.binding
        if event.call_id is None:
            raise Refused("NO_ACTIVE_CALL")
        current = self._hook("assist_call_current")
        hold = self._hook("async_assist_hold")
        action = self._hook("async_assist_action")
        if not self._hook("assist_local_stt")(binding.stt_entity_id):
            raise Refused("LOCAL_STT_REQUIRED")
        def validate(mode_generation=None):
            self._validate(request, mode_generation)
            if not current(binding.room_id, event.call_id, event.generation):
                raise Refused("STALE_CALL")
        validate()
        task = asyncio.current_task()
        self.tasks.add(task)
        held = False
        try:
            # One second reserved for release; endpoint independently enforces 15s.
            async with asyncio.timeout_at(event.received_at + HOLD_SECONDS - 1):
                held = True  # release even if cancellation races the begin ACK
                if not await hold(binding.room_id, event.call_id, event.generation, True):
                    raise Refused("COMMAND_HOLD_FAILED")
                validate()
                from homeassistant.components import stt
                engine = stt.async_get_speech_to_text_entity(self.hass, binding.stt_entity_id)
                if engine is None:
                    raise Refused("LOCAL_STT_REQUIRED")
                metadata = stt.SpeechMetadata(language="en", format=stt.AudioFormats.WAV,
                    codec=stt.AudioCodecs.PCM, bit_rate=stt.AudioBitRates.BITRATE_16,
                    sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
                    channel=stt.AudioChannels.CHANNEL_MONO)
                async def bounded_stream():
                    total = 0
                    async for chunk in stream:
                        validate()
                        if not isinstance(chunk, bytes) or not chunk or len(chunk) > 4096 or len(chunk) % 2:
                            raise Refused("INVALID_COMMAND_AUDIO")
                        total += len(chunk)
                        if total > 16000 * 2 * 14:
                            raise Refused("COMMAND_AUDIO_LIMIT")
                        yield chunk
                result = await engine.async_process_audio_stream(metadata, bounded_stream())
                validate()
                if result.result != stt.SpeechResultState.SUCCESS or not result.text:
                    return {"recognized": False}
                command = COMMANDS.get(normalized(result.text))
                if command is None:
                    return {"recognized": False}
                if command == "answer":
                    # Resume is not acceptance. Finish the exact flush/release
                    # ACK first, then recheck the same ringing call and apply
                    # explicit answer at its new mode generation. Never open
                    # conversation while the command recording is held.
                    if event.mode_generation == 2**32 - 1:
                        raise Refused("STALE_CALL")
                    held = False  # manager owns cutoff on failed release; no duplicate op5
                    if not await asyncio.wait_for(hold(
                            binding.room_id, event.call_id, event.generation, False), 1):
                        raise Refused("COMMAND_RELEASE_FAILED")
                    validate(event.mode_generation + 1)
                await action(binding.room_id, event.call_id, event.generation, command)
                return {"recognized": True, "action": command}
        except Exception:
            # STT/provider errors contain arbitrary text; never log/store it.
            return {"recognized": False}
        finally:
            self.tasks.discard(task)
            if held:
                # Manager must never release a newer call from this old callback.
                try:
                    async with asyncio.timeout(1):
                        await hold(binding.room_id, event.call_id, event.generation, False)
                except Exception:
                    pass  # endpoint hard deadline remains independently mandatory
                close = getattr(stream, "aclose", None)
                if close:
                    try:
                        async with asyncio.timeout(1):
                            await close()
                    except Exception:
                        pass


class _CallIntent(intent.IntentHandler):
    def __init__(self, controller, intent_type, policy):
        self.controller, self.intent_type, self.policy = controller, intent_type, policy

    async def async_handle(self, intent_obj):
        response = intent_obj.create_response()
        try:
            destination = intent_obj.slots.get("room", {}).get("value")
            result = self.controller.call(intent_obj.context, intent_obj.device_id, destination, self.policy,intent_obj.satellite_id)
            await self.controller.manager.wait_history_start(result["call_id"])
            response.async_set_speech("Calling.")
        except Refused:
            # Fixed response does not leak aliases, caller identity or backend errors.
            response.async_set_speech("That intercom request is unavailable.")
        return response


async def async_setup_assist(hass, manager):
    existing = getattr(manager, "assist", None)
    if isinstance(existing, LocalAssist) and not existing.closed:
        return existing
    controller = LocalAssist(hass, manager)
    # Refuse collisions rather than replacing another integration's handlers.
    registered = hass.data.get(intent.DATA_KEY, {})
    if any(name in registered for name in INTENTS):
        raise Refused("ASSIST_INTENT_COLLISION")
    for name, policy in INTENTS.items():
        handler = _CallIntent(controller, name, policy)
        intent.async_register(hass, handler)
        controller.handlers.append(handler)
    manager.assist = controller
    return controller


async def async_unload_assist(hass, manager):
    controller = getattr(manager, "assist", None)
    if not isinstance(controller, LocalAssist):
        return
    controller.closed = True
    controller.pending.clear()
    controller.seen.clear()
    tasks = list(controller.tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for handler in controller.handlers:
        if hass.data.get(intent.DATA_KEY, {}).get(handler.intent_type) is handler:
            intent.async_remove(hass, handler.intent_type)
    controller.handlers.clear()
    manager.assist = None
