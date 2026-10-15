"""The device's process table for the device test tools, read the way the
runtime reads it (timecapsulesmb.device.processes.ps_rows): only service
processes' command lines, for the manager's title. smbd's are never read: its
parent forks a child per connection, and a test under load can leave either
asleep in the kernel holding its memory map, which would hang the listing."""
from __future__ import annotations

from timecapsulesmb.device.processes import Process, parse_ps_rows, ps_rows_command

COMMAND = ps_rows_command("service")


def parse(output: str) -> list[Process]:
    """COMMAND's rows, zombies left out."""
    return [row for row in parse_ps_rows(output) if not row.zombie]


def manager(rows: list[Process]) -> Process:
    managers = [row for row in rows if row.ucomm == "service" and row.argv[:2] == ("service:", "role=manager")]
    if len(managers) != 1:
        raise RuntimeError(f"expected one manager, found {len(managers)}")
    return managers[0]


def samba_parent(rows: list[Process]) -> Process:
    """The smbd the manager runs (its direct child)."""
    parent = manager(rows).pid
    parents = [row for row in rows if row.ucomm == "smbd" and row.ppid == parent]
    if len(parents) != 1:
        raise RuntimeError(f"expected one smbd under the manager, found {len(parents)}")
    return parents[0]
