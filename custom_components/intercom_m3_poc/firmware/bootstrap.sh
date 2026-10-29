#!/bin/sh
# Offline candidate. HA supplies this boot's volatile authorization over pinned SSH.
set -eu
umask 077
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
STATE=/data/intercom-m56-install
PRIVATE=$STATE/private
APP=/usr/bin/linux-voice-assistant-cpp
TICKET=/tmp/intercom-m56-boot.clock.json
APP_SHA=6dde57cd3e79e8a793163abb0773b0f480db0c6f049e37c4ddb54d50e072cdb3
fail() { printf 'intercom_boot_unavailable: %s\n' "$1" >&2; exit 78; }
sha() { sha256sum "$1" | cut -c1-64; }
root_private() {
    [ -f "$1" ] && [ ! -L "$1" ] &&
        [ "$(LC_ALL=C ls -ldn "$1" | awk '{print $2 ":" $3 ":" $4 ":" $1}')" = 1:0:0:-rw------- ]
}
case "${1:-}" in --check|start) :;; *) fail invalid_action;; esac
[ "$#" = 1 ] && [ "$(id -u)" = 0 ] || fail root_required
for dir in "$STATE" "$PRIVATE"; do
    [ -d "$dir" ] && [ ! -L "$dir" ] &&
        [ "$(readlink -f "$dir")" = "$dir" ] &&
        [ "$(LC_ALL=C ls -ldn "$dir" | awk '{print $3 ":" $4 ":" $1}')" = 0:0:drwx------ ] || fail unsafe_state
done
if [ -e "$STATE/pending" ] || [ -L "$STATE/pending" ]; then
    root_private /tmp/intercom-m56-start-permit || fail transaction_pending
    [ -f "$STATE/pending" ] && [ ! -L "$STATE/pending" ] || fail unsafe_pending
    expected_permit=$(printf '%s\t%s' "$(cat /proc/sys/kernel/random/boot_id)" "$(cat "$STATE/pending")")
    [ "$(cat /tmp/intercom-m56-start-permit)" = "$expected_permit" ] || fail transaction_pending
fi
root_private "$TICKET" || fail fresh_ha_boot_authorization_required
boot=$(cat /proc/sys/kernel/random/boot_id)
jq -e --arg boot "$boot" --arg app "$APP_SHA" '.schema==1 and .boot_id==$boot and
 .application_sha256==$app and (.nonce | type=="string" and length==32) and
 .nonce!="00000000000000000000000000000000"' "$TICKET" >/dev/null || fail invalid_authorization
nonce=$(jq -er '.nonce' "$TICKET") || fail invalid_authorization
case "$nonce" in ''|*[!0-9a-f]*) fail invalid_authorization;; esac
[ "${#nonce}" = 32 ] && [ "$nonce" != 00000000000000000000000000000000 ] || fail invalid_authorization
[ -f "$APP" ] && [ ! -L "$APP" ] &&
    [ "$(sha "$APP")" = "$APP_SHA" ] || fail application_mismatch
for name in ca.pem endpoint.pem endpoint.key intercom_m3_poc.json sound.json; do
    root_private "$PRIVATE/$name" || fail unsafe_private_enrollment
done
configsha=$(sha "$PRIVATE/intercom_m3_poc.json")
jq -e --arg digest "$configsha" '.configuration_sha256==$digest' "$TICKET" >/dev/null || fail configuration_mismatch
[ -f "$STATE/attention.wav" ] && [ ! -L "$STATE/attention.wav" ] &&
    [ "$(sha "$STATE/attention.wav")" = 47320cbb9e7013967a4efdf3925fb5c1f28b5ccfeb69bf6a6f7d71b8cf922286 ] || fail attention_mismatch
# Native candidate consumes the full attestation at launch and checks HA bracket,
# boottime age, rate, generation, floor and configuration/self hashes. --check does
# not consume it. No device CLOCK_REALTIME value is an authority here.
jq -e '.enabled==true and .profile==4 and .local_commands_enabled==false and
 .ha_boot_clock_path=="/tmp/intercom-m56-boot.clock.json" and
 .ha_clock_generation>0 and .ha_clock_floor_utc_us>0 and .ha_clock_rate_ppb==1000000 and
 .ca_file=="/data/intercom-m56-install/private/ca.pem" and
 .certificate_file=="/data/intercom-m56-install/private/endpoint.pem" and
 .key_file=="/data/intercom-m56-install/private/endpoint.key" and
 .beep_file=="/data/intercom-m56-install/attention.wav"' "$PRIVATE/intercom_m3_poc.json" >/dev/null || fail invalid_configuration
jq -e '.active_wake_words == []' "$PRIVATE/sound.json" >/dev/null || fail assist_not_excluded
[ "${1:-}" = --check ] && exit 0
name=$(jq -er '.device.name | select(type=="string" and length>0)' /data/conf/device.json) || fail device_name_missing
# Child-only allocator policy is inseparable from the application/startup pair.
exec env MALLOC_MMAP_THRESHOLD_=131072 INTERCOM_POC_CONFIG="$PRIVATE/intercom_m3_poc.json" \
    "$APP" --name "$name" --host 0.0.0.0 --port 6053 --wakeword-models "" \
    --preferences-file "$PRIVATE/sound.json"
