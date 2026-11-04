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

People download `TimeCapsuleSMB.dmg` from the GitHub release page: a signed, notarized disk image that holds the app beside a link to Applications. GitHub shows the SHA256 digest of each release asset. Users can check a downloaded image with:

```bash
shasum -a 256 TimeCapsuleSMB.dmg
```

The digest printed by `shasum` should match the `sha256:` value shown for the asset on GitHub.

Each release also carries `TimeCapsuleSMB.app.zip` and `appcast.xml`, which the app's updater reads. Apps from before the updater open the newest release's page, whose notes point to the disk image. The downloaded `.dmg` file shows the generic disk image icon in Finder: a file's own icon does not survive GitHub's upload and a browser download. The mounted volume has the app's icon.

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
uv run macos/TimeCapsuleSMB/tools/package_app.py --configuration release --dmg --full-validation
```

Full validation fails if any bundled binary declares a newer minimum macOS than the app's own, 14.0 (`MINIMUM_MACOS` in `package_app.py`).

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

A pushed `v*` tag builds the release in CI (`.github/workflows/ci.yml`), after the tests, the packaging check, the Intel check and the dependency audit pass for that commit. The audit looks up `pyproject.toml`'s dependencies, resolved for every Python it allows (`uv audit --no-dev`), in the advisory databases. The packages the app bundles are resolved when it is packaged, so `pip-audit` checks them in the packaging job and again in Build Release, from the `python-packages.txt` that packaging writes beside the app. On other pushes a finding marks its step failed without failing the run. A tag with a finding builds nothing until the dependency is upgraded or the step that found it ignores the advisory (`--ignore ID` for `uv audit`, `--ignore-vuln ID` for `pip-audit`) with a comment that says why it does not affect users. The release steps:

1. **Build Release** (`macos-26`) checks the tag with `check_release.py`, sets up a temporary signing keychain and the `tcapsulesmb-notary` profile from the `release` environment's secrets, checks that the Sparkle key belongs to the app (so a wrong certificate, notary key or Sparkle key fails in the first minute), and runs the release command:

   ```bash
   uv run macos/TimeCapsuleSMB/tools/package_app.py --configuration release --arch universal --zip --dmg --full-validation --codesign-identity "Developer ID Application: Yushen Chang (M22Z394H44)" --notarize --notary-profile tcapsulesmb-notary
   ```

   After notarizing and stapling the app, it zips it and builds `TimeCapsuleSMB.dmg` from it, signs the image, notarizes it and staples its ticket, and checks that Gatekeeper accepts it (`spctl -a -t open --context context:primary-signature`). It then audits the bundled packages and writes `appcast.xml` with `sparkle_appcast.py`, signing the zip with the Sparkle key. That refuses a key that does not belong to the app's `SUPublicEDKey` and an app that is not universal.
2. **Check Release on Intel** (`macos-15-intel`) launches the signed, notarized app's code on a real Intel Mac (`package_app.py --check-app`), runs Gatekeeper's assessment (`spctl -a -t exec`) on it, and assesses the disk image as Gatekeeper does when it is opened.
3. **Create Draft Release** makes a draft release for the tag with `TimeCapsuleSMB.dmg`, `TimeCapsuleSMB.app.zip` and `appcast.xml`. A hidden build's draft is a prerelease.

Nothing reaches users until the draft is published. Never push a `v*` tag to try the workflow: every one starts a release. A run that fails or builds something wrong publishes nothing; fix it, delete the draft (and the tag, if the fix needs a new commit) and push the tag again.

The app checks `https://github.com/jamesyc/TimeCapsuleSMB/releases/latest/download/appcast.xml`, which follows the newest published release that is not a prerelease. Publishing a release with "Set as the latest release" ticked is what offers it to the apps; drafts and prereleases are never offered. Most apps then download it in the background at their next launch, without asking.

The update window shows the release's notes from its `release-notes.md`, which `appcast.xml` links to. Sparkle shows a `.md` file in its own text view, with headings, lists and links, where a link to the release page would show GitHub's whole page. `.github/workflows/release-notes.yml` writes that file from the notes in the release form when the release is published and again after every edit, so edit the notes only there. An update window opened in the seconds between publishing and the upload shows no notes.

The Homebrew tap, `jamesyc/homebrew-tap`, updates itself. Its own workflow, `update.yml`, runs daily: when the latest release has a `TimeCapsuleSMB.dmg` the tap does not have yet, it writes the version and the SHA256 of the disk image and of the tag's source tarball into the cask and formula in this repo's `homebrew/`, taken from `main`, adds the formula's Python packages with `brew update-python-resources`, and commits both to the tap once `brew audit --strict --online`, a source install of the formula and `brew test` pass. It needs no secret. Run it by hand after publishing so the tap gets the release at once, and with a tag to write a release again after fixing `homebrew/` on `main`:

```bash
gh workflow run update.yml -R jamesyc/homebrew-tap
gh workflow run update.yml -R jamesyc/homebrew-tap -f tag=v<version>
```

GitHub turns a public repository's schedule off after 60 days without a commit. Each run of `update.yml` enables the workflow again through GitHub's API, which restarts that count, so the daily run keeps itself on. If it is ever off, turn it back on in the tap's Actions tab.

### One-time setup

- A `release` environment in the repository settings whose deployment rule admits only `v*` tags, so only tag runs get its secrets.
- Its secrets: `MACOS_CERTIFICATE_P12` (base64 of the Developer ID Application certificate and key, exported as `.p12`) and `MACOS_CERTIFICATE_PASSWORD`; `NOTARY_KEY_P8` (base64 of an App Store Connect team API key with the Developer role), `NOTARY_KEY_ID` and `NOTARY_ISSUER_ID`; `SPARKLE_ED_PRIVATE_KEY`, the one line `generate_keys -x` exported. Set each with `gh secret set NAME --env release` and paste the value at its prompt, or pipe it in with `printf '%s'`. `echo` adds a newline. Build Release strips whitespace from `NOTARY_KEY_ID` and `NOTARY_ISSUER_ID`; of the other secrets, a trailing newline breaks only `MACOS_CERTIFICATE_PASSWORD`.
- Keep the exported Sparkle key backed up outside GitHub. Without it no release can ever update the installed apps again; a new key would need a new `SUPublicEDKey` that installed apps do not trust.
- Keep that key as private as the Developer ID certificate. Apps download an update and install it when they quit, with no click, so anyone with the key who can also change the feed reaches every app that opens with automatic download on.
- A tag ruleset that lets only admins create, move or delete `v*` tags, since pushing one is what reaches the `release` environment's secrets.

The Developer ID Application certificate expires. Renew it in the Apple Developer account before then, under the same team, export the new one as `.p12`, and set `MACOS_CERTIFICATE_P12` and `MACOS_CERTIFICATE_PASSWORD` again. Apps signed with the old one keep working: their signatures are timestamped. Build Release fails at its signing keychain step when the secret holds no valid identity, and lists what the keychain got under "Signing keychain contents": a certificate with another name means the wrong certificate was exported, a certificate with no identity means the `.p12` lacks its private key, and an identity listed with a reason such as `CSSMERR_TP_CERT_EXPIRED` is expired or untrusted.

Export the `.p12` from Keychain Access's login keychain, under "My Certificates": select the "Developer ID Application" row, whose private key shows beneath it, then File > Export Items. Exported from the plain "Certificates" list, the file holds the certificate without its key. It imports without an error, and Build Release's keychain step then reports that the secret holds no valid identity. Keep the file in the format Keychain Access writes. macOS imports only the legacy `.p12` encryption and reports any other as "MAC verification failed (wrong password?)", the same error as a wrong password. OpenSSL 3 writes the newer format by default, so a `.p12` rebuilt with `openssl pkcs12 -export` needs `-legacy`.

## Packaging The App

Run `macos/TimeCapsuleSMB/tools/package_app.py` with `uv run`, which gives it the Python 3.15 its header asks for, on an Apple silicon Mac: zeroconf publishes its Python 3.15 macOS wheels for arm64 only, and packaging installs wheels only. Nothing is pinned. Each build bundles the newest final Python 3.15 that python.org publishes as `python-3.15.N-macos11.pkg`, and only when `pkgutil --check-signature` names `Developer ID Installer: Python Software Foundation (BMM5U3QVKW)`, a team ID a lookalike certificate cannot share, and Gatekeeper accepts the package (`spctl -a -t install`). It then installs the newest releases of the project's dependencies and certifi that have wheels, and builds the project with the newest build tools. Its uv commands ignore uv's configuration files and take only releases at least a day old. The build log names the bundled Python and lists the packages, and `dist/python-packages.txt` holds both: a `# Python 3.15.N` line, then `name==version` lines. CI keys its package cache on that file, `package_app.py` and the app icon, so a new Python or package release saves a new cache and a change to the sources alone does not. To move to the next Python series, change `PYTHON_RUNTIME_SERIES` in `package_app.py` and the `requires-python` in its header and `sparkle_appcast.py`'s; a test fails until all three agree.

A universal build on Apple silicon runs the smoke test (the helper's requests, the app's launch and the bundled Python imports) a second time as x86_64, under Rosetta. A Mac without Rosetta skips that pass and says so. Nothing relies on it: the "Check App on Intel" CI job runs the x86_64 half on a real Intel Mac.

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
- When the run has made the draft, download its `TimeCapsuleSMB.dmg`, open it, drag the app to Applications, open it from there, wait a few seconds for its launch check, and check that Check for Updates... is enabled. An app run from the disk image itself is read-only, and Sparkle cannot update it.
- Write the release notes in the draft, with compatibility or flash-safety warnings when applicable.
- Publish with "Set as the latest release" ticked. A hidden build's draft is a prerelease: publish it as a prerelease, never as the latest release, so it never becomes the feed.
- Confirm the asset SHA256 digests are visible on GitHub, and that the Release notes workflow added `release-notes.md`.
- Open an older copy of the app with `open TimeCapsuleSMB.app --args -SUAutomaticallyUpdate YES` and check that it downloads the release and opens the update window. Without that argument, a Mac whose box was ever unticked tests only the ask-first path. The feed serves only published releases, so a problem found here needs a fixed release.
- Run the tap's `update.yml` (above) and check that it committed the release to `jamesyc/homebrew-tap`.

## Updating And Pulling Releases

Sparkle offers a release whose `CFBundleVersion` (`CLI_VERSION_CODE`) is above the running app's. The app checks at every launch and once a day while it stays open, or once a week while an installer waits. Unless the user unticks "Automatically download and install updates in the future", the update downloads silently, then a window offers Install and Relaunch, Install on Quit and Skip This Version, and quitting without an answer installs it. With the box unticked, the window asks before downloading, and Remind Me Later lasts until the next launch, or the next daily check if the app stays open, because each launch clears Sparkle's last check time.

If the user cannot write to the app's folder, the silent download cannot install. Sparkle shows Install Update, Remind Me Later and Skip This Version instead, it asks for authorization on Install Update, and quitting installs nothing. Sparkle keeps that download only in memory, so every launch then asks before downloading until the app reaches that version. The rest of that launch asks too, so declining the password prompt does not download again at the next daily check. The cost is one silent download per release.

Either window waits while the app is behind other apps, while a text field has focus, while a dialog is open and while a device operation runs. The app rechecks every 5 seconds, and Check for Updates... opens it at once. There is no time limit, because a held window loses nothing: quitting still installs, and a window with nothing ready returns at the next launch. Sparkle cannot resume a download, so if the app quits during a silent one, or it fails, the next launch asks first and shows its progress, once, unless the app already reached that version.

`UpdateFlow` (`macos/TimeCapsuleSMB/Sources/TimeCapsuleSMBApp/App/UpdateFlow.swift`) is the app's side of an update, as one state machine. Sparkle owns the session and the installer process. Each Sparkle callback is an event, and only the flow's actions start a follow-up check, open the window, run the recheck timer or write `UpdateMemory`, which is what a launch remembers about a silent update that reached no installer. Window phases carry Sparkle's stage (`notDownloaded`, `downloaded` or `installing`), so the flow knows from Sparkle what a window's buttons do and what quitting does. Its `step` function has no default case, so a new phase or event does not compile until every pair has an answer.

| Phase | Meaning | Leaves when |
| --- | --- | --- |
| `idle` | Nothing in progress | A silent download starts, or Sparkle has a window |
| `downloading` | Silent download, then the installer unpacking and validating it, memory says interrupted | The installer is ready, the cycle ends, or the app needs authorization and Sparkle has a `downloaded` window |
| `installerReady` | Installer ready, memory and last failure cleared, cycle still ending | The cycle ends |
| `reprobing` | The one follow-up check that finds the installer | Sparkle has a window, or the check ends without one: to `idle` if it found no update, otherwise `installerWaiting` |
| `windowHeld` | Sparkle built a window and left opening it to the app | It is shown, or the session ends: to `installerWaiting` at `installing`, otherwise `idle` |
| `windowOpen` | The window or Sparkle's progress window is up | Install on Quit, a relaunch request, Skip This Version, or the session ends |
| `installerWaiting` | The installer runs when the app quits. Includes a relaunch that Keep Open or a dialog cancelled, when Sparkle closes its window but keeps the session | The weekly reminder, Check for Updates..., a new download if the installer died, or a check that finds no update, because Sparkle resumes a live installer without looking for one |

The doc comment on `UpdateFlow` lists the rules it keeps for any order of events.

Sparkle is the truth. A window, cycle or install event the model did not predict corrects the phase, because a dropped window event would leave a window nothing opens. A download event it did not predict says nothing about the phase, so the flow ignores it. Both count as drift and log a fault. The diagnostics export shows `Update Flow`, such as `windowHeld installing 30900 for 12 min, 1 drift`, with the age worked out at export, plus `Update Memory`, `Skipped Update` and `Last Update Error`. `Automatic Update Installs` is the user's saved choice even on a launch that asks first. `Skipped Update` shows a version only while the app has not passed it. `Last Update Error` keeps the last real failure across launches, with its date and app version, until an installer is ready or a newer app starts. Phase changes stay in the system log, so `log show` works after the fact. To watch them live:

```bash
log stream --predicate 'subsystem == "com.timecapsulesmb.TimeCapsuleSMB" AND category == "updates"'
```

`UpdateFlowTests` checks every phase against every event from a table, and thousands of seeded random event orders against the rules. Change the table with the code, and add a rule there when you add one to `UpdateFlow`.

Reopening the app within about a second of quitting, while the installer swaps the bundle, can do nothing: `open` reports success and no process starts. The install still finishes, and reopening 2 seconds after quitting worked.

A change to any of this reaches users only from the release after the one that ships it, because the installed app runs each update.

After any Sparkle bump, run these flows again on a packaged build. The app depends on Sparkle's `SULastCheckTime` key, on `checkForUpdates()` opening a waiting window and on its gentle reminder delegate methods, and the tests catch only a changed method signature or stage name. Package the app, lower `CFBundleVersion` and `CFBundleShortVersionString` with `plutil`, sign it with `codesign --force --sign -`, and open it with `--args -SUAutomaticallyUpdate YES`: it takes the real feed. Check that the window opens by itself, waits with a text field focused, and that Install and Relaunch, Install on Quit and Skip This Version work. Skip This Version saves `SUSkippedVersion`, so delete that key afterwards.

Install and Relaunch quits the app the normal way, so during a device operation the app asks first. After Keep Open, Sparkle closes its window and keeps the session, and it installs the update when the app next quits, and relaunches it. While one of the app's own confirmation dialogs is open, such as Install / Update's, macOS refuses the quit without asking, and the update likewise waits for the next quit.

To pull a bad release, delete it, or mark the previous release as the latest: the feed then follows that release. That only stops new installs, and by then apps with automatic download on that were opened since publishing have the bad release downloaded. Sparkle never downgrades, so apps that installed it need a fixed release with a higher `CLI_VERSION_CODE`.
