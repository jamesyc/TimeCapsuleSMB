"""Run smbtorture against a deployed device and report what changed.

    .venv/bin/python -m tests.samba.torture --env .env.backup6 --tier quick --out DIR

The client runs in Docker (tests/samba/smbtorture/Dockerfile, built from the
Samba source build/env.sh pins); each suite or subtest runs in its own
container with a 30-minute limit, and its output lands in DIR/<name>.txt.
About 55 tests fail on the appliance for known reasons
(tests/samba/smbtorture/known_failures.txt). The report lists only failures
not in that list and listed failures that now pass, and the exit status is
nonzero when there is a new failure or a run produced no results.

Tiers (AGENTS.md, "Test tiers"): full runs every suite; quick leaves out the
five slow subtests, which take about 85% of the full run.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
KNOWN_FAILURES = HERE / "smbtorture" / "known_failures.txt"
IMAGE = "tc-smbtorture"
TIMEOUT = 1800

FULL_SUITES = (
    "smb2.dir", "smb2.create", "smb2.rename", "smb2.delete-on-close-perms", "smb2.compound_find",
    "smb2.getinfo", "smb2.setinfo", "smb2.fileid", "smb2.dosmode", "smb2.maximum_allowed",
    "smb2.openattr", "smb2.winattr", "smb2.winattr2", "smb2.timestamps", "smb2.streams",
    "smb2.charset", "smb2.name-mangling", "smb2.rw", "smb2.dirlease",
)
# Suites the quick tier runs subtest by subtest, to leave the slow ones out.
SUBTESTS = {
    "smb2.dir": ("find", "fixed", "one", "many", "modify", "sorted", "file-index", "large-files",
                 "1kfiles_rename"),
    "smb2.compound_find": ("compound_find_related", "compound_find_unrelated", "compound_find_close"),
}
# Measured 2026-09-28/29: compound_find_close 17-25 min, large-files 6-7,
# sorted 4, many 3.5, 1kfiles_rename 9-12; the rest of the list about 8.
QUICK_EXCLUDE = frozenset({
    "smb2.compound_find.compound_find_close", "smb2.dir.large-files", "smb2.dir.sorted",
    "smb2.dir.many", "smb2.dir.1kfiles_rename",
})
RESULT = re.compile(r"^(success|failure|error|skip|xfail|uxsuccess): (.+?)(?: \[)?$")


def plan(tier: str) -> list[str]:
    """The names to run, one smbtorture invocation each. The two long suites
    run subtest by subtest in both tiers, so each subtest gets the 30-minute
    limit: on NetBSD 4, smb2.dir as one run takes longer than that
    (1kfiles_rename alone is about 12 minutes there)."""
    if tier not in ("quick", "full"):
        raise ValueError(tier)
    names = []
    for suite in FULL_SUITES:
        if suite in SUBTESTS:
            names += [f"{suite}.{test}" for test in SUBTESTS[suite]
                      if tier == "full" or f"{suite}.{test}" not in QUICK_EXCLUDE]
        else:
            names.append(suite)
    return names


def selected(tier: str, only: list[str] | None) -> list[str]:
    """--test NAME ... runs just those (each a suite or suite.subtest from the
    full list); otherwise the tier's list."""
    if not only:
        return plan(tier)
    allowed = set(plan("full")) | set(FULL_SUITES)
    unknown = [name for name in only if name not in allowed]
    if unknown:
        raise ValueError(f"not in the full list: {unknown}")
    return list(only)


@dataclass(frozen=True)
class Known:
    name: str
    flaky: bool = False
    only: str | None = None


def parse_known(text: str) -> dict[str, Known]:
    known = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        flaky, only = False, None
        while True:
            head, _, rest = line.partition(" ")
            if head == "flaky":
                flaky, line = True, rest.strip()
            elif head.startswith("only="):
                only, line = head[len("only="):], rest.strip()
            else:
                break
        known[line] = Known(line, flaky, only)
    return known


def outcomes(run_name: str, text: str) -> dict[str, str]:
    """Each test's outcome in one smbtorture output. A subtest run reports its
    own last name component; a suite run reports its children's names."""
    leaf = run_name.rsplit(".", 1)[-1]
    result = {}
    for line in text.splitlines():
        match = RESULT.match(line.rstrip())
        if match:
            name = match.group(2)
            key = run_name if (name == leaf and run_name.count(".") >= 2) else f"{run_name}.{name}"
            result[key] = match.group(1)
    return result


@dataclass
class Report:
    new_failures: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    no_results: list[str] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def ok(self) -> bool:
        return not self.new_failures and not self.no_results


def compare(results: dict[str, dict[str, str]], known: dict[str, Known], device: str) -> Report:
    """results: run name -> {test: outcome}."""
    report = Report()
    applicable = {name: k for name, k in known.items() if k.only in (None, device)}
    for run_name, tests in results.items():
        if not tests:
            report.no_results.append(run_name)
        for test, outcome in tests.items():
            entry = applicable.get(test)
            if outcome in ("failure", "error"):
                report.failed += 1
                if entry is None:
                    report.new_failures.append(test)
            elif outcome in ("success", "uxsuccess"):
                report.passed += 1
                if entry is not None and not entry.flaky:
                    report.fixed.append(test)
            else:
                report.skipped += 1
    for field_ in (report.new_failures, report.fixed, report.no_results):
        field_.sort()
    return report


def format_report(report: Report, device: str) -> str:
    lines = [f"smbtorture on {device}: {report.passed} passed, {report.failed} failed "
             f"({len(report.new_failures)} not known), {report.skipped} skipped"]
    lines += [f"  NEW FAILURE {name}" for name in report.new_failures]
    lines += [f"  NO RESULTS  {name}" for name in report.no_results]
    lines += [f"  NOW PASSES  {name} (remove it from known_failures.txt)" for name in report.fixed]
    return "\n".join(lines)


def samba_pin() -> tuple[str, str, str]:
    """(url, ref, commit) from build/env.sh, as tests.samba.run does."""
    config = str(ROOT / "build/env.sh")
    out = subprocess.check_output(
        ["sh", "-c", '. "$1"; printf "%s\\n%s\\n%s\\n" "$SAMBA4X_GIT_URL" "$SAMBA4X_GIT_REF" "$SAMBA4X_GIT_COMMIT"',
         config, config], env=dict(os.environ, TC_ENV_FILE="/dev/null"), text=True)
    url, ref, commit = out.splitlines()
    return url, ref, commit


def image_tag(ref: str) -> str:
    return f"{IMAGE}:{ref}"


def build_image(run=subprocess.run) -> str:
    """Build (or reuse, through Docker's cache) the smbtorture image."""
    url, ref, commit = samba_pin()
    tag = image_tag(ref)
    run(["docker", "build", "-q", "-t", tag, "--build-arg", f"SAMBA_URL={url}", "--build-arg", f"SAMBA_REF={ref}",
         "--build-arg", f"SAMBA_COMMIT={commit}", "-f", str(HERE / "smbtorture" / "Dockerfile"),
         str(HERE / "smbtorture")], check=True, stdout=subprocess.DEVNULL)
    return tag


def auth_text(env: dict[str, str]) -> str:
    return f"username = {env.get('TC_SAMBA_USER') or 'admin'}\npassword = {env['TC_PASSWORD']}\n"


def command(tag: str, auth_path: Path, host: str, share: str, name: str) -> list[str]:
    return ["docker", "run", "--rm", "-v", f"{auth_path}:/auth:ro", tag,
            "timeout", str(TIMEOUT), "bin/smbtorture", f"//{host}/{share}", "-A", "/auth",
            "--option=clientmaxprotocol=SMB3", name]


def run_all(tag: str, env: dict[str, str], host: str, share: str, names: list[str], out: Path,
            run=subprocess.run, log=print) -> dict[str, dict[str, str]]:
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth"
        auth.write_text(auth_text(env))
        auth.chmod(0o600)
        for name in names:
            start = time.monotonic()
            proc = run(command(tag, auth, host, share, name), capture_output=True, text=True)
            text = (proc.stdout or "") + (proc.stderr or "")
            (out / f"{name}.txt").write_text(text)
            results[name] = outcomes(name, text)
            log(f"{name}: exit {proc.returncode}, {time.monotonic() - start:.0f} s, "
                f"{sum(1 for o in results[name].values() if o in ('failure', 'error'))} failed")
    return results


# How long stuck_children() watches, and the CPU share that marks a child as
# spinning: with every test connection closed, no smbd child should compute.
SPIN_INTERVAL = 5.0
SPIN_SHARE = 0.8
PS_COMMAND = "ps -ax -o pid,ppid,utime,command"


def cpu_seconds(text: str) -> float:
    """ps's utime: "1.375368", "22:40.62" or "0:44:17.45"."""
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


def smbd_children(ps: str) -> dict[int, float]:
    """smbd processes forked by another smbd (the parent's is the manager),
    with their user CPU seconds."""
    rows = []
    for line in ps.splitlines()[1:]:
        fields = line.split(None, 3)
        if len(fields) == 4 and "/smbd" in fields[3].split()[0]:
            rows.append((int(fields[0]), int(fields[1]), cpu_seconds(fields[2])))
    pids = {pid for pid, _, _ in rows}
    return {pid: utime for pid, ppid, utime in rows if ppid in pids}


def stuck_children(first: str, second: str, interval: float = SPIN_INTERVAL) -> list[int]:
    """Children that used at least SPIN_SHARE of a CPU between two ps samples
    taken interval seconds apart. On NetBSD 6 without patch 0070 a connection
    child could spin forever after smb2.dir.large-files."""
    before, after = smbd_children(first), smbd_children(second)
    return sorted(pid for pid, utime in after.items()
                  if pid in before and utime - before[pid] >= SPIN_SHARE * interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True)
    parser.add_argument("--share")
    parser.add_argument("--tier", choices=("quick", "full"), default="quick")
    parser.add_argument("--device", choices=("6", "4le"), help="for device-specific known failures")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--test", action="append", help="run only this suite or subtest (repeatable)")
    args = parser.parse_args()
    try:
        names = selected(args.tier, args.test)
    except ValueError as error:
        parser.error(str(error))
    from tests.samba.links_device import Device
    from timecapsulesmb.core.config import parse_env_file

    env = parse_env_file(Path(args.env))
    device = Device(env, args.share)
    name = args.device or ("4le" if "backup4" in args.env else "6")
    tag = build_image()
    results = run_all(tag, env, device.host, device.share, names, args.out)
    report = compare(results, parse_known(KNOWN_FAILURES.read_text()), name)
    print(format_report(report, name))
    first = device.sh(PS_COMMAND)
    time.sleep(SPIN_INTERVAL)
    stuck = stuck_children(first, device.sh(PS_COMMAND))
    for pid in stuck:
        print(f"  STUCK CHILD smbd pid {pid} is spinning with no client (kill it by PID)")
    return 0 if report.ok and not stuck else 1


if __name__ == "__main__":
    sys.exit(main())
