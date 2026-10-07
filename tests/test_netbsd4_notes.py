from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.build_wrapper_harness import REPO_ROOT, make_fake_elf_tools
from tests.executables import write_executable


TRIPLE = "arm--netbsdelf"

# The part of ld 2.16's evbarm default script that opens SECTIONS.
LD_216_SCRIPT = textwrap.dedent("""\
    OUTPUT_FORMAT("elf32-littlearm", "elf32-bigarm", "elf32-littlearm")
    SECTIONS
    {
      /* Read-only sections, merged into text segment: */
      PROVIDE (__executable_start = 0x8000); . = 0x8000 + SIZEOF_HEADERS;
      .interp         : { *(.interp) }
      .text           : { *(.text .text.*) }
    }
    """)


class NetBSD4NotesTests(unittest.TestCase):
    def run_helper(self, root: Path, command: str, **env_files: str) -> subprocess.CompletedProcess[str]:
        tools = root / "tools"
        make_fake_elf_tools(tools / "bin", TRIPLE)
        write_executable(tools / "bin" / f"{TRIPLE}-gcc", textwrap.dedent("""\
            #!/bin/sh
            printf '%s\\n' "$@" > "$TEST_GCC_ARGS"
            while [ "$#" -gt 1 ]; do [ "$1" = "-o" ] && out=$2; shift; done
            printf 'notes object\\n' > "$out"
            """))
        env = dict(os.environ, TOOLDIR=str(tools), TRIPLE=TRIPLE, TEST_GCC_ARGS=str(root / "gcc.args"))
        for name, text in env_files.items():
            path = root / name.lower()
            path.write_text(text)
            env[name] = str(path)
        return subprocess.run(["/bin/sh", "-c", f". build/_netbsd4_notes.sh; {command}"],
                              cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=False)

    def test_inputs_keep_both_notes_right_after_the_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"

            result = self.run_helper(root, f'netbsd4_keep_notes_inputs "{work}" && echo "$NETBSD4_KEEP_NOTES_LDFLAGS"',
                                     TEST_LD_SCRIPT=LD_216_SCRIPT)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            script = (work / "netbsd4-keep-notes.ld").read_text().splitlines()
            headers = next(i for i, line in enumerate(script) if "SIZEOF_HEADERS;" in line)
            self.assertEqual(script[headers + 1:headers + 3], [
                "  .note.netbsd.ident : { KEEP(*(.note.netbsd.ident)) }",
                "  .note.netbsd.pax : { KEEP(*(.note.netbsd.pax)) }",
            ])
            # Only the script between ld --verbose's "====" lines, unchanged otherwise.
            self.assertEqual(script[0], 'OUTPUT_FORMAT("elf32-littlearm", "elf32-bigarm", "elf32-littlearm")')
            self.assertNotIn("GNU ld version", "\n".join(script))
            self.assertIn("  .text           : { *(.text .text.*) }", script)
            # The notes object is assembled from the generated source.
            source = (work / "netbsd4-notes.S").read_text()
            self.assertIn(".section .note.netbsd.ident", source)
            self.assertIn(".long 0x17d78403", source)
            self.assertIn(".section .note.netbsd.pax", source)
            self.assertEqual((root / "gcc.args").read_text().splitlines(),
                             ["-c", str(work / "netbsd4-notes.S"), "-o", str(work / "netbsd4-notes.o")])
            self.assertEqual(result.stdout.strip(),
                             f"-Wl,--gc-sections -Wl,-T,{work}/netbsd4-keep-notes.ld {work}/netbsd4-notes.o")

    def test_inputs_refuse_a_default_script_without_the_headers_line(self) -> None:
        # Without the anchor the KEEP rules would silently go missing, and a
        # garbage-collected binary would lose its notes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_helper(root, f'netbsd4_keep_notes_inputs "{root / "work"}" && echo "set $NETBSD4_KEEP_NOTES_LDFLAGS"',
                                     TEST_LD_SCRIPT="SECTIONS\n{\n  .text : { *(.text) }\n}\n")

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("no SIZEOF_HEADERS line", result.stdout)
            self.assertNotIn("set ", result.stdout)

    def test_inputs_stop_when_the_notes_object_does_not_assemble(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            result = self.run_helper(root, f'TRIPLE=missing; netbsd4_keep_notes_inputs "{work}"; echo "rc=$?"')

            self.assertIn("rc=1", result.stdout)
            self.assertFalse((work / "netbsd4-keep-notes.ld").exists())

    def test_inputs_report_a_failing_linker_as_such(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            write_executable(root / "tools" / "bin" / f"{TRIPLE}-ld.real", "#!/bin/sh\nexit 1\n")
            result = self.run_helper(root, f'mv "$TOOLDIR/bin/{TRIPLE}-ld.real" "$TOOLDIR/bin/{TRIPLE}-ld"; '
                                           f'netbsd4_keep_notes_inputs "{work}"; echo "rc=$?"')

            self.assertIn(f"{TRIPLE}-ld --verbose failed", result.stdout)
            self.assertNotIn("SIZEOF_HEADERS", result.stdout)
            self.assertIn("rc=1", result.stdout)
            self.assertFalse((work / "netbsd4-keep-notes.ld").exists())

    def test_require_notes_accepts_a_binary_with_both(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_helper(Path(tmp), "netbsd4_require_notes service", TEST_OBJDUMP_HEADERS=(
                "Idx Name          Size      VMA       LMA       File off  Algn\n"
                "  0 .note.netbsd.ident 00000018  00008094  00008094  00000094  2**2\n"
                "  1 .note.netbsd.pax 00000014  000080ac  000080ac  000000ac  2**2\n"
                "  2 .text         00047000  000080c0  000080c0  000000c0  2**2\n"))

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("service: NetBSD note sections are present", result.stdout)

    def test_require_notes_rejects_a_binary_missing_either(self) -> None:
        for kept, missing in ((".note.netbsd.pax", ".note.netbsd.ident"),
                              (".note.netbsd.ident", ".note.netbsd.pax"),
                              (".text", ".note.netbsd.ident")):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                result = self.run_helper(Path(tmp), "netbsd4_require_notes service",
                                         TEST_OBJDUMP_HEADERS=f"  0 {kept} 00000014  000080ac\n")

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"service: missing {missing}", result.stdout)

    def test_require_notes_does_not_match_a_longer_section_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_helper(Path(tmp), "netbsd4_require_notes service", TEST_OBJDUMP_HEADERS=(
                "  0 .note.netbsd.ident.old 00000018\n  1 .note.netbsd.pax 00000014\n"))

            self.assertEqual(result.returncode, 1)
            self.assertIn("missing .note.netbsd.ident", result.stdout)


if __name__ == "__main__":
    unittest.main()
