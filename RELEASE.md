# Release Verification

TimeCapsuleSMB releases contain the macOS app bundle, the Python CLI, boot scripts, and checked-in static NetBSD binaries used by deploy. The release process should make it clear which artifacts were shipped and how to verify them.

## Release Assets

The primary user-facing release asset is `TimeCapsuleSMB.app.zip` on the GitHub release page. GitHub shows the SHA256 digest for uploaded release assets in the asset metadata. Users can verify a downloaded app zip with:

```bash
shasum -a 256 TimeCapsuleSMB.app.zip
```

The digest printed by `shasum` should match the `sha256:` value shown for the asset on GitHub.

## Checked-In Device Artifacts

The deploy flow uses the binaries checked into `bin/`:

| Device family | Samba binary | mDNS binary | NBNS binary |
| --- | --- | --- | --- |
| NetBSD 6 / 7 | `bin/samba4/smbd` | `bin/mdns/mdns-advertiser` | `bin/nbns/nbns-advertiser` |
| NetBSD 4 little-endian | `bin/samba4-netbsd4le/smbd` | `bin/mdns-netbsd4le/mdns-advertiser` | `bin/nbns-netbsd4le/nbns-advertiser` |
| NetBSD 4 big-endian | `bin/samba4-netbsd4be/smbd` | `bin/mdns-netbsd4be/mdns-advertiser` | `bin/nbns-netbsd4be/nbns-advertiser` |

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

For mDNS and NBNS advertisers, run the helper scripts from the repo root on the NetBSD VM:

```bash
./build/mdns.sh && ./build/mdnsoldle.sh && ./build/mdnsoldbe.sh && ./build/nbns.sh && ./build/nbnsoldle.sh && ./build/nbnsoldbe.sh
```

For Samba 4.x, build and validate one lane first when changing Samba source or build logic:

```bash
./build/downloadsamba4x.sh && ./build/samba4x.sh
./build/downloadsamba4xoldle.sh && ./build/samba4xoldle.sh
./build/downloadsamba4xoldbe.sh && ./build/samba4xoldbe.sh
```

Do not run underscore-prefixed helper scripts directly.

## Signing And Notarization

The macOS app packaging flow supports Developer ID signing and notarization when the relevant signing environment is configured. A public release should state whether the attached app zip is notarized. When notarization is enabled, the package validation step should complete successfully before the release asset is uploaded.

## Release Checklist

- Update `version.json` and `pyproject.toml` to the release version.
- Rebuild any changed NetBSD artifacts and update `artifact-manifest.json`.
- Run the artifact manifest tests.
- Run the Python and Swift test suites.
- Package and validate the macOS app.
- Upload `TimeCapsuleSMB.app.zip` to the GitHub release.
- Confirm the uploaded asset SHA256 digest is visible on GitHub.
- Include user-facing release notes with compatibility or flash-safety warnings when applicable.
