#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

# Run the three Samba 4.x appliance lanes without touching the prebuilt
# NetBSD toolchains. Each lane refreshes the patched Samba source tree before
# building so patch ordering and generated Waf cache changes stay lane-local.
run_lane() {
    label=$1
    download=$2
    build=$3

    echo "==> $label: download"
    "$download"
    echo "==> $label: build"
    "$build"
}

run_lane "NetBSD 6/7" ./build/downloadsamba4x.sh ./build/samba4x.sh
run_lane "NetBSD 4 LE" ./build/downloadsamba4xoldle.sh ./build/samba4xoldle.sh
run_lane "NetBSD 4 BE" ./build/downloadsamba4xoldbe.sh ./build/samba4xoldbe.sh
