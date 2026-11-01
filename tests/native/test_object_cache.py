"""tests/native/build.py: the workers of one pytest run compile each object once."""
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.native import build

ROOT = Path(__file__).resolve().parents[2]
compile_object = build._compile_object.__wrapped__


@pytest.fixture
def cc(tmp_path, monkeypatch):
    """A cc on PATH. With -E it prints the source and each -D whose name the
    source mentions, as a preprocessor would expand it. With -c it logs the
    run and writes "object" to its -o file, or fails, writing half an object
    first, while tmp_path/fail exists."""
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "cc.log"
    script = tools / "cc"
    script.write_text(f"""#!/bin/sh
mode= out= src= defines=
while [ $# -gt 0 ]; do
    case $1 in
        -E) mode=E ;;
        -c) mode=c ;;
        -o) out=$2; shift ;;
        -D*) defines="$defines ${{1#-D}}" ;;
        *.c) src=$1 ;;
    esac
    shift
done
if [ "$mode" = E ]; then
    text=$(cat "$src")
    printf '%s\\n' "$text"
    for define in $defines; do
        case $text in *"${{define%%=*}}"*) echo "$define" ;; esac
    done
    exit 0
fi
echo "$src" >> '{log}'
if [ -e '{tmp_path}/fail' ]; then printf half > "$out"; echo 'cc: boom' >&2; exit 1; fi
printf object > "$out"
""")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("TC_NATIVE_OBJECT_DIR", str(tmp_path / "objects"))
    monkeypatch.delenv("TC_NATIVE_COVERAGE_DIR", raising=False)
    return lambda: log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def sources(tmp_path):
    (tmp_path / "a.c").write_text("int a = X;\n")
    (tmp_path / "b.c").write_text("int b = Y;\n")
    return tmp_path / "a.c", tmp_path / "b.c"


def test_an_object_compiled_once_is_reused_by_the_next_worker(cc, sources, tmp_path):
    a, b = sources
    first = compile_object(a, ("-DX=1",), ())
    second = compile_object(a, ("-DX=1",), ())
    assert first == second and first.parent == tmp_path / "objects"
    assert first.read_text() == "object" and cc() == [str(a)]
    # A define the source uses, another source, or a flag that changes the
    # code is another object.
    assert len({first, compile_object(a, ("-DX=2",), ()), compile_object(b, ("-DX=1",), ()),
                compile_object(a, ("-DX=1", "-O2"), ())}) == 4
    assert len(cc()) == 4


def test_defines_a_source_never_uses_share_its_object(cc, sources, tmp_path):
    # Fixtures define paths under each worker's own directory.
    a, b = sources
    gw0 = ("-DX=1", f'-DTC_RAM_ROOT="{tmp_path}/gw0/ram"', "-I", str(tmp_path / "gw0"))
    gw1 = ("-DX=1", f'-DTC_RAM_ROOT="{tmp_path}/gw1/ram"', "-I", str(tmp_path / "gw1"))
    assert compile_object(a, gw0, ()) == compile_object(a, gw1, ())
    assert cc() == [str(a)]
    # A source that uses the define gets one object per worker.
    (tmp_path / "c.c").write_text("char *ram = TC_RAM_ROOT;\n")
    assert compile_object(tmp_path / "c.c", gw0, ()) != compile_object(tmp_path / "c.c", gw1, ())
    assert len(cc()) == 3


def test_an_object_another_worker_is_compiling_is_waited_for_not_compiled_again(cc, sources, tmp_path, monkeypatch):
    a, _ = sources
    obj = compile_object(a, (), ())
    obj.unlink()
    # Another worker holds the object's lock while it compiles.
    holder = open(obj.with_suffix(".lock"), "w")
    build.fcntl.flock(holder, build.fcntl.LOCK_EX)
    real_flock = build.fcntl.flock
    waiting = threading.Event()

    def flock(fd, operation):
        waiting.set()
        return real_flock(fd, operation)

    monkeypatch.setattr(build.fcntl, "flock", flock)
    found = []
    worker = threading.Thread(target=lambda: found.append(compile_object(a, (), ())))
    worker.start()
    assert waiting.wait(30)
    # The other worker publishes its object and lets go.
    obj.write_text("the other worker's object")
    holder.close()
    worker.join(30)
    assert found == [obj] and obj.read_text() == "the other worker's object"
    assert len(cc()) == 1


def test_a_failed_compile_publishes_nothing_and_the_next_try_compiles_again(cc, sources, tmp_path):
    a, _ = sources
    (tmp_path / "fail").touch()
    with pytest.raises(AssertionError, match="cc: boom"):
        compile_object(a, (), ())
    # Not even the half-written output is left for another worker to take.
    assert not list((tmp_path / "objects").glob("*.o"))
    (tmp_path / "fail").unlink()
    obj = compile_object(a, (), ())
    assert obj.read_text() == "object" and len(cc()) == 2


def test_objects_stay_private_without_a_run_directory(monkeypatch):
    monkeypatch.delenv("TC_NATIVE_COVERAGE_DIR", raising=False)
    monkeypatch.delenv("TC_NATIVE_OBJECT_DIR", raising=False)
    assert build.build_root("objects") == Path(build._BUILD.name)
    monkeypatch.setenv("TC_NATIVE_OBJECT_DIR", "/run/objects")
    assert build.build_root("objects") == Path("/run/objects")
    # Linked products stay in the worker's own directory either way.
    assert build.build_root("products") == Path(build._BUILD.name)


def test_a_run_gives_its_workers_one_object_directory_and_removes_it(tmp_path):
    seen = tmp_path / "seen"
    seen.mkdir()
    probe = tmp_path / "test_probe.py"
    # Every worker collects the module, so each one records what it got.
    probe.write_text(f"""import os, pathlib
objects = os.environ["TC_NATIVE_OBJECT_DIR"]
assert pathlib.Path(objects).is_dir()
pathlib.Path({str(seen)!r}, os.environ["PYTEST_XDIST_WORKER"]).write_text(objects)
def test_probe(): pass
""")
    # Not the CI job's TC_TEST_SHARD either, which could deselect the probe.
    env = {k: v for k, v in os.environ.items() if k not in ("TC_NATIVE_OBJECT_DIR", "TC_TEST_SHARD")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "tests.conftest", "-n", "2",
                             "-p", "no:cacheprovider", "-q", "--rootdir", str(tmp_path), str(probe)],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    dirs = {path.name: path.read_text() for path in seen.iterdir()}
    assert sorted(dirs) == ["gw0", "gw1"] and len(set(dirs.values())) == 1
    assert not Path(next(iter(dirs.values()))).exists()
