"""A fake of the devices' NetBSD ps, kill and pkill for scripts run on the host.

Scripts list the process table with `ps ax -o COLUMN= ...` and read one
process's command line with `ps -ww -o ucomm= -o command= -p PID` (see
timecapsulesmb.device.processes.ps_rows). This fake answers both from a
JSON state file and records every call, so a test can check which command
lines a script read. A listing that asks for command lines fails: reading
every process's command line is what can hang a device.

Each process is a dict: pid, stat, ucomm, command, and optionally ppid, pgid,
sl, wchan, time, etime, and
    "argv": "unreadable"  ps prints "(ucomm)", as when it cannot read it
            "vanish"      the process exits when its command line is read
    "stat_later": the state a second look at the process shows
    "ucomm_later": the name it shows by then, as after an exec
    "survives": the signals ("TERM", "KILL") it outlives
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

LIST_REJECTED = 64
DEFAULTS = {"ppid": "1", "sl": "0", "wchan": "-", "time": "0:00.00", "etime": "00:01"}


def ps_row(pid: int, ucomm: str, command: str = "", *, ppid: int = 1, pgid: int | None = None, stat: str = "S",
           sl: int = 0, wchan: str = "-", time: str = "0:00.00") -> str:
    """One ps_rows output row (timecapsulesmb.device.processes.PS_COLUMNS,
    then the command line), for parser fixtures."""
    fields = [pid, ppid, pid if pgid is None else pgid, stat, sl, wchan, time, ucomm]
    return " ".join(str(field) for field in fields) + (f" {command}" if command else "")


def process(row: str, **extra: object) -> dict[str, object]:
    """A process from a "pid stat ucomm [command...]" row."""
    pid, stat, ucomm, *command = row.split(None, 3)
    found: dict[str, object] = {"pid": pid, "stat": stat, "ucomm": ucomm, "command": command[0] if command else ucomm}
    found.update(extra)
    return found


def _value(proc: dict[str, object], column: str) -> str:
    if column == "pgid":
        return str(proc.get("pgid", proc["pid"]))
    return str(proc.get(column, DEFAULTS.get(column, "")))


def answer(processes: list[dict[str, object]], argv: list[str]) -> tuple[str, int, str | None]:
    """ps's output and status for argv, and the PID whose process vanishes."""
    columns: list[str] = []
    listing = False
    pid = None
    words = iter(argv)
    for word in words:
        if word in ("ax", "axww"):
            listing = True
        elif word == "-ww":
            continue
        elif word == "-o":
            columns.append(next(words).rstrip("="))
        elif word == "-p":
            pid = next(words)
        else:
            raise SystemExit(f"fake ps: unsupported argument {word!r}")
    if listing:
        if "command" in columns or "args" in columns:
            return "fake ps: refusing to list every command line\n", LIST_REJECTED, None
        rows = []
        for proc in processes:
            values = [_value(proc, column) for column in columns]
            # Like NetBSD's ps: numbers right-aligned, ucomm padded unless last.
            values = [value.rjust(5) if column in ("pid", "ppid", "pgid") else value
                      for column, value in zip(columns, values)]
            values = [value.ljust(16) if column == "ucomm" and index < len(columns) - 1 else value
                      for index, (column, value) in enumerate(zip(columns, values))]
            rows.append(" ".join(values))
        return "".join(f"{row}\n" for row in rows), 0, None
    proc = next((proc for proc in processes if str(proc["pid"]) == pid), None)
    if pid is None or proc is None:
        return "", 1, None
    if columns == ["stat"]:
        return f"{proc.get('stat_later', proc['stat'])}\n", 0, None
    if columns == ["ucomm", "command"]:
        if proc.get("argv") == "vanish":
            return "", 1, pid
        ucomm = str(proc.get("ucomm_later", proc["ucomm"]))
        command = f"({ucomm})" if proc.get("argv") == "unreadable" else proc["command"]
        return f"{ucomm.ljust(16)} {command}\n", 0, None
    raise SystemExit(f"fake ps: unsupported -p columns {columns!r}")


class FakePs:
    """A device's process table in a state file, and the commands that read it."""

    def __init__(self, directory: Path, processes: list[dict[str, object]], *, list_fails: bool = False):
        self.state = directory / "device-ps.json"
        self.write({"processes": processes, "calls": [], "signals": [], "list_fails": list_fails})

    def read(self) -> dict[str, object]:
        return json.loads(self.state.read_text())

    def write(self, data: dict[str, object]) -> None:
        self.state.write_text(json.dumps(data))

    def command(self, tool: str) -> str:
        return shlex.join([sys.executable, __file__, str(self.state), tool])

    def install(self, script: str) -> str:
        """script with its ps, kill and pkill replaced by this fake."""
        for path, tool in (("/bin/ps", "ps"), ("/bin/kill", "kill"), ("/usr/bin/pkill", "pkill")):
            script = script.replace(path, self.command(tool))
        return script

    @property
    def calls(self) -> list[list[str]]:
        return self.read()["calls"]  # type: ignore[return-value]

    def command_reads(self) -> list[str]:
        """The PIDs whose command lines were read, in order."""
        return [call[-1] for call in self.calls if "command=" in call and "-p" in call]

    @property
    def pids(self) -> set[str]:
        return {str(proc["pid"]) for proc in self.read()["processes"]}  # type: ignore[union-attr]

    @property
    def signals(self) -> list[list[str]]:
        return self.read()["signals"]  # type: ignore[return-value]


def main(argv: list[str]) -> int:
    state = Path(argv[0])
    tool, args = argv[1], argv[2:]
    data = json.loads(state.read_text())
    status = 0
    if tool == "ps":
        data["calls"].append(args)
        if data["list_fails"] and ("ax" in args or "axww" in args):
            status = 1
        else:
            out, status, vanished = answer(data["processes"], args)
            if status == LIST_REJECTED:
                sys.stderr.write(out)
            else:
                sys.stdout.write(out)
            if vanished is not None:
                data["processes"] = [proc for proc in data["processes"] if str(proc["pid"]) != vanished]
    elif tool in ("kill", "pkill"):
        signal, target = (args[0], args[1]) if len(args) == 2 else ("-TERM", args[0])
        signal = {"-9": "-KILL", "-15": "-TERM"}.get(signal, signal)
        if tool == "kill":
            hit = [proc for proc in data["processes"] if str(proc["pid"]) == target]
        else:
            # pkill matches ucomm against an anchored pattern, "^name$" here.
            hit = [proc for proc in data["processes"] if f"^{proc['ucomm']}$" == target]
        if signal != "-0":
            for proc in hit:
                data["signals"].append([signal, str(proc["pid"])])
            gone = {id(proc) for proc in hit if signal.lstrip("-") not in proc.get("survives", ())}
            data["processes"] = [proc for proc in data["processes"] if id(proc) not in gone]
        status = 0 if hit else 1
    elif tool == "exit":
        # The process exits on its own.
        data["processes"] = [proc for proc in data["processes"] if str(proc["pid"]) != args[0]]
    else:
        raise SystemExit(f"fake ps: unknown tool {tool!r}")
    state.write_text(json.dumps(data))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
