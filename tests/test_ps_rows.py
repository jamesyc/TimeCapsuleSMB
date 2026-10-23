"""ps_rows lists the device's processes without reading every command line.

Reading a process's command line waits for that process's memory map lock,
which a process asleep in fork() or msync() can hold while the disk keeps it
waiting. The function reads the process table alone and then reads command
lines one PID at a time, for the processes whose role depends on them.
"""
import os
import subprocess

import pytest

from tests.device_ps import FakePs, process, ps_row
from timecapsulesmb.device.processes import (
    PROCESS_SNAPSHOT_COMMAND,
    PS_LISTING_END,
    PS_ROWS_FUNCTION,
    UNREADABLE_COMMAND,
    Process,
    acpd_running,
    listing_complete,
    describe_uninterruptible,
    parse_ps_rows,
    ps_rows_command,
    render_process_present_by_ucomm,
    smbd_running,
)

# Ubuntu CI runs these scripts with dash; macOS has both.
SHELLS = [shell for shell in ("/bin/sh", "/bin/dash") if os.path.exists(shell)]
# A name, and a prefix pattern as the migrators' truncated names need.
CALL = "ps_rows service 'tc-xattr-hfs-mi*'"


def run_rows(tmp_path, shell, processes, *, prefix="", suffix="", **fake_options):
    fake = FakePs(tmp_path, processes, **fake_options)
    sleeps = tmp_path / "sleeps"
    script = fake.install(PS_ROWS_FUNCTION).replace("sleep 1", f"echo slept >> {sleeps}")
    result = subprocess.run([shell, "-c", f"{script}{prefix}{CALL}{suffix}"], capture_output=True, text=True)
    slept = len(sleeps.read_text().splitlines()) if sleeps.exists() else 0
    return result, fake, slept


def rows_by_pid(output):
    rows = {process.pid: process for process in parse_ps_rows(output)}
    # Every line is a row.
    assert len(rows) == len(output.splitlines()), output
    return rows


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
    assert rows[10] == Process(10, 1, 10, "S", 0, "-", "0:00.00", "service", ("service:", "role=manager", "started=12"))
    assert rows[11] == Process(11, 1, 11, "S", 0, "-", "0:00.00", "smbd")
    assert rows[12].argv == ("/mnt/Memory/tc-xattr-hfs-migrate", "multi", "copy", "--log", "/x")
    assert rows[13].argv == ()
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
    assert rows[10].unreadable
    assert rows[11].argv == ("service:", "role=discovery")
    assert (rows[12].stat, rows[12].argv) == ("D", ())
    assert rows[13].unreadable
    assert fake.command_reads() == ["11"]
    assert slept == 1


@pytest.mark.parametrize("shell", SHELLS)
def test_a_process_gone_since_the_listing_is_left_out(tmp_path, shell):
    result, fake, _ = run_rows(tmp_path, shell, [
        process("10 S service service: role=manager", argv="vanish"),
        process("11 S service service: role=discovery"),
    ])

    assert result.returncode == 0, result.stderr
    assert list(rows_by_pid(result.stdout)) == [11]


@pytest.mark.parametrize("shell", SHELLS)
def test_a_sleeping_process_gone_before_its_second_look_is_left_out(tmp_path, shell):
    fake = FakePs(tmp_path, [process("10 D service service: role=manager")])
    # The process exits during the one-second wait.
    script = fake.install(PS_ROWS_FUNCTION).replace(
        "sleep 1", f"{fake.command('kill')} -KILL 10"
    )
    result = subprocess.run([shell, "-c", f"{script}{CALL}"], capture_output=True, text=True)

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
    assert rows_by_pid(result.stdout)[10].argv == (UNREADABLE_COMMAND,)


@pytest.mark.parametrize("shell", SHELLS)
def test_zombies_and_short_rows_pass_through_unread(tmp_path, shell):
    result, fake, _ = run_rows(tmp_path, shell, [
        process("10 Z service service: role=manager"),
        process("11 S service service: role=discovery"),
    ])

    assert result.returncode == 0, result.stderr
    assert (rows_by_pid(result.stdout)[10].stat, rows_by_pid(result.stdout)[10].argv) == ("Z", ())
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
    assert result.stdout.splitlines() == [f"   10     1    10 S 0 - 0:00.00 service {command}"]


def test_ps_rows_command_passes_its_patterns_unexpanded(tmp_path):
    # The login shell must not expand a pattern against its directory first.
    work = tmp_path / "cwd"
    work.mkdir()
    (work / "tc-xattr-hfs-migrate.log").write_text("")
    fake = FakePs(tmp_path, [process("10 S tc-xattr-hfs-mi /mnt/Memory/tc-xattr-hfs-migrate multi copy"),
                             process("11 S sh /bin/sh /x")])
    command = ps_rows_command("tc-xattr-hfs-mi*")
    result = subprocess.run(fake.install(command), shell=True, capture_output=True, text=True, cwd=work)

    assert result.returncode == 0, result.stderr
    assert [(p.pid, p.argv) for p in parse_ps_rows(result.stdout)] == [
        (10, ("/mnt/Memory/tc-xattr-hfs-migrate", "multi", "copy")), (11, ())]


def test_parse_ps_rows_reads_the_layout_and_leaves_out_what_is_not_a_row():
    output = "\n".join([
        ps_row(10, "service", "service: role=manager", ppid=1, pgid=2, stat="Ss", sl=3, wchan="select", time="0:01.50"),
        ps_row(11, "smbd", stat="Z"),
        "ps: kvm_getprocs: Cannot allocate memory",
        "  12 1 12 S 0 select 0:00.00",       # no ucomm
        "  x 1 1 S 0 select 0:00.00 sh",       # no pid
        "  13 1 13 S soon select 0:00.00 sh",  # no sleep time
    ])

    assert parse_ps_rows(output) == [
        Process(10, 1, 2, "Ss", 3, "select", "0:01.50", "service", ("service:", "role=manager")),
        Process(11, 1, 11, "Z", 0, "-", "0:00.00", "smbd"),
    ]
    assert parse_ps_rows(output)[1].zombie and not parse_ps_rows(output)[0].zombie


def test_describe_uninterruptible_names_the_sleepers_but_not_kernel_threads():
    listing = "\n".join([
        "  812 700 812 D 127 vmmapbsy 0:00.00 ps",
        "  700 1 700 D 127 biowait 0:00.00 service",
        "  3 0 3 DK 127 pgdaemon 0:00.00 pgdaemon",
        "  701 1 701 S 127 select 0:00.00 smbd",
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


# The listing that can say ACPd is gone ends with PS_LISTING_END, and only
# once ps_rows printed every row.
def test_a_finished_listing_ends_with_its_end_line(tmp_path):
    fake = FakePs(tmp_path, [process("190 S ACPd /sbin/ACPd -nofork"),
                             process("300 S service service: role=manager started=24")])
    result = subprocess.run(fake.install(PROCESS_SNAPSHOT_COMMAND), shell=True, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == PS_LISTING_END
    assert listing_complete(result.stdout)
    assert acpd_running(result.stdout) is True
    assert [p.ucomm for p in parse_ps_rows(result.stdout)] == ["ACPd", "service"]


def test_a_finished_listing_without_acpd_says_it_quit(tmp_path):
    fake = FakePs(tmp_path, [process("107 S sshd /usr/sbin/sshd"),
                             process("300 S service service: role=manager started=24")])
    result = subprocess.run(fake.install(PROCESS_SNAPSHOT_COMMAND), shell=True, capture_output=True, text=True)

    assert acpd_running(result.stdout) is False


def test_a_listing_ps_could_not_make_has_no_end_line_and_says_nothing(tmp_path):
    fake = FakePs(tmp_path, [process("107 S sshd /usr/sbin/sshd")], list_fails=True)
    result = subprocess.run(fake.install(PROCESS_SNAPSHOT_COMMAND), shell=True, capture_output=True, text=True)

    assert PS_LISTING_END not in result.stdout
    assert acpd_running(result.stdout) is None
    # It fails as ps did, so a caller reads it as unread, not as no verdicts.
    assert result.returncode != 0


def test_only_a_last_end_line_makes_a_listing_complete():
    acpd = ps_row(190, "ACPd")
    assert listing_complete(f"{acpd}\n{PS_LISTING_END}\n\n")
    assert not listing_complete(f"{PS_LISTING_END}\n{acpd}\n")  # rows after it: not this listing's end
    assert not listing_complete(acpd)
    assert not listing_complete("")
    # A cut-short listing never reads as ACPd gone.
    assert acpd_running(ps_row(107, "sshd")) is None


def test_only_a_listing_that_shows_this_logins_sshd_says_acpd_quit():
    # The sshd serving the listing is always in it: rows that did not parse
    # as such (another layout, an error line, none at all) say nothing.
    sshd = ps_row(107, "sshd")
    assert acpd_running(f"{sshd}\n{PS_LISTING_END}") is False
    assert acpd_running(f"{sshd}\n{ps_row(190, 'ACPd')}\n{PS_LISTING_END}") is True
    for unparsed in (
        PS_LISTING_END,
        f"ps: kvm_getprocs: Cannot allocate memory\n{PS_LISTING_END}",
        f"190 ACPd Sa\n107 sshd S\n{PS_LISTING_END}",  # a layout without PS_COLUMNS
        f"{ps_row(107, 'sshd', stat='Z')}\n{PS_LISTING_END}",  # only a zombie sshd
    ):
        assert acpd_running(unparsed) is None, unparsed


def test_a_listing_taken_during_a_shutdown_never_says_acpd_quit():
    # A real restart's shutdown stops ACPd before sshd, its last step.
    sshd = ps_row(107, "sshd")
    for ucomm in ("shutdown", "reboot"):
        assert acpd_running(f"{sshd}\n{ps_row(901, ucomm)}\n{PS_LISTING_END}") is None, ucomm
    # ACPd still listed: it runs, shutdown or not.
    assert acpd_running(f"{sshd}\n{ps_row(190, 'ACPd')}\n{ps_row(901, 'shutdown')}\n{PS_LISTING_END}") is True


def test_smbd_running_says_whether_samba_outlived_acpd_by_the_same_rules():
    # Samba is the manager's child, so it can outlive ACPd: only a listing
    # that finished and shows this login's sshd can say it is gone.
    sshd = ps_row(107, "sshd")
    assert smbd_running(f"{sshd}\n{ps_row(320, 'smbd')}\n{PS_LISTING_END}") is True
    assert smbd_running(f"{sshd}\n{PS_LISTING_END}") is False
    assert smbd_running(f"{sshd}\n{ps_row(320, 'smbd', stat='Z')}\n{PS_LISTING_END}") is False
    assert smbd_running(f"{sshd}\n{ps_row(320, 'smbd')}") is None  # cut short
    assert smbd_running(f"ps: kvm_getprocs: Cannot allocate memory\n{PS_LISTING_END}") is None

