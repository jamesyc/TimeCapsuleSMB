#!/bin/sh
set -eu

SDK_FAMILY=netbsd4 DEVICE_FAMILY=old NETBSD4_ABI=be \
    exec "$(dirname "$0")/hello.sh" "$@"
