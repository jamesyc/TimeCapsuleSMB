#!/bin/sh

# Shared configuration for the reproducible NetBSD cross-build workflow.

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE="${TC_ENV_FILE:-$SCRIPT_DIR/.env}"
PRESET_BUILD_TARGET="${BUILD_TARGET:-}"
PRESET_BUILD_SRC="${BUILD_SRC:-}"
PRESET_BUILD_OUT="${BUILD_OUT:-}"
PRESET_SRC="${SRC:-}"
PRESET_OUT="${OUT:-}"

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

NETBSD4_ROOT="${NETBSD4_ROOT:-/root/netbsd4}"
NETBSD6_ROOT="${NETBSD6_ROOT:-/root/netbsd6}"
NETBSD7_ROOT="${NETBSD7_ROOT:-/root/netbsd7}"
NETBSD4_SRC="${NETBSD4_SRC:-$NETBSD4_ROOT/usr/src}"
NETBSD7_SRC="${NETBSD7_SRC:-$NETBSD7_ROOT/usr/src}"
NETBSD4_OUT="${NETBSD4_OUT:-/root/tc-earmv4-netbsd4}"
NETBSD7_OUT="${NETBSD7_OUT:-/root/tc-earmv4}"
BUILD_TARGET="${PRESET_BUILD_TARGET:-${BUILD_TARGET:-netbsd7}}"

case "$BUILD_TARGET" in
    netbsd4)
        BUILD_MACHINE="evbarm"
        BUILD_MACHINE_ARCH="arm"
        BUILD_ROOT_DEFAULT="$NETBSD4_ROOT"
        BUILD_SRC_DEFAULT="$NETBSD4_SRC"
        BUILD_OUT_DEFAULT="$NETBSD4_OUT"
        BUILD_DOWNLOAD_LOG_BASENAME="downloadold.log"
        BUILD_EXPECT_OS_RELEASE="NetBSD 4.0"
        BUILD_EXPECT_HOST_ALIAS="armv4-unknown-netbsd4.0"
        BUILD_EXPECT_EABI="EABI4"
        SAMBA4_WORK_DEFAULT="/root/tc-samba4-netbsd4"
        SAMBA4_STAGE_DEFAULT="/root/tc-stage4-netbsd4"
        SAMBA3_WORK_DEFAULT="/root/tc-samba3-netbsd4"
        SAMBA3_STAGE_DEFAULT="/root/tc-stage3-netbsd4"
        MDNS_STAGE_DEFAULT="/root/tc-stage-mdns-netbsd4"
        NBNS_STAGE_DEFAULT="/root/tc-stage-nbns-netbsd4"
        CROSS_EXEC_REMOTE_DIR_DEFAULT="/tmp/tc-samba-probes-netbsd4"
        ;;
    netbsd7)
        BUILD_MACHINE="evbarm"
        BUILD_MACHINE_ARCH="earmv4"
        BUILD_ROOT_DEFAULT="$NETBSD7_ROOT"
        BUILD_SRC_DEFAULT="$NETBSD7_SRC"
        BUILD_OUT_DEFAULT="$NETBSD7_OUT"
        BUILD_DOWNLOAD_LOG_BASENAME="download.log"
        BUILD_EXPECT_OS_RELEASE="NetBSD 6.0"
        BUILD_EXPECT_HOST_ALIAS="armv4-unknown-netbsd7.2"
        BUILD_EXPECT_EABI="EABI4"
        SAMBA4_WORK_DEFAULT="/root/tc-samba4"
        SAMBA4_STAGE_DEFAULT="/root/tc-stage4"
        SAMBA3_WORK_DEFAULT="/root/tc-samba3"
        SAMBA3_STAGE_DEFAULT="/root/tc-stage3"
        MDNS_STAGE_DEFAULT="/root/tc-stage-mdns"
        NBNS_STAGE_DEFAULT="/root/tc-stage-nbns"
        CROSS_EXEC_REMOTE_DIR_DEFAULT="/tmp/tc-samba-probes-netbsd7"
        ;;
    *)
        echo "Unsupported BUILD_TARGET: $BUILD_TARGET" >&2
        echo "Expected one of: netbsd4, netbsd7" >&2
        return 1
        ;;
esac

BUILD_ROOT="${BUILD_ROOT:-$BUILD_ROOT_DEFAULT}"
BUILD_SRC="${PRESET_BUILD_SRC:-${BUILD_SRC:-${PRESET_SRC:-$BUILD_SRC_DEFAULT}}}"
BUILD_OUT="${PRESET_BUILD_OUT:-${BUILD_OUT:-${PRESET_OUT:-$BUILD_OUT_DEFAULT}}}"
SRC="$BUILD_SRC"
OUT="$BUILD_OUT"
OBJ="$OUT/obj"
TOOLS="$OUT/tools"
STAMPS="$OUT/stamps"
TOOLS_STAMP="$STAMPS/tools.ok"
DIST_STAMP="$STAMPS/distribution.ok"
MKCONF=

HOST_CC="${HOST_CC:-/usr/pkg/gcc7/bin/gcc}"
HOST_CXX="${HOST_CXX:-/usr/pkg/gcc7/bin/g++}"
HOST_CFLAGS="${HOST_CFLAGS:--O -fcommon -fgnu89-inline}"
HOST_CXXFLAGS="${HOST_CXXFLAGS:--O -fcommon -fgnu89-inline}"
HOST_CPPFLAGS="${HOST_CPPFLAGS:--D__GNUC_GNU_INLINE__}"

# AirPort NetBSD 9 libpthread trips malloc aborts on-box. Keep this disabled in
# the target build products unless there is a proven reason to turn it back on.
NO_PTHREADS="${NO_PTHREADS:-1}"

TOOLS_LOG="$OUT/tools.log"
DIST_LOG="$OUT/distribution.log"
HELLO_LOG="$OUT/hello.log"
DOWNLOAD_LOG="${DOWNLOAD_LOG:-$OUT/$BUILD_DOWNLOAD_LOG_BASENAME}"
PROBE_DIR="$OUT/probe"
PROBE_SRC="$PROBE_DIR/hello.c"
PROBE_BIN="$PROBE_DIR/hello"
DIRPROBE_LOG="$OUT/dirprobe.log"
MDNS_LOG="$OUT/mdns.log"
MDNS_STAGE="${MDNS_STAGE:-$MDNS_STAGE_DEFAULT}"
MDNS_BIN_NAME="${MDNS_BIN_NAME:-mdns-smbd-advertiser}"
NBNS_STAGE="${NBNS_STAGE:-$NBNS_STAGE_DEFAULT}"
NBNS_LOG="${NBNS_LOG:-$OUT/nbns.log}"
NBNS_BIN_NAME="${NBNS_BIN_NAME:-nbns-advertiser}"

SAMBA4_VERSION="${SAMBA4_VERSION:-4.8.12}"
SAMBA4_GIT_URL="${SAMBA4_GIT_URL:-https://github.com/samba-team/samba.git}"
SAMBA4_GIT_REF="${SAMBA4_GIT_REF:-samba-${SAMBA4_VERSION}}"
SAMBA4_WORK="${SAMBA4_WORK:-$SAMBA4_WORK_DEFAULT}"
SAMBA4_SRC_DIR="${SAMBA4_SRC_DIR:-$SAMBA4_WORK/samba-${SAMBA4_VERSION}}"
SAMBA4_STAGE="${SAMBA4_STAGE:-$SAMBA4_STAGE_DEFAULT}"
SAMBA4_BUILD="${SAMBA4_BUILD:-$SAMBA4_WORK/build}"
SAMBA4_DOWNLOAD_LOG="${SAMBA4_DOWNLOAD_LOG:-$OUT/downloadsamba4.log}"
SAMBA4_LOG="${SAMBA4_LOG:-$OUT/samba4.log}"
SAMBA4_JOBS="${SAMBA4_JOBS:-2}"
SAMBA4_HOST_ALIAS="${SAMBA4_HOST_ALIAS:-$BUILD_EXPECT_HOST_ALIAS}"

SAMBA3_VERSION="${SAMBA3_VERSION:-3.6.25}"
SAMBA3_TARBALL_URL="${SAMBA3_TARBALL_URL:-https://download.samba.org/pub/samba/stable/samba-${SAMBA3_VERSION}.tar.gz}"
SAMBA3_GIT_URL="${SAMBA3_GIT_URL:-https://github.com/samba-team/samba.git}"
SAMBA3_GIT_REF="${SAMBA3_GIT_REF:-samba-${SAMBA3_VERSION}}"
SAMBA3_WORK="${SAMBA3_WORK:-$SAMBA3_WORK_DEFAULT}"
SAMBA3_SRC_DIR="${SAMBA3_SRC_DIR:-$SAMBA3_WORK/samba-${SAMBA3_VERSION}}"
SAMBA3_STAGE="${SAMBA3_STAGE:-$SAMBA3_STAGE_DEFAULT}"
SAMBA3_BUILD="${SAMBA3_BUILD:-$SAMBA3_WORK/build}"
SAMBA3_DOWNLOAD_LOG="${SAMBA3_DOWNLOAD_LOG:-$OUT/downloadsamba3.log}"
SAMBA3_LOG="${SAMBA3_LOG:-$OUT/samba3.log}"
SAMBA3_JOBS="${SAMBA3_JOBS:-2}"
SAMBA3_HOST_ALIAS="${SAMBA3_HOST_ALIAS:-$BUILD_EXPECT_HOST_ALIAS}"
CROSS_EXEC_REMOTE_DIR="${CROSS_EXEC_REMOTE_DIR:-$CROSS_EXEC_REMOTE_DIR_DEFAULT}"

TC_HOST="${TC_HOST:-root@192.168.1.217}"
TC_SSH_OPTS="${TC_SSH_OPTS:--o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null}"
TC_SSH_PROXYCOMMAND="${TC_SSH_PROXYCOMMAND:-}"
TC_PASSWORD_FILE="${TC_PASSWORD_FILE:-}"
TC_PASSWORD="${TC_PASSWORD:-}"

TC_NET_IFACE="${TC_NET_IFACE:-bridge0}"
TC_SHARE_NAME="${TC_SHARE_NAME:-Data}"
TC_NETBIOS_NAME="${TC_NETBIOS_NAME:-TimeCapsule}"
TC_PAYLOAD_DIR_NAME="${TC_PAYLOAD_DIR_NAME:-samba4}"
TC_CIFS_COMMENT="${TC_CIFS_COMMENT:-James's AirPort Time Capsule}"
TC_CIFS_SYSDNSNAME="${TC_CIFS_SYSDNSNAME:-James's AirPort Time Capsule}"
TC_CIFS_VOLUME_NAME="${TC_CIFS_VOLUME_NAME:-AirPort Disk}"

if [ ! -f "$TC_PASSWORD_FILE" ] && [ -n "$TC_PASSWORD" ]; then
    TC_PASSWORD_FILE=$(mktemp "${TMPDIR:-/tmp}/timecapsule-password.XXXXXX")
    chmod 600 "$TC_PASSWORD_FILE"
    printf '%s' "$TC_PASSWORD" >"$TC_PASSWORD_FILE"
fi

tc_ssh() {
    if [ -n "${TC_PASSWORD_FILE:-}" ] && [ -f "$TC_PASSWORD_FILE" ]; then
        SSHPASS=$(cat "$TC_PASSWORD_FILE")
        export SSHPASS
        if [ -n "$TC_SSH_PROXYCOMMAND" ]; then
            sshpass -e ssh $TC_SSH_OPTS -o "ProxyCommand=$TC_SSH_PROXYCOMMAND" "$@"
        else
            sshpass -e ssh $TC_SSH_OPTS "$@"
        fi
        return
    fi
    if [ -n "$TC_SSH_PROXYCOMMAND" ]; then
        ssh $TC_SSH_OPTS -o "ProxyCommand=$TC_SSH_PROXYCOMMAND" "$@"
    else
        ssh $TC_SSH_OPTS "$@"
    fi
}

tc_scp() {
    if [ -n "${TC_PASSWORD_FILE:-}" ] && [ -f "$TC_PASSWORD_FILE" ]; then
        SSHPASS=$(cat "$TC_PASSWORD_FILE")
        export SSHPASS
        if [ -n "$TC_SSH_PROXYCOMMAND" ]; then
            sshpass -e scp -O $TC_SSH_OPTS -o "ProxyCommand=$TC_SSH_PROXYCOMMAND" "$@"
        else
            sshpass -e scp -O $TC_SSH_OPTS "$@"
        fi
        return
    fi
    if [ -n "$TC_SSH_PROXYCOMMAND" ]; then
        scp -O $TC_SSH_OPTS -o "ProxyCommand=$TC_SSH_PROXYCOMMAND" "$@"
    else
        scp -O $TC_SSH_OPTS "$@"
    fi
}

export SCRIPT_DIR REPO_ROOT ENV_FILE
export NETBSD4_ROOT NETBSD6_ROOT NETBSD7_ROOT NETBSD4_SRC NETBSD7_SRC
export NETBSD4_OUT NETBSD7_OUT BUILD_TARGET BUILD_ROOT BUILD_SRC BUILD_OUT
export BUILD_MACHINE BUILD_MACHINE_ARCH
export SRC OUT OBJ TOOLS STAMPS TOOLS_STAMP DIST_STAMP MKCONF
export HOST_CC HOST_CXX HOST_CFLAGS HOST_CXXFLAGS HOST_CPPFLAGS
export NO_PTHREADS TOOLS_LOG DIST_LOG HELLO_LOG DOWNLOAD_LOG PROBE_DIR PROBE_SRC PROBE_BIN
export DIRPROBE_LOG MDNS_LOG MDNS_STAGE MDNS_BIN_NAME NBNS_STAGE NBNS_LOG NBNS_BIN_NAME
export BUILD_EXPECT_OS_RELEASE BUILD_EXPECT_HOST_ALIAS BUILD_EXPECT_EABI CROSS_EXEC_REMOTE_DIR
export SAMBA4_VERSION SAMBA4_GIT_URL SAMBA4_GIT_REF SAMBA4_WORK SAMBA4_SRC_DIR SAMBA4_STAGE
export SAMBA4_BUILD SAMBA4_DOWNLOAD_LOG SAMBA4_LOG SAMBA4_JOBS SAMBA4_HOST_ALIAS
export SAMBA3_VERSION SAMBA3_TARBALL_URL SAMBA3_GIT_URL SAMBA3_GIT_REF SAMBA3_WORK SAMBA3_SRC_DIR SAMBA3_STAGE
export SAMBA3_BUILD SAMBA3_DOWNLOAD_LOG SAMBA3_LOG SAMBA3_JOBS SAMBA3_HOST_ALIAS
export TC_HOST TC_SSH_OPTS TC_SSH_PROXYCOMMAND TC_PASSWORD_FILE TC_PASSWORD
export TC_NET_IFACE TC_SHARE_NAME TC_NETBIOS_NAME TC_PAYLOAD_DIR_NAME
export TC_CIFS_COMMENT TC_CIFS_SYSDNSNAME TC_CIFS_VOLUME_NAME
