# Release Verification

TimeCapsuleSMB releases contain the macOS app bundle, the Python CLI, boot scripts, and checked-in static NetBSD binaries used by deploy. The release process should make it clear which artifacts were shipped and how to verify them.

## Samba 4.25.0rc2 candidate

The current Samba build is pinned to `samba-4.25.0rc2`. This brings in upstream
stream parent-directory resolution and AFP_AfpInfo stat fixes. The downstream
series fixes NetBSD stream extent error handling, so deleting a file or directory
with metadata does not fail after removing only its primary stream. It also
preserves lookup and I/O failures instead of reporting false success.

The existing NetBSD SDKs are retained. A const-preserving charset fallback
supports their older GCC versions, and the embedded srvsvc and durable-cookie
patches are adapted to rc2's APIs. `streams_xattr:max xattrs per stream = 35`
remains necessary with this release candidate.

Before switching to 4.25 final, reapply the series to the final tag, check whether
the extent fix is incorporated upstream, and repeat the Samba regression and
macOS mounted-share checks documented in `tests/samba/README.md`.

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

## NetBSD Builds

When a change touches `build/`, rebuild the affected NetBSD artifact before release. Do not rebuild the NetBSD toolchains unless that is the explicit task. After a successful root build on the VM, copy the stripped binary back into `bin/`, wait a few seconds for filesystem state to settle, then update `src/timecapsulesmb/assets/artifact-manifest.json`.

For the unified native service, run the helper scripts from the repo root on the NetBSD VM:

```bash
./build/service.sh && ./build/serviceoldle.sh && ./build/serviceoldbe.sh
```

For Samba 4.x, build and validate one lane first when changing Samba source or build logic:

```bash
./build/downloadsamba4x.sh && ./build/samba4x.sh
./build/downloadsamba4xoldle.sh && ./build/samba4xoldle.sh
./build/downloadsamba4xoldbe.sh && ./build/samba4xoldbe.sh
```

Do not run underscore-prefixed helper scripts directly.

Run the downstream Samba regression tests on the devices before a release that changes Samba patches, the build flags or the linker layout. Some cases only prove anything on Apple's kernels: `nofollow_errno` (NetBSD's EFTYPE for `O_NOFOLLOW`) and `data_page_writes` (the UVM fault-ahead bug) pass on any host. The build copies each test to the device named in `build/.env` and stops before staging `smbd` if one fails; point the scratch directory at a mounted data volume:

```bash
SAMBA4X_CROSS_EXEC_REMOTE_DIR=/Volumes/dkN SAMBA4X_RUN_REGRESSION_TESTS=1 ./build/samba4x.sh
```

On NetBSD 4 LE, build the tests with `SAMBA4X_BUILD_REGRESSION_TESTS=1 ./build/samba4xoldle.sh` and run every case except the `tc_xattr_migrate_test` `tdb`, `resume`, `resource` and `all` cases (they need more space than NetBSD 4's root RAM disk has) through `./build/samba4-cross-exec.sh` with `CROSS_EXEC_REMOTE_DIR=/Volumes/dkN`. Check that the NetBSD 4 host, password and SSH ProxyCommand in the `.env` it uses are current first.

## Signing And Notarization

The macOS app packaging flow supports Developer ID signing and notarization when the relevant signing environment is configured. A public release should state whether the attached app zip is notarized. When notarization is enabled, the package validation step should complete successfully before the release asset is uploaded.

## Release Checklist

- Update `version.json`, `pyproject.toml`, and `src/timecapsulesmb/core/release.py` to the release version.
- Rebuild any changed NetBSD artifacts and update `artifact-manifest.json`.
- For Samba changes, run the device regression tests on NetBSD 6 (full suite) and NetBSD 4 LE (the cases that fit).
- Run the artifact manifest tests.
- Run the Python and Swift test suites.
- Package and validate the macOS app.
- Upload `TimeCapsuleSMB.app.zip` to the GitHub release.
- Confirm the uploaded asset SHA256 digest is visible on GitHub.
- Include user-facing release notes with compatibility or flash-safety warnings when applicable.
