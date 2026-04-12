#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"

mkdir -p "$OUT" "$SAMBA3_WORK"

{
    echo "Starting Samba 3 download workflow at $(date -u)"
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "SAMBA3_VERSION=$SAMBA3_VERSION"
    echo "SAMBA3_TARBALL_URL=$SAMBA3_TARBALL_URL"
    echo "SAMBA3_GIT_URL=$SAMBA3_GIT_URL"
    echo "SAMBA3_GIT_REF=$SAMBA3_GIT_REF"
    echo "SAMBA3_SRC_DIR=$SAMBA3_SRC_DIR"
    ARCHIVE_PATH="$SAMBA3_WORK/samba-${SAMBA3_VERSION}.tar.gz"

    rm -rf "$SAMBA3_SRC_DIR"
    curl -L "$SAMBA3_TARBALL_URL" -o "$ARCHIVE_PATH"
    tar -xzf "$ARCHIVE_PATH" -C "$SAMBA3_WORK"

    perl -0pi -e 's/if \\(defined\\(@\\$podl\\)\\) \\{/if (\\$podl) {/g' \
        "$SAMBA3_SRC_DIR/pidl/lib/Parse/Pidl/ODL.pm"
    perl -0pi -e 's/defined \\@\\$pidl/defined(\\$pidl)/g' \
        "$SAMBA3_SRC_DIR/pidl/pidl"

    ls -ld "$SAMBA3_SRC_DIR"
    tar -tzf "$ARCHIVE_PATH" | sed -n '1,5p'
    echo "Finished Samba 3 download workflow at $(date -u)"
} >"$SAMBA3_DOWNLOAD_LOG" 2>&1

printf 'Samba 3 download complete.\n'
printf 'Log: %s\n' "$SAMBA3_DOWNLOAD_LOG"
