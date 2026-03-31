# I ssh'd into an Apple Time Capsule (NetBSD 6.0 evbarm) using ssh -oHostKeyAlgorithms=+ssh-dss.

# Environment is super minimal (tiny mdroot, tiny flash, 16 MB tmpfs). No sftp-server, no compiler, limited tools. 256MB ram. The 2 TB disk is mounted at /Volumes/dk2.

# The Apple Time Capsule only supports AFP/SMBv1; we need to expose the disk over a modern protocol to use for Time Machine. MacOS is dropping support for AFP/SMBv1 for macos 27. It has 256 MB RAM, so anything we run must be small and staged on /Volumes/dk2.

# A bad option: Bridge elsewhere (raspberry pi): mount AFP/SSH on another box and re-export via Samba 4 + fruit (practical) but it doesn't actually work, Time Machine crashes after running for a few mins. 

# Static SMB server on-box: discussed as impractical with Samba due to deps/size... but fuck it, we're doing it for fun anyways.  

# Samba version reality: vfs_fruit is not in Samba 3.6 which is lightweight; appears in Samba 4.x (AAPL ext in 4.2; Time Machine switch in 4.8). So if you want Apple semantics/Time Machine over SMB, you need Samba ≥4.8.

# Build strategy:  
# Use a UTM VM: NetBSD 9/10 (aarch64) for speed and sane tools.
# From that VM, cross-compile for NetBSD 6/evbarm using NetBSD’s build.sh:
# Build tools and distribution to get TOOLDIR and DESTDIR (sysroot).
# Cross-build and stage GMP → nettle → GnuTLS into ~/tc-stage (your $PREFIX).
# Cross-build Samba 4.8.12 with Waf in file-server-only mode:
# Disable AD/DC, LDAP, winbind, cups, pam, quotas, ACLs.
# Build vfs_fruit, streams_xattr, catia.
# Prefer --nonshared-binary=smbd/smbd to bake Samba’s own libs into smbd.

# Install to $PREFIX/samba-min, copy $PREFIX to the Time Capsule under /Volumes/dk2/, and run:
# export LD_LIBRARY_PATH=/Volumes/dk2/lib
# /Volumes/dk2/samba-min/sbin/smbd -i -s /Volumes/dk2/samba-min/etc/smb.conf
# Suggested smb.conf uses SMB2 only and stacks catia, fruit, streams_xattr, with fruit:time machine = yes (on Samba 4.8).

# I set up the cross toolchain and successfully built GMP, nettle, GnuTLS into $PREFIX.

# THINGS I DID


git clone https://github.com/NetBSD/src /usr/src

cd /usr/src
./build.sh -U -O ~/arm-build -m evbarm -a earmv4 tools
./build.sh -U -u -O ~/arm-build -m evbarm -a earmv4 distribution

TOOLDIR="$(ls -d ~/arm-build/tooldir.*)"
DESTDIR="~/arm-build/destdir.evbarm"
TRIPLE="$(basename "$(ls "$TOOLDIR"/bin/*-netbsdelf-*gcc | head -n1)" | sed 's/-gcc$//')"

export PATH="$TOOLDIR/bin:$PATH"
export SYSROOT="$DESTDIR"
export CC="$TOOLDIR/bin/$TRIPLE-gcc --sysroot=$SYSROOT"
export CXX="$TOOLDIR/bin/$TRIPLE-g++ --sysroot=$SYSROOT"
export CPP="$TOOLDIR/bin/$TRIPLE-cpp --sysroot=$SYSROOT"
export AR="$TOOLDIR/bin/$TRIPLE-ar"
export RANLIB="$TOOLDIR/bin/$TRIPLE-ranlib"
export STRIP="$TOOLDIR/bin/$TRIPLE-strip"
export LD="$TOOLDIR/bin/$TRIPLE-ld --sysroot=$SYSROOT"

export PREFIX="$HOME/tc-stage"
mkdir -p "$PREFIX"
export PKG_CONFIG_DIR=
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$SYSROOT"







i am following https://e17i.github.io/articles-timecapsule-crossbuild/
but i am on netbsd 10.1

i did 
git clone https://github.com/NetBSD/src /usr/src


