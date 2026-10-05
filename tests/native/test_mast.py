"""Apple MaSt observations drive topology; errors must not mean no disks."""
import plistlib
import re
import subprocess

import pytest

from tests.native.build import ROOT, compile_modules
from tests.storage_fixtures import MAST_FIXTURES


@pytest.fixture(scope="module")
def parser(tmp_path_factory):
    binary = tmp_path_factory.mktemp("mast-native") / "mast"
    compile_modules(binary, ("native/storage/mast.c",),
                    flags=("-I", str(ROOT / "build/native")),
                    extra_sources=(ROOT / "tests/native/unit/test_mast.c",))
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


# What `acp -A MaSt` prints, from disassembling acp's PrintFUtils printer
# (NetBSD 6 7.9.1 and NetBSD 4 7.8.1 alike); plain `acp MaSt` prints XML.
# "{", "}", "[" and "]" stand alone on their lines, four spaces per level;
# entries are `key=value` in CFDictionary order; an empty array or dictionary
# is inline (`key=[]`, `key={}`). Data up to 16 bytes is hex, " |", the same
# bytes as text (0x20-0x7e as themselves, anything else as "^") and
# "| (N bytes)". A string value is its raw UTF-8 between quotes with nothing
# escaped. acp prints its "MaSt=" label after the value.
APPLE_ALTERNATE_FORM = (
    '\n[\n'
    '    {\n'
    '        partitions=\n'
    '        [\n'
    '            {\n'
    '                deviceName="dk3"\n'
    '                name="partitions=[ ] builtin=true "q" \\" \\\\ C:\\new\ttab  "\n'
    '                format="hfs"\n'
    '                users=0\n'
    '                uuid=7b7d5b5d 7c220a0d 3d28293b 2c2041ff |{}[]|"^^=();, A^| (16 bytes)\n'
    '                tags=[]\n'
    '                options={}\n'
    '            }\n'
    '            {\n'
    '                deviceName="dk4"\n'
    '                name="two\nlines"\n'
    '                format="hfs"\n'
    '                users=2\n'
    '                uuid=51f93e6f dc69524d 986dcee4 d7cb3573 |Q^>o^iRM^m^^^^5s| (16 bytes)\n'
    '            }\n'
    '        ]\n'
    '        builtin=false\n'
    '        deviceName="sd1"\n'
    '    }\n'
    ']\n'
    '\n'
    'MaSt=\n'
)


def volumes(result):
    assert result.returncode == 0, result.stderr
    return [(f[1].decode(), f[2].decode(), f[3:5], bytes.fromhex(f[5].decode()).decode())
            for f in (line.split() for line in result.stdout.splitlines()[1:])]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_alternate_form_strings_are_read_as_apple_writes_them(parser, newline):
    # The quotes, backslashes, tab, trailing spaces and line break in the
    # names are Apple's raw text, not escapes; the data's text column holds
    # braces, bars and a quote that are not structure. CRLF is the same text
    # read through ssh's terminal.
    result = run(parser, APPLE_ALTERNATE_FORM.replace("\n", newline))
    assert volumes(result) == [
        ("dk3", "7b7d5b5d-7c22-0a0d-3d28-293b2c2041ff", [b"0", b"0"],
         'partitions=[ ] builtin=true "q" \\" \\\\ C:\\new\ttab  '),
        ("dk4", "51f93e6f-dc69-524d-986d-cee4d7cb3573", [b"0", b"2"], "two" + newline + "lines"),
    ]


def test_alternate_form_cut_inside_a_name_is_not_an_inventory(parser):
    # A read that ends before a name's closing quote has no complete value.
    cut = APPLE_ALTERNATE_FORM.split('name="two')[0] + 'name="two\nli'
    assert run(parser, cut).returncode == 2


def test_openstep_form_still_unescapes_its_strings(parser):
    text = ('MaSt = ({ deviceName = "sd1"; builtin = false; partitions = ({ deviceName = "dk3";'
            ' name = "Say \\"hi\\" \\\\ x"; format = "hfs";'
            ' uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>; }); });')
    assert volumes(run(parser, text))[0][3] == 'Say "hi" \\ x'


def spaced_empty_elements_mast():
    """Apple's compact XML with every kind of empty element the reader handles."""
    return plistlib.dumps([
        {"deviceName": "sd1", "builtin": True, "info": "", "annotation": b"",
         "partitions": [volume(tags=[], options={})]},
        {"deviceName": "sd2", "builtin": False, "partitions": [volume(deviceName="dk5", name="Other")]},
    ]).decode()


def respace(text, sep):
    text = text.replace("<string></string>", f"<string{sep}/>")
    text = re.sub(r"<data>\s*</data>", f"<data{sep}/>", text)
    for tag in ("array", "dict", "true", "false"):
        text = text.replace(f"<{tag}/>", f"<{tag}{sep}/>")
    return text


@pytest.mark.parametrize("sep", [" ", "\t", "\n\t\t", "  "])
def test_whitespace_before_empty_element_close_is_the_same_inventory(parser, sep):
    compact = spaced_empty_elements_mast()
    spaced = respace(compact, sep)
    for tag in ("string", "data", "array", "dict", "true", "false"):
        assert f"<{tag}{sep}/>" in spaced
    expected = run(parser, compact)
    assert expected.returncode == 0, expected.stderr
    lines = expected.stdout.splitlines()
    assert lines[0] == b"2"
    assert [line.split()[3] for line in lines[1:]] == [b"1", b"0"]
    result = run(parser, spaced)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected.stdout


@pytest.mark.parametrize("broken", ["<true /", "<true/ >", "<truex/>", "<true >", "<tru/>", "<true / >"])
def test_malformed_empty_elements_reject_whole_observation(parser, broken):
    text = spaced_empty_elements_mast().replace("<true/>", broken, 1)
    assert broken in text
    assert run(parser, text).returncode == 2


@pytest.mark.parametrize("cut", ["<true", "<true ", "<true /", "<array ", "<dict \t"])
def test_empty_element_truncated_at_end_of_input_is_rejected(parser, cut):
    compact = spaced_empty_elements_mast()
    tag = cut.split()[0].rstrip("/")
    text = compact[:compact.index(tag + "/>")] + cut
    assert run(parser, text).returncode == 2
