#!/bin/sh

patch_fail() {
    echo "PATCH FAILED: $*" >&2
    exit 1
}

patch_perl() {
    desc="$1"
    expr="$2"
    shift 2

    for target in "$@"; do
        if [ ! -f "$target" ]; then
            patch_fail "$desc: missing file $target"
        fi
        before="${target}.patch-before.$$"
        cp "$target" "$before"
        perl -0pi -e "$expr" "$target"
        if cmp -s "$before" "$target"; then
            rm -f "$before"
            patch_fail "$desc: no change in $target"
        fi
        rm -f "$before"
    done
}

patch_perl_any() {
    desc="$1"
    expr="$2"
    shift 2

    changed=0
    for target in "$@"; do
        if [ ! -f "$target" ]; then
            patch_fail "$desc: missing file $target"
        fi
        before="${target}.patch-before.$$"
        cp "$target" "$before"
    done

    command perl -0pi -e "$expr" "$@"

    for target in "$@"; do
        before="${target}.patch-before.$$"
        if ! cmp -s "$before" "$target"; then
            changed=1
        fi
        rm -f "$before"
    done
    if [ "$changed" -ne 1 ]; then
        patch_fail "$desc: no files changed in $*"
    fi
}

patch_replace_checked() {
    desc="$1"
    target="$2"
    replacement="$3"

    if [ ! -f "$target" ]; then
        rm -f "$replacement"
        patch_fail "$desc: missing file $target"
    fi
    if cmp -s "$target" "$replacement"; then
        rm -f "$replacement"
        patch_fail "$desc: no change in $target"
    fi
    mv "$replacement" "$target"
}

patch_require_grep() {
    desc="$1"
    pattern="$2"
    target="$3"

    if ! grep -q "$pattern" "$target"; then
        patch_fail "$desc: expected pattern not found in $target"
    fi
}

patch_require_fixed() {
    desc="$1"
    pattern="$2"
    target="$3"

    if ! grep -F -q "$pattern" "$target"; then
        patch_fail "$desc: expected text not found in $target"
    fi
}
