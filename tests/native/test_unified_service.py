import subprocess

from tests.native.build import compile_native


def test_unified_service_dispatches_every_linked_native_mode(tmp_path):
    binary = compile_native("service", tmp_path / "service")

    def run(*args):
        return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=10)

    build_info = run("--build-info")
    assert build_info.returncode == 0
    assert "protocol=1" in build_info.stdout
    assert "modes=mdns,netbios,telemetry" in build_info.stdout

    telemetry = run("telemetry", "--version")
    assert telemetry.returncode == 0
    assert telemetry.stdout.strip() == "3"

    for role in ("mdns", "netbios", "discovery"):
        version = run(role, "--version")
        assert version.returncode == 0
        assert version.stdout.strip() == "30100"


def test_unified_inspect_plan_uses_service_collector(tmp_path):
    binary = compile_native("service", tmp_path / "service")
    facts = tmp_path / "facts"
    facts.write_text("mode=router\n")
    result = subprocess.run(
        [str(binary), "inspect", "plan", "--facts-file", str(facts)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    compatibility = subprocess.run(
        [str(binary), "--print-link-plan", "--facts-file", str(facts)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == compatibility.returncode
    assert result.stdout == compatibility.stdout
    assert result.stderr == compatibility.stderr
