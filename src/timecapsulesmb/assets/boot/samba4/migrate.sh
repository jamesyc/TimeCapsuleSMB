#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin

if [ "$#" -lt 8 ]; then
    echo "usage: migrate.sh phase tdb metadata migrator ram parent log root..." >&2
    exit 2
fi

migration_phase=$1
migration_tdb=$2
migration_metadata=$3
migration_binary=$4
migration_ram=$5
migration_parent=$6
migration_log=$7
shift 7

migration_runner=
migration_done=0
migration_status=127

migration_cleanup() {
    if [ -n "$migration_runner" ]; then
        /bin/kill -TERM "$migration_runner" 2>/dev/null || true
        /bin/kill -KILL "$migration_runner" 2>/dev/null || true
        migration_runner=
    fi
    # Match only the RAM migrator, not parent shells whose 'sh -c' command
    # line merely contains the RAM path as an argument (that suicide showed
    # as returncode -9 in Linux CI). A static migrator binary has argv[0] at
    # the RAM path (^ram); a shebang helper runs as '/bin/sh RAM ...'. A
    # 'sh -c' parent has '-c' after '/bin/sh ', so neither alternative matches
    # it, and the wrapper itself runs migrate.sh, not the RAM path.
    /usr/bin/pkill -KILL -f "^$migration_ram([[:space:]]|$)" >/dev/null 2>&1 || true
    /usr/bin/pkill -KILL -f "^/bin/sh $migration_ram([[:space:]]|$)" >/dev/null 2>&1 || true
    /bin/rm -f "$migration_ram"
}

migration_signal() {
    migration_signal_number=$1
    echo "migration wrapper received signal=$migration_signal_number" >>"$migration_log"
    migration_cleanup
    exit 1
}

migration_complete_success() {
    migration_done=1
    migration_status=0
}

migration_complete_failure() {
    migration_done=1
    migration_status=1
}

trap 'migration_signal 1' 1
trap 'migration_signal 2' 2
trap 'migration_signal 15' 15
# Use symbolic USR1/USR2 names: numeric values differ between BSD (30/31 on
# NetBSD/macOS) and Linux (10/12), so numbers break cross-platform traps.
trap 'migration_complete_success' USR1
trap 'migration_complete_failure' USR2

/bin/cp "$migration_binary" "$migration_ram" || {
    migration_cleanup
    exit 1
}
/bin/chmod 755 "$migration_ram" || {
    migration_cleanup
    exit 1
}

echo "migration wrapper start phase=$migration_phase metadata=$migration_metadata tdb=$migration_tdb roots=$#" >>"$migration_log"

migration_run() {
    migration_runner_parent=$1
    shift
    if "$migration_ram" "$migration_phase" "$migration_tdb" "$migration_metadata" "$@" >>"$migration_log" 2>&1; then
        migration_status=0
    else
        migration_status=1
    fi
    if [ "$migration_status" = 0 ]; then
        kill -USR1 "$migration_runner_parent" 2>/dev/null || true
    else
        kill -USR2 "$migration_runner_parent" 2>/dev/null || true
    fi
    exit 0
}

migration_wrapper_pid=$$
migration_run "$migration_wrapper_pid" "$@" &
migration_runner=$!
while [ "$migration_done" -eq 0 ] && /bin/kill -0 "$migration_runner" 2>/dev/null; do
    sleep 1 || :
done

if [ "$migration_done" -eq 0 ]; then
    migration_status=127
fi
migration_runner=
/bin/rm -f "$migration_ram"
echo "migration wrapper finish phase=$migration_phase status=$migration_status" >>"$migration_log"
if [ "$migration_status" -ne 0 ]; then
    kill -USR2 "$migration_parent" 2>/dev/null || true
else
    kill -USR1 "$migration_parent" 2>/dev/null || true
fi
exit "$migration_status"
