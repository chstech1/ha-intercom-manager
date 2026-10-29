# Intercom Manager

Home Assistant integration with a sidebar for room enrollment, permissions, calls, announcements, history and diagnostics.

**Development alpha 0.4.3 · Apache-2.0.** Repository: [chstech1/ha-intercom-manager](https://github.com/chstech1/ha-intercom-manager). This candidate is prepared for HACS custom-repository installation; 0.4.3 live acceptance remains pending.

Version 0.4.3 adds descriptions for the existing Home Assistant actions. Version 0.4.2 added standard subject and issuer key identifiers to newly issued enrollment certificates for strict TLS verification. It retains 0.4.1's nonce validation without jq regex support and keeps all eight firmware files unchanged. Existing identities require reviewed fresh paired enrollment; corrected end-to-end acceptance remains open.

## Alpha requirements and limits

- Home Assistant Core 2026.10.0 or newer; older versions are unsupported. The current source uses Core's `probatio` validation library. Compatibility beyond the tested Core versions is unproved.
- HACS already installed and authenticated, plus trusted HTTPS access to Home Assistant for browser microphone use.
- A Home Assistant administrator for configuration and explicit room permissions for callers.
- Real audio requires the supported profile4 speaker, verified SSH identity, explicit room permissions and valid endpoint credentials. The package includes an eight-file public firmware kit. It contains no credentials or device-specific configuration.

The **Speakers** page implements preparation, preflight, install, update/rollback and removal controls for the experimental permanent alpha. Connection enrollment uses Home Assistant's private native options flow; passwords and generated trust stay outside the replaceable component tree. Installing the integration alone does not install or start speaker firmware. Physical deployment requires an explicitly reviewed development-device checkpoint; first-install and recovery acceptance remain open.

The permanent startup design waits for fresh authenticated Home Assistant time authority after boot or process replacement; it must not resume using an old authority record. Enrollment credentials last seven days. Whole-device M4A hardening, physical reboot/power-loss/recovery, overlap/AEC, additional models, remote/caregiver use, production readiness and live HACS install/update/rollback acceptance remain unproved. Offline tests are not physical installation acceptance.

Simulation permits interface preparation without a live speaker. Treat a room's reported availability as current status, not a guarantee of physical audio, microphone or indicator health. Assist setup is outside this package's installation procedure.

## Install through HACS

1. Back up Home Assistant and inventory any existing `custom_components/intercom_m3_poc` directory and Intercom Manager entry before adopting this package. Preserve the exact previous code and integration data; do not overwrite a manual installation without an agreed migration.
2. Open HACS, select the three-dot menu, then **Custom repositories**. Enter `https://github.com/chstech1/ha-intercom-manager`, choose **Integration**, then **ADD**.
3. Open **Intercom Manager** in HACS and download the reviewed version. HACS uses the standard `custom_components/intercom_m3_poc` repository tree; no manual ZIP extraction is required. Select the exact reviewed release rather than an unreviewed default-branch build.
4. Follow HACS's discovery/restart indication. A Core restart or integration reload can interrupt other household services. On a production server, stop here until the administrator explicitly approves the required operation. This package does not schedule or perform a Core/server restart.
5. In **Settings → Devices & services → Add integration**, select **Intercom Manager**. Choose explicit allowed users and at least one trusted HTTPS origin. Enter only the origin (scheme, host and optional port), without a path, query, fragment or credentials.
6. Open **Intercom Manager** in the sidebar. A new entry starts empty. Configure rooms, compatible speaker profiles and room permissions in the manager. Start with simulation. To prepare a speaker, use the integration's **Configure** options flow to enter its connection details and independently verified SSH fingerprint, then inspect **Speakers**. Preparation does not grant approval for physical installation. Device actions require their separate exact checkpoint.

The official [HACS custom-repository instructions](https://www.hacs.xyz/docs/faq/custom_repositories/) describe repository registration. This project does not require default-catalog submission.

## Updates and rollback

End active calls and make a backup before updating. Use HACS's repository download/version controls to install a reviewed release. Runtime code changes may require an explicitly approved Core restart or entry reload; do not assume replacing files updates loaded Python code. Verify the manager, room metadata, permissions, history and endpoint availability afterwards, then perform a bounded authorized call check.

For rollback, select the previous reviewed HACS release, restore its code and use its matching data backup if a schema migration is incompatible. Obtain approval for any disruptive operation. This package has not yet passed HACS installation, upgrade, persistence or rollback testing in a real lab.

## Owned additions and removal

The integration owns:

- `custom_components/intercom_m3_poc/`, installed and replaced by HACS;
- one Home Assistant config entry under domain `intercom_m3_poc`;
- the **Intercom Manager** sidebar panel at `/intercom-m3-poc` and frontend assets at `/intercom_m3_poc`;
- manager entities and the `intercom_m3_poc.room_call`, `intercom_m3_poc.hangup` and `intercom_m3_poc.announce` actions;
- integration Store records named `intercom_m3_poc.manager.<entry_id>` and, when applicable, the matching `.migration_backup` record.

Real endpoint trust files are supplied separately and may be outside this directory. Keep all user data and private trust outside the replaceable component tree. The package does not modify household YAML, dashboards, automations, other integrations or networking as an installation step.

Speaker enrollment, operation journals and generated private trust are stored under Home Assistant's protected integration data directory, separately from HACS-managed code. Keep this data in a protected backup. The public firmware kit is replaceable release content; copying a new kit does not activate it on a speaker.

To remove it, end calls, preserve any required history/metadata backup, then remove the Intercom Manager entry using **Settings → Devices & services**. Remove the downloaded integration through HACS. Entry removal and HACS removal do not promise erasure of manager Store records, trust files or device identity; inspect the addition inventory and arrange approved cleanup of only owned data. Removal does not restore or erase a speaker runtime. Allow any required approved restart to complete before declaring removal verified.

## Privacy and support

Do not attach private keys, certificates, tokens, complete configuration dumps, household IP addresses or conversation audio to public issues. Share minimal redacted diagnostics in [ha-intercom-manager issues](https://github.com/chstech1/ha-intercom-manager/issues).

Project-authored integration, frontend, helpers, tone and icon are licensed under [Apache-2.0](LICENSE). See [NOTICE.md](NOTICE.md) and [THIRD_PARTY.json](THIRD_PARTY.json) for upstream provenance and third-party license notices.
