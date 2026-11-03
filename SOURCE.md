# Source and development guide

The repository root is the 0.4.10 Home Assistant integration and the paired eight-file firmware kit. The 37 files under `custom_components/intercom_m3_poc` are unchanged from the reviewed release. This is a source-support addition, not a new integration release.

## Firmware provenance and build

`firmware-source/` is the previously published source for application SHA-256 `46009f0cb4f697087d4bab66e0c7039e68f69a784c5717a105c21e92a77f35cd`; the [original source ZIP](https://github.com/chstech1/ha-intercom-manager/releases/download/v0.4.4/application-46009f0c-source.zip) has SHA-256 `37836c4bef997cceed4355e3af36bd212001c6e1b086e79521ad824b97c21c56`. Read `firmware-source/README-SOURCE.txt` for the pinned upstream revision, matching input inventory, overlay order, build and dependency constraints. `reviewed-vendor-modifications.patch` is a review aid; apply `apply_overlay.py` as instructed, not both patch and transform. Raw M4 files are inputs, not final firmware behavior. `vendor-transform-inventory.json` records before/after hashes. The released binary is not claimed reproducible from these sources alone.

The vendor SDK, models, shared libraries, compiler, stock firmware and backups are not included. Follow the pinned upstream's licenses and notices; Apache-2.0 on project work does not relicense third-party dependencies. The root `THIRD_PARTY.json`, `NOTICE.md`, and `LICENSES/` identify known notices and limitations.

## Offline tests

Use Python 3.12 and Node.js. The offline run used Python 3.12.14, Node 24.19.0, `aiohttp==3.14.5`, `probatio==0.13.0`, and `cryptography==50.0.2`. Create an isolated environment outside the checkout and install those direct dependencies (pip resolves their transitive dependencies):

```sh
python3.12 -m venv /tmp/intercom-test-venv
. /tmp/intercom-test-venv/bin/activate
python -m pip install aiohttp==3.14.5 probatio==0.13.0 cryptography==50.0.2
```

On Windows, use an environment path of your choice and activate its `Scripts/activate` instead. Run from the repository root. `PYTHONDONTWRITEBYTECODE=1` avoids creating Python bytecode in the checkout; the diagnostic round-trip uses a temporary directory:

```sh
export PYTHONDONTWRITEBYTECODE=1
set -e
for f in tests/*.py; do case "$f" in *prepare_probe.py) continue;; esac; python "$f"; done
for f in tests/*.mjs; do node "$f"; done
```

The scripts use simulated endpoints and browser/worklet objects; they do not contact a speaker. For the source-derived session probe, prepare an empty temporary directory and compile with a C++17 compiler:

```sh
python tests/prepare_probe.py /tmp/intercom-probe
c++ -std=c++17 -O0 -Wall -Wextra -I /tmp/intercom-probe -I firmware-source/project-overlay/vendor tests/probe_session.cpp -o /tmp/intercom-probe/session-probe
/tmp/intercom-probe/session-probe
```

The probe tests invalidation around mute, modeled TLS retry and partial writes; it is not actual TLS, a physical mute, acoustic verification or a diagnosis of any field event. Other scripts likewise do not prove real speaker, HA, browser, recovery, or remote acceptance.

## Deployment and recovery limits

The source for the deployment state machine and paired recovery scripts is in the component. Carefully review the exact target, backup, recovery path and current operation before any physical action. A checkpoint's `approved`, `boot_clock_ready`, and `recovery_ready` values are operator attestations, not proof of tests. Operator checkpoints are bound to their exact action and installation; never reuse an old checkpoint for a new operation or a new Home Assistant installation. The 0.4.10 interface cannot itself create a physical checkpoint; a new HA server needs a matching managed baseline or it reports `MANAGED_BASELINE_REQUIRED`. Manual TLS enrollment alone does not complete custody migration. Redacted, disabled historical checkpoint and build source is provided under `tools/reference/` for review only; it is not a portable checkpoint creator or an installation procedure. The project remains an alpha and recovery and physical installation need separate review and evidence.
