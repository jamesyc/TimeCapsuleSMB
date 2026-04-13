#!/bin/sh
set -eu

SDK_FAMILY=netbsd4 exec "$(dirname "$0")/nbns.sh" "$@"
