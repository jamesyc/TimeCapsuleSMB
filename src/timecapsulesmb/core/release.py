from __future__ import annotations

# Update this version info for each release, including beta releases.
CLI_VERSION = "3.1.1"
RELEASE_TAG = "v3.1.1"
CLI_VERSION_CODE = 30101
SAMBA_VERSION = "4.25.0rc2"


def release_major(version_code: int) -> int:
    """The major release of a CLI_VERSION_CODE: 30101 is v3.1.1, major 3."""
    return version_code // 10000
