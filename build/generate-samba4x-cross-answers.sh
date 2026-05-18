#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd "$(dirname "$0")" && pwd)"

export SDK_FAMILY=netbsd7
export DEVICE_FAMILY=new
export SAMBA_FAMILY=samba4x
export SAMBA4X_GENERATE_CROSS_ANSWERS=1

exec "$SCRIPT_DIR/_samba4x.sh" "$@"
