#!/bin/sh
set -eu

SDK_FAMILY=netbsd7 DEVICE_FAMILY=new SAMBA_FAMILY=samba4 \
    exec "$(dirname "$0")/_downloadsamba4.sh" "$@"
