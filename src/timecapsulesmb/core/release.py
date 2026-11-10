from __future__ import annotations

# Update this version info for each release, including beta releases.
CLI_VERSION = "3.8.6"
RELEASE_TAG = f"v{CLI_VERSION}"
CLI_VERSION_CODE = 30806
# Apps below this code see the update as critical, without Skip or Remind Me
# Later. 0 for none. The appcast holds one release, so the floor stays set across
# releases: lowering it forgets the apps that missed that release.
CRITICAL_BELOW_VERSION_CODE = 0
SAMBA_VERSION = "4.25.0"


def release_major(version_code: int) -> int:
    """The major release of a CLI_VERSION_CODE: 30101 is v3.1.1, major 3."""
    return version_code // 10000
