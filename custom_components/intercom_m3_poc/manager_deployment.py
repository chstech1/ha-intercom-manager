"""HA-owned bounded deployment workers and protected first-device admission."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import time
import uuid

from homeassistant.helpers.storage import Store

from .speaker_deployment import (SpeakerTarget, EphemeralCredentials, PreflightError,
    read_only_preflight, read_private_preferences, stage_permanent_package, permanent_action,
    cleanup_permanent_stage, ApprovedBuild, _validate_target, RAM_SHA256, STOCK_SHA256)

ACTIONS = {"install", "rollback_previous", "restore_stock", "remove"}
PUBLIC_FILES = {"application", "S99ha-speaker", "bootstrap.sh", "transaction.sh",
                "S98intercom-recover", "attention.wav", "bundle.sha256", "compatibility.json"}
LIVE_BLOCKERS = ["EXACT_PHYSICAL_CHECKPOINT_REQUIRED", "NATIVE_HA_CLOCK_ACCEPTANCE_REQUIRED",
                 "MONITOR_ADOPTION_DEVICE_ACCEPTANCE_REQUIRED", "DURABLE_RECOVERY_DEVICE_ACCEPTANCE_REQUIRED"]
ALPHA_LIMITATIONS = ["PRIVATE_CLOCK_FLOOR_NOT_PROTECTED_ANTIROLLBACK", "SEVEN_DAY_ALPHA_ENROLLMENT"]
FIXED_ACCESS_ERRORS = {"AUTHENTICATION_FAILED", "HOST_KEY_REJECTED", "IDENTITY_MISMATCH", "UNSUPPORTED_PLATFORM", "INVALID_ENROLLMENT"}
REMOTE_PRIVATE = "/data/intercom-m56-install/private"
MAX_PRIVATE_JSON = 32768


class DeploymentError(Exception):
    """Fixed safe code only."""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _private_directory(path):
    if os.name != "posix":
        raise DeploymentError("UNSUPPORTED_HA_HOST")
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
    value = path.lstat()
    if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o700 or path.resolve() != path.absolute()):
        raise DeploymentError("UNSAFE_PRIVATE_STORAGE")


def _private_write(path, data):
    _private_directory(path.parent)
    temporary = path.parent / (".new-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if path.is_symlink():
            raise DeploymentError("UNSAFE_PRIVATE_STORAGE")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _private_read(path):
    _private_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        value = os.fstat(source.fileno())
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid()
                or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1
                or value.st_size > MAX_PRIVATE_JSON):
            raise DeploymentError("UNSAFE_PRIVATE_STORAGE")
        return source.read(MAX_PRIVATE_JSON + 1)


def _json_write(path, value):
    _private_write(path, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())


def _catalog(root, compatibility_pin, manifest_pin):
    if root is None or not compatibility_pin or not manifest_pin:
        raise DeploymentError("PACKAGE_UNAVAILABLE")
    files = {}
    if root.is_symlink() or {path.name for path in root.iterdir()} != PUBLIC_FILES:
        raise DeploymentError("INVALID_PACKAGE")
    for name in PUBLIC_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise DeploymentError("INVALID_PACKAGE")
        files[name] = path.read_bytes()
    if _sha(files["compatibility.json"]) != compatibility_pin or _sha(files["bundle.sha256"]) != manifest_pin:
        raise DeploymentError("PACKAGE_CHANGED")
    catalog = json.loads(files["compatibility.json"])
    if (catalog.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{64}", catalog.get("application_sha256", ""))
            or type(catalog.get("live_activation")) is not bool or not isinstance(catalog.get("version"), str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", catalog["version"])
            or catalog.get("platform", {}).get("ha_boot_clock_binding") != 1):
        raise DeploymentError("INVALID_PACKAGE")
    payloads = PUBLIC_FILES - {"bundle.sha256", "compatibility.json"}
    expected = catalog.get("files")
    if not isinstance(expected, dict) or set(expected) != payloads:
        raise DeploymentError("INVALID_PACKAGE")
    actual = {name: _sha(files[name]) for name in payloads}
    manifest = dict((line.split("  ", 1)[1], line.split("  ", 1)[0])
                    for line in files["bundle.sha256"].decode("ascii").splitlines())
    if expected != actual or manifest != actual or len(files["bundle.sha256"].splitlines()) != len(payloads):
        raise DeploymentError("PACKAGE_CHANGED")
    head = files["application"][:20]
    if (actual["application"] != catalog["application_sha256"] or head[:6] != b"\x7fELF\x02\x01"
            or len(head) != 20 or int.from_bytes(head[18:20], "little") != 183):
        raise DeploymentError("INVALID_PACKAGE")
    return catalog, files


def _identities(directory, device_id, ha_id):
    """Existing RSA2048/URI/EKU interop pattern; signing key stays in memory."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start, end = now - timedelta(minutes=1), now + timedelta(days=7)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Intercom alpha enrollment CA")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
        .sign(key, hashes.SHA256()))
    _private_write(directory / "ca.pem", ca.public_bytes(serialization.Encoding.PEM))
    pins = {}
    for label, identity, role in (("endpoint", device_id, ExtendedKeyUsageOID.SERVER_AUTH),
                                  ("ha", ha_id, ExtendedKeyUsageOID.CLIENT_AUTH)):
        leaf = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        uri = f"urn:intercom:{label}:{identity}"
        certificate = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)]))
            .issuer_name(name).public_key(leaf.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf.public_key()), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), True)
            .add_extension(x509.ExtendedKeyUsage([role]), False)
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]), False)
            .sign(key, hashes.SHA256()))
        _private_write(directory / (label + ".pem"), certificate.public_bytes(serialization.Encoding.PEM))
        _private_write(directory / (label + ".key"), leaf.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        pins[label] = _sha(leaf.public_key().public_bytes(serialization.Encoding.DER,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo))
    return pins, end.isoformat()


class DeploymentManager:
    """One entry's finite deployment operations; no automatic device activation."""
    def __init__(self, hass, entry_id, private_root, bundle_root=None,
                 compatibility_sha256=None, bundle_manifest_sha256=None):
        self.hass = hass
        self.root = Path(private_root).absolute()
        self.bundle_root = Path(bundle_root) if bundle_root is not None else None
        self.compatibility_pin, self.manifest_pin = compatibility_sha256, bundle_manifest_sha256
        self.store = Store(hass, 1, f"intercom_m3_poc.deployment.{entry_id}")
        self.state = {"schema": 1, "revision": 0, "ha_uuid": str(uuid.uuid4()), "devices": {}}
        self.lock = asyncio.Lock()
        self.tasks = {}
        self.targets = {}
        self.reservations = set()
        self.approvals = {}
        self.is_target_busy = None
        self.quiesce_target = None
        self.on_installed = None
        self.watch_task = None
        self.blocked_devices = {}
        self.closed = False
        self.storage_failed = False

    async def load(self):
        await self.hass.async_add_executor_job(_private_directory, self.root)
        saved = await self.store.async_load()
        if saved is not None:
            if (not isinstance(saved, dict) or set(saved) != set(self.state) or saved["schema"] != 1
                    or type(saved["revision"]) is not int or saved["revision"] < 0
                    or not isinstance(saved["devices"], dict) or len(saved["devices"]) > 64):
                raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
            uuid.UUID(saved["ha_uuid"])
            for device_id, row in saved["devices"].items():
                if str(uuid.UUID(device_id)) != device_id or set(row) != {"label", "model", "firmware_state", "operation"}:
                    raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                if (not isinstance(row["label"], str) or not 1 <= len(row["label"]) <= 64
                        or not row["label"].isprintable() or row["model"] != "3RAB01090D"
                        or not isinstance(row["firmware_state"], dict)
                        or set(row["firmware_state"]) != {"current_version", "target_version", "mode"}
                        or row["firmware_state"]["mode"] not in {"unknown", "stock", "ram", "permanent"}):
                    raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                for key in ("current_version", "target_version"):
                    value = row["firmware_state"][key]
                    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value)):
                        raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                target, _ = await self.hass.async_add_executor_job(self._enrollment, device_id)
                self.targets[device_id] = target
                operation = row["operation"]
                if operation is not None:
                    required = {"operation_id", "action", "stage", "reason", "prepared_at", "target_version",
                                "app_restart_required", "reboot_required", "review_required"}
                    if (not isinstance(operation, dict) or not required <= operation.keys()
                            or set(operation) - required - {"artifact_sha256", "credential_expires", "mutation_started", "reconciles_operation_id"}
                            or not re.fullmatch(r"[0-9a-f]{32}", operation["operation_id"])
                            or operation["action"] not in ACTIONS | {"probe", "reconcile"}
                            or operation["stage"] not in {"CHECKING", "PREPARING", "READY", "REVIEW_REQUIRED", "RUNNING", "SUCCEEDED", "FAILED", "UNCERTAIN"}):
                        raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                    for key in ("reason",):
                        if operation[key] is not None and not re.fullmatch(r"[A-Z0-9_]{1,64}", operation[key]):
                            raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                    for key in ("app_restart_required", "reboot_required", "review_required"):
                        if type(operation[key]) is not bool:
                            raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                    if operation["target_version"] is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", operation["target_version"]):
                        raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                    if "artifact_sha256" in operation and not re.fullmatch(r"[0-9a-f]{64}", operation["artifact_sha256"]):
                        raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                    if operation["prepared_at"] is not None and type(operation["prepared_at"]) is not int:
                        raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                    if "credential_expires" in operation:
                        datetime.fromisoformat(operation["credential_expires"])
                    if operation.get("mutation_started", operation["stage"] == "RUNNING"):
                        self.reservations.add(target.host)
                    if operation.get("reason") in FIXED_ACCESS_ERRORS:
                        self.blocked_devices[device_id] = operation["reason"]
                    if "mutation_started" in operation and type(operation["mutation_started"]) is not bool:
                        raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                    if "reconciles_operation_id" in operation and not re.fullmatch(r"[0-9a-f]{32}", operation["reconciles_operation_id"]):
                        raise DeploymentError("INVALID_DEPLOYMENT_STORAGE")
                    operation["stage"] = "UNCERTAIN"
                    operation["reason"] = "RECONCILIATION_REQUIRED"
            self.state = saved
            self.state["revision"] += 1
        await self.store.async_save(deepcopy(self.state))

    def bind_runtime(self, is_target_busy, quiesce_target, on_installed):
        self.is_target_busy, self.quiesce_target, self.on_installed = is_target_busy, quiesce_target, on_installed
        if self.watch_task is None and not self.closed:
            self.watch_task = asyncio.create_task(self._watch())

    def has_reserved_host(self, host):
        return host in self.reservations

    def _enrollment(self, device_id):
        try:
            value = json.loads(_private_read(self.root / device_id / "enrollment.json"))
            if set(value) != {"target", "password"}:
                raise ValueError
            target, credentials = SpeakerTarget(**value["target"]), EphemeralCredentials(value["password"])
            _validate_target(target, credentials)
            return target, credentials
        except Exception:
            raise DeploymentError("INVALID_PRIVATE_ENROLLMENT") from None

    async def _save(self, state):
        await self.store.async_save(deepcopy(state))
        self.state = state

    def _admit(self, expected, device_id=None):
        if self.closed or self.storage_failed:
            raise DeploymentError("DEPLOYMENT_UNAVAILABLE")
        if type(expected) is not int or expected != self.state["revision"]:
            raise DeploymentError("DEPLOYMENT_REVISION_CONFLICT")
        if device_id is not None and device_id not in self.state["devices"]:
            raise DeploymentError("UNKNOWN_DEPLOYMENT_DEVICE")
        if device_id in self.tasks:
            raise DeploymentError("DEPLOYMENT_BUSY")

    async def configure(self, expected_deployment_revision, target, credentials, label, device_id=None):
        _validate_target(target, credentials)
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 64 or not label.isprintable():
            raise DeploymentError("INVALID_DEVICE_LABEL")
        async with self.lock:
            self._admit(expected_deployment_revision, device_id)
            if device_id is not None:
                old_operation = self.state["devices"][device_id]["operation"]
                if (old_operation and old_operation["stage"] == "UNCERTAIN" and old_operation.get("mutation_started")
                        and self.targets.get(device_id) != target):
                    raise DeploymentError("RECONCILIATION_REQUIRED")
            for stored_id in self.state["devices"]:
                stored_target, _ = await self.hass.async_add_executor_job(self._enrollment, stored_id)
                if stored_id == device_id:
                    if stored_target.mac != target.mac or stored_target.serial != target.serial:
                        raise DeploymentError("DEVICE_IDENTITY_CHANGED")
                elif stored_target.mac == target.mac or stored_target.serial == target.serial or stored_target.host == target.host:
                    raise DeploymentError("DUPLICATE_DEPLOYMENT_DEVICE")
            if device_id is None:
                if len(self.state["devices"]) >= 64:
                    raise DeploymentError("DEVICE_LIMIT")
                device_id = str(uuid.uuid4())
            private_path = self.root / device_id / "enrollment.json"
            previous = await self.hass.async_add_executor_job(_private_read, private_path) if await self.hass.async_add_executor_job(private_path.exists) else None
            await self.hass.async_add_executor_job(_json_write, private_path,
                                                  {"target": asdict(target), "password": credentials.password})
            state = deepcopy(self.state)
            if device_id in state["devices"] and self.targets.get(device_id) == target:
                state["devices"][device_id]["label"] = label.strip()
                old_operation = state["devices"][device_id]["operation"]
                if old_operation and old_operation["stage"] == "UNCERTAIN":
                    old_operation["reason"] = "CREDENTIAL_UPDATED_RECONCILIATION_REQUIRED"
            else:
                state["devices"][device_id] = {"label": label.strip(), "model": target.model,
                    "firmware_state": {"current_version": None, "target_version": None, "mode": "unknown"}, "operation": None}
            state["revision"] += 1
            try:
                await self._save(state)
            except BaseException as error:
                if previous is not None:
                    await self.hass.async_add_executor_job(_private_write, private_path, previous)
                else:
                    await self.hass.async_add_executor_job(private_path.unlink)
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise DeploymentError("STORAGE_UNAVAILABLE") from None
            self.targets[device_id] = target
            self.approvals.pop(device_id, None)
            self.blocked_devices.pop(device_id, None)
        return self.status()

    def status(self):
        rows = []
        for device_id, value in self.state["devices"].items():
            row = deepcopy(value)
            uncertain = bool(row["operation"] and row["operation"]["stage"] == "UNCERTAIN")
            busy = device_id in self.tasks
            usable = not self.closed and not self.storage_failed and not busy
            package = self.bundle_root is not None and bool(self.compatibility_pin and self.manifest_pin)
            row.update(device_id=device_id, configured=True,
                capabilities={"probe": usable and not uncertain, "prepare": usable and package and not uncertain,
                              "reconcile": usable, **{action: False for action in ACTIONS}},
                reasons={"probe": ["RECONCILIATION_REQUIRED"] if uncertain else [],
                         "prepare": (["PACKAGE_UNAVAILABLE"] if not package else []) + (["RECONCILIATION_REQUIRED"] if uncertain else []),
                         "reconcile": [], **{action: list(LIVE_BLOCKERS) for action in ACTIONS}})
            if row["firmware_state"]["mode"] == "stock":
                row["reasons"]["rollback_previous"] = ["PREVIOUS_MATERIAL_UNAVAILABLE"]
            rows.append(row)
            if device_id in self.blocked_devices:
                row["capabilities"]["probe"] = row["capabilities"]["prepare"] = False
                row["reasons"]["probe"] = row["reasons"]["prepare"] = [self.blocked_devices[device_id]]
            approval = self.approvals.get(device_id)
            if (usable and approval and approval["expires_epoch"] > time.time() and row["operation"]
                    and approval["operation_id"] == row["operation"]["operation_id"]
                    and approval["action"] == row["operation"]["action"]
                    and approval["prepared_archive_sha256"] == row["operation"].get("artifact_sha256")):
                requested = row["operation"]["action"]
                if requested in ACTIONS and row["operation"]["stage"] == "REVIEW_REQUIRED":
                    row["capabilities"][requested] = True
                    row["reasons"][requested] = []
        return {"revision": self.state["revision"], "mode": "permanent", "devices": rows,
                "limitations": list(ALPHA_LIMITATIONS) + (["STORAGE_UNAVAILABLE"] if self.storage_failed else [])}

    async def probe(self, expected_deployment_revision, device_id):
        return await self._start(expected_deployment_revision, device_id, "probe")

    async def reconcile(self, expected_deployment_revision, device_id):
        return await self._start(expected_deployment_revision, device_id, "reconcile")

    async def prepare(self, expected_deployment_revision, device_id, action):
        if action not in ACTIONS:
            raise DeploymentError("INVALID_DEPLOYMENT_ACTION")
        return await self._start(expected_deployment_revision, device_id, action, preparing=True)

    async def _start(self, expected, device_id, action, preparing=False):
        async with self.lock:
            self._admit(expected, device_id)
            if device_id in self.blocked_devices and action != "reconcile":
                raise DeploymentError("CONFIGURATION_REVIEW_REQUIRED")
            if action == "reconcile":
                self.blocked_devices.pop(device_id, None)
            previous = self.state["devices"][device_id]["operation"]
            if previous and previous["stage"] == "UNCERTAIN" and action != "reconcile":
                raise DeploymentError("RECONCILIATION_REQUIRED")
            operation = {"operation_id": uuid.uuid4().hex, "action": action,
                "stage": "PREPARING" if preparing else "CHECKING", "reason": None,
                "prepared_at": None, "target_version": "stock" if action in {"remove", "restore_stock"} else None,
                "app_restart_required": action in ACTIONS, "reboot_required": False, "review_required": preparing,
                "mutation_started": False}
            if action == "reconcile" and previous and previous["stage"] == "UNCERTAIN":
                operation["reconciles_operation_id"] = previous.get("reconciles_operation_id", previous["operation_id"])
                if previous.get("mutation_started"):
                    operation["mutation_started"] = True
                    if not previous.get("reconciles_operation_id"):
                        await self.hass.async_add_executor_job(_json_write,
                            self.root / device_id / operation["reconciles_operation_id"] / "uncertain-operation.json", previous)
            state = deepcopy(self.state)
            state["devices"][device_id]["operation"] = operation
            state["revision"] += 1
            await self._save(state)
            self.tasks[device_id] = asyncio.create_task(self._work(device_id, operation, preparing))
        return self.status()

    async def _work(self, device_id, operation, preparing):
        try:
            async with asyncio.timeout(45):
                target, credentials = await self.hass.async_add_executor_job(self._enrollment, device_id)
                approved = await self._approved_build(device_id)
                metadata = await read_only_preflight(target, credentials, approved)
                if metadata.transaction_pending:
                    raise DeploymentError("DEVICE_TRANSACTION_PENDING")
                mode = "stock" if metadata.prestate.application_sha256 == STOCK_SHA256 else (
                    "ram" if metadata.prestate.application_mode == "ram" else "permanent")
                update = {"current_version": metadata.prestate.application_sha256[:8],
                          "target_version": None, "mode": mode}
                if preparing:
                    preferences = await read_private_preferences(target, credentials)
                    material = asyncio.ensure_future(self.hass.async_add_executor_job(
                        self._prepare_files, device_id, operation, target, metadata, preferences))
                    try:
                        version, archive_sha, expiry = await asyncio.shield(material)
                    except asyncio.CancelledError:
                        await material  # Drain custody writes before another owner can prepare this device.
                        raise
                    operation.update(stage="REVIEW_REQUIRED", reason="EXACT_PHYSICAL_CHECKPOINT_REQUIRED",
                        prepared_at=int(time.time()), artifact_sha256=archive_sha, credential_expires=expiry,
                        target_version=version)
                    if mode == "ram":
                        operation["reason"] = "RAM_STOCK_RESTORATION_REQUIRED"
                    update["target_version"] = version
                else:
                    if operation["action"] == "reconcile" and operation.get("mutation_started"):
                        await self._reconcile_material(device_id, operation, target, credentials, metadata)
                    operation.update(stage="READY", reason="READ_ONLY_PREFLIGHT_COMPLETE")
                async with self.lock:
                    state = deepcopy(self.state)
                    state["devices"][device_id].update(firmware_state=update, operation=operation)
                    state["revision"] += 1
                    await self._save(state)
        except asyncio.CancelledError:
            await self._failed(device_id, operation, "RECONCILIATION_REQUIRED", "UNCERTAIN")
            raise
        except (PreflightError, DeploymentError) as error:
            await self._failed(device_id, operation, str(error),
                               "UNCERTAIN" if operation.get("mutation_started") or str(error) == "DEVICE_TRANSACTION_PENDING" else "FAILED")
        except Exception:
            await self._failed(device_id, operation, "DEPLOYMENT_PREPARATION_FAILED",
                               "UNCERTAIN" if operation.get("mutation_started") else "FAILED")
        finally:
            self.tasks.pop(device_id, None)

    async def _reconcile_material(self, device_id, operation, target, credentials, metadata):
        origin = operation.get("reconciles_operation_id")
        if not origin or metadata.prestate.application_mode != "system" or metadata.prestate.application_pid == 0:
            raise DeploymentError("RECONCILIATION_REQUIRED")
        if metadata.prestate.application_sha256 != STOCK_SHA256:
            directory = self.root / device_id / origin
            record = json.loads(await self.hass.async_add_executor_job(_private_read, directory / "operation.json"))
            pair = record["target_build"]
            requested_matches = (metadata.configuration_sha256 == record["configuration_sha256"]
                and metadata.prestate.application_sha256 == pair["application_sha256"]
                and metadata.prestate.init_sha256 == pair["init_sha256"]
                and metadata.prestate.bootstrap_sha256 == pair["bootstrap_sha256"])
            restored_active = False
            if not requested_matches:
                active = await self.hass.async_add_executor_job(self._active, device_id)
                if active is None:
                    raise DeploymentError("RECONCILIATION_REQUIRED")
                catalog = await self.hass.async_add_executor_job(self._retained_catalog, device_id, active["operation_id"])
                if (metadata.configuration_sha256 != active["configuration_sha256"]
                        or metadata.prestate.application_sha256 != catalog["application_sha256"]
                        or metadata.prestate.init_sha256 != catalog["files"]["S99ha-speaker"]
                        or metadata.prestate.bootstrap_sha256 != catalog["files"]["bootstrap.sh"]):
                    raise DeploymentError("RECONCILIATION_REQUIRED")
                directory = self.root / device_id / active["operation_id"]
                restored_active = True
            if self.on_installed is None:
                raise DeploymentError("RUNTIME_BRIDGE_UNAVAILABLE")
            endpoint = json.loads(await self.hass.async_add_executor_job(_private_read, directory / "ha-endpoint.json"))
            await self.on_installed(device_id, endpoint)
            if not restored_active:
                authority = json.loads(await self.hass.async_add_executor_job(_private_read, directory / "uncertain-operation.json"))
                await self.hass.async_add_executor_job(self._remember_active, device_id, authority, metadata.configuration_sha256)
        await cleanup_permanent_stage(target, credentials, origin)
        if metadata.prestate.application_sha256 == STOCK_SHA256:
            active_path = self.root / device_id / "active-install.json"
            if await self.hass.async_add_executor_job(active_path.exists):
                await self.hass.async_add_executor_job(active_path.unlink)
        operation["mutation_started"] = False
        self.reservations.discard(target.host)

    def _active(self, device_id):
        path = self.root / device_id / "active-install.json"
        if not path.exists():
            return None
        value = json.loads(_private_read(path))
        if (not {"operation_id", "configuration_sha256", "approval_operation"} <= value.keys()
                or set(value) - {"operation_id", "configuration_sha256", "approval_operation", "previous_operation_id"}
                or not re.fullmatch(r"[0-9a-f]{32}", value["operation_id"])
                or not re.fullmatch(r"[0-9a-f]{64}", value["configuration_sha256"])
                or (value.get("previous_operation_id") is not None and not re.fullmatch(r"[0-9a-f]{32}", value["previous_operation_id"]))):
            raise DeploymentError("INVALID_RETAINED_INSTALL")
        return value

    def _retained_catalog(self, device_id, operation_id, following=False):
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise DeploymentError("INVALID_RETAINED_INSTALL")
        directory = self.root / device_id / operation_id
        record = json.loads(_private_read(directory / "operation.json"))
        compatibility = _private_read(directory / "compatibility.json")
        manifest = _private_read(directory / "bundle.sha256")
        if (_sha(compatibility) != record["compatibility_sha256"] or _sha(manifest) != record["bundle_manifest_sha256"]):
            raise DeploymentError("RETAINED_PACKAGE_CHANGED")
        catalog = json.loads(compatibility)
        if set(catalog["files"]) != PUBLIC_FILES - {"compatibility.json", "bundle.sha256"}:
            raise DeploymentError("RETAINED_PACKAGE_CHANGED")
        for name, digest in catalog["files"].items():
            path = directory / name
            if path.is_symlink() or _sha(path.read_bytes()) != digest:
                raise DeploymentError("RETAINED_PACKAGE_CHANGED")
        material = record.get("material_source_operation_id")
        if material is not None:
            if following or material == operation_id:
                raise DeploymentError("INVALID_RETAINED_INSTALL")
            return self._retained_catalog(device_id, material, following=True)
        return catalog

    async def _approved_build(self, device_id=None):
        builds = []
        if self.bundle_root is not None:
            catalog, _ = await self.hass.async_add_executor_job(_catalog, self.bundle_root, self.compatibility_pin, self.manifest_pin)
            builds.append(ApprovedBuild(catalog["application_sha256"], catalog["files"]["S99ha-speaker"], catalog["files"]["bootstrap.sh"]))
        if device_id is not None:
            active = await self.hass.async_add_executor_job(self._active, device_id)
            if active:
                for operation_id in (active["operation_id"], active.get("previous_operation_id")):
                    if operation_id:
                        catalog = await self.hass.async_add_executor_job(self._retained_catalog, device_id, operation_id)
                        build = ApprovedBuild(catalog["application_sha256"], catalog["files"]["S99ha-speaker"], catalog["files"]["bootstrap.sh"])
                        if build not in builds:
                            builds.append(build)
        return tuple(builds) if builds else None

    def _remember_active(self, device_id, operation, configuration_sha256):
        old = self._active(device_id)
        previous = old.get("previous_operation_id") if old and old["operation_id"] == operation["operation_id"] else (
            old["operation_id"] if old else None)
        _json_write(self.root / device_id / "active-install.json", {"operation_id": operation["operation_id"],
            "configuration_sha256": configuration_sha256, "approval_operation": deepcopy(operation), "previous_operation_id": previous})

    async def _failed(self, device_id, operation, reason, stage="FAILED"):
        if reason in FIXED_ACCESS_ERRORS:
            self.blocked_devices[device_id] = reason
        operation.update(stage=stage, reason=reason)
        try:
            async with self.lock:
                state = deepcopy(self.state)
                state["devices"][device_id]["operation"] = operation
                state["revision"] += 1
                await self._save(state)
        except Exception:
            self.storage_failed = True

    def _prepare_files(self, device_id, operation, target, metadata, preferences):
        catalog, files = _catalog(self.bundle_root, self.compatibility_pin, self.manifest_pin)
        directory = self.root / device_id / operation["operation_id"]
        _private_directory(directory)
        for name, data in files.items():
            _private_write(directory / name, data)
        enrollment = directory / "enrollment"
        _private_directory(enrollment)
        pins, expiry = _identities(enrollment, device_id, self.state["ha_uuid"])
        clock_path = self.root / device_id / "clock-state.json"
        clock = json.loads(_private_read(clock_path)) if clock_path.exists() else {"generation": 0, "floor_utc_us": 0}
        if (set(clock) != {"generation", "floor_utc_us"} or any(type(value) is not int or value < 0 for value in clock.values())):
            raise DeploymentError("INVALID_CLOCK_CUSTODY")
        clock = {"generation": clock["generation"] + 1,
                 "floor_utc_us": max(clock["floor_utc_us"], time.time_ns() // 1000 - 1_000_000)}
        _json_write(clock_path, clock)
        config = {"enabled": True, "profile": 4, "local_commands_enabled": False,
            "bind_address": target.host, "port": 6054, "device_uuid": device_id,
            "ca_file": REMOTE_PRIVATE + "/ca.pem", "certificate_file": REMOTE_PRIVATE + "/endpoint.pem",
            "key_file": REMOTE_PRIVATE + "/endpoint.key", "peer_uri": "urn:intercom:ha:" + self.state["ha_uuid"],
            "peer_spki_sha256": pins["ha"], "ha_boot_clock_path": "/tmp/intercom-m56-boot.clock.json",
            "ha_clock_generation": clock["generation"], "ha_clock_floor_utc_us": clock["floor_utc_us"],
            "ha_clock_rate_ppb": 1000000, "sink": "alsa_output.hw_0_1",
            "sink_volume": 1.0, "beep_file": "/data/intercom-m56-install/attention.wav",
            "aec_tail_ms": 160, "capture_source_age_us": 20000}
        _json_write(enrollment / "intercom_m3_poc.json", config)
        _private_write(enrollment / "sound.json", preferences)
        material_source = None
        previous_endpoint = None
        target_catalog = catalog
        version = catalog["version"] if operation["action"] == "install" else (
            "stock" if operation["action"] in {"restore_stock", "remove"} else None)
        if operation["action"] == "rollback_previous":
            active = self._active(device_id)
            if active is None:
                raise DeploymentError("PREVIOUS_MATERIAL_UNAVAILABLE")
            if active is not None:
                prior = active.get("previous_operation_id")
                if prior is None:
                    version = "stock"
                else:
                    previous_record = json.loads(_private_read(self.root / device_id / prior / "operation.json"))
                    material_source = previous_record.get("material_source_operation_id", prior)
                    target_catalog = self._retained_catalog(device_id, material_source)
                    if target_catalog["application_sha256"] == RAM_SHA256:
                        raise DeploymentError("PREVIOUS_CLOCK_BUILD_UNSUPPORTED")
                    source = self.root / device_id / material_source
                    for name in ("ca.pem", "endpoint.pem", "endpoint.key", "intercom_m3_poc.json", "sound.json", "ha.pem", "ha.key"):
                        _private_write(enrollment / name, _private_read(source / "enrollment" / name))
                    previous_endpoint = json.loads(_private_read(source / "ha-endpoint.json"))
                    previous_record = json.loads(_private_read(source / "operation.json"))
                    expiry = previous_record["credential_expires"]
                    config = json.loads(_private_read(enrollment / "intercom_m3_poc.json"))
                    clock = {"generation": config["ha_clock_generation"], "floor_utc_us": config["ha_clock_floor_utc_us"]}
                    version = target_catalog["version"]
        expected = asdict(metadata.prestate)
        expected.update(serial=target.serial, mac=target.mac, model=target.model)
        record = {"schema": 1, "operation_id": operation["operation_id"], "action": operation["action"],
            "bundle_manifest_sha256": self.manifest_pin, "compatibility_sha256": self.compatibility_pin,
            "expected": expected, "checkpoint": {"persistent_write": False, "boot_clock_ready": False},
            "ha_utc_epoch": int(time.time()), "prepared_deployment_revision": self.state["revision"],
            "credential_expires": expiry, "clock": clock,
            "configuration_sha256": _sha((enrollment / "intercom_m3_poc.json").read_bytes()),
            "target_build": {"application_sha256": target_catalog["application_sha256"],
                "init_sha256": target_catalog["files"]["S99ha-speaker"], "bootstrap_sha256": target_catalog["files"]["bootstrap.sh"]}}
        if material_source is not None:
            record["material_source_operation_id"] = material_source
        _json_write(directory / "operation.json", record)
        _private_write(directory / "operation.id", (operation["operation_id"] + "\n").encode())
        _private_write(directory / "approved-manifest.sha256", (self.manifest_pin + "\n").encode())
        expected_lines = f"{expected['application_sha256']}  application\n{expected['init_sha256']}  S99ha-speaker\n"
        if expected["bootstrap_sha256"] is not None:
            expected_lines += f"{expected['bootstrap_sha256']}  bootstrap.sh\n"
        else:
            expected_lines += "ABSENT  bootstrap.sh\n"
        _private_write(directory / "expected.sha256", expected_lines.encode())
        self._runtime_expected(directory, record, metadata, approved=False)
        device_members = sorted(PUBLIC_FILES | {"operation.json", "operation.id", "approved-manifest.sha256", "expected.sha256"})
        device_members += ["runtime.expected"]
        device_members += ["enrollment/" + name for name in ("ca.pem", "endpoint.pem", "endpoint.key", "intercom_m3_poc.json", "sound.json")]
        archive = directory / "device-stage.tar"
        descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output, tarfile.open(fileobj=output, mode="w") as package:
            for name in device_members:
                package.add(directory / name, arcname=name, recursive=False)
        # HA private key stays locally outside the device archive.
        _json_write(directory / "ha-endpoint.json", previous_endpoint or {"endpoint_host": target.host, "endpoint_port": 6054,
            "expected_endpoint_uuid": device_id, "expected_endpoint_uri": "urn:intercom:endpoint:" + device_id,
            "expected_endpoint_spki_sha256": pins["endpoint"], "endpoint_profile": 4,
            "endpoint_ca_path": str(enrollment / "ca.pem"), "endpoint_client_cert_path": str(enrollment / "ha.pem"),
            "endpoint_client_key_path": str(enrollment / "ha.key")})
        return version, _sha(archive.read_bytes()), expiry

    def _runtime_expected(self, directory, record, metadata, approved):
        state = metadata.prestate
        values = {"schema": 1, "operation_id": record["operation_id"], "boot_id": state.boot_id,
            "monitor_pid": state.monitor_pid, "monitor_start_ticks": state.monitor_start_ticks,
            "monitor_cmdline_sha256": state.monitor_cmdline_sha256, "monitor_exe_sha256": state.monitor_exe_sha256,
            "application_pid": state.application_pid, "application_start_ticks": state.application_start_ticks,
            "application_sha256": state.application_sha256, "pulse_pid": state.pulse_pid,
            "pulse_start_ticks": state.pulse_start_ticks, "pulse_sha256": state.pulse_sha256,
            "persistent_write": "true" if approved else "false", "boot_clock_ready": "true" if approved else "false",
            "recovery_ready": "true" if approved else "false", "ha_issued_epoch": record["ha_utc_epoch"],
            "device_uptime": metadata.uptime_seconds}
        _private_write(directory / "runtime.expected", "".join(f"{key}\t{value}\n" for key, value in values.items()).encode())

    def _approval(self, device_id, operation):
        accepted = self.root / device_id / operation["operation_id"] / "accepted-checkpoint.json"
        path = accepted if accepted.exists() else self.root / device_id / "physical-checkpoint.json"
        if not path.exists():
            return None
        value = json.loads(_private_read(path))
        required = {"schema", "approved", "operation_id", "action", "target_binding_sha256", "compatibility_sha256",
                    "bundle_manifest_sha256", "prepared_archive_sha256", "expires_epoch", "boot_clock_ready", "recovery_ready"}
        binding = _sha(json.dumps(asdict(self.targets[device_id]), sort_keys=True, separators=(",", ":")).encode())
        if (set(value) != required or value["schema"] != 1 or value["approved"] is not True
                or value["boot_clock_ready"] is not True or value["recovery_ready"] is not True
                or value["operation_id"] != operation["operation_id"] or value["action"] != operation["action"]
                or value["target_binding_sha256"] != binding
                or value["compatibility_sha256"] != json.loads(_private_read(self.root / device_id / operation["operation_id"] / "operation.json"))["compatibility_sha256"]
                or value["bundle_manifest_sha256"] != json.loads(_private_read(self.root / device_id / operation["operation_id"] / "operation.json"))["bundle_manifest_sha256"]
                or value["prepared_archive_sha256"] != operation.get("artifact_sha256")
                or type(value["expires_epoch"]) is not int or not time.time() < value["expires_epoch"] <= time.time() + 7 * 86400):
            raise DeploymentError("PHYSICAL_CHECKPOINT_MISMATCH")
        return value

    async def refresh_approvals(self):
        if self.closed:
            return self.status()
        approved = {}
        for device_id, row in self.state["devices"].items():
            operation = row["operation"]
            if operation and operation["stage"] == "REVIEW_REQUIRED":
                try:
                    value = await self.hass.async_add_executor_job(self._approval, device_id, operation)
                    if value is not None:
                        approved[device_id] = value
                except Exception:
                    pass
        self.approvals = approved
        return self.status()

    def _execution_packet(self, device_id, operation, metadata, ha_low_us, ha_high_us, source_operation=None):
        reactivating = source_operation is not None
        source_operation = source_operation or operation
        directory = self.root / device_id / source_operation["operation_id"]
        record = json.loads(_private_read(directory / "operation.json"))
        if (record["operation_id"] != source_operation["operation_id"] or record["action"] != source_operation["action"]
                or (not reactivating and (record["bundle_manifest_sha256"] != self.manifest_pin
                    or record["compatibility_sha256"] != self.compatibility_pin))):
            raise DeploymentError("DEPLOYMENT_BINDING_MISMATCH")
        expected = dict(record["expected"])
        for key in ("serial", "mac", "model"):
            expected.pop(key)
        if not reactivating and expected != asdict(metadata.prestate):
            raise DeploymentError("DEVICE_PRESTATE_CHANGED")
        original = directory / "device-stage.tar"
        if original.is_symlink() or _sha(original.read_bytes()) != source_operation["artifact_sha256"]:
            raise DeploymentError("PRIVATE_PACKAGE_CHANGED")
        if ha_high_us < ha_low_us or ha_high_us - ha_low_us > 120_000_000:
            raise DeploymentError("HA_CLOCK_OBSERVATION_INVALID")
        catalog = self._retained_catalog(device_id, source_operation["operation_id"]) if reactivating else _catalog(
            self.bundle_root, self.compatibility_pin, self.manifest_pin)[0]
        target_build = record.get("target_build", {"application_sha256": catalog["application_sha256"],
            "init_sha256": catalog["files"]["S99ha-speaker"], "bootstrap_sha256": catalog["files"]["bootstrap.sh"]})
        if target_build["application_sha256"] == RAM_SHA256:
            raise DeploymentError("HA_CLOCK_BUILD_REQUIRED")
        config = json.loads(_private_read(directory / "enrollment" / "intercom_m3_poc.json"))
        config_sha = _sha(_private_read(directory / "enrollment" / "intercom_m3_poc.json"))
        if datetime.now(timezone.utc) + timedelta(seconds=300) >= datetime.fromisoformat(record["credential_expires"]):
            raise DeploymentError("ENROLLMENT_EXPIRING")
        if reactivating:
            if (metadata.prestate.application_sha256 != target_build["application_sha256"]
                    or metadata.prestate.init_sha256 != target_build["init_sha256"]
                    or metadata.prestate.bootstrap_sha256 != target_build["bootstrap_sha256"]
                    or metadata.prestate.application_mode != "stopped" or metadata.configuration_sha256 != config_sha):
                raise DeploymentError("BOOT_BINDING_MISMATCH")
            record["operation_id"], record["action"] = operation["operation_id"], "activate_boot"
            record["expected"] = {**asdict(metadata.prestate), **{key: record["expected"][key] for key in ("serial", "mac", "model")}}
        clock = {"schema": 1, "boot_id": metadata.prestate.boot_id, "nonce": uuid.uuid4().hex,
            "boottime_us": metadata.boottime_us, "ha_utc_low_us": ha_low_us, "ha_utc_high_us": ha_high_us,
            "generation": config["ha_clock_generation"], "floor_utc_us": config["ha_clock_floor_utc_us"],
            "max_rate_ppb": config["ha_clock_rate_ppb"], "application_sha256": target_build["application_sha256"],
            "configuration_sha256": config_sha}
        if ha_low_us - 10_000 < clock["floor_utc_us"]:
            raise DeploymentError("HA_CLOCK_BELOW_ENROLLMENT_FLOOR")
        record["checkpoint"] = {"persistent_write": True, "boot_clock_ready": True, "recovery_ready": True}
        record["ha_utc_epoch"] = ha_high_us // 1_000_000
        runtime = {"schema": 1, "operation_id": record["operation_id"], "boot_id": metadata.prestate.boot_id,
            "monitor_pid": metadata.prestate.monitor_pid, "monitor_start_ticks": metadata.prestate.monitor_start_ticks,
            "monitor_cmdline_sha256": metadata.prestate.monitor_cmdline_sha256,
            "monitor_exe_sha256": metadata.prestate.monitor_exe_sha256, "application_pid": metadata.prestate.application_pid,
            "application_start_ticks": metadata.prestate.application_start_ticks,
            "application_sha256": metadata.prestate.application_sha256, "pulse_pid": metadata.prestate.pulse_pid,
            "pulse_start_ticks": metadata.prestate.pulse_start_ticks, "pulse_sha256": metadata.prestate.pulse_sha256,
            "persistent_write": "true", "boot_clock_ready": "true", "recovery_ready": "true",
            "ha_issued_epoch": record["ha_utc_epoch"], "device_uptime": metadata.uptime_seconds}
        substitutions = {"operation.json": (json.dumps(record, sort_keys=True) + "\n").encode(),
            "runtime.expected": "".join(f"{key}\t{value}\n" for key, value in runtime.items()).encode(),
            "boot-clock.json": (json.dumps(clock, sort_keys=True) + "\n").encode()}
        if not reactivating and metadata.prestate.application_sha256 != STOCK_SHA256:
            active = self._active(device_id)
            if active is None:
                raise DeploymentError("MANAGED_BASELINE_REQUIRED")
            old_directory = self.root / device_id / active["operation_id"]
            old_config = json.loads(_private_read(old_directory / "enrollment" / "intercom_m3_poc.json"))
            if metadata.configuration_sha256 != active["configuration_sha256"]:
                raise DeploymentError("ACTIVE_CONFIGURATION_CHANGED")
            fallback = {**clock, "nonce": uuid.uuid4().hex, "application_sha256": metadata.prestate.application_sha256,
                "configuration_sha256": active["configuration_sha256"], "generation": old_config["ha_clock_generation"],
                "floor_utc_us": old_config["ha_clock_floor_utc_us"], "max_rate_ppb": old_config["ha_clock_rate_ppb"]}
            substitutions["rollback-boot-clock.json"] = (json.dumps(fallback, sort_keys=True) + "\n").encode()
        if reactivating:
            substitutions["operation.id"] = (operation["operation_id"] + "\n").encode()
            substitutions["expected.sha256"] = (f"{metadata.prestate.application_sha256}  application\n"
                f"{metadata.prestate.init_sha256}  S99ha-speaker\n{metadata.prestate.bootstrap_sha256}  bootstrap.sh\n").encode()
        result = io.BytesIO()
        with tarfile.open(original) as source, tarfile.open(fileobj=result, mode="w") as destination:
            for member in source.getmembers():
                if not member.isfile() or member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise DeploymentError("INVALID_PRIVATE_PACKAGE")
                data = substitutions.pop(member.name, None)
                if data is None:
                    data = source.extractfile(member).read()
                member.size, member.uid, member.gid, member.mode = len(data), 0, 0, 0o600
                destination.addfile(member, io.BytesIO(data))
            for name, data in substitutions.items():
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o600
                destination.addfile(member, io.BytesIO(data))
        return result.getvalue()

    async def execute(self, expected_deployment_revision, device_id, operation_id, action):
        async with self.lock:
            self._admit(expected_deployment_revision, device_id)
            operation = self.state["devices"][device_id]["operation"]
            if (action not in ACTIONS or operation is None or operation["operation_id"] != operation_id
                    or operation["action"] != action or operation["stage"] != "REVIEW_REQUIRED"):
                raise DeploymentError("DEPLOYMENT_BINDING_MISMATCH")
            host = self.targets[device_id].host
            if self.is_target_busy is None or self.quiesce_target is None or self.on_installed is None:
                raise DeploymentError("RUNTIME_BRIDGE_UNAVAILABLE")
            if host in self.reservations or self.is_target_busy(host):
                raise DeploymentError("DEVICE_CALL_BUSY")
            # Same event loop: reserve before the first await prevents new call admission.
            self.reservations.add(host)
            try:
                approval = await self.hass.async_add_executor_job(self._approval, device_id, operation)
                if approval is None:
                    raise DeploymentError("EXACT_PHYSICAL_CHECKPOINT_REQUIRED")
                self.approvals[device_id] = approval
                await self.hass.async_add_executor_job(_json_write,
                    self.root / device_id / operation["operation_id"] / "accepted-checkpoint.json", approval)
                operation = deepcopy(operation)
                operation.update(stage="RUNNING", reason=None, mutation_started=True)
                state = deepcopy(self.state)
                state["devices"][device_id]["operation"] = operation
                state["revision"] += 1
                await self._save(state)
                self.tasks[device_id] = asyncio.create_task(self._execute_work(device_id, operation))
            except BaseException:
                self.reservations.discard(host)
                raise
        return self.status()

    async def _execute_work(self, device_id, operation):
        committed = False
        mutation_started = False
        try:
            async with asyncio.timeout(180):
                target, credentials = await self.hass.async_add_executor_job(self._enrollment, device_id)
                if self.is_target_busy(target.host):
                    raise DeploymentError("DEVICE_CALL_BUSY")
                await self.quiesce_target(target.host)
                low = time.time_ns() // 1000
                builds = await self._approved_build(device_id)
                metadata = await read_only_preflight(target, credentials, builds)
                high = time.time_ns() // 1000
                if metadata.transaction_pending or metadata.established_6054:
                    raise DeploymentError("DEVICE_CONNECTION_BUSY")
                if operation["action"] == "install":
                    if metadata.prestate.application_mode != "system":
                        raise DeploymentError("RAM_STOCK_RESTORATION_REQUIRED")
                    if metadata.prestate.application_sha256 != STOCK_SHA256:
                        active = await self.hass.async_add_executor_job(self._active, device_id)
                        if active is None or active["configuration_sha256"] != metadata.configuration_sha256:
                            raise DeploymentError("MANAGED_BASELINE_REQUIRED")
                packet = await self.hass.async_add_executor_job(self._execution_packet, device_id, operation, metadata, low, high)
                mutation_started = True
                await stage_permanent_package(target, credentials, packet, _sha(packet), operation["operation_id"])
                response = await permanent_action(target, credentials, operation["operation_id"], operation["action"])
                if response["status"] != "VERIFIED":
                    raise DeploymentError("DEVICE_ACTIVATION_NOT_VERIFIED")
                committed = True
                catalog, _ = await self.hass.async_add_executor_job(_catalog, self.bundle_root, self.compatibility_pin, self.manifest_pin)
                approved = ApprovedBuild(catalog["application_sha256"], catalog["files"]["S99ha-speaker"], catalog["files"]["bootstrap.sh"])
                observed = await read_only_preflight(target, credentials, builds)
                if observed.transaction_pending:
                    raise DeploymentError("DEVICE_TRANSACTION_PENDING")
                if operation["action"] == "install":
                    if (observed.prestate.application_sha256 != approved.application_sha256
                            or observed.prestate.init_sha256 != approved.init_sha256
                            or observed.prestate.bootstrap_sha256 != approved.bootstrap_sha256
                            or observed.prestate.application_mode != "system" or observed.prestate.application_pid == 0):
                        raise DeploymentError("DEVICE_ACTIVATION_NOT_VERIFIED")
                    directory = self.root / device_id / operation["operation_id"]
                    endpoint = json.loads(await self.hass.async_add_executor_job(_private_read, directory / "ha-endpoint.json"))
                    await self.on_installed(device_id, endpoint)
                    configuration_sha = _sha(await self.hass.async_add_executor_job(_private_read, directory / "enrollment" / "intercom_m3_poc.json"))
                    await self.hass.async_add_executor_job(self._remember_active, device_id, operation, configuration_sha)
                elif operation["action"] == "rollback_previous":
                    # Restored trust must be matched to its retained HA sidecar, never freshly minted trust.
                    if observed.prestate.application_sha256 != STOCK_SHA256:
                        directory = self.root / device_id / operation["operation_id"]
                        record = json.loads(await self.hass.async_add_executor_job(_private_read, directory / "operation.json"))
                        target_pair = record["target_build"]
                        if (observed.configuration_sha256 != record["configuration_sha256"]
                                or observed.prestate.application_sha256 != target_pair["application_sha256"]
                                or observed.prestate.init_sha256 != target_pair["init_sha256"]
                                or observed.prestate.bootstrap_sha256 != target_pair["bootstrap_sha256"]
                                or observed.prestate.application_mode != "system"):
                            raise DeploymentError("RESTORED_HA_TRUST_RECONCILIATION_REQUIRED")
                        endpoint = json.loads(await self.hass.async_add_executor_job(_private_read, directory / "ha-endpoint.json"))
                        await self.on_installed(device_id, endpoint)
                        await self.hass.async_add_executor_job(self._remember_active, device_id, operation, observed.configuration_sha256)
                elif observed.prestate.application_sha256 != STOCK_SHA256:
                    raise DeploymentError("STOCK_RESTORATION_NOT_VERIFIED")
                await cleanup_permanent_stage(target, credentials, operation["operation_id"])
                if observed.prestate.application_sha256 == STOCK_SHA256:
                    active_path = self.root / device_id / "active-install.json"
                    if active_path.exists():
                        await self.hass.async_add_executor_job(active_path.unlink)
                operation.update(stage="SUCCEEDED", reason="DEVICE_ACTION_VERIFIED", mutation_started=False)
                async with self.lock:
                    state = deepcopy(self.state)
                    state["devices"][device_id]["operation"] = operation
                    state["devices"][device_id]["firmware_state"].update(current_version=observed.prestate.application_sha256[:8],
                        mode="stock" if observed.prestate.application_sha256 == STOCK_SHA256 else "permanent")
                    state["revision"] += 1
                    await self._save(state)
        except asyncio.CancelledError:
            await self._failed(device_id, operation, "RECONCILIATION_REQUIRED", "UNCERTAIN")
            raise
        except (DeploymentError, PreflightError) as error:
            operation["mutation_started"] = mutation_started
            await self._failed(device_id, operation, str(error), "UNCERTAIN" if committed or mutation_started else "FAILED")
        except Exception:
            await self._failed(device_id, operation, "RECONCILIATION_REQUIRED", "UNCERTAIN")
        finally:
            latest = self.state["devices"][device_id]["operation"]
            if not latest or latest["stage"] != "UNCERTAIN" or not latest.get("mutation_started"):
                self.reservations.discard(self.targets[device_id].host)
            self.tasks.pop(device_id, None)

    async def _watch(self):
        initial = True
        try:
            while not self.closed:
                for device_id in list(self.state["devices"]):
                    active = self.root / device_id / "active-install.json"
                    if device_id not in self.blocked_devices and (initial or await self.hass.async_add_executor_job(active.exists)):
                        try:
                            await self._boot_reconcile(device_id, initial)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # Fixed status only; no exception/credential/path is logged.
                            pass
                initial = False
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise

    async def _boot_reconcile(self, device_id, initial):
        async with self.lock:
            if self.closed or device_id in self.tasks or self.is_target_busy(self.targets[device_id].host):
                return
            self.tasks[device_id] = asyncio.current_task()
        reserved = False
        operation = deepcopy(self.state["devices"][device_id]["operation"])
        try:
            async with asyncio.timeout(180):
                target, credentials = await self.hass.async_add_executor_job(self._enrollment, device_id)
                approved = await self._approved_build(device_id)
                low = time.time_ns() // 1000
                observed = await read_only_preflight(target, credentials, approved)
                high = time.time_ns() // 1000
                if observed.transaction_pending:
                    raise DeploymentError("DEVICE_TRANSACTION_PENDING")
                active_path = self.root / device_id / "active-install.json"
                active = await self.hass.async_add_executor_job(self._active, device_id)
                if active is not None:
                    active_record = json.loads(await self.hass.async_add_executor_job(_private_read,
                        self.root / device_id / active["operation_id"] / "operation.json"))
                    if datetime.now(timezone.utc) >= datetime.fromisoformat(active_record["credential_expires"]):
                        raise DeploymentError("ENROLLMENT_EXPIRED")
                if observed.prestate.application_mode == "stopped" and active is not None:
                    authority = active["approval_operation"]
                    approval = await self.hass.async_add_executor_job(self._approval, device_id, authority)
                    if approval is None:
                        raise DeploymentError("EXACT_PHYSICAL_CHECKPOINT_REQUIRED")
                    async with self.lock:
                        if self.is_target_busy(target.host):
                            return
                        self.reservations.add(target.host)
                        reserved = True
                    await self.quiesce_target(target.host)
                    # The second observation brackets the actual start authorization.
                    low = time.time_ns() // 1000
                    observed = await read_only_preflight(target, credentials, approved)
                    high = time.time_ns() // 1000
                    if observed.established_6054 or observed.transaction_pending:
                        raise DeploymentError("DEVICE_CONNECTION_BUSY")
                    operation = {"operation_id": uuid.uuid4().hex, "action": "reconcile", "stage": "RUNNING",
                        "reason": "HA_BOOT_ACTIVATION", "prepared_at": None, "target_version": None,
                        "app_restart_required": True, "reboot_required": False, "review_required": False, "mutation_started": True}
                    async with self.lock:
                        state = deepcopy(self.state)
                        state["devices"][device_id]["operation"] = operation
                        state["revision"] += 1
                        await self._save(state)
                    packet = await self.hass.async_add_executor_job(self._execution_packet, device_id, operation,
                        observed, low, high, authority)
                    await stage_permanent_package(target, credentials, packet, _sha(packet), operation["operation_id"])
                    result = await permanent_action(target, credentials, operation["operation_id"], "activate_boot")
                    if result["status"] != "VERIFIED":
                        raise DeploymentError("BOOT_ACTIVATION_NOT_VERIFIED")
                    observed = await read_only_preflight(target, credentials, approved)
                    active_catalog = await self.hass.async_add_executor_job(self._retained_catalog, device_id, active["operation_id"])
                    if (observed.prestate.application_sha256 != active_catalog["application_sha256"]
                            or observed.prestate.application_mode != "system" or observed.prestate.application_pid == 0
                            or observed.configuration_sha256 != active["configuration_sha256"]):
                        raise DeploymentError("BOOT_ACTIVATION_NOT_VERIFIED")
                    await cleanup_permanent_stage(target, credentials, operation["operation_id"])
                if initial and operation is not None and operation["stage"] == "UNCERTAIN":
                    # Reconcile an already committed candidate with its retained HA identity.
                    directory = self.root / device_id / operation["operation_id"]
                    record_path = directory / "operation.json"
                    if observed.prestate.application_mode == "system" and await self.hass.async_add_executor_job(record_path.exists):
                        record = json.loads(await self.hass.async_add_executor_job(_private_read, record_path))
                        if record["action"] == "install" and observed.configuration_sha256 == record["configuration_sha256"]:
                            endpoint = json.loads(await self.hass.async_add_executor_job(_private_read, directory / "ha-endpoint.json"))
                            await self.on_installed(device_id, endpoint)
                            await self.hass.async_add_executor_job(self._remember_active, device_id, operation, observed.configuration_sha256)
                            active = await self.hass.async_add_executor_job(self._active, device_id)
                if operation is not None and (initial or reserved):
                    if operation.get("mutation_started") and not reserved:
                        safe_stock = observed.prestate.application_sha256 == STOCK_SHA256 and observed.prestate.application_mode == "system"
                        safe_custom = (active is not None and observed.configuration_sha256 == active["configuration_sha256"]
                                       and observed.prestate.application_mode == "system" and observed.prestate.application_pid > 0)
                        if not safe_stock and not safe_custom:
                            raise DeploymentError("RECONCILIATION_REQUIRED")
                        if safe_custom:
                            endpoint = json.loads(await self.hass.async_add_executor_job(_private_read,
                                self.root / device_id / active["operation_id"] / "ha-endpoint.json"))
                            await self.on_installed(device_id, endpoint)
                        await cleanup_permanent_stage(target, credentials, operation["operation_id"])
                        if safe_stock and await self.hass.async_add_executor_job(active_path.exists):
                            await self.hass.async_add_executor_job(active_path.unlink)
                        self.reservations.discard(target.host)
                    operation.update(stage="READY", reason="HA_BOOT_IDLE_VERIFIED" if reserved else "READ_ONLY_PREFLIGHT_COMPLETE", mutation_started=False)
                    async with self.lock:
                        state = deepcopy(self.state)
                        state["devices"][device_id]["operation"] = operation
                        state["devices"][device_id]["firmware_state"].update(
                            current_version=observed.prestate.application_sha256[:8],
                            mode="stock" if observed.prestate.application_sha256 == STOCK_SHA256 else (
                                "ram" if observed.prestate.application_mode == "ram" else "permanent"))
                        state["revision"] += 1
                        await self._save(state)
        except asyncio.CancelledError:
            if operation is not None:
                await self._failed(device_id, operation, "RECONCILIATION_REQUIRED", "UNCERTAIN")
            raise
        except (PreflightError, DeploymentError) as error:
            if operation is None:
                operation = {"operation_id": uuid.uuid4().hex, "action": "reconcile", "stage": "UNCERTAIN", "reason": None,
                    "prepared_at": None, "target_version": None, "app_restart_required": False, "reboot_required": False,
                    "review_required": False, "mutation_started": False}
            await self._failed(device_id, operation, str(error), "UNCERTAIN")
        finally:
            if reserved and operation is not None and operation["stage"] != "UNCERTAIN":
                self.reservations.discard(self.targets[device_id].host)
            self.tasks.pop(device_id, None)

    async def close(self):
        self.closed = True
        if self.watch_task is not None:
            self.watch_task.cancel()
        tasks = list(self.tasks.values())
        if self.watch_task is not None and self.watch_task not in tasks:
            tasks.append(self.watch_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
