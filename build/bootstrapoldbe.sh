#!/bin/sh
set -eu

SDK_FAMILY=netbsd4 NETBSD4_ABI=be exec "$(dirname "$0")/_bootstrap_sdk.sh" "$@"
