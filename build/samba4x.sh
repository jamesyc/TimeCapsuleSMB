#!/bin/sh
set -eu

SDK_FAMILY=netbsd7 DEVICE_FAMILY=new SAMBA_FAMILY=samba4x \
    exec "$(dirname "$0")/_samba4x.sh" "$@"
