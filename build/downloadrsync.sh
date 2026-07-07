#!/bin/sh
set -eu

SDK_FAMILY=netbsd7 DEVICE_FAMILY=new \
    exec "$(dirname "$0")/_downloadrsync.sh" "$@"
