# smbtorture, the SMB client test suite, built from the Samba source the
# appliance builds from (build/env.sh: SAMBA4X_GIT_URL, SAMBA4X_GIT_REF and
# the pinned SAMBA4X_GIT_COMMIT). tests/samba/torture.py builds this image and
# runs one suite per container against a device.
FROM ubuntu:24.04
ARG SAMBA_URL
ARG SAMBA_REF
ARG SAMBA_COMMIT
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        build-essential pkg-config python3 python3-dev git bison flex libparse-yapp-perl \
        libgnutls28-dev zlib1g-dev libpopt-dev libacl1-dev libattr1-dev libjansson-dev \
        ca-certificates >/dev/null
RUN git clone -q --depth 1 --branch "$SAMBA_REF" "$SAMBA_URL" /samba && \
    test "$(git -C /samba rev-parse HEAD)" = "$SAMBA_COMMIT"
WORKDIR /samba
# Only smbtorture, statically linked against the bundled libraries. The
# snapper module does not build without its D-Bus headers, and nothing here
# needs it.
RUN ./configure --without-ad-dc --without-ads --without-ldap --without-json --without-libarchive \
        --without-acl-support --without-pam --without-winbind --without-quotas --without-utmp \
        --without-automount --without-dmapi --without-gettext --without-syslog --without-ldb-lmdb \
        --disable-cups --disable-iprint --disable-avahi --bundled-libraries=ALL \
        '--with-shared-modules=!vfs_snapper' --nonshared-binary=smbtorture >/tmp/configure.log 2>&1 && \
    PYTHONHASHSEED=1 buildtools/bin/waf build -j8 --targets=smbtorture >/tmp/build.log 2>&1 && \
    test -x bin/smbtorture || { tail -30 /tmp/configure.log /tmp/build.log 2>/dev/null; exit 1; }
