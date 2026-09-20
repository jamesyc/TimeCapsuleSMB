"""Behavioral replacement for shell share projection and Samba configuration."""
import configparser
import plistlib
import shlex
import subprocess

import pytest

from tests.native.build import ROOT, instrumentation_flags
from tests.storage_fixtures import MAST_FIXTURES


@pytest.fixture(scope="module")
def renderer(tmp_path_factory):
    root = tmp_path_factory.mktemp("samba-config")
    binary, config = root / "render", root / "runtime.conf"
    sources = ["storage/mast.c", "storage/shares.c", "samba/config.c", "common/config.c"]
    subprocess.run(["cc", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror", *instrumentation_flags(),
                    f'-DTC_FLASH_CONFIG_PATH="{config}"', "-I", str(ROOT / "build/native"),
                    *(str(ROOT / "build/native" / source) for source in sources),
                    str(ROOT / "tests/native/unit/test_samba_config.c"), "-o", str(binary)],
                   check=True, capture_output=True)
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
    assert global_["interfaces"] == "127.0.0.1/8 ::1/128 192.0.2.3/24"
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
        assert share["smbd max xattr size"] == "3802" and share["streams_xattr:max xattrs per stream"] == "35"
        assert global_["tc:volume " + share["tc:volume device"]] == share["tc:volume uuid"]


def test_netbsd4_cache_remains_disk_backed(renderer):
    conf = render(renderer, args=("netbsd4",))
    assert conf["global"]["cache directory"] == "/Volumes/dk2/.samba4/cache"


def test_tested_aio_and_debug_preferences(renderer):
    conf = render(renderer, {"VFS_AIO_FORK_ENABLED": 1, "SMBD_DEBUG_LOGGING": 1})
    assert conf["global"]["smb2 max read"] == conf["global"]["smb2 max write"] == "131072"
    assert conf["global"]["aio read size"] == conf["global"]["aio write size"] == "1"
    assert conf["global"]["max log size"] == "0" and conf["global"]["log level"] == "10"
    assert conf["Data"]["aio_fork:max_children"] == "8"
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


def test_unavailable_volume_not_projected_and_usb_payload_remains_a_share(renderer):
    conf = render(renderer, args=("skip-first",))
    assert conf.sections() == ["global", "Data"]
    assert conf["Data"]["path"] == "/Volumes/dk3"
    assert conf["Data"]["veto files"] == "/.samba4/"
    assert "tc:volume dk2" not in conf["global"]
    assert conf["global"]["tc:volume dk3"] == conf["Data"]["tc:volume uuid"]


def test_names_sanitized_bounded_and_ascii_case_collisions_disambiguated(renderer):
    names = [' /Bad:*=Name[]?"<>|,\\ ', 'data', 'DATA', '中é' * 70]
    parts = [{"deviceName": f"dk{i+2}", "format": "hfs", "name": name,
              "uuid": f"00000000-0000-0000-0000-{i+1:012x}"} for i, name in enumerate(names)]
    conf = render(renderer, inventory=plistlib.dumps([{"deviceName": "sd0", "partitions": parts}]))
    assert conf.sections()[1:4] == ['_Bad___Name_________', 'data', 'DATA (dk4)']
    last = conf.sections()[-1]
    assert len(last.encode()) <= 194 and last.encode().decode() == last


@pytest.mark.parametrize("text", ["RSYNC_ENABLED=maybe\n", "TELEMETRY=$(touch bad)\n",
                                 "REQUIRE_SMB_ENCRYPTION=1\nFORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=1\n",
                                 "RSYNC_ENABLED='unterminated\n"])
def test_invalid_configuration_is_rejected_before_render(renderer, text):
    binary, config = renderer
    config.write_text(text)
    result = subprocess.run([str(binary)], input=b"[]", capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b""
