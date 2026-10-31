"""Exercise development setup through make without downloading dependencies."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = Path(__file__).resolve().parents[1]
    for name in ("Makefile", "pyproject.toml"):
        shutil.copyfile(root / name, tmp_path / name)
    monkeypatch.delenv("UV_PYTHON", raising=False)
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    uv = bindir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:] + [os.environ.get('UV_PYTHON', '')]\n"
        "with Path('uv-calls.jsonl').open('a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "if args[0] == 'venv':\n"
        "    target = Path(args[-2])\n"
        "    # Like uv, refuse an existing environment unless told to keep it.\n"
        "    if target.exists() and '--allow-existing' not in args:\n"
        "        sys.exit('A virtual environment already exists')\n"
        "    python = target / 'bin/python'\n"
        "    if not python.exists():\n"
        "        python.parent.mkdir(parents=True, exist_ok=True)\n"
        "        python.write_text('#!/bin/sh\\nexit 99\\n')\n"
        "        python.chmod(0o755)\n"
        "elif args[:2] == ['pip', 'install']:\n"
        "    assert Path(args[args.index('--python') + 1]).is_file()\n"
        "else:\n"
        "    sys.exit(2)\n"
    )
    uv.chmod(0o755)
    return tmp_path


def make(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", *args],
        cwd=project, capture_output=True, text=True, check=False,
    )


def calls(project: Path) -> list[list[str]]:
    return [json.loads(line) for line in (project / "uv-calls.jsonl").read_text().splitlines()]


VENV = ["venv", "--allow-existing", ".venv", ""]
INSTALL = [
    "pip", "install", "--upgrade", "--python", ".venv/bin/python", "-e", ".", "--group", "dev", "",
]


@pytest.mark.parametrize("target", ["venv", "setup", "install"])
def test_fresh_setup_uses_uv(project: Path, target: str) -> None:
    result = make(project, target)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = [VENV]
    if target != "venv":
        expected.append(INSTALL)
    assert calls(project) == expected
    assert (project / ".venv/.deps-installed").exists() == (target != "venv")


def test_uv_python_selects_ci_interpreter(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # setup-uv's python-version sets UV_PYTHON, which replaces .python-version.
    monkeypatch.setenv("UV_PYTHON", "/ci/python3.9")
    result = make(project, "install")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls(project) == [VENV[:-1] + ["/ci/python3.9"], INSTALL[:-1] + ["/ci/python3.9"]]


def test_existing_environment_without_pip_is_preserved(project: Path) -> None:
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 99\n")
    python.chmod(0o755)
    result = make(project, "install")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls(project) == [INSTALL]
    assert python.read_text() == "#!/bin/sh\nexit 99\n"


def test_unchanged_install_is_skipped(project: Path) -> None:
    first = make(project, "install")
    assert first.returncode == 0, first.stdout + first.stderr
    stamp = (project / ".venv/.deps-installed").stat().st_mtime_ns
    second = make(project, "install")
    assert second.returncode == 0, second.stdout + second.stderr
    assert len(calls(project)) == 2
    assert (project / ".venv/.deps-installed").stat().st_mtime_ns == stamp


def test_changed_pyproject_reinstalls_without_recreating_environment(project: Path) -> None:
    first = make(project, "install")
    assert first.returncode == 0, first.stdout + first.stderr
    # Set ordering explicitly: no sleep or filesystem timestamp-resolution race.
    stamp = project / ".venv/.deps-installed"
    now = stamp.stat().st_mtime
    for name in ("pyproject.toml", ".venv/bin/python"):
        os.utime(project / name, (now - 20, now - 20))
    os.utime(stamp, (now - 10, now - 10))
    os.utime(project / "pyproject.toml", (now - 5, now - 5))
    result = make(project, "install")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls(project)[1:] == [INSTALL, INSTALL]
    assert stamp.stat().st_mtime > now - 10


def test_forced_install_upgrades_in_the_existing_environment(project: Path) -> None:
    # CONTRIBUTING's refresh command: -B remakes the venv target as well.
    first = make(project, "install")
    assert first.returncode == 0, first.stdout + first.stderr
    python = project / ".venv/bin/python"
    python.write_text("#!/bin/sh\nexit 98\n")
    result = make(project, "-B", "install")
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls(project) == [VENV, INSTALL, VENV, INSTALL]
    assert python.read_text() == "#!/bin/sh\nexit 98\n"
