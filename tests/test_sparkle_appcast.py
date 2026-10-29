from __future__ import annotations

import base64
import importlib.util
import io
import plistlib
import struct
import sys
import zipfile
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "macos" / "TimeCapsuleSMB" / "tools" / "sparkle_appcast.py"
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
# RFC 8032's first ed25519 test key, in generate_keys -x's one-line format.
# No real key is in the repository.
SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC_KEY = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
PRIVATE_KEY = base64.b64encode(SEED) + b"\n"
SIGNATURE = "c2lnbmF0dXJl"
ARM64 = 0x0100000C
X86_64 = 0x01000007


def fat_executable(*cpu_types: int, fat64: bool = False) -> bytes:
    """A universal Mach-O header with one slice per CPU type."""
    header = struct.pack(">II", 0xCAFEBABF if fat64 else 0xCAFEBABE, len(cpu_types))
    for index, cpu_type in enumerate(cpu_types):
        offset = 0x4000 * (index + 1)
        if fat64:
            header += struct.pack(">iiQQII", cpu_type, 0, offset, 0x1000, 14, 0)
        else:
            header += struct.pack(">iiIII", cpu_type, 0, offset, 0x1000, 14)
    return header + b"\0" * 64


def thin_executable(cpu_type: int) -> bytes:
    """A thin 64-bit Mach-O header, as the file stores it (little-endian)."""
    return struct.pack("<IiiIIII", 0xFEEDFACF, cpu_type, 0, 2, 0, 0, 0) + b"\0" * 64


def load_module():
    spec = importlib.util.spec_from_file_location("sparkle_appcast", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_release(tmp_path: Path, *, executable: bytes | None = None, **info_overrides: object) -> SimpleNamespace:
    """A release zip laid out as ditto -c -k --keepParent writes it, with a
    universal executable unless another is given."""
    info = {
        "CFBundleVersion": "30800",
        "CFBundleShortVersionString": "3.8.0",
        "LSMinimumSystemVersion": "14.0",
        "SUPublicEDKey": base64.b64encode(PUBLIC_KEY).decode(),
        **info_overrides,
    }
    info = {key: value for key, value in info.items() if value is not None}
    archive = tmp_path / "TimeCapsuleSMB.app.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("TimeCapsuleSMB.app/Contents/MacOS/TimeCapsuleSMB", executable or fat_executable(X86_64, ARM64))
        zipped.writestr("TimeCapsuleSMB.app/Contents/Info.plist", plistlib.dumps(info))
    key = tmp_path / "key.txt"
    key.write_bytes(PRIVATE_KEY)
    stdin_log = tmp_path / "sign_update-stdin"
    return SimpleNamespace(archive=archive, key=key, stdin_log=stdin_log, out=tmp_path / "appcast.xml")


def stub_sign_update(tmp_path: Path, release: SimpleNamespace, *, output: str | None = None, rc: int = 0) -> Path:
    """sign_update that records its stdin and arguments and prints the fragment."""
    printed = output if output is not None else f'sparkle:edSignature="{SIGNATURE}" length="1234"'
    stub = tmp_path / "sign_update"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(release.stdin_log)!r}, 'ab').write(sys.stdin.buffer.read())\n"
        f"open({str(release.stdin_log) + '.args'!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"print({printed!r})\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def run(
    module,
    release: SimpleNamespace,
    sign_update: Path,
    *,
    tag: str = "v3.8.0",
    key: str | None = None,
    extra: tuple[str, ...] = (),
) -> int:
    return module.main([
        "--zip", str(release.archive),
        "--tag", tag,
        "--out", str(release.out),
        "--ed-key-file", key or str(release.key),
        "--sign-update", str(sign_update),
        *extra,
    ])


def test_appcast_names_the_release_its_signature_and_notes(tmp_path: Path) -> None:
    module = load_module()
    release = make_release(tmp_path)
    sign_update = stub_sign_update(tmp_path, release)

    assert run(module, release, sign_update) == 0

    root = ElementTree.parse(release.out).getroot()
    assert root.tag == "rss" and root.get("version") == "2.0"
    items = root.findall("channel/item")
    assert len(items) == 1
    item = items[0]
    assert item.findtext("title") == "3.8.0"
    assert item.findtext(f"{SPARKLE}version") == "30800"
    assert item.findtext(f"{SPARKLE}shortVersionString") == "3.8.0"
    assert item.findtext(f"{SPARKLE}minimumSystemVersion") == "14.0"
    # A universal app is offered to every Mac.
    assert item.find(f"{SPARKLE}hardwareRequirements") is None
    assert item.findtext(f"{SPARKLE}releaseNotesLink") == "https://github.com/jamesyc/TimeCapsuleSMB/releases/tag/v3.8.0"
    assert parsedate_to_datetime(item.findtext("pubDate") or "").tzinfo is not None
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert enclosure.attrib == {
        "url": "https://github.com/jamesyc/TimeCapsuleSMB/releases/download/v3.8.0/TimeCapsuleSMB.app.zip",
        "length": "1234",
        "type": "application/octet-stream",
        f"{SPARKLE}edSignature": SIGNATURE,
    }
    # The key went to sign_update on stdin, never as a file argument.
    assert release.stdin_log.read_bytes() == PRIVATE_KEY
    assert Path(str(release.stdin_log) + ".args").read_text().split() == ["--ed-key-file", "-", str(release.archive)]


def test_the_key_can_come_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    release = make_release(tmp_path)
    sign_update = stub_sign_update(tmp_path, release)
    monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(PRIVATE_KEY)))

    assert run(module, release, sign_update, key="-") == 0

    assert release.stdin_log.read_bytes() == PRIVATE_KEY


def test_a_key_for_another_app_is_refused_before_signing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = load_module()
    release = make_release(tmp_path, SUPublicEDKey=base64.b64encode(bytes(32)).decode())
    sign_update = stub_sign_update(tmp_path, release)

    assert run(module, release, sign_update) == 1

    assert "does not belong to the app's public key" in capsys.readouterr().err
    assert not release.stdin_log.exists()
    assert not release.out.exists()


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        (b"not base64 !!!\n", "not base64"),
        # The expanded private key and public key, which is not what generate_keys -x writes.
        (base64.b64encode(bytes(64) + PUBLIC_KEY) + b"\n", "is 96 bytes, expected 32"),
    ],
)
def test_a_key_that_is_not_a_sparkle_key_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], key: bytes, reason: str,
) -> None:
    module = load_module()
    release = make_release(tmp_path)
    release.key.write_bytes(key)
    sign_update = stub_sign_update(tmp_path, release)

    assert run(module, release, sign_update) == 1

    assert reason in capsys.readouterr().err
    assert not release.stdin_log.exists()


@pytest.mark.parametrize(
    ("seed", "public_key"),
    [
        # RFC 8032 section 7.1, tests 1 to 3.
        (SEED, PUBLIC_KEY),
        (
            "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
            "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        ),
        (
            "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
            "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        ),
    ],
)
def test_the_public_key_is_derived_as_rfc_8032_does(seed: bytes | str, public_key: bytes | str) -> None:
    module = load_module()

    def as_bytes(value: bytes | str) -> bytes:
        return value if isinstance(value, bytes) else bytes.fromhex(value)

    assert module.ed25519_public_key(as_bytes(seed)) == as_bytes(public_key)


@pytest.mark.parametrize("contents", [b"not a zip", None])
def test_a_zip_without_the_apps_info_plist_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], contents: bytes | None,
) -> None:
    module = load_module()
    release = make_release(tmp_path)
    if contents is None:
        with zipfile.ZipFile(release.archive, "w") as zipped:
            zipped.writestr("Other.app/Contents/Info.plist", b"")
    else:
        release.archive.write_bytes(contents)
    sign_update = stub_sign_update(tmp_path, release)

    assert run(module, release, sign_update) == 1

    assert "no readable TimeCapsuleSMB.app/Contents/Info.plist" in capsys.readouterr().err
    assert not release.stdin_log.exists()
    assert not release.out.exists()


def test_a_tag_that_is_not_the_apps_version_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = load_module()
    release = make_release(tmp_path)
    sign_update = stub_sign_update(tmp_path, release)

    assert run(module, release, sign_update, tag="v3.8.1") == 1

    assert "tag v3.8.1 is not the app's v3.8.0" in capsys.readouterr().err
    assert not release.out.exists()


@pytest.mark.parametrize("key", ["CFBundleVersion", "CFBundleShortVersionString", "LSMinimumSystemVersion", "SUPublicEDKey"])
def test_an_info_plist_without_a_needed_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str,
) -> None:
    module = load_module()
    release = make_release(tmp_path, **{key: None})
    sign_update = stub_sign_update(tmp_path, release)

    assert run(module, release, sign_update) == 1

    assert f"Info.plist has no {key}" in capsys.readouterr().err
    assert not release.out.exists()


@pytest.mark.parametrize(
    ("output", "rc", "reason"),
    [
        ("Error: could not sign", 1, "sign_update failed (rc=1)"),
        ("something else", 0, "printed no signature"),
    ],
)
def test_a_failed_signing_writes_no_appcast(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output: str, rc: int, reason: str,
) -> None:
    module = load_module()
    release = make_release(tmp_path)
    sign_update = stub_sign_update(tmp_path, release, output=output, rc=rc)

    assert run(module, release, sign_update) == 1

    assert reason in capsys.readouterr().err
    assert not release.out.exists()


def test_a_missing_sign_update_says_how_to_point_at_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = load_module()
    release = make_release(tmp_path)

    assert run(module, release, tmp_path / "absent" / "sign_update") == 1

    assert "--sign-update" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("executable", "found"),
    [(thin_executable(ARM64), "arm64"), (thin_executable(X86_64), "x86_64"), (fat_executable(ARM64), "arm64"), (b"#!/bin/sh\n", "none")],
    ids=["thin-arm64", "thin-x86_64", "fat-arm64-only", "not-macho"],
)
def test_an_app_that_is_not_universal_is_refused_before_signing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    executable: bytes,
    found: str,
) -> None:
    module = load_module()
    release = make_release(tmp_path, executable=executable)
    sign_update = stub_sign_update(tmp_path, release)

    assert run(module, release, sign_update) == 1

    err = capsys.readouterr().err
    assert f"is not universal (found: {found})" in err
    assert "--arch universal" in err and "--arm64-only" in err
    assert not release.out.exists()
    assert not release.stdin_log.exists()


def test_a_universal_app_in_a_fat64_header_is_accepted(tmp_path: Path) -> None:
    module = load_module()
    release = make_release(tmp_path, executable=fat_executable(ARM64, X86_64, fat64=True))

    assert run(module, release, stub_sign_update(tmp_path, release)) == 0


def test_arm64_only_offers_the_update_to_apple_silicon_only(tmp_path: Path) -> None:
    module = load_module()
    release = make_release(tmp_path, executable=thin_executable(ARM64))

    assert run(module, release, stub_sign_update(tmp_path, release), extra=("--arm64-only",)) == 0

    item = ElementTree.parse(release.out).getroot().find("channel/item")
    assert item is not None
    assert item.findtext(f"{SPARKLE}hardwareRequirements") == "arm64"


@pytest.mark.parametrize(
    ("executable", "message"),
    [
        (fat_executable(X86_64, ARM64), "still runs on Intel Macs; leave out --arm64-only"),
        (thin_executable(X86_64), "does not run on Apple silicon (found: x86_64)"),
    ],
    ids=["universal", "thin-x86_64"],
)
def test_arm64_only_is_refused_for_an_app_it_does_not_describe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    executable: bytes,
    message: str,
) -> None:
    module = load_module()
    release = make_release(tmp_path, executable=executable)
    sign_update = stub_sign_update(tmp_path, release)

    assert run(module, release, sign_update, extra=("--arm64-only",)) == 1

    assert message in capsys.readouterr().err
    assert not release.out.exists()
    assert not release.stdin_log.exists()


def test_a_zip_without_the_apps_executable_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    module = load_module()
    release = make_release(tmp_path)
    with zipfile.ZipFile(release.archive) as zipped:
        info = zipped.read("TimeCapsuleSMB.app/Contents/Info.plist")
    with zipfile.ZipFile(release.archive, "w") as zipped:
        zipped.writestr("TimeCapsuleSMB.app/Contents/Info.plist", info)
    sign_update = stub_sign_update(tmp_path, release)

    assert run(module, release, sign_update) == 1

    assert "no readable TimeCapsuleSMB.app/Contents/MacOS/TimeCapsuleSMB" in capsys.readouterr().err
    assert not release.out.exists()
