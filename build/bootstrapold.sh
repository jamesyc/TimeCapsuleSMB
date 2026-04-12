#!/bin/sh
set -eu

BUILD_TARGET=netbsd4 exec "$(dirname "$0")/bootstrap.sh" "$@"
