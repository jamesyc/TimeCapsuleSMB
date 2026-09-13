"""Exercise runner failure propagation without downloading or building Samba."""
import subprocess
import sys

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


def test_runner_rejects_missing_test_without_running_later_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "cases", lambda: iter([("missing", ()), ("later", ())]))
    with pytest.raises(FileNotFoundError):
        run.run_tests(tmp_path)


def test_staged_targets_compile_current_fixtures_and_preserve_existing_rules(tmp_path):
    modules = tmp_path / "source3/modules"
    modules.mkdir(parents=True)
    script = modules / "wscript_build"
    script.write_text("bld.SAMBA3_BINARY('existing', source='existing.c')\n")
    run.stage(tmp_path)
    run.stage(tmp_path)
    calls = []

    class Builder:
        def SAMBA3_BINARY(self, name, **kwargs):
            calls.append((name, kwargs))

    exec(compile(script.read_text(), str(script), "exec"), {"bld": Builder()})
    assert [name for name, _ in calls] == ["existing", *run.TARGETS[1:]]
    for name, arguments in calls[1:]:
        assert arguments["deps"] == "smbd_base"
        assert arguments["install"] is False
        assert (modules / arguments["source"]).read_bytes() == (run.HERE / (name + ".c")).read_bytes()


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
