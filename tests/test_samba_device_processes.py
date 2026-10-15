"""The device test tools read the process table the way the runtime does."""
import subprocess

import pytest

from tests.device_ps import FakePs, process
from tests.samba import device_processes
from tests.samba.swap_smbd import smbd_parent

SMBD = "/mnt/Memory/samba4/sbin/smbd -F --no-process-group -s /mnt/Memory/samba4/etc/smb.conf"


def listing(tmp_path, processes):
    fake = FakePs(tmp_path, processes)
    result = subprocess.run(
        fake.install(device_processes.COMMAND).replace("sleep 1", ":"), shell=True, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, fake


def test_finds_the_manager_and_its_smbd_reading_only_service_command_lines(tmp_path):
    output, fake = listing(tmp_path, [
        process("132 S service service: role=manager started=4", pgid="2"),
        process("140 S service service: role=discovery nbns=ready", ppid="132", pgid="140"),
        process(f"21323 S smbd {SMBD}", ppid="132", pgid="21323"),
        process(f"22862 D smbd {SMBD}", ppid="21323", pgid="21323"),
        process("371 S mDNSResponder /sbin/mDNSResponder -d"),
        process("90 Z sh (sh)"),
    ])
    rows = device_processes.parse(output)

    # smbd forks per connection: its command line is never read.
    assert fake.command_reads() == ["132", "140"]
    assert [row.pid for row in rows] == [132, 140, 21323, 22862, 371]
    assert device_processes.manager(rows).pgid == 2
    samba = device_processes.samba_parent(rows)
    assert (samba.pid, samba.pgid, samba.argv) == (21323, 21323, ())
    assert smbd_parent(output) == 21323


@pytest.mark.parametrize("rows,message", [
    ([], "expected one manager, found 0"),
    (["132 S service service: role=manager", "133 S service service: role=manager"], "expected one manager, found 2"),
    (["132 S service service: role=manager"], "expected one smbd under the manager, found 0"),
    # An smbd that is not the manager's child is not the one it runs.
    (["132 S service service: role=manager", f"21323 S smbd {SMBD}"], "expected one smbd under the manager, found 0"),
])
def test_refuses_anything_but_one_manager_with_one_smbd(tmp_path, rows, message):
    output, _ = listing(tmp_path, [process(row) for row in rows])

    with pytest.raises(RuntimeError, match=message):
        smbd_parent(output)
