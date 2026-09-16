# Vendored Apple `dns_sd` client stub

These four files are copied **verbatim** from Apple's open-source mDNSResponder
release, directory `mDNSShared/`:

- Source: https://github.com/apple-oss-distributions/mDNSResponder
- Tag: `mDNSResponder-379.38.1`
- Tarball kept locally at `plan/spike-2026-09-16/mDNSResponder-379.38.1.tar.gz`
  (git-ignored, maintainer only)

| File | SHA-256 |
| --- | --- |
| `dns_sd.h` | `22ce94ed8370ddfcf2cf976528758bc05b5e99a61865fc8fb44cdab977dd1102` |
| `dnssd_ipc.h` | `2ceabfa0fc065f9c33bfeb7da5520eb1950b6aeb2a625ff385279fd760f2ae84` |
| `dnssd_ipc.c` | `bd19446712de02f3cdf93864436f6979e2fd438a1b3345728b4bc198ad7a3e52` |
| `dnssd_clientstub.c` | `13f4869eb720f0fcb4fb616c5476e4c6dc4c41af02d1dd62c1fc3c28d17c35fa` |
| `LICENSE` (tarball root) | `b46276a73da6301d2dd25a45b04f1b73ccb893a8e420070f49c7afc7f3b097d5` |

The device daemon is `mDNSResponder-397.32`; IPC `VERSION 1` is identical, and
the Unix socket is `/var/run/mDNSResponder` (`MDNS_UDS_SERVERPATH`).

## License

Each of the four files carries Apple's **three-clause BSD** notice in its header
(the tarball `LICENSE` explains that the shared client-library code, which is
linked into the client's address space, is BSD-licensed so that it is compatible
with any client license; the daemon itself is Apache-2.0). The v3.1
implementation guide said "Apache-2.0 notice" — the file headers are the
authority, and they are BSD. `LICENSE` is the tarball's root license file,
copied unchanged.

## Build rules

- Compile with the helper's normal flags plus `-D_DNS_SD_LIBDISPATCH=0`. On a
  Linux host (CI) add `-DNOT_HAVE_SA_LEN` as Apple's own Linux build does; the
  stub reads `sin_len`/`sin6_len` otherwise. NetBSD and macOS have `sa_len`.
- Only `mdns.sources` links these files; the other helpers never talk to the
  daemon. The upstream `dnssd_clientlib.c` is not vendored: the registrant
  assembles its TXT bytes itself and does not use those convenience helpers.
- Do not edit these files. `dnssd_clientstub.c` trips
  `-Wunused-but-set-variable` under clang `-Wall -Wextra -Werror` on the host;
  `tests/native/build.py` and `tests/native/cases.py` add a per-file
  `-Wno-unused-but-set-variable` instead of patching the source. The NetBSD 4
  gcc 4.1.2 lane compiles them unchanged.
- The test suite points the stub at a fake daemon with
  `-DMDNS_UDS_SERVERPATH="<tmpdir>/mDNSResponder"` (see
  `tests/native/integration/fake_dnssd_daemon.py`).
