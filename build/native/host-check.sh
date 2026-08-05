#!/bin/sh
set -eu
build_dir=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
for target in mdns nbns service telemetry; do
    case "$target" in
        mdns|nbns) binary="$target-advertiser" ;;
        *) binary=$target ;;
    esac
    set --
    while IFS= read -r source; do set -- "$@" "$build_dir/$source"; done <"$build_dir/native/$target.sources"
    # Compile all four products with the production source lists. Target ELF/ABI
    # verification belongs to the VM build, not the macOS host compiler.
    cc -D_GNU_SOURCE -Wall -Wextra -Werror -Wno-sign-compare -Wno-unterminated-string-initialization "$@" -o "$work/$binary"
    "$work/$binary" --version
 done
