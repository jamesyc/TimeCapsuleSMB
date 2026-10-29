from __future__ import annotations

from typing import Mapping

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.discovery.models import normalize_airport_mac


def request_password(params: Mapping[str, object]) -> str:
    value = params.get("password")
    if isinstance(value, str) and value:
        return value
    credentials = params.get("credentials")
    if isinstance(credentials, Mapping):
        nested = credentials.get("password")
        if isinstance(nested, str) and nested:
            return nested
    return ""


def saved_airport_mac(config: AppConfig) -> str | None:
    """The saved device's AirPort MAC (waMA), TC_AIRPORT_MAC of this config.

    configure writes it to the profile's .env; for the app, load_request_config
    puts the app's own value there (overlay_request_airport_mac).
    """
    value = config.get("TC_AIRPORT_MAC") or None
    if value is None:
        return None
    mac = normalize_airport_mac(value)
    if mac is None:
        raise ValueError(f"TC_AIRPORT_MAC is not an AirPort MAC address: {value!r}")
    return mac


def request_saved_airport_macs(params: Mapping[str, object]) -> frozenset[str]:
    """The MACs of every AirPort the app has saved, or none.

    The app sends them when it adds a device that matches no saved one by its
    address or Bonjour record: configure stops at a saved AirPort, and the app
    sets it up as that device, with its own settings.
    """
    value = params.get("saved_airport_macs")
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError(f"saved_airport_macs is not a list: {value!r}")
    macs: set[str] = set()
    for item in value:
        mac = normalize_airport_mac(item) if isinstance(item, str) else None
        if mac is None:
            raise ValueError(f"saved_airport_macs has an entry that is not an AirPort MAC address: {item!r}")
        macs.add(mac)
    return frozenset(macs)


def request_airport_mac(params: Mapping[str, object]) -> str | None:
    """The MAC the app saved for this request's profile, if it has one.

    The app's device registry owns a profile's MAC and sends it with each
    operation; configure gets it for a profile being edited.
    """
    value = params.get("airport_mac")
    mac = normalize_airport_mac(value) if value is not None else None
    if value is not None and mac is None:
        raise ValueError(f"airport_mac is not an AirPort MAC address: {value!r}")
    return mac


def overlay_request_airport_mac(config: AppConfig, params: Mapping[str, object]) -> AppConfig:
    """The config with the MAC the app saved for this profile, or none:
    a profile the app holds none for has none, whatever the .env says."""
    mac = request_airport_mac(params)
    values = dict(config.values)
    values["TC_AIRPORT_MAC"] = mac or ""
    return AppConfig.from_values(
        values,
        path=config.path,
        exists=config.exists,
        file_values=config.file_values,
    )


def overlay_request_credentials(config: AppConfig, params: Mapping[str, object]) -> AppConfig:
    password = request_password(params)
    if not password:
        return config
    values = dict(config.values)
    values["TC_PASSWORD"] = password
    return AppConfig.from_values(
        values,
        path=config.path,
        exists=config.exists,
        file_values=config.file_values,
    )
