"""Create the empty manager; preserve first-migration singleton options."""

from __future__ import annotations

from typing import Any

import probatio

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.helpers.storage import Store

from . import (
    CONF_ALLOWED_USERS,
    BACKEND_REAL,
    BACKEND_SIMULATION,
    CONF_BACKEND,
    CONF_INTERNAL_ORIGIN,
    CONF_NABU_ORIGIN,
    CONF_ROOM_NAME,
    DEFAULT_ROOM_NAME,
    DOMAIN,
    DATA_COMPONENT,
)
from .endpoint import EndpointConfig
from .session import Refused, canonical_origin
from .manager_deployment import DeploymentError
from .speaker_deployment import SpeakerTarget, EphemeralCredentials, PreflightError


ENDPOINT_FIELDS = (
    "endpoint_host", "endpoint_port", "expected_endpoint_uuid",
    "expected_endpoint_uri", "expected_endpoint_spki_sha256",
    "endpoint_ca_path", "endpoint_client_cert_path", "endpoint_client_key_path",
)


def _room_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid room name")
    name = value.strip()
    if not 1 <= len(name) <= 64 or any(not char.isprintable() for char in name):
        raise ValueError("invalid room name")
    return name


class IntercomM3PocConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure the manager's existing user and secure-ingress boundary."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        users = {
            user.id: user.name or user.id
            for user in await self.hass.auth.async_get_users()
            if user.is_active and not user.system_generated
        }
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input[CONF_ALLOWED_USERS]
            internal = user_input.get(CONF_INTERNAL_ORIGIN, "").strip()
            nabu = user_input.get(CONF_NABU_ORIGIN, "").strip()
            if not selected or any(user_id not in users for user_id in selected):
                errors["base"] = "invalid_users"
            try:
                internal = canonical_origin(internal) if internal else ""
                nabu = canonical_origin(nabu) if nabu else ""
                if not internal and not nabu:
                    raise Refused("SECURE_INGRESS_REQUIRED")
            except Refused:
                errors["base"] = "invalid_origin"
            if not errors:
                return self.async_create_entry(
                    title="Intercom Manager",
                    data={
                        "manager_bootstrap": "empty",
                        CONF_ALLOWED_USERS: list(selected),
                        CONF_INTERNAL_ORIGIN: internal,
                        CONF_NABU_ORIGIN: nabu,
                    },
                )
        schema = probatio.Schema(
            {
                probatio.Required(CONF_ALLOWED_USERS): cv.multi_select(users),
                probatio.Optional(CONF_INTERNAL_ORIGIN, default=""): str,
                probatio.Optional(CONF_NABU_ORIGIN, default=""): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    def async_get_options_flow(config_entry):
        return IntercomM3PocOptionsFlow(config_entry)


class IntercomM3PocOptionsFlow(config_entries.OptionsFlow):
    """Private native speaker enrollment; retain first-migration options."""

    def __init__(self, config_entry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        key = f"intercom_m3_poc.manager.{self._entry.entry_id}"
        if (self._entry.data.get("manager_bootstrap") == "empty"
                or await Store(self.hass, 1, key).async_load() is not None
                or await Store(self.hass, 1, key + ".migration_backup").async_load() is not None):
            return await self.async_step_manager()
        configured = {**self._entry.data, **self._entry.options}
        if user_input is not None:
            backend = user_input[CONF_BACKEND]
            try:
                self._room_name = _room_name(
                    user_input.get(CONF_ROOM_NAME,
                                   configured.get(CONF_ROOM_NAME, DEFAULT_ROOM_NAME)))
            except ValueError:
                return self.async_show_form(
                    step_id="init", data_schema=self._init_schema(configured),
                    errors={"base": "invalid_room_name"})
            if backend == BACKEND_REAL:
                return await self.async_step_endpoint()
            saved = {key: configured[key] for key in ENDPOINT_FIELDS if key in configured}
            return self.async_create_entry(
                title="", data={**saved, CONF_ROOM_NAME: self._room_name,
                                CONF_BACKEND: BACKEND_SIMULATION})
        return self.async_show_form(step_id="init", data_schema=self._init_schema(configured))

    def _deployment(self):
        component = self.hass.data.get(DATA_COMPONENT)
        controller = getattr(component, "deployment", None)
        if controller is None or controller.closed:
            raise DeploymentError("DEPLOYMENT_UNAVAILABLE")
        return controller

    async def async_step_manager(self, user_input=None):
        return self.async_show_menu(step_id="manager", menu_options=["speaker", "select_speaker", "rooms"])

    async def async_step_rooms(self, user_input=None):
        return self.async_abort(reason="manage_rooms")

    async def async_step_select_speaker(self, user_input=None):
        try:
            controller = self._deployment()
        except DeploymentError:
            return self.async_abort(reason="deployment_unavailable")
        choices = {row["device_id"]: row["label"] for row in controller.status()["devices"]}
        if not choices:
            return self.async_abort(reason="no_speakers")
        if user_input is not None:
            if user_input.get("device_id") not in choices:
                return self.async_abort(reason="deployment_changed")
            self._speaker_device_id = user_input["device_id"]
            return await self.async_step_speaker()
        return self.async_show_form(step_id="select_speaker", data_schema=probatio.Schema({
            probatio.Required("device_id"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": device_id, "label": label} for device_id, label in choices.items()]))
        }))

    async def async_step_speaker(self, user_input=None):
        try:
            controller = self._deployment()
        except DeploymentError:
            return self.async_abort(reason="deployment_unavailable")
        # A native options flow holds the revision, never a submitted authority field.
        if not hasattr(self, "_deployment_revision"):
            self._deployment_revision = controller.status()["revision"]
        errors = {}
        if user_input is not None:
            try:
                mac = user_input["mac"].strip().lower()
                target = SpeakerTarget(host=user_input["host"].strip(), port=22,
                    serial="3RSPK-" + mac.replace(":", "").upper(), mac=mac,
                    model="3RAB01090D", architecture="aarch64",
                    host_key_sha256=user_input["host_key_sha256"].strip())
                await controller.configure(self._deployment_revision, target,
                    EphemeralCredentials(user_input["password"]), user_input["label"],
                    getattr(self, "_speaker_device_id", None))
            except PreflightError:
                errors["base"] = "invalid_speaker"
            except DeploymentError as error:
                if str(error) == "DEPLOYMENT_REVISION_CONFLICT":
                    return self.async_abort(reason="deployment_changed")
                errors["base"] = "speaker_configuration_failed"
            except (KeyError, TypeError, ValueError):
                errors["base"] = "invalid_speaker"
            else:
                # Secrets live in the controller's protected directory, never entry
                # data/options. Abort completes without reloading working calls.
                return self.async_abort(reason="speaker_connection_saved")
        return self.async_show_form(step_id="speaker", errors=errors,
            data_schema=probatio.Schema({
                probatio.Required("label"): str,
                probatio.Required("host"): str,
                probatio.Required("mac"): str,
                probatio.Required("host_key_sha256"): str,
                probatio.Required("password"): selector.TextSelector(selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD)),
            }))

    @staticmethod
    def _init_schema(configured: dict[str, Any]):
        choices = {
            BACKEND_SIMULATION: "Simulation",
            BACKEND_REAL: "Real authenticated endpoint",
        }
        return probatio.Schema({
            probatio.Required(CONF_BACKEND, default=configured.get(CONF_BACKEND, BACKEND_SIMULATION)):
                probatio.In(choices),
            probatio.Required(CONF_ROOM_NAME,
                              default=configured.get(CONF_ROOM_NAME, DEFAULT_ROOM_NAME)): str,
        })

    async def async_step_endpoint(self, user_input: dict[str, Any] | None = None):
        configured = {**self._entry.data, **self._entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            values = dict(user_input)
            endpoint_uuid = str(values.get("expected_endpoint_uuid", "")).strip()
            values["expected_endpoint_uri"] = f"urn:intercom:endpoint:{endpoint_uuid}"
            try:
                endpoint = await self.hass.async_add_executor_job(
                    EndpointConfig.from_mapping, values)
            except (OSError, ValueError):
                errors["base"] = "invalid_endpoint"
            else:
                return self.async_create_entry(title="", data={
                    CONF_BACKEND: BACKEND_REAL,
                    CONF_ROOM_NAME: getattr(
                        self, "_room_name", configured.get(CONF_ROOM_NAME, DEFAULT_ROOM_NAME)),
                    "endpoint_host": endpoint.host,
                    "endpoint_port": endpoint.port,
                    "expected_endpoint_uuid": str(endpoint.expected_uuid),
                    "expected_endpoint_uri": endpoint.expected_uri,
                    "expected_endpoint_spki_sha256": endpoint.expected_spki_sha256,
                    "endpoint_ca_path": endpoint.ca_path,
                    "endpoint_client_cert_path": endpoint.client_cert_path,
                    "endpoint_client_key_path": endpoint.client_key_path,
                })
        schema = probatio.Schema({
            probatio.Required("endpoint_host", default=configured.get("endpoint_host", "")): str,
            probatio.Required("endpoint_port", default=configured.get("endpoint_port", 6054)): cv.port,
            probatio.Required("expected_endpoint_uuid", default=configured.get("expected_endpoint_uuid", "")): str,
            probatio.Required("expected_endpoint_spki_sha256", default=configured.get("expected_endpoint_spki_sha256", "")): str,
            probatio.Required("endpoint_ca_path", default=configured.get("endpoint_ca_path", "")): str,
            probatio.Required("endpoint_client_cert_path", default=configured.get("endpoint_client_cert_path", "")): str,
            probatio.Required("endpoint_client_key_path", default=configured.get("endpoint_client_key_path", "")): str,
        })
        return self.async_show_form(
            step_id="endpoint", data_schema=schema, errors=errors)
