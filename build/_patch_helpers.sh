#!/bin/sh

patch_fail() {
    echo "PATCH FAILED: $*" >&2
    exit 1
}

# Refuse a downloaded or cached source archive whose bytes differ from the
# pinned hash in env.sh. Callers capture stdout, so report on stderr only.
verify_sha256() {
    file="$1"
    expected="$2"

    actual="$(sha256 -q "$file")" || return 1
    if [ "$actual" != "$expected" ]; then
        echo "SHA-256 mismatch for $file: expected $expected, got $actual" >&2
        return 1
    fi
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

    # Pass the literal search text through the environment. awk -v parses
    # escape sequences in assigned strings, so text like "\n" would become a
    # newline before index() sees it.
    if ! PATCH_REQUIRE_FIXED_PATTERN="$pattern" awk '
        BEGIN { pattern = ENVIRON["PATCH_REQUIRE_FIXED_PATTERN"] }
        index($0, pattern) { found = 1 }
        END { exit(found ? 0 : 1) }
    ' "$target"; then
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

# Source files that do not exist upstream live whole under the series'
# overlay/ directory, mirroring the source tree, so they get ordinary diffs
# instead of patches to patches. An overlay only adds files: refuse to
# overwrite anything already in the tree. Check every path before copying so
# a refusal leaves the tree untouched. Dotfiles (such as a Finder .DS_Store)
# are never source.
patch_copy_overlay() {
    desc="$1"
    overlay_dir="$2"
    workdir="$3"

    if [ ! -d "$overlay_dir" ]; then
        return 0
    fi
    overlay_files="$(cd "$overlay_dir" && find . -type f ! -name '.*' | sort)" ||
        patch_fail "$desc: cannot list overlay $overlay_dir"
    # One path per line: split on newlines only, and never glob.
    overlay_saved_ifs=$IFS
    IFS='
'
    set -f
    for overlay_file in $overlay_files; do
        if [ -e "$workdir/$overlay_file" ]; then
            patch_fail "$desc: overlay file ${overlay_file#./} already exists in $workdir"
        fi
    done
    for overlay_file in $overlay_files; do
        mkdir -p "$workdir/$(dirname "$overlay_file")" ||
            patch_fail "$desc: cannot create directory for ${overlay_file#./}"
        cp "$overlay_dir/$overlay_file" "$workdir/$overlay_file" ||
            patch_fail "$desc: cannot copy overlay file ${overlay_file#./}"
    done
    set +f
    IFS=$overlay_saved_ifs
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
    patch_copy_overlay "$desc_prefix" "$series_dir/overlay" "$workdir"

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
