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

LOCAL_BIN=$1
shift

REMOTE_DIR="/tmp/tc-samba4-probes"
REMOTE_BIN="$REMOTE_DIR/$(basename "$LOCAL_BIN").$$"
REMOTE_CMD=$(quote_arg "$REMOTE_BIN")

for arg in "$@"; do
    REMOTE_CMD="$REMOTE_CMD $(quote_arg "$arg")"
done

tc_ssh "$TC_HOST" "mkdir -p \"$REMOTE_DIR\""
tc_scp "$LOCAL_BIN" "$TC_HOST:$REMOTE_BIN" >/dev/null

status=0
if ! tc_ssh "$TC_HOST" "chmod +x \"$REMOTE_BIN\" && exec $REMOTE_CMD"; then
    status=$?
fi

tc_ssh "$TC_HOST" "rm -f \"$REMOTE_BIN\"" >/dev/null 2>&1 || true
exit "$status"
