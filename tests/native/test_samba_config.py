"""Behavioral replacement for shell share projection and Samba configuration."""
import configparser
import importlib.util
import plistlib
import re
import shlex
import subprocess
import unicodedata

import pytest

from tests.native.build import ROOT, compile_modules
from tests.storage_fixtures import MAST_FIXTURES
from timecapsulesmb.core.smb_config import apple_precompose, parse_active_share_names


@pytest.fixture(scope="module")
def renderer(tmp_path_factory):
    root = tmp_path_factory.mktemp("samba-config")
    binary, config = root / "render", root / "runtime.conf"
    modules = ["native/storage/mast.c", "native/storage/shares.c",
               "native/samba/config.c", "native/common/config.c"]
    compile_modules(binary, modules,
                    flags=(f'-DTC_FLASH_CONFIG_PATH="{config}"', "-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_samba_config.c",))
    return binary, config


def render(renderer, options=None, *, inventory=None, args=()):
    binary, config = renderer
    config.write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in (options or {}).items()))
    raw = inventory or next(f.raw for f in MAST_FIXTURES if f.name == "openstep_duplicate_internal_external_names")
    if isinstance(raw, str): raw = raw.encode()
    result = subprocess.run([str(binary), *args], input=raw, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    parsed = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parsed.read_string(result.stdout.decode())
    return parsed


@pytest.mark.parametrize("args", [(), ("netbsd4",)])
def test_default_config_preserves_the_working_shell_settings(renderer, args):
    conf = render(renderer, args=args)
    global_ = conf["global"]
    assert "interfaces" not in global_
    assert "bind interfaces only" not in global_
    assert global_["fruit:model"] == "TimeCapsule6,116"
    assert global_["lock directory"] == "/mnt/Locks"
    assert global_["cache directory"] == ("/Volumes/dk2/.samba4/cache" if args else "/mnt/Memory/samba4/var")
    assert global_["log file"] == "/Volumes/dk2/.samba4/logs/log.smbd"
    assert global_["max log size"] == "128" and "log level" not in global_
    assert global_["min protocol"] == "SMB2" and global_["max protocol"] == "SMB3"
    assert global_["deadtime"] == "720" and global_["smb3 directory leases"] == "no"
    assert global_.getint("max smbd processes") == 12
    assert global_["aio read size"] == global_["aio write size"] == "1"
    assert global_["passdb backend"].endswith("/private/smbpasswd")
    assert global_["username map"].endswith("/private/username.map")
    assert set(conf.sections()) == {"global", "Data", "Data (dk3)"}
    assert conf["Data"]["path"] == "/Volumes/dk2/ShareRoot"
    assert conf["Data (dk3)"]["path"] == "/Volumes/dk3"
    for name in conf.sections()[1:]:
        share = conf[name]
        assert share["fruit:metadata"] == "netatalk"
        assert share["fruit:resource"] == "file"
        assert share["xattr_tdb:file"] == "/Volumes/dk2/.samba4/private/xattr.tdb"
        assert share["vfs objects"] == "catia fruit streams_xattr acl_xattr xattr_tdb aio_fork"
        assert share["aio_fork:max_children"] == "2"
        assert share["tc:native symlinks"] == "yes"
        assert share["veto files"] == "/.samba4/.tc-xsym.*/" and share["delete veto files"] == "yes"
        assert share["smbd max xattr size"] == "3802" and share["streams_xattr:max xattrs per stream"] == "35"
        assert global_["tc:volume " + share["tc:volume device"]] == share["tc:volume uuid"] + "|" + share["path"]


def test_netbsd4_cache_remains_disk_backed(renderer):
    conf = render(renderer, args=("netbsd4",))
    assert conf["global"]["cache directory"] == "/Volumes/dk2/.samba4/cache"


@pytest.mark.parametrize("args,socket_options", [
    ((), "TCP_NODELAY SO_RCVBUF=2097152"),
    (("netbsd4",), "TCP_NODELAY SO_SNDBUF=229376 SO_RCVBUF=393216"),
])
def test_socket_buffers_balance_throughput_and_each_kernels_pool(renderer, args, socket_options):
    # Exercise the production renderer with AIO on and off: the fixed TCP
    # buffers apply to both paths, and NetBSD 6 keeps its existing options.
    for options in (None, {"VFS_AIO_FORK_ENABLED": 1}, {"VFS_AIO_FORK_ENABLED": 0}):
        conf = render(renderer, options, args=args)
        assert conf["global"]["socket options"] == socket_options


@pytest.mark.parametrize("args", [(), ("netbsd4",)])
def test_smb3_algorithms_prefer_the_cheapest_on_these_cpus(renderer, args):
    # Samba takes its first algorithm the client offers, so the order is the
    # preference. No lane's nettle has GHASH assembly: GMAC and GCM go last
    # on both, and the slower AES-256-CCM still beats AES-128-GCM.
    for options in (None, {"VFS_AIO_FORK_ENABLED": 0}):
        global_ = render(renderer, options, args=args)["global"]
        assert global_["server smb3 signing algorithms"] == "HMAC-SHA256 AES-128-CMAC AES-128-GMAC"
        assert global_["server smb3 encryption algorithms"] == "AES-128-CCM AES-256-CCM AES-128-GCM AES-256-GCM"


@pytest.mark.parametrize("options,aio", [
    ({}, True),  # deploy always writes the key; aio_fork is on without it too
    ({"VFS_AIO_FORK_ENABLED": 1}, True),
    ({"VFS_AIO_FORK_ENABLED": 0}, False),
])
def test_aio_fork_is_on_unless_the_config_turns_it_off(renderer, options, aio):
    conf = render(renderer, options)
    assert conf["global"]["aio read size"] == conf["global"]["aio write size"] == ("1" if aio else "0")
    for name in conf.sections()[1:]:
        assert conf[name]["vfs objects"].endswith(" aio_fork") == aio
        assert ("aio_fork:max_children" in conf[name]) == aio


def test_tested_aio_and_debug_preferences(renderer):
    conf = render(renderer, {"VFS_AIO_FORK_ENABLED": 1, "SMBD_DEBUG_LOGGING": 1})
    # aio_fork's helper buffers cover Samba's default SMB2 sizes, so none are set.
    assert "smb2 max read" not in conf["global"] and "smb2 max write" not in conf["global"]
    assert conf["global"]["aio read size"] == conf["global"]["aio write size"] == "1"
    assert conf["global"]["max log size"] == "0" and conf["global"]["log level"] == "10"
    assert conf["Data"]["aio_fork:max_children"] == "2"
    assert conf["Data"]["vfs objects"].endswith(" aio_fork")


@pytest.mark.parametrize("options,expected,absent", [
    # By default the client decides about signing and encryption.
    ({}, {"min protocol": "SMB2", "max protocol": "SMB3"},
     ["server min protocol", "ntlm auth", "smb1 unix extensions", "server signing", "server smb encrypt"]),
    # Samba's own default minimum is SMB 2.0.2, so SMB1 must be named, and its
    # older clients often log in only with NTLMv1. SMB1 Unix extensions would
    # bypass fruit and the native link handling.
    ({"ANY_PROTOCOL": 1},
     {"server min protocol": "NT1", "ntlm auth": "ntlmv1-permitted", "smb1 unix extensions": "no"},
     ["min protocol", "max protocol"]),
    ({"ANY_PROTOCOL": 1, "DISABLE_SMB_ENCRYPTION": 1},
     {"server min protocol": "NT1", "ntlm auth": "ntlmv1-permitted", "smb1 unix extensions": "no",
      "server smb encrypt": "off"}, ["min protocol", "server signing"]),
    ({"REQUIRE_SMB_ENCRYPTION": 1}, {"server smb encrypt": "required", "server min protocol": "SMB3_00"},
     ["min protocol", "ntlm auth", "smb1 unix extensions"]),
    # Configure refuses this pair, but a hand-edited Flash config can hold it:
    # required encryption wins and SMB1 stays off.
    ({"REQUIRE_SMB_ENCRYPTION": 1, "ANY_PROTOCOL": 1},
     {"server smb encrypt": "required", "server min protocol": "SMB3_00", "server max protocol": "SMB3"},
     ["min protocol", "ntlm auth", "smb1 unix extensions"]),
    # The old key turns off encryption only: "server signing = disabled" does
    # nothing for SMB2/3, so it is not written.
    ({"DISABLE_SMB_ENCRYPTION": 1}, {"server smb encrypt": "off", "min protocol": "SMB2"},
     ["server min protocol", "ntlm auth", "smb1 unix extensions", "server signing"]),
    ({"REQUIRE_SMB_SIGNING": 1}, {"server signing": "mandatory", "min protocol": "SMB2", "max protocol": "SMB3"},
     ["server smb encrypt", "server min protocol"]),
    # Signed but never encrypted.
    ({"REQUIRE_SMB_SIGNING": 1, "DISABLE_SMB_ENCRYPTION": 1},
     {"server signing": "mandatory", "server smb encrypt": "off"}, []),
    ({"REQUIRE_SMB_SIGNING": 1, "REQUIRE_SMB_ENCRYPTION": 1},
     {"server signing": "mandatory", "server smb encrypt": "required", "server min protocol": "SMB3_00"}, []),
    # SMB1 clients have to sign too.
    ({"REQUIRE_SMB_SIGNING": 1, "ANY_PROTOCOL": 1},
     {"server signing": "mandatory", "server min protocol": "NT1"}, ["server smb encrypt"]),
    ({"REQUIRE_SMB_SIGNING": 0}, {"min protocol": "SMB2"}, ["server signing"]),
])
def test_protocol_preferences(renderer, options, expected, absent):
    global_ = render(renderer, options)["global"]
    for key, value in expected.items(): assert global_[key] == value
    for key in absent: assert key not in global_


def test_root_browse_and_metadata_preferences(renderer):
    conf = render(renderer, {"INTERNAL_SHARE_USE_DISK_ROOT": 1, "SMB_BROWSE_COMPATIBILITY": 1, "FRUIT_METADATA_NETATALK": 0})
    assert conf["Data"]["path"] == "/Volumes/dk2"
    assert conf["Data"]["fruit:metadata"] == "stream"
    assert conf["global"]["restrict anonymous"] == "0"


@pytest.mark.parametrize("options,args", [
    ({}, ()),
    ({"VFS_AIO_FORK_ENABLED": 1, "SMBD_DEBUG_LOGGING": 1}, ()),
    ({"INTERNAL_SHARE_USE_DISK_ROOT": 1, "FRUIT_METADATA_NETATALK": 0}, ("netbsd4",)),
    ({}, ("skip-first",)),
])
def test_every_share_lists_dos_device_names_as_stored(renderer, options, args):
    # Samba's default, "illegal", lists AUX, CON, NUL, ... under 8.3 aliases
    # that a Mac's lookup of the real name cannot find (issue 347).
    conf = render(renderer, options, args=args)
    shares = conf.sections()[1:]
    assert shares
    for name in shares:
        assert conf[name].get("mangled names", conf["global"].get("mangled names", "illegal")) == "no", name


def test_unavailable_volume_not_projected_and_usb_payload_remains_a_share(renderer):
    conf = render(renderer, args=("skip-first",))
    assert conf.sections() == ["global", "Data"]
    assert conf["Data"]["path"] == "/Volumes/dk3"
    assert conf["Data"]["veto files"] == "/.samba4/.tc-xsym.*/"
    # Patch 0060 moves a converted file aside under .tc-xsym.*; a leftover must
    # not keep its folder from being deleted over SMB.
    assert conf["Data"]["delete veto files"] == "yes"
    assert "tc:volume dk2" not in conf["global"]
    assert conf["global"]["tc:volume dk3"] == conf["Data"]["tc:volume uuid"] + "|" + conf["Data"]["path"]


def test_names_sanitized_bounded_and_ascii_case_collisions_disambiguated(renderer):
    names = [' /Bad:*=Name[]?"<>|,\\ ', 'data', 'DATA', '中é' * 70]
    parts = [{"deviceName": f"dk{i+2}", "format": "hfs", "name": name,
              "uuid": f"00000000-0000-0000-0000-{i+1:012x}"} for i, name in enumerate(names)]
    conf = render(renderer, inventory=plistlib.dumps([{"deviceName": "sd0", "partitions": parts}]))
    assert conf.sections()[1:4] == ['_Bad___Name_________', 'data', 'DATA (dk4)']
    last = conf.sections()[-1]
    assert len(last.encode()) <= 194 and last.encode().decode() == last


def test_space_runs_collapse_so_the_advertised_name_is_the_served_name(renderer):
    # Samba serves "[A  B]" as "A B" and matches tree connects exactly, so the
    # name used in smb.conf and the ADisk TXT must already be collapsed. Two
    # volumes that differ only in spacing then collide and are disambiguated.
    names = ["Nicholas  McBride's Time Ca", "Nicholas McBride's Time Ca", "Tab\tName", "One Space"]
    parts = [{"deviceName": f"dk{i+2}", "format": "hfs", "name": name,
              "uuid": f"00000000-0000-0000-0000-{i+1:012x}"} for i, name in enumerate(names)]
    conf = render(renderer, inventory=plistlib.dumps([{"deviceName": "sd0", "partitions": parts}]))
    assert conf.sections()[1:] == [
        "Nicholas McBride's Time Ca", "Nicholas McBride's Time Ca (dk3)", "Tab_Name", "One Space",
    ]


def render_names(renderer, names):
    """Render one share per volume name (a str, or bytes that need not be
    UTF-8); return the ADisk names the manager would advertise and smb.conf's
    raw section names, both as bytes, and smb.conf itself."""
    marker = "\ue000{}\ue001".format
    parts = [{"deviceName": f"dk{i+2}", "format": "hfs", "name": marker(i) if isinstance(name, bytes) else name,
              "uuid": f"00000000-0000-0000-0000-{i+1:012x}"} for i, name in enumerate(names)]
    inventory = plistlib.dumps([{"deviceName": "sd0", "partitions": parts}])
    for i, name in enumerate(names):
        if isinstance(name, bytes):
            inventory = inventory.replace(marker(i).encode(), name)
    binary, config = renderer
    config.write_text("")
    result = subprocess.run([str(binary)], input=inventory, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    adisk = [line[len(b"adisk="):] for line in result.stderr.splitlines() if line.startswith(b"adisk=")]
    sections = [line[1:-1] for line in result.stdout.splitlines() if line.startswith(b"[") and line != b"[global]"]
    return adisk, sections, result.stdout


def raw_share_sections(renderer, names):
    """smb.conf's raw section names (as the ADisk TXT carries them) and the
    names Samba's parser serves."""
    _, sections, smb_conf = render_names(renderer, names)
    return [section.decode() for section in sections], parse_active_share_names(smb_conf.decode())


@pytest.mark.parametrize("names,expected", [
    # The ADisk budget for a 3-byte device name and a 36-byte UUID is 194
    # bytes. Cut there, this name would end in a space, which Samba drops.
    (["A" * 193 + " " + "B" * 20], ["A" * 193]),
    # A collision cuts 6 bytes earlier for " (dk3)". Ending that cut in a
    # space would leave "C...C  (dk3)", which Samba serves as "C...C (dk3)".
    (["C" * 187 + " " + "D" * 20] * 2, ["C" * 187 + " " + "D" * 6, "C" * 187 + " (dk3)"]),
])
def test_cut_names_do_not_end_in_a_space_samba_would_change(renderer, names, expected):
    raw, served = raw_share_sections(renderer, names)
    assert raw == expected
    # Time Machine asks for the advertised name; Samba must serve exactly it.
    assert served == raw


def adisk_and_sections(renderer, names):
    adisk, sections, _ = render_names(renderer, names)
    return [name.decode() for name in adisk], [section.decode() for section in sections]


@pytest.mark.parametrize("name", [
    # Apple's own default names and names seen on devices, as HFS returns them.
    "AirMac ディスク", "AirPort 디스크", "AirPort-hårddisk", "Time Capsule de Carlão",
    "Valentin Müllers Time Caps", "Vidéos Réseau",
    # Hangul with a final consonant, two marks on one letter, a two-step composite.
    "한글 백업", "Phở Việt", "ǖ",
])
def test_sections_are_precomposed_and_adisk_keeps_the_hfs_spelling(renderer, name):
    decomposed = unicodedata.normalize("NFD", name)
    adisk, raw = adisk_and_sections(renderer, [decomposed])
    # Apple's diskd advertises the name as HFS stores it; macOS composes it and
    # sends that in the tree connect, which Samba matches byte for byte.
    assert adisk == [decomposed]
    assert raw == [unicodedata.normalize("NFC", name)]


@pytest.mark.parametrize("name, section", [
    # A mark with nothing before it, and marks that compose with nothing.
    ("\u0301Data", "\u0301Data"),
    ("q\u0323\u0307", "q\u0323\u0307"),
    # The second mark meets the composite of the first; no composite exists.
    ("a\u0323\u0301", "\u1ea1\u0301"),
    # A mark combines only into the character just before it: NFC would give
    # U+1E8D U+0323 here, but Apple (and so the Mac) leaves it.
    ("x\u0323\u0308", "x\u0323\u0308"),
    # A composition exclusion stays decomposed.
    ("\u0915\u093c", "\u0915\u093c"),
    # Apple's table is Unicode 3.2's: it composes the U+2000-2FFF pairs HFS
    # does not decompose, and not Balinese (Unicode 5.0).
    ("\u2203\u0338", "\u2204"),
    ("\u1b05\u1b35", "\u1b05\u1b35"),
    # Hangul: U+11A7 is no final consonant, and archaic vowels from U+1176
    # make no syllable.
    ("\uac00\u11a7", "\uac00\u11a7"),
    ("\u1100\u1176", "\u1100\u1176"),
    # Precomposed and characters without decompositions pass through.
    ("Café AirPort磁盘 ♨️", "Café AirPort磁盘 ♨️"),
])
def test_composition_edges(renderer, name, section):
    adisk, raw = adisk_and_sections(renderer, [name])
    assert adisk == [name]
    assert raw == [section] == [apple_precompose(name)]


@pytest.mark.parametrize("raw_name, expected", [
    # Each byte of a sequence that is not UTF-8 becomes '_'. A stray
    # continuation byte, a cut-off sequence before a mark, and an invalid lead
    # byte; none may swallow or combine a neighbour.
    (b"A\x80e\xcc e\xcc\x81 \xffx", "A_e_ e\u0301 _x"),
    # Overlong forms of a newline, '=', '/', ']' and 'A'.
    (b"x\xc0\x8a\xc0\xbd\xc0\xaf\xc1\x9d\xe0\x81\x81\xf0\x80\x81\x81y", "x" + "_" * 15 + "y"),
    # A surrogate, a code point past U+10FFFF, a lead byte no form uses, and
    # a sequence cut off at the end of the name.
    (b"s\xed\xa0\x80 \xf4\x90\x80\x80 \xf5\x80 \xe3\x81", "s___ ____ __ __"),
    # U+FFFE and U+FFFF, which the Mac kernel's decoder refuses: a Mac cannot
    # ask for a name holding them.
    ("a\ufffe b\uffff".encode(), "a___ b___"),
    # The longest valid forms and the last character below U+FFFE stay whole.
    ("\U0010fffd \U0001f600 \ufffd".encode(), "\U0010fffd \U0001f600 \ufffd"),
])
def test_bytes_that_are_not_utf8_become_underscores_in_both_names(renderer, raw_name, expected):
    adisk, sections, _ = render_names(renderer, [raw_name])
    assert adisk == [expected.encode()]
    assert sections == [apple_precompose(expected).encode()]


def test_an_overlong_newline_cannot_add_a_line_to_smb_conf(renderer):
    # Overlong '\n', '=' and '/' spell out a parameter line after the section.
    newline, equals, slash = b"\xc0\x8a", b"\xc0\xbd", b"\xc0\xaf"
    raw = b"x" + newline + b"    root preexec " + equals + b" " + slash + b"bin" + slash + b"sh"
    _, sections, smb_conf = render_names(renderer, [raw])
    assert sections == [b"x__ root preexec __ __bin__sh"]
    assert b"root preexec" not in smb_conf.replace(sections[0], b"")


def test_composed_sections_collide_only_when_the_names_do(renderer):
    # Collisions are judged on the section Samba sees: distinct decomposed
    # names stay distinct once composed.
    names = [unicodedata.normalize("NFD", n) for n in ("Café", "Café", "Café 2", "Cafe")]
    adisk, raw = adisk_and_sections(renderer, names)
    assert raw == [apple_precompose(n) for n in adisk]
    assert raw == ["Café", "Café (dk3)", "Café 2", "Cafe"]


def test_names_that_compose_alike_get_distinct_sections(renderer):
    # HFS leaves U+2204 whole and keeps U+2203 U+0338 as typed; Apple composes
    # both to U+2204, so the second disk takes a suffix in both names.
    adisk, raw = adisk_and_sections(renderer, ["\u2204", "\u2203\u0338"])
    assert adisk == ["\u2204", "\u2203\u0338 (dk3)"]
    assert raw == ["\u2204", "\u2204 (dk3)"]


@pytest.mark.parametrize("names, sections", [
    # Samba lowercases with its own table: ASCII as before, Greek, circled
    # and full-width letters too, and both cases of a composed letter.
    (["Data", "data"], ["Data", "data (dk3)"]),
    (["\u03a3", "\u03c3"], ["\u03a3", "\u03c3 (dk3)"]),
    (["\u24b6", "\u24d0", "\uff21", "\uff41"], ["\u24b6", "\u24d0 (dk3)", "\uff21", "\uff41 (dk5)"]),
    (["Cafe\u0301", "CAFE\u0301"], ["Caf\u00e9", "CAF\u00c9 (dk3)"]),
    (["\u01c4", "\u01c6"], ["\u01c4", "\u01c6 (dk3)"]),
    # Where Python's lower() folds and Samba's table does not, Samba serves
    # two shares and neither takes a suffix.
    (["\u01c5", "\u01c6"], ["\u01c5", "\u01c6"]),
    (["\u01f8", "\u01f9"], ["\u01f8", "\u01f9"]),
])
def test_sections_samba_lowercases_alike_collide(renderer, names, sections):
    adisk, raw = adisk_and_sections(renderer, names)
    assert raw == sections
    assert raw == [apple_precompose(name) for name in adisk]


@pytest.mark.parametrize("name, reserved", [
    # Samba opens the global section for these, ignoring case and spaces.
    ("Global", True), ("GLOBALS", True), ("Glo bal", True), ("global s", True),
    # A print share, by the same comparison.
    ("Printers", True), ("Print ers", True),
    # Each user's home directory, and Samba's IPC service, by exact name.
    ("homes", True), ("HOMES", True), ("IPC$", True), ("ipc$", True),
    ("Global2", False), ("ho mes", False), ("IPC", False), ("printer", False),
    ("ADMIN$", False), ("My Global", False),
])
def test_names_samba_reserves_take_a_suffix(renderer, name, reserved):
    adisk, raw = adisk_and_sections(renderer, [name])
    # The ADisk name follows the section, so a Mac asks for what Samba serves.
    assert adisk == raw == [f"{name} (dk2)" if reserved else name]


@pytest.mark.parametrize("name, served", [
    # Samba expands these before it compares a section with the name a Mac
    # asks for, so the share could never be opened.
    ("50%Done", "50_Done"), ("Data %U", "Data _U"), ("%h backup", "_h backup"),
    ("%$(HOME)", "_$(HOME)"), ("%T%t", "_T_t"),
    # A % Samba leaves alone stays: before a space, a letter it does not
    # expand, another %, or the end of the name.
    ("100% Backup", "100% Backup"), ("Data %u", "Data %u"), ("100%%", "100%%"),
    ("Mix %X", "Mix %X"), ("Data%", "Data%"),
])
def test_percent_sequences_samba_expands_become_underscores(renderer, name, served):
    adisk, raw = adisk_and_sections(renderer, [name])
    assert adisk == raw == [served]


def test_case_table_matches_the_pinned_samba():
    # Samba's lowercase table can change with a new release; regenerate the
    # header from that release when SAMBA4X_VERSION moves.
    header = (ROOT / "build/native/storage/case_table.h").read_text().splitlines()[0]
    pinned = re.search(r'^SAMBA4X_VERSION="\$\{SAMBA4X_VERSION:-([^}]+)\}"', (ROOT / "build/env.sh").read_text(), re.M)
    assert pinned, "SAMBA4X_VERSION default not found in build/env.sh"
    assert f"from Samba {pinned.group(1)} lowcase_table" in header, (
        f"case_table.h was generated from another Samba: {header!r}; run "
        f"python3 build/native/storage/gen_case_table.py SAMBA/lib/util/charset/codepoints.c {pinned.group(1)}")


def test_case_table_is_samba_lowercase():
    text = (ROOT / "build/native/storage/case_table.h").read_text()
    runs = [tuple(int(v, 0) for v in row) for row in
            re.findall(r"\{(0x[0-9a-f]+), (0x[0-9a-f]+), (0x[0-9a-f]+), (\d+)\}", text)]
    assert runs and all(first <= last and step in (1, 2) and (last - first) % step == 0
                        for first, last, _, step in runs)
    assert all(a[1] < b[0] for a, b in zip(runs, runs[1:]))

    def lower(cp):
        return next(((cp + delta) & 0xFFFF for first, last, delta, step in runs
                     if first <= cp <= last and (cp - first) % step == 0), cp)
    # Spot values from Samba's lowcase_table, including code points where it
    # and Python's lower() disagree.
    assert [lower(ord(c)) for c in "AZ\u00c0\u03a3\u0410\u24b6\uff21"] == [ord(c) for c in "az\u00e0\u03c3\u0430\u24d0\uff41"]
    assert [lower(cp) for cp in (0x1c4, 0x1c5, 0x1f8, 0x130, 0x212a, 0x1e9e, 0x10a0)] == [
        0x1c6, 0x1c5, 0x1f8, 0x130, 0x212a, 0x1e9e, 0x10a0]


def test_every_decomposable_character_composes_as_apple_firmware_does(renderer):
    # Every BMP character with a canonical decomposition, and every Hangul
    # syllable, in the decomposed form HFS returns, packed into volume names.
    # A space between characters keeps one character's marks off the next.
    chars = [unicodedata.normalize("NFD", chr(cp)) for cp in range(0x10000)
             if not 0xd800 <= cp < 0xe000 and unicodedata.normalize("NFD", chr(cp)) != chr(cp)]
    names, current = [], ""
    for text in chars:
        if current and len(f"{current} {text}".encode()) > 150:
            names.append(current)
            current = ""
        current = f"{current} {text}" if current else text
    names.append(current)
    composed = 0
    for start in range(0, len(names), 16):
        adisk, raw = adisk_and_sections(renderer, names[start:start + 16])
        # Sanitizing (= < > become _) applies to both; the section follows the ADisk name.
        assert len(adisk) == len(raw) == len(names[start:start + 16])
        for advertised, section in zip(adisk, raw):
            assert section == apple_precompose(advertised)
            composed += section != advertised
    # Nearly every name holds a pair Apple composes.
    assert composed > len(names) * 0.9


def test_compose_table_matches_its_generator():
    # The generator formats doctor's apple_composites(), so this also proves
    # the device composes with the pairs doctor compares by.
    spec = importlib.util.spec_from_file_location("gen_compose_table", ROOT / "build/native/storage/gen_compose_table.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    assert generator.render() == (ROOT / "build/native/storage/compose_table.h").read_text()


@pytest.mark.parametrize("text", ["RSYNC_ENABLED=maybe\n", "TELEMETRY=$(touch bad)\n", "REQUIRE_SMB_SIGNING=maybe\n",
                                 "REQUIRE_SMB_ENCRYPTION=1\nDISABLE_SMB_ENCRYPTION=1\n",
                                 "RSYNC_ENABLED='unterminated\n"])
def test_invalid_configuration_is_rejected_before_render(renderer, text):
    binary, config = renderer
    config.write_text(text)
    result = subprocess.run([str(binary)], input=b"[]", capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b""
