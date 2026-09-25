from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.build_wrapper_harness import FAKE_ELF, REPO_ROOT, make_fake_elf_tools


TRIPLE = "armv4--netbsdelf-eabi"

# readelf 2.16 output for the NetBSD 4 LE service: .ctors instead of
# .init_array, absolute linker symbols, and "[ 8]"-style section numbers.
NETBSD4_ELF = {
    "load": (
        "  LOAD           0x000000 0x00008000 0x00008000 0x47c54 0x47c54 R E 0x8000\n"
        "  LOAD           0x047c54 0x00057c54 0x00057c54 0x015c8 0x0587c RW  0x8000\n"
    ),
    "sections": (
        "  [ 8] .ctors            PROGBITS        00057c54 047c54 000008 00  WA  0   0  4\n"
        "  [12] .data             PROGBITS        00057cac 047cac 001570 00  WA  0   0  4\n"
    ),
    "symbols": (
        "  1582: 000468ec     0 FUNC    GLOBAL DEFAULT    4 madvise\n"
        "  1584: 0005d4d0     0 NOTYPE  GLOBAL DEFAULT  ABS end\n"
        "  1896: 0005d4d0     0 NOTYPE  GLOBAL DEFAULT  ABS _end\n"
        "  1901: 00008300    48 FUNC    LOCAL  DEFAULT    4 disable_data_faultahead\n"
        "  2000: 00057c54     0 NOTYPE  GLOBAL DEFAULT  ABS __preinit_array_start\n"
    ),
    "disasm": "    8320:\teb00f971 \tbl\t468ec <_madvise>\n",
}


class DataFaultaheadCheckTests(unittest.TestCase):
    def check(self, function: str = "disable_data_faultahead", **overrides: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / "tools"
            make_fake_elf_tools(tools / "bin", TRIPLE)
            env = os.environ.copy()
            env.update({"TOOLDIR": str(tools), "TRIPLE": TRIPLE, "TEST_OBJDUMP_ARGS": str(root / "objdump.args")})
            for name, text in overrides.items():
                path = root / name
                path.write_text(text)
                env[{"disasm": "TEST_OBJDUMP_DISASM"}.get(name, f"TEST_READELF_{name.upper()}")] = str(path)
            result = subprocess.run(
                ["/bin/sh", "-c", '. build/_data_segment_check.sh; verify_data_faultahead "$0" "$1"', "smbd", function],
                cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=False,
            )
            args_path = root / "objdump.args"
            return result, args_path.read_text().splitlines() if args_path.exists() else []

    def symbols_without(self, name: str) -> str:
        return "".join(line + "\n" for line in FAKE_ELF["symbols"].splitlines() if not line.endswith(" " + name))

    def assert_rejected(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(reason, result.stdout)

    def test_netbsd6_layout_passes_and_disassembles_only_the_constructor(self) -> None:
        result, objdump_args = self.check()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("disable_data_faultahead turns off fault-ahead for 0x000752b0..0x0007a328", result.stdout)
        # disable_data_faultahead is 60 bytes at 0x10100.
        self.assertEqual(objdump_args, ["-d", "--start-address=0x00010100", "--stop-address=0x1013c", "smbd"])

    def test_netbsd4_layout_with_absolute_symbols_and_libc_alias_passes(self) -> None:
        result, objdump_args = self.check(**NETBSD4_ELF)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(objdump_args[1:3], ["--start-address=0x00008300", "--stop-address=0x8330"])

    def test_each_required_symbol_and_section_must_exist(self) -> None:
        for name in ("__preinit_array_start", "end", "disable_data_faultahead"):
            with self.subTest(name=name):
                result, objdump_args = self.check(symbols=self.symbols_without(name))
                self.assert_rejected(result, f"missing {name}")
                self.assertEqual(objdump_args, [])
        result, _ = self.check(sections=FAKE_ELF["sections"].splitlines()[0] + "\n")
        self.assert_rejected(result, "missing .data")

    def test_function_named_by_the_caller_must_be_a_function(self) -> None:
        result, _ = self.check(function="tc_missing_constructor")
        self.assert_rejected(result, "missing tc_missing_constructor")
        not_code = FAKE_ELF["symbols"].replace("FUNC    LOCAL  DEFAULT    4 disable_", "OBJECT  LOCAL  DEFAULT    4 disable_")
        result, _ = self.check(symbols=not_code)
        self.assert_rejected(result, "missing disable_data_faultahead")

    def test_end_must_close_the_writable_segment(self) -> None:
        # The NetBSD 4 migrator's _end pointed into the text segment; an "end"
        # like that, or one short of .bss, would leave pages with fault-ahead.
        for wrong in ("000100b4", "0007a000"):
            with self.subTest(end=wrong):
                symbols = FAKE_ELF["symbols"].replace("0007a328     0 NOTYPE  GLOBAL DEFAULT   11 end\n",
                                                      f"{wrong}     0 NOTYPE  GLOBAL DEFAULT   11 end\n")
                result, _ = self.check(symbols=symbols)
                self.assert_rejected(result, f"end 0x{wrong} does not close the writable segment (0x7a328)")

    def test_range_start_must_lie_between_segment_start_and_data(self) -> None:
        for first in ("00075300", "00070000"):
            with self.subTest(first=first):
                symbols = FAKE_ELF["symbols"].replace("000752b0", first)
                result, _ = self.check(symbols=symbols)
                self.assert_rejected(result, f"__preinit_array_start 0x{first} is not between")
        # Equal to the segment start and to .data are both inside the range.
        for first in ("00070364", "000752f8"):
            with self.subTest(first=first):
                result, _ = self.check(symbols=FAKE_ELF["symbols"].replace("000752b0", first))
                self.assertEqual(result.returncode, 0, result.stdout)

    def test_exactly_one_writable_segment_is_required(self) -> None:
        text_only = FAKE_ELF["load"].splitlines()[0] + "\n"
        for load in (text_only, FAKE_ELF["load"] + FAKE_ELF["load"].splitlines()[1] + "\n"):
            with self.subTest(load=load):
                result, _ = self.check(load=load)
                self.assert_rejected(result, "expected one writable PT_LOAD segment")

    def test_constructor_must_call_madvise(self) -> None:
        for disasm in ("", "   10130:\tebffb52e \tbl\t3d5f0 <posix_madvise>\n", "   10130:\tebffb52e \tbl\t3d5f0 <mprotect>\n"):
            with self.subTest(disasm=disasm):
                result, _ = self.check(disasm=disasm)
                self.assert_rejected(result, "disable_data_faultahead does not call madvise")


if __name__ == "__main__":
    unittest.main()
