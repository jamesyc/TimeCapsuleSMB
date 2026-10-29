# Release Verification

TimeCapsuleSMB releases contain the macOS app bundle, the Python CLI, boot scripts, and checked-in static NetBSD binaries used by deploy. The release process should make it clear which artifacts were shipped and how to verify them.

## Samba 4.25.0

The current Samba build is pinned to `samba-4.25.0`. This brings in upstream
stream parent-directory resolution and AFP_AfpInfo stat fixes. The downstream
series fixes NetBSD stream extent error handling, so deleting a file or directory
with metadata does not fail after removing only its primary stream. It also
preserves lookup and I/O failures instead of reporting false success. 4.25.0
does not include the extent fix, so the series still carries it.

The existing NetBSD SDKs are retained. A const-preserving charset fallback
supports their older GCC versions, and the embedded srvsvc and durable-cookie
patches are adapted to 4.25's APIs. `streams_xattr:max xattrs per stream = 35`
remains necessary.

When moving to a later Samba release, reapply the series to its tag, check which
downstream fixes are now upstream, and repeat the Samba regression and macOS
mounted-share checks documented in `tests/samba/README.md`.

## Release Assets

The primary user-facing release asset is `TimeCapsuleSMB.app.zip` on the GitHub release page. GitHub shows the SHA256 digest for uploaded release assets in the asset metadata. Users can verify a downloaded app zip with:

```bash
shasum -a 256 TimeCapsuleSMB.app.zip
```

The digest printed by `shasum` should match the `sha256:` value shown for the asset on GitHub.

## Checked-In Device Artifacts

The deploy flow uses the binaries checked into `bin/`:

| Device family | Samba binary | Native service |
| --- | --- | --- |
| NetBSD 6 / 7 | `bin/samba4/smbd` | `bin/service/service` |
| NetBSD 4 little-endian | `bin/samba4-netbsd4le/smbd` | `bin/service-netbsd4le/service` |
| NetBSD 4 big-endian | `bin/samba4-netbsd4be/smbd` | `bin/service-netbsd4be/service` |

Every checked-in device artifact must have a matching entry in `src/timecapsulesmb/assets/artifact-manifest.json`. The manifest stores the repo-relative path and SHA256 digest used by deploy-time validation.

Before tagging a release, run:

```bash
.venv/bin/pytest tests/test_artifacts.py tests/test_artifact_resolver.py
```

For a full local release check, run:

```bash
make test-parallel
swift test --package-path macos/TimeCapsuleSMB
python3 macos/TimeCapsuleSMB/tools/package_app.py --configuration release --arch native --full-validation
```

The app bundles `smbclient` from Homebrew's macOS 14 (Sonoma) bottles, pinned in `HOMEBREW_BOTTLE_ROOTS` in `package_app.py`. Packaging downloads them and their recorded dependencies from ghcr.io; it does not use the packaging Mac's Homebrew, whose bottles are built for that Mac's macOS. Full validation fails if any bundled binary declares a newer minimum macOS than 14.8. To move the pin, pick a version whose bottles still include both `arm64_sonoma` and `sonoma`.

## NetBSD Builds

When a change touches `build/`, rebuild the affected NetBSD artifact before release. Do not rebuild the NetBSD toolchains unless that is the explicit task. After a successful root build on the VM, copy the stripped binary back into `bin/`, wait a few seconds for filesystem state to settle, then update `src/timecapsulesmb/assets/artifact-manifest.json`.

For the unified native service, run the helper scripts from the repo root on the NetBSD VM:

```bash
./build/service.sh && ./build/serviceoldle.sh && ./build/serviceoldbe.sh
```

For Samba 4.x, `./build/samba4xall.sh` installs the lanes' VM packages (bison, Parse::Yapp, ccache), then downloads and builds all three lanes at once (the default); name lanes to build only those (each once). Ctrl-C stops every lane, and each lane ends with a `ccache (lane): N hits, M misses` line. When changing Samba source or build logic, build and validate one lane first:

```bash
./build/samba4xall.sh            # NetBSD 6, NetBSD 4 LE and NetBSD 4 BE together
./build/samba4xall.sh 4le        # one lane: 6, 4le or 4be
```

Each lane compiles through ccache (`SAMBA4X_CCACHE`, `CCACHE_DIR` and `CCACHE_MAXSIZE` in `build/env.sh`; `SAMBA4X_CCACHE=''` turns it off) with `SAMBA4X_JOBS` (4) jobs. A clean build of all three lanes takes about 3.5 minutes with a warm cache and about 7 minutes with an empty one; the output is the same either way. `.venv/bin/python -m tests.samba.vm_build` runs the same parallel build from the Mac (`--lanes` picks lanes).

Do not run underscore-prefixed helper scripts directly.

Run the downstream Samba regression tests on the devices before a release that changes Samba patches, the build flags or the linker layout. Some cases only prove anything on Apple's kernels: `nofollow_errno` (NetBSD's EFTYPE for `O_NOFOLLOW`) and `data_page_writes` (the UVM fault-ahead bug) pass on any host. The build copies each test to the device named in `build/.env` and stops before staging `smbd` if one fails; point the scratch directory at a mounted data volume:

```bash
SAMBA4X_CROSS_EXEC_REMOTE_DIR=/Volumes/dkN SAMBA4X_RUN_REGRESSION_TESTS=1 ./build/samba4x.sh
```

On NetBSD 4 LE, build the tests with `SAMBA4X_BUILD_REGRESSION_TESTS=1 ./build/samba4xoldle.sh` and run every case except the `tc_xattr_migrate_test` `tdb`, `resume`, `resource` and `all` cases (they need more space than NetBSD 4's root RAM disk has) through `./build/samba4-cross-exec.sh` with `CROSS_EXEC_REMOTE_DIR=/Volumes/dkN`. Check that the NetBSD 4 host, password and SSH ProxyCommand in the `.env` it uses are current first.

## Signing And Notarization

The macOS app packaging flow supports Developer ID signing and notarization when the relevant signing environment is configured. A public release should state whether the attached app zip is notarized. When notarization is enabled, the package validation step should complete successfully before the release asset is uploaded.

## Version Sources

`src/timecapsulesmb/core/release.py` is the only place a version is typed. Every release, hidden builds included, sets `CLI_VERSION` and bumps `CLI_VERSION_CODE`, a plain integer that only goes up; the release tag and the Python package version are derived from `CLI_VERSION`. A hidden build has a `-N` suffix on `CLI_VERSION` and is published as a prerelease.

`.github/scripts/check_release.py TAG --newest-release-py FILE` checks a release before it is built: the tag must be release.py's tag, and `CLI_VERSION_CODE` must be above the code in FILE, the release.py of the newest published release, prereleases included.

`version.json` on `main` is what the apps and the CLI read. Bumping `current_version` and `latest_tag` to the release by hand is optional and always fine: older apps then name the new release in their update notice, and their download link opens the newest release either way. Change `min_supported_version` only to stop unsupported versions. `current_version` must stay at or above `min_supported_version`: clients ignore the file, and so stop blocking unsupported versions, when it does not, so raising the minimum past `current_version` means raising `current_version` with it. `tests/test_release.py` checks this.

## Release Checklist

- Set `CLI_VERSION` and bump `CLI_VERSION_CODE` in `src/timecapsulesmb/core/release.py`.
- Optionally bump `current_version` and `latest_tag` in `version.json` to the release, never for a hidden build. Bump it in the release commit, not earlier, or older apps announce a release that is not published yet.
- Rebuild any changed NetBSD artifacts and update `artifact-manifest.json`.
- For Samba changes, run the device regression tests on NetBSD 6 (full suite) and NetBSD 4 LE (the cases that fit).
- Run the artifact manifest tests.
- Run the Python and Swift test suites.
- Package and validate the macOS app.
- Upload `TimeCapsuleSMB.app.zip` to the GitHub release.
- Publish with "Set as the latest release" ticked, except for a hidden build, which is published as a prerelease.
- Confirm the uploaded asset SHA256 digest is visible on GitHub.
- Include user-facing release notes with compatibility or flash-safety warnings when applicable.
