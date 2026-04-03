#!/bin/sh

# Shared configuration for the reproducible NetBSD cross-build workflow.

NETBSD6_ROOT=/root/netbsd6
NETBSD7_ROOT=/root/netbsd7
SRC="$NETBSD7_ROOT/usr/src"
OUT=/root/tc-earmv4
OBJ="$OUT/obj"
TOOLS="$OUT/tools"
STAMPS="$OUT/stamps"
TOOLS_STAMP="$STAMPS/tools.ok"
DIST_STAMP="$STAMPS/distribution.ok"
MKCONF=

HOST_CC=/usr/pkg/gcc7/bin/gcc
HOST_CXX=/usr/pkg/gcc7/bin/g++
HOST_CFLAGS='-O -fcommon -fgnu89-inline'
HOST_CXXFLAGS='-O -fcommon -fgnu89-inline'
HOST_CPPFLAGS='-D__GNUC_GNU_INLINE__'

# AirPort NetBSD 9 libpthread trips malloc aborts on-box. Keep this disabled in
# the target build products unless there is a proven reason to turn it back on.
NO_PTHREADS=1

TOOLS_LOG="$OUT/tools.log"
DIST_LOG="$OUT/distribution.log"
HELLO_LOG="$OUT/hello.log"
DOWNLOAD_LOG="$OUT/download.log"
PROBE_DIR="$OUT/probe"
PROBE_SRC="$PROBE_DIR/hello.c"
PROBE_BIN="$PROBE_DIR/hello"
DIRPROBE_LOG="$OUT/dirprobe.log"

SAMBA4_VERSION="${SAMBA4_VERSION:-4.3.13}"
SAMBA4_GIT_URL="${SAMBA4_GIT_URL:-https://github.com/samba-team/samba.git}"
SAMBA4_GIT_REF="${SAMBA4_GIT_REF:-samba-${SAMBA4_VERSION}}"
SAMBA4_WORK="${SAMBA4_WORK:-/root/tc-samba4}"
SAMBA4_SRC_DIR="${SAMBA4_SRC_DIR:-$SAMBA4_WORK/samba-${SAMBA4_VERSION}}"
SAMBA4_STAGE="${SAMBA4_STAGE:-/root/tc-stage4}"
SAMBA4_BUILD="${SAMBA4_BUILD:-$SAMBA4_WORK/build}"
SAMBA4_DOWNLOAD_LOG="${SAMBA4_DOWNLOAD_LOG:-$OUT/downloadsamba4.log}"
SAMBA4_LOG="${SAMBA4_LOG:-$OUT/samba4.log}"
SAMBA4_JOBS="${SAMBA4_JOBS:-2}"
SAMBA4_HOST_ALIAS="${SAMBA4_HOST_ALIAS:-armv4-unknown-netbsd7.2}"
SAMBA4_FALLBACK_VERSION="${SAMBA4_FALLBACK_VERSION:-4.0.26}"
SAMBA4_FALLBACK_GIT_REF="${SAMBA4_FALLBACK_GIT_REF:-samba-${SAMBA4_FALLBACK_VERSION}}"

TC_HOST=root@192.168.1.217
TC_SSH_OPTS='-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1'
TC_PASSWORD_FILE="${TC_PASSWORD_FILE:-$(CDPATH= cd -- "$(dirname "$0")" && pwd)/password.txt}"

export NETBSD6_ROOT NETBSD7_ROOT SRC OUT OBJ TOOLS STAMPS TOOLS_STAMP DIST_STAMP MKCONF
export HOST_CC HOST_CXX HOST_CFLAGS HOST_CXXFLAGS HOST_CPPFLAGS
export NO_PTHREADS TOOLS_LOG DIST_LOG HELLO_LOG DOWNLOAD_LOG PROBE_DIR PROBE_SRC PROBE_BIN
export DIRPROBE_LOG
export SAMBA4_VERSION SAMBA4_GIT_URL SAMBA4_GIT_REF SAMBA4_WORK SAMBA4_SRC_DIR SAMBA4_STAGE
export SAMBA4_BUILD SAMBA4_DOWNLOAD_LOG SAMBA4_LOG SAMBA4_JOBS SAMBA4_HOST_ALIAS
export SAMBA4_FALLBACK_VERSION SAMBA4_FALLBACK_GIT_REF
export TC_HOST TC_SSH_OPTS TC_PASSWORD_FILE
