#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

phase=${1:-}

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

case "$phase" in
    ''|tools|distribution) ;;
    *)
        echo "Usage: ./scripts/bootstrap.sh [tools|distribution]"
        exit 1
        ;;
esac

mkdir -p "$OUT" "$STAMPS"

clean_magic_cache() {
    magic_root="$SRC/external/bsd/file/dist/magic"
    if [ -d "$magic_root" ]; then
        find "$magic_root" -type f \( -name '*.mgc' -o -name 'magic.mgc' \) -delete
    fi
}

run_tools() {
    rm -rf "$OBJ" "$TOOLS"
    mkdir -p "$OBJ" "$TOOLS"
    rm -f "$TOOLS_STAMP" "$DIST_STAMP"
    clean_magic_cache
    cd "$SRC"
    env HOST_CC="$HOST_CC" HOST_CXX="$HOST_CXX" \
        HOST_CFLAGS="$HOST_CFLAGS" HOST_CXXFLAGS="$HOST_CXXFLAGS" \
        HOST_CPPFLAGS="$HOST_CPPFLAGS" \
        ./build.sh -U -m evbarm -a earmv4 \
        -V NO_PTHREADS="$NO_PTHREADS" \
        -O "$OBJ" -T "$TOOLS" tools \
        >"$TOOLS_LOG" 2>&1
    date -u >"$TOOLS_STAMP"
}

run_distribution() {
    if [ ! -f "$TOOLS_STAMP" ] && [ ! -x "$TOOLS/bin/nbmake" ]; then
        echo "Missing successful tools build."
        echo "Run ./scripts/bootstrap.sh tools first."
        exit 1
    fi
    mkdir -p "$OBJ" "$TOOLS"
    rm -f "$DIST_STAMP"
    clean_magic_cache
    cd "$SRC"
    env HOST_CC="$HOST_CC" HOST_CXX="$HOST_CXX" \
        HOST_CFLAGS="$HOST_CFLAGS" HOST_CXXFLAGS="$HOST_CXXFLAGS" \
        HOST_CPPFLAGS="$HOST_CPPFLAGS" \
        ./build.sh -U -m evbarm -a earmv4 \
        -V NO_PTHREADS="$NO_PTHREADS" \
        -O "$OBJ" -T "$TOOLS" distribution \
        >"$DIST_LOG" 2>&1
    date -u >"$DIST_STAMP"
}

case "$phase" in
    tools)
        run_tools
        ;;
    distribution)
        run_distribution
        ;;
    '')
        if [ -f "$TOOLS_STAMP" ]; then
            echo "Skipping tools; found $TOOLS_STAMP"
        else
            run_tools
        fi
        if [ -f "$DIST_STAMP" ]; then
            echo "Skipping distribution; found $DIST_STAMP"
        else
            run_distribution
        fi
        ;;
esac

printf 'Bootstrap complete.\n'
printf 'Tools log: %s\n' "$TOOLS_LOG"
printf 'Distribution log: %s\n' "$DIST_LOG"
