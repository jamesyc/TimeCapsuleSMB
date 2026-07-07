#!/bin/sh
set -eu

SDK_FAMILY=netbsd4 NETBSD4_ABI=le DEVICE_FAMILY=old \
    exec "$(dirname "$0")/_rsync.sh" "$@"
