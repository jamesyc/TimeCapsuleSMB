from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from timecapsulesmb.core.config import DEFAULTS, shell_quote
from timecapsulesmb.device.compat import PAYLOAD_FAMILY_NETBSD6, is_netbsd4_payload_family


@dataclass(frozen=True)
class TemplateBundle:
    start_script_replacements: dict[str, str]
    watchdog_replacements: dict[str, str]
    smbconf_replacements: dict[str, str]


def cache_directory_replacements(payload_family: str, payload_dir_name: str) -> tuple[str, str]:
    if is_netbsd4_payload_family(payload_family):
        return (
            "$PAYLOAD_DIR/cache",
            "__PAYLOAD_DIR__/cache",
        )
    return ("/mnt/Memory/samba4/var", "/mnt/Memory/samba4/var")


def load_boot_asset_text(name: str) -> str:
    return resources.files("timecapsulesmb.assets.boot.samba4").joinpath(name).read_text()


def render_template_text(content: str, replacements: dict[str, str]) -> str:
    for key, value in replacements.items():
        content = content.replace(key, value)
    return content


def render_template(name: str, replacements: dict[str, str]) -> str:
    return render_template_text(load_boot_asset_text(name), replacements)


def write_boot_asset(name: str, destination: Path) -> None:
    destination.write_text(load_boot_asset_text(name))


def build_template_bundle(
    values: dict[str, str],
    *,
    adisk_disk_key: str = "dk0",
    adisk_uuid: str = "",
    payload_family: str = PAYLOAD_FAMILY_NETBSD6,
    debug_logging: bool = False,
    data_root: str | None = None,
    share_use_disk_root: bool = False,
) -> TemplateBundle:
    device_model = values.get("TC_MDNS_DEVICE_MODEL", DEFAULTS["TC_MDNS_DEVICE_MODEL"])
    start_cache_directory, smbconf_cache_directory = cache_directory_replacements(
        payload_family,
        values["TC_PAYLOAD_DIR_NAME"],
    )
    smbd_log_file = "/mnt/Memory/samba4/var/log.smbd"
    smbd_max_log_size = "256"
    smbd_log_level_line = ""
    mdns_log_enabled = "0"
    mdns_log_file = "/mnt/Memory/samba4/var/mdns.log"
    if debug_logging:
        if not data_root:
            raise ValueError("data_root is required when debug_logging is enabled")
        smbd_log_file = f"{data_root}/samba4-logs/log.smbd"
        smbd_max_log_size = "1048576"
        smbd_log_level_line = "\n    log level = 5 vfs:8 fruit:8"
        mdns_log_enabled = "1"
    return TemplateBundle(
        start_script_replacements={
            "__PAYLOAD_DIR_NAME__": shell_quote(values["TC_PAYLOAD_DIR_NAME"]),
            "__CACHE_DIRECTORY__": start_cache_directory,
            "__SMB_SHARE_NAME__": shell_quote(values["TC_SHARE_NAME"]),
            "__SMB_NETBIOS_NAME__": shell_quote(values["TC_NETBIOS_NAME"]),
            "__NET_IFACE__": shell_quote(values["TC_NET_IFACE"]),
            "__MDNS_INSTANCE_NAME__": shell_quote(values["TC_MDNS_INSTANCE_NAME"]),
            "__MDNS_HOST_LABEL__": shell_quote(values["TC_MDNS_HOST_LABEL"]),
            "__MDNS_DEVICE_MODEL__": shell_quote(device_model),
            "__AIRPORT_SYAP__": shell_quote(values.get("TC_AIRPORT_SYAP", DEFAULTS["TC_AIRPORT_SYAP"])),
            "__ADISK_DISK_KEY__": shell_quote(adisk_disk_key),
            "__ADISK_UUID__": shell_quote(adisk_uuid),
            "__MDNS_LOG_ENABLED__": mdns_log_enabled,
            "__MDNS_LOG_FILE__": shell_quote(mdns_log_file),
            "__SHARE_USE_DISK_ROOT__": "true" if share_use_disk_root else "false",
        },
        watchdog_replacements={
            "__SMB_SHARE_NAME__": shell_quote(values["TC_SHARE_NAME"]),
            "__SMB_NETBIOS_NAME__": shell_quote(values["TC_NETBIOS_NAME"]),
            "__NET_IFACE__": shell_quote(values["TC_NET_IFACE"]),
            "__MDNS_INSTANCE_NAME__": shell_quote(values["TC_MDNS_INSTANCE_NAME"]),
            "__MDNS_HOST_LABEL__": shell_quote(values["TC_MDNS_HOST_LABEL"]),
            "__MDNS_DEVICE_MODEL__": shell_quote(device_model),
            "__AIRPORT_SYAP__": shell_quote(values.get("TC_AIRPORT_SYAP", DEFAULTS["TC_AIRPORT_SYAP"])),
            "__ADISK_DISK_KEY__": shell_quote(adisk_disk_key),
            "__ADISK_UUID__": shell_quote(adisk_uuid),
            "__MDNS_LOG_ENABLED__": mdns_log_enabled,
            "__MDNS_LOG_FILE__": shell_quote(mdns_log_file),
        },
        smbconf_replacements={
            "__PAYLOAD_DIR_NAME__": values["TC_PAYLOAD_DIR_NAME"],
            "__SMB_SHARE_NAME__": values["TC_SHARE_NAME"],
            "__SMB_SAMBA_USER__": values["TC_SAMBA_USER"],
            "__SMB_NETBIOS_NAME__": values["TC_NETBIOS_NAME"],
            "__NET_IFACE__": values["TC_NET_IFACE"],
            "__CACHE_DIRECTORY__": smbconf_cache_directory,
            "__SMBD_LOG_FILE__": smbd_log_file,
            "__SMBD_MAX_LOG_SIZE__": smbd_max_log_size,
            "__SMBD_LOG_LEVEL_LINE__": smbd_log_level_line,
        },
    )
