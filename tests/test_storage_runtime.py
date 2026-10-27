from __future__ import annotations

import plistlib
import shlex
import subprocess
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from xml.parsers.expat import ExpatError
from unittest import mock

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.core.release import CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.services.deploy import render_flash_runtime_config, render_payload_checksums, render_rsync_daemon_config
from timecapsulesmb.services.deploy import render_flash_runtime_config as render_gui_flash_runtime_config
from timecapsulesmb.deploy.planner import (
    GENERATED_FLASH_CONFIG_SOURCE,
    build_deployment_plan,
)
from timecapsulesmb.device.storage import (
    VolumeClaims,
    MaStDiskSnapshot,
    MaStPartitionSnapshot,
    MAST_PARTITIONLESS_ATTEMPTS,
    MAST_PROBE_COMMAND,
    PayloadVerificationResult,
    StorageDeviceError,
    MaStProbeDiagnostics,
    MaStReadResult,
    MaStVolume,
    PayloadHome,
    diskd_rpc_status_conn,
    ensure_volume_root_mounted_conn,
    mast_probe_debug_summary,
    mast_volumes_debug_summary,
    ordered_payload_candidate_volumes,
    payload_candidate_checks_debug_summary,
    parse_mast_inventory,
    parse_mast_xml_inventory,
    parse_mast_plist,
    probe_mast_diagnostics_conn,
    render_ensure_volume_root_mounted_script,
    select_payload_home_with_diagnostics_conn,
    verify_payload_home_conn,
    VolumeMountResult,
    volume_root_is_writable_conn,
    wait_for_mast_volumes_conn,
)
from timecapsulesmb.transport.ssh import SshCommandTimeout, SshConnection
from tests.storage_fixtures import MAST_FIXTURES, SHELL_MAST_FIXTURES, FakeDiskd


def netbsd6_internal_and_usb_xml(smart_status: str = "verified") -> str:
    """`acp MaSt` on the LAN NetBSD 6 unit (serials and UUIDs replaced), the
    same disks as StorageRuntimeTests.NETBSD6_INTERNAL_AND_USB."""
    return "MaSt=" + plistlib.dumps([
        {
            "blockSize": 512, "builtin": True, "deviceName": "wd0", "info": "Disk 1",
            "partitions": [{
                "deviceName": "dk2", "format": "hfs", "name": "AirPort Disk", "size": 1905681,
                "sizeFree": 513501, "sizeUsed": 1392180, "users": 1, "uuid": bytes([10] + [0] * 14 + [2]),
            }],
            "product": "SERIAL6", "revision": "PA45", "size": 1907729, "smartStatus": smart_status,
            "softDisconnected": False, "uuid": bytes([10] + [0] * 14 + [1]), "vendor": "PALP EDH DTS0200MD00 1",
        },
        {
            "blockSize": 512, "deviceName": "sd0", "info": "Disk 2",
            "partitions": [{
                "deviceName": "dk4", "format": "hfs", "name": "4TB", "size": 3815086,
                "sizeFree": 3741102, "sizeUsed": 73984, "users": 1, "uuid": bytes([11] + [0] * 14 + [2]),
            }],
            "product": "Elements 25A1", "revision": "1014", "size": 3815415,
            "softDisconnected": False, "uuid": bytes([11] + [0] * 14 + [1]), "vendor": "WD",
        },
    ]).decode()


class StorageRuntimeTests(unittest.TestCase):


    def test_parse_mast_plist_matches_golden_fixtures(self) -> None:
        for fixture in MAST_FIXTURES:
            with self.subTest(fixture=fixture.name):
                self.assertEqual(parse_mast_plist(fixture.raw), fixture.expected)

    def test_parse_mast_openstep_fallback_handles_current_acp_line_format(self) -> None:
        raw = """\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "Data; Main";
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
                name = "uuid = fake";
                format = "hfs";
                uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>;
            }
        );
    }
);
"""

        self.assertEqual(
            parse_mast_plist(raw),
            (
                MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data; Main", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs"),
                MaStVolume("sd0", "dk3", "/Volumes/dk3", "uuid = fake", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs"),
            ),
        )

    # One disk object and one partition object per brace pair, whichever
    # keys they have. The XML plist and acp's text forms give the same answer.
    TEXT_PARTITION_WITHOUT_DEVICE = """\
MaSt = (
    {
        deviceName = "wd0";
        builtin = true;
        partitions = (
            {
                name = "Free";
            },
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
        partitions = ();
    }
);
"""
    XML_PARTITION_WITHOUT_DEVICE = plistlib.dumps([
        {"deviceName": "wd0", "builtin": True, "partitions": [
            {"name": "Free"},
            {"deviceName": "dk2", "name": "Data", "format": "hfs",
             "uuid": "f42bdb83-c265-5522-a087-25606a4d0abf"},
        ]},
        {"deviceName": "sd0", "builtin": False, "partitions": []},
    ])

    def test_a_partition_without_a_device_keeps_later_partitions_on_its_disk(self) -> None:
        data = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        inventory = (
            MaStDiskSnapshot("wd0", "", None, True, (
                MaStPartitionSnapshot("", "Free", ""),
                MaStPartitionSnapshot("dk2", "Data", "hfs", uuid="f42bdb83-c265-5522-a087-25606a4d0abf"),
            )),
            MaStDiskSnapshot("sd0", "", None, False, ()),
        )
        for form, raw in (("text", self.TEXT_PARTITION_WITHOUT_DEVICE), ("xml", self.XML_PARTITION_WITHOUT_DEVICE)):
            with self.subTest(form=form):
                self.assertEqual(parse_mast_plist(raw), (data,))
                self.assertEqual(parse_mast_inventory(raw), inventory)

    def test_an_unnamed_hfs_partition_is_named_after_its_device_in_both_forms(self) -> None:
        text = """\
MaSt = (
    {
        deviceName = "sd0";
        builtin = false;
        partitions = (
            {
                deviceName = "dk3";
                format = "hfs";
                uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>;
            }
        );
    }
);
"""
        xml = plistlib.dumps([{"deviceName": "sd0", "builtin": False, "partitions": [
            {"deviceName": "dk3", "format": "hfs", "uuid": "51f93e6f-dc69-524d-986d-cee4d7cb3573"}]}])
        expected = (MaStVolume("sd0", "dk3", "/Volumes/dk3", "dk3", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs"),)
        self.assertEqual(parse_mast_plist(text), expected)
        self.assertEqual(parse_mast_plist(xml), expected)

    def test_inventory_keeps_every_disk_and_partition_with_typed_values(self) -> None:
        # Diagnostics report what MaSt lists, usable or not: an empty object,
        # a non-HFS partition and one with no uuid all stay. builtin is a
        # real boolean and size keeps acp's text.
        text = """\
MaSt = (
    {
    },
    {
        deviceName = "sd0";
        model = "USB Disk";
        capacity = 1000;
        size = 2000;
        builtin = false;
        partitions = (
            {
                deviceName = "dk1";
                name = "APconfig";
                format = "MSDOS";
            },
            {
                deviceName = "dk2";
                format = "hfs";
            }
        );
    }
);
"""
        self.assertEqual(parse_mast_inventory(text), (
            MaStDiskSnapshot("", "", None, False, ()),
            MaStDiskSnapshot("sd0", "USB Disk", "2000", False, (
                MaStPartitionSnapshot("dk1", "APconfig", "msdos"),
                MaStPartitionSnapshot("dk2", "", "hfs"),
            )),
        ))
        self.assertEqual(parse_mast_plist(text), ())

    # What `acp -A MaSt` prints, from disassembling acp's PrintFUtils printer
    # (NetBSD 6 7.9.1 and NetBSD 4 7.8.1 alike). Plain `acp MaSt` prints XML.
    # - A blank line, the array, a blank line, then acp's trailing "MaSt=".
    # - Four spaces per level; "{", "}", "[" and "]" each alone on a line;
    #   an empty array or dictionary inline as `key=[]` or `key={}`.
    # - Entries as `key=value` in CFDictionary order, so a disk's deviceName
    #   and builtin may follow its partitions.
    # - Numbers as `%lld`, booleans as true/false.
    # - Data up to 16 bytes inline: hex in groups of four bytes, " |", the
    #   same bytes as text (0x20-0x7e as themselves, so braces, brackets,
    #   bars and quotes too; anything else as "^"), "| (N bytes)".
    # - A string value as its raw UTF-8 between quotes, nothing escaped:
    #   quotes, backslashes and line breaks in a name appear as they are.
    APPLE_ALTERNATE_FORM = (
        '\n[\n'
        '    {\n'
        '        partitions=\n'
        '        [\n'
        '            {\n'
        '                format="hfs"\n'
        '                deviceName="dk2"\n'
        '                size=1905681\n'
        '                name="Say "hi" \\"q\\" \\\\ C:\\new\ttab  "\n'
        '                uuid=105b5aa0 ebe65928 84d09b3f 959fe903 |^[Z^^^Y(^^^?^^^^| (16 bytes)\n'
        '            }\n'
        '            {\n'
        '                deviceName="dk3"\n'
        '                format="hfs"\n'
        '                name="two\nlines"\n'
        '                uuid=7b7d5b5d 7c220a0d 3d28293b 2c2041ff |{}[]|"^^=();, A^| (16 bytes)\n'
        '                tags=[]\n'
        '                options={}\n'
        '            }\n'
        '        ]\n'
        '        size=1907729\n'
        '        deviceName="wd0"\n'
        '        builtin=true\n'
        '    }\n'
        ']\n'
        '\n'
        'MaSt=\n'
    )

    def test_acp_alternate_form_strings_are_read_as_apple_writes_them(self) -> None:
        # Kept as printed: \" and \\ are not escapes, the tab and the
        # trailing spaces are part of the name.
        first = 'Say "hi" \\"q\\" \\\\ C:\\new\ttab  '
        self.assertEqual(parse_mast_plist(self.APPLE_ALTERNATE_FORM), (
            MaStVolume("wd0", "dk2", "/Volumes/dk2", first, "105b5aa0-ebe6-5928-84d0-9b3f959fe903", True, "hfs"),
            MaStVolume("wd0", "dk3", "/Volumes/dk3", "two\nlines", "7b7d5b5d-7c22-0a0d-3d28-293b2c2041ff", True, "hfs"),
        ))
        self.assertEqual(parse_mast_inventory(self.APPLE_ALTERNATE_FORM), (
            MaStDiskSnapshot("wd0", "", "1907729", True, (
                MaStPartitionSnapshot("dk2", first, "hfs", size_mib=1905681, uuid="105b5aa0-ebe6-5928-84d0-9b3f959fe903"),
                MaStPartitionSnapshot("dk3", "two\nlines", "hfs", uuid="7b7d5b5d-7c22-0a0d-3d28-293b2c2041ff"),
            )),
        ))
        # The same text through ssh's terminal, with CRLF line ends.
        crlf = self.APPLE_ALTERNATE_FORM.replace("\n", "\r\n")
        self.assertEqual(parse_mast_plist(crlf), parse_mast_plist(self.APPLE_ALTERNATE_FORM))

    def test_openstep_form_still_unescapes_its_strings(self) -> None:
        text = (
            'MaSt = (\n    {\n        deviceName = "sd0";\n        builtin = false;\n'
            '        partitions = (\n            {\n                deviceName = "dk3";\n'
            '                name = "Say \\"hi\\" \\\\ x";\n                format = "hfs";\n'
            '                uuid = <51f93e6f dc69524d 986dcee4 d7cb3573>;\n            }\n        );\n    }\n);\n'
        )
        self.assertEqual(parse_mast_plist(text)[0].name, 'Say "hi" \\ x')

    def test_an_unterminated_alternate_form_string_is_not_a_value(self) -> None:
        # A cut-off read ends inside a name. The rest of the text is taken
        # as that name until a line ends with a quote, so the partition gets
        # no name and no uuid and is left out; the complete volume stays.
        cut = self.APPLE_ALTERNATE_FORM.split('name="two')[0] + 'name="two\nli'
        self.assertEqual([v.partition_device for v in parse_mast_plist(cut)], ["dk2"])

    def test_inventory_reads_acps_native_text_form(self) -> None:
        fixture = next(f for f in MAST_FIXTURES if f.name == "native_acp_array_internal_external")
        self.assertEqual(parse_mast_inventory(fixture.raw), (
            MaStDiskSnapshot("wd0", "", None, True, (
                MaStPartitionSnapshot("dk2", "Data", "hfs", uuid="f42bdb83-c265-5522-a087-25606a4d0abf"),
            )),
            MaStDiskSnapshot("sd0", "", None, False, (
                MaStPartitionSnapshot("dk3", "Untitled", "hfs", uuid="51f93e6f-dc69-524d-986d-cee4d7cb3573"),
            )),
        ))

    # `acp -A MaSt` on the LAN NetBSD 6 unit (serials and UUIDs replaced): the
    # internal disk is wd0, a USB disk sd0 with no builtin key and no smartStatus.
    NETBSD6_INTERNAL_AND_USB = (
        '\n[\n'
        '    {\n'
        '        product="SERIAL6"\n'
        '        blockSize=512\n'
        '        deviceName="wd0"\n'
        '        size=1907729\n'
        '        vendor="PALP EDH DTS0200MD00 1"\n'
        '        partitions=\n'
        '        [\n'
        '            {\n'
        '                format="hfs"\n'
        '                sizeUsed=1392180\n'
        '                deviceName="dk2"\n'
        '                size=1905681\n'
        '                users=1\n'
        '                name="AirPort Disk"\n'
        '                sizeFree=513501\n'
        '                uuid=0a000000 00000000 00000000 00000002 |^^^^^^^^^^^^^^^^| (16 bytes)\n'
        '            }\n'
        '        ]\n'
        '        builtin=true\n'
        '        uuid=0a000000 00000000 00000000 00000001 |^^^^^^^^^^^^^^^^| (16 bytes)\n'
        '        smartStatus="verified"\n'
        '        info="Disk 1"\n'
        '        revision="PA45"\n'
        '        softDisconnected=false\n'
        '    }\n'
        '    {\n'
        '        product="Elements 25A1"\n'
        '        blockSize=512\n'
        '        deviceName="sd0"\n'
        '        size=3815415\n'
        '        vendor="WD"\n'
        '        partitions=\n'
        '        [\n'
        '            {\n'
        '                format="hfs"\n'
        '                sizeUsed=73984\n'
        '                deviceName="dk4"\n'
        '                size=3815086\n'
        '                users=1\n'
        '                name="4TB"\n'
        '                sizeFree=3741102\n'
        '                uuid=0b000000 00000000 00000000 00000002 |^^^^^^^^^^^^^^^^| (16 bytes)\n'
        '            }\n'
        '        ]\n'
        '        uuid=0b000000 00000000 00000000 00000001 |^^^^^^^^^^^^^^^^| (16 bytes)\n'
        '        info="Disk 2"\n'
        '        revision="1014"\n'
        '        softDisconnected=false\n'
        '    }\n'
        ']\n'
        '\n'
        'MaSt='
    )

    def test_inventory_reads_smart_status_label_and_sizes_from_the_text_form(self) -> None:
        self.assertEqual(parse_mast_inventory(self.NETBSD6_INTERNAL_AND_USB), (
            MaStDiskSnapshot(
                "wd0", "", "1907729", True,
                (MaStPartitionSnapshot(
                    "dk2", "AirPort Disk", "hfs", 1905681, 1392180, 513501, users=1,
                    uuid="0a000000-0000-0000-0000-000000000002",
                ),),
                label="Disk 1", smart_status="verified",
                # wd0's model as Apple's NetBSD 6 kernel reports it: byte-swapped.
                model="PALP EDH DTS0200MD00 1", revision="PA45", soft_disconnected=False,
                product="SERIAL6", uuid="0a000000-0000-0000-0000-000000000001",
            ),
            MaStDiskSnapshot(
                "sd0", "", "3815415", False,
                (MaStPartitionSnapshot(
                    "dk4", "4TB", "hfs", 3815086, 73984, 3741102, users=1, uuid="0b000000-0000-0000-0000-000000000002",
                ),),
                label="Disk 2", smart_status=None, model="WD", revision="1014", soft_disconnected=False,
                product="Elements 25A1", uuid="0b000000-0000-0000-0000-000000000001",
            ),
        ))

    def test_inventory_reads_each_disks_identity_in_either_form(self) -> None:
        # Wipe Internal Disk addresses the disk by its uuid and reports its
        # serial; the uuid survives a wipe because it hashes only the drive's
        # identity.
        for raw in (self.NETBSD6_INTERNAL_AND_USB, netbsd6_internal_and_usb_xml()):
            with self.subTest(form="xml" if "<?xml" in raw else "text"):
                internal, usb = parse_mast_inventory(raw)
                self.assertEqual((internal.product, internal.size_mib), ("SERIAL6", 1907729))
                self.assertEqual(internal.uuid, "0a000000-0000-0000-0000-000000000001")
                self.assertEqual(internal.partitions[0].uuid, "0a000000-0000-0000-0000-000000000002")
                self.assertEqual((usb.product, usb.uuid), ("Elements 25A1", "0b000000-0000-0000-0000-000000000001"))

    def test_inventory_reads_softdisconnected_and_users_in_either_form(self) -> None:
        # diskd's hang signature: the disk soft-disconnected, its volume held by no one.
        text = (
            '[\n    {\n        softDisconnected=true\n        partitions=\n        [\n            {\n'
            '                users=0\n            }\n        ]\n    }\n    {\n        softDisconnected=maybe\n    }\n]\n'
        )
        xml = plistlib.dumps([
            {"softDisconnected": True, "partitions": [{"users": 0}]}, {"softDisconnected": "maybe"},
        ])
        for raw in (text, xml):
            with self.subTest(form="xml" if isinstance(raw, bytes) else "text"):
                hung, odd = parse_mast_inventory(raw)
                self.assertEqual((hung.soft_disconnected, hung.partitions[0].users), (True, 0))
                self.assertIsNone(odd.soft_disconnected)

    def test_both_forms_of_the_same_mast_give_the_same_disks(self) -> None:
        text, xml = parse_mast_inventory(self.NETBSD6_INTERNAL_AND_USB), parse_mast_inventory(netbsd6_internal_and_usb_xml())
        # Only the disk's own size keeps its form: digits in text, an integer in XML.
        self.assertEqual([replace(disk, size=None) for disk in text], [replace(disk, size=None) for disk in xml])
        self.assertEqual([disk.size for disk in xml], [1907729, 3815415])

    def test_strict_xml_inventory_reads_what_the_lenient_one_does(self) -> None:
        xml = netbsd6_internal_and_usb_xml()
        self.assertEqual(parse_mast_xml_inventory(xml), parse_mast_inventory(xml))
        # Behind lines the SSH client printed, too.
        self.assertEqual(parse_mast_xml_inventory(f"Warning: added host key\n{xml}"), parse_mast_inventory(xml))
        # A device without disks: an empty array is an answer.
        self.assertEqual(parse_mast_xml_inventory("MaSt=" + plistlib.dumps([]).decode()), ())
        self.assertEqual(parse_mast_xml_inventory("MaSt=" + plistlib.dumps([{"deviceName": "sd0"}]).decode())[0].partitions, ())

    def test_strict_xml_inventory_refuses_what_is_not_an_array_of_disks(self) -> None:
        def xml(root: object) -> str:
            return "MaSt=" + plistlib.dumps(root).decode()

        cases = {
            # The lenient parse reads each of these as no disks.
            "bare property name": ("MaSt=", ValueError, "acp printed no MaSt plist"),
            "not a plist": ("MaSt=not a plist", ValueError, "acp printed no MaSt plist"),
            "text form": (self.NETBSD6_INTERNAL_AND_USB, ValueError, "acp printed no MaSt plist"),
            "a dictionary": (xml({"MaSt": []}), ValueError, "MaSt is not an array of disks"),
            "a disk that is not a dictionary": (xml([{"deviceName": "wd0"}, "sd0"]), ValueError,
                                                "MaSt is not an array of disks"),
            "partitions not an array": (xml([{"deviceName": "wd0", "partitions": "dk2"}]), ValueError,
                                        "MaSt lists wd0's partitions as something else"),
            "a partition that is not a dictionary": (xml([{"partitions": [{"deviceName": "dk2"}, 3]}]), ValueError,
                                                     "MaSt lists a disk's partitions as something else"),
            "a plist cut off": ('MaSt=<?xml version="1.0"?><plist><array><dict>', ExpatError, None),
        }
        for case, (output, error, message) in cases.items():
            with self.subTest(case):
                with self.assertRaises(error) as raised:
                    parse_mast_xml_inventory(output)
                if message is not None:
                    self.assertEqual(str(raised.exception), message)
        # Why doctor needs the strict one: the lenient parse reads these as no disks.
        for output in ("MaSt=", "MaSt=not a plist"):
            self.assertEqual(parse_mast_inventory(output), ())

    def test_inventory_reads_the_same_fields_from_xml(self) -> None:
        # `acp MaSt` on the LAN NetBSD 4 unit, whose internal disk is sd0.
        raw = plistlib.dumps([{
            "blockSize": 512, "builtin": True, "deviceName": "sd0", "info": "Disk 1",
            "partitions": [{
                "deviceName": "dk2", "format": "hfs", "name": "Data", "size": 1905681,
                "sizeFree": 1896704, "sizeUsed": 8977, "users": 1, "uuid": bytes(16),
            }],
            "product": "SERIAL4", "revision": "12.01D04", "size": 1907729, "smartStatus": "verified",
            "softDisconnected": False, "uuid": bytes(16), "vendor": "WDC WD20EARS-42S0XB0",
        }]).decode()
        self.assertEqual(parse_mast_inventory(f"MaSt={raw}"), (
            MaStDiskSnapshot(
                "sd0", "", 1907729, True,
                (MaStPartitionSnapshot(
                    "dk2", "Data", "hfs", 1905681, 8977, 1896704, users=1, uuid="00000000-0000-0000-0000-000000000000",
                ),),
                label="Disk 1", smart_status="verified",
                model="WDC WD20EARS-42S0XB0", revision="12.01D04", soft_disconnected=False,
                product="SERIAL4", uuid="00000000-0000-0000-0000-000000000000",
            ),
        ))

    def test_inventory_keeps_only_sizes_that_are_whole_non_negative_numbers(self) -> None:
        raw = plistlib.dumps([{"deviceName": "sd0", "partitions": [
            {"deviceName": "dk1", "format": "msdos", "size": 15000, "sizeUsed": 2.5, "sizeFree": -1},
            {"deviceName": "dk2", "format": "msdos", "size": True, "sizeUsed": "12"},
        ]}])
        partitions = parse_mast_inventory(raw)[0].partitions
        # Digits read as the text form gives them; a bool, a fraction or a negative never.
        self.assertEqual([(p.size_mib, p.used_mib, p.free_mib) for p in partitions], [(15000, None, None), (None, 12, None)])
        text = (
            '[\n    {\n        partitions=\n        [\n            {\n                size=12x\n'
            '                sizeUsed=7\n                sizeFree=²\n            }\n        ]\n    }\n]\n'
        )
        partition = parse_mast_inventory(text)[0].partitions[0]
        # "²" passes isdigit() but not int(): it is no size, not an error.
        self.assertEqual((partition.size_mib, partition.used_mib, partition.free_mib), (None, 7, None))

    def test_wait_for_mast_volumes_retries_until_available(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        volume = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            side_effect=[
                MaStReadResult((), "MaSt=first"),
                MaStReadResult((), "MaSt=second"),
                MaStReadResult((volume,), "MaSt=third"),
            ],
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        self.assertEqual(result.volumes, (volume,))
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.raw_output, "MaSt=third")
        self.assertEqual(read_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3), mock.call(3)])

    def test_wait_for_mast_volumes_returns_empty_after_exhaustion(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), "MaSt=[]"),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=3, delay_seconds=3)

        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.raw_output, "MaSt=[]")
        self.assertEqual(read_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3), mock.call(3)])

    def test_wait_for_mast_volumes_stops_when_disk_has_no_hfs_partitions(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        raw_output = """
MaSt = (
    {
        deviceName = "wd0";
        name = "Seagate Expansion HDD";
        builtin = true;
        partitions = (
            {
                deviceName = "dk2";
                name = "PS3FAT";
                format = "msdos";
            }
        );
    }
);
"""

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), raw_output),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.raw_output, raw_output)
        # The first read still checks a failed get against ACPd.
        read_mock.assert_called_once_with(connection, check_acpd=True)
        sleep_mock.assert_not_called()

    def test_wait_for_mast_volumes_keeps_reading_while_every_disk_shows_no_partitions(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        raw_output = """
MaSt = (
    {
        deviceName = "wd0";
        model = "Seagate Expansion HDD";
        size = 8000000000000;
        builtin = true;
        partitions = (
        );
    }
);
"""

        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), raw_output),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep_mock:
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)

        # Healthy disks list no partitions for a while after they appear, so
        # the wait reads for 60 s (20 reads 3 s apart), then returns the last
        # read for the caller to explain.
        inventory = parse_mast_inventory(raw_output)
        self.assertEqual(result.volumes, ())
        self.assertEqual(result.attempts, MAST_PARTITIONLESS_ATTEMPTS)
        self.assertEqual(MAST_PARTITIONLESS_ATTEMPTS * 3, 60)
        self.assertEqual(result.raw_output, raw_output)
        self.assertEqual(inventory[0].name, "Seagate Expansion HDD")
        self.assertEqual(inventory[0].size, "8000000000000")
        self.assertEqual(inventory[0].partitions, ())
        self.assertEqual(read_mock.call_count, MAST_PARTITIONLESS_ATTEMPTS)
        # Only the first read checks a failed get against ACPd.
        self.assertEqual(read_mock.call_args_list[0], mock.call(connection, check_acpd=True))
        self.assertEqual(sleep_mock.call_args_list, [mock.call(3)] * (MAST_PARTITIONLESS_ATTEMPTS - 1))

    def test_wait_for_mast_volumes_stops_once_partitions_fill_in(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        empty = "MaSt = (\n{\ndeviceName = \"wd0\";\nbuiltin = true;\npartitions = (\n);\n}\n);\n"
        filled = (
            "MaSt = (\n{\ndeviceName = \"wd0\";\nbuiltin = true;\npartitions = (\n{\n"
            "deviceName = \"dk2\";\nformat = \"msdos\";\n}\n);\n}\n);\n"
        )
        reads = [MaStReadResult((), empty), MaStReadResult((), empty), MaStReadResult((), filled)]
        with mock.patch("timecapsulesmb.device.storage.read_mast_volumes_with_output_conn", side_effect=reads):
            with mock.patch("timecapsulesmb.device.storage.time.sleep"):
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)
        # A disk with any partition, even one deploy cannot use, ends the wait.
        self.assertEqual((result.attempts, result.volumes, result.raw_output), (3, (), filled))

    def test_wait_for_mast_volumes_waits_attempts_only_when_no_disk_appears(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch(
            "timecapsulesmb.device.storage.read_mast_volumes_with_output_conn",
            return_value=MaStReadResult((), "MaSt = (\n);\n"),
        ) as read_mock:
            with mock.patch("timecapsulesmb.device.storage.time.sleep"):
                result = wait_for_mast_volumes_conn(connection, attempts=10, delay_seconds=3)
        self.assertEqual((result.attempts, read_mock.call_count), (10, 10))

    def test_probe_mast_diagnostics_records_empty_success(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        raw_output = "MaSt = (\n);\n"
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=raw_output, stderr="")

        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=proc) as run_mock:
            diagnostics = probe_mast_diagnostics_conn(connection)

        self.assertEqual(diagnostics.command, MAST_PROBE_COMMAND)
        self.assertEqual(diagnostics.returncode, 0)
        self.assertEqual(diagnostics.volumes, ())
        self.assertEqual(diagnostics.stdout, raw_output)
        self.assertEqual(diagnostics.stderr, "")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.args[:2], (connection, MAST_PROBE_COMMAND))
        self.assertFalse(run_mock.call_args.kwargs["check"])
        summary = mast_probe_debug_summary(diagnostics)
        self.assertEqual(summary["mast_probe_volume_count"], 0)
        self.assertEqual(summary["mast_probe_stdout_chars"], len(raw_output))
        self.assertEqual(summary["mast_probe_stdout"], raw_output)
        self.assertEqual(summary["mast_probe_stderr"], "<empty>")

    def test_probe_mast_diagnostics_records_parsed_volume(self) -> None:
        fixture = SHELL_MAST_FIXTURES[0]
        raw_output = fixture.raw.decode("utf-8", errors="replace") if isinstance(fixture.raw, bytes) else fixture.raw
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=raw_output, stderr="")

        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=proc):
            diagnostics = probe_mast_diagnostics_conn(connection)

        self.assertEqual(diagnostics.returncode, 0)
        self.assertEqual(diagnostics.volumes, fixture.expected)
        summary = mast_probe_debug_summary(diagnostics)
        self.assertEqual(summary["mast_probe_volume_count"], len(fixture.expected))
        self.assertEqual(summary["mast_probe_candidates"], mast_volumes_debug_summary(fixture.expected))

    def test_probe_mast_diagnostics_captures_failure_stderr(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        proc = subprocess.CompletedProcess(args=["ssh"], returncode=7, stdout="", stderr="acp failed\n")

        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=proc):
            diagnostics = probe_mast_diagnostics_conn(connection)

        self.assertEqual(diagnostics.returncode, 7)
        self.assertEqual(diagnostics.volumes, ())
        self.assertEqual(diagnostics.stderr, "acp failed\n")
        summary = mast_probe_debug_summary(diagnostics)
        self.assertEqual(summary["mast_probe_stderr_chars"], len("acp failed\n"))
        self.assertEqual(summary["mast_probe_stderr"], "acp failed\n")

    def test_mast_probe_debug_summary_bounds_long_output(self) -> None:
        diagnostics = MaStProbeDiagnostics(
            command=MAST_PROBE_COMMAND,
            returncode=0,
            volumes=(),
            stdout="a" * 10000,
            stderr="b" * 10001,
        )

        summary = mast_probe_debug_summary(diagnostics)

        self.assertEqual(summary["mast_probe_stdout_chars"], 10000)
        self.assertEqual(summary["mast_probe_stderr_chars"], 10001)
        self.assertIn("<truncated", str(summary["mast_probe_stdout"]))
        self.assertIn("<truncated", str(summary["mast_probe_stderr"]))
        self.assertLess(len(str(summary["mast_probe_stdout"])), 10000)
        self.assertLess(len(str(summary["mast_probe_stderr"])), 10001)

    def test_payload_candidate_order_is_internal_first_then_external_mast_order_in_python(self) -> None:
        external_a = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB A", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external_b = MaStVolume("sd1", "dk4", "/Volumes/dk4", "USB B", "7d40eaac-182b-562b-a7b8-49bb5ed69c0f", False, "hfs")

        self.assertEqual(
            ordered_payload_candidate_volumes((external_a, internal, external_b)),
            (internal, external_a, external_b),
        )

    def test_select_payload_home_prefers_writable_internal_volume(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=VolumeMountResult(True)) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", side_effect=[True]) as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (external, internal), ".samba4", wait_seconds=30, claims=VolumeClaims())

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"))
        mount_mock.assert_called_once_with(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=30, claims=mock.ANY)
        writable_mock.assert_called_once_with(connection, "/Volumes/dk2")

    def test_ensure_volume_root_mounted_conn_claims_diskd_without_mount_hfs_fallback(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch(
            "timecapsulesmb.device.storage.run_ssh",
            return_value=mock.Mock(returncode=0, stdout="use_volume_rcs=0 mounted=yes\n"),
        ) as run_ssh_mock:
            self.assertTrue(ensure_volume_root_mounted_conn(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=12, claims=VolumeClaims()))

        run_ssh_mock.assert_called_once()
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("/bin/df -k /Volumes/dk2", remote_command)
        self.assertIn("/usr/bin/tail -n +2", remote_command)
        self.assertIn("/usr/bin/acp rpc diskd.useVolume", remote_command)
        self.assertLess(remote_command.index("/usr/bin/acp rpc diskd.useVolume"), remote_command.index("/bin/df -k /Volumes/dk2"))
        self.assertIn('while [ "$diskd_attempt" -le 2 ]', remote_command)
        self.assertNotIn("mount_hfs", remote_command)
        self.assertNotIn("grep", remote_command)
        self.assertNotIn("awk", remote_command)
        self.assertNotIn("cut", remote_command)
        self.assertEqual(run_ssh_mock.call_args.kwargs["timeout"], 69)

    def test_ensure_volume_root_mounted_conn_reports_failure(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch(
            "timecapsulesmb.device.storage.run_ssh",
            return_value=mock.Mock(returncode=1, stdout="use_volume_rcs=1,1 mounted=no\n"),
        ):
            result = ensure_volume_root_mounted_conn(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=0, claims=VolumeClaims())
        self.assertFalse(result)
        self.assertEqual(result.detail, "use_volume_rcs=1,1 mounted=no")

    def test_ensure_volume_root_mounted_conn_tolerates_missing_status_line(self) -> None:
        # An SSH failure produces no script output; the result is still usable.
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=mock.Mock(returncode=255, stdout="")):
            result = ensure_volume_root_mounted_conn(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=0, claims=VolumeClaims())
        self.assertFalse(result)
        self.assertEqual(result.detail, "")

    def _run_mount_script(
        self, tmp: Path, *, use_volume_rcs: list[int], mounted_after_claim: bool, mounted_at_start: bool = False,
        errors: list[int | None] | None = None, held: bool = False,
    ):
        """Run the real mount script on a FakeDiskd.

        The fake acp answers with the given exit codes in order and, for a
        failure with an error code, prints acp's `### RPC function ... failed:
        CODE` line. The volume appears in df once a claim succeeds (if
        mounted_after_claim) or is mounted from the start (mounted_at_start)."""
        errors = errors or [None] * len(use_volume_rcs)
        diskd = FakeDiskd(
            tmp, mounted=mounted_at_start, answers=zip(use_volume_rcs, errors), default=None,
            mounts=mounted_after_claim,
        )
        script = render_ensure_volume_root_mounted_script("/Volumes/dk2", "/dev/dk2", 1, held=held)
        result = diskd.run_ssh(None, f"/bin/sh -c {shlex.quote(script)}")
        return result, {"calls": diskd.calls}

    def test_mount_script_reports_a_successful_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, state = self._run_mount_script(Path(tmp), use_volume_rcs=[0], mounted_after_claim=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "use_volume_rcs=0 mounted=yes use_volume_errors=0")
        self.assertEqual(state["calls"], 1)

    def test_mount_script_reports_each_refused_claim_and_an_unmounted_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, state = self._run_mount_script(Path(tmp), use_volume_rcs=[1, 3], mounted_after_claim=True)
        self.assertEqual(result.returncode, 1)
        # acp printed no error code: "?".
        self.assertEqual(result.stdout.strip(), "use_volume_rcs=1,3 mounted=no use_volume_errors=?,?")
        self.assertEqual(state["calls"], 2)

    def test_mount_script_reports_a_mounted_volume_diskd_would_not_claim(self) -> None:
        # Mounted but unclaimed still fails: diskd may unmount it mid-deploy.
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run_mount_script(
                Path(tmp), use_volume_rcs=[1, 1], mounted_after_claim=False, mounted_at_start=True,
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "use_volume_rcs=1,1 mounted=yes use_volume_errors=?,?")

    def test_mount_result_tells_a_mounted_but_unconfirmed_volume_from_a_missing_one(self) -> None:
        cases = [
            (VolumeMountResult(True, "use_volume_rcs=0 mounted=yes"), True, True),
            # diskd refused, or mounted it only after the wait: not usable, but there.
            (VolumeMountResult(False, "use_volume_rcs=1,1 mounted=yes"), False, True),
            (VolumeMountResult(False, "use_volume_rcs=0,0 mounted=yes"), False, True),
            (VolumeMountResult(False, "use_volume_rcs=1,1 mounted=no"), False, False),
            # No script output (the SSH command failed): nothing is known.
            (VolumeMountResult(False, ""), False, False),
        ]
        for result, usable, present in cases:
            with self.subTest(detail=result.detail, mounted=result.mounted):
                self.assertIs(bool(result), usable)
                self.assertIs(result.present, present)

    def test_mount_script_reports_an_accepted_claim_whose_volume_never_appears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, state = self._run_mount_script(Path(tmp), use_volume_rcs=[0, 0], mounted_after_claim=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "use_volume_rcs=0,0 mounted=no use_volume_errors=0,0")
        self.assertEqual(state["calls"], 2)

    def test_mount_script_keeps_a_mounted_volume_when_acpd_lost_diskd_rpc_names(self) -> None:
        # -6727: a second diskd deleted diskd.useVolume, or diskd does not know
        # the volume. diskd cannot unmount it either way: use it, no retry.
        with tempfile.TemporaryDirectory() as tmp:
            result, state = self._run_mount_script(
                Path(tmp), use_volume_rcs=[22, 22], errors=[-6727, -6727],
                mounted_after_claim=False, mounted_at_start=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "use_volume_rcs=22 mounted=yes use_volume_errors=-6727")
        self.assertEqual(state["calls"], 1)

    def test_mount_script_reports_lost_diskd_rpc_names_on_an_unmounted_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, state = self._run_mount_script(
                Path(tmp), use_volume_rcs=[22, 22], errors=[-6727, -6727], mounted_after_claim=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "use_volume_rcs=22,22 mounted=no use_volume_errors=-6727,-6727")
        self.assertEqual(state["calls"], 2)

    def test_mount_script_refuses_a_mounted_volume_for_any_other_diskd_error(self) -> None:
        # -6723: diskd is disconnecting every user (archive or erase): it will unmount.
        with tempfile.TemporaryDirectory() as tmp:
            result, state = self._run_mount_script(
                Path(tmp), use_volume_rcs=[22, 22], errors=[-6723, -6723],
                mounted_after_claim=False, mounted_at_start=True,
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "use_volume_rcs=22,22 mounted=yes use_volume_errors=-6723,-6723")
        self.assertEqual(state["calls"], 2)
        self.assertTrue(VolumeMountResult(False, result.stdout.strip()).present)

    def test_mount_script_skips_the_claim_for_a_held_volume_that_is_still_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, state = self._run_mount_script(
                Path(tmp), use_volume_rcs=[], mounted_after_claim=True, mounted_at_start=True, held=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "use_volume_rcs= mounted=yes use_volume_errors= claim=held")
        self.assertEqual(state["calls"], 0)

    def test_mount_script_claims_a_held_volume_again_once_it_is_unmounted(self) -> None:
        # diskd unmounts only when the count reaches 0, so the claim is gone.
        with tempfile.TemporaryDirectory() as tmp:
            result, state = self._run_mount_script(
                Path(tmp), use_volume_rcs=[0], mounted_after_claim=True, mounted_at_start=False, held=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "use_volume_rcs=0 mounted=yes use_volume_errors=0")
        self.assertEqual(state["calls"], 1)

    def test_mount_script_claims_a_mounted_volume_it_does_not_hold(self) -> None:
        # Mounted without our claim, another client's release could unmount it.
        with tempfile.TemporaryDirectory() as tmp:
            result, state = self._run_mount_script(
                Path(tmp), use_volume_rcs=[0], mounted_after_claim=True, mounted_at_start=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["calls"], 1)

    def _guard(self, diskd: FakeDiskd, claims: VolumeClaims) -> VolumeMountResult:
        with mock.patch("timecapsulesmb.device.storage.run_ssh", side_effect=diskd.run_ssh), \
                mock.patch("timecapsulesmb.device.storage.raise_if_acpd_silent"):
            return ensure_volume_root_mounted_conn(
                SshConnection("root@10.0.0.2", "pw", ""), "/Volumes/dk2", "/dev/dk2", wait_seconds=1, claims=claims,
            )

    def test_fake_diskd_fails_a_call_it_has_no_answer_for(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            diskd = FakeDiskd(Path(tmp), answers=[(0, None)], default=None)
            self.assertTrue(self._guard(diskd, VolumeClaims()))
            with self.assertRaisesRegex(AssertionError, "more often than answered"):
                self._guard(diskd, VolumeClaims())

    def test_an_operation_claims_a_volume_once_however_often_it_checks_it(self) -> None:
        claims = VolumeClaims()
        with tempfile.TemporaryDirectory() as tmp:
            diskd = FakeDiskd(Path(tmp))
            results = [self._guard(diskd, claims) for _ in range(5)]
            self.assertEqual(diskd.calls, 1)
        self.assertTrue(all(results))
        self.assertEqual(claims.roots, {"/Volumes/dk2"})

    def test_a_held_volume_that_diskd_unmounted_is_claimed_again(self) -> None:
        claims = VolumeClaims()
        with tempfile.TemporaryDirectory() as tmp:
            diskd = FakeDiskd(Path(tmp))
            self.assertTrue(self._guard(diskd, claims))
            diskd.unmount()
            self.assertTrue(self._guard(diskd, claims))
            self.assertEqual(diskd.calls, 2)

    def test_each_operation_makes_its_own_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            diskd = FakeDiskd(Path(tmp), mounted=True)
            self.assertTrue(self._guard(diskd, VolumeClaims()))
            self.assertTrue(self._guard(diskd, VolumeClaims()))
            self.assertEqual(diskd.calls, 2)

    def test_a_refused_claim_is_not_remembered(self) -> None:
        claims = VolumeClaims()
        with tempfile.TemporaryDirectory() as tmp:
            diskd = FakeDiskd(Path(tmp), answers=[(22, -6723), (22, -6723)])
            self.assertFalse(self._guard(diskd, claims))
            self.assertEqual(claims.roots, set())
            self.assertTrue(self._guard(diskd, claims))
            self.assertEqual(diskd.calls, 3)
        self.assertEqual(claims.roots, {"/Volumes/dk2"})

    def test_a_mounted_volume_kept_after_lost_rpc_names_is_not_remembered(self) -> None:
        # -6727 on a mounted volume succeeds without a claim: the next check asks again.
        claims = VolumeClaims()
        with tempfile.TemporaryDirectory() as tmp:
            diskd = FakeDiskd(Path(tmp), mounted=True, default=(22, -6727))
            self.assertTrue(self._guard(diskd, claims))
            self.assertTrue(self._guard(diskd, claims))
            self.assertEqual(diskd.calls, 2)
        self.assertEqual(claims.roots, set())

    def test_an_accepted_claim_is_remembered_even_when_the_volume_never_appears(self) -> None:
        claims = VolumeClaims()
        with tempfile.TemporaryDirectory() as tmp:
            diskd = FakeDiskd(Path(tmp), mounts=False)
            result = self._guard(diskd, claims)
            self.assertEqual(diskd.calls, 2)
        self.assertFalse(result)
        self.assertEqual(claims.roots, {"/Volumes/dk2"})

    def test_diskd_rpc_status_reports_acpd_answer_or_error_code(self) -> None:
        cases = [
            ("echo '<plist/>'", "answered"),
            ("echo '### RPC function \"diskd.getVolumeCounts\" failed: -6727' >&2; exit 22", "-6727"),
            ("echo '### invalid function parameters' >&2; exit 22", "?"),
        ]
        for fake_acp, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                acp = Path(tmp) / "acp"
                acp.write_text(f"#!/bin/sh\n{fake_acp}\n")
                acp.chmod(0o755)

                def run(connection, command, **kwargs):
                    script = shlex.split(command)[2].replace("/usr/bin/acp", str(acp))
                    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)

                with mock.patch("timecapsulesmb.device.storage.run_ssh", side_effect=run):
                    self.assertEqual(diskd_rpc_status_conn(SshConnection("root@10.0.0.2", "pw", "")), expected)

    def test_render_ensure_volume_root_mounted_script_quotes_paths(self) -> None:
        script = render_ensure_volume_root_mounted_script("/Volumes/dk 2", "/dev/dk2", 1)
        self.assertIn("mkdir -p '/Volumes/dk 2'", script)
        self.assertIn("diskd.useVolume path:s:'/Volumes/dk 2'", script)

    def test_verify_payload_home_conn_passes_for_boot_compatible_payload(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=VolumeMountResult(True)) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=mock.Mock(returncode=0, stdout="ok\n")) as run_ssh_mock:
                result = verify_payload_home_conn(connection, payload_home, wait_seconds=5, claims=VolumeClaims())

        self.assertEqual(result, PayloadVerificationResult(True, "ok"))
        mount_mock.assert_called_once_with(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=5, claims=mock.ANY)
        remote_command = run_ssh_mock.call_args.args[1]
        self.assertIn("[ -d /Volumes/dk2/.samba4 ]", remote_command)
        self.assertIn("[ -x /Volumes/dk2/.samba4/smbd ]", remote_command)
        self.assertIn("[ -x /Volumes/dk2/.samba4/sbin/smbd ]", remote_command)
        self.assertIn("[ -d /Volumes/dk2/.samba4/private ]", remote_command)

    def test_verify_payload_home_conn_reports_mount_and_payload_failures(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        payload_home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=VolumeMountResult(False)):
            result = verify_payload_home_conn(connection, payload_home, wait_seconds=5, claims=VolumeClaims())
        self.assertEqual(result, PayloadVerificationResult(False, "volume /Volumes/dk2 is not mounted"))

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=VolumeMountResult(True)):
            with mock.patch(
                "timecapsulesmb.device.storage.run_ssh",
                return_value=mock.Mock(returncode=1, stdout="missing smbd; missing private directory\n"),
            ):
                result = verify_payload_home_conn(connection, payload_home, wait_seconds=5, claims=VolumeClaims())
        self.assertEqual(result, PayloadVerificationResult(False, "missing smbd; missing private directory"))

    def test_select_payload_home_skips_unmountable_internal_before_external(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", side_effect=[VolumeMountResult(False), VolumeMountResult(True)]) as mount_mock:
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=True) as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (external, internal), ".samba4", wait_seconds=9, claims=VolumeClaims())

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))
        self.assertEqual(
            mount_mock.call_args_list,
            [
                mock.call(connection, "/Volumes/dk2", "/dev/dk2", wait_seconds=9, claims=mock.ANY),
                mock.call(connection, "/Volumes/dk3", "/dev/dk3", wait_seconds=9, claims=mock.ANY),
            ],
        )
        writable_mock.assert_called_once_with(connection, "/Volumes/dk3")

    def test_select_payload_home_with_diagnostics_records_mount_and_write_results(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch(
            "timecapsulesmb.device.storage.ensure_volume_root_mounted_conn",
            side_effect=[
                VolumeMountResult(False, "use_volume_rcs=1,1 mounted=no"),
                VolumeMountResult(True, "use_volume_rcs=0 mounted=yes"),
            ],
        ):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=True):
                selection = select_payload_home_with_diagnostics_conn(
                    connection,
                    (external, internal),
                    ".samba4",
                    wait_seconds=9,
                    claims=VolumeClaims(),
                )

        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))
        self.assertEqual(selection.checks[0].volume, internal)
        self.assertFalse(selection.checks[0].mounted)
        self.assertIsNone(selection.checks[0].writable)
        self.assertEqual(selection.checks[1].volume, external)
        self.assertTrue(selection.checks[1].mounted)
        self.assertTrue(selection.checks[1].writable)
        self.assertEqual(
            payload_candidate_checks_debug_summary(selection.checks),
            [
                {
                    "disk": "wd0",
                    "part": "dk2",
                    "root": "/Volumes/dk2",
                    "name": "Data",
                    "format": "hfs",
                    "builtin": True,
                    "uuid": "f42bdb83-c265-5522-a087-25606a4d0abf",
                    "mounted": False,
                    "writable": None,
                    "mount": "use_volume_rcs=1,1 mounted=no",
                },
                {
                    "disk": "sd0",
                    "part": "dk3",
                    "root": "/Volumes/dk3",
                    "name": "USB",
                    "format": "hfs",
                    "builtin": False,
                    "uuid": "51f93e6f-dc69-524d-986d-cee4d7cb3573",
                    "mounted": True,
                    "writable": True,
                    "mount": "use_volume_rcs=0 mounted=yes",
                },
            ],
        )

    def test_select_payload_home_with_diagnostics_returns_no_home_when_all_unwritable(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=VolumeMountResult(True)):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=False):
                selection = select_payload_home_with_diagnostics_conn(
                    connection,
                    (internal, external),
                    ".samba4",
                    wait_seconds=30,
                    claims=VolumeClaims(),
                )

        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.mounted for check in selection.checks], [True, True])
        self.assertEqual([check.writable for check in selection.checks], [False, False])

    def test_volume_root_writable_timeout_raises_coded_disk_write_error(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        with mock.patch(
            "timecapsulesmb.device.storage.run_ssh",
            side_effect=SshCommandTimeout("Timed out waiting for ssh command to finish: mkdir write test"),
        ):
            with self.assertRaises(StorageDeviceError) as raised:
                volume_root_is_writable_conn(connection, "/Volumes/dk2")

        self.assertEqual(raised.exception.code, "disk_write_test_unresponsive")
        self.assertIn("The disk did not respond when tested.", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, SshCommandTimeout)

    def test_select_payload_home_records_unmountable_candidates(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=VolumeMountResult(False)):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn") as writable_mock:
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30, claims=VolumeClaims())

        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.mounted for check in selection.checks], [False, False])
        writable_mock.assert_not_called()

    def test_select_payload_home_falls_back_to_external_and_records_none_writable(self) -> None:
        connection = SshConnection("root@10.0.0.2", "pw", "")
        internal = MaStVolume("wd0", "dk2", "/Volumes/dk2", "Data", "f42bdb83-c265-5522-a087-25606a4d0abf", True, "hfs")
        external = MaStVolume("sd0", "dk3", "/Volumes/dk3", "USB", "51f93e6f-dc69-524d-986d-cee4d7cb3573", False, "hfs")

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=VolumeMountResult(True)):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", side_effect=[False, True]):
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30, claims=VolumeClaims())
        self.assertEqual(selection.payload_home, PayloadHome("/Volumes/dk3", "/dev/dk3", ".samba4"))

        with mock.patch("timecapsulesmb.device.storage.ensure_volume_root_mounted_conn", return_value=VolumeMountResult(True)):
            with mock.patch("timecapsulesmb.device.storage.volume_root_is_writable_conn", return_value=False):
                selection = select_payload_home_with_diagnostics_conn(connection, (internal, external), ".samba4", wait_seconds=30, claims=VolumeClaims())
        self.assertIsNone(selection.payload_home)
        self.assertEqual([check.writable for check in selection.checks], [False, False])

    def test_flash_runtime_config_contains_runtime_settings_and_no_share_name(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_SAMBA_USER": "admin",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,106",
                "TC_AIRPORT_SYAP": "106",
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
                "TC_SMB_BROWSE_COMPATIBILITY": "true",
                "TC_ANY_PROTOCOL": "true",
                "TC_FRUIT_METADATA_NETATALK": "true",
                "TC_VFS_AIO_FORK_DISABLED": "false",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=True,
        )

        self.assertNotIn("PAYLOAD_DIR_NAME", rendered)
        self.assertNotIn("SMB_SAMBA_USER", rendered)
        self.assertNotIn("MDNS_DEVICE_MODEL", rendered)
        self.assertNotIn("AIRPORT_SYAP", rendered)
        self.assertNotIn("NET_IFACE", rendered)
        self.assertNotIn("NET_IPV4_HINT", rendered)
        self.assertNotIn("PAYLOAD_VOLUME_HINT", rendered)
        self.assertNotIn("PAYLOAD_DEVICE_HINT", rendered)
        self.assertNotIn("PAYLOAD_INSTALL_ID", rendered)
        self.assertIn(f"TC_DEPLOY_RELEASE_TAG={RELEASE_TAG}\n", rendered)
        self.assertIn(f"TC_DEPLOY_CLI_VERSION_CODE={CLI_VERSION_CODE}\n", rendered)
        self.assertIn("TELEMETRY=true\n", rendered)
        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertNotIn("SMB_BIND_LAN_ONLY", rendered)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=1\n", rendered)
        self.assertIn("MDNS_ADVERTISE_AFP=0\n", rendered)
        self.assertIn("ANY_PROTOCOL=1\n", rendered)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=0\n", rendered)
        self.assertIn("FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=0\n", rendered)
        self.assertIn("FRUIT_METADATA_NETATALK=1\n", rendered)
        self.assertIn("VFS_AIO_FORK_ENABLED=1\n", rendered)
        self.assertIn("DISKD_USE_VOLUME_ATTEMPTS=2\n", rendered)
        self.assertIn("ATA_IDLE_SECONDS=300\n", rendered)
        self.assertIn("ATA_STANDBY=''\n", rendered)
        self.assertNotIn("NBNS_ENABLED=", rendered)
        self.assertIn("RSYNC_ENABLED=0\n", rendered)
        self.assertIn("SMBD_DEBUG_LOGGING=1\n", rendered)
        self.assertNotIn("SMB_NETBIOS_NAME", rendered)
        self.assertNotIn("TC_CONFIG_VERSION", rendered)
        # Name overrides travel only when the user set them (v3.1.0: the native
        # helpers read them from this file).
        self.assertNotIn("TC_MDNS_INSTANCE_NAME", rendered)
        self.assertNotIn("TC_NETBIOS_NAME", rendered)
        self.assertNotIn("MDNS_HOST_LABEL", rendered)

    def test_flash_runtime_config_ignores_deprecated_name_overrides(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_MDNS_INSTANCE_NAME": "James's Capsule",
                "TC_NETBIOS_NAME": "JAMESCAP",
                "TC_MDNS_DEVICE_MODEL": "TimeCapsule6,106",
            }
        )
        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
        )
        # Old configs remain loadable, but neither shell nor native runtime
        # receives an override. Exercise the rendered shell environment too.
        proc = subprocess.run(["/bin/sh", "-c", rendered + "printf '%s|%s\\n' \"${TC_MDNS_INSTANCE_NAME-unset}\" \"${TC_NETBIOS_NAME-unset}\""],
                              capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout, "unset|unset\n")
        self.assertNotIn("TC_SHARE_NAME", rendered)

    def test_flash_runtime_config_can_disable_telemetry(self) -> None:
        rendered = render_flash_runtime_config(
            AppConfig.from_values({}),
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            telemetry_enabled=False,
        )

        self.assertIn("TELEMETRY=false\n", rendered)

    def test_payload_checksums_record_the_crc32_of_each_binary_staged_to_ram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            smbd, rsync = Path(tmp) / "smbd", Path(tmp) / "rsync"
            smbd.write_bytes(b"payload data")
            rsync.write_bytes(b"rsync 316")
            rendered = render_payload_checksums(smbd_path=smbd, rsync_path=rsync)
        # zlib's CRC-32, eight lowercase hex digits with leading zeros kept,
        # in the "NAME XXXXXXXX" lines the manager's staging reads.
        self.assertEqual(rendered, "smbd 84d769f0\nrsync 00e7f91d\n")

    def test_flash_runtime_config_records_no_checksums(self) -> None:
        # They travel with the payload they describe, not in device config.
        rendered = render_flash_runtime_config(
            AppConfig.from_values({}),
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
        )

        self.assertNotIn("CRC32", rendered)

    def test_flash_runtime_config_can_enable_rsync(self) -> None:
        rendered = render_flash_runtime_config(
            AppConfig.from_values({}),
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            rsync_enabled=True,
        )

        self.assertIn("RSYNC_ENABLED=1\n", rendered)

    def test_rsync_daemon_config_chroots_payload_volume_share_root_without_pid_file(self) -> None:
        rendered = render_rsync_daemon_config(PayloadHome("/Volumes/dk5", "/dev/dk5", ".samba4"))

        self.assertEqual(
            rendered,
            textwrap.dedent(
                """\
                port = 873
                log file = /mnt/Memory/samba4/var/rsync.log
                uid = root
                gid = wheel
                use chroot = yes
                read only = false
                list = true

                [shareroot]
                path = /Volumes/dk5/ShareRoot
                """
            ),
        )
        self.assertNotIn("pid file", rendered.lower())

    def test_flash_runtime_config_uses_saved_debug_logging(self) -> None:
        config = AppConfig.from_values({"TC_DEBUG_LOGGING": "true"})

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=None,
        )

        self.assertIn("SMBD_DEBUG_LOGGING=1\n", rendered)
        self.assertIn("MDNS_DEBUG_LOGGING=1\n", rendered)

    def test_flash_runtime_config_deploy_time_debug_override_can_disable_saved_value(self) -> None:
        config = AppConfig.from_values({"TC_DEBUG_LOGGING": "true"})

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
        )

        self.assertIn("SMBD_DEBUG_LOGGING=0\n", rendered)
        self.assertIn("MDNS_DEBUG_LOGGING=0\n", rendered)

    def test_flash_runtime_config_accepts_deploy_time_advanced_overrides(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "false",
                "TC_SMB_BROWSE_COMPATIBILITY": "false",
                "TC_ANY_PROTOCOL": "false",
                "TC_FRUIT_METADATA_NETATALK": "false",
                "TC_VFS_AIO_FORK_DISABLED": "true",
            }
        )

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
            internal_share_use_disk_root=True,
            smb_browse_compatibility=True,
            mdns_advertise_afp=True,
            any_protocol=True,
            fruit_metadata_netatalk=True,
            vfs_aio_fork_enabled=True,
        )

        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=1\n", rendered)
        self.assertNotIn("SMB_BIND_LAN_ONLY", rendered)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=1\n", rendered)
        self.assertIn("MDNS_ADVERTISE_AFP=1\n", rendered)
        self.assertIn("ANY_PROTOCOL=1\n", rendered)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=0\n", rendered)
        self.assertIn("FRUIT_METADATA_NETATALK=1\n", rendered)
        self.assertIn("VFS_AIO_FORK_ENABLED=1\n", rendered)

    def test_flash_runtime_config_requires_smb_encryption(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_ANY_PROTOCOL": "false",
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
        )

        self.assertIn("ANY_PROTOCOL=0\n", rendered)
        self.assertIn("REQUIRE_SMB_ENCRYPTION=1\n", rendered)

    def test_flash_runtime_config_forces_smb_signing_and_encryption_off(self) -> None:
        config = AppConfig.from_values(
            {"TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION": "true"}
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
        )

        self.assertIn("REQUIRE_SMB_ENCRYPTION=0\n", rendered)
        self.assertIn("FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION=1\n", rendered)

    def test_flash_runtime_config_rejects_required_and_disabled_smb_encryption(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
                "TC_FORCE_DISABLE_SMB_SIGNING_AND_ENCRYPTION": "true",
            }
        )

        with self.assertRaisesRegex(ValueError, "cannot be used with Force Disable"):
            render_flash_runtime_config(
                config,
                PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            )

    def test_flash_runtime_config_rejects_any_protocol_with_smb_encryption(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_ANY_PROTOCOL": "true",
                "TC_REQUIRE_SMB_ENCRYPTION": "true",
            }
        )

        with self.assertRaisesRegex(ValueError, "SMB encryption requires SMB3-only"):
            render_flash_runtime_config(
                config,
                PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            )

    def test_flash_runtime_config_deploy_time_overrides_can_disable_saved_values(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_INTERNAL_SHARE_USE_DISK_ROOT": "true",
                "TC_SMB_BROWSE_COMPATIBILITY": "true",
                "TC_MDNS_ADVERTISE_AFP": "true",
                "TC_ANY_PROTOCOL": "true",
                "TC_FRUIT_METADATA_NETATALK": "true",
                "TC_VFS_AIO_FORK_DISABLED": "false",
            }
        )

        rendered = render_gui_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
            internal_share_use_disk_root=False,
            smb_browse_compatibility=False,
            mdns_advertise_afp=False,
            any_protocol=False,
            fruit_metadata_netatalk=False,
            vfs_aio_fork_enabled=False,
        )

        self.assertIn("INTERNAL_SHARE_USE_DISK_ROOT=0\n", rendered)
        self.assertNotIn("SMB_BIND_LAN_ONLY", rendered)
        self.assertIn("SMB_BROWSE_COMPATIBILITY=0\n", rendered)
        self.assertIn("MDNS_ADVERTISE_AFP=0\n", rendered)
        self.assertIn("ANY_PROTOCOL=0\n", rendered)
        self.assertIn("FRUIT_METADATA_NETATALK=0\n", rendered)
        self.assertIn("VFS_AIO_FORK_ENABLED=0\n", rendered)


    def test_flash_runtime_config_turns_vfs_aio_fork_on_unless_disabled(self) -> None:
        home = PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4")
        cases = (
            ("no key", {}, None, "1"),
            # The old opt-in key, saved as "false" by default, decides nothing.
            ("old key false", {"TC_VFS_AIO_FORK_ENABLED": "false"}, None, "1"),
            ("disabled", {"TC_VFS_AIO_FORK_DISABLED": "true"}, None, "0"),
            ("not disabled", {"TC_VFS_AIO_FORK_DISABLED": "false"}, None, "1"),
            ("override off", {"TC_VFS_AIO_FORK_DISABLED": "false"}, False, "0"),
            ("override on", {"TC_VFS_AIO_FORK_DISABLED": "true"}, True, "1"),
        )
        for name, values, override, expected in cases:
            with self.subTest(name):
                rendered = render_flash_runtime_config(AppConfig.from_values(values), home,
                                                       vfs_aio_fork_enabled=override)
                self.assertIn(f"\nVFS_AIO_FORK_ENABLED={expected}\n", rendered)

    def test_flash_runtime_config_uses_drive_settings_from_config(self) -> None:
        config = AppConfig.from_values(
            {
                "TC_ATA_IDLE_SECONDS": "0",
                "TC_ATA_STANDBY": "0",
            }
        )

        rendered = render_flash_runtime_config(
            config,
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            debug_logging=False,
        )

        self.assertIn("ATA_IDLE_SECONDS=0\n", rendered)
        self.assertIn("ATA_STANDBY=0\n", rendered)


    def test_deployment_plan_uses_flash_pointer_and_single_private_payload(self) -> None:
        plan = build_deployment_plan(
            "root@10.0.0.2",
            PayloadHome("/Volumes/dk2", "/dev/dk2", ".samba4"),
            Path("/tmp/smbd"),

            xattr_migrator_path=Path("/tmp/xattr-hfs-migrate"),
            rsync_path=Path("/tmp/rsync"),
         service_path=Path("bin/service"))
        source_ids = {upload.source_id for upload in plan.uploads}

        self.assertNotIn(GENERATED_FLASH_CONFIG_SOURCE, source_ids)
        self.assertEqual(plan.config_upload.source_id, GENERATED_FLASH_CONFIG_SOURCE)
        self.assertNotIn("rendered:smb.conf.template", source_ids)
        self.assertNotIn("generated:adisk.uuid", source_ids)
        self.assertNotIn("generated:nbns.enabled", source_ids)
        self.assertNotIn("generated:install.id", source_ids)
        self.assertEqual(plan.private_dir, "/Volumes/dk2/.samba4/private")
        self.assertEqual(plan.flash_targets["tcapsulesmb.conf"], "/mnt/Flash/tcapsulesmb.conf")
        self.assertIn("/Volumes/dk2/.samba4/smb.conf.template", {path for action in plan.replace_software_actions for path in getattr(action, "paths", ())})
        self.assertIn("/Volumes/dk2/.samba4/private/adisk.uuid", {path for action in plan.replace_software_actions for path in getattr(action, "paths", ())})
        self.assertIn("/Volumes/dk2/.samba4/private/nbns.enabled", {path for action in plan.replace_software_actions for path in getattr(action, "paths", ())})
        # The installer applies config permissions after verified migration.
        self.assertNotIn(
            ("/mnt/Flash/tcapsulesmb.conf", "600"),
            {(permission.path, permission.mode) for permission in plan.permissions},
        )


    # ---- v3.1.0 boot: Apple mDNSResponder stays, diskd moves to loopback ----


    # ---- v3.1.0 discovery policy and registrant restarts ----


if __name__ == "__main__":
    unittest.main()


class AcpdDiskReadTests(unittest.TestCase):
    """A failed or hung MaSt or diskd.useVolume blames Apple's ACPd only when
    the device's own acp cannot read syUT either; acp prints the same failed-get
    text for any property and exits 0."""

    CONNECTION = SshConnection("root@10.0.0.2", "pw", "")
    FAILED_GET = "### get 'MaSt' failed: <<UNKNOWN FORMAT CONVERSION CODE %m>>\n"

    def read(self, outcome, *, answers: bool):
        from timecapsulesmb.device import storage

        run = mock.Mock(side_effect=outcome) if isinstance(outcome, Exception) else mock.Mock(
            return_value=subprocess.CompletedProcess(["ssh"], 0, stdout=outcome, stderr=""))
        with mock.patch("timecapsulesmb.device.storage.run_ssh", run), \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=answers) as answer:
            try:
                return storage.read_mast_volumes_with_output_conn(self.CONNECTION), answer
            except Exception as exc:  # noqa: BLE001 - the test inspects what was raised
                return exc, answer

    def test_a_failed_mast_get_with_a_silent_acpd_is_broken_acpd(self) -> None:
        from timecapsulesmb.device.storage import StorageDeviceError

        error, answer = self.read(self.FAILED_GET, answers=False)
        self.assertIsInstance(error, StorageDeviceError)
        self.assertEqual(error.code, "acpd_broken")
        self.assertIn("Apple ACPd is broken", str(error))
        answer.assert_called_once_with(self.CONNECTION)

    QUIT_LISTING = "  107 1 15 S 0 select 0:00.32 sshd\ntcapsule-ps: end\n"
    RUNNING_LISTING = "  107 1 15 S 0 select 0:00.32 sshd\n  190 1 15 Sa 8 kqread 0:00.00 ACPd\ntcapsule-ps: end\n"

    def test_a_failed_mast_get_on_a_device_whose_acpd_quit_says_so(self) -> None:
        # syUT fails at once when ACPd is gone; two listings without it name
        # the cause, with its own code and next steps.
        from timecapsulesmb.device.storage import StorageDeviceError

        with mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=self.QUIT_LISTING) as listing:
            error, answer = self.read(self.FAILED_GET, answers=False)
        self.assertIsInstance(error, StorageDeviceError)
        self.assertEqual(error.code, "acpd_quit")
        self.assertEqual(
            str(error),
            "Apple ACPd is broken: the AirPort service on this device has quit, a bug in Apple's firmware. "
            "File sharing stays off until the device restarts. Its disks cannot be read.",
        )
        self.assertEqual(listing.call_count, 2)
        answer.assert_called_once_with(self.CONNECTION)

    def test_a_failed_mast_get_on_a_device_whose_acpd_quit_after_samba_started_says_sharing_may_work(self) -> None:
        from timecapsulesmb.device.storage import StorageDeviceError

        serving = self.QUIT_LISTING.replace("tcapsule-ps: end", "  320 300 320 Ss 1 select 0:01.20 smbd\ntcapsule-ps: end")
        with mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=serving):
            error, _answer = self.read(self.FAILED_GET, answers=False)
        self.assertIsInstance(error, StorageDeviceError)
        self.assertEqual(error.code, "acpd_quit")
        self.assertEqual(
            str(error),
            "Apple ACPd is broken: the AirPort service on this device has quit, a bug in Apple's firmware. "
            "Sharing may work now, but until the device is unplugged it cannot be updated or restarted over the "
            "network, and AirPort Utility cannot reach it. Its disks cannot be read.",
        )

    def test_a_failed_mast_get_with_acpd_still_running_stays_broken_acpd(self) -> None:
        for name, listing in (("running", self.RUNNING_LISTING), ("unread", "")):
            with self.subTest(listing=name):
                with mock.patch("timecapsulesmb.device.acpd.read_processes", return_value=listing):
                    error, _answer = self.read(self.FAILED_GET, answers=False)
                self.assertEqual(error.code, "acpd_broken")
                self.assertIn("is not answering", str(error))

    def test_an_acpd_that_answers_is_never_listed(self) -> None:
        with mock.patch("timecapsulesmb.device.acpd.read_processes") as listing:
            self.read(self.FAILED_GET, answers=True)
        listing.assert_not_called()

    def test_a_failed_mast_get_with_a_working_acpd_is_an_empty_read(self) -> None:
        result, _answer = self.read(self.FAILED_GET, answers=True)
        self.assertEqual(result.volumes, ())

    def test_a_valid_empty_mast_never_asks_acpd(self) -> None:
        result, answer = self.read("MaSt=[]\n", answers=False)
        self.assertEqual(result.volumes, ())
        answer.assert_not_called()

    def test_a_hung_mast_read_blames_acpd_only_when_syut_fails(self) -> None:
        timeout = SshCommandTimeout("Timed out waiting for ssh command to finish: /usr/bin/acp MaSt")
        for answers, code in ((False, "acpd_broken"), (True, None)):
            with self.subTest(answers=answers):
                error, _answer = self.read(timeout, answers=answers)
                self.assertEqual(getattr(error, "code", None), code)
                if code is None:
                    self.assertIs(error, timeout)

    def test_broken_acpd_stops_the_mast_retries(self) -> None:
        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=subprocess.CompletedProcess(
                ["ssh"], 0, stdout=self.FAILED_GET, stderr="")) as run, \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=False), \
                mock.patch("timecapsulesmb.device.storage.time.sleep") as sleep:
            with self.assertRaises(Exception) as raised:
                wait_for_mast_volumes_conn(self.CONNECTION, attempts=10, delay_seconds=3)
        self.assertEqual(getattr(raised.exception, "code", None), "acpd_broken")
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def mount(self, outcome, *, answers: bool):
        from timecapsulesmb.device.storage import ensure_volume_root_mounted_conn

        run = mock.Mock(side_effect=outcome) if isinstance(outcome, Exception) else mock.Mock(return_value=outcome)
        with mock.patch("timecapsulesmb.device.storage.run_ssh", run), \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=answers) as answer:
            try:
                return ensure_volume_root_mounted_conn(self.CONNECTION, "/Volumes/dk2", "/dev/dk2", wait_seconds=1, claims=VolumeClaims()), answer
            except Exception as exc:  # noqa: BLE001 - the test inspects what was raised
                return exc, answer

    def test_a_refused_use_volume_is_diskd_s_answer_while_acpd_works(self) -> None:
        refused = subprocess.CompletedProcess(
            ["ssh"], 1, stdout="use_volume_rcs=22,22 mounted=no use_volume_errors=-6727,-6727\n", stderr="")
        result, answer = self.mount(refused, answers=True)
        self.assertFalse(result)
        self.assertEqual(result.detail, "use_volume_rcs=22,22 mounted=no use_volume_errors=-6727,-6727")
        answer.assert_called_once_with(self.CONNECTION)
        error, _answer = self.mount(refused, answers=False)
        self.assertEqual(error.code, "acpd_broken")

    def test_a_hung_use_volume_with_a_silent_acpd_is_broken_acpd(self) -> None:
        error, _answer = self.mount(SshCommandTimeout("Timed out waiting for ssh command to finish"), answers=False)
        self.assertEqual(error.code, "acpd_broken")

    def test_a_claimed_volume_never_asks_acpd(self) -> None:
        claimed = subprocess.CompletedProcess(["ssh"], 0, stdout="use_volume_rcs=0 mounted=yes use_volume_errors=0\n", stderr="")
        result, answer = self.mount(claimed, answers=False)
        self.assertTrue(result)
        answer.assert_not_called()

    def test_a_hang_whose_syut_check_cannot_run_raises_the_hang(self) -> None:
        from timecapsulesmb.device import storage
        from timecapsulesmb.transport.errors import SshError

        timeout = SshCommandTimeout("Timed out waiting for ssh command to finish: /usr/bin/acp MaSt")
        lost = SshError("Connection reset by peer")
        with mock.patch("timecapsulesmb.device.storage.run_ssh", side_effect=timeout), \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", side_effect=lost):
            with self.assertRaises(SshCommandTimeout) as raised:
                storage.read_mast_volumes_with_output_conn(self.CONNECTION)
        self.assertIs(raised.exception, timeout)
        self.assertIs(raised.exception.__cause__, lost)

    def test_one_mast_wait_asks_acpd_once(self) -> None:
        with mock.patch("timecapsulesmb.device.storage.run_ssh", return_value=subprocess.CompletedProcess(
                ["ssh"], 0, stdout=self.FAILED_GET, stderr="")) as run, \
                mock.patch("timecapsulesmb.device.acpd.acpd_answers", return_value=True) as answer, \
                mock.patch("timecapsulesmb.device.storage.time.sleep"):
            result = wait_for_mast_volumes_conn(self.CONNECTION, attempts=10, delay_seconds=3)
        self.assertEqual(result.volumes, ())
        self.assertEqual(run.call_count, 10)
        answer.assert_called_once_with(self.CONNECTION)

    def test_a_single_failed_mast_read_asks_acpd_at_once(self) -> None:
        result, answer = self.read(self.FAILED_GET, answers=True)
        self.assertTrue(result.get_failed)
        answer.assert_called_once_with(self.CONNECTION)
