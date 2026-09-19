import subprocess

import pytest

from tests.native.build import ROOT
from tests.storage_fixtures import MAST_FIXTURES


@pytest.fixture(scope="module")
def mast_parser(tmp_path_factory):
    output = tmp_path_factory.mktemp("native-mast") / "test_mast"
    native = ROOT / "build/native"
    result = subprocess.run(
        [
            "cc", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror",
            "-I", str(native / "storage"), "-I", str(native / "common"),
            str(native / "storage/mast.c"), str(native / "common/acp.c"),
            str(ROOT / "tests/native/unit/test_mast.c"), "-o", str(output),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return output


@pytest.mark.parametrize("fixture", [item for item in MAST_FIXTURES if item.shell_supported], ids=lambda item: item.name)
def test_native_mast_parser_matches_supported_device_formats(mast_parser, tmp_path, fixture):
    source = tmp_path / "mast"
    source.write_text(fixture.raw)
    result = subprocess.run([str(mast_parser), str(source)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    rows = [line.split("\t") for line in result.stdout.splitlines()[1:]]
    assert [row[1] for row in rows] == [volume.partition_device for volume in fixture.expected]
    assert [row[3] for row in rows] == [volume.name for volume in fixture.expected]
    assert [row[4] for row in rows] == [volume.adisk_uuid for volume in fixture.expected]
    assert [row[5] == "1" for row in rows] == [volume.builtin for volume in fixture.expected]


def test_native_mast_parser_distinguishes_invalid_from_empty(mast_parser, tmp_path):
    empty = tmp_path / "empty"
    empty.write_text("MaSt = (\n);\n")
    invalid = tmp_path / "invalid"
    invalid.write_text("<?xml version='1.0'?><plist><array/></plist>")
    empty_result = subprocess.run([str(mast_parser), str(empty)], capture_output=True, text=True, timeout=5)
    invalid_result = subprocess.run([str(mast_parser), str(invalid)], capture_output=True, text=True, timeout=5)
    assert empty_result.returncode == 0
    assert "valid=1 empty=1 count=0" in empty_result.stdout
    assert invalid_result.returncode == 3
