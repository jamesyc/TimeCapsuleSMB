#!/bin/sh
set -eu
build_dir=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
# The vendored Apple dns_sd stub touches sockaddr sa_len fields unless told
# the platform lacks them; Apple's own Linux build defines NOT_HAVE_SA_LEN
# (review finding 8). NetBSD and macOS have sa_len and must not get it.
stub_flags=
case "$(uname -s)" in
    Linux) stub_flags=-DNOT_HAVE_SA_LEN ;;
esac
trap 'rm -rf "$work"' EXIT HUP INT TERM
for target in discovery service telemetry; do
    target_flags=
    case "$target" in
        discovery) binary=discoveryd ;;
        service) binary=service; target_flags=-DTC_UNIFIED_SERVICE ;;
        *) binary=$target ;;
    esac
    set --
    while IFS= read -r source; do set -- "$@" "$build_dir/$source"; done <"$build_dir/native/$target.sources"
    # Compile all three products with the production source lists. Target ELF/ABI
    # verification belongs to the VM build, not the macOS host compiler. The
    # vendored Apple dns_sd stub is compiled unchanged (build/native/dnssd/README.md),
    # hence -Wno-unused-but-set-variable.
    # shellcheck disable=SC2086
    cc -D_GNU_SOURCE -D_DNS_SD_LIBDISPATCH=0 $stub_flags $target_flags -Wall -Wextra -Werror -Wno-sign-compare -Wno-unterminated-string-initialization -Wno-unused-but-set-variable "$@" -o "$work/$binary"
    "$work/$binary" --version
 done
