#!/bin/sh
set -eu

SDK_FAMILY=netbsd4 DEVICE_FAMILY=old SAMBA_FAMILY=samba4x NETBSD4_ABI=le \
    exec "$(dirname "$0")/_downloadsamba4x.sh" "$@"
