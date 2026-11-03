# Existing speaker adoption candidate — 0.5.0b1

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

## Remaining live acceptance

Install the candidate integration on Home Assistant and reload that integration. Prepare pairing for the selected speaker; inspect any eligibility failure without forcing it. After the owner confirms the precise pairing replacement, execute it and verify a real native TLS connection and the resulting room. Assign the intended room and check permitted users. With a person at the speaker, verify two-way sound, mute indication, unmute chime and hangup. HA restart, speaker reboot and real power-loss tests remain separate, unperformed acceptance work.

At publication, this build has not been accepted on a physical speaker. Successful offline checks do not establish physical sound, TLS reconnection, privacy indicators, restart or power-loss behavior. Future firmware upgrades and general unknown-firmware conversion are outside this candidate.
