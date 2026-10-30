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

Each release also carries `appcast.xml`, the feed the app's updater reads; it is not for users to download.

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
python3 macos/TimeCapsuleSMB/tools/package_app.py --configuration release --full-validation
```

The app bundles `smbclient` from Homebrew's macOS 14 (Sonoma) bottles, pinned in `HOMEBREW_BOTTLE_ROOTS` in `package_app.py`. Packaging downloads them and their recorded dependencies from ghcr.io; it does not use the packaging Mac's Homebrew, whose bottles are built for that Mac's macOS. Full validation fails if any bundled binary declares a newer minimum macOS than 14.8. To move the pin, pick a version whose bottles still include both `arm64_sonoma` and `sonoma`.

CI packages the universal app on every push, and its "Check App on Intel" job launches the app, its Python modules and the helper on a real Intel Mac (`package_app.py --check-app`, on GitHub's `macos-15-intel` image until GitHub retires it in fall 2027). Nothing else runs the app's x86_64 half on a real Intel Mac before release. `sparkle_appcast.py` refuses an app that is not universal, because Sparkle would install it over a working app on Intel Macs, where it cannot launch. If the app ever drops Intel, pass `--arm64-only` so Intel Macs stop being offered updates. That protects only apps with Sparkle: apps from before Sparkle still download whatever `releases/latest` holds, so the release notes of such a release must say it needs Apple silicon.

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

## Release Builds

A pushed `v*` tag builds the release in CI (`.github/workflows/ci.yml`), after the tests, the packaging check and the Intel check pass for that commit:

1. **Build Release** (`macos-26`) checks the tag with `check_release.py`, sets up a temporary signing keychain and the `tcapsulesmb-notary` profile from the `release` environment's secrets, checks that the Sparkle key belongs to the app (so a wrong certificate, notary key or Sparkle key fails in the first minute), and runs the release command:

   ```bash
   python3 macos/TimeCapsuleSMB/tools/package_app.py --configuration release --arch universal --zip --full-validation --codesign-identity "Developer ID Application: Yushen Chang (M22Z394H44)" --notarize --notary-profile tcapsulesmb-notary
   ```

   It then writes `appcast.xml` with `sparkle_appcast.py`, signing the zip with the Sparkle key. That refuses a key that does not belong to the app's `SUPublicEDKey` and an app that is not universal.
2. **Check Release on Intel** (`macos-15-intel`) launches the signed, notarized app's code on a real Intel Mac (`package_app.py --check-app`) and runs Gatekeeper's assessment (`spctl -a -t exec`) on it.
3. **Create Draft Release** makes a draft release for the tag with `TimeCapsuleSMB.app.zip` and `appcast.xml`. A hidden build's draft is a prerelease.

Nothing reaches users until the draft is published. Never push a `v*` tag to try the workflow: every one starts a release. A run that fails or builds something wrong publishes nothing; fix it, delete the draft (and the tag, if the fix needs a new commit) and push the tag again.

The app checks `https://github.com/jamesyc/TimeCapsuleSMB/releases/latest/download/appcast.xml`, which follows the newest published release that is not a prerelease. Publishing a release with "Set as the latest release" ticked is what offers it to the apps; drafts and prereleases are never offered.

### One-time setup

- A `release` environment in the repository settings whose deployment rule admits only `v*` tags, so only tag runs get its secrets.
- Its secrets: `MACOS_CERTIFICATE_P12` (base64 of the Developer ID Application certificate and key, exported as `.p12`) and `MACOS_CERTIFICATE_PASSWORD`; `NOTARY_KEY_P8` (base64 of an App Store Connect team API key with the Developer role), `NOTARY_KEY_ID` and `NOTARY_ISSUER_ID`; `SPARKLE_ED_PRIVATE_KEY`, the one line `generate_keys -x` exported.
- Keep the exported Sparkle key backed up outside GitHub. Without it no release can ever update the installed apps again; a new key would need a new `SUPublicEDKey` that installed apps do not trust.
- A tag ruleset that lets only admins create, move or delete `v*` tags, since pushing one is what reaches the `release` environment's secrets.

The Developer ID Application certificate expires. Renew it in the Apple Developer account before then, under the same team, export the new one as `.p12`, and set `MACOS_CERTIFICATE_P12` and `MACOS_CERTIFICATE_PASSWORD` again. Apps signed with the old one keep working: their signatures are timestamped. Build Release fails at its signing keychain step when the secret holds no valid identity.

## Packaging The App

`macos/TimeCapsuleSMB/tools/package_app.py` needs `uv`. It installs the app's Python packages from `macos/TimeCapsuleSMB/tools/bundled-requirements.txt` and builds the project with the tools pinned in `build-requirements.txt`, checking everything it downloads by hash. After changing a dependency or `[build-system]` in `pyproject.toml`, or `PYTHON_RUNTIME_VERSION` in `package_app.py`, regenerate both files with `python3 macos/TimeCapsuleSMB/tools/update_python_requirements.py` (Python 3.11 or newer). It keeps the versions already pinned where they still fit; add `--upgrade` to move every pin to the newest version. A new `PYTHON_RUNTIME_VERSION` also needs its package's `PYTHON_RUNTIME_SHA256`: download the package from python.org, check that `pkgutil --check-signature` names `Developer ID Installer: Python Software Foundation (BMM5U3QVKW)`, a team ID a lookalike certificate cannot share, and says Apple notarized it, and only then record `shasum -a 256` of it. Never copy the hash from packaging's mismatch error, which reports whatever was downloaded.

A universal build on Apple silicon runs the smoke test (the helper's requests, the bundled tools, the app's launch and the bundled Python imports) a second time as x86_64, under Rosetta. A Mac without Rosetta skips that pass and says so. Nothing relies on it: the "Check App on Intel" CI job runs the x86_64 half on a real Intel Mac.

## Version Sources

`src/timecapsulesmb/core/release.py` is the only place a version is typed. Every release, hidden builds included, sets `CLI_VERSION` and bumps `CLI_VERSION_CODE`, a plain integer that only goes up; the release tag and the Python package version are derived from `CLI_VERSION`. A hidden build has a `-N` suffix on `CLI_VERSION` and is published as a prerelease.

`.github/scripts/check_release.py TAG --newest-release-py FILE` checks a release before it is built: the tag must be release.py's tag, and `CLI_VERSION_CODE` must be above the code in FILE, the release.py of the newest published release, prereleases included.

`version.json` on `main` is what the apps and the CLI read. Bumping `current_version` and `latest_tag` to the release by hand is optional and always fine: older apps then name the new release in their update notice, and their download link opens the newest release either way. Change `min_supported_version` only to stop unsupported versions. `current_version` must stay at or above `min_supported_version`: clients ignore the file, and so stop blocking unsupported versions, when it does not, so raising the minimum past `current_version` means raising `current_version` with it. `tests/test_release.py` checks this.

## Release Checklist

- Set `CLI_VERSION` and bump `CLI_VERSION_CODE` in `src/timecapsulesmb/core/release.py`.
- Optionally bump `current_version` and `latest_tag` in `version.json` to the release, never for a hidden build. Bump it in the release commit, not earlier, or older apps announce a release that is not published yet.
- Rebuild any changed NetBSD artifacts and update `artifact-manifest.json`.
- For Samba changes, run the device regression tests on NetBSD 6 (full suite) and NetBSD 4 LE (the cases that fit).
- Push the release commit to `main`, then push its tag (`git tag v<version> && git push origin v<version>`).
- When the run has made the draft, download its `TimeCapsuleSMB.app.zip`, unzip it, move the app out of Downloads with Finder (macOS runs a downloaded app from a read-only copy until Finder moves it, and Sparkle cannot update that copy), open it, and check that Check for Updates... is enabled.
- Write the release notes in the draft, with compatibility or flash-safety warnings when applicable.
- Publish with "Set as the latest release" ticked. A hidden build's draft is a prerelease: publish it as a prerelease, never as the latest release, so it never becomes the feed.
- Confirm the asset SHA256 digests are visible on GitHub.

## Updating And Pulling Releases

Sparkle offers a release whose `CFBundleVersion` (`CLI_VERSION_CODE`) is above the running app's. Its Install and Relaunch quits the app the normal way, so during a device operation the app asks first. After Keep Open, Sparkle's documented behavior is to install the update when the app next quits.

To pull a bad release, delete it, or mark the previous release as the latest: the feed then follows that release. That only stops new installs. Sparkle never downgrades, so apps that already installed the bad release need a fixed release with a higher `CLI_VERSION_CODE`.
