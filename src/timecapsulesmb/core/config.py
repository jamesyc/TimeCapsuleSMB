from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
import ipaddress
import re


REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = REPO_ROOT / ".env"
MAX_DNS_LABEL_BYTES = 63
MAX_DNS_NAME_BYTES = 255
MAX_DNS_TXT_BYTES = 255
MAX_NETBIOS_NAME_BYTES = 15
MODEL_TXT_PREFIX = "model="
ADISK_DEFAULT_DISK_KEY = "dk2"
ADISK_DISK_UUID_EXAMPLE = "12345678-1234-1234-1234-123456789012"
ADISK_DISK_TXT_MID = "=adVF=0x1093,adVN="
ADISK_DISK_TXT_SUFFIX = ",adVU="
MAX_SAMBA_USER_BYTES = 32


@dataclass(frozen=True)
class AirportDeviceIdentity:
    syap: str
    mdns_model: str
    display_name: str
    family: str
    compatibility_group: str


AIRPORT_DEVICE_IDENTITIES = (
    AirportDeviceIdentity("104", "AirPort5,104", "AirPort Extreme 1st generation", "airport_extreme", "netbsd4be"),
    AirportDeviceIdentity("105", "AirPort5,105", "AirPort Extreme 2nd generation", "airport_extreme", "netbsd4be"),
    AirportDeviceIdentity("106", "TimeCapsule6,106", "Time Capsule 1st generation", "time_capsule", "netbsd4be"),
    AirportDeviceIdentity("108", "AirPort5,108", "AirPort Extreme 3rd generation", "airport_extreme", "netbsd4le"),
    AirportDeviceIdentity("109", "TimeCapsule6,109", "Time Capsule 2nd generation", "time_capsule", "netbsd4be"),
    AirportDeviceIdentity("113", "TimeCapsule6,113", "Time Capsule 3rd generation", "time_capsule", "netbsd4le"),
    AirportDeviceIdentity("114", "AirPort5,114", "AirPort Extreme 4th generation", "airport_extreme", "netbsd4le"),
    AirportDeviceIdentity("116", "TimeCapsule6,116", "Time Capsule 4th generation", "time_capsule", "netbsd4le"),
    AirportDeviceIdentity("117", "AirPort5,117", "AirPort Extreme 5th generation", "airport_extreme", "netbsd4le"),
    AirportDeviceIdentity("119", "TimeCapsule8,119", "Time Capsule 5th generation", "time_capsule", "netbsd6"),
    AirportDeviceIdentity("120", "AirPort7,120", "AirPort Extreme 6th generation", "airport_extreme", "netbsd6"),
)
AIRPORT_IDENTITIES_BY_SYAP = {identity.syap: identity for identity in AIRPORT_DEVICE_IDENTITIES}
AIRPORT_IDENTITIES_BY_MODEL = {identity.mdns_model: identity for identity in AIRPORT_DEVICE_IDENTITIES}
VALID_AIRPORT_SYAP_CODES = frozenset(AIRPORT_IDENTITIES_BY_SYAP)
VALID_MDNS_DEVICE_MODELS = frozenset(
    {"TimeCapsule", "AirPort"} | {identity.mdns_model for identity in AIRPORT_DEVICE_IDENTITIES}
)
AIRPORT_SYAP_TO_MODEL = {
    identity.syap: identity.mdns_model
    for identity in AIRPORT_DEVICE_IDENTITIES
}


def airport_identity_from_values(values: dict[str, str]) -> AirportDeviceIdentity | None:
    syap = values.get("TC_AIRPORT_SYAP", "")
    model = values.get("TC_MDNS_DEVICE_MODEL", "")
    return AIRPORT_IDENTITIES_BY_SYAP.get(syap) or AIRPORT_IDENTITIES_BY_MODEL.get(model)


def airport_family_display_name(values: dict[str, str]) -> str:
    model = values.get("TC_MDNS_DEVICE_MODEL", "")
    identity = airport_identity_from_values(values)
    family = identity.family if identity is not None else ""
    if family == "time_capsule" or model == "TimeCapsule":
        return "Time Capsule"
    if family == "airport_extreme" or model == "AirPort":
        return "AirPort Extreme"
    return "AirPort storage device"


def airport_exact_display_name(values: dict[str, str]) -> str:
    identity = airport_identity_from_values(values)
    if identity is not None:
        return identity.display_name
    return airport_family_display_name(values)


DEFAULTS = {
    "TC_HOST": "root@192.168.1.101",
    "TC_SSH_OPTS": "-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    "TC_NET_IFACE": "bridge0",
    "TC_SHARE_NAME": "Data",
    "TC_SAMBA_USER": "admin",
    "TC_NETBIOS_NAME": "TimeCapsule",
    "TC_PAYLOAD_DIR_NAME": ".samba4",
    "TC_MDNS_INSTANCE_NAME": "Time Capsule Samba 4",
    "TC_MDNS_HOST_LABEL": "timecapsulesamba4",
    "TC_MDNS_DEVICE_MODEL": "TimeCapsule",
    "TC_AIRPORT_SYAP": "",
    "TC_SHARE_USE_DISK_ROOT": "false",
}

REQUIRED_ENV_KEYS = [
    "TC_HOST",
    "TC_PASSWORD",
    "TC_NET_IFACE",
    "TC_SHARE_NAME",
    "TC_SAMBA_USER",
    "TC_NETBIOS_NAME",
    "TC_PAYLOAD_DIR_NAME",
    "TC_MDNS_INSTANCE_NAME",
    "TC_MDNS_HOST_LABEL",
    "TC_MDNS_DEVICE_MODEL",
]

CONFIG_FIELDS = [
    ("TC_HOST", "Device SSH target", DEFAULTS["TC_HOST"], False),
    ("TC_PASSWORD", "Device root password", "", True),
    ("TC_NET_IFACE", "Network interface on the device", DEFAULTS["TC_NET_IFACE"], False),
    ("TC_SHARE_NAME", "SMB share name", DEFAULTS["TC_SHARE_NAME"], False),
    ("TC_SAMBA_USER", "Samba username", DEFAULTS["TC_SAMBA_USER"], False),
    ("TC_NETBIOS_NAME", "Samba NetBIOS name", DEFAULTS["TC_NETBIOS_NAME"], False),
    ("TC_PAYLOAD_DIR_NAME", "Persistent payload directory name", DEFAULTS["TC_PAYLOAD_DIR_NAME"], False),
    ("TC_MDNS_INSTANCE_NAME", "mDNS SMB instance name", DEFAULTS["TC_MDNS_INSTANCE_NAME"], False),
    ("TC_MDNS_HOST_LABEL", "mDNS host label", DEFAULTS["TC_MDNS_HOST_LABEL"], False),
    ("TC_AIRPORT_SYAP", "Airport Utility syAP code", DEFAULTS["TC_AIRPORT_SYAP"], False),
    ("TC_MDNS_DEVICE_MODEL", "mDNS device model hint", DEFAULTS["TC_MDNS_DEVICE_MODEL"], False),
]

ENV_FILE_KEYS = [
    "TC_HOST",
    "TC_PASSWORD",
    "TC_SSH_OPTS",
    "TC_NET_IFACE",
    "TC_SHARE_NAME",
    "TC_SAMBA_USER",
    "TC_NETBIOS_NAME",
    "TC_PAYLOAD_DIR_NAME",
    "TC_MDNS_INSTANCE_NAME",
    "TC_MDNS_HOST_LABEL",
    "TC_MDNS_DEVICE_MODEL",
    "TC_AIRPORT_SYAP",
    "TC_SHARE_USE_DISK_ROOT",
    "TC_CONFIGURE_ID",
]

CONFIG_HEADER = """# Local user/device configuration for TimeCapsuleSMB.
# Generated by tcapsule configure
"""


@dataclass(frozen=True)
class AppConfig:
    values: dict[str, str]

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def require(self, key: str, *, messagebefore: str = "", messageafter: str = "") -> str:
        value = self.get(key)
        if not value:
            raise SystemExit(f"{messagebefore}Missing required setting in .env: {key}{messageafter}")
        return value


@dataclass(frozen=True)
class ConfigValidationError:
    key: str
    message: str

    def format_for_cli(self) -> str:
        return f"{self.key} is invalid. Run the `configure` command again.\n{self.message}"


def parse_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    try:
        tokens = shlex.split(value)
        # A single parsed token means the env value was one scalar, possibly
        # quoted. Multi-token values such as TC_SSH_OPTS must remain intact and
        # are interpreted later by the transport layer.
        if len(tokens) == 1:
            return tokens[0]
        return value
    except ValueError:
        return value.strip("'\"")


def parse_env_values(path: Path, *, defaults: Optional[dict[str, str]] = None) -> dict[str, str]:
    values = dict(DEFAULTS if defaults is None else defaults)
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = parse_env_value(value)
    return values


def missing_required_keys(values: dict[str, str]) -> list[str]:
    return [key for key in REQUIRED_ENV_KEYS if not values.get(key, "")]


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def extract_host(target: str) -> str:
    return target.split("@", 1)[1] if "@" in target else target


def _contains_invalid_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _contains_whitespace(value: str) -> bool:
    return any(ch.isspace() for ch in value)


def _has_only_safe_chars(value: str, pattern: str) -> bool:
    return re.fullmatch(pattern, value) is not None


def build_mdns_device_model_txt(value: str) -> Optional[str]:
    txt = MODEL_TXT_PREFIX + value
    if len(txt.encode("utf-8")) > MAX_DNS_TXT_BYTES:
        return None
    return txt


def build_adisk_share_txt(value: str) -> Optional[str]:
    txt = f"{ADISK_DEFAULT_DISK_KEY}{ADISK_DISK_TXT_MID}{value}{ADISK_DISK_TXT_SUFFIX}{ADISK_DISK_UUID_EXAMPLE}"
    if len(txt.encode("utf-8")) > MAX_DNS_TXT_BYTES:
        return None
    return txt


def validate_dns_label(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if len(value.encode("utf-8")) > MAX_DNS_LABEL_BYTES:
        return f"{field_name} must be {MAX_DNS_LABEL_BYTES} bytes or fewer."
    if "." in value:
        return f"{field_name} must not contain dots."
    if _contains_invalid_control_character(value):
        return f"{field_name} contains an invalid control character."
    return None


def validate_mdns_device_model(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if value not in VALID_MDNS_DEVICE_MODELS:
        return f"{field_name} is not a supported AirPort storage device model."
    if build_mdns_device_model_txt(value) is None:
        return f"{field_name} must be 249 bytes or fewer."
    if _contains_invalid_control_character(value):
        return f"{field_name} contains an invalid control character."
    return None


def validate_mdns_instance_name(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if len(value.encode("utf-8")) > MAX_DNS_LABEL_BYTES:
        return f"{field_name} must be {MAX_DNS_LABEL_BYTES} bytes or fewer."
    if "." in value:
        return f"{field_name} must not contain dots."
    if _contains_invalid_control_character(value):
        return f"{field_name} contains an invalid control character."
    return None


def validate_mdns_host_label(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if len(value.encode("utf-8")) > MAX_DNS_LABEL_BYTES:
        return f"{field_name} must be {MAX_DNS_LABEL_BYTES} bytes or fewer."
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        pass
    if "." in value:
        return f"{field_name} must not contain dots."
    if value.startswith("-") or value.endswith("-"):
        return f"{field_name} must not start or end with a hyphen."
    if not _has_only_safe_chars(value, r"[A-Za-z0-9-]+"):
        return f"{field_name} may contain only letters, numbers, and hyphens."
    return None


def validate_adisk_share_name(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if build_adisk_share_txt(value) is None:
        max_share_bytes = (
            MAX_DNS_TXT_BYTES
            - len(ADISK_DEFAULT_DISK_KEY.encode("utf-8"))
            - len(ADISK_DISK_TXT_MID.encode("utf-8"))
            - len(ADISK_DISK_TXT_SUFFIX.encode("utf-8"))
            - len(ADISK_DISK_UUID_EXAMPLE.encode("utf-8"))
        )
        return f"{field_name} must be {max_share_bytes} bytes or fewer."
    if _contains_invalid_control_character(value):
        return f"{field_name} contains an invalid control character."
    if any(ch in value for ch in '/\\[]:*?"<>|,='):
        return f"{field_name} contains a character that is not safe for Samba/adisk."
    return None


def validate_netbios_name(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if len(value.encode("utf-8")) > MAX_NETBIOS_NAME_BYTES:
        return f"{field_name} must be {MAX_NETBIOS_NAME_BYTES} bytes or fewer."
    if _contains_invalid_control_character(value):
        return f"{field_name} contains an invalid control character."
    if not _has_only_safe_chars(value, r"[A-Za-z0-9_-]+"):
        return f"{field_name} may contain only letters, numbers, underscores, and hyphens."
    return None


def validate_samba_user(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if len(value.encode("utf-8")) > MAX_SAMBA_USER_BYTES:
        return f"{field_name} must be {MAX_SAMBA_USER_BYTES} bytes or fewer."
    if _contains_invalid_control_character(value):
        return f"{field_name} contains an invalid control character."
    if _contains_whitespace(value):
        return f"{field_name} must not contain whitespace."
    if not _has_only_safe_chars(value, r"[A-Za-z0-9._-]+"):
        return f"{field_name} may contain only letters, numbers, dots, underscores, and hyphens."
    return None


def validate_payload_dir_name(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if value in {".", ".."}:
        return f"{field_name} must not be . or ..."
    if "/" in value or "\\" in value:
        return f"{field_name} must be a single directory name, not a path."
    if value.startswith("-"):
        return f"{field_name} must not start with a hyphen."
    if _contains_invalid_control_character(value):
        return f"{field_name} contains an invalid control character."
    if not _has_only_safe_chars(value, r"[A-Za-z0-9._-]+"):
        return f"{field_name} may contain only letters, numbers, dots, underscores, and hyphens."
    return None


def validate_net_iface(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if _contains_invalid_control_character(value):
        return f"{field_name} contains an invalid control character."
    if _contains_whitespace(value):
        return f"{field_name} must not contain whitespace."
    if not _has_only_safe_chars(value, r"[A-Za-z0-9._:-]+"):
        return f"{field_name} may contain only letters, numbers, dots, underscores, colons, and hyphens."
    return None


def validate_ssh_target(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if _contains_invalid_control_character(value):
        return f"{field_name} contains an invalid control character."
    if _contains_whitespace(value):
        return f"{field_name} must not contain whitespace."
    if "@" not in value:
        return f"{field_name} must include a username, like root@192.168.1.101."
    user, host = value.split("@", 1)
    if not user:
        return f"{field_name} must include a username before @."
    if not host:
        return f"{field_name} must include a host after @."
    if not _has_only_safe_chars(user, r"[A-Za-z0-9._-]+"):
        return f"{field_name} username may contain only letters, numbers, dots, underscores, and hyphens."
    if host.startswith("-"):
        return f"{field_name} host must not start with a hyphen."
    return None


def parse_bool(value: str) -> bool:
    return value.strip().lower() == "true"


def validate_bool(value: str, field_name: str) -> Optional[str]:
    if value == "":
        return None
    if value.strip().lower() not in {"true", "false"}:
        return f"{field_name} must be true or false."
    return None


def validate_airport_syap(value: str, field_name: str) -> Optional[str]:
    if not value:
        return f"{field_name} cannot be blank."
    if not value.isdigit():
        return f"{field_name} must contain only digits."
    if value not in VALID_AIRPORT_SYAP_CODES:
        return "The configured syAP is invalid."
    return None


def infer_mdns_device_model_from_airport_syap(syap: str) -> Optional[str]:
    return AIRPORT_SYAP_TO_MODEL.get(syap)


def validate_mdns_device_model_matches_syap(syap: str, device_model: str) -> Optional[str]:
    expected_model = infer_mdns_device_model_from_airport_syap(syap)
    if expected_model is None:
        return None
    if device_model != expected_model:
        return (f'TC_MDNS_DEVICE_MODEL "{device_model}" must match the '
                f'configured syAP expected value "{expected_model}".')
    return None


CONFIG_VALIDATORS: dict[str, Callable[[str, str], Optional[str]]] = {
    "TC_HOST": validate_ssh_target,
    "TC_NET_IFACE": validate_net_iface,
    "TC_SHARE_NAME": validate_adisk_share_name,
    "TC_SAMBA_USER": validate_samba_user,
    "TC_NETBIOS_NAME": validate_netbios_name,
    "TC_PAYLOAD_DIR_NAME": validate_payload_dir_name,
    "TC_MDNS_INSTANCE_NAME": validate_mdns_instance_name,
    "TC_MDNS_HOST_LABEL": validate_mdns_host_label,
    "TC_AIRPORT_SYAP": validate_airport_syap,
    "TC_MDNS_DEVICE_MODEL": validate_mdns_device_model,
    "TC_SHARE_USE_DISK_ROOT": validate_bool,
}

CONFIG_VALIDATION_PROFILES: dict[str, tuple[str, ...]] = {
    "configure": (
        "TC_NET_IFACE",
        "TC_SHARE_NAME",
        "TC_SAMBA_USER",
        "TC_NETBIOS_NAME",
        "TC_PAYLOAD_DIR_NAME",
        "TC_MDNS_INSTANCE_NAME",
        "TC_MDNS_HOST_LABEL",
        "TC_MDNS_DEVICE_MODEL",
        "TC_AIRPORT_SYAP",
        "TC_SHARE_USE_DISK_ROOT",
    ),
    "deploy": (
        "TC_HOST",
        "TC_NET_IFACE",
        "TC_SHARE_NAME",
        "TC_SAMBA_USER",
        "TC_NETBIOS_NAME",
        "TC_PAYLOAD_DIR_NAME",
        "TC_MDNS_INSTANCE_NAME",
        "TC_MDNS_HOST_LABEL",
        "TC_MDNS_DEVICE_MODEL",
        "TC_AIRPORT_SYAP",
        "TC_SHARE_USE_DISK_ROOT",
    ),
    "activate": (
        "TC_HOST",
        "TC_NET_IFACE",
        "TC_SHARE_NAME",
        "TC_SAMBA_USER",
        "TC_NETBIOS_NAME",
        "TC_PAYLOAD_DIR_NAME",
        "TC_MDNS_INSTANCE_NAME",
        "TC_MDNS_HOST_LABEL",
        "TC_MDNS_DEVICE_MODEL",
        "TC_AIRPORT_SYAP",
        "TC_SHARE_USE_DISK_ROOT",
    ),
    "doctor": (
        "TC_HOST",
        "TC_NET_IFACE",
        "TC_SHARE_NAME",
        "TC_SAMBA_USER",
        "TC_NETBIOS_NAME",
        "TC_PAYLOAD_DIR_NAME",
        "TC_MDNS_INSTANCE_NAME",
        "TC_MDNS_HOST_LABEL",
        "TC_MDNS_DEVICE_MODEL",
        "TC_AIRPORT_SYAP",
        "TC_SHARE_USE_DISK_ROOT",
    ),
    "uninstall": ("TC_HOST", "TC_PAYLOAD_DIR_NAME"),
    "fsck": ("TC_HOST",),
    "repair_xattrs": ("TC_SHARE_NAME",),
}


def validate_config_values(values: dict[str, str], *, profile: str) -> list[ConfigValidationError]:
    errors: list[ConfigValidationError] = []
    for key in CONFIG_VALIDATION_PROFILES[profile]:
        validator = CONFIG_VALIDATORS.get(key)
        if validator is None:
            continue
        error = validator(values.get(key, ""), key)
        if error:
            errors.append(ConfigValidationError(key, error))
    if profile in {"deploy", "activate", "doctor"}:
        syap_model_error = validate_mdns_device_model_matches_syap(
            values.get("TC_AIRPORT_SYAP", ""),
            values.get("TC_MDNS_DEVICE_MODEL", ""),
        )
        if syap_model_error:
            errors.append(ConfigValidationError("TC_MDNS_DEVICE_MODEL", syap_model_error))
    return errors


def require_valid_config(values: dict[str, str], *, profile: str) -> None:
    errors = validate_config_values(values, profile=profile)
    if errors:
        raise SystemExit(errors[0].format_for_cli())


def render_env_text(values: dict[str, str]) -> str:
    lines = [CONFIG_HEADER.rstrip(), ""]
    for key in ENV_FILE_KEYS:
        rendered_value = values.get(key, DEFAULTS.get(key, ""))
        lines.append(f"{key}={shell_quote(rendered_value)}")
    lines.append("")
    return "\n".join(lines)


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.write_text(render_env_text(values))


def upsert_env_key(path: Path, key: str, value: str) -> None:
    rendered = f"{key}={shell_quote(value)}"
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        path.write_text(f"{CONFIG_HEADER.rstrip()}\n\n{rendered}\n")
        return

    updated = False
    new_lines: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            if not updated:
                new_lines.append(rendered)
                updated = True
            continue
        new_lines.append(line)
    if not updated:
        if new_lines and new_lines[-1] != "":
            new_lines.append("")
        new_lines.append(rendered)
    new_lines.append("")
    path.write_text("\n".join(new_lines))
