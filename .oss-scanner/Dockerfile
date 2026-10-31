# Build from the repository root:
# docker build -f .oss-scanner/Dockerfile -t timecapsulesmb-oss-scanner .
# Debian 13 includes a Git new enough for the patch replay tests (--empty=stop).
FROM debian:trixie AS host

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential gcc-13 g++-13 ca-certificates curl expect file git patch pkg-config \
      python3 python3-dev python3-venv ripgrep smbclient openssh-client \
      bison flex libparse-yapp-perl libgnutls28-dev zlib1g-dev libpopt-dev \
    && rm -rf /var/lib/apt/lists/*

# Match Ubuntu CI's compiler: GCC 14 rejects an implicit declaration in the
# existing durable-handle regression driver. Native fixtures invoke cc directly.
ENV CC=gcc-13 CXX=g++-13
RUN update-alternatives --install /usr/bin/cc cc /usr/bin/gcc-13 100 \
    && update-alternatives --install /usr/bin/c++ c++ /usr/bin/g++-13 100

# Permission tests must run unprivileged, even though the scanner's final layer
# runs as root. The threat model gives runuser commands for that environment.
RUN useradd --create-home --uid 1000 scanner \
    && mkdir -p /src /opt/tcapsulesmb \
    && chown scanner:scanner /src /opt/tcapsulesmb /opt \
    && git config --system --add safe.directory /src \
    && git config --system --add safe.directory /opt/samba-host/source
COPY --chown=scanner:scanner . /src
WORKDIR /src
USER scanner

# Install all Python build and test dependencies while the network is available.
# Keep Makefile's dependency stamp current so make test also works offline.
ENV PATH="/src/.venv/bin:${PATH}"
RUN python3 -m venv .venv \
    && pip install --upgrade pip setuptools wheel \
    && pip install -e . --group dev \
    && touch .venv/.deps-installed

# Retain a production-source Linux service with debug symbols for local PoCs.
# Appliance-only syscalls still need NetBSD hardware validation.
RUN cc -g -fno-omit-frame-pointer \
      -D_GNU_SOURCE -D_DNS_SD_LIBDISPATCH=0 -DNOT_HAVE_SA_LEN \
      -Wall -Wextra -Werror -Wno-sign-compare -Wno-unterminated-string-initialization \
      -Wno-unused-but-set-variable \
      $(sed 's|^|build/|' build/native/service.sources) \
      -o /opt/tcapsulesmb/service

# Keep compilation fatal, but let the scanner inspect a successfully built
# program even if a smoke check finds a regression.
RUN (native_hash=$(printf password | /opt/tcapsulesmb/service --print-nt-hash-from-stdin) \
      && test "$native_hash" = 8846F7EAEE8FB117AD06BDD830B7586C \
      && /opt/tcapsulesmb/service discovery --help \
      && /opt/tcapsulesmb/service telemetry --version) \
    || echo "WARNING: native service smoke checks failed; continuing security audit"

# The existing host harness downloads the pinned Samba source, applies our
# patches, and builds smbd, the migrator and regression drivers with sanitizers.
# Keep the entire configured tree: offline audits can inspect and rebuild it.
FROM host AS audit
ARG JOBS=4
RUN python -m tests.samba.run host --work /opt/samba-host --jobs "$JOBS" --sanitizers --build-only
# This separate layer tolerates test failures only, never failed compilation.
RUN ASAN_OPTIONS=detect_leaks=0:exitcode=86 UBSAN_OPTIONS=halt_on_error=1:exitcode=86 \
      python -m tests.samba.run run --source /opt/samba-host/source \
    || echo "WARNING: Samba regression tests failed; continuing security audit"

CMD ["bash"]
