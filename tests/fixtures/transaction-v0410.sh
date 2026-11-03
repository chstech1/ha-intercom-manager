#!/bin/sh
# Paired application/startup transaction. Offline alpha; live gate intentionally closed.
set -eu
umask 077
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
ROOT=''
FIXTURE=0
if [ -n "${INTERCOM_FIXTURE_ROOT:-}" ]; then
    ROOT=$(readlink -f "$INTERCOM_FIXTURE_ROOT")
    case "$ROOT" in /tmp/intercom-permanent-fixture.*) :;; *) exit 78;; esac
    [ -d "$ROOT" ] && [ ! -L "$INTERCOM_FIXTURE_ROOT" ] &&
        [ "$(cat "$ROOT/.intercom-fixture" 2>/dev/null)" = offline-paired-fixture-v1 ] || exit 78
    FIXTURE=1
fi
STATE=$ROOT/data/intercom-m56-install
STAGE=$ROOT/tmp/intercom-permanent-alpha
APP=$ROOT/usr/bin/linux-voice-assistant-cpp
INIT=$ROOT/etc/init.d/S99ha-speaker
BOOT=$ROOT/etc/init.d/S98intercom-recover
HELPER=$STATE/bootstrap.sh
OP=00000000000000000000000000000000
ACTION=${1:-}
LOCKED=0
die() { printf 'permanent-alpha: %s\n' "$*" >&2; exit 1; }
regular() { [ -f "$1" ] && [ ! -L "$1" ]; }
mode_is() { [ "$(LC_ALL=C ls -ldn "$1" | awk '{print $1}')" = "$2" ]; }
sha() { sha256sum "$1" | cut -c1-64; }
hex() { [ "${#1}" = "$2" ] && ! printf '%s' "$1" | LC_ALL=C grep -q '[^0-9a-f]'; }
emit() { printf '{"schema":1,"operation_id":"%s","action":"%s","stage":"%s","status":"%s"}\n' "$OP" "$ACTION" "$1" "$2"; }
secure_path() {
    current=$1
    while [ "$current" != "$ROOT" ] && [ "$current" != / ]; do
        [ ! -L "$current" ] || die symlink_path
        if [ "$FIXTURE" != 1 ] && [ "$current" = /tmp ]; then
            [ "$(LC_ALL=C ls -ldn /tmp | awk '{print $3 ":" $1}')" = 0:drwxrwxrwt ] || die unsafe_tmp_parent
            return 0
        fi
        if [ -e "$current" ]; then
            [ "$(LC_ALL=C ls -ldn "$current" | awk '{print $3}')" = "$(id -u)" ] || die wrong_owner
            perm=$(LC_ALL=C ls -ldn "$current" | awk '{print $1}')
            [ "$(printf '%s' "$perm" | cut -c6)" != w ] &&
                [ "$(printf '%s' "$perm" | cut -c9)" != w ] || die writable_path
        fi
        current=$(dirname "$current")
    done
}
# A fixture can stop the real shell at every durable seam. Never enabled live.
seam() {
    sync
    if [ "$FIXTURE" = 1 ] && [ "${INTERCOM_FIXTURE_CRASH:-}" = "$1" ]; then kill -KILL $$; fi
}
atomic_copy() {
    regular "$1" || die missing_source
    secure_path "$2"
    secure_path "$2.new"
    [ ! -e "$2.new" ] || regular "$2.new" || die unsafe_staging_file
    cp "$1" "$2.new"
    chmod "$3" "$2.new"
    seam copy_complete
    mv -f "$2.new" "$2"
    sync
}
atomic_text() { secure_path "$1"; secure_path "$1.new"; printf '%s\n' "$2" > "$1.new"; sync; mv -f "$1.new" "$1"; sync; }
pair_record() {
    printf '%s  application\n' "$(sha "$APP")"
    printf '%s  S99ha-speaker\n' "$(sha "$INIT")"
    if regular "$HELPER"; then printf '%s  bootstrap.sh\n' "$(sha "$HELPER")"; else printf '%s\n' 'ABSENT  bootstrap.sh'; fi
}
PRIVATE_NAMES='ca.pem endpoint.pem endpoint.key intercom_m3_poc.json sound.json'
private_record() {
    for name in $PRIVATE_NAMES; do
        path=$STATE/private/$name
        if regular "$path"; then
            secure_path "$path"; mode_is "$path" -rw------- || die private_permissions
            printf '%s  %s\n' "$(sha "$path")" "$name"
        else [ ! -e "$path" ] && [ ! -L "$path" ] || die unsafe_private_file; printf 'ABSENT  %s\n' "$name"; fi
    done
}
verify_private_snapshot() {
    private_dir=$STATE/snapshots/$1/private
    record=$STATE/snapshots/$1/private.sha256
    regular "$record" && [ "$(wc -l < "$record" | tr -d ' ')" = 5 ] || die private_snapshot_missing
    for name in $PRIVATE_NAMES; do
        [ "$(awk -v n="$name" '$2==n {count++} END {print count+0}' "$record")" = 1 ] || die private_snapshot_inventory
        expected=$(awk -v n="$name" '$2==n {print $1}' "$record")
        if [ "$expected" = ABSENT ]; then [ ! -e "$private_dir/$name" ] && [ ! -L "$private_dir/$name" ] || die private_snapshot_mismatch;
        else hex "$expected" 64 && regular "$private_dir/$name" && [ "$(sha "$private_dir/$name")" = "$expected" ] || die private_snapshot_mismatch; fi
    done
}
validate_record() {
    regular "$1" || die pair_record_missing
    [ "$(wc -l < "$1" | tr -d ' ')" = 3 ] || die invalid_pair_record
    awk 'NR==1 {if ($2!="application" || length($1)!=64 || $1~/[^0-9a-f]/) exit 1}
         NR==2 {if ($2!="S99ha-speaker" || length($1)!=64 || $1~/[^0-9a-f]/) exit 1}
         NR==3 {if ($2!="bootstrap.sh" || ($1!="ABSENT" && (length($1)!=64 || $1~/[^0-9a-f]/))) exit 1}
         NF!=2 {exit 1}' "$1" || die invalid_pair_record
}
verify_snapshot() {
    dir=$STATE/snapshots/$1
    secure_path "$dir"
    validate_record "$dir/pair.sha256"
    for name in application S99ha-speaker bootstrap.sh; do
        expected=$(awk -v n="$name" '$2==n {print $1}' "$dir/pair.sha256")
        if [ "$expected" = ABSENT ]; then
            [ ! -e "$dir/$name" ] && [ ! -L "$dir/$name" ] || die snapshot_absence_mismatch
        else
            regular "$dir/$name" && [ "$(sha "$dir/$name")" = "$expected" ] || die snapshot_digest_mismatch
        fi
    done
    verify_private_snapshot "$1"
}
read_ref() { regular "$STATE/$1" || die ref_missing; ref=$(cat "$STATE/$1"); hex "$ref" 32 || die invalid_ref; verify_snapshot "$ref"; }
snapshot_current() {
    dir=$STATE/snapshots/$1
    [ ! -e "$dir" ] && [ ! -L "$dir" ] || die operation_reused
    mkdir "$dir"
    cp "$APP" "$dir/application"; chmod 700 "$dir/application"
    cp "$INIT" "$dir/S99ha-speaker"; chmod 700 "$dir/S99ha-speaker"
    if regular "$HELPER"; then cp "$HELPER" "$dir/bootstrap.sh"; chmod 700 "$dir/bootstrap.sh"; fi
    pair_record > "$dir/pair.sha256"
    mkdir "$dir/private"
    for name in $PRIVATE_NAMES; do
        if regular "$STATE/private/$name"; then cp "$STATE/private/$name" "$dir/private/$name"; chmod 600 "$dir/private/$name"; fi
    done
    private_record > "$dir/private.sha256"
    sync
    verify_snapshot "$1"
}
check_current() {
    regular "$APP" && regular "$INIT" || die pair_missing
    mode_is "$APP" -rwxr-xr-x && mode_is "$INIT" -rwxr-xr-x || die pair_mode_mismatch
    if regular "$HELPER"; then mode_is "$HELPER" -rwx------ || die helper_mode_mismatch; fi
    if regular "$STATE/current.ref"; then
        read_ref current.ref
        pair_record | cmp -s - "$STATE/snapshots/$ref/pair.sha256" || die unmanaged_current_pair
        private_record | cmp -s - "$STATE/snapshots/$ref/private.sha256" || die unmanaged_current_private
    fi
}
fixture_service() {
    # Only this fixed fixture path is executable; operation input never supplies code.
    "$ROOT/fixture-service.sh" "$1"
}
runtime_get() { awk -F '\t' -v k="$1" '$1==k {print $2}' "${RUNTIME_PROOF:-$STATE/runtime.expected}"; }
runtime_admit() {
    proof=$STAGE/runtime.expected
    regular "$proof" || die runtime_identity_missing
    secure_path "$proof"
    mode_is "$proof" -rw------- || die runtime_identity_permissions
    awk -F '\t' 'BEGIN {split("schema operation_id boot_id monitor_pid monitor_start_ticks monitor_cmdline_sha256 monitor_exe_sha256 application_pid application_start_ticks application_sha256 pulse_pid pulse_start_ticks pulse_sha256 persistent_write boot_clock_ready recovery_ready ha_issued_epoch device_uptime", keys," "); for(i in keys) allowed[keys[i]]=1}
      NF!=2 || !allowed[$1] || seen[$1]++ {exit 1} END {if(NR!=18) exit 1}' "$proof" || die invalid_runtime_identity
    RUNTIME_PROOF=$proof
    [ "$(runtime_get schema)" = 1 ] && [ "$(runtime_get operation_id)" = "$OP" ] || die runtime_operation_mismatch
    [ "$(runtime_get boot_id)" = "$(cat /proc/sys/kernel/random/boot_id)" ] || die runtime_boot_changed
    issued=$(runtime_get ha_issued_epoch); uptime=$(runtime_get device_uptime)
    case "$issued:$uptime" in ''|*[!0-9:]*|:*|*:) die invalid_runtime_time;; esac
    mono=$(cut -d. -f1 /proc/uptime)
    # HA clock admission is the native consumed attestation, not device realtime.
    [ "$mono" -ge "$uptime" ] && [ "$mono" -le "$((uptime + 120))" ] || die stale_runtime_admission
    for key in monitor_pid monitor_start_ticks pulse_pid pulse_start_ticks; do
        number=$(runtime_get "$key"); case "$number" in ''|*[!0-9]*|0) die invalid_runtime_pid;; esac
    done
    for key in application_pid application_start_ticks; do
        number=$(runtime_get "$key"); case "$number" in ''|*[!0-9]*) die invalid_runtime_pid;; 0) [ "$ACTION" = activate_boot ] || die stopped_voice_not_admitted;; esac
    done
    for key in monitor_cmdline_sha256 monitor_exe_sha256 application_sha256 pulse_sha256; do hex "$(runtime_get "$key")" 64 || die invalid_runtime_hash; done
    pid=$(runtime_get application_pid)
    regular "$ROOT/var/run/thirdreality/ha-speaker.pid" &&
        [ "$(cat "$ROOT/var/run/thirdreality/ha-speaker.pid")" = "$(runtime_get monitor_pid)" ] || die monitor_pidfile_mismatch
    if [ "$pid" = 0 ]; then
        [ "$ACTION" = activate_boot ] && [ "$(runtime_get application_start_ticks)" = 0 ] &&
            regular "$STATE/current.ref" && regular "$HELPER" || die stopped_voice_not_admitted
        [ -z "$(pidof linux-voice-assistant-cpp 2>/dev/null || true)" ] || die unexpected_voice_process
        [ "$(runtime_get application_sha256)" = "$(sha "$APP")" ] || die stopped_voice_hash_mismatch
    else
        regular "$ROOT/var/run/thirdreality/voice-assistant.pid" &&
            [ "$(cat "$ROOT/var/run/thirdreality/voice-assistant.pid")" = "$pid" ] || die voice_pidfile_mismatch
        [ "$(readlink "/proc/$pid/exe")" = "$APP" ] || die non_native_baseline
        proc_matches "$pid" "$(runtime_get application_start_ticks)" "$(runtime_get application_sha256)" || die voice_prestate_changed
    fi
    proc_matches "$(runtime_get monitor_pid)" "$(runtime_get monitor_start_ticks)" "$(runtime_get monitor_exe_sha256)" "$(runtime_get monitor_cmdline_sha256)" || die monitor_prestate_changed
    proc_matches "$(runtime_get pulse_pid)" "$(runtime_get pulse_start_ticks)" "$(runtime_get pulse_sha256)" || die pulse_prestate_changed
    [ "$(pidof pulseaudio 2>/dev/null || true)" = "$(runtime_get pulse_pid)" ] || die pulse_not_single
    if [ "$FIXTURE" != 1 ]; then
        [ -z "$(pidof linux-voice-assistant-cpp-m56-poc 2>/dev/null || true)" ] || die ram_stock_restoration_required
        if [ ! -e "$STATE/current.ref" ]; then
            [ "$(sha "$APP")" = 2117c45183e6b21da92bc4aa8e46a9c58a0fd6d5b8176661dd90042e6e645a88 ] &&
                [ "$(sha "$INIT")" = 4d010c5749a457d45d59bd20524963e360f7499496303b2e102d0a96097091c1 ] || die unsupported_stock_baseline
        fi
    fi
    atomic_copy "$proof" "$STATE/runtime.expected" 600
    RUNTIME_PROOF=$STATE/runtime.expected
    shared_state > "$STATE/shared.expected"
    chmod 600 "$STATE/shared.expected"
    if [ "$ACTION" = install ]; then atomic_copy "$STAGE/S99ha-speaker" "$STATE/managed-init" 700; fi
}
proc_start() { awk '{print $22}' "/proc/$1/stat" 2>/dev/null; }
proc_live() { kill -0 "$1" 2>/dev/null && [ "$(awk '{print $3}' "/proc/$1/stat" 2>/dev/null)" != Z ]; }
proc_matches() {
    proc_live "$1" && [ "$(proc_start "$1")" = "$2" ] &&
        [ "$(sha "/proc/$1/exe")" = "$3" ] &&
        { [ -z "${4:-}" ] || [ "$(sha "/proc/$1/cmdline")" = "$4" ]; }
}
stop_exact() {
    stop_pid=$1; stop_start=$2; stop_exe=$3; stop_cmd=${4:-}
    proc_live "$stop_pid" || return 0
    proc_matches "$stop_pid" "$stop_start" "$stop_exe" "$stop_cmd" || die process_identity_changed
    kill -TERM "$stop_pid"
    attempts=0
    while proc_live "$stop_pid" && [ "$attempts" -lt 5 ]; do sleep 1; attempts=$((attempts + 1)); done
    if proc_live "$stop_pid"; then
        proc_matches "$stop_pid" "$stop_start" "$stop_exe" "$stop_cmd" || die process_identity_changed
        kill -KILL "$stop_pid"
    fi
    attempts=0
    while proc_live "$stop_pid" && [ "$attempts" -lt 3 ]; do sleep 1; attempts=$((attempts + 1)); done
    ! proc_live "$stop_pid" || die process_stop_unproved
}
shared_state() {
    # Never signal these services; compare process identity, including PID reuse.
    for name in pulseaudio sendspin-client bluetoothd; do
        for shared_pid in $(pidof "$name" 2>/dev/null || true); do
            printf '%s %s %s %s\n' "$name" "$shared_pid" "$(proc_start "$shared_pid")" "$(sha "/proc/$shared_pid/exe")"
        done
    done
    for name in netmonitor.pid; do
        file=$ROOT/var/run/thirdreality/$name
        if regular "$file"; then
            shared_pid=$(cat "$file"); case "$shared_pid" in ''|*[!0-9]*) die invalid_shared_pid;; esac
            proc_live "$shared_pid" || die shared_process_dead
            printf '%s %s %s %s\n' "$name" "$shared_pid" "$(proc_start "$shared_pid")" "$(sha "/proc/$shared_pid/exe")"
        fi
    done
}
runtime_stop() {
    regular "$STATE/runtime.expected" || die runtime_identity_missing
    [ "$(runtime_get boot_id)" = "$(cat /proc/sys/kernel/random/boot_id)" ] || die runtime_boot_changed
    flag=$ROOT/tmp/ha_disable_voice
    secure_path "$flag"
    if [ -e "$flag" ]; then regular "$flag" && [ ! -s "$flag" ] || die unsafe_disable_flag; else : > "$flag"; chmod 600 "$flag"; fi
    monitor_file=$ROOT/var/run/thirdreality/ha-speaker.pid
    if regular "$monitor_file"; then
        pid=$(cat "$monitor_file"); case "$pid" in ''|*[!0-9]*) die invalid_monitor_pid;; esac
        if proc_live "$pid"; then
            if [ "$pid" = "$(runtime_get monitor_pid)" ]; then
                stop_exact "$pid" "$(runtime_get monitor_start_ticks)" "$(runtime_get monitor_exe_sha256)" "$(runtime_get monitor_cmdline_sha256)"
            elif regular "$STATE/managed-monitor.identity"; then
                read -r managed_pid managed_start managed_exe managed_cmd < "$STATE/managed-monitor.identity"
                [ "$pid" = "$managed_pid" ] || die unknown_monitor
                stop_exact "$pid" "$managed_start" "$managed_exe" "$managed_cmd"
            else die unknown_monitor; fi
        fi
        rm "$monitor_file"
    fi
    voice_file=$ROOT/var/run/thirdreality/voice-assistant.pid
    if regular "$voice_file"; then
        pid=$(cat "$voice_file"); case "$pid" in ''|*[!0-9]*) die invalid_voice_pid;; esac
        if proc_live "$pid"; then
            [ "$(readlink "/proc/$pid/exe")" = "$APP" ] || die unexpected_voice_executable
            app_hash=$(sha "/proc/$pid/exe")
            old=$(awk '$2=="application" {print $1}' "$STATE/snapshots/$(cat "$STATE/pending")/pair.sha256")
            new=$(awk '$2=="application" {print $1}' "$STATE/proposed.sha256")
            [ "$app_hash" = "$old" ] || [ "$app_hash" = "$new" ] || die unexpected_voice_hash
            if [ "$pid" = "$(runtime_get application_pid)" ]; then
                [ "$(proc_start "$pid")" = "$(runtime_get application_start_ticks)" ] || die voice_pid_reused
            fi
            stop_exact "$pid" "$(proc_start "$pid")" "$app_hash"
        fi
        rm "$voice_file"
    fi
    shared_state | cmp -s - "$STATE/shared.expected" || die shared_service_changed
}
runtime_verify() {
    file=$ROOT/var/run/thirdreality/voice-assistant.pid
    regular "$file" || return 1
    pid=$(cat "$file"); case "$pid" in ''|*[!0-9]*) return 1;; esac
    proc_live "$pid" && [ "$(readlink "/proc/$pid/exe")" = "$APP" ] &&
        [ "$(sha "/proc/$pid/exe")" = "$(sha "$APP")" ] || return 1
    ports=6053
    if regular "$HELPER"; then
        tr '\000' '\n' < "/proc/$pid/environ" | grep -qx 'MALLOC_MMAP_THRESHOLD_=131072' || return 1
        ports='6053 6054'
    fi
        # Native and media listeners must belong to this exact process.
        for port in $ports; do
            hex_port=$(printf '%04X' "$port")
            found=0
            for inode in $(awk -v p=":$hex_port" '$4=="0A" && substr($2,length($2)-length(p)+1)==p {print $10}' /proc/net/tcp /proc/net/tcp6); do
                for fd in "/proc/$pid/fd/"*; do [ "$(readlink "$fd" 2>/dev/null)" != "socket:[$inode]" ] || found=1; done
            done
            [ "$found" = 1 ] || return 1
        done
    shared_state | cmp -s - "$STATE/shared.expected"
}
runtime_start() {
    # Old stock init has no monitor-adopt entry; run the reviewed stock monitor
    # through the installed patched init retained in the admitted staging bundle.
    monitor_init=$INIT
    if ! regular "$HELPER"; then monitor_init=$STATE/managed-init; fi
    regular "$monitor_init" || die monitor_adoption_source_missing
    for path in "$STATE/monitor.log" "$STATE/application.log" "$STATE/managed-monitor.identity"; do secure_path "$path"; done
    # Daemon stdout must not hold the finite SSH command channel open or leak
    # application diagnostics into the public JSON operation receipt.
    "$monitor_init" monitor-adopt >> "$STATE/monitor.log" 2>&1 < /dev/null || return 1
    file=$ROOT/var/run/thirdreality/ha-speaker.pid
    regular "$file" || return 1
    pid=$(cat "$file"); case "$pid" in ''|*[!0-9]*) return 1;; esac
    proc_live "$pid" || return 1
    printf '%s %s %s %s\n' "$pid" "$(proc_start "$pid")" "$(sha "/proc/$pid/exe")" "$(sha "/proc/$pid/cmdline")" > "$STATE/managed-monitor.identity"
    permit=$ROOT/tmp/intercom-m56-start-permit
    secure_path "$permit"
    if regular "$STATE/pending"; then
        printf '%s\t%s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$(cat "$STATE/pending")" > "$permit"
        chmod 600 "$permit"
    fi
    "$INIT" voice-assistant start >> "$STATE/application.log" 2>&1 < /dev/null || { rm -f "$permit"; return 1; }
    attempts=0
    while [ "$attempts" -lt 15 ]; do runtime_verify && { rm -f "$permit"; return 0; }; sleep 1; attempts=$((attempts + 1)); done
    rm -f "$permit"
    return 1
}
quiesce() {
    if [ "$FIXTURE" = 1 ] && [ "${INTERCOM_PROCESS_FIXTURE:-0}" != 1 ]; then fixture_service stop; else runtime_stop; fi
}
adopt_start_verify() {
    if [ "$FIXTURE" = 1 ] && [ "${INTERCOM_PROCESS_FIXTURE:-0}" != 1 ]; then fixture_service adopt && fixture_service start && fixture_service verify
    else runtime_start; fi
}
apply_snapshot() {
    verify_snapshot "$1"
    dir=$STATE/snapshots/$1
    mkdir -p "$STATE/private"; chmod 700 "$STATE/private"
    for name in $PRIVATE_NAMES; do
        expected=$(awk -v n="$name" '$2==n {print $1}' "$dir/private.sha256")
        if [ "$expected" = ABSENT ]; then secure_path "$STATE/private/$name"; rm -f "$STATE/private/$name";
        else atomic_copy "$dir/private/$name" "$STATE/private/$name" 600; fi
    done
    seam private_replaced
    atomic_copy "$dir/application" "$APP" 755; seam application_replaced
    atomic_copy "$dir/S99ha-speaker" "$INIT" 755; seam init_replaced
    expected=$(awk '$2=="bootstrap.sh" {print $1}' "$dir/pair.sha256")
    if [ "$expected" = ABSENT ]; then rm -f "$HELPER"; else atomic_copy "$dir/bootstrap.sh" "$HELPER" 700; fi
    seam helper_replaced
}
recover_pending() {
    if [ ! -e "$STATE/pending" ] && [ ! -L "$STATE/pending" ]; then return 0; fi
    regular "$STATE/pending" || die unsafe_pending
    pending=$(cat "$STATE/pending"); hex "$pending" 32 || die invalid_pending
    verify_snapshot "$pending"
    # Recovery accepts only old or known proposed bytes, never overwrites unknown changes.
    regular "$STATE/proposed.sha256" || die proposed_pair_missing
    validate_record "$STATE/proposed.sha256"
    for name in application S99ha-speaker bootstrap.sh; do
        case "$name" in application) path=$APP;; S99ha-speaker) path=$INIT;; *) path=$HELPER;; esac
        old=$(awk -v n="$name" '$2==n {print $1}' "$STATE/snapshots/$pending/pair.sha256")
        new=$(awk -v n="$name" '$2==n {print $1}' "$STATE/proposed.sha256")
        if regular "$path"; then actual=$(sha "$path"); else [ ! -e "$path" ] && [ ! -L "$path" ] || die unsafe_partial_pair; actual=ABSENT; fi
        [ "$actual" = "$old" ] || [ "$actual" = "$new" ] || die unknown_partial_pair
    done
    regular "$STATE/proposed-private.sha256" || die proposed_private_missing
    for name in $PRIVATE_NAMES; do
        path=$STATE/private/$name
        old=$(awk -v n="$name" '$2==n {print $1}' "$STATE/snapshots/$pending/private.sha256")
        new=$(awk -v n="$name" '$2==n {print $1}' "$STATE/proposed-private.sha256")
        if regular "$path"; then actual=$(sha "$path"); else [ ! -e "$path" ] && [ ! -L "$path" ] || die unsafe_partial_private; actual=ABSENT; fi
        [ "$actual" = "$old" ] || [ "$actual" = "$new" ] || die unknown_partial_private
    done
    if [ "$ACTION" != boot-reconcile ]; then quiesce; fi
    apply_snapshot "$pending"
    if [ "$ACTION" != boot-reconcile ]; then
        if regular "$HELPER" && { [ "$FIXTURE" != 1 ] || [ "${INTERCOM_PROCESS_FIXTURE:-0}" = 1 ]; }; then
            # A failed launch may already have consumed the candidate ticket.
            # HA may pre-enroll one separate, short-lived ticket for the exact
            # fallback app/config; it is never persisted or locally fabricated.
            regular "$STAGE/rollback-boot-clock.json" || die recovery_fresh_ha_clock_required
            secure_path "$STAGE/rollback-boot-clock.json"
            mode_is "$STAGE/rollback-boot-clock.json" -rw------- || die recovery_ticket_permissions
            atomic_copy "$STAGE/rollback-boot-clock.json" "$ROOT/tmp/intercom-m56-boot.clock.json" 600
        fi
        adopt_start_verify || die recovery_start_failed
    fi
    atomic_text "$STATE/current.ref" "$pending"
    rm -f "$STATE/pending"
    sync
    emit restored VERIFIED
}
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$LOCKED" = 1 ]; then rm -f "$STATE/lock/owner"; rmdir "$STATE/lock" 2>/dev/null || true; fi
    exit "$result"
}
case "$ACTION" in preflight|install|verify|rollback_previous|restore_stock|remove|reconcile|boot-reconcile|activate_boot) :;; *) die invalid_action;; esac
[ "$#" = 1 ] || die invalid_arguments
if [ "$FIXTURE" != 1 ]; then
    [ "$(id -u)" = 0 ] || die root_required
    if [ "$ACTION" = boot-reconcile ]; then
        regular "$STATE/engine.sh" && [ "$(sha "$STATE/engine.sh")" = "$(sha "$0")" ] || die uninstalled_boot_engine
    fi
    # The HA API never emits these approvals. They require the separate exact
    # physical checkpoint, not a GUI flag, browser argument or environment value.
    if [ "$ACTION" != boot-reconcile ] && { ! regular "$STAGE/runtime.expected" ||
       ! awk -F '\t' '$1=="persistent_write" || $1=="boot_clock_ready" || $1=="recovery_ready" {if($2=="true") n++} END {exit n==3?0:1}' "$STAGE/runtime.expected"; }; then
        emit admission BLOCKED_PERSISTENT_ACTIVATION
        exit 78
    fi
fi
for path in "$STAGE" "$STATE" "$APP" "$INIT" "$BOOT" "$STATE/lock" "$STATE/pending" \
    "$STATE/snapshots" "$STATE/current.ref" "$STATE/previous.ref" "$STATE/stock.ref" "$STATE/proposed.sha256"; do secure_path "$path"; done
mkdir -p "$STATE/snapshots"
if [ "$ACTION" != reconcile ] && [ "$ACTION" != boot-reconcile ]; then
    regular "$STAGE/operation.id" || die operation_missing
    OP=$(cat "$STAGE/operation.id"); hex "$OP" 32 || die invalid_operation
    regular "$STAGE/expected.sha256" || die expected_pair_missing
    validate_record "$STAGE/expected.sha256"
    pair_record | cmp -s "$STAGE/expected.sha256" - || die stale_prestate
fi
if { [ "$ACTION" = reconcile ] || [ "$ACTION" = boot-reconcile ]; } && [ -d "$STATE/lock" ]; then
    regular "$STATE/lock/owner" || die unsafe_lock
    read -r lock_pid lock_start lock_boot < "$STATE/lock/owner"
    case "$lock_pid:$lock_start" in *[!0-9:]*|:|*:) die unsafe_lock;; esac
    if [ "$lock_boot" = "$(cat /proc/sys/kernel/random/boot_id)" ] && [ -r "/proc/$lock_pid/stat" ] && [ "$(awk '{print $22}' "/proc/$lock_pid/stat")" = "$lock_start" ]; then die operation_locked; fi
    rm "$STATE/lock/owner"; rmdir "$STATE/lock"
fi
mkdir "$STATE/lock" || die operation_locked
LOCKED=1
printf '%s %s %s\n' "$$" "$(awk '{print $22}' /proc/$$/stat)" "$(cat /proc/sys/kernel/random/boot_id)" > "$STATE/lock/owner"
trap cleanup EXIT
trap 'exit 143' HUP INT TERM
if [ "$ACTION" = reconcile ] || [ "$ACTION" = boot-reconcile ]; then recover_pending; emit reconciled VERIFIED; exit 0; fi
[ ! -e "$STATE/pending" ] && [ ! -L "$STATE/pending" ] || die recovery_required
check_current
case "$ACTION" in
    preflight) emit preflight VERIFIED; exit 0;;
    verify)
        if [ "$FIXTURE" = 1 ] && [ "${INTERCOM_PROCESS_FIXTURE:-0}" != 1 ]; then fixture_service verify || die verification_failed;
        else runtime_verify || die verification_failed; fi
        emit running VERIFIED; exit 0;;
    activate_boot)
        runtime_admit; quiesce
        runtime_start || die boot_start_failed_voice_unavailable
        emit boot_active VERIFIED; exit 0;;
esac
if [ "$ACTION" = install ]; then
    secure_path "$STATE/proposed.sha256.new"
    regular "$STAGE/bundle.sha256" && regular "$STAGE/approved-manifest.sha256" || die manifest_missing
    [ "$(sha "$STAGE/bundle.sha256")" = "$(cat "$STAGE/approved-manifest.sha256")" ] || die manifest_pin_mismatch
    [ "$(wc -l < "$STAGE/bundle.sha256" | tr -d ' ')" = 6 ] || die manifest_inventory
    for name in application S99ha-speaker bootstrap.sh transaction.sh S98intercom-recover attention.wav; do
        [ "$(awk -v n="$name" '$2==n {count++} END {print count+0}' "$STAGE/bundle.sha256")" = 1 ] || die manifest_inventory
        regular "$STAGE/$name" || die payload_missing
        secure_path "$STAGE/$name"
        case "$name" in attention.wav) mode_is "$STAGE/$name" -rw------- || mode_is "$STAGE/$name" -rw-r--r-- || die payload_mode_mismatch;;
            *) mode_is "$STAGE/$name" -rwx------ || mode_is "$STAGE/$name" -rwxr-xr-x || die payload_mode_mismatch;; esac
        expected=$(awk -v n="$name" '$2==n {print $1}' "$STAGE/bundle.sha256")
        hex "$expected" 64 && [ "$(sha "$STAGE/$name")" = "$expected" ] || die payload_digest_mismatch
    done
    [ "$(sha "$0")" = "$(sha "$STAGE/transaction.sh")" ] || die engine_changed
    if [ "$FIXTURE" != 1 ]; then
        /lib/ld-linux-aarch64.so.1 --list "$STAGE/application" >/dev/null || die loader_dependency_mismatch
        required_kb=$(( ( $(wc -c < "$APP") * 2 + $(wc -c < "$STAGE/application") * 3 ) / 1024 + 1024 ))
        for path in "$STATE" "$(dirname "$APP")" "$(dirname "$INIT")"; do
            available_kb=$(df -Pk "$path" | awk 'END {print $4}')
            case "$available_kb" in ''|*[!0-9]*) die free_space_unavailable;; esac
            [ "$available_kb" -ge "$required_kb" ] || die insufficient_transaction_space
        done
    fi
    for name in $PRIVATE_NAMES; do
        regular "$STAGE/enrollment/$name" || die enrollment_missing
        secure_path "$STAGE/enrollment/$name"
        mode_is "$STAGE/enrollment/$name" -rw------- || die enrollment_permissions
    done
    if [ "$FIXTURE" != 1 ] || [ "${INTERCOM_PROCESS_FIXTURE:-0}" = 1 ]; then runtime_admit; fi
    # Public helpers must be exactly those admitted by the HA artifact catalog.
    if regular "$STATE/engine.sh"; then
        [ "$(sha "$STATE/engine.sh")" = "$(sha "$0")" ] || die engine_update_not_supported
    else
        atomic_copy "$STAGE/transaction.sh" "$STATE/engine.sh" 700
    fi
    if regular "$STATE/attention.wav"; then
        [ "$(sha "$STATE/attention.wav")" = "$(sha "$STAGE/attention.wav")" ] || die attention_update_not_supported
    else atomic_copy "$STAGE/attention.wav" "$STATE/attention.wav" 600; fi
    if regular "$BOOT"; then [ "$(sha "$BOOT")" = "$(sha "$STAGE/S98intercom-recover")" ] || die existing_recovery_hook;
    else [ ! -e "$BOOT" ] && [ ! -L "$BOOT" ] || die existing_recovery_hook; atomic_copy "$STAGE/S98intercom-recover" "$BOOT" 755; fi
    seam recovery_hook_installed
fi
if [ "$ACTION" != install ] && { [ "$FIXTURE" != 1 ] || [ "${INTERCOM_PROCESS_FIXTURE:-0}" = 1 ]; }; then runtime_admit; fi
snapshot_current "$OP"
if [ ! -e "$STATE/stock.ref" ]; then
    [ "$ACTION" = install ] || die not_installed
    atomic_text "$STATE/stock.ref" "$OP"
fi
if [ "$ACTION" = install ]; then
    awk '$2=="application" || $2=="S99ha-speaker" || $2=="bootstrap.sh"' "$STAGE/bundle.sha256" > "$STATE/proposed.sha256.new"
    validate_record "$STATE/proposed.sha256.new"
    mv -f "$STATE/proposed.sha256.new" "$STATE/proposed.sha256"
    for name in $PRIVATE_NAMES; do printf '%s  %s\n' "$(sha "$STAGE/enrollment/$name")" "$name"; done > "$STATE/proposed-private.sha256"
else
    case "$ACTION" in rollback_previous) read_ref previous.ref;; restore_stock|remove) read_ref stock.ref;; esac
    chosen=$ref
    cp "$STATE/snapshots/$chosen/pair.sha256" "$STATE/proposed.sha256"
    cp "$STATE/snapshots/$chosen/private.sha256" "$STATE/proposed-private.sha256"
fi
sync
atomic_text "$STATE/pending" "$OP"; seam journal_written
quiesce; seam voice_stopped
if [ "$ACTION" = install ]; then
    mkdir -p "$STATE/private"; chmod 700 "$STATE/private"
    for name in $PRIVATE_NAMES; do atomic_copy "$STAGE/enrollment/$name" "$STATE/private/$name" 600; done
    seam private_replaced
    atomic_copy "$STAGE/bootstrap.sh" "$HELPER" 700; seam helper_replaced
    atomic_copy "$STAGE/S99ha-speaker" "$INIT" 755; seam init_replaced
    atomic_copy "$STAGE/application" "$APP" 755; seam application_replaced
else apply_snapshot "$chosen"; fi
if ! adopt_start_verify; then
    recover_pending || die automatic_rollback_failed
    die candidate_start_failed_restored
fi
seam start_verified
# Snapshot verified result separately; immutable generation refs make crash recovery deterministic.
newid=$(printf '%s' "$OP" | sed 's/^./f/')
[ "$newid" != "$OP" ] || newid=$(printf '%s' "$OP" | sed 's/^./e/')
snapshot_current "$newid"
atomic_text "$STATE/previous.ref" "$OP"; seam previous_committed
atomic_text "$STATE/current.ref" "$newid"; seam current_committed
rm -f "$STATE/pending"; seam journal_cleared
if [ "$ACTION" = remove ]; then
    # Keep exact stock/previous evidence and private enrollment for inspectable cleanup.
    # Only our pinned boot hook is removed after successful stock verification.
    [ "$(sha "$BOOT")" = "$(sha "$STAGE/S98intercom-recover")" ] || die recovery_hook_changed
    rm "$BOOT"; sync
fi
emit completed VERIFIED
