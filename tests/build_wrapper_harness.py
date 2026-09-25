from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

# readelf/objdump output for a NetBSD 6 service link that
# build/_data_segment_check.sh accepts: __preinit_array_start opens the one
# writable segment before .data, "end" closes it, and both fault-ahead
# constructor names call madvise.
FAKE_ELF = {
    "load": (
        "  LOAD           0x000000 0x00010000 0x00010000 0x50364 0x50364 R E 0x10000\n"
        "  LOAD           0x050364 0x00070364 0x00070364 0x05a08 0x09fc4 RW  0x10000\n"
    ),
    "sections": (
        "  [ 7] .init_array       INIT_ARRAY      000752b0 0552b0 000008 00  WA  0   0  4\n"
        "  [10] .data             PROGBITS        000752f8 0552f8 000a74 00  WA  0   0  8\n"
    ),
    "symbols": (
        "   993: 000752b0     0 NOTYPE  LOCAL  DEFAULT    7 __preinit_array_start\n"
        "   994: 00010100    60 FUNC    LOCAL  DEFAULT    4 disable_data_faultahead\n"
        "   995: 00010200    60 FUNC    LOCAL  DEFAULT    4 tc_disable_data_faultahead\n"
        "  2192: 0003d5f0    12 FUNC    GLOBAL DEFAULT    4 madvise\n"
        "  2195: 0007a328     0 NOTYPE  GLOBAL DEFAULT   11 end\n"
        "  2573: 0007a328     0 NOTYPE  GLOBAL DEFAULT   11 _end\n"
    ),
    "disasm": "   10130:\tebffb52e \tbl\t3d5f0 <madvise>\n",
}


def make_fake_elf_tools(tools: Path, triple: str) -> None:
    """readelf/objdump stubs; TEST_READELF_{LOAD,SECTIONS,SYMBOLS} and
    TEST_OBJDUMP_DISASM name files that replace the FAKE_ELF defaults."""
    data = tools / "fake-elf"
    data.mkdir(parents=True, exist_ok=True)
    for name, text in FAKE_ELF.items():
        (data / name).write_text(text)
    for name, script in {
        "readelf": """\
            #!/bin/sh
            d="$(dirname "$0")/fake-elf"
            case "${1:-}" in
                -lW) cat "${TEST_READELF_LOAD:-$d/load}" ;;
                -S*) cat "${TEST_READELF_SECTIONS:-$d/sections}" ;;
                -sW) cat "${TEST_READELF_SYMBOLS:-$d/symbols}" ;;
            esac
            """,
        "objdump": """\
            #!/bin/sh
            d="$(dirname "$0")/fake-elf"
            case "${1:-}" in
                -h)
                    printf '  1 .note.netbsd.ident 00000000\\n'
                    printf '  2 .note.netbsd.pax 00000000\\n'
                    ;;
                -p) printf 'Program Header:\\n' ;;
                -d)
                    if [ -n "${TEST_OBJDUMP_ARGS:-}" ]; then
                        printf '%s\\n' "$@" >> "$TEST_OBJDUMP_ARGS"
                    fi
                    cat "${TEST_OBJDUMP_DISASM:-$d/disasm}"
                    ;;
            esac
            """,
    }.items():
        path = tools / f"{triple}-{name}"
        path.write_text(textwrap.dedent(script))
        path.chmod(0o755)


class BuildWrapperHarness:
    def make_executable(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def prepare_fake_toolchain(self, out: Path, triple: str) -> None:
        tools = out / "tools" / "bin"
        sysroot = out / "obj" / "destdir.evbarm"
        sysroot.mkdir(parents=True, exist_ok=True)
        self.make_executable(tools / "nbmake", "#!/bin/sh\nexit 0\n")
        self.make_executable(tools / "nbfile", "#!/bin/sh\nprintf '%s: fake ELF\\n' \"$1\"\n")
        self.make_executable(
            tools / f"{triple}-gcc",
            textwrap.dedent(
                """\
                #!/bin/sh
                printf '%s\\n' "$@" > "$TEST_GCC_ARGS"
                out=
                while [ "$#" -gt 0 ]; do
                    if [ "$1" = "-o" ]; then
                        shift
                        out="$1"
                    fi
                    shift || break
                done
                mkdir -p "$(dirname "$out")"
                printf 'fake service\\n' > "$out"
                """
            ),
        )
        self.make_executable(
            tools / f"{triple}-strip",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$TEST_STRIP_ARGS\"\n",
        )
        make_fake_elf_tools(tools, triple)

    def env_for(self, root: Path, *, triple: str) -> tuple[dict[str, str], Path, Path, Path]:
        out = root / "out"
        stage = root / "stage"
        log = root / "service.log"
        gcc_args = root / "gcc.args"
        strip_args = root / "strip.args"
        self.prepare_fake_toolchain(out, triple)
        env = os.environ.copy()
        env.update({
            "TC_ENV_FILE": "/dev/null",
            "BUILD_OUT": str(out),
            "BUILD_SRC": str(root / "src"),
            "SERVICE_STAGE": str(stage),
            "SERVICE_LOG": str(log),
            "TEST_GCC_ARGS": str(gcc_args),
            "TEST_STRIP_ARGS": str(strip_args),
        })
        return env, log, gcc_args, strip_args

    def run_wrapper(self, wrapper: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(REPO_ROOT / "build" / wrapper)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
