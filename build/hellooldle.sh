#!/bin/sh
set -eu

SDK_FAMILY=netbsd4 DEVICE_FAMILY=old NETBSD4_ABI=le \
    exec "$(dirname "$0")/hello.sh" "$@"
