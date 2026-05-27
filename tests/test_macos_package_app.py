from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


PACKAGE_SCRIPT = Path(__file__).resolve().parents[1] / "macos" / "TimeCapsuleSMB" / "tools" / "package_app.py"


def load_package_app_module():
    spec = importlib.util.spec_from_file_location("package_app", PACKAGE_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_fake_app_executable_and_resources(app: Path) -> None:
    executable = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    resource_bundle = (
        app
        / "Contents"
        / "Resources"
        / "TimeCapsuleSMBMac_TimeCapsuleSMBApp.bundle"
        / "en.lproj"
    )
    executable.parent.mkdir(parents=True, exist_ok=True)
    resource_bundle.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    create_fake_python_runtime(app)
    (resource_bundle / "Localizable.strings").write_text('"screen.readiness" = "Readiness";\n', encoding="utf-8")


def create_fake_python_runtime(app: Path) -> None:
    python_home = (
        app
        / "Contents"
        / "Resources"
        / "Python"
        / "Runtime"
        / "Python.framework"
        / "Versions"
        / "Current"
    )
    python_home.mkdir(parents=True, exist_ok=True)
    (python_home / "bin").mkdir(parents=True, exist_ok=True)
    (python_home / "bin" / "python3").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (python_home / "bin" / "python3").chmod(0o755)
    (python_home / "Python").write_text("python framework", encoding="utf-8")


def create_fake_certifi_package(site_packages: Path) -> None:
    certifi = site_packages / "certifi"
    certifi.mkdir(parents=True, exist_ok=True)
    (certifi / "__init__.py").write_text("def where(): return __file__\n", encoding="utf-8")
    (certifi / "cacert.pem").write_text("test ca bundle\n", encoding="utf-8")


def test_smoke_request_accepts_successful_result_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    calls: list[dict[str, object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"type":"stage","operation":"capabilities"}\n{"type":"result","operation":"capabilities","ok":true}\n',
            stderr="",
        )

    monkeypatch.setattr(package_app, "run", fake_run)

    package_app.smoke_request(tmp_path / "tcapsule", "capabilities", tmp_path)

    assert calls
    assert calls[0]["cmd"] == [str(tmp_path / "tcapsule"), "api"]


def test_smoke_request_rejects_missing_result_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout='{"type":"stage","operation":"capabilities"}\n', stderr="")

    monkeypatch.setattr(package_app, "run", fake_run)

    with pytest.raises(RuntimeError, match="did not emit a result event"):
        package_app.smoke_request(tmp_path / "tcapsule", "capabilities", tmp_path)


def test_smoke_request_rejects_failed_result_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"type":"result","operation":"validate-install","ok":false}\n',
            stderr="",
        )

    monkeypatch.setattr(package_app, "run", fake_run)

    with pytest.raises(RuntimeError, match="smoke test failed"):
        package_app.smoke_request(tmp_path / "tcapsule", "validate-install", tmp_path)


def test_assert_bundle_layout_checks_helper_python_tools_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, tools, distribution / "bin" / "payloads"):
        directory.mkdir(parents=True)
    create_fake_app_executable_and_resources(app)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    (distribution / "artifact-manifest.json").write_text('{"artifacts":{}}', encoding="utf-8")

    monkeypatch.setattr(package_app, "artifact_paths", lambda: ["bin/payloads/one", "bin/payloads/two"])
    monkeypatch.setattr(package_app, "assert_python_dependencies_are_bundled", lambda app: None)
    # This synthetic bundle-layout test should stay portable across the CI
    # matrix. Dedicated tests below cover the macOS Mach-O validators directly.
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies", lambda app: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: None)
    monkeypatch.setattr(package_app, "validate_app_resources", lambda app: None)
    create_fake_certifi_package(python_packages)
    (distribution / "bin" / "payloads" / "one").write_text("one", encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing payload artifact"):
        package_app.assert_bundle_layout(app)

    (distribution / "bin" / "payloads" / "two").write_text("two", encoding="utf-8")

    package_app.assert_bundle_layout(app)


def test_assert_bundle_layout_requires_artifact_manifest(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, tools, distribution / "bin"):
        directory.mkdir(parents=True)
    create_fake_app_executable_and_resources(app)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)

    with pytest.raises(RuntimeError, match="missing bundled artifact manifest"):
        package_app.assert_bundle_layout(app)


def test_assert_bundle_layout_requires_python_packages(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, tools, distribution / "bin"):
        directory.mkdir(parents=True)
    create_fake_app_executable_and_resources(app)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)

    with pytest.raises(RuntimeError, match="missing bundled Python packages"):
        package_app.assert_bundle_layout(app)


def test_assert_bundle_layout_requires_swift_resource_bundle(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    executable = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, executable.parent, python_packages, tools, distribution / "bin"):
        directory.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    create_fake_python_runtime(app)
    (distribution / "artifact-manifest.json").write_text('{"artifacts":{}}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="missing Swift resource bundle"):
        package_app.assert_bundle_layout(app)


def test_build_swift_creates_universal_binary_with_lipo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["swift", "build"]:
            architecture = cmd[cmd.index("--triple") + 1].split("-", 1)[0]
            executable = package_app.swift_build_dir("release", architecture) / "TimeCapsuleSMB"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text(architecture, encoding="utf-8")
            executable.chmod(0o755)
        if cmd and cmd[0] == "lipo":
            output = Path(cmd[cmd.index("-output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("universal", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(package_app, "run", fake_run)

    executable, resource_build_dir = package_app.build_swift("release", ("arm64", "x86_64"))

    assert executable == tmp_path / ".build" / "package-app" / "release" / "TimeCapsuleSMB"
    assert resource_build_dir == tmp_path / ".build" / "arm64-apple-macosx" / "release"
    assert ["lipo", "-create"] == calls[-1][:2]


def test_remove_optional_zeroconf_extensions_keeps_pure_python_package(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    zeroconf = tmp_path / "site-packages" / "zeroconf"
    nested = zeroconf / "_services"
    nested.mkdir(parents=True)
    py_module = nested / "browser.py"
    extension = nested / "browser.cpython-39-darwin.so"
    py_module.write_text("# pure python fallback\n", encoding="utf-8")
    extension.write_text("arm64 binary", encoding="utf-8")

    package_app.remove_optional_zeroconf_extensions(tmp_path / "site-packages")

    assert py_module.is_file()
    assert not extension.exists()


def test_prune_python_runtime_removes_unused_gui_frameworks(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    framework = tmp_path / "Python.framework"
    version = framework / "Versions" / "3.13"
    current = framework / "Versions" / "Current"
    (version / "bin").mkdir(parents=True)
    (version / "Python").write_text("python", encoding="utf-8")
    (version / "bin" / "python3-intel64").write_text("intel shim", encoding="utf-8")
    dynload = version / "lib" / "python3.13" / "lib-dynload"
    dynload.mkdir(parents=True)
    (dynload / "_tkinter.cpython-313-darwin.so").write_text("tk", encoding="utf-8")
    for relative in (
        "Frameworks/Tcl.framework",
        "Frameworks/Tk.framework",
        "lib/tcl8.6",
        "lib/tk8.6",
        "lib/python3.13/idlelib",
        "lib/python3.13/tkinter",
        "lib/python3.13/test",
    ):
        (version / relative).mkdir(parents=True)
    current.symlink_to(version)

    package_app.prune_python_runtime(framework)

    assert not (version / "bin" / "python3-intel64").exists()
    assert not (version / "Frameworks" / "Tk.framework").exists()
    assert not (version / "lib" / "python3.13" / "tkinter").exists()
    assert not (dynload / "_tkinter.cpython-313-darwin.so").exists()


def test_create_app_icon_reuses_cached_icns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    source = tmp_path / "tcs.jpg"
    source.write_bytes(b"fake jpg")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "sips":
            output = Path(cmd[cmd.index("--out") + 1])
            output.write_text("png", encoding="utf-8")
        elif cmd[0] == "iconutil":
            output = Path(cmd[cmd.index("-o") + 1])
            output.write_text("icns", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(package_app, "run", fake_run)

    first_resources = tmp_path / "FirstResources"
    second_resources = tmp_path / "SecondResources"
    first_resources.mkdir()
    second_resources.mkdir()

    package_app.create_app_icon(source, first_resources)
    assert calls

    calls.clear()
    package_app.create_app_icon(source, second_resources)

    assert calls == []
    assert (second_resources / "TimeCapsuleSMB.icns").read_text(encoding="utf-8") == "icns"


def test_prepared_python_framework_reuses_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    calls: list[Path] = []
    source = tmp_path / "python.pkg"
    source.write_text("pkg", encoding="utf-8")

    def fake_runtime_source(args: object) -> tuple[str, Path, dict[str, object]]:
        return ("pkg", source, {"source_sha256": "pkg"})

    def fake_extract(pkg: Path, destination: Path) -> Path:
        calls.append(destination)
        current = destination / "Versions" / "Current"
        (current / "bin").mkdir(parents=True)
        (current / "Python").write_text("python dylib", encoding="utf-8")
        (current / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
        return destination

    monkeypatch.setattr(package_app, "python_runtime_source", fake_runtime_source)
    monkeypatch.setattr(package_app, "extract_python_framework", fake_extract)
    monkeypatch.setattr(package_app, "prune_python_runtime", lambda framework: None)
    monkeypatch.setattr(package_app, "rewrite_python_framework_install_names", lambda framework: None)
    monkeypatch.setattr(package_app, "assert_macho_has_architectures", lambda path, architectures, label: None)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_python_framework", lambda framework: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid_for_roots", lambda roots: None)

    args = SimpleNamespace()
    first = package_app.prepared_python_framework(args, ("arm64", "x86_64"))
    second = package_app.prepared_python_framework(args, ("arm64", "x86_64"))

    assert first == second
    assert len(calls) == 1
    assert (second / "Versions" / "Current" / "bin" / "python3").is_file()


def test_create_python_packages_reuses_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    cache_entry = tmp_path / "cache" / "site"
    calls: list[Path] = []

    def fake_build(python: str, site_packages: Path) -> None:
        calls.append(site_packages)
        package = site_packages / "timecapsulesmb"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# cached package\n", encoding="utf-8")

    monkeypatch.setattr(package_app, "python_site_packages_cache_entry", lambda python, architectures: cache_entry)
    monkeypatch.setattr(package_app, "build_python_packages", fake_build)

    first_resources = tmp_path / "FirstResources"
    second_resources = tmp_path / "SecondResources"

    package_app.create_python_packages("python3", first_resources, ("arm64",))
    package_app.create_python_packages("python3", second_resources, ("arm64",))

    assert len(calls) == 1
    assert (first_resources / "Python" / "site-packages" / "timecapsulesmb" / "__init__.py").is_file()
    assert (second_resources / "Python" / "site-packages" / "timecapsulesmb" / "__init__.py").is_file()


def test_package_args_do_not_allow_missing_bundled_tools() -> None:
    package_app = load_package_app_module()

    args = package_app.parse_args([])
    assert not hasattr(args, "require_tools")
    assert args.no_cache is False
    assert args.full_validation is False
    assert package_app.parse_args(["--no-cache"]).no_cache is True
    assert package_app.parse_args(["--full-validation"]).full_validation is True
    with pytest.raises(SystemExit):
        package_app.parse_args(["--allow-missing-tools"])


def test_helper_wrapper_uses_bundled_python_runtime(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    helper = tmp_path / "tcapsule"

    package_app.write_helper_wrapper(helper)

    text = helper.read_text(encoding="utf-8")
    assert "Python/Runtime/Python.framework/Versions/Current" in text
    assert 'PYTHON="$PYTHON_HOME/bin/python3"' in text
    assert 'export PYTHONHOME="$PYTHON_HOME"' in text
    assert "certifi/cacert.pem" in text
    assert "SSL_CERT_FILE" in text
    assert "PYTHONDONTWRITEBYTECODE=1" in text
    assert "/usr/bin/python3" not in text


def test_assert_bundle_layout_requires_bundled_ca_certificates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, tools, distribution / "bin"):
        directory.mkdir(parents=True)
    create_fake_app_executable_and_resources(app)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    (distribution / "artifact-manifest.json").write_text('{"artifacts":{}}', encoding="utf-8")
    monkeypatch.setattr(package_app, "artifact_paths", lambda: [])

    with pytest.raises(RuntimeError, match="missing bundled CA certificates"):
        package_app.assert_bundle_layout(app)


def test_assert_bundle_layout_uses_full_macho_validation_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, tools, distribution / "bin"):
        directory.mkdir(parents=True)
    create_fake_app_executable_and_resources(app)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    (distribution / "artifact-manifest.json").write_text('{"artifacts":{}}', encoding="utf-8")
    create_fake_certifi_package(python_packages)
    calls: list[str] = []

    monkeypatch.setattr(package_app, "artifact_paths", lambda: [])
    monkeypatch.setattr(package_app, "assert_macho_has_architectures", lambda path, architectures, label: None)
    monkeypatch.setattr(package_app, "assert_python_extension_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_tool_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_python_dependencies_are_bundled", lambda app: None)
    monkeypatch.setattr(package_app, "validate_app_resources", lambda app: None)
    monkeypatch.setattr(package_app, "assert_runtime_macho_architectures", lambda app, architectures: calls.append("runtime"))
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies", lambda app: calls.append("external"))
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: calls.append("codesign"))

    package_app.assert_bundle_layout(app, architectures=("arm64",))
    assert calls == []

    package_app.assert_bundle_layout(app, architectures=("arm64",), full_validation=True)
    assert calls == ["runtime", "external", "codesign"]


def test_copy_tools_creates_arch_dispatch_wrappers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    sources = tmp_path / "sources"
    sources.mkdir()
    for tool in ("sshpass", "smbclient"):
        for architecture in ("arm64", "x86_64"):
            source = sources / f"{tool}-{architecture}"
            source.write_text(tool, encoding="utf-8")
            source.chmod(0o755)
            monkeypatch.setenv(f"TCAPSULE_PACKAGE_{tool.upper()}_{architecture.upper()}", str(source))

    def fake_architectures(path: Path) -> set[str]:
        if str(path).endswith("-arm64"):
            return {"arm64"}
        if str(path).endswith("-x86_64"):
            return {"x86_64"}
        return set()

    monkeypatch.setattr(package_app, "macho_architectures", fake_architectures)
    monkeypatch.setattr(package_app.shutil, "which", lambda name: None)

    resources = tmp_path / "Resources"
    package_app.copy_tools(resources, ("arm64", "x86_64"))

    tools_bin = resources / "Tools" / "bin"
    assert "arm64) exec" in (tools_bin / "sshpass").read_text(encoding="utf-8")
    assert "x86_64) exec" in (tools_bin / "smbclient").read_text(encoding="utf-8")
    assert (tools_bin / "arm64" / "sshpass").is_file()
    assert (tools_bin / "x86_64" / "smbclient").is_file()


def test_copy_tools_requires_each_architecture_when_requested(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    arm_sshpass = tmp_path / "sshpass-arm64"
    arm_sshpass.write_text("sshpass", encoding="utf-8")
    arm_sshpass.chmod(0o755)
    monkeypatch.setenv("TCAPSULE_PACKAGE_SSHPASS_ARM64", str(arm_sshpass))
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"} if path == arm_sshpass else set())
    monkeypatch.setattr(package_app.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match=r"sshpass \(x86_64\).*smbclient \(arm64\).*smbclient \(x86_64\)"):
        package_app.copy_tools(tmp_path / "Resources", ("arm64", "x86_64"))


def test_copy_native_tools_layer_reuses_cached_vendored_layer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    sources_dir = tmp_path / "sources"
    sshpass = sources_dir / "sshpass"
    smbclient = sources_dir / "smbclient"
    dependency = sources_dir / "libnative.dylib"
    for path in (sshpass, smbclient, dependency):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
        path.chmod(0o755)
    sources = {
        ("sshpass", "arm64"): sshpass,
        ("smbclient", "arm64"): smbclient,
    }
    vendor_calls: list[Path] = []

    def fake_vendor(app: Path) -> set[Path]:
        vendor_calls.append(app)
        frameworks = app / "Contents" / "Frameworks"
        frameworks.mkdir(parents=True, exist_ok=True)
        (frameworks / "libnative.dylib").write_text("vendored", encoding="utf-8")
        return {dependency}

    monkeypatch.setattr(package_app, "resolve_tool_sources", lambda architectures: sources)
    monkeypatch.setattr(package_app, "vendor_macho_dependencies", fake_vendor)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_macho_bundle", lambda app: None)
    monkeypatch.setattr(package_app, "assert_tool_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_runtime_macho_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies", lambda app: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: None)

    first_app = tmp_path / "First.app"
    second_app = tmp_path / "Second.app"
    package_app.copy_native_tools_layer(first_app, ("arm64",))
    package_app.copy_native_tools_layer(second_app, ("arm64",))

    assert len(vendor_calls) == 1
    assert (second_app / "Contents" / "Resources" / "Tools" / "bin" / "smbclient").is_file()
    assert (second_app / "Contents" / "Frameworks" / "libnative.dylib").is_file()


def test_copy_native_tools_layer_rebuilds_when_vendored_input_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    sources_dir = tmp_path / "sources"
    sshpass = sources_dir / "sshpass"
    smbclient = sources_dir / "smbclient"
    dependency = sources_dir / "libnative.dylib"
    for path in (sshpass, smbclient, dependency):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original", encoding="utf-8")
        path.chmod(0o755)
    sources = {
        ("sshpass", "arm64"): sshpass,
        ("smbclient", "arm64"): smbclient,
    }
    vendor_calls: list[Path] = []

    def fake_vendor(app: Path) -> set[Path]:
        vendor_calls.append(app)
        frameworks = app / "Contents" / "Frameworks"
        frameworks.mkdir(parents=True, exist_ok=True)
        (frameworks / "libnative.dylib").write_text("vendored", encoding="utf-8")
        return {dependency}

    monkeypatch.setattr(package_app, "resolve_tool_sources", lambda architectures: sources)
    monkeypatch.setattr(package_app, "vendor_macho_dependencies", fake_vendor)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_macho_bundle", lambda app: None)
    monkeypatch.setattr(package_app, "assert_tool_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_runtime_macho_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies", lambda app: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: None)

    package_app.copy_native_tools_layer(tmp_path / "First.app", ("arm64",))
    dependency.write_text("changed", encoding="utf-8")
    package_app.copy_native_tools_layer(tmp_path / "Second.app", ("arm64",))

    assert len(vendor_calls) == 2


def test_copy_native_tools_layer_rebuilds_when_cached_output_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    sources_dir = tmp_path / "sources"
    sshpass = sources_dir / "sshpass"
    smbclient = sources_dir / "smbclient"
    dependency = sources_dir / "libnative.dylib"
    for path in (sshpass, smbclient, dependency):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original", encoding="utf-8")
        path.chmod(0o755)
    sources = {
        ("sshpass", "arm64"): sshpass,
        ("smbclient", "arm64"): smbclient,
    }
    vendor_calls: list[Path] = []

    def fake_vendor(app: Path) -> set[Path]:
        vendor_calls.append(app)
        frameworks = app / "Contents" / "Frameworks"
        frameworks.mkdir(parents=True, exist_ok=True)
        (frameworks / "libnative.dylib").write_text("vendored", encoding="utf-8")
        return {dependency}

    monkeypatch.setattr(package_app, "resolve_tool_sources", lambda architectures: sources)
    monkeypatch.setattr(package_app, "vendor_macho_dependencies", fake_vendor)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_macho_bundle", lambda app: None)
    monkeypatch.setattr(package_app, "assert_tool_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_runtime_macho_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies", lambda app: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: None)

    package_app.copy_native_tools_layer(tmp_path / "First.app", ("arm64",))
    cache_entry = next((tmp_path / ".build" / "package-app" / "native-tools").iterdir())
    (cache_entry / "Contents" / "Frameworks" / "libnative.dylib").write_text("corrupt", encoding="utf-8")
    package_app.copy_native_tools_layer(tmp_path / "Second.app", ("arm64",))

    assert len(vendor_calls) == 2


def test_vendor_macho_dependencies_rewrites_loader_path_to_matching_source_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    arm_tool = tools / "arm64" / "smbclient"
    x86_tool = tools / "x86_64" / "smbclient"
    for tool in (arm_tool, x86_tool):
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("tool", encoding="utf-8")
        tool.chmod(0o755)

    sources = tmp_path / "sources"
    arm_i18n = sources / "arm64" / "libicui18n.78.dylib"
    arm_icuuc = sources / "arm64" / "libicuuc.78.dylib"
    arm_icudata = sources / "arm64" / "libicudata.78.dylib"
    x86_i18n = sources / "x86_64" / "libicui18n.78.dylib"
    x86_icuuc = sources / "x86_64" / "libicuuc.78.dylib"
    x86_icudata = sources / "x86_64" / "libicudata.78.dylib"
    for source in (arm_i18n, arm_icuuc, arm_icudata, x86_i18n, x86_icuuc, x86_icudata):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(source.parent.name, encoding="utf-8")

    def fake_dependencies(path: Path) -> list[str] | None:
        resolved = path.resolve()
        if resolved == arm_tool.resolve():
            return [str(arm_i18n)]
        if resolved == x86_tool.resolve():
            return [str(x86_i18n)]
        if path.name.startswith("libicui18n"):
            return ["@loader_path/libicuuc.78.dylib", "@loader_path/libicudata.78.dylib"]
        if path.name.startswith("libicuuc"):
            return ["@loader_path/libicudata.78.dylib"]
        return []

    changes: list[list[str]] = []

    def fake_run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        changes.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(package_app, "macho_dependencies", fake_dependencies)
    monkeypatch.setattr(package_app, "run_quiet", fake_run_quiet)
    monkeypatch.setattr(package_app, "set_macho_id_if_supported", lambda path: None)

    package_app.vendor_macho_dependencies(app)

    frameworks = app / "Contents" / "Frameworks"
    assert (frameworks / "libicuuc.78.dylib").is_file()
    x86_icuuc_bundle = next(frameworks.glob("libicuuc-*.78.dylib"))
    x86_icudata_bundle = next(frameworks.glob("libicudata-*.78.dylib"))
    x86_i18n_bundle = next(frameworks.glob("libicui18n-*.78.dylib"))

    assert any(
        cmd[0:3] == ["install_name_tool", "-change", "@loader_path/libicuuc.78.dylib"]
        and cmd[3] == f"@loader_path/{x86_icuuc_bundle.name}"
        and cmd[-1] == str(x86_i18n_bundle)
        for cmd in changes
    )
    assert any(
        cmd[0:3] == ["install_name_tool", "-change", "@loader_path/libicudata.78.dylib"]
        and cmd[3] == f"@loader_path/{x86_icudata_bundle.name}"
        and cmd[-1] == str(x86_icuuc_bundle)
        for cmd in changes
    )


def test_ad_hoc_codesign_macho_bundle_signs_only_macho_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    macho = app / "Contents" / "Resources" / "Tools" / "bin" / "smbclient"
    script = tmp_path / "wrapper"
    macho.parent.mkdir(parents=True)
    macho.write_text("macho", encoding="utf-8")
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [macho, script])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"} if path == macho else set())

    def fake_run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(package_app, "run_quiet", fake_run_quiet)

    package_app.ad_hoc_codesign_macho_bundle(app)

    assert calls == [["codesign", "--force", "--sign", "-", str(macho)]]


def test_ad_hoc_codesign_macho_bundle_does_not_sign_app_executable_as_nested_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    executable = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    library = app / "Contents" / "Frameworks" / "libtool.dylib"
    tool = app / "Contents" / "Resources" / "Tools" / "bin" / "smbclient"
    for path in (executable, library, tool):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("macho", encoding="utf-8")
    calls: list[Path] = []

    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [executable, tool, library])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})
    monkeypatch.setattr(package_app, "ad_hoc_codesign", lambda path: calls.append(path))

    package_app.ad_hoc_codesign_macho_bundle(app)

    assert executable not in calls
    assert library in calls
    assert tool in calls


def test_ad_hoc_codesign_macho_bundle_signs_python_framework_last(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    python_binary = app / "Contents" / "Resources" / "Python" / "Runtime" / "Python.framework" / "Versions" / "3.13" / "Python"
    framework = app / "Contents" / "Resources" / "Python" / "Runtime" / "Python.framework"
    python_binary.parent.mkdir(parents=True)
    python_binary.write_text("python", encoding="utf-8")
    calls: list[Path] = []

    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [python_binary])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})
    monkeypatch.setattr(package_app, "ad_hoc_codesign", lambda path: calls.append(path))

    package_app.ad_hoc_codesign_macho_bundle(app)

    assert calls == [python_binary, framework]


def test_assert_macho_code_signatures_valid_reports_invalid_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    macho = app / "Contents" / "Resources" / "Tools" / "bin" / "smbclient"
    macho.parent.mkdir(parents=True)
    macho.write_text("macho", encoding="utf-8")

    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [macho])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="invalid signature\n")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="invalid Mach-O code signature"):
        package_app.assert_macho_code_signatures_valid(app)


def test_macho_files_under_skips_symlink_aliases(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    root = tmp_path / "root"
    root.mkdir()
    real = root / "libcrypto.3.dylib"
    alias = root / "libcrypto.dylib"
    real.write_text("macho", encoding="utf-8")
    alias.symlink_to(real.name)

    paths = package_app.macho_files_under([root])

    assert real in paths
    assert alias not in paths


def test_runtime_macho_architecture_validation_checks_internal_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    executable = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    dependency = app / "Contents" / "Frameworks" / "libtool.dylib"
    executable.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    executable.write_text("app", encoding="utf-8")
    dependency.write_text("dependency", encoding="utf-8")

    def fake_architectures(path: Path) -> set[str]:
        if path.resolve() == executable.resolve():
            return {"arm64", "x86_64"}
        if path.resolve() == dependency.resolve():
            return {"arm64"}
        return set()

    def fake_dependencies(path: Path) -> list[str] | None:
        if path.resolve() == executable.resolve():
            return ["@loader_path/../Frameworks/libtool.dylib"]
        return []

    monkeypatch.setattr(package_app, "macho_architectures", fake_architectures)
    monkeypatch.setattr(package_app, "macho_dependencies", fake_dependencies)

    with pytest.raises(RuntimeError, match=r"libtool\.dylib: missing x86_64"):
        package_app.assert_runtime_macho_architectures(app, ("arm64", "x86_64"))


def test_python_dependency_validation_uses_bundled_python(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    create_fake_app_executable_and_resources(app)
    site_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    site_packages.mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs["env"]))  # type: ignore[index]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)

    package_app.assert_python_dependencies_are_bundled(app)

    assert calls
    cmd, env = calls[0]
    assert cmd[0] == str(package_app.bundled_python_executable(app))
    assert env["PYTHONHOME"] == str(package_app.bundled_python_home(app))
    assert env["PYTHONPATH"] == str(site_packages)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_validate_app_resources_rejects_swift_resource_bundle_crash(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    executable = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho resource crash >&2\nexit 70\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(RuntimeError, match="App executable resource validation failed"):
        package_app.validate_app_resources(app)
