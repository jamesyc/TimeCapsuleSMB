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
set --
while IFS= read -r source; do set -- "$@" "$build_dir/$source"; done <"$build_dir/native/service.sources"
# Compile the one production image. Target ELF/ABI verification belongs to the
# VM build, not the host compiler. The vendored Apple dns_sd stub is unchanged.
# shellcheck disable=SC2086
cc -D_GNU_SOURCE -DTC_SERVICE_MULTICALL -D_DNS_SD_LIBDISPATCH=0 $stub_flags \
    -Wall -Wextra -Werror -Wno-sign-compare -Wno-unterminated-string-initialization \
    -Wno-unused-but-set-variable "$@" -o "$work/service"
# Run each entry point on the host: the top-level helper on a known NT hash
# ("password"), then the discovery and telemetry roles.
test "$(printf password | "$work/service" --print-nt-hash-from-stdin)" = 8846F7EAEE8FB117AD06BDD830B7586C
"$work/service" discovery --help 2>/dev/null
"$work/service" telemetry --version
