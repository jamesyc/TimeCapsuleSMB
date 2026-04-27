#!/bin/sh
set -eu

SDK_FAMILY=netbsd4 NETBSD4_ABI=le exec "$(dirname "$0")/_download_sdk.sh" "$@"
