#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

# Run the Samba 4.x appliance lanes without touching the prebuilt NetBSD
# toolchains. Each lane refreshes its own patched Samba source tree before
# building so patch ordering and generated Waf cache changes stay lane-local.
#
# The lanes share nothing but the read-only checkout and the ccache directory
# (build/env.sh), which ccache locks, so they run at once: each lane's
# configure is a single process, and three of them overlap instead of queuing.
# Name lanes (6, 4le, 4be) to build only those; the default is all three.
# Each lane writes its own logs (see build/env.sh); this prints one line per
# step and lane, and fails if any lane failed. Ctrl-C stops every lane.
run_lane() {
    label=$1
    download=$2
    build=$3

    echo "==> $label: download"
    if ! "$download"; then
        echo "==> $label: download FAILED"
        return 1
    fi
    echo "==> $label: build"
    if ! "$build"; then
        echo "==> $label: build FAILED"
        return 1
    fi
    echo "==> $label: done"
}

[ "$#" -gt 0 ] || set -- 6 4le 4be
# Check every name before starting any lane, so a typo starts nothing. A lane
# named twice would run two builds in its one tree at once.
named=
for lane in "$@"; do
    case "$lane" in
        6|4le|4be) ;;
        *) echo "unknown lane $lane (use 6, 4le or 4be)" >&2; exit 2 ;;
    esac
    case " $named " in
        *" $lane "*) echo "lane $lane is named twice" >&2; exit 2 ;;
    esac
    named="$named $lane"
done
# One pkgin run before the lanes, not three at once (_samba4x_host_packages.sh).
./build/_samba4x_host_packages.sh
# The lanes run in the background, where a non-interactive shell ignores
# SIGINT, and waf keeps ignoring it, so Ctrl-C would stop only this script and
# leave three root builds writing the lane trees. On Ctrl-C or TERM, stop
# every process below this script: the lanes, their download and build
# scripts, waf and the compilers. They are all collected before any is
# signalled, so none is reparented first. The process group would be simpler,
# but a caller that started this script without job control shares it.
stop_lanes() {
    trap - INT TERM
    below=$(ps -A -o pid= -o ppid= | awk -v top=$$ '
        { parent[$1] = $2 }
        END {
            for (p in parent) {
                q = p
                while (q in parent && q != top && q > 1) q = parent[q]
                if (q == top && p != top) print p
            }
        }')
    [ -z "$below" ] || kill -TERM $below 2>/dev/null || true
    exit "$1"
}
trap 'stop_lanes 130' INT
trap 'stop_lanes 143' TERM
pids=
for lane in "$@"; do
    case "$lane" in
        6) run_lane "NetBSD 6/7" ./build/downloadsamba4x.sh ./build/samba4x.sh & ;;
        4le) run_lane "NetBSD 4 LE" ./build/downloadsamba4xoldle.sh ./build/samba4xoldle.sh & ;;
        4be) run_lane "NetBSD 4 BE" ./build/downloadsamba4xoldbe.sh ./build/samba4xoldbe.sh & ;;
    esac
    pids="$pids $!"
done

rc=0
for pid in $pids; do
    wait "$pid" || rc=1
done
exit "$rc"
