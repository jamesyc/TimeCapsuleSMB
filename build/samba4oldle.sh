#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)

export SDK_FAMILY=netbsd4
export DEVICE_FAMILY=old
export SAMBA_FAMILY=samba4
export NETBSD4_ABI=le

exec "$SCRIPT_DIR/_samba4.sh" "$@"
