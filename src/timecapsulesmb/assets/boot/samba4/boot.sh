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

# ACPd starts two Apple daemons that would take over from ours, so boot.sh
# replaces each with a guard that exits at once. /sbin and /usr/libexec are on
# the RAM root, recreated at every boot. The hard link from /usr/libexec keeps
# the process named after the daemon (the multi-call binary dispatches on
# argv[0]'s basename); renaming the guard into place never writes through that
# shared inode. Without the link, /sbin keeps Apple's binary.
tc_guard() {
    ln -f /sbin/ACPd "/usr/libexec/$1" &&
        cat >"/sbin/.$1.$$" &&
        chmod 555 "/sbin/.$1.$$" &&
        mv -f "/sbin/.$1.$$" "/sbin/$1" &&
        return 0
    rm -f "/sbin/.$1.$$"
    echo "boot: $1 guard unavailable; ACPd may start a second $1"
}
# ACPd starts diskd at boot and whenever it applies a sharing setting. The
# manager already runs diskd on loopback. ACPd keeps one table of diskd.* RPC
# names: the second diskd fails to register, and when it exits ACPd deletes the
# names it tried, which are the manager diskd's. After a few starts
# diskd.useVolume is gone until reboot, so deploy and disk activation fail.
# Only the manager's loopback diskd may start.
tc_guard diskd <<'EOF'
#!/bin/sh
# TimeCapsuleSMB diskd guard: see /mnt/Flash/boot.sh.
if [ "${1:-}" = -i ] && [ "${2:-}" = lo0 ]; then
    exec /usr/libexec/diskd "$@"
fi
{ echo "$(/bin/date '+%Y-%m-%d %H:%M:%S') diskd guard: ignored ACPd start: $*" \
    >>/mnt/Memory/samba4/var/runtime.log; } 2>/dev/null
exit 0
EOF
# ACPd starts wcifsnd, without arguments, at boot and on every address change,
# and once more about 25 s later if it exits. A second wcifsnd takes UDP
# 137/138/922 from the one discovery owns, whose name registrations then fail
# until the manager stops the other. Discovery starts /usr/libexec/wcifsnd
# (/sbin/wcifsnd when that link is missing); every start of /sbin/wcifsnd is
# ACPd's.
tc_guard wcifsnd <<'EOF'
#!/bin/sh
# TimeCapsuleSMB wcifsnd guard: see /mnt/Flash/boot.sh.
{ echo "$(/bin/date '+%Y-%m-%d %H:%M:%S') wcifsnd guard: ignored ACPd start" \
    >>/mnt/Memory/samba4/var/runtime.log; } 2>/dev/null
exit 0
EOF

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

# Keep Apple's buffer cache settings (vm.bufcache 15). A smaller cache leaves
# NetBSD's getnewbuf() fewer buffers to recycle, and it can then sleep forever
# in needbuf (kern/60584), which stalled Time Machine backups (PR 353).

mkdir -p /root || exit 1
for tc_prefix in /root/tc-netbsd7 /root/tc-netbsd4 /root/tc-netbsd4le /root/tc-netbsd4be; do
    # Already prepared prefixes can be in use by live Samba workers. Fresh
    # deployments remove obsolete software before boot; boot never unlinks it.
    if [ -e "$tc_prefix" ] || [ -L "$tc_prefix" ]; then continue; fi
    ln -s "$RAM_ROOT" "$tc_prefix" || exit 1
done

echo 'boot: starting native manager'
exec /mnt/Flash/service manager >>"$RAM_ROOT/var/runtime.log" 2>&1
