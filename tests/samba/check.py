"""Run the quick or full test tier (AGENTS.md, "Test tiers").

    .venv/bin/python -m tests.samba.check --tier quick --out DIR --build
    .venv/bin/python -m tests.samba.check --tier full --out DIR --build
    .venv/bin/python -m tests.samba.check --tier quick --out DIR --binaries DIR --device 6

Phases, in order, each step's output in DIR/<device>-<step>.log:
  mac      pytest and the host regression run in Docker, in parallel;
  build    (--build) all three lanes on the VM (tests/samba/vm_build.py), into
           DIR/binaries; the full tier also installs smbd and the migrators
           into bin/ and updates the manifest;
  install  quick: swap the built smbd in without deploying
           (tests/samba/swap_smbd.py); full: deploy NetBSD 6, then NetBSD 4;
  devices  per device, the devices in parallel: the regression drivers from
           RAM, doctor, dir_device, growth_device, full only durable_device
           and links_device, then smbtorture (tests/samba/torture.py).

Without --build or --binaries there is nothing new to install or to run the
drivers from, so those steps are skipped and the devices are tested as they
are. The lock rows are held for each phase that needs them. The quick tier
stops at the first failed phase; the full tier runs everything. A summary
with each step's time ends the run; the exit status is nonzero if any step
failed.
"""
from __future__ import annotations

import argparse
import functools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import os
from pathlib import Path
import resource
import shlex
import subprocess
import sys
import time
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
ENVS = {"6": ".env.backup6", "4le": ".env.backup4"}
DRIVERS = ("tc_at_emulation_test", "tc_file_growth_test")
GROWTH_AIO = ("write:maxfilesize", "write:past-volume", "allowed:hole", "allowed:sequential")


@dataclass
class Step:
    name: str
    device: str | None
    argv: list[str] | None = None
    func: Callable[[Path], bool] | None = None


@dataclass
class Phase:
    name: str
    steps: list[Step]
    parallel: bool = True
    locks: list[str] = field(default_factory=list)
    # The devices phase: each device's steps, run in order, devices in parallel.
    per_device: dict[str, list[Step]] = field(default_factory=dict)


def py(*args: str) -> list[str]:
    return [sys.executable, "-m", *args]


def device_steps(tier: str, lane: str, env: str, out: Path, drivers_from: Path | None,
                 skip: set[str]) -> list[Step]:
    quick = tier == "quick"
    steps: list[Step] = []
    if drivers_from is not None:
        steps.append(Step("drivers", lane, func=functools.partial(run_drivers, env, lane, drivers_from)))
    steps.append(Step("doctor", lane, py("timecapsulesmb.cli.main", "doctor", "--config", env)))
    steps.append(Step("dir_device", lane, py("tests.samba.dir_device", "--env", env,
                                             *(["--quick"] if quick else ["--record", str(out / f"{lane}-dir.json")]))))
    steps.append(Step("growth_device", lane, py("tests.samba.growth_device", "--env", env, *(["--quick"] if quick else []))))
    if not quick:
        steps.append(Step("growth_device_aio", lane, py("tests.samba.growth_device", "--env", env, "--aio",
                                                        *[a for case in GROWTH_AIO for a in ("--case", case)])))
        steps.append(Step("durable_device", lane, py("tests.samba.durable_device", "--env", env)))
        steps.append(Step("links_device", lane, py("tests.samba.links_device", "--env", env)))
    steps.append(Step("smbtorture", lane, py("tests.samba.torture", "--env", env, "--tier", tier,
                                             "--device", lane, "--out", str(out / f"{lane}-smbtorture"))))
    return [step for step in steps if step.name not in skip]


def plan(tier: str, lanes: list[str], envs: dict[str, str], out: Path, build: bool,
         binaries: Path | None, skip: set[str]) -> list[Phase]:
    from tests.samba.locks import DEVICE_ROWS

    phases = []
    mac = [Step("pytest", None, func=run_pytest), Step("host_regression", None, func=run_host_regression)]
    phases.append(Phase("mac", [s for s in mac if s.name not in skip]))
    if build and "build" not in skip:
        binaries = out / "binaries"
        phases.append(Phase("build", [Step("vm_build", None, py(
            "tests.samba.vm_build", "--out", str(binaries), "--lanes", "6,4le,4be",
            *(["--install"] if tier == "full" else [])))], parallel=False, locks=[]))
        # vm_build holds the VM rows itself.
    # A reboot of NetBSD 6 drops NetBSD 4's network, so a full run holds both.
    device_rows = [DEVICE_ROWS[lane] for lane in lanes]
    if tier == "full" and "6" in lanes and DEVICE_ROWS["4le"] not in device_rows:
        device_rows.append(DEVICE_ROWS["4le"])
    if "install" not in skip:
        if tier == "quick" and binaries is not None:
            phases.append(Phase("install", [Step("swap_smbd", lane, py(
                "tests.samba.swap_smbd", "swap", "--env", envs[lane], str(binaries / f"smbd.{lane}")))
                for lane in lanes], locks=device_rows))
        elif tier == "full":
            order = [lane for lane in ("6", "4le") if lane in lanes]
            phases.append(Phase("install", [Step("deploy", lane, py(
                "timecapsulesmb.cli.main", "deploy", "--config", envs[lane], "--debug-logging", "--yes"))
                for lane in order], parallel=False, locks=device_rows))
    phases.append(Phase("devices", [], locks=device_rows,
                        per_device={lane: device_steps(tier, lane, envs[lane], out, binaries, skip)
                                    for lane in lanes}))
    return phases


def run_pytest(log: Path) -> bool:
    def limit() -> None:
        # Native children close descriptors up to the soft limit (AGENTS.md).
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
    with log.open("w") as out:
        return subprocess.run(py("pytest", "-n", "auto", "--dist", "worksteal", "-q", "-p", "no:cacheprovider"),
                              cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, preexec_fn=limit).returncode == 0


def run_host_regression(log: Path) -> bool:
    name = f"tc-host-{os.getpid()}"
    script = (
        f"docker rm -f {name} >/dev/null 2>&1; "
        f"docker run -d --name {name} ubuntu:24.04 sleep infinity >/dev/null && "
        f"docker exec {name} sh -c 'apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install "
        "-y -qq build-essential pkg-config python3 git bison flex libparse-yapp-perl libgnutls28-dev zlib1g-dev "
        "libpopt-dev patch rsync >/dev/null' && "
        "tar --exclude=./bin --exclude=./.git --exclude='*.pyc' --exclude=./macos --exclude=./.venv -cf - . | "
        f"docker exec -i {name} sh -c 'mkdir -p /root/repo && tar -xf - -C /root/repo' && "
        f"docker exec {name} sh -c 'cd /root/repo && python3 -m tests.samba.run host --work /root/work "
        "--jobs 8 --sanitizers'; rc=$?; "
        f"docker rm -f {name} >/dev/null 2>&1; exit $rc")
    with log.open("w") as out:
        return subprocess.run(["sh", "-c", script], cwd=ROOT, stdout=out, stderr=subprocess.STDOUT).returncode == 0


def driver_cases() -> dict[str, tuple[str, ...]]:
    from tests.samba.run import AT_EMULATION_CASES, FILE_GROWTH_CASES
    return {"tc_at_emulation_test": AT_EMULATION_CASES, "tc_file_growth_test": FILE_GROWTH_CASES}


def run_drivers(env: str, lane: str, binaries: Path, log: Path) -> bool:
    """Each driver from /mnt/Memory (AGENTS.md: not from the HFS disk on NetBSD 4),
    with TMPDIR on the data disk, one case at a time."""
    from tests.samba.links_device import Device
    from tests.samba.swap_smbd import disk_of
    from timecapsulesmb.core.config import parse_env_file

    device = Device(parse_env_file(ROOT / env))
    scratch = f"{disk_of(device.root)}/__tc_drivers__"
    failed = []
    with log.open("w") as out:
        device.sh(f"rm -rf {scratch} && mkdir -p {scratch}")
        try:
            for driver, cases in driver_cases().items():
                binary = binaries / f"{driver}.{lane}"
                ram = f"/mnt/Memory/{driver}"
                device.put(ram, binary.read_bytes())
                try:
                    device.sh(f"chmod 755 {ram}")
                    for case in cases:
                        text = device.sh(f"TMPDIR={scratch} {ram} {shlex.quote(case)} 2>&1; echo rc=$?", check=False)
                        out.write(f"== {driver} {case}\n{text}")
                        if not text.rstrip().endswith("rc=0"):
                            failed.append(f"{driver} {case}")
                finally:
                    device.sh(f"rm -f {ram}", check=False)
        finally:
            device.sh(f"rm -rf {scratch}", check=False)
        out.write(f"{len(failed)} failed: {failed}\n")
    return not failed


@dataclass
class Outcome:
    phase: str
    step: str
    device: str | None
    ok: bool
    seconds: float


def run_step(step: Step, out: Path) -> Outcome:
    log = out / f"{step.device or 'mac'}-{step.name}.log"
    start = time.monotonic()
    if step.func is not None:
        try:
            ok = step.func(log)
        except Exception as error:  # a step that crashes is a failed step
            log.write_text(f"{type(error).__name__}: {error}\n")
            ok = False
    else:
        with log.open("w") as handle:
            ok = subprocess.run(step.argv, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                                env=dict(os.environ, PYTHONPATH=f"{ROOT}:{ROOT / 'src'}")).returncode == 0
    return Outcome("", step.name, step.device, ok, time.monotonic() - start)


def run_phases(phases: list[Phase], out: Path, stop_on_failure: bool, holder: str,
               runner: Callable[[Step, Path], Outcome] = run_step, locks_cls=None) -> list[Outcome]:
    if locks_cls is None:
        from tests.samba.locks import Locks as locks_cls
    outcomes: list[Outcome] = []
    for phase in phases:
        per_device = phase.per_device
        print(f"== {phase.name}", flush=True)
        with locks_cls(phase.locks, holder, f"check {phase.name}") if phase.locks else _null():
            if per_device:
                def device_run(steps: list[Step]) -> list[Outcome]:
                    done = []
                    for step in steps:
                        result = runner(step, out)
                        done.append(result)
                        print(f"   {result.device}:{result.step} {'ok' if result.ok else 'FAILED'} "
                              f"{result.seconds:.0f} s", flush=True)
                    return done
                with ThreadPoolExecutor(len(per_device)) as pool:
                    results = [r for group in pool.map(device_run, per_device.values()) for r in group]
            elif phase.parallel and len(phase.steps) > 1:
                with ThreadPoolExecutor(len(phase.steps)) as pool:
                    results = list(pool.map(lambda s: runner(s, out), phase.steps))
            else:
                results = []
                for step in phase.steps:
                    results.append(runner(step, out))
                    if not results[-1].ok and stop_on_failure:
                        break
        for result in results:
            result.phase = phase.name
            if not per_device:
                print(f"   {result.device or 'mac'}:{result.step} {'ok' if result.ok else 'FAILED'} "
                      f"{result.seconds:.0f} s", flush=True)
        outcomes += results
        if stop_on_failure and not all(r.ok for r in results):
            break
    return outcomes


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def describe(phases: list[Phase]) -> str:
    """The plan, for --dry-run."""
    lines = []
    for phase in phases:
        mode = "parallel" if phase.parallel else "in order"
        lines.append(f"{phase.name} ({mode}{'; locks ' + ', '.join(phase.locks) if phase.locks else ''})")
        groups = phase.per_device or {None: phase.steps}
        for device, steps in groups.items():
            for step in steps:
                what = shlex.join(step.argv) if step.argv else f"{getattr(step.func, 'func', step.func).__name__}()"
                lines.append(f"  {step.device or 'mac'}:{step.name}  {what}")
    return "\n".join(lines)


def summary(outcomes: list[Outcome], total: float) -> str:
    lines = [f"{'phase':8} {'device':6} {'step':18} {'result':7} time"]
    for o in outcomes:
        lines.append(f"{o.phase:8} {o.device or 'mac':6} {o.step:18} {'ok' if o.ok else 'FAILED':7} "
                     f"{o.seconds / 60:.1f} min")
    failed = [o for o in outcomes if not o.ok]
    lines.append(f"{len(outcomes) - len(failed)} ok, {len(failed)} failed, {total / 60:.1f} min in all")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier", choices=("quick", "full"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--device", action="append", choices=tuple(ENVS), help="default: both")
    parser.add_argument("--build", action="store_true", help="build all three lanes on the VM first")
    parser.add_argument("--binaries", type=Path, help="a directory of vm_build outputs to test instead")
    parser.add_argument("--skip", action="append", default=[],
                        help="a step or phase to leave out (e.g. host_regression, smbtorture, install)")
    parser.add_argument("--env6", default=ENVS["6"])
    parser.add_argument("--env4le", default=ENVS["4le"])
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    args = parser.parse_args()
    if args.build and args.binaries:
        parser.error("--build and --binaries are exclusive")
    from tests.samba.locks import default_holder

    lanes = args.device or ["6", "4le"]
    # The env files live in the main checkout; steps run from the repo root.
    envs = {"6": str(Path(args.env6).resolve()), "4le": str(Path(args.env4le).resolve())}
    phases = plan(args.tier, lanes, envs, args.out, args.build, args.binaries, set(args.skip))
    if args.dry_run:
        print(describe(phases))
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    outcomes = run_phases(phases, args.out, args.tier == "quick", default_holder(ROOT))
    print(summary(outcomes, time.monotonic() - start))
    return 0 if outcomes and all(o.ok for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
