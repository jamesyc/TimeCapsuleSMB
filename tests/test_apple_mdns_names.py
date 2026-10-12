"""Apple's Bonjour naming, checked against answers produced by Apple's own firmware code."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from timecapsulesmb.device.probe import (
    apple_bonjour_host_label,
    apple_conflict_base,
    apple_conflict_name,
    apple_conflict_rename,
    apple_name_key,
    derive_runtime_naming_identity,
    normalize_runtime_mdns_instance_name,
)

FIXTURE = json.loads((Path(__file__).resolve().parents[1]
                      / "macos/TimeCapsuleSMB/Tests/TimeCapsuleSMBAppTests/Fixtures/apple_mdns_names.json").read_text())
RENAMES = [(case["name"], not case["service_name"], case["renamed"]) for case in FIXTURE["renames"]]


def renamed_number(renamed: str, host_label: bool) -> int:
    digits = renamed.rsplit("-", 1)[1] if host_label else renamed.rsplit("(", 1)[1].rstrip(")")
    return int(digits)


@pytest.mark.parametrize("name, host_label, renamed", RENAMES)
def test_a_conflict_renames_a_name_as_apples_firmware_does(name, host_label, renamed):
    assert apple_conflict_name(name, renamed_number(renamed, host_label), host_label=host_label) == renamed
    assert apple_conflict_rename(renamed, name, host_label=host_label)


# Apple's parser needs text before the suffix: "-10" alone has none, so the
# names that are nothing but a suffix are left out.
@pytest.mark.parametrize("name, host_label, renamed",
                         [case for case in RENAMES if apple_conflict_base(case[2], host_label=case[1])])
def test_a_later_conflict_is_still_the_same_device_name(name, host_label, renamed):
    # The number keeps going up (from 10 at random), so any higher one is Apple's too.
    number = renamed_number(renamed, host_label) + 7
    assert apple_conflict_rename(apple_conflict_name(name, number, host_label=host_label), name, host_label=host_label)


@pytest.mark.parametrize("advertised, original", [
    ("Capsule  (2)", "Capsule "),   # Apple drops the trailing space before " (2)"
    ("Capsule (1)", "Capsule"),     # the first rename is 2
    ("Capsule (2)", "Capsule (2)"),  # the same name is not a rename
    ("Capsule (2)", "Capsule (3)"),  # the number never goes down
    ("capsule (2)", "Capsule"),     # a service name keeps its case
    ("Capsule 2", "Capsule"),       # "Capsule 2" is another name, not a rename
    ("Capsule(2)", "Capsule"),
    ("Office (2)", "Capsule"),
    ("Capsule", "Capsule"),
])
def test_other_service_names_are_not_apples_rename(advertised, original):
    assert not apple_conflict_rename(advertised, original, host_label=False)


@pytest.mark.parametrize("advertised, original, expected", [
    ("AirPort-Time-Capsule-2", "airport-time-capsule", True),  # DNS names ignore case
    ("office-3", "office-2", True),
    ("office-2", "office-3", False),
    ("office-1", "office", False),
    ("office2", "office", False),
    ("office--2", "office", False),
    ("office-2", "office-2", False),
])
def test_host_label_renames(advertised, original, expected):
    assert apple_conflict_rename(advertised, original, host_label=True) is expected


def test_the_base_of_a_name_is_what_two_devices_collide_on():
    assert apple_conflict_base("AirPort Time Capsule (2)", host_label=False) == "AirPort Time Capsule"
    assert apple_conflict_base("Capsule ", host_label=False) == "Capsule"
    assert apple_conflict_base("AirPort-Time-Capsule-2", host_label=True) == "airport-time-capsule"
    # A real "Office 2" has the host label Office-2, which looks like a rename.
    assert apple_conflict_base(apple_bonjour_host_label("Office 2"), host_label=True) == "office"
    # Apple's numbers start at 2: a 1 or a 0 is part of the name.
    assert apple_conflict_base("Office (1)", host_label=False) == "Office (1)"
    assert apple_conflict_base("Office-1", host_label=True) == "office-1"
    assert apple_conflict_base("Office (0)", host_label=False) == "Office (0)"


def test_names_compare_as_mdnscore_compares_them():
    # SameDomainLabel folds ASCII A-Z only (plan/mdns-naming-20261012/find_samelabel.py).
    assert apple_name_key("AirPort Time Capsule") == apple_name_key("airport TIME capsule")
    assert apple_name_key("Été") != apple_name_key("été")
    assert apple_name_key("[Office]") == "[office]"
    assert apple_conflict_rename("Office-2", "OFFICE", host_label=True)
    assert not apple_conflict_rename("Été-2", "été", host_label=True)


@pytest.mark.parametrize("case", FIXTURE["host_labels"], ids=lambda case: repr(case["name"]))
def test_the_host_label_is_the_one_acpd_builds(case):
    assert apple_bonjour_host_label(case["name"]) == case["label"]


@pytest.mark.parametrize("name, expected", [
    ("AirPort Time Capsule", "AirPort Time Capsule"),
    ("  Capsule  ", "  Capsule  "),
    ("x" * 63, "x" * 63),
    ("é" * 31 + "a", "é" * 31 + "a"),
    # mDNSResponder copies syNm whole or not at all: over 63 bytes it uses its own name.
    ("x" * 64, ""),
    ("é" * 32, ""),
    ("", ""),
    ("ctrl\x01char", "ctrl-char"),
])
def test_the_instance_name_is_syNm_whole_or_unknown(name, expected):
    assert normalize_runtime_mdns_instance_name(name) == expected


def test_an_unpredictable_instance_name_is_none_not_a_host_label_guess():
    identity = derive_runtime_naming_identity("x" * 64, "office", system_dns_name="Office")
    assert identity.mdns_instance_name is None
    assert identity.mdns_host_label == "office"
    assert derive_runtime_naming_identity(None, "office").mdns_instance_name is None
