# Native appliance runtime regressions

These tests compile production C modules or run the actual combined manager
against controlled appliance interfaces. Process groups, pipes, signals, file
staging, and publication are real. Apple-specific observations are documented
beside the cases; physical cable tests remain device integration gates.

The shell runtime tests moved with their behavior:

| Former shell responsibility | Native coverage |
| --- | --- |
| MaSt formats, UUIDs, malformed input and volatile fields | `test_mast.py`, `test_storage_settle.py` |
| Share names, collisions, ADisk limits and Samba tuning | `test_samba_config.py`, exact-argv manager case |
| Diskd claims, failed mounts, markers and payload choice | `test_storage_runtime.py` |
| RAM copies, auth/config failures, retries and desired-state reversion | `test_worker.py`, `test_staging.py`, `test_manager.py` |
| Bind retention and cold startup | `test_retained_policy.py`, `test_plan.py`, manager network-history case |
| Manager, discovery, rsync and telemetry ownership | `test_process.py`, `test_manager.py`, `test_inspect.py` |
| Apple diskd recovery and CIFS/NBNS conflicts | Manager recovery cases; Apple AFP/mDNS exclusion in inspection cases |
| Local hostname resolution and bounded logs | `test_hosts.py`, `test_log_trim.py`, existing timestamp cases |
| No boot/hotplug migration or dependence on TDB/checkpoint presence | Manager legacy-metadata case |
| One-time boot preparation | `tests/test_native_boot.py` (retained shell, executed directly) |
| Legacy release shutdown and telemetry workspace cleanup | `tests/test_appliance_process_shutdown.py`, `tests/test_telemetry_cleanup.py` |

Tests of shell cadence arithmetic and copying a second full smbd image were
retired deliberately. Absolute deadlines and event-driven recovery replace
those cadences; replacement occurs after the old process group exits so the
15 MiB RAM filesystem does not need two executable images. Small configuration
files are prepared before publication, and interrupted work remains retryable.

Python deployment/storage tests and Doctor checks remain in their existing
suites. The actual patched Samba descriptor/reload/AIO tests live in
`tests/samba/tc_storage_reload_test.c`.

The recovery regressions also cover partial storage outcomes with bounded
retries, unchanged MaSt after a failed preparation, and payload-candidate caches
that follow volume identity across inventory reordering. Retrying a failed disk
must not inspect or claim healthy disks. Retry-only state changes must not
restart Samba or discovery.

`test_nested_owner.py` kills real setup workers while their commands and
children remain alive. The manager's inventory/network collection tests exercise
both owned collection completion and parent death. Standalone ACP cancellation
keeps its separate-group coverage, while managed reads share their job or role's
cleanup boundary. Telemetry retains its protected diagnostic drain policy.

Payload discovery and console logs are trimmed only at launch/restart, using the
actual selected destination. Tests preserve debug output and file identity,
reject symlinks, and verify that healthy audits touch only the bounded RAM logs.
This deliberately provides launch-time retention, not a continuous HDD log cap.

Native NBNS failures stop and reap only discovery's wcifsnd child, then retry
with bounded backoff while preserving Bonjour's original IPC connections.
Apple's reference-counted adds are never retransmitted after an uncertain reply.
The controller tests cover partial registration, deadline/reset arithmetic,
validated-plan startup gating, disable/shutdown during retry, and escalation
when the old child cannot be safely cleaned up. Device supervision verifies
that repeated native child deaths preserve discovery, Bonjour, and an SMB handle.
