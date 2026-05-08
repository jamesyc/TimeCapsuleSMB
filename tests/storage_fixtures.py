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
                uuid=f42bdb83 c2655522 a0872560 6a4d0abf |binary| (16 bytes)
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
                uuid=51f93e6f dc69524d 986dcee4 d7cb3573 |binary| (16 bytes)
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
