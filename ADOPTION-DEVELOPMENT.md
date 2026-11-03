# Existing speaker adoption candidate — 0.5.0b4

This development build adds **Speakers → Pair existing speaker with this Home Assistant → Prepare pairing**. It uses the existing protected SSH management connection. The owner does not supply an old backup, old Home Assistant identity, or old intercom credentials. New initial SSH connection setup still requires the device address and verified SSH identity; this build does not implement address or SSH-key discovery.

The candidate targets the exact 46009f0c application with the released paired startup/helper files, stopped application, no active native connection, intact device journal and original 0.4.10 transaction engine. It refuses unknown or running builds. The journal and engine eligibility must be confirmed on each device. Do not claim live acceptance from offline tests.

After preparation, review the selected speaker and pairing replacement. The operation must execute within 15 minutes. It replaces private pairing files, starts the existing application with a fresh HA clock authorization, verifies its process and listeners, and checks its installed configuration digest before enrolling the room and remembering authority for later HA-coordinated startup. The application, init, helper, stock reference and existing recovery engine are retained.

Pairing changes use the device's existing journal format and an automatic temporary snapshot of its current files. That snapshot is made by the transaction, not supplied by the owner. A failed start restores the original stopped pairing. A pending interrupted transaction remains gated until reconciliation; the original recovery engine can restore the snapshot at boot. An old HA paired identity is not imported into the new server.

Adoption authorization has a separate schema and purpose. It binds the prepared package, target and operation and records no physical-test attestations. The original install/update gates remain separate. Once committed, the exact retained adoption authority permits HA-coordinated fresh boot authorization for the same installation; it does not authorize a different target or package.

## Validation completed locally

- Admission tests reject mismatching firmware, a running application, active native connections, pending transactions, missing configuration and already-managed devices.
- Authorization tests cover target/package binding, malformed and expired interactive authorization, and retained boot authority.
- The real shell transaction is exercised in an isolated filesystem fixture for success, failed start and interruption after journal creation, pairing replacement, start verification and both reference commits. Recovery uses the original hash-pinned 0.4.10 script. On macOS, the fixture normalizes the platform's `ls` suffix, supplies a synthetic stale lock and omits disk sync; this is a process-interruption check, not a physical power-loss test.
- All 21 existing Python checks and 8 Node checks passed using the documented dependency versions.
- Python/JavaScript/shell syntax and package digests were checked. The pairing review was rendered and its disabled-to-enabled checkbox behavior checked in a clearly labeled local simulated GUI.

## Live validation

The 0.5.0b4 candidate was installed through HACS and exercised against an existing supported speaker on a new Home Assistant deployment. Adoption completed with `DEVICE_ACTION_VERIFIED`; the room was enrolled, named and available over the native authenticated connection. No previous HA credentials or owner-supplied backup were used.

A bounded listen-only browser test used synthetic silence for uplink without requesting the browser microphone. The native call reached `ACTIVE_FULL` with fresh clocks and sustained endpoint capture, endpoint sink and browser rendering progress. Hangup terminated the call and the room returned to idle. These counters establish transport and processing progress, not audible speech or acoustic quality. Remote playback queue drops were observed; physical sound and listening quality remain unverified.

Call permissions remain explicit per Home Assistant user. Administrator configuration visibility alone does not grant a browser call; review **People & permissions** for the account that will use the room.

## Remaining physical acceptance

With a person at the speaker, verify sound, microphone pickup, mute indication and unmute chime. HA restart after adoption, speaker reboot and real power-loss tests remain unperformed. Future firmware upgrades and general unknown-firmware conversion are outside this candidate.

## Live findings during prerelease validation

The initial live attempt was refused before room enrollment because the current private pairing files differed from the old journal snapshot. Version 0.5.0b4 lets the distinct adoption action validate and snapshot the actual protected files, while retaining the mismatch refusal for normal installation. Tests cover this transfer and restoring the actual prior bytes after failed startup. Live adoption and the transport test subsequently succeeded as described above.
