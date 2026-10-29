"""Pinned bounded speaker transport with a closed deployment command set.

Enrollment supplies the already accepted management fingerprint and identity.
Observed process metadata is a snapshot, never permission for a later write.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import ipaddress
import io
import json
import re
import tarfile
import uuid

SUPPORTED_MODEL = "3RAB01090D"
SUPPORTED_ABI = "aarch64"
STOCK_SHA256 = "2117c45183e6b21da92bc4aa8e46a9c58a0fd6d5b8176661dd90042e6e645a88"
INIT_SHA256 = "4d010c5749a457d45d59bd20524963e360f7499496303b2e102d0a96097091c1"
RAM_SHA256 = "9919a95b2106a589039a2fca8434b9342075c0a37ee27389f1d04c36eb4363c5"
CONNECT_SECONDS = 8
OPERATION_SECONDS = 15
CLOSE_SECONDS = 1
OUTPUT_LIMIT = 4096

# Fixed public metadata only. No configuration/key/audio contents or writes.
READ_ONLY_COMMAND = r"""set -eu
printf 'uid='; id -u
printf 'serial='; cat /proc/sys/kernel/hostname
printf 'mac='; cat /sys/class/net/wlan0/address
printf 'model='; jq -er '.device.modelID' /data/conf/device.json
printf 'abi='; uname -m
printf 'boot_id='; cat /proc/sys/kernel/random/boot_id
printf 'stock_sha256='; sha256sum /usr/bin/linux-voice-assistant-cpp | cut -c1-64
printf 'init_sha256='; sha256sum /etc/init.d/S99ha-speaker | cut -c1-64
pids="$(pidof linux-voice-assistant-cpp-m56-poc 2>/dev/null || true) $(pidof linux-voice-assistant-cpp 2>/dev/null || true)"
set -- $pids
case "$#" in
 0) p=0; start=0;;
 1) p=$1; case "$p" in ''|*[!0-9]*) exit 21;; esac
    start=$(awk '{print $22}' "/proc/$p/stat");;
 *) exit 21;;
esac
printf 'application_pid=%s\napplication_start_ticks=%s\n' "$p" "$start"
if [ "$p" = 0 ]; then
 printf 'application_name=none\napplication_mode=stopped\n'
 printf 'application_sha256='; sha256sum /usr/bin/linux-voice-assistant-cpp | cut -c1-64
else
 printf 'application_name='; cat "/proc/$p/comm"
 printf 'application_sha256='; sha256sum "/proc/$p/exe" | cut -c1-64
 executable=$(readlink "/proc/$p/exe")
 case "$executable" in
  /usr/bin/linux-voice-assistant-cpp) printf 'application_mode=system\n';;
  /tmp/*/linux-voice-assistant-cpp-m56-poc) printf 'application_mode=ram\n';;
  *) exit 23;;
 esac
fi
configuration=/data/intercom-m56-install/private/intercom_m3_poc.json
if [ -f "$configuration" ] && [ ! -L "$configuration" ]; then
 ls -ln "$configuration" | awk '$2==1 && $3==0 && $4==0 {ok=1} END {exit ok?0:1}'
 printf 'configuration_sha256='; sha256sum "$configuration" | cut -c1-64
else
 [ ! -e "$configuration" ] && [ ! -L "$configuration" ]
 printf 'configuration_sha256=none\n'
fi
monitor=$(cat /var/run/thirdreality/ha-speaker.pid)
case "$monitor" in ''|*[!0-9]*) exit 24;; esac
monitor_start=$(awk '{print $22}' "/proc/$monitor/stat")
printf 'monitor_pid=%s\nmonitor_start_ticks=%s\n' "$monitor" "$monitor_start"
printf 'monitor_cmdline_sha256='; sha256sum "/proc/$monitor/cmdline" | cut -c1-64
printf 'monitor_exe_sha256='; sha256sum "/proc/$monitor/exe" | cut -c1-64
bootstrap=/data/intercom-m56-install/bootstrap.sh
if [ -f "$bootstrap" ] && [ ! -L "$bootstrap" ]; then
 printf 'bootstrap_sha256='; sha256sum "$bootstrap" | cut -c1-64
else
 [ ! -e "$bootstrap" ] && [ ! -L "$bootstrap" ]
 printf 'bootstrap_sha256=none\n'
fi
set -- $(pidof pulseaudio 2>/dev/null || true)
[ "$#" = 1 ]
pulse=$1
case "$pulse" in ''|*[!0-9]*) exit 22;; esac
pulse_start=$(awk '{print $22}' "/proc/$pulse/stat")
printf 'pulse_pid=%s\npulse_start_ticks=%s\n' "$pulse" "$pulse_start"
printf 'pulse_sha256='; sha256sum "/proc/$pulse/exe" | cut -c1-64
printf 'established_6054='; awk '$2 ~ /:17A6$/ && $4=="01" {n++} END {print n+0}' /proc/net/tcp /proc/net/tcp6
printf 'utc_epoch='; date -u +%s
printf 'uptime_seconds='; cut -d. -f1 /proc/uptime
printf 'boottime_us='; awk '{printf "%.0f\n", $1 * 1000000}' /proc/uptime
if [ -e /data/intercom-m56-install/pending ] || [ -L /data/intercom-m56-install/pending ]; then
 printf 'transaction_pending=1\n'
else
 printf 'transaction_pending=0\n'
fi
[ "$p" = 0 ] || [ "$(awk '{print $22}' "/proc/$p/stat")" = "$start" ]
[ "$(awk '{print $22}' "/proc/$pulse/stat")" = "$pulse_start" ]
[ "$(awk '{print $22}' "/proc/$monitor/stat")" = "$monitor_start" ]
printf 'complete=1\n'
"""


class PreflightError(Exception):
    """A fixed safe code; never contains remote output or exception text."""


@dataclass(frozen=True, repr=False)
class SpeakerTarget:
    host: str
    port: int
    serial: str
    mac: str
    model: str
    architecture: str
    host_key_sha256: str


@dataclass(frozen=True)
class EphemeralCredentials:
    password: str = field(repr=False)


@dataclass(frozen=True)
class DevicePrestate:
    boot_id: str
    application_sha256: str
    application_pid: int
    application_start_ticks: int
    pulse_pid: int
    pulse_start_ticks: int
    monitor_pid: int
    monitor_start_ticks: int
    monitor_cmdline_sha256: str
    bootstrap_sha256: str | None
    init_sha256: str
    application_mode: str
    monitor_exe_sha256: str
    pulse_sha256: str


@dataclass(frozen=True)
class PreflightMetadata:
    prestate: DevicePrestate
    established_6054: int
    utc_epoch: int
    transaction_pending: bool
    uptime_seconds: int
    boottime_us: int
    configuration_sha256: str | None


@dataclass(frozen=True)
class ApprovedBuild:
    """Only constructed from a server-side hash-pinned public package."""
    application_sha256: str
    init_sha256: str
    bootstrap_sha256: str


def _validate_target(target: SpeakerTarget, credentials: EphemeralCredentials) -> None:
    try:
        address = ipaddress.IPv4Address(target.host)
        if (str(address) != target.host or not address.is_private or address.is_loopback
                or address.is_link_local or address.is_unspecified or address.is_multicast
                or address.is_reserved or type(target.port) is not int or target.port != 22):
            raise ValueError
        if target.model != SUPPORTED_MODEL or target.architecture != SUPPORTED_ABI:
            raise PreflightError("UNSUPPORTED_PLATFORM")
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", target.mac):
            raise ValueError
        mac = bytes.fromhex(target.mac.replace(":", ""))
        if mac[0] & 1 or mac == bytes(6):
            raise ValueError
        if target.serial != "3RSPK-" + target.mac.replace(":", "").upper():
            raise ValueError
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", target.host_key_sha256):
            raise ValueError
        if len(base64.b64decode(target.host_key_sha256[7:] + "=", validate=True)) != 32:
            raise ValueError
        if (not isinstance(credentials.password, str) or not 1 <= len(credentials.password) <= 1024
                or any(c in credentials.password for c in "\x00\r\n")):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PreflightError("INVALID_ENROLLMENT") from None


def _parse_metadata(data: bytes, target: SpeakerTarget, approved_build=None) -> PreflightMetadata:
    try:
        lines = data.decode("ascii").splitlines()
        values = dict(line.split("=", 1) for line in lines)
        fields = {"uid", "serial", "mac", "model", "abi", "boot_id", "stock_sha256",
                  "init_sha256", "application_pid", "application_start_ticks", "application_name",
                  "application_sha256", "pulse_pid", "pulse_start_ticks", "established_6054",
                  "utc_epoch", "complete", "application_mode", "monitor_pid",
                  "monitor_start_ticks", "monitor_cmdline_sha256", "bootstrap_sha256", "transaction_pending",
                  "monitor_exe_sha256", "pulse_sha256", "uptime_seconds", "boottime_us", "configuration_sha256"}
        if len(values) != len(lines) or values.keys() != fields or values["complete"] != "1":
            raise ValueError
        if (values["uid"] != "0" or values["serial"] != target.serial
                or values["mac"] != target.mac or values["model"] != target.model
                or values["abi"] != target.architecture):
            raise PreflightError("IDENTITY_MISMATCH")
        builds = () if approved_build is None else (approved_build if isinstance(approved_build, tuple) else (approved_build,))
        if len(builds) > 3:
            raise ValueError
        for build in builds:
            if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in (
                    build.application_sha256, build.init_sha256, build.bootstrap_sha256)):
                raise ValueError
        extra = tuple(build.application_sha256 for build in builds)
        baseline = values["stock_sha256"] == STOCK_SHA256 and values["init_sha256"] == INIT_SHA256
        candidate = any(values["stock_sha256"] == build.application_sha256
                        and values["init_sha256"] == build.init_sha256
                        and values["bootstrap_sha256"] == build.bootstrap_sha256 for build in builds)
        if not baseline and not candidate:
            raise PreflightError("UNSUPPORTED_BUILD")
        app_sha = values["application_sha256"]
        if app_sha not in (STOCK_SHA256, RAM_SHA256) + extra:
            raise PreflightError("UNSUPPORTED_BUILD")
        if candidate and values["application_mode"] != "ram" and app_sha != values["stock_sha256"]:
            raise PreflightError("UNSUPPORTED_BUILD")
        # Linux comm is truncated to 15 bytes for both application variants.
        if values["application_name"] != ("none" if values["application_mode"] == "stopped" else "linux-voice-ass"):
            raise PreflightError("IDENTITY_MISMATCH")
        if values["application_mode"] not in ("system", "ram", "stopped"):
            raise ValueError
        if values["transaction_pending"] not in ("0", "1"):
            raise ValueError
        for key in ("monitor_cmdline_sha256", "bootstrap_sha256", "monitor_exe_sha256", "pulse_sha256", "configuration_sha256"):
            if key in {"bootstrap_sha256", "configuration_sha256"} and values[key] == "none":
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", values[key]):
                raise ValueError
        if str(uuid.UUID(values["boot_id"])) != values["boot_id"]:
            raise ValueError
        numbers = {}
        for key in ("application_pid", "application_start_ticks", "pulse_pid", "pulse_start_ticks",
                    "established_6054", "utc_epoch", "monitor_pid", "monitor_start_ticks", "uptime_seconds", "boottime_us"):
            value = values[key]
            if not re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
                raise ValueError
            numbers[key] = int(value)
            zeros = {"established_6054", "uptime_seconds"}
            if values["application_mode"] == "stopped":
                zeros |= {"application_pid", "application_start_ticks"}
            if (key not in zeros and numbers[key] == 0) or numbers[key] > 2**63 - 1:
                raise ValueError
        if values["application_mode"] == "stopped" and (numbers["application_pid"] != 0 or numbers["application_start_ticks"] != 0):
            raise ValueError
        if numbers["application_pid"] == numbers["pulse_pid"]:
            raise ValueError
        return PreflightMetadata(DevicePrestate(values["boot_id"], app_sha,
            numbers["application_pid"], numbers["application_start_ticks"], numbers["pulse_pid"],
            numbers["pulse_start_ticks"], numbers["monitor_pid"], numbers["monitor_start_ticks"],
            values["monitor_cmdline_sha256"], None if values["bootstrap_sha256"] == "none" else values["bootstrap_sha256"],
            values["init_sha256"], values["application_mode"], values["monitor_exe_sha256"], values["pulse_sha256"]),
            numbers["established_6054"], numbers["utc_epoch"], values["transaction_pending"] == "1", numbers["uptime_seconds"],
            numbers["boottime_us"], None if values["configuration_sha256"] == "none" else values["configuration_sha256"])
    except (ValueError, UnicodeError, KeyError):
        raise PreflightError("INVALID_METADATA") from None


async def _run_fixed(target, credentials, command, input_data=None, duration=None, allow_gate=False):
    """Read fixed metadata once from an explicitly enrolled target; no retries.

    Caller owns HA admin authorization and secret custody. This function neither
    stores credentials nor grants installation permission. No live registration
    exists yet. Import is lazy until the integration requirement is coordinated.
    """
    _validate_target(target, credentials)
    try:
        import asyncssh
    except ImportError:
        raise PreflightError("SSH_DEPENDENCY_UNAVAILABLE") from None

    class PinnedClient(asyncssh.SSHClient):
        verified = False
        rejected = False

        def validate_host_public_key(self, host, addr, port, key):
            self.verified = (host == target.host and addr == target.host and port == target.port
                             and key.get_fingerprint("sha256") == target.host_key_sha256)
            self.rejected = not self.verified
            return self.verified

    client = PinnedClient()

    def password():
        if not client.verified:
            raise PreflightError("HOST_KEY_REJECTED")
        return credentials.password

    connection = None
    try:
        async with asyncio.timeout(OPERATION_SECONDS if duration is None else duration):
            connection = await asyncssh.connect(target.host, target.port, config=None,
                client_factory=lambda: client, known_hosts=([], [], []),
                server_host_key_algs=["ssh-ed25519"], username="root", password=password,
                preferred_auth=["password"], client_keys=[], agent_path=None,
                password_auth=True, disable_trivial_auth=True,
                public_key_auth=False, kbdint_auth=False, host_based_auth=False,
                gss_auth=False, gss_kex=False, agent_forwarding=False,
                connect_timeout=CONNECT_SECONDS, login_timeout=CONNECT_SECONDS)
            if not client.verified or connection.get_extra_info("peername")[:2] != (target.host, target.port):
                raise PreflightError("HOST_KEY_REJECTED")
            process = await connection.create_process(command, encoding=None,
                request_pty=False, env={}, window=OUTPUT_LIMIT, max_pktsize=OUTPUT_LIMIT)
            if input_data is not None:
                process.stdin.write(input_data)
                await process.stdin.drain()
            process.stdin.write_eof()
            size = 0

            async def bounded_read(stream):
                nonlocal size
                parts = []
                while chunk := await stream.read(1024):
                    size += len(chunk)
                    if size > OUTPUT_LIMIT:
                        raise PreflightError("OUTPUT_LIMIT")
                    parts.append(chunk)
                return b"".join(parts)

            async with asyncio.TaskGroup() as group:
                stdout = group.create_task(bounded_read(process.stdout))
                stderr = group.create_task(bounded_read(process.stderr))
            await process.wait_closed()
            if (process.exit_status != 0 and not (allow_gate and process.exit_status == 78)) or stderr.result():
                raise PreflightError("REMOTE_PREFLIGHT_FAILED")
            return process.exit_status, stdout.result()
    except TimeoutError:
        raise PreflightError("TIMEOUT") from None
    except PreflightError:
        raise
    except ExceptionGroup as errors:
        if any(isinstance(error, PreflightError) and str(error) == "OUTPUT_LIMIT"
               for error in errors.exceptions):
            raise PreflightError("OUTPUT_LIMIT") from None
        raise PreflightError("TRANSPORT_FAILED") from None
    except Exception as error:
        code = "HOST_KEY_REJECTED" if client.rejected else "TRANSPORT_FAILED"
        if isinstance(error, asyncssh.PermissionDenied):
            code = "AUTHENTICATION_FAILED"
        raise PreflightError(code) from None
    finally:
        if connection is not None:
            try:
                connection.close()
                async with asyncio.timeout(CLOSE_SECONDS):
                    await connection.wait_closed()
            except Exception:
                try:
                    connection.abort()
                except Exception:
                    raise PreflightError("CLEANUP_FAILED") from None


async def read_only_preflight(target: SpeakerTarget, credentials: EphemeralCredentials,
                              approved_build: ApprovedBuild | None = None) -> PreflightMetadata:
    _, data = await _run_fixed(target, credentials, READ_ONLY_COMMAND)
    return _parse_metadata(data, target, approved_build)


STAGE_COMMAND = r"""set -eu
[ "$(id -u)" = 0 ]
umask 077
stage=/tmp/intercom-permanent-alpha
[ ! -e "$stage" ] && [ ! -L "$stage" ]
mkdir "$stage"
chmod 700 "$stage"
tar -xf - -C "$stage"
cd "$stage"
op=$(cat operation.id)
case "$op" in ''|*[!0-9a-f]*) exit 31;; esac
[ "${#op}" = 32 ]
[ "$(sha256sum bundle.sha256 | cut -c1-64)" = "$(cat approved-manifest.sha256)" ]
sha256sum -c bundle.sha256 >/dev/null
for name in application S99ha-speaker bootstrap.sh transaction.sh S98intercom-recover attention.wav operation.json operation.id expected.sha256 runtime.expected approved-manifest.sha256 compatibility.json; do
 [ -f "$name" ] && [ ! -L "$name" ]
 chmod 600 "$name"
done
for name in application S99ha-speaker bootstrap.sh transaction.sh S98intercom-recover; do chmod 700 "$name"; done
for name in ca.pem endpoint.pem endpoint.key intercom_m3_poc.json sound.json; do
 [ -f "enrollment/$name" ] && [ ! -L "enrollment/$name" ]
 chmod 600 "enrollment/$name"
done
chmod 700 enrollment
clock=/tmp/intercom-m56-boot.clock.json
if [ -e "$clock" ] || [ -L "$clock" ]; then
 [ -f "$clock" ] && [ ! -L "$clock" ]
 ls -ln "$clock" | awk '$2==1 && $3==0 && $4==0 {ok=1} END {exit ok?0:1}'
fi
[ -f boot-clock.json ] && [ ! -L boot-clock.json ]
chmod 600 boot-clock.json
[ ! -e "$clock.new" ] && [ ! -L "$clock.new" ]
cp boot-clock.json "$clock.new"
chmod 600 "$clock.new"
mv -f "$clock.new" "$clock"
printf '{"schema":1,"operation_id":"%s","action":"stage","stage":"staged","status":"VERIFIED"}\n' "$op"
"""


def _action_result(code, data, operation_id, action):
    try:
        value = json.loads(data.decode("ascii"))
        if (not isinstance(value, dict) or set(value) != {"schema", "operation_id", "action", "stage", "status"}
                or value["schema"] != 1 or value["operation_id"] != operation_id or value["action"] != action
                or not re.fullmatch(r"[a-z_]{1,48}", value["stage"])
                or not re.fullmatch(r"[A-Z_]{1,64}", value["status"])):
            raise ValueError
        if code == 0 and value["status"] != "VERIFIED":
            raise ValueError
        return value
    except (ValueError, UnicodeError, TypeError):
        raise PreflightError("INVALID_ACTION_RESULT") from None


async def stage_permanent_package(target, credentials, package, package_sha256, operation_id):
    """Internal guarded caller only; GUI accepts neither archive bytes nor paths."""
    if (not isinstance(package, bytes) or len(package) > 4 * 1024 * 1024
            or hashlib.sha256(package).hexdigest() != package_sha256
            or not re.fullmatch(r"[0-9a-f]{32}", operation_id)):
        raise PreflightError("INVALID_PACKAGE")
    allowed = {"application", "S99ha-speaker", "bootstrap.sh", "transaction.sh", "S98intercom-recover",
        "attention.wav", "operation.json", "operation.id", "expected.sha256", "runtime.expected",
        "approved-manifest.sha256", "compatibility.json", "bundle.sha256", "boot-clock.json"}
    allowed |= {"enrollment/" + name for name in ("ca.pem", "endpoint.pem", "endpoint.key", "intercom_m3_poc.json", "sound.json")}
    try:
        with tarfile.open(fileobj=io.BytesIO(package)) as archive:
            members = archive.getmembers()
            operation_file = archive.extractfile("operation.id")
            if operation_file is None or operation_file.read(34) != (operation_id + "\n").encode():
                raise ValueError
        names = {member.name for member in members}
        if "rollback-boot-clock.json" in names:
            allowed.add("rollback-boot-clock.json")
        if (len(members) != len(allowed) or names != allowed
                or any(not member.isfile() for member in members)):
            raise ValueError
    except (ValueError, KeyError, tarfile.TarError):
        raise PreflightError("INVALID_PACKAGE") from None
    code, data = await _run_fixed(target, credentials, STAGE_COMMAND, package, duration=45)
    return _action_result(code, data, operation_id, "stage")


async def permanent_action(target, credentials, operation_id, action):
    if (action not in {"install", "verify", "activate_boot", "rollback_previous", "restore_stock", "remove", "reconcile"}
            or not re.fullmatch(r"[0-9a-f]{32}", operation_id)):
        raise PreflightError("INVALID_DEPLOYMENT_ACTION")
    # Only the closed enum is appended; no user command/path/URL can enter here.
    command = "/bin/sh /tmp/intercom-permanent-alpha/transaction.sh " + action
    code, data = await _run_fixed(target, credentials, command, duration=90, allow_gate=True)
    return _action_result(code, data, operation_id, action)


async def read_private_preferences(target, credentials):
    """Internal exact-file copy; never return these bytes through HA APIs."""
    _, data = await _run_fixed(target, credentials,
        "set -eu; [ -f /data/conf/sound.json ] && [ ! -L /data/conf/sound.json ]; cat /data/conf/sound.json")
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError
        value["active_wake_words"] = []
        return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    except (ValueError, UnicodeError):
        raise PreflightError("INVALID_PRIVATE_PREFERENCES") from None


async def cleanup_permanent_stage(target, credentials, operation_id):
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise PreflightError("INVALID_DEPLOYMENT_ACTION")
    command = "expected=" + operation_id + "\n" + r"""set -eu
stage=/tmp/intercom-permanent-alpha
if [ ! -e "$stage" ] && [ ! -L "$stage" ]; then
 printf '{"schema":1,"operation_id":"%s","action":"cleanup","stage":"absent","status":"VERIFIED"}\n' "$expected"
 exit 0
fi
[ -d "$stage" ] && [ ! -L "$stage" ]
[ "$(cat "$stage/operation.id")" = "$expected" ]
[ ! -e /data/intercom-m56-install/pending ] && [ ! -L /data/intercom-m56-install/pending ]
clock=/tmp/intercom-m56-boot.clock.json
if [ -e "$clock" ] || [ -L "$clock" ]; then
 [ -f "$clock" ] && [ ! -L "$clock" ]
 [ "$(sha256sum "$clock" | cut -c1-64)" = "$(sha256sum "$stage/boot-clock.json" | cut -c1-64)" ]
 rm "$clock"
fi
for name in ca.pem endpoint.pem endpoint.key intercom_m3_poc.json sound.json; do
 [ -f "$stage/enrollment/$name" ] && [ ! -L "$stage/enrollment/$name" ]
 rm "$stage/enrollment/$name"
done
rmdir "$stage/enrollment"
if [ -e "$stage/rollback-boot-clock.json" ] || [ -L "$stage/rollback-boot-clock.json" ]; then
 [ -f "$stage/rollback-boot-clock.json" ] && [ ! -L "$stage/rollback-boot-clock.json" ]
 rm "$stage/rollback-boot-clock.json"
fi
for name in application S99ha-speaker bootstrap.sh transaction.sh S98intercom-recover attention.wav operation.json operation.id expected.sha256 runtime.expected approved-manifest.sha256 compatibility.json bundle.sha256 boot-clock.json; do
 [ -f "$stage/$name" ] && [ ! -L "$stage/$name" ]
 rm "$stage/$name"
done
rmdir "$stage"
printf '{"schema":1,"operation_id":"%s","action":"cleanup","stage":"cleaned","status":"VERIFIED"}\n' "$expected"
"""
    code, data = await _run_fixed(target, credentials, command)
    return _action_result(code, data, operation_id, "cleanup")
