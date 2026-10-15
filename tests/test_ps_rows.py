"""ps_rows lists the device's processes without reading every command line.

Reading a process's command line waits for that process's memory map lock,
which a process asleep in fork() or msync() can hold while the disk keeps it
waiting. The function reads the process table alone and then reads command
lines one PID at a time, for the processes whose role depends on them.
"""
import os
import subprocess

import pytest

from tests.device_ps import FakePs, process
from timecapsulesmb.device.processes import (
    UNREADABLE_COMMAND,
    describe_uninterruptible,
    ps_rows_command,
    render_ps_rows,
    render_process_present_by_ucomm,
)

# Ubuntu CI runs these scripts with dash; macOS has both.
SHELLS = [shell for shell in ("/bin/sh", "/bin/dash") if os.path.exists(shell)]
FUNCTION = render_ps_rows("rows", ("pid", "ppid", "stat"), ("service", "tc-xattr-hfs-mi*"))


def run_rows(tmp_path, shell, processes, *, prefix="", suffix="", **fake_options):
    fake = FakePs(tmp_path, processes, **fake_options)
    sleeps = tmp_path / "sleeps"
    script = fake.install(FUNCTION).replace("sleep 1", f"echo slept >> {sleeps}")
    result = subprocess.run([shell, "-c", f"{script}\n{prefix}rows{suffix}"], capture_output=True, text=True)
    slept = len(sleeps.read_text().splitlines()) if sleeps.exists() else 0
    return result, fake, slept


def rows_by_pid(output):
    return {line.split()[0]: line.split() for line in output.splitlines()}


@pytest.mark.parametrize("shell", SHELLS)
def test_reads_command_lines_only_for_selected_processes(tmp_path, shell):
    result, fake, slept = run_rows(tmp_path, shell, [
        process("10 S service service: role=manager started=12"),
        process("11 S smbd /mnt/Memory/samba4/sbin/smbd -F"),
        process("12 S tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate multi copy --log /x"),
        process("13 S sh /bin/sh /mnt/Flash/boot.sh"),
    ])

    assert result.returncode == 0, result.stderr
    rows = rows_by_pid(result.stdout)
    assert rows["10"] == ["10", "1", "S", "service", "service:", "role=manager", "started=12"]
    assert rows["11"] == ["11", "1", "S", "smbd"]
    assert rows["12"][4:] == ["/mnt/Memory/tc-xattr-hfs-migrate", "multi", "copy", "--log", "/x"]
    assert rows["13"] == ["13", "1", "S", "sh"]
    assert fake.command_reads() == ["10", "12"]
    assert slept == 0


@pytest.mark.parametrize("shell", SHELLS)
def test_a_process_asleep_in_the_kernel_is_looked_at_again_but_never_read(tmp_path, shell):
    # One look a second later for every D row together: a brief disk wait is
    # read as usual, one that lasts may hold the map lock and stays unread.
    result, fake, slept = run_rows(tmp_path, shell, [
        process("10 D service service: role=manager", stat_later="D"),
        process("11 D service service: role=discovery", stat_later="S"),
        process("12 D smbd /mnt/Memory/samba4/sbin/smbd -F"),
        process("13 D service service: role=job", stat_later="DE"),
    ])

    assert result.returncode == 0, result.stderr
    rows = rows_by_pid(result.stdout)
    assert rows["10"][4:] == [UNREADABLE_COMMAND]
    assert rows["11"][4:] == ["service:", "role=discovery"]
    assert rows["12"] == ["12", "1", "D", "smbd"]
    assert rows["13"][4:] == [UNREADABLE_COMMAND]
    assert fake.command_reads() == ["11"]
    assert slept == 1


@pytest.mark.parametrize("shell", SHELLS)
def test_a_process_gone_since_the_listing_is_left_out(tmp_path, shell):
    result, fake, _ = run_rows(tmp_path, shell, [
        process("10 S service service: role=manager", argv="vanish"),
        process("11 S service service: role=discovery"),
    ])

    assert result.returncode == 0, result.stderr
    assert list(rows_by_pid(result.stdout)) == ["11"]


@pytest.mark.parametrize("shell", SHELLS)
def test_a_sleeping_process_gone_before_its_second_look_is_left_out(tmp_path, shell):
    fake = FakePs(tmp_path, [process("10 D service service: role=manager")])
    # The process exits during the one-second wait.
    script = fake.install(FUNCTION).replace(
        "sleep 1", f"{fake.command('kill')} -KILL 10"
    )
    result = subprocess.run([shell, "-c", f"{script}\nrows"], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert fake.command_reads() == []


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("extra", [{"argv": "unreadable"}, {"ucomm_later": "smbd"}])
def test_a_live_process_without_a_readable_command_line_is_kept_as_unreadable(tmp_path, shell, extra):
    # ps prints "(ucomm)" when it cannot read the command line; a process that
    # ran another program since the listing is no longer the listed one.
    result, _, _ = run_rows(tmp_path, shell, [process("10 S service service: role=manager", **extra)])

    assert result.returncode == 0, result.stderr
    assert rows_by_pid(result.stdout)["10"] == ["10", "1", "S", "service", UNREADABLE_COMMAND]


@pytest.mark.parametrize("shell", SHELLS)
def test_zombies_and_short_rows_pass_through_unread(tmp_path, shell):
    result, fake, _ = run_rows(tmp_path, shell, [
        process("10 Z service service: role=manager"),
        process("11 S service service: role=discovery"),
    ])

    assert result.returncode == 0, result.stderr
    assert rows_by_pid(result.stdout)["10"] == ["10", "1", "Z", "service"]
    assert fake.command_reads() == ["11"]


@pytest.mark.parametrize("shell", SHELLS)
def test_a_failed_listing_fails_the_function(tmp_path, shell):
    result, fake, _ = run_rows(tmp_path, shell, [process("10 S service x")], list_fails=True)

    assert result.returncode == 1
    assert result.stdout == ""
    assert fake.command_reads() == []


@pytest.mark.parametrize("shell", SHELLS)
def test_command_lines_are_printed_verbatim_whatever_the_callers_ifs_and_globbing(tmp_path, shell):
    # A command line's glob characters stay as they are, and the function
    # leaves the caller's options and IFS alone.
    (tmp_path / "match-me").write_text("")
    command = f"/mnt/Flash/service telemetry --daemon {tmp_path}/*"
    result, _, _ = run_rows(
        tmp_path, shell, [process(f"10 S service {command}")],
        prefix="IFS=:\n",
        suffix='\ncase "$-" in *f*) echo noglob-leaked ;; esac\n[ "$IFS" = : ] || echo ifs-changed',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"   10     1 S service {command}"]


def test_ps_rows_command_runs_the_function_over_ssh(tmp_path):
    fake = FakePs(tmp_path, [process("10 S service service: role=manager")])
    command = ps_rows_command("rows", ("pid", "stat"), ("service",))
    result = subprocess.run(fake.install(command), shell=True, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["10", "S", "service", "service:", "role=manager"]


def test_describe_uninterruptible_names_the_sleepers_but_not_kernel_threads():
    listing = "\n".join([
        "  812   700 D    vmmapbsy ps",
        "  700     1 D    biowait  service",
        "    3     0 DK   pgdaemon pgdaemon",
        "  701     1 S    select   smbd",
        "garbage",
    ])
    assert describe_uninterruptible(listing) == "ps (pid 812) on vmmapbsy, service (pid 700) on biowait"
    assert describe_uninterruptible("") == ""


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("rows,list_fails,present", [
    (["10 S smbd /x"], False, True),
    (["10 Z smbd /x", "11 S smbdx /x"], False, False),
    ([], False, False),
    # A listing that fails proves nothing: keep treating the process as present.
    (["10 S smbd /x"], True, True),
])
def test_process_present_by_ucomm_reads_no_command_lines(tmp_path, shell, rows, list_fails, present):
    fake = FakePs(tmp_path, [process(row) for row in rows], list_fails=list_fails)
    result = subprocess.run([shell, "-c", fake.install(render_process_present_by_ucomm("smbd"))], capture_output=True)

    assert (result.returncode == 0) == present
    assert fake.command_reads() == []
    assert not list(tmp_path.glob("tcapsule-ps.*"))
