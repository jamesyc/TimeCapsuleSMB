"""Behavioral replacement for shell share projection and Samba configuration."""
import configparser
import plistlib
import shlex
import subprocess

import pytest

from tests.native.build import ROOT, compile_modules
from tests.storage_fixtures import MAST_FIXTURES
from timecapsulesmb.core.smb_config import parse_active_share_names


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


def test_default_config_preserves_the_working_shell_settings(renderer):
    conf = render(renderer)
    global_ = conf["global"]
    assert "interfaces" not in global_
    assert "bind interfaces only" not in global_
    assert global_["fruit:model"] == "TimeCapsule6,116"
    assert global_["lock directory"] == "/mnt/Locks"
    assert global_["cache directory"] == "/mnt/Memory/samba4/var"
    assert global_["log file"] == "/Volumes/dk2/.samba4/logs/log.smbd"
    assert global_["max log size"] == "128" and "log level" not in global_
    assert global_["min protocol"] == "SMB2" and global_["max protocol"] == "SMB3"
    assert global_["deadtime"] == "720" and global_["smb3 directory leases"] == "no"
    assert global_["aio read size"] == global_["aio write size"] == "0"
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
        assert share["vfs objects"] == "catia fruit streams_xattr acl_xattr xattr_tdb"
        assert share["tc:native symlinks"] == "yes"
        assert share["veto files"] == "/.samba4/.tc-xsym.*/" and share["delete veto files"] == "yes"
        assert share["smbd max xattr size"] == "3802" and share["streams_xattr:max xattrs per stream"] == "35"
        assert global_["tc:volume " + share["tc:volume device"]] == share["tc:volume uuid"] + "|" + share["path"]


def test_netbsd4_cache_remains_disk_backed(renderer):
    conf = render(renderer, args=("netbsd4",))
    assert conf["global"]["cache directory"] == "/Volumes/dk2/.samba4/cache"


def test_tested_aio_and_debug_preferences(renderer):
    conf = render(renderer, {"VFS_AIO_FORK_ENABLED": 1, "SMBD_DEBUG_LOGGING": 1})
    # aio_fork's helper buffers cover Samba's default SMB2 sizes, so none are set.
    assert "smb2 max read" not in conf["global"] and "smb2 max write" not in conf["global"]
    assert conf["global"]["aio read size"] == conf["global"]["aio write size"] == "1"
    assert conf["global"]["max log size"] == "0" and conf["global"]["log level"] == "10"
    assert conf["Data"]["aio_fork:max_children"] == "2"
    assert conf["Data"]["vfs objects"].endswith(" aio_fork")


@pytest.mark.parametrize("options,expected,absent", [
    ({"ANY_PROTOCOL": 1}, {}, ["min protocol", "max protocol", "server min protocol"]),
    ({"REQUIRE_SMB_ENCRYPTION": 1}, {"server smb encrypt": "required", "server min protocol": "SMB3_00"}, ["min protocol"]),
    ({"FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION": 1}, {"server signing": "disabled", "server smb encrypt": "off", "min protocol": "SMB2"}, []),
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


def raw_share_sections(renderer, names):
    """Render one share per volume name; return smb.conf's raw section names
    (as the ADisk TXT carries them) and the names Samba's parser serves."""
    binary, config = renderer
    config.write_text("")
    parts = [{"deviceName": f"dk{i+2}", "format": "hfs", "name": name,
              "uuid": f"00000000-0000-0000-0000-{i+1:012x}"} for i, name in enumerate(names)]
    result = subprocess.run([str(binary)], input=plistlib.dumps([{"deviceName": "sd0", "partitions": parts}]),
                            capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    text = result.stdout.decode()
    raw = [line[1:-1] for line in text.splitlines() if line.startswith("[") and line != "[global]"]
    return raw, parse_active_share_names(text)


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


@pytest.mark.parametrize("text", ["RSYNC_ENABLED=maybe\n", "TELEMETRY=$(touch bad)\n",
                                 "REQUIRE_SMB_ENCRYPTION=1\nFORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=1\n",
                                 "RSYNC_ENABLED='unterminated\n"])
def test_invalid_configuration_is_rejected_before_render(renderer, text):
    binary, config = renderer
    config.write_text(text)
    result = subprocess.run([str(binary)], input=b"[]", capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b""
