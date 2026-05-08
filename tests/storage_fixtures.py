from __future__ import annotations

from dataclasses import dataclass
import plistlib

from timecapsulesmb.device.storage import MaStVolume


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
