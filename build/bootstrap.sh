#!/bin/sh
set -eu

SDK_FAMILY=netbsd7 exec "$(dirname "$0")/_bootstrap_sdk.sh" "$@"
