"""Apple MaSt observations drive topology; errors must not mean no disks."""
import plistlib
import subprocess

import pytest

from tests.native.build import ROOT, instrumentation_flags
from tests.storage_fixtures import MAST_FIXTURES


@pytest.fixture(scope="module")
def parser(tmp_path_factory):
    binary = tmp_path_factory.mktemp("mast-native") / "mast"
    subprocess.run(["cc", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror",
                    *instrumentation_flags(), "-I", str(ROOT / "build/native"),
                    str(ROOT / "build/native/storage/mast.c"),
                    str(ROOT / "tests/native/unit/test_mast.c"), "-o", str(binary)],
                   check=True, capture_output=True)
    return binary


def run(parser, text, *args):
    return subprocess.run([str(parser), *args], input=text.encode() if isinstance(text, str) else text,
                          capture_output=True, timeout=5)


@pytest.mark.parametrize("fixture", MAST_FIXTURES, ids=lambda f: f.name)
def test_same_inventory_as_recorded_apple_and_legacy_shell_fixtures(parser, fixture):
    result = run(parser, fixture.raw, "topology")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.decode().splitlines()
    assert int(lines[0]) == len(fixture.expected)
    actual = []
    for line in lines[1:]:
        disk, part, uuid, builtin, users, name = line.split()
        actual.append((disk, part, uuid, builtin == "1", bytes.fromhex(name).decode()))
    assert actual == [(v.disk_device, v.partition_device, v.adisk_uuid, v.builtin, v.name)
                      for v in fixture.expected]


@pytest.mark.parametrize("text", ["[]", "MaSt = ();", "[\n]\nMaSt=", plistlib.dumps([])])
def test_authoritative_empty_inventory(parser, text):
    result = run(parser, text)
    assert result.returncode == 0 and result.stdout == b"0\n"


@pytest.mark.parametrize("text", ["", "MaSt=", "garbage", "[", "[{}", "[] garbage",
                                 "[not a disk]", "[]\0garbage", "MaSt = [}",
                                 "<plist><array><dict></array></plist>",
                                 "<plist><array/>", "<plist><array/></plist>garbage"])
def test_failed_or_truncated_observation_is_not_an_empty_inventory(parser, text):
    assert run(parser, text).returncode == 2


def volume(**kwargs):
    return {"deviceName": "dk3", "format": "hfs", "name": "Data", "users": 0,
            "uuid": bytes.fromhex("0123456789abcdef0123456789abcdef"), **kwargs}


def test_binary_uuid_entities_unicode_and_users(parser):
    raw = plistlib.dumps([{"deviceName": "sd1", "builtin": False,
                          "partitions": [volume(name='A&B <"é中">')]}])
    result = run(parser, raw)
    assert result.returncode == 0
    assert result.stdout.splitlines()[1].split()[4] == b"0"
    assert bytes.fromhex(result.stdout.splitlines()[1].split()[5].decode()).decode() == 'A&B <"é中">'


def test_overflow_duplicate_devices_and_unsafe_names_reject_whole_observation(parser):
    for parts in [[volume(deviceName=f"dk{i}") for i in range(17)],
                  [volume(), volume()], [volume(name="x" * 600)]]:
        result = run(parser, plistlib.dumps([{"deviceName": "sd1", "partitions": parts}]))
        assert result.returncode == 2


def test_native_annotation_and_quoted_structural_names_are_data(parser):
    text = '''[
 { deviceName="sd1";
   partitions=[{ deviceName="dk3"; name="partitions = [ ] builtin=true";
   format="hfs"; users=0;
   uuid=01234567 89abcdef 01234567 89abcdef |[{}](binary)| (16 bytes)
   }]; builtin=false;
 }
]'''
    result = run(parser, text)
    assert result.returncode == 0, result.stderr
    fields = result.stdout.splitlines()[1].split()
    assert fields[2] == b"01234567-89ab-cdef-0123-456789abcdef"
    assert fields[3:5] == [b"0", b"0"]
    assert bytes.fromhex(fields[5].decode()).decode() == "partitions = [ ] builtin=true"
