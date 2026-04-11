#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
. "$SCRIPT_DIR/env.sh"

replay_out=${1:-"$SAMBA4_COMPAT_DIR/compat-$(date +%Y%m%d-%H%M%S).replay"}
if [ "$#" -gt 0 ]; then
    shift
fi

case "$replay_out" in
    /*)
        ;;
    *)
        replay_out="$SAMBA4_COMPAT_DIR/$replay_out"
        ;;
esac

mkdir -p "$(dirname "$replay_out")"

printf 'Recording Samba cross-exec results to %s\n' "$replay_out"
exec env \
    SAMBA4_CROSS_EXEC_MODE=record \
    SAMBA4_COMPAT_REPLAY_OUT="$replay_out" \
    sh "$SCRIPT_DIR/samba4.sh" "$@"
