"""The device's process table for the device test tools, read the way the
runtime reads it (timecapsulesmb.device.processes.render_ps_rows): command
lines only for the manager and smbd, so a process stuck on the disk, which a
test under load can make, cannot hang the listing."""
from __future__ import annotations

from dataclasses import dataclass

from timecapsulesmb.device.processes import ps_rows_command

COMMAND = ps_rows_command("rows", ("pid", "ppid", "pgid", "stat"), ("service", "smbd"))


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    group: int
    stat: str
    name: str
    # The command line for service and smbd processes ("(unreadable)" when
    # one was asleep in the kernel); empty for the rest.
    args: str


def parse(output: str) -> list[Process]:
    """COMMAND's rows, zombies left out."""
    rows = []
    for line in output.splitlines():
        fields = line.split(None, 5)
        if len(fields) >= 5 and all(field.isdigit() for field in fields[:3]) and not fields[3].startswith("Z"):
            rows.append(Process(int(fields[0]), int(fields[1]), int(fields[2]), fields[3], fields[4],
                                fields[5] if len(fields) == 6 else ""))
    return rows


def manager(rows: list[Process]) -> Process:
    managers = [row for row in rows if row.name == "service" and row.args.startswith("service: role=manager")]
    if len(managers) != 1:
        raise RuntimeError(f"expected one manager, found {len(managers)}")
    return managers[0]


def samba_parent(rows: list[Process]) -> Process:
    """The smbd the manager runs (its direct child)."""
    parent = manager(rows).pid
    parents = [row for row in rows if row.name == "smbd" and row.parent == parent]
    if len(parents) != 1:
        raise RuntimeError(f"expected one smbd under the manager, found {len(parents)}")
    return parents[0]
