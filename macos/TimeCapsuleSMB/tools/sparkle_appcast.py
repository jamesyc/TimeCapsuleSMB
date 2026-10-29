#!/usr/bin/env python3
"""Write the Sparkle appcast for one release of the packaged app.

The feed has one item: the release being made. Its version, version code and
minimum macOS come from the Info.plist inside the zip itself, so they always
describe the bytes that are signed; its signature comes from Sparkle's
sign_update, and its release notes are the GitHub release page.

Before signing, the private key must belong to the public key packaged into
the app. A release signed with any other key can never be installed by
Sparkle, and nothing later would catch it before it is published.

The app must be universal too. Sparkle installs whatever the feed offers, so
an Apple-silicon-only app offered to Intel Macs would replace a working app
with one that cannot launch, and so can never update itself again.
--arm64-only marks the update as needing Apple silicon instead, for the day
the app stops supporting Intel: Intel Macs are then not offered it.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import plistlib
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from package_app import APP_NAME, DEFAULT_ARCHITECTURES, PRODUCT_NAME, swift_scratch_path  # noqa: E402


SPARKLE_NAMESPACE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
REPOSITORY_URL = "https://github.com/jamesyc/TimeCapsuleSMB"
ZIP_NAME = f"{APP_NAME}.app.zip"
# Where ditto -c -k --keepParent puts the app's Info.plist in the zip.
ZIPPED_INFO_PLIST = f"{APP_NAME}.app/Contents/Info.plist"
ZIPPED_EXECUTABLE = f"{APP_NAME}.app/Contents/MacOS/{PRODUCT_NAME}"
# Mach-O CPU types, from <mach/machine.h>.
MACHO_CPU_TYPES = {0x01000007: "x86_64", 0x0100000C: "arm64"}
# generate_keys -x writes one base64 line: the 32-byte ed25519 seed.
PRIVATE_KEY_BYTES = 32
SIGNATURE_FRAGMENT = re.compile(r'sparkle:edSignature="([^"]+)"\s+length="(\d+)"')


def default_sign_update() -> Path:
    """sign_update from the Sparkle artifact of packaging's universal build.
    package_app.py always builds with --scratch-path, so this is not .build/artifacts."""
    return swift_scratch_path(DEFAULT_ARCHITECTURES) / "artifacts" / "sparkle" / "Sparkle" / "bin" / "sign_update"


def app_info(archive: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(archive) as zipped:
            info = plistlib.loads(zipped.read(ZIPPED_INFO_PLIST))
    except (KeyError, zipfile.BadZipFile, plistlib.InvalidFileException) as exc:
        raise RuntimeError(f"{archive}: no readable {ZIPPED_INFO_PLIST}: {exc}") from exc
    values: dict[str, str] = {}
    for key in ("CFBundleVersion", "CFBundleShortVersionString", "LSMinimumSystemVersion", "SUPublicEDKey"):
        value = info.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{archive}: Info.plist has no {key}")
        values[key] = value.strip()
    return values


def macho_architectures(header: bytes) -> set[str]:
    """The architectures a Mach-O file's first bytes declare: each slice of a
    universal file's big-endian fat header, or a thin 64-bit file's own."""
    magic = int.from_bytes(header[:4], "big")
    if magic in (0xCAFEBABE, 0xCAFEBABF):
        entry_size = 20 if magic == 0xCAFEBABE else 32
        count = int.from_bytes(header[4:8], "big")
        cpu_types = [int.from_bytes(header[8 + index * entry_size:12 + index * entry_size], "big") for index in range(count)]
    elif magic == 0xCFFAEDFE:
        cpu_types = [int.from_bytes(header[4:8], "little")]
    else:
        return set()
    return {MACHO_CPU_TYPES.get(cpu_type, hex(cpu_type)) for cpu_type in cpu_types}


def check_architectures(archive: Path, *, arm64_only: bool) -> None:
    """The app's executable must run on every Mac the update is offered to."""
    try:
        with zipfile.ZipFile(archive) as zipped, zipped.open(ZIPPED_EXECUTABLE) as executable:
            architectures = macho_architectures(executable.read(4096))
    except (KeyError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"{archive}: no readable {ZIPPED_EXECUTABLE}: {exc}") from exc
    found = ", ".join(sorted(architectures)) or "none"
    if arm64_only:
        if "arm64" not in architectures:
            raise RuntimeError(f"{ZIPPED_EXECUTABLE} does not run on Apple silicon (found: {found})")
        if "x86_64" in architectures:
            raise RuntimeError(f"{ZIPPED_EXECUTABLE} still runs on Intel Macs; leave out --arm64-only")
    elif not {"arm64", "x86_64"} <= architectures:
        raise RuntimeError(
            f"{ZIPPED_EXECUTABLE} is not universal (found: {found}); package it with --arch universal, "
            "or pass --arm64-only if the app no longer supports Intel Macs"
        )


def ed25519_public_key(seed: bytes) -> bytes:
    """The public key of an ed25519 private key seed (RFC 8032 section 5.1.5).
    The standard library has no ed25519; this is the RFC's reference
    arithmetic, enough to derive one key and never used to sign."""
    p = 2**255 - 19
    d = -121665 * pow(121666, p - 2, p) % p

    def add(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x1, y1, z1, t1 = a
        x2, y2, z2, t2 = b
        e = (y1 - x1) * (y2 - x2) % p
        f = (y1 + x1) * (y2 + x2) % p
        g = t1 * 2 * d * t2 % p
        h = z1 * 2 * z2 % p
        return ((f - e) * (h - g) % p, (h + g) * (f + e) % p, (h - g) * (h + g) % p, (f - e) * (f + e) % p)

    y = 4 * pow(5, p - 2, p) % p
    xx = (y * y - 1) * pow(d * y * y + 1, p - 2, p) % p
    x = pow(xx, (p + 3) // 8, p)
    if (x * x - xx) % p:
        x = x * pow(2, (p - 1) // 4, p) % p
    if x & 1:
        x = p - x
    point, result = (x, y, 1, x * y % p), (0, 1, 1, 0)
    digest = hashlib.sha512(seed).digest()
    scalar = (int.from_bytes(digest[:32], "little") & ((1 << 254) - 8)) | (1 << 254)
    while scalar:
        if scalar & 1:
            result = add(result, point)
        point = add(point, point)
        scalar >>= 1
    x, y, z, _ = result
    z_inverse = pow(z, p - 2, p)
    x, y = x * z_inverse % p, y * z_inverse % p
    return (y | (x & 1) << 255).to_bytes(32, "little")


def check_key_matches_app(private_key: bytes, public_key_text: str) -> None:
    """The key file is one base64 line holding the 32-byte seed; the public
    key derived from it must be the app's SUPublicEDKey."""
    try:
        seed = base64.b64decode(private_key.strip(), validate=True)
        public_key = base64.b64decode(public_key_text, validate=True)
    except binascii.Error as exc:
        raise RuntimeError(f"the Sparkle key is not base64: {exc}") from exc
    if len(seed) != PRIVATE_KEY_BYTES:
        raise RuntimeError(
            f"the Sparkle private key is {len(seed)} bytes, expected {PRIVATE_KEY_BYTES} "
            "(the one line generate_keys -x writes)"
        )
    key_public = ed25519_public_key(seed)
    if key_public != public_key:
        raise RuntimeError(
            "the Sparkle private key does not belong to the app's public key: "
            f"key {base64.b64encode(key_public).decode()}, app {public_key_text}"
        )


def sign(sign_update: Path, private_key: bytes, archive: Path) -> tuple[str, int]:
    if not sign_update.is_file():
        raise RuntimeError(f"sign_update not found at {sign_update}; build the app first or pass --sign-update")
    completed = subprocess.run(
        [str(sign_update), "--ed-key-file", "-", str(archive)],
        input=private_key,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    output = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise RuntimeError(f"sign_update failed (rc={completed.returncode}): {completed.stderr.decode('utf-8', 'replace').strip()}")
    match = SIGNATURE_FRAGMENT.search(output)
    if match is None:
        raise RuntimeError(f"sign_update printed no signature: {output.strip()!r}")
    return match.group(1), int(match.group(2))


def appcast_xml(
    *,
    tag: str,
    info: dict[str, str],
    signature: str,
    length: int,
    published: datetime,
    arm64_only: bool = False,
) -> bytes:
    ElementTree.register_namespace("sparkle", SPARKLE_NAMESPACE)

    def sparkle(name: str) -> str:
        return f"{{{SPARKLE_NAMESPACE}}}{name}"

    rss = ElementTree.Element("rss", {"version": "2.0"})
    channel = ElementTree.SubElement(rss, "channel")
    ElementTree.SubElement(channel, "title").text = "TimeCapsuleSMB"
    item = ElementTree.SubElement(channel, "item")
    ElementTree.SubElement(item, "title").text = info["CFBundleShortVersionString"]
    ElementTree.SubElement(item, "pubDate").text = format_datetime(published)
    ElementTree.SubElement(item, sparkle("version")).text = info["CFBundleVersion"]
    ElementTree.SubElement(item, sparkle("shortVersionString")).text = info["CFBundleShortVersionString"]
    ElementTree.SubElement(item, sparkle("minimumSystemVersion")).text = info["LSMinimumSystemVersion"]
    if arm64_only:
        ElementTree.SubElement(item, sparkle("hardwareRequirements")).text = "arm64"
    ElementTree.SubElement(item, sparkle("releaseNotesLink")).text = f"{REPOSITORY_URL}/releases/tag/{tag}"
    ElementTree.SubElement(item, "enclosure", {
        "url": f"{REPOSITORY_URL}/releases/download/{tag}/{ZIP_NAME}",
        "length": str(length),
        "type": "application/octet-stream",
        sparkle("edSignature"): signature,
    })
    ElementTree.indent(rss)
    return ElementTree.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the Sparkle appcast for a packaged TimeCapsuleSMB release.")
    parser.add_argument("--zip", type=Path, required=True, help="The release's TimeCapsuleSMB.app.zip.")
    parser.add_argument("--tag", required=True, help="The release tag, such as v3.8.0.")
    parser.add_argument("--out", type=Path, required=True, help="Where to write appcast.xml.")
    parser.add_argument("--ed-key-file", required=True,
                        help="The Sparkle private key file generate_keys -x wrote, or - to read it from stdin.")
    parser.add_argument("--sign-update", type=Path, default=None, help="Sparkle's sign_update; defaults to packaging's.")
    parser.add_argument("--arm64-only", action="store_true",
                        help="The app no longer supports Intel Macs: offer the update to Apple silicon only.")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        info = app_info(args.zip)
        if args.tag != f"v{info['CFBundleShortVersionString']}":
            raise RuntimeError(f"tag {args.tag} is not the app's v{info['CFBundleShortVersionString']}")
        check_architectures(args.zip, arm64_only=args.arm64_only)
        private_key = sys.stdin.buffer.read() if args.ed_key_file == "-" else Path(args.ed_key_file).read_bytes()
        check_key_matches_app(private_key, info["SUPublicEDKey"])
        signature, length = sign(args.sign_update or default_sign_update(), private_key, args.zip)
        args.out.write_bytes(appcast_xml(
            tag=args.tag,
            info=info,
            signature=signature,
            length=length,
            published=datetime.now(timezone.utc),
            arm64_only=args.arm64_only,
        ))
    except (OSError, RuntimeError) as exc:
        print(f"sparkle_appcast: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {args.out} for {args.tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
