I ssh'd into an Apple Time Capsule (NetBSD 6.0 evbarm) using ssh -oHostKeyAlgorithms=+ssh-dss.

Environment is super minimal (tiny mdroot, tiny flash, 16 MB tmpfs). No sftp-server, no compiler, limited tools. 256MB ram. The 2 TB disk is mounted at /Volumes/dk2.

The Apple Time Capsule only supports AFP/SMBv1; we need to expose the disk over a modern protocol to use for Time Machine. MacOS is dropping support for AFP/SMBv1 for macos 27. It has 256 MB RAM, so anything we run must be small and staged on /Volumes/dk2.

A bad option: Bridge elsewhere (raspberry pi): mount AFP/SSH on another box and re-export via Samba 4 + fruit (practical) but it doesn't actually work, Time Machine crashes after running for a few mins. 

Static SMB server on-box: discussed as impractical with Samba due to deps/size... but fuck it, we're doing it for fun anyways.  

Samba version reality: vfs_fruit is not in Samba 3.6 which is lightweight; appears in Samba 4.x (AAPL ext in 4.2; Time Machine switch in 4.8). So if you want Apple semantics/Time Machine over SMB, you need Samba ≥4.8.

Build strategy:  
Use a UTM VM: NetBSD 9/10 (aarch64) for speed and sane tools.
From that VM, cross-compile for NetBSD 6/evbarm using NetBSD’s build.sh:
Build tools and distribution to get TOOLDIR and DESTDIR (sysroot).
Cross-build and stage GMP → nettle → GnuTLS into ~/tc-stage (your $PREFIX).
Cross-build Samba 4.8.12 with Waf in file-server-only mode:
Disable AD/DC, LDAP, winbind, cups, pam, quotas, ACLs.
Build vfs_fruit, streams_xattr, catia.
Prefer --nonshared-binary=smbd/smbd to bake Samba’s own libs into smbd.

Install to $PREFIX/samba-min, copy $PREFIX to the Time Capsule under /Volumes/dk2/, and run:
export LD_LIBRARY_PATH=/Volumes/dk2/lib
/Volumes/dk2/samba-min/sbin/smbd -i -s /Volumes/dk2/samba-min/etc/smb.conf
Suggested smb.conf uses SMB2 only and stacks catia, fruit, streams_xattr, with fruit:time machine = yes (on Samba 4.8).

I set up up the cross toolchain and successfully built GMP, nettle, GnuTLS into $PREFIX.

THINGS I DID

Fetch NetBSD 6 source tarball from archive (src.tgz, etc):
mkdir -p ~/netbsd6 && cd ~/netbsd6
curl https://archive.netbsd.org/pub/NetBSD-archive/NetBSD-6.0/source/sets/src.tgz -o src.tgz
tar -xzf src.tgz -C ~/netbsd6
cd /root/netbsd6/usr/src

Start building (use gcc 4 flags):
env HOST_CC=/usr/pkg/gcc12/bin/gcc HOST_CXX=/usr/pkg/gcc12/bin/g++ HOST_CFLAGS='-O -fcommon -fgnu89-inline' HOST_CXXFLAGS='-O -fcommon -fgnu89-inline' HOST_CPPFLAGS='-D__GNUC_GNU_INLINE__' ./build.sh -U -m evbarm tools
env HOST_CC=/usr/pkg/gcc12/bin/gcc HOST_CXX=/usr/pkg/gcc12/bin/g++ HOST_CFLAGS='-O -fcommon -fgnu89-inline' HOST_CXXFLAGS='-O -fcommon -fgnu89-inline' HOST_CPPFLAGS='-D__GNUC_GNU_INLINE__' ./build.sh -U -m evbarm distribution

# compile prereqs
# 0) Make sure env vars are set
cd /root/netbsd6/usr/src
TOOLDIR=$(ls -d $(pwd)/obj/tooldir.*)
echo $TOOLDIR
DESTDIR=$(ls -d $(pwd)/obj/destdir.evbarm)
echo $DESTDIR
TRIPLE=$(basename $(ls "$TOOLDIR"/bin/*-netbsdelf-*gcc | head -n1) | sed 's/-gcc$//')
echo "$TRIPLE"     # expect something like: arm--netbsdelf-eabi
export TOOLDIR DESTDIR TRIPLE
export SYSROOT="$DESTDIR"

# 1) Export tool vars 
export PATH="$TOOLDIR/bin:$PATH"
export CC="$TOOLDIR/bin/$TRIPLE-gcc --sysroot=$SYSROOT"
export CXX="$TOOLDIR/bin/$TRIPLE-g++ --sysroot=$SYSROOT"
export CPP="$TOOLDIR/bin/$TRIPLE-cpp --sysroot=$DESTDIR"
export AR="$TOOLDIR/bin/$TRIPLE-ar"
export RANLIB="$TOOLDIR/bin/$TRIPLE-ranlib"
export STRIP="$TOOLDIR/bin/$TRIPLE-strip"
export LD="$TOOLDIR/bin/$TRIPLE-ld --sysroot=$DESTDIR"
export CFLAGS="-Os"
export LDFLAGS="--sysroot=$DESTDIR"
export PREFIX="$HOME/tc-stage"
export CPPFLAGS="-I$PREFIX/include -I$SYSROOT/usr/include"
export LDFLAGS="-L$PREFIX/lib -L$SYSROOT/lib -L$SYSROOT/usr/lib"
export PKG_CONFIG_DIR=
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$SYSROOT"

# 2) Use a clean workspace OUTSIDE /usr/src
mkdir -p /root/tc-build 
cd /root/tc-build
PREFIX=$HOME/tc-stage                 # will mirror to /Volumes/dk2 on the TC 
mkdir -p "$PREFIX"

# 3) Build GMP → nettle → GnuTLS (installed into $PREFIX)  
# 3.1) GMP
cd /root/tc-build
curl -LO https://gmplib.org/download/gmp/gmp-6.2.1.tar.xz
tar -xf gmp-6.2.1.tar.xz 

cd /root/tc-build/gmp-6.2.1
gmake distclean 2>/dev/null || true
./configure --build="$(sh config.guess)" --host="$TRIPLE" --prefix="$PREFIX" \
        --disable-nls --enable-shared --disable-static
gmake -j"$(sysctl -n hw.ncpu)" && gmake install

# 3.2) nettle
cd /root/tc-build
curl -LO https://ftp.gnu.org/gnu/nettle/nettle-3.4.1.tar.gz
tar -xzf nettle-3.4.1.tar.gz 

cd /root/tc-build/nettle-3.4.1
gmake distclean 2>/dev/null || true
CFLAGS="-Os -std=gnu99" CPPFLAGS="-I$PREFIX/include" LDFLAGS="-L$PREFIX/lib" \
    ./configure --build="$(sh config.guess)" --host="$TRIPLE" --prefix="$PREFIX" \
    --enable-shared --disable-static --disable-openssl --disable-documentation
gmake -j"$(sysctl -n hw.ncpu)"
env -u DESTDIR gmake install 

# 3.3) GnuTLS
cd /root/tc-build
curl -LO https://www.gnupg.org/ftp/gcrypt/gnutls/v3.4/gnutls-3.4.10.tar.xz
tar -xf gnutls-3.4.10.tar.xz 

cd /root/tc-build/gnutls-3.4.10
gmake distclean 2>/dev/null || true
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
CPPFLAGS="-I$PREFIX/include" LDFLAGS="-L$PREFIX/lib" \
    ./configure --host="$TRIPLE" --prefix="$PREFIX" \
      --with-included-libtasn1 --with-included-unistring \
      --disable-doc --disable-tests --disable-guile --without-p11-kit
gmake -j"$(sysctl -n hw.ncpu)"
env -u DESTDIR gmake install   # again, avoid DESTDIR here



# prepare samba) Work in clean build dir, fetch & unpack Samba 4.8.x
cd /root/tc-build
curl -LO https://download.samba.org/pub/samba/stable/samba-4.8.12.tar.gz
tar -xzf samba-4.8.12.tar.gz
cd /root/tc-build/samba-4.8.12
rm -rf bin/ .waf* config.log 2>/dev/null || true

# === 0) Toolchain/env from your NetBSD 6 source tree ===
cd /root/netbsd6/usr/src
TOOLDIR="$(ls -d "$(pwd)"/obj/tooldir.*)"
DESTDIR="$(ls -d "$(pwd)"/obj/destdir.evbarm)"
TRIPLE="$(basename "$(ls "$TOOLDIR"/bin/*-netbsdelf-*gcc | head -n1)" | sed 's/-gcc$//')"
export TOOLDIR DESTDIR TRIPLE
export SYSROOT="$DESTDIR"

# Clean env noise
unset CC CXX CPP LD AR RANLIB STRIP CFLAGS CPPFLAGS LDFLAGS \
      PKG_CONFIG_PATH PKG_CONFIG_LIBDIR PKG_CONFIG_DIR PKG_CONFIG_SYSROOT_DIR

# Paths to tools
export PATH="$TOOLDIR/bin:/usr/pkg/bin:$PATH"
export LD="$TOOLDIR/bin/$TRIPLE-ld --sysroot=$SYSROOT"
export AR="$TOOLDIR/bin/$TRIPLE-ar"
export RANLIB="$TOOLDIR/bin/$TRIPLE-ranlib"
export STRIP="$TOOLDIR/bin/$TRIPLE-strip"

# Your staging prefix (later copied onto the Time Capsule)
export PREFIX="$HOME/tc-stage"
mkdir -p "$PREFIX"

# === 1) Small/LFS compiler wrappers ===
mkdir -p /root/tc-wrap
GCC_ABS="$TOOLDIR/bin/$TRIPLE-gcc"
GXX_ABS="$TOOLDIR/bin/$TRIPLE-g++"
SR="$SYSROOT"

cat > /root/tc-wrap/cc-lfs.sh <<'EOF'
#!/bin/sh
exec "__GCC__" --sysroot="__SR__" -B"__SR__/usr/lib" -B"__SR__/usr/lib/csu" \
  -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES "$@"
EOF
sed -i "s#__GCC__#$GCC_ABS#g;s#__SR__#$SR#g" /root/tc-wrap/cc-lfs.sh

cat > /root/tc-wrap/cxx-lfs.sh <<'EOF'
#!/bin/sh
exec "__GXX__" --sysroot="__SR__" -B"__SR__/usr/lib" -B"__SR__/usr/lib/csu" \
  -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES "$@"
EOF
sed -i "s#__GXX__#$GXX_ABS#g;s#__SR__#$SR#g" /root/tc-wrap/cxx-lfs.sh

chmod +x /root/tc-wrap/cc-lfs.sh /root/tc-wrap/cxx-lfs.sh

export CC="/root/tc-wrap/cc-lfs.sh"
export CXX="/root/tc-wrap/cxx-lfs.sh"
export CPP="$TOOLDIR/bin/$TRIPLE-cpp --sysroot=$SYSROOT"
export HOSTCC=cc
export HOSTCXX=c++
export PYTHON=/usr/pkg/bin/python2.7
[ -e /usr/pkg/bin/python ] && echo "/usr/pkg/bin/python already exists" || ln -s /usr/pkg/bin/python2.7 /usr/pkg/bin/python

# Base flags + fixes used earlier
export CFLAGS="-Os -fno-ident"
export CPPFLAGS="-I$PREFIX/include -I$SYSROOT/usr/include -D_NETBSD_SOURCE -D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES -DID_REAL=0 -DID_EFFECTIVE=1"
export LDFLAGS="-L$PREFIX/lib -L$SYSROOT/lib -L$SYSROOT/usr/lib"

# pkg-config to your stage
export PKG_CONFIG_DIR=
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR="$SYSROOT"

# === 2) Determine NSIG from target headers and force _NSIG numerically ===
NSIG_VAL="$($CPP -dM -E - <<'EOT'
#include <signal.h>
EOT
 | awk '/^[[:space:]]*#define[[:space:]]+NSIG[[:space:]]/{print $3; exit}')"
NSIG_VAL="${NSIG_VAL:-33}"
export CPPFLAGS="$CPPFLAGS -D_NSIG=$NSIG_VAL"

# === 3) Enter Samba tree, clean caches ===
cd /root/tc-build
[ -d samba-4.8.12 ] || { curl -LO https://download.samba.org/pub/samba/stable/samba-4.8.12.tar.gz && tar -xzf samba-4.8.12.tar.gz; }
cd /root/tc-build/samba-4.8.12
rm -rf bin .waf* config.log config.cache 2>/dev/null || true

# === 4) Complete cross-answers file (includes numeric NSIG/_NSIG) ===
cat > netbsd-arm.txt <<EOF
Checking uname sysname type: "NetBSD"
Checking uname machine type: "evbarm"
Checking uname release type: "6.0"
Checking uname version type: "6.0"
Checking simple C program: "hello world"
Checking getconf LFS_CFLAGS: "-D_LARGEFILE_SOURCE -D_FILE_OFFSET_BITS=64 -D_LARGE_FILES"
Checking for large file support without additional flags: "no"
Checking for -D_FILE_OFFSET_BITS=64: "yes"
Checking for -D_LARGE_FILES: "yes"
Checking sizeof(off_t): "8"
Checking for HAVE_SECURE_MKSTEMP: "yes"
Checking for HAVE_MKOSTEMP: "yes"
Checking for HAVE_MKDTEMP: "yes"
Checking whether setresuid is available: "yes"
Checking for setresuid: "yes"
Checking for HAVE_SETRESUID: "yes"
Checking whether setreuid is available: "yes"
Checking for setreuid: "yes"
Checking for HAVE_SETREUID: "yes"
Checking whether setuidx is available: "no"
Checking for setuidx: "no"
Checking for HAVE_SETUIDX: "no"
Checking whether fcntl locking is available: "yes"

rpath library support: "yes"
-Wl,--version-script support: "no"
Checking getconf large file support flags work: "yes"
Checking correct behavior of strtoll: "yes"
Checking for working strptime: "yes"
Checking for C99 vsnprintf: "yes"
Checking for HAVE_SHARED_MMAP: "yes"
Checking for HAVE_INCOHERENT_MMAP: "no"
Checking for HAVE_IFACE_GETIFADDRS: "yes"
Checking for HAVE_IFACE_AIX: "no"
Checking for HAVE_IFACE_IFCONF: "yes"
Checking value of NSIG: "$NSIG_VAL"
Checking value of _NSIG: "$NSIG_VAL"
Checking errno of iconv for illegal multibyte sequence: "EILSEQ"
Checking for kernel change notify support: "no"
Checking for Linux kernel oplocks: "no"
Checking for kernel share modes: "no"
Checking if can we convert from CP850 to UCS-2LE: "no"
Checking if can we convert from IBM850 to UCS-2LE: "no"
Checking if can we convert from UTF-8 to UCS-2LE: "yes"
Checking if can we convert from UTF8 to UCS-2LE: "yes"
Checking for the maximum value of the 'time_t' type: "2147483647"
Checking whether the realpath function allows a NULL argument: "no"
Checking for ftruncate extend: "yes"
getcwd takes a NULL argument: "yes"
EOF

# 5) make build work on aarch64

cd /root/tc-build/samba-4.8.12
rm -rf bin .waf* config.log

# NetBSD’s headers already typedef __uintptr_t, but Samba’s vendored third_party/cmocka/cmocka.h tries to typedef it again
cd /root/tc-build/samba-4.8.12/third_party/cmocka
awk '{
  if ($0 ~ /^#if[[:space:]]*!defined\(_UINTPTR_T\)/) { print "#if !defined(__DEFINED_uintptr_t)"; next }
  if ($0 ~ /^# define _UINTPTR_T$/) { print "# define __DEFINED_uintptr_t"; skip=1; next }
  if ($0 ~ /^# define _UINTPTR_T_DEFINED$/) { next }
  if (skip && $0 ~ /^\#endif/) { skip=0 }
  print
}' cmocka.h > cmocka.h.new && mv cmocka.h.new cmocka.h
cd /root/tc-build/samba-4.8.12

# OPEN A NEW SSH window now and run this to use aarch64 heimdal for configure:

cd /root/host-tools
curl -LO https://github.com/heimdal/heimdal/releases/download/heimdal-7.7.1/heimdal-7.7.1.tar.gz
tar -xzf heimdal-7.7.1.tar.gz

cd /root/host-tools/heimdal-7.7.1
./configure --prefix=/usr/local

gmake -j$(sysctl -n hw.ncpu)
gmake install

gmake -C lib/com_err V=1 compile_et
SRC=lib/com_err/compile_et
install -d /usr/local/libexec/heimdal
install -m 0755 lib/com_err/.libs/compile_et /usr/local/libexec/heimdal/compile_et
ln -sf /usr/local/libexec/heimdal/asn1_compile f
ln -sf /usr/local/libexec/heimdal/compile_et   /usr/local/bin/compile_et

# THEN SWITCH BACK TO THE OTHER SSH SHELL
cd /root/tc-build/samba-4.8.12
export ASN1_COMPILE=/usr/local/libexec/heimdal/asn1_compile
export COMPILE_ET=/usr/local/libexec/heimdal/compile_et
export CPPFLAGS="$CPPFLAGS -D_UINTPTR_T -D_UINTPTR_T_DEFINED -DHAVE_UINTPTR_T -D__DEFINED_uintptr_t"
# use native compiler for build-time tools
export HOSTCC=cc

# === 6) Configure minimal file server (no AD/DC, no winbind, etc.) ===
# run configure again; note the extra --bundled-libraries bit
./configure \
  --cross-compile \
  --cross-answers=netbsd-arm.txt \
  --hostcc="$HOSTCC" \
  --disable-python --without-ad-dc \
  --without-ads --without-ldap \
  --without-winbind --disable-cups --without-pam \
  --without-quotas --without-acl-support \
  --prefix="$PREFIX/samba-min" \
  --with-static-modules=catia,streams_xattr,fruit \
  --with-shared-modules=!DEFAULT \
  --nonshared-binary=smbd/smbd \
  --bundled-libraries='heimdal,!asn1_compile,!compile_et'

cd /root/tc-build/samba-4.8.12
# Keep a backup 
cp source4/torture/wscript_build source4/torture/wscript_build.orig
# go to this file and comment out all RECURSE smb2 and any line related to vfs
# if replacing this file with a stub, it refuses to build
vim source4/torture/wscript_build

python ./buildtools/bin/waf build -j"$(sysctl -n hw.ncpu)"

unset DESTDIR
export PREFIX=/root/tc-stage
python ./buildtools/bin/waf install

# Results
# ja# pwd
# /root/tc-stage
# ja# ls
# bin       include   lib       samba-min share
# ja# ls samba-min/
# bin      bind-dns etc      include  lib      private  sbin     share    var
# ja# ls -l /root/tc-stage/samba-min/sbin/smbd
# -rwxr-xr-x  1 root  wheel  17739110 Sep 11 22:08 /root/tc-stage/samba-min/sbin/smbd