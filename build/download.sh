#!/bin/sh
set -eu

SDK_FAMILY=netbsd7 exec "$(dirname "$0")/_download_sdk.sh" "$@"
