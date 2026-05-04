#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

# Run the three Samba 4.x appliance lanes without touching the prebuilt
# NetBSD toolchains. Each lane refreshes the patched Samba source tree before
# building so patch ordering and generated Waf cache changes stay lane-local.
./build/downloadsamba4x.sh && ./build/samba4x.sh
./build/downloadsamba4xoldle.sh && ./build/samba4xoldle.sh
./build/downloadsamba4xoldbe.sh && ./build/samba4xoldbe.sh
