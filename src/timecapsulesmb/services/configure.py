from __future__ import annotations

from timecapsulesmb.core.config import DEFAULTS, parse_bool, preserved_env_file_values


def build_configure_env_values(
    existing: dict[str, str],
    *,
    host: str,
    password: str,
    ssh_opts: str,
    configure_id: str,
    internal_share_use_disk_root: bool | None = None,
    any_protocol: bool | None = None,
) -> dict[str, str]:
    values = preserved_env_file_values(existing)
    values.update({
        "TC_HOST": host,
        "TC_PASSWORD": password,
        "TC_SSH_OPTS": ssh_opts,
        "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true" if (
            parse_bool(existing.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"]))
            if internal_share_use_disk_root is None
            else internal_share_use_disk_root
        ) else "false",
        "TC_ANY_PROTOCOL": "true" if (
            parse_bool(existing.get("TC_ANY_PROTOCOL", DEFAULTS["TC_ANY_PROTOCOL"]))
            if any_protocol is None
            else any_protocol
        ) else "false",
        "TC_CONFIGURE_ID": configure_id,
    })
    return values
