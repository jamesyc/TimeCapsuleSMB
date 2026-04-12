#!/bin/sh
set -eu

SDK_FAMILY=netbsd4 DEVICE_FAMILY=old SAMBA_FAMILY=samba3 \
    exec "$(dirname "$0")/_samba3.sh" "$@"
