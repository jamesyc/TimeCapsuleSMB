"""Exercise runner failure propagation without downloading or building Samba."""
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.samba import run


@pytest.mark.parametrize("status", [0, 1, 2, 86, 90])
def test_runner_propagates_test_exit_status(tmp_path, monkeypatch, status):
    binary = tmp_path / "bin/default/source3/modules/fixture"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!{sys.executable}\nraise SystemExit({status})\n")
    binary.chmod(0o755)
    monkeypatch.setattr(run, "cases", lambda: iter([("fixture", ())]))
    if status:
        with pytest.raises(subprocess.CalledProcessError) as error:
            run.run_tests(tmp_path)
        assert error.value.returncode == status
    else:
        run.run_tests(tmp_path)


def fixture_driver(tmp_path, monkeypatch, script):
    binary = tmp_path / "bin/default/source3/modules/fixture"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n" + script)
    binary.chmod(0o755)
    monkeypatch.setattr(run, "cases", lambda: iter([("fixture", ())]))


def wait_gone(pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.02)
    pytest.fail(f"driver descendant {pid} outlived its test")


@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError])
@pytest.mark.parametrize("status", [0, 2])
def test_runner_accepts_either_answer_for_a_group_with_no_live_member(tmp_path, monkeypatch, error, status):
    # Linux answers ESRCH; Darwin can answer EPERM just after the reap.
    fixture_driver(tmp_path, monkeypatch, f"exit {status}\n")
    groups = []
    def killpg(pgid, sig):
        groups.append((pgid, sig))
        raise error(1, "no live member")
    monkeypatch.setattr(run.os, "killpg", killpg)
    if status:
        with pytest.raises(subprocess.CalledProcessError) as raised:
            run.run_tests(tmp_path)
        assert raised.value.returncode == status
    else:
        run.run_tests(tmp_path)
    assert len(groups) == 1 and groups[0][1] == signal.SIGKILL


@pytest.mark.parametrize("status", [0, 3])
def test_runner_kills_what_a_finished_driver_left_in_its_group(tmp_path, monkeypatch, status):
    fixture_driver(tmp_path, monkeypatch, f'sleep 30 &\necho $! > "{tmp_path}/child"\nexit {status}\n')
    if status:
        with pytest.raises(subprocess.CalledProcessError):
            run.run_tests(tmp_path)
    else:
        run.run_tests(tmp_path)
    wait_gone(int((tmp_path / "child").read_text()))


def test_runner_timeout_kills_the_whole_driver_group(tmp_path, monkeypatch):
    child = tmp_path / "child"
    fixture_driver(tmp_path, monkeypatch, f'sleep 60 &\necho $! > "{child}.new"\nmv "{child}.new" "{child}"\nsleep 3\n')
    def timeout_once_started(_target, _cross_exec):
        # The runner asks after starting the driver; time out only once its
        # child exists, however long a loaded host takes to get there.
        deadline = time.monotonic() + 30
        while not child.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        return .1
    monkeypatch.setattr(run, "case_timeout", timeout_once_started)
    with pytest.raises(subprocess.TimeoutExpired):
        run.run_tests(tmp_path)
    wait_gone(int(child.read_text()))


def test_runner_rejects_missing_test_without_running_later_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "cases", lambda: iter([("missing", ()), ("later", ())]))
    with pytest.raises(FileNotFoundError):
        run.run_tests(tmp_path)


@pytest.mark.parametrize("remote_dir", [None, "/tmp/probes", "/mnt/Memory/probes", "/Volumes/"])
def test_device_execution_rejects_root_and_ram_scratch_before_running(
    tmp_path, monkeypatch, remote_dir
):
    if remote_dir is None:
        monkeypatch.delenv("CROSS_EXEC_REMOTE_DIR", raising=False)
    else:
        monkeypatch.setenv("CROSS_EXEC_REMOTE_DIR", remote_dir)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("unsafe scratch must fail before execution"),
    )

    with pytest.raises(RuntimeError, match="CROSS_EXEC_REMOTE_DIR under /Volumes/"):
        run.run_tests(tmp_path, "cross-exec")


def test_device_execution_uploads_large_native_fixture_once():
    device_cases = list(run.execution_cases(True))
    host_cases = list(run.execution_cases(False))

    assert [item for item in device_cases if item[0] == run.TARGETS[4]] == [
        (run.TARGETS[4], ("all",)),
    ]
    assert [item for item in host_cases if item[0] == run.TARGETS[4]] == [
        *((run.TARGETS[4], (case,)) for case in run.NATIVE_METADATA_CASES),
        (run.TARGETS[4], ("all",)),
    ]
    assert [item for item in device_cases if item[0] == run.TARGETS[5]] == [
        (run.TARGETS[5], ("all",)),
    ]
    assert [item for item in host_cases if item[0] == run.TARGETS[5]] == [
        *((run.TARGETS[5], (case,)) for case in run.XATTR_MIGRATE_CASES),
        (run.TARGETS[5], ("all",)),
    ]
    assert run.case_timeout(run.TARGETS[4], True) == 180
    assert run.case_timeout(run.TARGETS[9], True) == 180  # time_range's vnode churn on HFS
    assert run.case_timeout(run.TARGETS[3], True) == 60
    assert run.case_timeout(run.TARGETS[4], False) == 25


def test_staged_targets_compile_current_fixtures_and_preserve_existing_rules(tmp_path):
    modules = tmp_path / "source3/modules"
    modules.mkdir(parents=True)
    script = modules / "wscript_build"
    script.write_text("bld.SAMBA3_BINARY('existing', source='existing.c')\n")
    smbd = tmp_path / "source3/smbd"
    smbd.mkdir()
    names = ("smbd_parent_conf_updated", "smbd_parent_sig_hup_handler", "smbd_sig_hup_handler", "smbd_conf_updated")
    bodies = [f"static void {name}(void)\n{{\n    observed += {1 << i};\n}}\n" for i, name in enumerate(names)]
    (smbd / "server.c").write_text("\n".join(bodies[:2]))
    (smbd / "smb2_process.c").write_text("\n".join(bodies[2:]))
    # Each growth caller region sits between unrelated code; its function has an
    # indented inner block, so only the column-0 brace may end the cut.
    regions = []
    for filename, first, last in run.GROWTH_CALLERS:
        region = (first + " /* state */\n};\n\n" if first != last else "") + last + (
            "off_t n)\n{\n\tif (n) {\n\t\treturn tc_file_growth_check(0, n);\n\t}\n\treturn 0;\n}")
        regions.append(region)
        (tmp_path / filename).write_text("static int before;\n\n" + region + "\n\nstatic void after(void)\n{\n}\n")
    run.stage(tmp_path)
    run.stage(tmp_path)
    assert (modules / "tc_file_growth_callers.inc").read_text() == "\n\n".join(regions) + "\n"
    calls = []

    class Builder:
        def SAMBA3_BINARY(self, name, **kwargs):
            calls.append((name, kwargs))

    exec(compile(script.read_text(), str(script), "exec"), {"bld": Builder()})
    assert [name for name, _ in calls] == ["existing", *run.TARGETS]
    for name, arguments in calls[1:]:
        expected_deps = {
            "tc_pthreadpool_sync_test": ["PTHREADPOOL"],
            "tc_streams_xattr_test": ["smbd_base", "HASH_INODE"],
            "tc_native_metadata_test": [
                "smbd_base", "HASH_INODE", "ADOUBLE", "OFFLOAD_TOKEN",
                "STRING_REPLACE", "dbwrap", "xattr_tdb",
            ],
            "tc_xattr_migrate_test": ["smbd_base", "dbwrap", "xattr_tdb"],
            # Includes vfs_catia.c, which maps names through STRING_REPLACE.
            "tc_catia_links_test": ["smbd_base", "STRING_REPLACE"],
            # The *at emulation lives in libreplace; the driver needs nothing else.
            "tc_at_emulation_test": ["replace"],
            # So does the NetBSD 6 fork repair (patch 0070).
            "tc_fork_repair_test": ["replace"],
        }.get(name, ["smbd_base"])
        assert arguments["deps"].split() == expected_deps
        assert arguments["install"] is False
        assert (modules / arguments["source"]).read_bytes() == (run.HERE / (name + ".c")).read_bytes()
    driver = modules / "callbacks.c"
    driver.write_text('static int observed;\n#include "tc_storage_reload_callbacks.inc"\n'
                      'int main(void) {\n' +
                      "".join(f"{name}();\n" for name in names) +
                      "return observed == 15 ? 0 : 1;\n}\n")
    binary = tmp_path / "callbacks"
    subprocess.run(["cc", str(driver), "-o", str(binary)], check=True, capture_output=True)
    subprocess.run([str(binary)], check=True, timeout=5)


def test_host_refuses_existing_directory_before_external_commands(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("must not run external commands for an existing checkout")

    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(subprocess, "check_output", unexpected)
    marker = tmp_path / "keep"
    marker.write_text("user data")
    with pytest.raises(FileExistsError):
        run.host(tmp_path, 2, False)
    assert marker.read_text() == "user data"


def build_inputs(root):
    files = {
        "build/_patch_helpers.sh": "patch_apply_series() { :; }\n",
        "build/patches/samba4x/series": "0001-a.patch\n",
        "build/patches/samba4x/0001-a.patch": "--- a\n+++ b\n",
        "build/patches/samba4x/overlay/lib/replace/x.c": "int x;\n",
        "tests/samba/run.py": "# runner\n",
        "tests/samba/targets.py": "# targets\n",
        "tests/samba/tc_a_test.c": "int main(void) { return 0; }\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return list(files)


SANITIZED = {"PATH": os.environ["PATH"], "CFLAGS": "-fsanitize=address", "LDFLAGS": "-fsanitize=address"}


def test_host_build_key_follows_every_build_input_and_nothing_else(tmp_path):
    inputs = build_inputs(tmp_path)
    key = run.host_build_key("url", "samba-4.25.0rc2", SANITIZED, tmp_path)
    assert run.host_build_key("url", "samba-4.25.0rc2", SANITIZED, tmp_path) == key
    for name in inputs:
        path = tmp_path / name
        original = path.read_text()
        path.write_text(original + "changed\n")
        assert run.host_build_key("url", "samba-4.25.0rc2", SANITIZED, tmp_path) != key, name
        path.write_text(original)
    assert run.host_build_key("url", "samba-4.25.0rc1", SANITIZED, tmp_path) != key
    assert run.host_build_key("other", "samba-4.25.0rc2", SANITIZED, tmp_path) != key
    # A new overlay file, or a new driver, is an input too.
    (tmp_path / "build/patches/samba4x/overlay/new.c").write_text("int y;\n")
    assert run.host_build_key("url", "samba-4.25.0rc2", SANITIZED, tmp_path) != key
    (tmp_path / "build/patches/samba4x/overlay/new.c").unlink()
    (tmp_path / "tests/samba/tc_b_test.c").write_text("int main(void) { return 1; }\n")
    assert run.host_build_key("url", "samba-4.25.0rc2", SANITIZED, tmp_path) != key
    (tmp_path / "tests/samba/tc_b_test.c").unlink()
    # Documentation and device-only tools are not.
    (tmp_path / "tests/samba/README.md").write_text("notes\n")
    (tmp_path / "tests/samba/dir_device.py").write_text("# device\n")
    (tmp_path / "build/service.sh").write_text("# service\n")
    assert run.host_build_key("url", "samba-4.25.0rc2", SANITIZED, tmp_path) == key


@pytest.mark.parametrize("change", [
    {"CFLAGS": None, "LDFLAGS": None},  # no sanitizers
    {"CFLAGS": "-fsanitize=address -O3"},
    {"CPPFLAGS": "-DNDEBUG"},
    {"LDFLAGS": "-fsanitize=address -static-libasan"},
    {"LINKFLAGS": "-Wl,-z,now"},
    {"CC": "compiler-b"},
])
def test_host_build_key_follows_the_compiler_and_flags_configure_reads(tmp_path, change):
    build_inputs(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("compiler-a", "compiler-b"):
        (bin_dir / name).write_text(f"#!/bin/sh\necho {name}\n")
        (bin_dir / name).chmod(0o755)
    env = {**SANITIZED, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CC": "compiler-a"}
    key = run.host_build_key("url", "ref", env, tmp_path)
    changed = {name: value for name, value in {**env, **change}.items() if value is not None}
    assert run.host_build_key("url", "ref", changed, tmp_path) != key
    # Settings configure does not read, such as the drivers' runtime options, are not inputs.
    assert run.host_build_key("url", "ref", {**env, "ASAN_OPTIONS": "detect_leaks=1"}, tmp_path) == key


def test_host_build_key_without_cc_follows_the_gcc_waf_picks(tmp_path):
    build_inputs(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("gcc", "cc"):
        (bin_dir / name).write_text(f"#!/bin/sh\necho {name} 13\n")
        (bin_dir / name).chmod(0o755)
    env = {**SANITIZED, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    key = run.host_build_key("url", "ref", env, tmp_path)
    (bin_dir / "cc").write_text("#!/bin/sh\necho cc 14\n")
    assert run.host_build_key("url", "ref", env, tmp_path) == key
    (bin_dir / "gcc").write_text("#!/bin/sh\necho gcc 14\n")
    assert run.host_build_key("url", "ref", env, tmp_path) != key


def test_host_build_key_follows_the_compiler_cc_names_not_just_its_name(tmp_path):
    # The same CC string, but a different compiler behind it (an upgrade).
    build_inputs(tmp_path)
    compiler = tmp_path / "bin" / "cc-wrapper"
    compiler.parent.mkdir()
    compiler.write_text("#!/bin/sh\necho gcc 13\n")
    compiler.chmod(0o755)
    env = {**SANITIZED, "CC": f"{compiler} -m64"}
    key = run.host_build_key("url", "ref", env, tmp_path)
    compiler.write_text("#!/bin/sh\necho gcc 14\n")
    assert run.host_build_key("url", "ref", env, tmp_path) != key


class FakeHostBuild:
    """Stands in for the clone/configure/build and the driver run."""

    def __init__(self, monkeypatch, key="first"):
        self.key = key
        self.builds = []
        self.runs = []
        self.fail = False
        monkeypatch.setattr(run.subprocess, "check_output", lambda *a, **k: "url\nref\n")
        monkeypatch.setattr(run, "host_build_key", lambda url, ref, env: self.key)
        monkeypatch.setattr(run, "build_host", self.build)
        monkeypatch.setattr(run, "run_host_tests", lambda source, env: self.runs.append(source))

    def build(self, url, ref, source, jobs, env):
        # Every build starts from an empty directory: never on an old tree.
        assert not source.exists()
        self.builds.append(source)
        if self.fail:
            raise subprocess.CalledProcessError(2, ["waf", "build"])
        (source / "bin").mkdir(parents=True)


def test_tree_cache_builds_once_then_runs_the_same_build_while_inputs_match(tmp_path, monkeypatch):
    fake = FakeHostBuild(monkeypatch)
    cache = tmp_path / "tree"
    run.host(tmp_path / "work1", 8, True, cache)
    run.host(tmp_path / "work2", 8, True, cache)
    assert fake.builds == [cache / "source"]
    assert fake.runs == [cache / "source", cache / "source"]
    assert (cache / "build-key").read_text() == "first"


def test_tree_cache_rebuilds_from_scratch_when_an_input_changes(tmp_path, monkeypatch):
    fake = FakeHostBuild(monkeypatch)
    cache = tmp_path / "tree"
    run.host(tmp_path / "work1", 8, True, cache)
    (cache / "source/stale-object.o").write_text("old")
    fake.key = "second"
    run.host(tmp_path / "work2", 8, True, cache)
    assert fake.builds == [cache / "source", cache / "source"]
    assert not (cache / "source/stale-object.o").exists()
    assert (cache / "build-key").read_text() == "second"
    assert len(fake.runs) == 2


def test_tree_cache_failed_build_runs_nothing_and_is_rebuilt_next_time(tmp_path, monkeypatch):
    fake = FakeHostBuild(monkeypatch)
    cache = tmp_path / "tree"
    fake.fail = True
    with pytest.raises(subprocess.CalledProcessError):
        run.host(tmp_path / "work1", 8, True, cache)
    assert not (cache / "build-key").exists() and fake.runs == []
    fake.fail = False
    run.host(tmp_path / "work2", 8, True, cache)
    assert len(fake.builds) == 2 and fake.runs == [cache / "source"]


def test_without_tree_cache_every_run_builds_its_new_work_directory(tmp_path, monkeypatch):
    fake = FakeHostBuild(monkeypatch)
    run.host(tmp_path / "work1", 8, True)
    run.host(tmp_path / "work2", 8, True)
    assert fake.builds == [tmp_path / "work1/source", tmp_path / "work2/source"]
    assert fake.runs == fake.builds
