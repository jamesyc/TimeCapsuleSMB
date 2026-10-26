from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys

from timecapsulesmb.device.storage import MaStVolume
from timecapsulesmb.integrations.acp import DEVICE_ACP_PATH


class FakeDiskd:
    """A device for the real diskd.useVolume mount guard: patch it in as
    device.storage.run_ssh and it runs each guard script against a fake acp
    and df, counting the diskd.useVolume calls.

    acp answers the calls with `answers` in turn, (exit status, acp's error
    code or None), then with `default`; with no default, a call past the
    answers fails the test. An accepted call mounts every volume when
    `mounts`.

    One device-wide mounted flag stands for every volume: df reports any root
    it is asked about as mounted once one is, so it fits single-volume tests
    only. Commands must be `/bin/sh -c SCRIPT`, as the guard is run."""

    def __init__(self, tmp: Path, *, mounted: bool = False, answers=(), default=(0, None), mounts: bool = True):
        self._state = tmp / "diskd.json"
        self._state.write_text(json.dumps({
            "mounted": mounted, "calls": 0, "answers": [list(answer) for answer in answers],
            "default": None if default is None else list(default), "mounts": mounts, "unexpected": 0,
        }))
        self._tool = tmp / "diskd.py"
        self._tool.write_text(f"""
import json, sys
from pathlib import Path
state = Path({str(self._state)!r})
d = json.loads(state.read_text())
if sys.argv[1] == "acp":
    d["calls"] += 1
    if not d["answers"] and d["default"] is None:
        d["unexpected"] += 1
        state.write_text(json.dumps(d))
        sys.exit(1)
    rc, error = d["answers"].pop(0) if d["answers"] else d["default"]
    print("<plist>status</plist>")
    if error is not None:
        print(f'### RPC function "diskd.useVolume" failed: {{error}}', file=sys.stderr)
    if rc == 0 and d["mounts"]:
        d["mounted"] = True
    state.write_text(json.dumps(d))
    sys.exit(rc)
print("Filesystem 1K-blocks Used Avail Capacity Mounted on")
if d["mounted"]:
    print(f"/dev/dk2 100 1 99 1% {{sys.argv[-1]}}")
""")

    def _read(self) -> dict:
        return json.loads(self._state.read_text())

    @property
    def calls(self) -> int:
        return self._read()["calls"]

    def unmount(self) -> None:
        d = self._read()
        d["mounted"] = False
        self._state.write_text(json.dumps(d))

    def run_ssh(self, _connection, command: str, **_kwargs) -> subprocess.CompletedProcess:
        fake = lambda name: f"{sys.executable} {self._tool} {name}"
        script = shlex.split(command)[2]
        script = re.sub(r"mkdir -p [^;]+;", ":;", script, count=1)
        script = script.replace(f"{DEVICE_ACP_PATH} rpc diskd.useVolume", fake("acp"))
        script = script.replace("/bin/df -k", fake("df"))
        script = script.replace("/usr/bin/tail -n +2", "sed 1d")
        script = script.replace("sleep 1", ":")
        result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)
        if self._read()["unexpected"]:
            raise AssertionError(f"diskd.useVolume called more often than answered ({self.calls} calls)")
        return result


@dataclass(frozen=True)
class MaStFixture:
    name: str
    raw: str | bytes
    expected: tuple[MaStVolume, ...]
    shell_supported: bool = True


INTERNAL_DATA = MaStVolume(
    "wd0",
    "dk2",
    "/Volumes/dk2",
    "Data",
    "f42bdb83-c265-5522-a087-25606a4d0abf",
    True,
    "hfs",
)
EXTERNAL_UNTITLED = MaStVolume(
    "sd0",
    "dk3",
    "/Volumes/dk3",
    "Untitled",
    "51f93e6f-dc69-524d-986d-cee4d7cb3573",
    False,
    "hfs",
)
EXTERNAL_DATA = MaStVolume(
    "sd0",
    "dk3",
    "/Volumes/dk3",
    "Data",
    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    False,
    "hfs",
)
EXTERNAL_BACKUP = MaStVolume(
    "sd0",
    "dk5",
    "/Volumes/dk5",
    "USB Backup",
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    False,
    "hfs",
)
INTERNAL_ARCHIVE = MaStVolume(
    "wd0",
    "dk4",
    "/Volumes/dk4",
    "Archive",
    "11111111-2222-3333-4444-555555555555",
    True,
    "hfs",
)
INTERNAL_ADVERSARIAL = MaStVolume(
    "wd0",
    "dk2",
    "/Volumes/dk2",
    "builtin=true Data=Backup",
    "01234567-89ab-cdef-0123-456789abcdef",
    True,
    "hfs",
)
EXTERNAL_ADVERSARIAL = MaStVolume(
    "sd0",
    "dk3",
    "/Volumes/dk3",
    "uuid = fake",
    "fedcba98-7654-3210-fedc-ba9876543210",
    False,
    "hfs",
)


MAST_FIXTURES: tuple[MaStFixture, ...] = (
    MaStFixture(
        name="xml_internal_external",
        raw=plistlib.dumps(
            [
                {
                    "deviceName": "wd0",
                    "builtin": True,
                    "partitions": [
                        {
                            "deviceName": "dk2",
                            "name": "Data",
                            "format": "hfs",
                            "uuid": bytes.fromhex("f42bdb83c2655522a08725606a4d0abf"),
                        },
                        {
                            "deviceName": "dk1",
                            "name": "APconfig",
                            "format": "msdos",
                            "uuid": bytes.fromhex("00000000000000000000000000000000"),
                        },
                    ],
                },
                {
                    "deviceName": "sd0",
                    "builtin": False,
                    "partitions": [
                        {
                            "deviceName": "dk3",
                            "name": "Untitled",
                            "format": "HFS",
                            "uuid": "51f93e6f-dc69-524d-986d-cee4d7cb3573",
                        }
                    ],
                },
            ]
        ),
        expected=(INTERNAL_DATA, EXTERNAL_UNTITLED),
        shell_supported=False,
    ),
    MaStFixture(
        name="xml_spaced_mast_assignment",
        raw="MaSt = "
        + plistlib.dumps(
            [
                {
                    "deviceName": "wd0",
                    "builtin": True,
                    "partitions": [
                        {
                            "deviceName": "dk2",
                            "name": "Data",
                            "format": "hfs",
                            "uuid": "f42bdb83-c265-5522-a087-25606a4d0abf",
                        }
                    ],
                }
            ]
        ).decode("utf-8"),
        expected=(INTERNAL_DATA,),
        shell_supported=False,
    ),
    MaStFixture(
        name="openstep_internal_external",
        raw="""\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "Data";
                format = "hfs";
                uuid = <f42bdb83 c2655522 a0872560 6a4d0abf>;
            },
            {
                deviceName = "dk1";
                name = "APconfig";
                format = "msdos";
                uuid = <00000000 00000000 00000000 00000000>;
            }
        );
    },
    {
        deviceName = "sd0";
        builtin = false;
        partitions = (
            {
                deviceName = "dk3";
                name = "Untitled";
                format = "hfs";
                uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>;
            }
        );
    }
);
""",
        expected=(INTERNAL_DATA, EXTERNAL_UNTITLED),
    ),
    MaStFixture(
        name="openstep_bare_partition_array_closer",
        raw="""\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "Data";
                format = "hfs";
                uuid = <f42bdb83 c2655522 a0872560 6a4d0abf>;
            }
        )
    },
    {
        deviceName = "sd0";
        builtin = false;
        partitions = (
            {
                deviceName = "dk3";
                name = "Untitled";
                format = "hfs";
                uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>;
            }
        )
    }
);
""",
        expected=(INTERNAL_DATA, EXTERNAL_UNTITLED),
    ),
    MaStFixture(
        name="openstep_unquoted_simple_strings",
        raw="""\
MaSt = (
    {
        deviceName = wd0;
        builtin = true;
        partitions = (
            {
                deviceName = dk2;
                name = Data;
                format = hfs;
                uuid = <f42bdb83 c2655522 a0872560 6a4d0abf>;
            }
        );
    },
    {
        deviceName = sd0;
        builtin = false;
        partitions = (
            {
                deviceName = dk3;
                name = Untitled;
                format = hfs;
                uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>;
            }
        );
    }
);
""",
        expected=(INTERNAL_DATA, EXTERNAL_UNTITLED),
    ),
    # `acp -A MaSt` as Apple's printer writes it: data as hex, " |", the same
    # bytes as text (0x20-0x7e as themselves, anything else as "^"), then
    # "| (N bytes)"; acp prints its "MaSt=" label after the value.
    MaStFixture(
        name="native_acp_array_internal_external",
        raw="""\
[
    {
        deviceName="wd0"
        partitions=
        [
            {
                deviceName="dk2"
                name="Data"
                format="hfs"
                uuid=f42bdb83 c2655522 a0872560 6a4d0abf |^+^^^eU"^^%`jM^^| (16 bytes)
            }
        ]
        builtin=true
    }
    {
        deviceName="sd0"
        partitions=
        [
            {
                deviceName="dk3"
                name="Untitled"
                format="hfs"
                uuid=51f93e6f dc69524d 986dcee4 d7cb3573 |Q^>o^iRM^m^^^^5s| (16 bytes)
            }
        ]
    }
]

MaSt=
""",
        expected=(INTERNAL_DATA, EXTERNAL_UNTITLED),
    ),
    MaStFixture(
        name="openstep_reordered_disk_keys",
        raw="""\
MaSt = (
    {
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "Data";
                format = "hfs";
                uuid = <f42bdb83 c2655522 a0872560 6a4d0abf>;
            }
        );
        deviceName = "wd0";
    },
    {
        partitions = (
            {
                deviceName = "dk3";
                name = "Untitled";
                format = "hfs";
                uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>;
            }
        );
        builtin = false;
        deviceName = "sd0";
    }
);
""",
        expected=(INTERNAL_DATA, EXTERNAL_UNTITLED),
    ),
    MaStFixture(
        name="openstep_duplicate_internal_external_names",
        raw="""\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "Data";
                format = "hfs";
                uuid = <f42bdb83 c2655522 a0872560 6a4d0abf>;
            }
        );
    },
    {
        deviceName = "sd0";
        builtin = false;
        partitions = (
            {
                deviceName = "dk3";
                name = "Data";
                format = "hfs";
                uuid = <bbbbbbbb bbbbbbbb bbbbbbbb bbbbbbbb>;
            }
        );
    }
);
""",
        expected=(INTERNAL_DATA, EXTERNAL_DATA),
    ),
    MaStFixture(
        name="openstep_external_only",
        raw="""\
MaSt = (
    {
        deviceName = "sd0";
        builtin = false;
        partitions = (
            {
                deviceName = "dk5";
                name = "USB Backup";
                format = "hfs";
                uuid = <aaaaaaaabbbbccccddddeeeeeeeeeeee>;
            }
        );
    }
);
""",
        expected=(EXTERNAL_BACKUP,),
    ),
    MaStFixture(
        name="openstep_internal_only",
        raw="""\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "Data";
                format = "hfs";
                uuid = <f42bdb83 c2655522 a0872560 6a4d0abf>;
            }
        );
    }
);
""",
        expected=(INTERNAL_DATA,),
    ),
    MaStFixture(
        name="openstep_adversarial_names_and_keys",
        raw="""\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        notbuiltin = false;
        partitions = (
            {
                deviceName = "dk2";
                name = "builtin=true Data=Backup";
                some_name = "Not the real name";
                format = "hfs";
                format_hint = "msdos";
                uuid = <01234567 89abcdef 01234567 89abcdef>;
                uuid_hint = <00000000 00000000 00000000 00000000>;
            }
        );
    },
    {
        deviceName = "sd0";
        builtin = false;
        some_builtin = true;
        partitions = (
            {
                deviceName = "dk3";
                name = "uuid = fake";
                format = "hfs";
                uuid = <fedcba98 76543210 fedcba98 76543210>;
            }
        );
    }
);
""",
        expected=(INTERNAL_ADVERSARIAL, EXTERNAL_ADVERSARIAL),
    ),
    MaStFixture(
        name="openstep_skips_unusable_partitions",
        raw="""\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "Missing UUID";
                format = "hfs";
            },
            {
                deviceName = "dk1";
                name = "APconfig";
                format = "msdos";
                uuid = <00000000 00000000 00000000 00000000>;
            },
            {
                deviceName = "rd0";
                name = "Not a dk partition";
                format = "hfs";
                uuid = <99999999 99999999 99999999 99999999>;
            },
            {
                deviceName = "dk4";
                name = "Archive";
                format = "hfs";
                uuid = <11111111222233334444555555555555>;
            }
        );
    }
);
""",
        expected=(INTERNAL_ARCHIVE,),
    ),
    MaStFixture(
        name="openstep_no_valid_hfs_partitions",
        raw="""\
MaSt = (
    {
        deviceName = "sd0";
        builtin = false;
        partitions = (
            {
                deviceName = "dk1";
                name = "APconfig";
                format = "msdos";
                uuid = <00000000 00000000 00000000 00000000>;
            }
        );
    }
);
""",
        expected=(),
    ),
)


SHELL_MAST_FIXTURES = tuple(fixture for fixture in MAST_FIXTURES if fixture.shell_supported)
