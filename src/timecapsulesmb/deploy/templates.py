from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from timecapsulesmb.core.config import DEFAULTS, shell_quote


@dataclass(frozen=True)
class TemplateBundle:
    start_script_replacements: dict[str, str]
    watchdog_replacements: dict[str, str]
    smbconf_replacements: dict[str, str]


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


def build_template_bundle(values: dict[str, str], *, adisk_disk_key: str = "dk0", adisk_uuid: str = "") -> TemplateBundle:
    device_model = values.get("TC_MDNS_DEVICE_MODEL", DEFAULTS["TC_MDNS_DEVICE_MODEL"])
    return TemplateBundle(
        start_script_replacements={
            "__PAYLOAD_DIR_NAME__": shell_quote(values["TC_PAYLOAD_DIR_NAME"]),
            "__SMB_SHARE_NAME__": shell_quote(values["TC_SHARE_NAME"]),
            "__SMB_NETBIOS_NAME__": shell_quote(values["TC_NETBIOS_NAME"]),
            "__NET_IFACE__": shell_quote(values["TC_NET_IFACE"]),
            "__MDNS_INSTANCE_NAME__": shell_quote(values["TC_MDNS_INSTANCE_NAME"]),
            "__MDNS_HOST_LABEL__": shell_quote(values["TC_MDNS_HOST_LABEL"]),
            "__MDNS_DEVICE_MODEL__": shell_quote(device_model),
            "__ADISK_DISK_KEY__": shell_quote(adisk_disk_key),
            "__ADISK_UUID__": shell_quote(adisk_uuid),
        },
        watchdog_replacements={
            "__SMB_SHARE_NAME__": shell_quote(values["TC_SHARE_NAME"]),
            "__SMB_NETBIOS_NAME__": shell_quote(values["TC_NETBIOS_NAME"]),
            "__NET_IFACE__": shell_quote(values["TC_NET_IFACE"]),
            "__MDNS_INSTANCE_NAME__": shell_quote(values["TC_MDNS_INSTANCE_NAME"]),
            "__MDNS_HOST_LABEL__": shell_quote(values["TC_MDNS_HOST_LABEL"]),
            "__MDNS_DEVICE_MODEL__": shell_quote(device_model),
            "__ADISK_DISK_KEY__": shell_quote(adisk_disk_key),
            "__ADISK_UUID__": shell_quote(adisk_uuid),
        },
        smbconf_replacements={
            "__PAYLOAD_DIR_NAME__": values["TC_PAYLOAD_DIR_NAME"],
            "__SMB_SHARE_NAME__": values["TC_SHARE_NAME"],
            "__SMB_SAMBA_USER__": values["TC_SAMBA_USER"],
            "__SMB_NETBIOS_NAME__": values["TC_NETBIOS_NAME"],
            "__NET_IFACE__": values["TC_NET_IFACE"],
        },
    )
