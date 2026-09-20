#!/bin/sh
set -eu

PATH=/bin:/sbin:/usr/bin:/usr/sbin
RAM_ROOT=/mnt/Memory/samba4
LOCKS_ROOT=/mnt/Locks

[ "$#" -eq 0 ] || { echo 'boot.sh takes no arguments' >&2; exit 2; }

# Only one-time platform preparation belongs in shell. These operations must
# preserve an already running installation: the C manager takes its singleton
# lock and reconciles old process groups before clearing any Samba lock files.
mkdir -p "$RAM_ROOT/sbin" "$RAM_ROOT/etc" "$RAM_ROOT/var" "$RAM_ROOT/private" \
    "$RAM_ROOT/var/run/ncalrpc" "$RAM_ROOT/var/cores" || exit 1
chmod 755 "$RAM_ROOT" "$RAM_ROOT/sbin" "$RAM_ROOT/etc" "$RAM_ROOT/var" \
    "$RAM_ROOT/var/run" "$RAM_ROOT/var/run/ncalrpc" || exit 1
chmod 700 "$RAM_ROOT/private" "$RAM_ROOT/var/cores" || exit 1
exec >>"$RAM_ROOT/var/rc.local.log" 2>&1

tc_prepare_locks() {
    mkdir -p "$LOCKS_ROOT" || return 1
    tc_mounts=$(/sbin/mount) || return 1
    while IFS= read -r tc_mount; do
        case "$tc_mount" in *" on $LOCKS_ROOT "*) return 0 ;; esac
    done <<MOUNTS
$tc_mounts
MOUNTS
    tc_release=$(/usr/bin/uname -r) || return 1
    case "$tc_release" in
        6.*|7.*)
            # Preserve the existing NetBSD 6 plain-directory fallback. Never
            # mount over lock files left by a still-running fallback instance.
            for tc_entry in "$LOCKS_ROOT"/* "$LOCKS_ROOT"/.[!.]* "$LOCKS_ROOT"/..?*; do
                if [ -e "$tc_entry" ] || [ -L "$tc_entry" ]; then return 0; fi
            done
            /sbin/mount_tmpfs -s 4m tmpfs "$LOCKS_ROOT" && return 0
            echo 'boot: Locks tmpfs unavailable; using existing directory'
            return 0
            ;;
        *)
            # NetBSD 4 has a tiny root RAM filesystem. Its separate mfs uses
            # 512-byte sectors: 8192 sectors is 4 MiB; rootfs fallback is unsafe.
            /sbin/mount_mfs -s 8192 swap "$LOCKS_ROOT" || return 1
            ;;
    esac
}

if ! tc_prepare_locks; then
    echo 'boot: Locks filesystem unavailable'
    exit 1
fi

tc_bufcache=$(/sbin/sysctl -n vm.bufcache 2>/dev/null) || tc_bufcache=
if [ -n "$tc_bufcache" ] && [ "$tc_bufcache" != 5 ]; then
    /sbin/sysctl -w vm.bufcache=5 || echo 'boot: could not tune vm.bufcache'
fi

mkdir -p /root || exit 1
for tc_prefix in /root/tc-netbsd7 /root/tc-netbsd4 /root/tc-netbsd4le /root/tc-netbsd4be; do
    # Already prepared prefixes can be in use by live Samba workers. Fresh
    # deployments remove obsolete software before boot; boot never unlinks it.
    if [ -e "$tc_prefix" ] || [ -L "$tc_prefix" ]; then continue; fi
    ln -s "$RAM_ROOT" "$tc_prefix" || exit 1
done

echo 'boot: starting native manager'
exec /mnt/Flash/service manager >>"$RAM_ROOT/var/runtime.log" 2>&1
