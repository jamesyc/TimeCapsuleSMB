#!/bin/sh
set -eu

# The VM packages the Samba 4.x lanes need: bison and Parse::Yapp for Samba's
# generated sources, and ccache, which build/_samba4x.sh puts in front of the
# lane compilers. Each lane's download step runs this, and so do
# build/samba4xall.sh and tests/samba/vm_build.py's job once before they start
# the lanes at once, so that three downloads never run pkgin together on a VM
# that lacks a package (pkgin locks its database). PKGIN only lets the build
# tests stand in a fake.
PKGIN="${PKGIN:-/usr/pkg/bin/pkgin}"
for pkg in bison p5-Parse-Yapp; do
    if pkg_info "$pkg" >/dev/null 2>&1; then
        echo "$pkg is already installed on the VM; skipping pkgin install."
    else
        "$PKGIN" -4 -y install "$pkg"
    fi
done
# ccache only makes the build faster: a lane without it compiles everything
# and says so in its log, so a failed install does not stop the build.
if pkg_info ccache >/dev/null 2>&1; then
    echo "ccache is already installed on the VM; skipping pkgin install."
elif ! "$PKGIN" -4 -y install ccache; then
    echo "warning: pkgin could not install ccache; the lanes build without it" >&2
fi
