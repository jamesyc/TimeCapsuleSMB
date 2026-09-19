# Agent Notes

## Design Rule

Do exactly what Apple's firmware does. When an exact match would cost a lot of code, a slightly imperfect but practical option that takes far less code is acceptable — say which one you chose and why. Apple's behavior is the reference; our own earlier behavior is not.

## NetBSD Builds

If a change touches `build/`, rebuild the affected artifact in the NetBSD VM before considering the work done.

- VM: `james@192.168.64.4`
- If `192.168.64.4` times out, retry via UTM's bridge using `ssh -b 192.168.64.1` (or `rsync -e 'ssh -b 192.168.64.1'`) before declaring the VM unreachable.
- VM password: look in `.env.backup6` file
- Sync: `rsync` the repo to `~/TimeCapsuleSMB/` on the VM
- Sync only source/build inputs; exclude host build artifacts and caches (`.build/`, `dist/`, `DerivedData/`, virtualenvs, bytecode, coverage/test caches, `.git/`, and checked-in `bin/` outputs). Remove any such copies from the VM repo checkout before building, but never remove the VM SDK/toolchains or active NetBSD build outputs outside that checkout.
- Build as `root`: `ssh` in as `james`, then run `su` interactively with the same password. Do not use `su -c` on this VM. `/root` is readable by `james` but not writable: stage files via `/tmp`, then `cp` as root.
- The VM disk is ~94% full (~1.3 GB free). Do not clone a second Samba tree there, and exclude macOS build junk from the rsync or it fills up.
- Run the artifact-specific helper under `build/`
- For `discoveryd`, `./build/discovery.sh` builds the NetBSD 6 variant, `./build/discoveryoldle.sh` builds the current NetBSD 4 little-endian variant, and `./build/discoveryoldbe.sh` uses the parallel big-endian-named lane
- If the build fails, investigate why: inspect the VM log, fix the source, resync, and rebuild
- After a successful build, copy the stripped root-built binary back into the repo, sleep 5 seconds, then update hashes in `src/timecapsulesmb/assets/artifact-manifest.json`
- For `discoveryd`, the stripped root outputs are:
  - VM `/root/tc-netbsd7/discoveryd.stripped` -> repo `bin/discovery/discoveryd`
  - VM `/root/tc-netbsd4le/discoveryd.stripped` -> repo `bin/discovery-netbsd4le/discoveryd`
  - VM `/root/tc-netbsd4be/discoveryd.stripped` -> repo `bin/discovery-netbsd4be/discoveryd`

  To run the build, you're supposed to run
  ./build/discovery.sh && ./build/discoveryoldle.sh && ./build/discoveryoldbe.sh

  For current Samba 4.24.1, to build: 
  NetBSD 6: ./build/downloadsamba4x.sh && ./build/samba4x.sh 
  NetBSD 4 LE: ./build/downloadsamba4xoldle.sh && ./build/samba4xoldle.sh 
  NetBSD 4 BE: ./build/downloadsamba4xoldbe.sh && ./build/samba4xoldbe.sh

  Do not run _downloadsamba4.sh, _downloadsamba4x.sh, or other underscore helpers directly.

  The `samba4x*.sh` scripts distclean and rebuild everything (hours) and relink `smbd`. For a one-binary change (e.g. the migrator), the configured lane trees under `/root/tc-samba4x-{netbsd7,netbsd4le,netbsd4be}/samba` accept `waf build --targets=...` after sourcing `build/env.sh` with the lane variables (`SDK_FAMILY`, `NETBSD4_ABI`, `TC_ENV_FILE=/dev/null`). Those trees can drift from the patch series (the netbsd7 migrator once carried uncommitted debug code): diff the tree file against the patch before building from it. Update the manifest hashes and record stripped sizes per lane either way.

Do NOT rebuild netbsd 6/4LE/4BE toolchain. That takes 3 hours each (9 hours total), and I already set them up with ./build/download.sh && ./build/bootstrap.sh && ./build/downloadoldle.sh && ./build/bootstrapoldle.sh && ./build/downloadoldbe.sh && ./build/bootstrapoldbe.sh 
That is a 3 hour mistake you just made there if you wiped the predownloaded and prebuilt netbsd toolchain.

If you need to add a patch or fix to the Samba 4 source code or build process, add a comment. The build process files should contain expanatory comments explaining why each piece of code is needed.

If you are changing samba code in "build", just build 1 first and validate that one. Don't do all 3 at once if you're going to be validating it with a deploy.

## Device Info
All Apple Time Capsule devices do not support pthread, so keep that in mind. We are just building for that target, so we can probably assume that in our code a bit. 

Some things work in NetBSD 4, others are supported in only NetBSD 6/7. 

The /dev/dk2 (or some other name) disk can be unmounted by Apple at any time to save power! that's why we copy smbd to /mnt/Memory ramdisk! Do not ln binaries that actually live on the disk. The disk is just for long term storage, we copy binaries from there on boot but cannot assume it exists after startup.

We need to reduce the size of the binary so we can fit it on ramdisk. This is critical. The first goal is to make a working binary, but we ideally want to keep it small. Our backup plan is to unmount the 15MB /mnt/Memory ramdisk that Apple provides, and mount it again as a bigger ramdisk, but this is not ideal. Try to strip out the code and keep things small- notice how the old ./build/downloadsamba4.sh code strips out Python. 

Do not remove any tests in `doctor`. 

Device lacks wc tr grep awk cut cksum md5 sha1 stat head, the `time` builtin, and NetBSD 4 lacks scp. NetBSD 4 sed has no `\|`. Use sed and sh and dd. The old NetBSD 4 and 6 devies have a tiny userspace. Do not assume it has a binary you want to use.

On NetBSD 4, `/` (including `/tmp`) is a ~10 MB RAM disk with a few hundred KB free. Put test scratch under `/Volumes/dkN` and clean it up; the `tdb`/`resume`/`resource` migrator regression cases only fit on NetBSD 6.

ACPd starts `afpserver`/`wcifsfs` after `rc.local` has finished, so a boot-time kill is not enough; the manager re-kills them each pass.

This old netbsd 4 system can't load dynamic binaries and load another library; the binary has to be fully static. And we can't add another binary, each binary is 100kb even for a hello world; adding some code to a pre-existing binary adds a few kb. The discovery controller shares the native plan collector with the service helper.

## Runtime

NetBSD 4 `/bin/sh` does not reliably enforce `set -e` inside functions invoked from conditional contexts like `if ! helper; then`; runtime shell helpers must explicitly check critical commands and nested helpers with `|| return 1`.

## Runtime State

Avoid adding new runtime state files. Do not create temporary files to track data such as process PIDs; save that data in process-local variables or use another strategy. They are easy to leave stale, especially on boot scripts, watchdog loops, ramdisks, and device probes. Prefer process-local state such as shell globals, live probes/functions that read the current system state, or another strategy that cannot drift from reality. Only use temporary data files to pass data between processes if absolutely necessary, and ask for approval first. Only add a state file when it is truly required across process boundaries, and document why a global variable or live probe is not enough. Persistent state follows the same rule and must have a documented cross-process justification. Historical 3.x managers may have written the payload-local `xattr-migration-completed.txt` checkpoint; the explicit upgrade operation does not trust it as a completion receipt, and the new runtime neither polls nor writes it. The one approved persistent upgrade state is `/mnt/Flash/xattr-upgrade.state`: only the standalone xattr migrator writes it; deploy reads it and must never certify completion. `xattr.tdb.orphaned.N` files are quarantined metadata and must never be deleted by scripts.

## Device Access

- NetBSD 6 device credentials live in `.env.backup6`
- NetBSD 4 LE device credentials live in `.env.backup4` (verified 2026-09-16: its Apple ELF is LSB and it runs the `bin/discovery-netbsd4le` build; older notes called it BE)
- NetBSD 4 UK device credentials live in `.env.backup4uk` (presumed BE; verify with `file` on first contact)
- The UK NetBSD 4 device is behind an SSH jump host in London, is much slower than the LAN-local NetBSD 6 or NetBSD 4 LE device, and is usually offline
- Never deploy or reboot both LAN devices at the same time: the older router's Ethernet is plugged into the newer one, so rebooting the newer one drops the older one's connection.
- Hard reboot via the Tasmota plug: `curl -X POST 'http://tasmota-41a426-1062.udr7.local/cm?cmnd=Power%20Off'`, wait ~10 s, then the same with `Power%20On`. It is currently plugged into the NetBSD 4 router but may be swapped later; verify which device it feeds before using it. NetBSD 6 gets soft reboots only.
- `pkill -f /mnt/Flash/manager.sh` over ssh kills your own ssh session (its `sh -c` command line matches); kill the manager's PIDs from `ps ax` instead.
- The `plan/spike-2026-09-16/tc4` and `tc6` helpers take the device host (`root@<ip>`) as their first argument.
- If a NetBSD 6 or NetBSD 4 device's IP address in `.env` or its `.env.backup*` file is out of date, use `dns-sd` on the device's local network to discover its current Bonjour address before connecting or deploying.

Run `cp .env.backup6 .env` to change the .env file, and then run `.venv/bin/tcapsule deploy --debug-logging --yes` to deploy it to device. 

Run `.venv/bin/tcapsule doctor` to test the device.

## Telemetry Server

- The telemetry server is at `ssh oracle-instance`
- If server side needs investigation, SSH into that server, go to `~/timecapsulesmb-server`, and read `AGENTS.md` there
- Most telemetry investigations do not need to deal with the server, though. Most of the time just investigate locally. Usually the server only gets involved if there's a telemetry failure; the server is just a simple "convert POST requests to sqlite logs" app.
- To update the server, redeploy via Docker Compose
- The telemetry data is in the SQLite file

## Test Quality

- Do not write shitty tests that just grep for a line of code to check if it exists. Tests should mock the callers etc and check the flow of the code.
- Think about the reason the code exists, what states it has, and what edge cases it has
- Write tests for both the happy path and the edge cases. Write tests for both the positive and negative case. 
- If you see a bad quality test that just greps/asserts/etc for a line of code, analyse the intent behind the test, and re-write it into a real test. 
- For every state, branch, edge case, write a test for it if it makes sense (usually yes)
- Do not randomly remove tests or edit tests. Reason about the correct architechture and intent of the code first, then fix the test (or don't edit the test and fix the code instead)
- Shell-runtime tests cannot override functions defined in `manager.sh` by appending to `common.sh`; stub the `common.sh` function they call, or define the stub after sourcing the extracted library.
- `make test-parallel` has two timing-flaky tests under full load (`test_collector_syscall_failures_and_retries[normal]`, `test_no_debug_means_post_only[false]`); rerun them alone before treating them as failures.

# Info

At the end of every fix, after you say everything, also wrap up with a sentence that can be used as a git commit message.
Run tests from the repo root with `.venv/bin/pytest`. `pyproject.toml` configures pytest with both `.` and `src` on `pythonpath`, so tests can import shared helpers as `tests.*` and the package imports from the working tree.
For full-suite local verification, prefer the parallel runner when practical: `make test-parallel`, or `.venv/bin/pytest -n auto --dist loadfile` if you only need pytest.
Use normal single-process pytest for focused debugging when worker isolation makes failures harder to inspect.

## macOS Swift Tests

The Xcode toolchain may not find XCTest or Swift Testing from plain `swift` invocations unless Xcode's platform framework path is supplied. I verified that `import XCTest` works when this framework search path is passed:

```bash
-F /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/Library/Frameworks
```

The Swift package under `macos/TimeCapsuleSMB` wires this path into the test target so `swift test` can run the macOS helper/client tests.

# mDNSResponder
Since v3.1.0 the runtime never kills Apple's `mDNSResponder`: it is the only responder on the device and ACPd does not respawn it (a reboot is the only recovery). `discoveryd` registers our `_smb`/`_adisk` records through its `dns_sd` IPC. `boot.sh` relaunches Apple's `diskd` as `/sbin/diskd -i lo0 -d local.` so Apple's own `_smb`/`_adisk`/`_afpovertcp` registrations stay on loopback while `diskd` keeps serving `acp -q MaSt` and `diskd.useVolume`. `afpserver` is killed unless `MDNS_ADVERTISE_AFP=1`.

# dns-sd
Doctor must remain Linux compatible and use Python zeroconf as its primary Bonjour decision path. On macOS only, when a family-specific zeroconf pass produces no usable matching result, native `dns-sd` may be used as a bounded fallback decision. It must not override a concrete zeroconf mismatch such as the wrong instance, address, host label, or port. Outside that narrow fallback, keep `dns-sd` diagnostic-only.

Python Bonjour discovery should run IPv4 and IPv6 as separate zeroconf passes and merge the results. Do not default configure or discover to zeroconf `IPVersion.All`; macOS zeroconf has missed AirPort services in that mode.

# 

## Device Safety

- Only ever read ACP on a device: `acp -q <key>` and `acp -A <key>`. Never run `acp notelisten`, `acp rpc`, `acp rc`, `acp rs`, `acp remove`, `acp crash`, `acp corrupt_hfs`, `acp setplistvalue`, or any other `acp` subcommand. The one time `acp notelisten` was run (2026-09-15), the device reset its AirPort settings on the next reboot (`/mnt/Flash` was reformatted; passwords survived). It is not worth the experiment.
- After writing anything under `/mnt/Flash`, run `sync`, wait at least 10 seconds, `sync` again, and only then reboot. An unclean flash filesystem at boot makes the firmware recreate it, which wipes both our runtime and the user's AirPort settings.
- Do not rename files on `/mnt/Flash` or reboot within ~20 minutes of an AirPort Utility change (mode switch, guest network, WAN checkboxes); ACPd is still writing its config on the same partition.
- If an SSH session that ends in `reboot` hangs instead of closing, do not retry; wait for the device.
- `plan/` and `archive/` are git-ignored on purpose and live only on the maintainer's machine; the mDNS redesign plan and the 2026-09-15 device captures are there.

## Native NBNS ownership

`discoveryd` owns a foreground Apple `/sbin/wcifsnd` child and registers machine/workgroup names over loopback UDP 922. Never restart or kill that child independently during routine Samba checks. On controller failure, the manager cleans the orphan and starts a fresh generation. Apple `wcifsfs` remains a conflict; if it returns, stop it and reset the discovery generation because it may have added native name references. Do not restore the deleted custom NBNS responder or its payload. Process titles expose native registration readiness; no PID/status file is used.
