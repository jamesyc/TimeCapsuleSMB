#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd)"

export SDK_FAMILY=netbsd4
export DEVICE_FAMILY=old
export SAMBA_FAMILY=samba4x
export NETBSD4_ABI=be
export SAMBA4X_GENERATE_CROSS_ANSWERS=1

exec "$SCRIPT_DIR/_samba4x.sh" "$@"
