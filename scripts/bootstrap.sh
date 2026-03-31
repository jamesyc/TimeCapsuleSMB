#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

if [ ! -d "$SRC" ]; then
    echo "Missing source tree at $SRC"
    exit 1
fi

for tool in "$HOST_CC" "$HOST_CXX" curl tar gmake python2.7; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Missing required host tool: $tool"
        exit 1
    fi
done

mkdir -p "$OUT" "$OBJ" "$TOOLS"
rm -rf "$OBJ" "$TOOLS"
mkdir -p "$OBJ" "$TOOLS"
cd "$SRC"

env HOST_CC="$HOST_CC" HOST_CXX="$HOST_CXX" \
    HOST_CFLAGS="$HOST_CFLAGS" HOST_CXXFLAGS="$HOST_CXXFLAGS" \
    HOST_CPPFLAGS="$HOST_CPPFLAGS" \
    ./build.sh -U -m evbarm -a earmv4 \
    -V NO_PTHREADS="$NO_PTHREADS" \
    -O "$OBJ" -T "$TOOLS" tools \
    >"$TOOLS_LOG" 2>&1

env HOST_CC="$HOST_CC" HOST_CXX="$HOST_CXX" \
    HOST_CFLAGS="$HOST_CFLAGS" HOST_CXXFLAGS="$HOST_CXXFLAGS" \
    HOST_CPPFLAGS="$HOST_CPPFLAGS" \
    ./build.sh -U -m evbarm -a earmv4 \
    -V NO_PTHREADS="$NO_PTHREADS" \
    -O "$OBJ" -T "$TOOLS" distribution \
    >"$DIST_LOG" 2>&1

printf 'Bootstrap complete.\n'
printf 'Tools log: %s\n' "$TOOLS_LOG"
printf 'Distribution log: %s\n' "$DIST_LOG"
