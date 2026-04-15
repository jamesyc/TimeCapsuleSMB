#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <binary> [args...]" >&2
    exit 2
fi

quote_arg() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

LOCAL_CMD=$1
shift

if [ -f "$LOCAL_CMD" ]; then
    REMOTE_DIR="${CROSS_EXEC_REMOTE_DIR:-/tmp/tc-samba4-probes}"
    REMOTE_BIN="$REMOTE_DIR/$(basename "$LOCAL_CMD").$$"
    REMOTE_CMD=$(quote_arg "$REMOTE_BIN")
    REMOTE_BIN_Q=$(quote_arg "$REMOTE_BIN")
    REMOTE_DIR_Q=$(quote_arg "$REMOTE_DIR")

    for arg in "$@"; do
        REMOTE_CMD="$REMOTE_CMD $(quote_arg "$arg")"
    done

    # NetBSD4 does not always have the disk mounted at /Volumes/dk2. If that
    # path is only a directory on /, configure probes can fill the 10MB root
    # filesystem. Refuse /Volumes scratch space unless df shows it is a mounted
    # filesystem distinct from /.
    case "$REMOTE_DIR" in
        /Volumes/*)
            tc_ssh "$TC_HOST" "df -k $REMOTE_DIR_Q 2>/dev/null | sed -n '2p' | sed -n '/[[:space:]]\\/Volumes\\//p'" </dev/null | grep . >/dev/null || {
                echo "Refusing CROSS_EXEC_REMOTE_DIR=$REMOTE_DIR because it is not a mounted /Volumes filesystem." >&2
                exit 1
            }
            ;;
    esac

    tc_ssh "$TC_HOST" "mkdir -p $REMOTE_DIR_Q" </dev/null
    status=0
    # The NetBSD 4 Time Capsule does not ship scp, so cross-exec has to upload
    # probe binaries over ssh. Keep this as an explicit pipeline: it proved more
    # reliable through the bastion than redirecting the ssh wrapper's stdin.
    cat "$LOCAL_CMD" | tc_ssh "$TC_HOST" "cat > $REMOTE_BIN_Q" || status=$?
    if [ "$status" -ne 0 ]; then
        tc_ssh "$TC_HOST" "rm -f $REMOTE_BIN_Q" </dev/null || true
        exit "$status"
    fi

    status=0
    tc_ssh "$TC_HOST" "chmod +x \"$REMOTE_BIN\" && $REMOTE_CMD; rc=\$?; rm -f $REMOTE_BIN_Q; exit \$rc" </dev/null || status=$?
    exit "$status"
fi

REMOTE_CMD=$(quote_arg "$LOCAL_CMD")
for arg in "$@"; do
    REMOTE_CMD="$REMOTE_CMD $(quote_arg "$arg")"
done

tc_ssh "$TC_HOST" "exec $REMOTE_CMD" </dev/null
