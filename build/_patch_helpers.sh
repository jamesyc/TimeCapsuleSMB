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

    if ! awk -v pattern="$pattern" 'index($0, pattern) { found = 1 } END { exit(found ? 0 : 1) }' "$target"; then
        patch_fail "$desc: expected text not found in $target"
    fi
}

patch_apply_checked() {
    desc="$1"
    patch_file="$2"
    workdir="$3"

    if [ ! -f "$patch_file" ]; then
        patch_fail "$desc: missing patch file $patch_file"
    fi
    if [ ! -d "$workdir" ]; then
        patch_fail "$desc: missing workdir $workdir"
    fi

    git -C "$workdir" apply --check "$patch_file" ||
        patch_fail "$desc: patch does not apply cleanly"
    git -C "$workdir" apply "$patch_file" ||
        patch_fail "$desc: patch apply failed"
}

patch_apply_series() {
    desc_prefix="$1"
    series_file="$2"
    workdir="$3"
    series_dir="$(dirname "$series_file")"
    series_lineno=0

    if [ ! -f "$series_file" ]; then
        patch_fail "$desc_prefix: missing patch series $series_file"
    fi

    while IFS= read -r series_line || [ -n "$series_line" ]; do
        series_lineno=$((series_lineno + 1))
        case "$series_line" in
            ''|\#*)
                continue
                ;;
        esac

        patch_name="${series_line%%|*}"
        if [ "$patch_name" = "$series_line" ]; then
            patch_desc="$patch_name"
        else
            patch_desc="${series_line#*|}"
        fi

        patch_apply_checked "$desc_prefix $patch_desc" \
            "$series_dir/$patch_name" \
            "$workdir" ||
            patch_fail "$desc_prefix: failed at $series_file:$series_lineno"
    done <"$series_file"
}
