# Installation

Intercom Manager 0.4.10 is a development alpha. Home Assistant integration setup and simulation are available; **a complete self-service path for permanent speaker installation, transfer and recovery is not yet available**. The release's review checkbox does not create the required action-bound physical checkpoint. Checkpoints are bound to their exact action and installation; never reuse an old operator checkpoint for a new operation or Home Assistant installation. Do not fabricate checkpoint files or bypass admission checks.

## Home Assistant

1. Back up Home Assistant and preserve any existing Intercom Manager component, entry and data. Install HACS using its [official instructions](https://www.hacs.xyz/docs/use/).
2. In HACS, open the three-dot menu â†’ **Custom repositories**. Add `https://github.com/chstech1/ha-intercom-manager` as **Integration**. Open Intercom Manager and select the reviewed [v0.4.10 development prerelease](https://github.com/chstech1/ha-intercom-manager/releases/tag/v0.4.10); enable prerelease versions if needed.
3. Follow the HACS restart indication when the Home Assistant administrator is ready for any household interruption. In **Settings â†’ Devices & services â†’ Add integration**, choose **Intercom Manager**, select permitted users and enter a trusted HTTPS origin. Open its sidebar and begin with simulation.

Browser microphone use requires trusted HTTPS and microphone permission. Home Assistant must reach the speaker on its local network or a properly routed VPN; Nabu Casa supplies the caller-to-Home-Assistant remote path and does not itself connect Home Assistant to a speaker on another LAN. Do not expose speaker management or intercom ports to the internet.

## Speaker preparation and current stop

The alpha targets a specific Third Reality `3RAB01090D` AArch64 platform with strict identity and known-build checks; similar devices are not automatically supported. Obtain the exact device's IP, Wi-Fi MAC, independently verified SSH host fingerprint and owner-controlled root SSH credential through legitimate device-specific means. Never post passwords, private keys or full configuration in an issue. No shared project password or other owner's files are needed or supplied.

In **Settings â†’ Devices & services â†’ Intercom Manager â†’ Configure â†’ Add a speaker connection**, enter the details privately. Saving the connection does not connect, install or restart the speaker. Under **Intercom Manager â†’ Speakers**, select it, choose **Check speaker**, and use **Reload status** to inspect the result. A successful compatibility check does not establish that backup, boot authority, physical safety or recovery are complete.

**Stop before executing a persistent action.** Prepare creates private paired identity material and a review-required operation, but the current release lacks a public general-purpose creator and owner guide for the required physical checkpoint. Its review checkbox alone cannot satisfy that requirement. A previously managed device on a different Home Assistant additionally requires a reviewed ownership/trust transfer and matching recoverable baseline; an IP or SSH change does not transfer intercom trust, and `MANAGED_BASELINE_REQUIRED` is a stop condition. No complete universal physical qualification checklist, stock restore or managed-device migration procedure is established yet. Preserve the current speaker and its backups; do not force execution, reset Wi-Fi or flash a whole image as a workaround.

After a future admitted and verified install, the code is designed to enroll or reuse a native profile and create a real-endpoint room named from the connection label if that device has no room. It extends existing matching call/history grants for selected allowed users. Existing rooms are not automatically repaired or remapped. Verify the real room, profile, users' permissions and actual availability before testing; a management connection is not itself a callable room.

For **stock** product information see [Third Reality's Voice & Music Assistant repository](https://github.com/thirdreality/voice-music-assistant). Stock-vendor instructions do not establish that Wi-Fi onboarding, reset or flashing is safe for a device already running this intercom application. The intercom protocol is not an ESPHome API; do not add the custom intercom endpoint as an ESPHome device.

New paired device identities have a finite 3,653-day lifetime and do not renew automatically. A supported owner-operated renewal workflow is not complete. Removing the HA integration does not automatically restore speaker software or erase its private data. Preserve backups and consult a verified device-specific recovery procedure before removal.