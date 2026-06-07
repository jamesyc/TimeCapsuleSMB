#!/bin/sh
set -eu

. "$(dirname "$0")/env.sh"
. "$(dirname "$0")/_patch_helpers.sh"

PATCH_DIR="$(CDPATH= cd "$(dirname "$0")/patches/samba4x" && pwd)"

mkdir -p "$OUT" "$SAMBA4X_WORK"

{
    echo "Starting Samba 4.x download workflow at $(date -u)"
    echo "SDK_FAMILY=$SDK_FAMILY"
    echo "DEVICE_FAMILY=$DEVICE_FAMILY"
    echo "NETBSD4_ABI=$NETBSD4_ABI"
    echo "SAMBA4X_VERSION=$SAMBA4X_VERSION"
    echo "SAMBA4X_GIT_URL=$SAMBA4X_GIT_URL"
    echo "SAMBA4X_GIT_REF=$SAMBA4X_GIT_REF"
    echo "SAMBA4X_SRC_DIR=$SAMBA4X_SRC_DIR"

    echo "Installing Samba 4.x host build tools on the VM."
    for pkg in bison p5-Parse-Yapp; do
        if pkg_info "$pkg" >/dev/null 2>&1; then
            echo "$pkg is already installed on the VM; skipping pkgin install."
        else
            /usr/pkg/bin/pkgin -4 -y install "$pkg"
        fi
    done

    if [ -d "$SAMBA4X_SRC_DIR/.git" ]; then
        printf 'Refreshing existing git checkout at %s\n' "$SAMBA4X_SRC_DIR"
        git -C "$SAMBA4X_SRC_DIR" fetch --depth 1 origin "$SAMBA4X_GIT_REF"
        # The downloader applies the appliance patch series in-place, so a
        # previously prepared Samba tree is expected to be dirty. Reset before
        # switching refs; otherwise git refuses the checkout and leaves the
        # build lane pinned to the old source.
        git -C "$SAMBA4X_SRC_DIR" reset --hard HEAD
        git -C "$SAMBA4X_SRC_DIR" checkout -B "$SAMBA4X_GIT_REF" "FETCH_HEAD"
        git -C "$SAMBA4X_SRC_DIR" reset --hard "FETCH_HEAD"
    elif [ -d "$SAMBA4X_SRC_DIR" ]; then
        printf 'Removing existing non-git Samba source tree at %s\n' "$SAMBA4X_SRC_DIR"
        rm -rf "$SAMBA4X_SRC_DIR"
        git clone --depth 1 --branch "$SAMBA4X_GIT_REF" "$SAMBA4X_GIT_URL" "$SAMBA4X_SRC_DIR"
    else
        git clone --depth 1 --branch "$SAMBA4X_GIT_REF" "$SAMBA4X_GIT_URL" "$SAMBA4X_SRC_DIR"
    fi

    # The ordered patch series is grouped by purpose in build/patches/samba4x.
    # Keep comments in that series and in the patch hunks themselves so the
    # downloader stays readable while patch order remains explicit.
    patch_apply_series "Samba 4.x" "$PATCH_DIR/series" "$SAMBA4X_SRC_DIR"

    git -C "$SAMBA4X_SRC_DIR" rev-parse --short HEAD
    git -C "$SAMBA4X_SRC_DIR" log -1 --format='%H%n%cd%n%s' --date=iso
    echo "Finished Samba 4.x download workflow at $(date -u)"
} >"$SAMBA4X_DOWNLOAD_LOG" 2>&1

printf 'Samba 4.x download complete.\n'
printf 'Log: %s\n' "$SAMBA4X_DOWNLOAD_LOG"
