#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
. "$SCRIPT_DIR/env.sh"

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <replay-file> [samba4.sh args...]" >&2
    exit 2
fi

replay_in=$1
shift

case "$replay_in" in
    /*)
        ;;
    *)
        replay_in="$SAMBA4_COMPAT_DIR/$replay_in"
        ;;
esac

printf 'Replaying Samba cross-exec results from %s\n' "$replay_in"
exec env \
    SAMBA4_CROSS_EXEC_MODE=replay \
    SAMBA4_COMPAT_REPLAY_IN="$replay_in" \
    sh "$SCRIPT_DIR/samba4.sh" "$@"
