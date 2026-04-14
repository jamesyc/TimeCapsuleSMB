#!/bin/sh
set -eu

SDK_FAMILY=netbsd4 DEVICE_FAMILY=old SAMBA_FAMILY=samba4 \
    exec "$(dirname "$0")/_downloadsamba4.sh" "$@"
