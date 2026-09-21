# Native device helpers

`service.sources` links one fully static device executable, with independent
`manager`, `discovery`, and `telemetry` processes and diagnostic entrypoints.
Common code is linked once. No role uses pthreads, and host regressions compile
the same unified source list.

| Entrypoint | Responsibility |
| --- | --- |
| `service manager` | Event-driven supervision, Apple volume activation, guarded RAM staging, Samba configuration and child lifetimes. |
| `service discovery` | Bonjour registrations through Apple's mDNSResponder and one owned native wcifsnd child. |
| `service telemetry --daemon` | Heartbeat collection/POST, scheduling and signed debug execution. |
| `service --print-*` | Existing identity, hashing, MaSt and network diagnostics. |

Build the device image with `build/service.sh`, `build/serviceoldle.sh`, or
`build/serviceoldbe.sh` using the existing NetBSD SDK lanes. Boot executes the
Flash copy. Samba and optional rsync execute from RAM because Apple may unmount
the HDD. The manager, discovery and telemetry each collect their own network
plan; successfully applied disk shares are passed to discovery as argv.

Bonjour registrations use `name=NULL` and flags `0`: Apple owns the default
instance name and conflict renaming across SMB/ADisk. This follows the live
stock `diskd` test on 2026-09-19, where an SMB-only collision renamed both
services together without changing `syNm` or the hostname. Successful callback
names are accepted as returned; they are not treated as conflicts against ACP.

`common/` is the shared device-facts collector (v3.1.0): `iflist.c` walks
`sysctl(NET_RT_IFLIST)` itself (Apple's kernel `if_msghdr` differs from the
SDK's, so `getifaddrs()` returns garbage names), `acp.c` runs `acp -q`
children with timeouts and a non-blocking mode, `config.c` decodes the flash
config literally, and `topology.c`/`policy.c`/`identity.c`/`plan.c` turn
those facts into a `struct device_plan` (link roles, service masks, bind
tokens, identity) with the retained-policy rules of the redesign plan.
`loop.c` is the daemons' select loop (PF_ROUTE debounce + 30 s poll).
Host test builds (`TC_NATIVE_TEST`) accept `--facts-file` snapshots. Device
builds omit the fixture parser and accept only live facts; `--print-link-plan`
remains available for live diagnostics.
There is no shared plan daemon or cache/status file; each process owns its own
last validated plan. Fixtures from both device lanes live under
`tests/native/fixtures/iflist/`.

ACP capture uses one process/timeout engine with independent multiline and
whitespace-trimming options and caller-owned buffers. Scalar facts retain their
256-byte first-line, trimmed behavior; raw password and MaSt reads preserve
whitespace. `service --print-device-nt-hash` reads `syPW` and hashes it without
passing plaintext through the shell or temporary files. Its raw capture is
limited to 8 KiB; the existing hash-input limit remains 4096 bytes.
`service --print-samba-identity` returns a versioned four-line response with
NetBIOS name, server string and observed fruit model. Legacy name/model overrides
are ignored; deploy no longer forwards them.

`service --print-mast [--timeout-seconds N]` performs a bounded `acp -A MaSt`
read without starting a daemon. The manager uses the same collector asynchronously,
with a native parser, five-second topology confirmation and ten-second recovery
poll. Apple EVFILT_DEVICE notifications trigger earlier observations and retained
Samba-binding validation even if a brief detach/replug leaves inventory unchanged.

Cold start grants no sharing services until critical facts validate. Failed
rereads retain permissions only on unchanged interfaces. The manager stores this
history directly in C; no process serializes or transfers retained policy.

Module headers declare cross-module functions. `TC_LOCAL` keeps internal
helpers static in device builds; only host regression tests define
`TC_NATIVE_TEST` to link selected internal functions. This matters on NetBSD 4,
where the linker deliberately does not use section garbage collection because
it can discard required ELF notes. Do not include implementation `.c` files.

The manager owns foreground Samba in a separate process group and waits for the
whole group before replacing executables or clearing locks. Anonymous stdin
pipes carry parent lifetime to managed roles. Setup work runs in bounded child
jobs so slow disks do not block supervision. Singleton ownership uses a lock on
the existing service executable, without a new PID or status file.

Apple's mDNSResponder and afpserver remain alive; the AFP preference controls
advertising only. Loopback diskd retains filesystem ownership. Discovery alone
owns wcifsnd; controller failure or a returning wcifsfs conflict replaces that
generation. See [runtime tests](../../tests/native/README.md).

Telemetry creates only `/mnt/Memory/debug` and `/mnt/Memory/debug.sig`. It locks
the existing `/mnt/Memory` directory to exclude concurrent cycles, including
manual runs, without a lock file or job directory. That directory must be
root-owned, with sticky permissions if writable by other users.

## Telemetry protocol

Schema 2 reports `nbns_enabled`, `debug_logging` (Samba or mDNS), and
`advertise_afp`, using null for unreadable settings. `plan_error` contains a
short critical-facts failure reason and is omitted on success. There is no
`ps` probe, constant daemon label, or live registration-state upload.
This probe has no retained history and does not assert that services stopped.

`service telemetry --daemon` sends a boot heartbeat and then one every 12 hours.
`--once [reason]` performs one cycle; `--print-payload [reason]` only prints.
`--cleanup` removes stale debug files without collecting or posting telemetry.
An existing cycle or inherited debug lock causes `--once` and `--cleanup` to
exit 75. Cleanup errors return 1 and prevent a new cycle from starting.

The POST to `/v1/router-heartbeats` uses schema version 2 and preserves the
original device fields. It adds `target_lane` (`6`, `4le`, `4be`) and `debug_nonce` (16 random
bytes as 32 lowercase hex characters). Empty or legacy successful responses
without DEBUG end the cycle. New responses contain a top-level JSON boolean:

```json
{"DEBUG": false}
```

For true, `DEBUG_SIGNATURE` must contain a hex Ed25519 signature of:

```text
tc-debug-v1\n<lowercase SHA-512 hex of the exact POST body bytes>\n
```

The line breaks above denote LF bytes. The body includes the fresh nonce and
device identity, so an HTTP intermediary cannot replay a decision or substitute
a different router ID. Use the same deployed public key as binary verification.
Malformed/duplicate fields, wrong types, invalid authorization or transport
failure never cause execution. Unknown JSON fields are validated and ignored.

The client downloads `/downloads/bin/heartbeat<lane>?debug=true` and
`/downloads/bin/heartbeat<lane>.sig?debug=true`. The server maps these to separate
`debug<lane>` files; this preserves the existing NetBSD 4 HTTP proxy exception.
Old requests without the query still download the old heartbeat. New direct
`/downloads/bin/debug<lane>` routes are also supported by the application.

Debug's detached Ed25519 signature covers the complete executable bytes. The
executable is bounded to 1 MiB while receiving, verified before being written
executable, and run with only the cycle reason as an argument. Response bodies
are bounded to 4096 bytes and signatures to 512. Curl config files are disabled;
fixed endpoints are used without following redirects. No server-provided shell
command or arbitrary URL is executed.

Downloads are written with exclusive creation and mode 0600. The signature file
is deleted immediately after verification, including failed verification. Only
a verified executable is made executable. Each completed or failed cycle
removes its files with checked `unlink` calls; unexpected directories are
reported to stderr and syslog and preserved, and symlinks are removed without
following them.
Telemetry never recursively deletes `/mnt/Memory`.

TERM cancels preparation and stops future scheduling. An already-started debug
process is allowed to finish and is reaped by telemetry. Debug inherits the
kernel lock across exec, so even a killed telemetry parent does not make an
active debug job look stale. `TC_DEBUG_LOCK_FD` identifies the descriptor: debug
must keep it open, and pass it to any detached worker, until all work is done.
Curl does not inherit this descriptor.

After releasing its own reference, telemetry reacquires the lock before
cleanup. If a surviving worker still owns it, cleanup is deferred. Recovery
also runs before each cycle and every 30 seconds while the daemon is idle,
without PID files or age-based guesses. A killed process cannot clean up
immediately; the next unlocked recovery removes its leftovers. Power loss
discards these RAM files.

Reset and uninstall stop telemetry scheduling with TERM. Uninstall checks
`--cleanup` before deleting the runtime and defers if debug remains active.
Migration removes the old `/mnt/Memory/tc-telemetry` tree recursively only after
legacy telemetry/debug/heartbeat processes have exited and no nested mounts
remain. New telemetry never recreates that tree.

Server rollout retains legacy heartbeat routes and responses. New capability
fields select the DEBUG response. `TELEMETRY_ROUTER_DEBUG_IDS` is an explicit
comma-separated list of router IDs, empty by default. Install matching debug
executables in `TELEMETRY_ROUTER_DEBUG_DIR` (default `/data/router-debug`) and
publish matching signatures when replacing them. While selected, a router may
run debug on every cycle; this boolean contract does not claim exactly-once jobs.

## Builds and checks

Run `./build/service.sh`, `./build/serviceoldle.sh`, and
`./build/serviceoldbe.sh` in the existing NetBSD VM as root, or use
`make -C build service-all`. These commands build only the unified service and
never download or rebuild a toolchain.

After copying stripped outputs back, wait five seconds and refresh the artifact
manifest hashes. All three service artifacts must be static ARM ELF executables
with the correct byte order and NetBSD note.

From the repository root:

```sh
./build/native/host-check.sh
.venv/bin/pytest
.venv/bin/pytest -n 4 --dist loadfile
TC_NATIVE_SANITIZERS=1 UBSAN_OPTIONS=halt_on_error=1 .venv/bin/pytest tests/native tests/test_deploy_modules.py
make coverage-native
```

The native suite includes checked-in packet and interface regression cases,
pure parser/scheduler tests, a local HTTP server, real curl and a harmless signed
test executable. Test-only builds use a separate key and local endpoints.
Production endpoints/keys are not used by integration tests. Test children have
deadlines and isolated workspaces. Coverage uses LLVM tools and retains its HTML
report under the temporary output directory printed at completion.
