"""The managed settings deploy writes to the device's /mnt/Flash/tcapsulesmb.conf.

Deploy renders that file from these values, and doctor compares the same
values with what the device's file holds, so both read this Mac's settings the
same way: each one the caller sets, or else the value saved in the .env.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from timecapsulesmb.core.config import (
    ATA_IDLE_MAX_SECONDS,
    DEFAULTS,
    AppConfig,
    device_ata_idle_seconds,
    effective_smb_security,
    parse_bool,
    vfs_aio_fork_enabled_in,
)


@dataclass(frozen=True)
class ComparedSetting:
    flash_key: str
    # The app's parameter name; the app names the setting by it.
    setting_id: str
    label: str
    # Compared only with the app's device profile. The .env has no rsync
    # setting: only deploy --enable-rsync turns it on. It does hold debug
    # logging, but that is usually set for one deploy with --debug-logging or
    # edited on the device by testers, so a .env comparison would warn about
    # intended values.
    app_only: bool = False


# The settings a person chooses that doctor compares with the device's file.
COMPARED_SETTINGS: tuple[ComparedSetting, ...] = (
    ComparedSetting("MDNS_ADVERTISE_AFP", "mdns_advertise_afp", "Advertise AFP over Bonjour"),
    ComparedSetting("INTERNAL_SHARE_USE_DISK_ROOT", "internal_share_use_disk_root", "Share internal disk root"),
    ComparedSetting("SMB_BROWSE_COMPATIBILITY", "smb_browse_compatibility", "Allow SMB share browsing"),
    ComparedSetting("ANY_PROTOCOL", "any_protocol", "Allow any SMB protocol"),
    ComparedSetting("REQUIRE_SMB_SIGNING", "require_smb_signing", "Require SMB signing"),
    ComparedSetting("REQUIRE_SMB_ENCRYPTION", "require_smb_encryption", "Require SMB encryption"),
    ComparedSetting("DISABLE_SMB_ENCRYPTION", "disable_smb_encryption", "Turn off SMB encryption"),
    ComparedSetting("FRUIT_METADATA_NETATALK", "fruit_metadata_netatalk", "Use Netatalk metadata"),
    ComparedSetting("VFS_AIO_FORK_ENABLED", "vfs_aio_fork_enabled", "Enable vfs_aio_fork"),
    ComparedSetting("ATA_IDLE_SECONDS", "ata_idle_seconds", "ATA idle"),
    ComparedSetting("ATA_STANDBY", "ata_standby", "ATA standby"),
    ComparedSetting("RSYNC_ENABLED", "rsync_enabled", "Enable rsync", app_only=True),
    # MDNS_DEBUG_LOGGING always holds the same value.
    ComparedSetting("SMBD_DEBUG_LOGGING", "debug_logging", "Enable debug logging", app_only=True),
)
COMPARED_FLASH_KEYS = tuple(setting.flash_key for setting in COMPARED_SETTINGS)


@dataclass(frozen=True)
class ManagedSettings:
    """The settings a person chooses for a device. None reads the value saved
    in the .env."""
    debug_logging: bool | None = None
    internal_share_use_disk_root: bool | None = None
    smb_browse_compatibility: bool | None = None
    mdns_advertise_afp: bool | None = None
    any_protocol: bool | None = None
    require_smb_signing: bool | None = None
    require_smb_encryption: bool | None = None
    disable_smb_encryption: bool | None = None
    fruit_metadata_netatalk: bool | None = None
    vfs_aio_fork_enabled: bool | None = None
    ata_idle_seconds: str | int | None = None
    ata_standby: str | int | None = None
    # Never in the .env: deploy's flag or the app's profile. None is off.
    rsync_enabled: bool | None = None


@dataclass(frozen=True)
class SettingsSource:
    """Where doctor takes this Mac's settings from, which is where that
    Mac's deploy takes them from:

    - env: the CLI's .env, comparing every setting the .env saves;
    - app_profile: the app's device profile, comparing them all;
    - app_refused: the app sent a combination deploy refuses, so doctor
      compares nothing with the device.
    """
    kind: Literal["env", "app_profile", "app_refused"]
    settings: ManagedSettings = ManagedSettings()

    def __post_init__(self) -> None:
        # Only the app's profile carries settings: the .env is read where it
        # is needed, and a refused profile has none to compare.
        if self.kind not in ("env", "app_profile", "app_refused"):
            raise ValueError(f"unknown settings source {self.kind!r}")
        if self.kind != "app_profile" and self.settings != ManagedSettings():
            raise ValueError(f"a {self.kind} settings source carries no settings")

    @classmethod
    def env(cls) -> SettingsSource:
        return cls("env")

    @classmethod
    def app_profile(cls, settings: ManagedSettings) -> SettingsSource:
        return cls("app_profile", settings)

    @classmethod
    def app_refused(cls) -> SettingsSource:
        return cls("app_refused")

    @property
    def compared(self) -> tuple[ComparedSetting, ...]:
        """The settings this source can say were changed."""
        if self.kind == "app_profile":
            return COMPARED_SETTINGS
        if self.kind == "env":
            return tuple(setting for setting in COMPARED_SETTINGS if not setting.app_only)
        return ()


def runtime_unsigned_config_value(config: AppConfig, key: str, default: str) -> str:
    raw_value = config.get(key, default).strip()
    if raw_value == "":
        raw_value = default
    if raw_value == "":
        return ""
    if not raw_value.isdigit():
        raise ValueError(f"{key} must be a non-negative integer")
    return str(int(raw_value))


def runtime_unsigned_override_value(value: str | int) -> str | int:
    if isinstance(value, int):
        if value < 0:
            raise ValueError("runtime setting override must be a non-negative integer")
        return value
    raw_value = value.strip()
    if raw_value == "":
        return ""
    if not raw_value.isdigit():
        raise ValueError("runtime setting override must be a non-negative integer")
    return str(int(raw_value))


def ata_idle_seconds_value(config: AppConfig, override: str | int | None) -> str | int:
    """The idle timer deploy writes: the one passed, else the .env's. A .env
    value past config.ATA_IDLE_MAX_SECONDS, saved by an older release, is
    written as the 300 the device would run it as; a larger one passed for
    this deploy is refused."""
    if override is None:
        return device_ata_idle_seconds(
            runtime_unsigned_config_value(config, "TC_ATA_IDLE_SECONDS", DEFAULTS["TC_ATA_IDLE_SECONDS"])
        )
    value = runtime_unsigned_override_value(override)
    if value != "" and int(value) > ATA_IDLE_MAX_SECONDS:
        raise ValueError(f"ATA idle seconds must be no larger than {ATA_IDLE_MAX_SECONDS}")
    return value


def effective_internal_root(config: AppConfig, override: bool | None) -> bool:
    """Whether the internal disk's share is the whole disk, not ShareRoot."""
    if override is not None:
        return override
    return parse_bool(config.get("TC_INTERNAL_SHARE_USE_DISK_ROOT", DEFAULTS["TC_INTERNAL_SHARE_USE_DISK_ROOT"]))


def _bool_setting(config: AppConfig, key: str, override: bool | None) -> bool:
    return parse_bool(config.get(key, DEFAULTS[key])) if override is None else override


def managed_flash_values(config: AppConfig, settings: ManagedSettings = ManagedSettings()) -> dict[str, str | int]:
    """The Flash config keys the managed settings set, with the values deploy
    writes. Raises ValueError for a value or combination the device refuses."""
    smb = effective_smb_security(
        config,
        any_protocol=settings.any_protocol,
        require_smb_signing=settings.require_smb_signing,
        require_smb_encryption=settings.require_smb_encryption,
        disable_smb_encryption=settings.disable_smb_encryption,
    )
    vfs_aio_fork_enabled = (
        vfs_aio_fork_enabled_in(config) if settings.vfs_aio_fork_enabled is None else settings.vfs_aio_fork_enabled
    )
    debug_logging = _bool_setting(config, "TC_DEBUG_LOGGING", settings.debug_logging)
    return {
        "INTERNAL_SHARE_USE_DISK_ROOT": 1 if effective_internal_root(config, settings.internal_share_use_disk_root) else 0,
        "SMB_BROWSE_COMPATIBILITY": 1 if _bool_setting(config, "TC_SMB_BROWSE_COMPATIBILITY", settings.smb_browse_compatibility) else 0,
        "MDNS_ADVERTISE_AFP": 1 if _bool_setting(config, "TC_MDNS_ADVERTISE_AFP", settings.mdns_advertise_afp) else 0,
        "ANY_PROTOCOL": 1 if smb.any_protocol else 0,
        "REQUIRE_SMB_SIGNING": 1 if smb.require_smb_signing else 0,
        "REQUIRE_SMB_ENCRYPTION": 1 if smb.require_smb_encryption else 0,
        "DISABLE_SMB_ENCRYPTION": 1 if smb.disable_smb_encryption else 0,
        "FRUIT_METADATA_NETATALK": 1 if _bool_setting(config, "TC_FRUIT_METADATA_NETATALK", settings.fruit_metadata_netatalk) else 0,
        "VFS_AIO_FORK_ENABLED": 1 if vfs_aio_fork_enabled else 0,
        "ATA_IDLE_SECONDS": ata_idle_seconds_value(config, settings.ata_idle_seconds),
        "ATA_STANDBY": (
            runtime_unsigned_config_value(config, "TC_ATA_STANDBY", DEFAULTS["TC_ATA_STANDBY"])
            if settings.ata_standby is None
            else runtime_unsigned_override_value(settings.ata_standby)
        ),
        "RSYNC_ENABLED": 1 if settings.rsync_enabled else 0,
        "SMBD_DEBUG_LOGGING": 1 if debug_logging else 0,
        "MDNS_DEBUG_LOGGING": 1 if debug_logging else 0,
    }


def settings_not_applied(
    wanted: Mapping[str, str | int],
    installed: Mapping[str, str],
    compared: tuple[ComparedSetting, ...] = COMPARED_SETTINGS,
) -> list[ComparedSetting]:
    """The `compared` settings whose value in this Mac's settings (`wanted`)
    differs from the device's file (`installed`). A key the file does not set
    is unknown, not changed."""
    return [
        setting
        for setting in compared
        if setting.flash_key in installed and setting.flash_key in wanted
        and str(wanted[setting.flash_key]) != installed[setting.flash_key]
    ]
