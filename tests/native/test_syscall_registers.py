"""`service --print-syscall-registers` reads the user registers a process
asleep in a system call entered the kernel with, from the trapframe at the top
of its u-area. Doctor runs it for each smbd asleep in "lockf"."""
import struct
import subprocess

import pytest

from tests.native.build import compile_service

USPACE = 8192


@pytest.fixture(scope="module")
def netbsd6(tmp_path_factory):
    return compile_service(tmp_path_factory.mktemp("regs6") / "service",
                           flags=["-UTC_NATIVE_TEST", "-DTC_TRAPFRAME_FILL=1"])


@pytest.fixture(scope="module")
def netbsd4(tmp_path_factory):
    return compile_service(tmp_path_factory.mktemp("regs4") / "service",
                           flags=["-UTC_NATIVE_TEST", "-DTC_TRAPFRAME_FILL=0"])


def frame(*, spsr, registers, pc, fill):
    """struct trapframe: spsr, (fill), r0-r12, usr_sp, usr_lr, svc_sp, svc_lr, pc."""
    words = [spsr] + ([0xF111F111] if fill else []) + list(registers) + [0] * (13 - len(registers))
    words += [0x5555AAA0, 0x5555AAA4, 0xC0DE0000, 0xC0DE0004, pc]
    return struct.pack(f"={len(words)}I", *words)


def kernel(tmp_path, frames, *, wchans=None):
    """A stand-in for /dev/kmem with each frame at the top of its u-area, and
    the PID to u-area and wait channel table KERN_PROC2 would give. wchans
    maps a PID to its wait channel on the first and second look ("lockf"
    both times by default)."""
    kmem = tmp_path / "kmem"
    uareas = tmp_path / "uareas"
    rows = []
    with open(kmem, "wb") as out:
        for index, (pid, data) in enumerate(frames):
            uarea = 0x10000 + index * USPACE
            first, second = (wchans or {}).get(pid, ("lockf", "lockf"))
            rows.append(f"{pid} {uarea:x} {first} {second}\n")
            out.seek(uarea + USPACE - len(data))
            out.write(data)
        # Garbage above the last u-area: a reader that went past the frame
        # would pick it up.
        out.write(b"\xEE" * 64)
    uareas.write_text("".join(rows))
    return {"TC_TEST_KMEM": str(kmem), "TC_TEST_UAREAS": str(uareas)}


def run(binary, env, *pids):
    return subprocess.run([str(binary), "--print-syscall-registers", *map(str, pids)], capture_output=True,
                          text=True, env=env, timeout=10)


def test_netbsd6_frame_reads_the_registers_after_the_pad_word(netbsd6, tmp_path):
    # flock(12, LOCK_EX) and fcntl(7, F_SETLKW, ...), both entered from user mode.
    env = kernel(tmp_path, [
        (7652, frame(spsr=0x60000010, registers=[12, 2, 0xBEEF0000, 0x11], pc=0x0012ABCD, fill=True)),
        (1894, frame(spsr=0x20000010, registers=[7, 9, 0x7FFFD000], pc=0x00034560, fill=True)),
    ])

    result = run(netbsd6, env, 7652, 1894)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "7652 wchan=lockf spsr=60000010 r0=0000000c r1=00000002 r2=beef0000 r3=00000011 pc=0012abcd",
        "1894 wchan=lockf spsr=20000010 r0=00000007 r1=00000009 r2=7fffd000 r3=00000000 pc=00034560",
    ]


def test_netbsd4_frame_has_no_pad_word(netbsd4, tmp_path):
    env = kernel(tmp_path, [(626, frame(spsr=0x10, registers=[377, 12, 0x1000, 0x2000], pc=0x8000, fill=False))])

    result = run(netbsd4, env, 626)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "626 wchan=lockf spsr=00000010 r0=00000179 r1=0000000c r2=00001000 r3=00002000 pc=00008000\n"


def test_a_frame_read_with_the_other_lanes_layout_takes_the_pad_word_for_spsr(netbsd4, tmp_path):
    # The frame ends at the top of the u-area in both layouts, so the pad only
    # moves spsr: a NetBSD 6 frame read as NetBSD 4's keeps r0-r3 and pc and
    # reports the pad as spsr. Doctor's user-mode check rejects this pad value;
    # the other mistake reads a kernel stack word as spsr, which it need not.
    env = kernel(tmp_path, [(7652, frame(spsr=0x60000010, registers=[12, 2], pc=0x1234, fill=True))])

    result = run(netbsd4, env, 7652)

    assert result.stdout == "7652 wchan=lockf spsr=f111f111 r0=0000000c r1=00000002 r2=00000000 r3=00000000 pc=00001234\n"


def test_a_process_whose_sleep_changed_during_the_read_is_not_reported(netbsd6, tmp_path):
    # 1894 woke from lockf and went back to sleep in poll between the two
    # looks: its frame now holds poll's arguments, so none are printed.
    env = kernel(tmp_path, [
        (1894, frame(spsr=0x10, registers=[0x20000, 2, 1000], pc=0x1234, fill=True)),
        (7652, frame(spsr=0x10, registers=[12, 2], pc=0x1234, fill=True)),
    ], wchans={1894: ("lockf", "poll")})

    result = run(netbsd6, env, 1894, 7652)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "1894: changed during the read",
        "7652 wchan=lockf spsr=00000010 r0=0000000c r1=00000002 r2=00000000 r3=00000000 pc=00001234",
    ]


def test_a_process_asleep_elsewhere_is_printed_with_its_wait_channel(netbsd6, tmp_path):
    # Steady in poll on both looks: its registers are right for poll, and the
    # wait channel says so; deciding what they mean is the reader's job.
    env = kernel(tmp_path, [(1894, frame(spsr=0x10, registers=[0x20000, 2, 1000], pc=0x1234, fill=True))],
                 wchans={1894: ("poll", "poll")})

    result = run(netbsd6, env, 1894)

    assert result.stdout == "1894 wchan=poll spsr=00000010 r0=00020000 r1=00000002 r2=000003e8 r3=00000000 pc=00001234\n"


def test_a_missing_process_or_bad_argument_fails_but_the_others_are_still_read(netbsd6, tmp_path):
    env = kernel(tmp_path, [(7652, frame(spsr=0x10, registers=[12, 2], pc=0x1234, fill=True))])

    result = run(netbsd6, env, 9999, "smbd", 0, 99999999999, 7652)

    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert lines[0].startswith("9999: ") and lines[0] != "9999: short read"
    assert lines[1:] == [
        "smbd: not a PID",
        "0: not a PID",
        # Past pid_t: it would otherwise wrap round to another process.
        "99999999999: not a PID",
        "7652 wchan=lockf spsr=00000010 r0=0000000c r1=00000002 r2=00000000 r3=00000000 pc=00001234",
    ]


def test_a_u_area_past_the_end_of_kernel_memory_is_a_short_read(netbsd6, tmp_path):
    env = kernel(tmp_path, [(7652, frame(spsr=0x10, registers=[12, 2], pc=0x1234, fill=True))])
    (tmp_path / "uareas").write_text("7652 10000 lockf\n8000 900000 lockf\n")

    result = run(netbsd6, env, 8000, 7652)

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "8000: short read",
        "7652 wchan=lockf spsr=00000010 r0=0000000c r1=00000002 r2=00000000 r3=00000000 pc=00001234",
    ]


def test_unreadable_kernel_memory_reads_no_process(netbsd6, tmp_path):
    env = {"TC_TEST_KMEM": str(tmp_path / "missing"), "TC_TEST_UAREAS": str(tmp_path / "missing")}

    result = run(netbsd6, env, 7652)

    assert result.returncode == 1
    assert result.stdout.startswith(f"{tmp_path / 'missing'}: ")
    assert "7652" not in result.stdout
