# TimeCapsuleSMB threat model

## Product and assets

TimeCapsuleSMB installs a patched, static Samba server and a native C service on
Apple AirPort/Time Capsule appliances running Apple's NetBSD 4 or 6 firmware.
Python CLI tooling on macOS/Linux performs discovery, configuration, deployment,
diagnostics, metadata migration, disk repair, and optional firmware boot-hook
patching. The macOS Swift app invokes the same Python backend through a bundled
helper using JSON over pipes; it is not a network API or a privileged macOS daemon.

Protect user files and Time Machine backups, native HFS metadata, appliance
firmware/configuration, device administrator passwords and SMB hashes, and the
files and credentials of the host user running the CLI/app. Preserve the intended
share boundaries and avoid attacker-induced execution, destructive writes, or
unbounded resource consumption on these memory-constrained devices.

## Scope and useful entry points

Focus on project-owned code and its integration with dependencies:

- `src/timecapsulesmb/`: CLI/app request handling; config and credential handling;
  Bonjour/ACP discovery and saved-device identity; SSH command construction and
  transfers; payload selection/verification; deployment, migration, uninstall,
  flash parsing/writes, and telemetry/log redaction. Useful directories are
  `app/`, `core/`, `discovery/`, `transport/`, `integrations/`, `device/`,
  `deploy/`, and `services/`.
- `build/native/`: the unified `service` executable's manager, discovery,
  telemetry, and diagnostic roles; parsers for ACP/MaSt, routing messages,
  configuration and HTTP/JSON; generated Samba configuration; storage/staging
  logic; process ownership; signed debug authorization and execution.
- `src/timecapsulesmb/assets/boot/samba4/`: root boot preparation and cleanup.
- `build/patches/samba4x/series`, patches, and `overlay/`: project changes to
  Samba, especially native symlinks/reparse data, HFS xattrs/resource forks,
  AppleDouble/TDB migration, `*at` emulation, durable handles, disk rebinding,
  and fork repair. Review changed behavior in its upstream calling context.
- `macos/TimeCapsuleSMB/Sources/`: profile identity, Keychain integration,
  helper request/event handling, command confirmation, and credential exposure.
  The app's helper launcher is `TimeCapsuleSMBHelper/main.c`; Python's request
  entry point is `src/timecapsulesmb/app/helper.py`.

The separate telemetry server is not in this repository. Standalone audits of
Apple firmware, upstream Samba/rsync/curl/Python, vendored libraries, or macOS are
outside this project's primary scope. Their use, project patches, and unsafe
configuration are in scope; identify upstream-only issues separately rather than
attributing them to this project. Dependency version/CVE matches alone need an
applicable code path and impact.

## Attacker capabilities and trust boundaries

1. **Unauthenticated network peer.** A peer on a reachable network can send SMB
   requests, publish misleading Bonjour services/TXT records and addresses, and
   respond to discovery probes. An on-path attacker can alter or block traffic.
   Discovery names and IP addresses are not proof of identity. Inspect the flow
   from discovery through saved AirPort MAC/password checks to destructive
   operations, including address changes and multiple devices sharing a password.
2. **Authenticated SMB client or writer of shared files.** A client can supply
   paths, filenames, links, reparse payloads, alternate streams, metadata, and
   unusual operation sequences. Consider concurrent clients, renames, reconnects,
   disk replacement, and malformed legacy TDB/AppleDouble data. Distinguish the
   ability to modify an exported share from arbitrary appliance execution or
   writes outside that share.
3. **Untrusted device data reaching the host.** Treat Bonjour/ACP fields, volume
   labels, remote paths and command output, and firmware/container bytes as data
   at parser and shell boundaries. A malicious or compromised appliance already
   owns itself, but must not gain execution on the administrator's computer or
   cause unintended host file access through those inputs.
4. **Telemetry network intermediary.** Device heartbeats and debug downloads use
   HTTP. Responses and executable bytes are untrusted until checked. Debug runs
   as appliance root: authorization must bind to the exact fresh heartbeat body
   (including its nonce), and the downloaded binary must verify under the embedded
   Ed25519 public key. Parser ambiguity, replay, signature bypass, substitution,
   unsafe workspace paths, and cleanup races are high-value targets. A validly
   authorized, signed debug executable is intentionally trusted root code.
5. **Local lower-privilege user or filesystem adversary.** Check permissions,
   symlinks and races where another user can actually control a file or directory.
   Do not assume access to the administrator's Keychain, home directory, shell
   environment, trusted source checkout, or installed app without demonstrating
   how it is obtained. The local operator deliberately choosing commands, config,
   SSH options, or a helper executable is trusted; those interfaces are not a
   sandbox for hostile same-user code.

## Existing design constraints and limitations

- This is a consumer LAN appliance, not an internet-facing hardened service.
  Apple's firmware/kernel, administrator workstation, authentic release inputs,
  and telemetry signing private key are trusted. No test should assume an
  attacker already has appliance root, the signing key, or arbitrary write access
  to the installed application merely to demonstrate equivalent access.
- SMB authentication uses the AirPort device password; usernames map to Unix
  `root` and filesystem access runs as root. There is no per-user SMB isolation.
  Guest file access is disabled. `build/native/samba/config.c` is authoritative
  for defaults and optional protocol/signing/encryption settings.
- Samba listens on wildcard IPv4/IPv6 TCP 445. Bonjour/NBNS advertisement policy
  is not a packet filter: Apple's firewall determines WAN/guest reachability.
  The optional rsync daemon is disabled by default; enabling it intentionally
  exposes a writable unauthenticated module as root on TCP 873.
- Default SSH options allow legacy algorithms and disable host-key verification
  (`src/timecapsulesmb/core/config.py`). Saved MAC/password checks help avoid
  operating on the wrong appliance but do not provide a general cryptographic
  defense against an active network impersonator.
- Native telemetry HTTP provides no confidentiality. Its embedded bearer token
  is public source data, not a secret or debug-execution authority. The signatures
  protect debug authorization and code integrity; they do not encrypt telemetry.
  CLI configuration may contain a plaintext device password; the macOS app uses
  Keychain. Unexpected credential disclosure in logs, results, or telemetry
  remains in scope.
- Payload SHA-256 manifests and on-device staging CRCs detect mismatches/damage;
  they do not authenticate a distribution an attacker can replace in full.
  Deployment can stop services and reboot, and an interrupted deployment may be
  temporarily unusable. Rerunning it should converge while preserving user data
  and migration metadata. Uninstall deliberately removes project metadata.
- Apple's old kernels lack threads and some system calls and have known VM/HFS
  defects. Host tests cannot prove behavior on these kernels. Report whether a
  failure is in project code, a project workaround, or unchanged firmware.

Use these limitations to explain a finding's actual additional impact. Do not
infer protections from advertisement policy, a checksum, root credential mapping,
or a successful Linux test that the product does not claim to provide.

## Validation and reporting

The checkout is `/src`, with its editable Python installation and test dependencies
in `/src/.venv`. `/opt/tcapsulesmb/service` is a production-source Linux build with
debug symbols. `/opt/samba-host/source` contains the pinned Samba source with our
patches applied, a configured build, smbd, the migrator, and regression drivers
built with AddressSanitizer and UndefinedBehaviorSanitizer. The extra upstream
source is there to exercise our changes, not to expand the scan to unrelated
upstream code. NetBSD executables in `/src/bin` are retained for manifest checks;
they cannot run on Linux.

Image creation stops on compilation errors. Native smoke checks and Samba
regressions report failures as warnings so the scanner can still audit the built
code. The standalone test commands below retain their failing exit status.

From the scanner's root shell, run tests as the image's `scanner` user so Unix
permission checks work. These commands need no internet access:

```sh
runuser -u scanner -- sh -c 'cd /src && .venv/bin/pytest -n 2 --dist worksteal'
runuser -u scanner -- sh -c 'cd /src && ASAN_OPTIONS=detect_leaks=0:exitcode=86 UBSAN_OPTIONS=halt_on_error=1:exitcode=86 .venv/bin/python -m tests.samba.run run --source /opt/samba-host/source'
```

For local validation, build from the repository root for the host's native
architecture and run the same suite with the network disabled. Use `--init`,
as Anthropic's `tools/check` does, so orphaned test children are reaped. The
image already defaults to the unprivileged user:

```sh
docker build -f .oss-scanner/Dockerfile -t timecapsulesmb-oss-scanner .
docker run --rm --init --network=none timecapsulesmb-oss-scanner .venv/bin/pytest -n 2 --dist worksteal
docker run --rm --init --network=none -e ASAN_OPTIONS=detect_leaks=0:exitcode=86 -e UBSAN_OPTIONS=halt_on_error=1:exitcode=86 timecapsulesmb-oss-scanner .venv/bin/python -m tests.samba.run run --source /opt/samba-host/source
```

Native test fixtures compile their sources locally on demand. For sanitizer
coverage, set `TC_NATIVE_SANITIZERS=1 UBSAN_OPTIONS=halt_on_error=1` and select
`tests/native tests/test_deploy_modules.py`. `make test-c` rechecks the production
service. For edited Samba sources, rebuild the desired target in the retained
tree as `scanner`:

```sh
runuser -u scanner -- sh -c 'cd /opt/samba-host/source && PYTHONHASHSEED=1 /src/.venv/bin/python buildtools/bin/waf build --targets=TARGET'
```

Do not rerun the Samba `host` subcommand offline: it creates a fresh checkout
from the network. Changes to our patch series must be reapplied to the source
tree before rebuilding; editing a patch file alone does not update that tree.
To prepare a patch replay offline, clone the retained tree's pristine Git tag
(its patched working files are not cloned) into a new directory:

```sh
runuser -u scanner -- sh -c 'cd /src && SAMBA4X_GIT_URL=/opt/samba-host/source .venv/bin/python build/samba4x_replay.py init /tmp/samba-replay'
```

The scanner container supports offline Linux Python/native host tests. macOS UI,
Apple kernel behavior and ARM device binaries require separate environments;
source review is still useful. Prefer existing `tests/native/` fixtures,
loopback-only telemetry tests with their test signing key, mocked SSH/ACP calls,
and temporary synthetic files. Do not contact real appliances, the build VM,
production telemetry, or other external services, deploy payloads, write firmware,
or use credentials from local `.env` files.

For each finding include the scanned commit, affected path/function and relevant
lines, attacker role and prerequisites (including non-default settings), entry
point and trust boundary, expected versus actual result, and concrete impact.
Supply the smallest safe reproducer/test and exact command, showing a reachable
production path rather than only a test-only helper. State what was reproduced
on Linux and what still requires macOS or an appliance; do not claim hardware
validation from mocks. Avoid real secrets or personal backup contents.

Prioritize unauthenticated execution, authentication/signature bypass, host
command injection, credential disclosure, share escapes, persistent corruption
of user data, and practical remote denial of service. Judge severity from the
demonstrated capability, exposure, required credentials/settings, persistence,
and recovery, not merely from a dangerous function's presence. Report bounded
resource issues with realistic input sizes and distinguish intended destructive
administrator actions from attacker-controlled effects.

Use Critical for demonstrated unauthenticated arbitrary execution or similarly
broad compromise; High for authentication/signature bypass, meaningful share
escape, sensitive credential disclosure, or substantial persistent data damage;
Medium for practical service denial or limited disclosure requiring narrower
conditions; and Low for small security impact. Explain any departure from these
examples. A memory-safety failure without a demonstrated exploit still deserves
a report; state the proven impact and uncertainty rather than assuming execution.

Group reports by root cause and fix, listing affected entry points together.
Prefer a minimal candidate patch with a behavioral regression test covering the
exploit and legitimate input. Follow `AGENTS.md` when proposing fixes; regenerate
Samba patches with `build/samba4x_replay.py` rather than hand-editing patch files.
Do not modify shipped binaries or claim device validation in this container.

Send technical findings privately through OSS Scanner's configured report
recipient. `SECURITY.md` describes the general public reporting entry point and
asks that exploit details not be posted in a public issue.
