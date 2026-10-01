from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
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


PLURALS_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict></dict></plist>
"""


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
    (resource_bundle / "Localizable.stringsdict").write_text(PLURALS_PLIST, encoding="utf-8")


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


def test_assert_bundle_layout_requires_plural_localizations(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, tools, distribution / "bin"):
        directory.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    create_fake_app_executable_and_resources(app)
    create_fake_certifi_package(python_packages)
    (distribution / "artifact-manifest.json").write_text('{"artifacts":{}}', encoding="utf-8")
    plurals = app / "Contents" / "Resources" / package_app.RESOURCE_BUNDLE_NAME / "en.lproj" / "Localizable.stringsdict"
    plurals.unlink()

    with pytest.raises(RuntimeError, match="missing Swift resource bundle plural localizations"):
        package_app.assert_bundle_layout(app)


def app_with_deep_resource_bundle(package_app, tmp_path: Path) -> Path:
    """A packaged app whose Swift resource bundle has Swift Build's layout."""
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, tools, distribution / "bin"):
        directory.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    create_fake_app_executable_and_resources(app)
    create_fake_certifi_package(python_packages)
    (distribution / "artifact-manifest.json").write_text('{"artifacts":{}}', encoding="utf-8")
    bundle = app / "Contents" / "Resources" / package_app.RESOURCE_BUNDLE_NAME
    deep = bundle / "Contents" / "Resources"
    deep.mkdir(parents=True)
    (bundle / "en.lproj").rename(deep / "en.lproj")
    return app


def test_assert_bundle_layout_accepts_the_swift_build_resource_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_app = load_package_app_module()
    app = app_with_deep_resource_bundle(package_app, tmp_path)
    # Only the resource bundle is under test; the checks after it are stubbed
    # as in test_assert_bundle_layout_checks_helper_python_tools_and_artifacts.
    monkeypatch.setattr(package_app, "artifact_paths", lambda: [])
    monkeypatch.setattr(package_app, "assert_python_dependencies_are_bundled", lambda app: None)
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies", lambda app: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: None)
    monkeypatch.setattr(package_app, "validate_app_resources", lambda app: None)

    package_app.assert_bundle_layout(app)


def test_assert_bundle_layout_requires_plurals_in_the_swift_build_resource_bundle(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = app_with_deep_resource_bundle(package_app, tmp_path)
    plurals = (app / "Contents" / "Resources" / package_app.RESOURCE_BUNDLE_NAME / "Contents" / "Resources"
               / "en.lproj" / "Localizable.stringsdict")
    plurals.unlink()

    with pytest.raises(RuntimeError, match="missing Swift resource bundle plural localizations"):
        package_app.assert_bundle_layout(app)


def fake_swift_build(package_app, root: Path, layout: str, calls: list[list[str]], lipo_inputs: dict[str, str]):
    """swift build as each build system lays out its products: the native one
    in <arch>-apple-macosx/release, Swift Build in one out/Products/Release that
    every architecture's build overwrites."""

    def bin_dir(architecture: str) -> Path:
        if layout == "native":
            return root / ".build" / f"{architecture}-apple-macosx" / "release"
        return root / ".build" / "out" / "Products" / "Release"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["swift", "build"]:
            architecture = cmd[cmd.index("--triple") + 1].split("-", 1)[0]
            if "--show-bin-path" in cmd:
                assert kwargs.get("capture") is True
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{bin_dir(architecture)}\n")
            product = cmd[cmd.index("--product") + 1]
            executable = bin_dir(architecture) / product
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text(f"{product} {architecture}", encoding="utf-8")
            executable.chmod(0o755)
        if cmd and cmd[0] == "lipo":
            inputs = cmd[cmd.index("-create") + 1:cmd.index("-output")]
            lipo_inputs.update({path: Path(path).read_text(encoding="utf-8") for path in inputs})
            output = Path(cmd[cmd.index("-output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("universal", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    return fake_run


@pytest.mark.parametrize("layout", ["native", "swiftbuild"])
def test_build_swift_lipos_each_architectures_own_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, layout: str
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    calls: list[list[str]] = []
    lipo_inputs: dict[str, str] = {}
    monkeypatch.setattr(package_app, "run", fake_swift_build(package_app, tmp_path, layout, calls, lipo_inputs))

    executable, resource_build_dir = package_app.build_swift("release", ("arm64", "x86_64"))

    assert executable == tmp_path / ".build" / "package-app" / "release" / "TimeCapsuleSMB"
    # Each architecture's binary went into lipo, even where the second build
    # replaced the first in Swift Build's shared directory.
    assert sorted(lipo_inputs.values()) == ["TimeCapsuleSMB arm64", "TimeCapsuleSMB x86_64"]
    expected = (tmp_path / ".build" / "arm64-apple-macosx" / "release" if layout == "native"
                else tmp_path / ".build" / "out" / "Products" / "Release")
    assert resource_build_dir == expected
    assert ["lipo", "-create"] == calls[-1][:2]


def test_build_swift_ignores_a_stale_native_build_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A native build from before the switch to Swift Build is still on disk;
    # packaging it shipped a months-old app and a bundle without plurals.
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    stale = tmp_path / ".build" / "arm64-apple-macosx" / "release" / "TimeCapsuleSMB"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")
    calls: list[list[str]] = []
    lipo_inputs: dict[str, str] = {}
    monkeypatch.setattr(package_app, "run", fake_swift_build(package_app, tmp_path, "swiftbuild", calls, lipo_inputs))

    executable, resource_build_dir = package_app.build_swift("release", ("arm64",))

    assert executable.read_text(encoding="utf-8") == "TimeCapsuleSMB arm64"
    assert resource_build_dir == tmp_path / ".build" / "out" / "Products" / "Release"


def test_build_swift_reports_a_product_swift_build_did_not_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--show-bin-path" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{tmp_path / 'out'}\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(package_app, "run", fake_run)
    with pytest.raises(RuntimeError, match="did not produce"):
        package_app.build_swift("release", ("arm64",))


def test_build_helper_creates_universal_helper_with_lipo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    calls: list[list[str]] = []
    lipo_inputs: dict[str, str] = {}
    monkeypatch.setattr(package_app, "run", fake_swift_build(package_app, tmp_path, "swiftbuild", calls, lipo_inputs))

    executable = package_app.build_helper("release", ("arm64", "x86_64"))

    assert executable == tmp_path / ".build" / "package-app" / "release" / "tcapsule"
    assert ["swift", "build"] == calls[0][:2]
    assert calls[0][calls[0].index("--product") + 1] == "tcapsule"
    assert sorted(lipo_inputs.values()) == ["tcapsule arm64", "tcapsule x86_64"]
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
    # This cache test runs on Linux CI; Mach-O validators are covered separately
    # and shell out to macOS tools such as lipo and otool.
    monkeypatch.setattr(package_app, "assert_macho_has_architectures", lambda path, architectures, label: None)
    monkeypatch.setattr(package_app, "assert_macho_architectures_for_roots", lambda roots, architectures, label: None)
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies_for_roots", lambda roots: None)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_python_framework", lambda framework: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid_for_roots", lambda roots: None)

    args = SimpleNamespace()
    first = package_app.prepared_python_framework(args, ("arm64", "x86_64"))
    second = package_app.prepared_python_framework(args, ("arm64", "x86_64"))

    assert first == second
    assert len(calls) == 1
    assert (second / "Versions" / "Current" / "bin" / "python3").is_file()


def assert_no_python_bytecode(root: Path) -> None:
    assert not list(root.rglob("__pycache__"))
    assert not list(root.rglob("*.pyc"))
    assert not list(root.rglob("*.pyo"))


def create_python_bytecode(root: Path) -> None:
    pycache = root / "timecapsulesmb" / "__pycache__"
    pycache.mkdir(parents=True, exist_ok=True)
    (pycache / "__init__.cpython-313.pyc").write_bytes(b"pyc")
    (root / "timecapsulesmb" / "stale.pyo").write_bytes(b"pyo")


def test_python_subprocess_env_disables_bytecode_and_redirects_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)

    env = package_app.python_subprocess_env(
        {"PYTHONDONTWRITEBYTECODE": "0", "PYTHONNOUSERSITE": "0"},
        python_home=tmp_path / "Python.framework" / "Versions" / "Current",
    )

    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONHOME"] == str(tmp_path / "Python.framework" / "Versions" / "Current")
    assert env["PYTHONPYCACHEPREFIX"] == str(tmp_path / ".build" / "package-app" / "python-bytecode")


def test_build_python_packages_uses_bytecode_safe_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs["env"]))  # type: ignore[index]
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(package_app, "python_major_minor", lambda python: (3, 13))
    monkeypatch.setattr(package_app, "run", fake_run)
    monkeypatch.setattr(package_app, "remove_optional_zeroconf_extensions", lambda site_packages: None)

    package_app.build_python_packages("python3", tmp_path / "site-packages")

    assert len(calls) == 4
    for _cmd, env in calls:
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["PYTHONNOUSERSITE"] == "1"
        assert Path(env["PYTHONPYCACHEPREFIX"]).name == "pycache"


def test_remove_python_bytecode_removes_nested_pycache_and_orphans(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    root = tmp_path / "site-packages"
    package = root / "timecapsulesmb"
    create_python_bytecode(root)
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")

    package_app.remove_python_bytecode(root)

    assert (package / "module.py").is_file()
    assert_no_python_bytecode(root)


def test_remove_appledouble_files_removes_metadata_sidecars(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    root = tmp_path / "TimeCapsuleSMB.app"
    normal = root / "Contents" / "Resources" / "Python"
    sidecar = root / "Contents" / "Resources" / "._Python"
    nested_sidecar_dir = root / "Contents" / "Resources" / "._Metadata"
    normal.mkdir(parents=True)
    sidecar.write_text("appledouble", encoding="utf-8")
    nested_sidecar_dir.mkdir()
    (nested_sidecar_dir / "file").write_text("metadata", encoding="utf-8")

    package_app.remove_appledouble_files(root)

    assert normal.is_dir()
    assert not sidecar.exists()
    assert not nested_sidecar_dir.exists()
    package_app.assert_no_appledouble_files(root)


def test_assert_no_appledouble_files_reports_nested_sidecars(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    sidecar = tmp_path / "TimeCapsuleSMB.app" / "Contents" / "Resources" / "._Python"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("appledouble", encoding="utf-8")

    with pytest.raises(RuntimeError, match="AppleDouble metadata files"):
        package_app.assert_no_appledouble_files(tmp_path / "TimeCapsuleSMB.app")


def test_create_python_packages_reuses_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    cache_entry = tmp_path / "cache" / "site"
    calls: list[Path] = []

    def fake_build(python: str, site_packages: Path) -> None:
        calls.append(site_packages)
        package = site_packages / "timecapsulesmb"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# cached package\n", encoding="utf-8")
        create_python_bytecode(site_packages)

    monkeypatch.setattr(package_app, "python_site_packages_cache_entry", lambda python, architectures: cache_entry)
    monkeypatch.setattr(package_app, "build_python_packages", fake_build)

    first_resources = tmp_path / "FirstResources"
    second_resources = tmp_path / "SecondResources"

    package_app.create_python_packages("python3", first_resources, ("arm64",))
    package_app.create_python_packages("python3", second_resources, ("arm64",))

    assert len(calls) == 1
    assert (first_resources / "Python" / "site-packages" / "timecapsulesmb" / "__init__.py").is_file()
    assert (second_resources / "Python" / "site-packages" / "timecapsulesmb" / "__init__.py").is_file()
    assert_no_python_bytecode(first_resources / "Python" / "site-packages")
    assert_no_python_bytecode(second_resources / "Python" / "site-packages")
    assert_no_python_bytecode(cache_entry / "site-packages")


def make_cache_entries(root: Path, names: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for age, name in enumerate(reversed(names)):
        path = root / name
        if name.endswith(".icns"):
            path.write_text("icns", encoding="utf-8")
        else:
            path.mkdir()
            (path / ".complete").write_text("ok\n", encoding="utf-8")
        stamp = 1_000_000 + age
        os.utime(path, (stamp, stamp))


def test_evict_stale_cache_entries_keeps_most_recent_and_marks_current_used(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    root = tmp_path / "python-site-packages"
    # Newest first; "current" is the oldest but is the entry in use now.
    make_cache_entries(root, ["new1", "new2", "new3", "old1", "old2.icns", "current"])

    entry = package_app.evict_stale_cache_entries(root / "current", keep=4)

    assert entry == root / "current"
    assert sorted(path.name for path in root.iterdir()) == ["current", "new1", "new2", "new3"]
    assert (root / "current").stat().st_mtime > (root / "new1").stat().st_mtime


def test_evict_stale_cache_entries_counts_an_entry_not_built_yet(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    root = tmp_path / "native-tools"
    make_cache_entries(root, ["new1", "new2", "new3", "old1"])

    package_app.evict_stale_cache_entries(root / "missing", keep=4)

    assert sorted(path.name for path in root.iterdir()) == ["new1", "new2", "new3"]
    assert not (root / "missing").exists()


def test_evict_stale_cache_entries_leaves_builds_in_progress(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    root = tmp_path / "python-framework"
    make_cache_entries(root, ["current", "old1", "abc.tmp-123"])

    package_app.evict_stale_cache_entries(root / "current", keep=1)

    assert sorted(path.name for path in root.iterdir()) == ["abc.tmp-123", "current"]


def test_evict_stale_cache_entries_keeps_everything_under_the_limit(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    root = tmp_path / "app-icon"
    make_cache_entries(root, ["a.icns", "b.icns"])

    package_app.evict_stale_cache_entries(root / "c.icns")

    assert sorted(path.name for path in root.iterdir()) == ["a.icns", "b.icns"]


PACKAGER_CHILD = """
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("package_app", sys.argv[1])
package_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_app)
package_app.PACKAGE_ROOT = Path(sys.argv[2])
cache = package_app.package_cache_dir("native-tools")
role = sys.argv[3]
with package_app.package_cache_lock():
    if role == "user":
        # Stand in for a run that found its cached entry and is copying it.
        print("using", flush=True)
        sys.stdin.readline()
        print("intact" if (cache / "a").exists() else "gone", flush=True)
    elif role == "holder":
        print("locked", flush=True)
        sys.stdin.readline()
    else:
        package_app.evict_stale_cache_entries(cache / "new", keep=1)
        print("evicted", flush=True)
"""


def start_packager(tmp_path: Path, role: str) -> subprocess.Popen[str]:
    child = tmp_path / "packager_child.py"
    child.write_text(PACKAGER_CHILD, encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(child), str(PACKAGE_SCRIPT), str(tmp_path), role],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def package_cache_lock_is_held(root: Path) -> bool:
    # flock conflicts between separate open files even within one process.
    with (root / ".build" / "package-app" / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def test_concurrent_packager_cannot_evict_an_entry_another_run_is_using(tmp_path: Path) -> None:
    # Packager A found cached entry "a" and is copying from it. Packager B
    # needs a different key and would evict "a" as the oldest entry: it must
    # wait until A has finished, not delete A's input underneath it.
    make_cache_entries(tmp_path / ".build" / "package-app" / "native-tools", ["b", "c", "a"])
    user = start_packager(tmp_path, "user")
    evictor = None
    try:
        assert user.stdout.readline() == "using\n"
        evictor = start_packager(tmp_path, "evictor")
        assert evictor.stderr.readline() == "Waiting for another packaging run to finish with the package cache.\n"

        user.stdin.write("done\n")
        user.stdin.flush()
        assert user.stdout.readline() == "intact\n"
        assert user.wait(timeout=30) == 0

        # Once A releases the cache, B's eviction goes ahead as usual.
        assert evictor.stdout.readline() == "evicted\n"
        assert evictor.wait(timeout=30) == 0
    finally:
        for process in (user, evictor):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
    assert sorted(path.name for path in (tmp_path / ".build" / "package-app" / "native-tools").iterdir()) == []


def test_package_cache_lock_is_released_when_its_holder_dies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    holder = start_packager(tmp_path, "holder")
    try:
        assert holder.stdout.readline() == "locked\n"
        assert package_cache_lock_is_held(tmp_path)
    finally:
        holder.kill()
        holder.wait()

    with package_app.package_cache_lock():
        assert package_cache_lock_is_held(tmp_path)
    assert not package_cache_lock_is_held(tmp_path)
    assert "Waiting" not in capsys.readouterr().err


def test_package_cache_lock_does_not_touch_cache_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The lock file lives beside the cache kinds, where eviction never looks.
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    root = tmp_path / ".build" / "package-app" / "native-tools"
    make_cache_entries(root, ["old"])

    with package_app.package_cache_lock():
        package_app.evict_stale_cache_entries(root / "new", keep=1)

    assert (tmp_path / ".build" / "package-app" / ".lock").is_file()
    assert list(root.iterdir()) == []


def test_site_packages_cache_evicts_entries_left_by_older_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(package_app, "python_cache_identity", lambda python: {"python": python})
    source = {"revision": 0}
    monkeypatch.setattr(package_app, "python_package_source_fingerprint", lambda: dict(source))

    def fake_build(python: str, site_packages: Path) -> None:
        (site_packages / "timecapsulesmb").mkdir(parents=True)

    monkeypatch.setattr(package_app, "build_python_packages", fake_build)
    monkeypatch.setattr(package_app, "assert_macho_architectures_for_roots", lambda *args: None)
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies_for_roots", lambda roots: None)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_site_packages", lambda path: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid_for_roots", lambda roots: None)

    entries = []
    for revision in range(6):
        source["revision"] = revision
        package_app.create_python_packages("python3", tmp_path / f"Resources{revision}", ("arm64",))
        entry = package_app.python_site_packages_cache_entry("python3", ("arm64",))
        entries.append(entry)
        stamp = 2_000_000 + revision
        os.utime(entry, (stamp, stamp))

    cache_root = tmp_path / ".build" / "package-app" / "python-site-packages"
    assert sorted(path.name for path in cache_root.iterdir()) == sorted(entry.name for entry in entries[-4:])
    assert (entries[-1] / "site-packages" / "timecapsulesmb").is_dir()


def test_create_python_packages_cleans_bytecode_from_existing_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    cache_entry = tmp_path / "cache" / "site"
    cached_site_packages = cache_entry / "site-packages"
    package = cached_site_packages / "timecapsulesmb"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# cached package\n", encoding="utf-8")
    create_python_bytecode(cached_site_packages)
    (cache_entry / ".complete").write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(package_app, "python_site_packages_cache_entry", lambda python, architectures: cache_entry)
    monkeypatch.setattr(package_app, "build_python_packages", lambda python, site_packages: pytest.fail("cache was not reused"))

    resources = tmp_path / "Resources"
    package_app.create_python_packages("python3", resources, ("arm64",))

    assert (resources / "Python" / "site-packages" / "timecapsulesmb" / "__init__.py").is_file()
    assert_no_python_bytecode(resources / "Python" / "site-packages")


def test_finalize_python_bundle_cleans_before_resigning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    resources = tmp_path / "Resources"
    framework = resources / "Python" / "Runtime" / "Python.framework"
    site_packages = resources / "Python" / "site-packages"
    framework.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    create_python_bytecode(framework)
    create_python_bytecode(site_packages)
    calls: list[str] = []

    def fake_sign_framework(path: Path) -> None:
        assert path == framework
        assert_no_python_bytecode(resources)
        calls.append("framework")

    def fake_sign_site_packages(path: Path) -> None:
        assert path == site_packages
        assert_no_python_bytecode(resources)
        calls.append("site-packages")

    monkeypatch.setattr(package_app, "ad_hoc_codesign_python_framework", fake_sign_framework)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_site_packages", fake_sign_site_packages)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid_for_roots", lambda roots: calls.append("verify"))

    package_app.finalize_python_bundle(resources)

    assert calls == ["framework", "site-packages", "verify"]
    assert_no_python_bytecode(resources)


def test_package_args_do_not_allow_missing_bundled_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TCAPSULE_CODESIGN_IDENTITY", raising=False)
    monkeypatch.delenv("TCAPSULE_NOTARY_PROFILE", raising=False)
    monkeypatch.delenv("TCAPSULE_NOTARY_TIMEOUT", raising=False)
    package_app = load_package_app_module()

    args = package_app.parse_args([])
    assert not hasattr(args, "require_tools")
    assert args.no_cache is False
    assert args.full_validation is False
    assert args.zip is False
    assert args.zip_output is None
    assert args.codesign_identity is None
    assert args.notarize is False
    assert args.notary_profile == "tcapsulesmb-notary"
    assert args.notary_timeout == "30m"
    assert package_app.parse_args(["--no-cache"]).no_cache is True
    assert package_app.parse_args(["--full-validation"]).full_validation is True
    assert package_app.parse_args(["--zip"]).zip is True
    notarize_args = package_app.parse_args([
        "--notarize",
        "--codesign-identity",
        "Developer ID Application: Example (TEAMID)",
        "--notary-profile",
        "release-profile",
        "--notary-timeout",
        "45m",
    ])
    assert notarize_args.notarize is True
    assert notarize_args.codesign_identity == "Developer ID Application: Example (TEAMID)"
    assert notarize_args.notary_profile == "release-profile"
    assert notarize_args.notary_timeout == "45m"
    with pytest.raises(SystemExit):
        package_app.parse_args(["--notarize", "--no-notarize"])
    zip_args = package_app.parse_args(["--zip-output", "release.zip"])
    assert zip_args.zip_output == Path("release.zip")
    with pytest.raises(SystemExit):
        package_app.parse_args(["--allow-missing-tools"])


def test_copy_helper_executable_preserves_bundled_helper_path(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    source = tmp_path / "build" / "tcapsule"
    destination = tmp_path / "TimeCapsuleSMB.app" / "Contents" / "Helpers" / "tcapsule"
    source.parent.mkdir(parents=True)
    source.write_text("mach-o helper", encoding="utf-8")
    source.chmod(0o644)

    package_app.copy_helper_executable(source, destination)

    assert destination.read_text(encoding="utf-8") == "mach-o helper"
    assert destination.stat().st_mode & 0o777 == 0o755


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
    architecture_labels: list[str] = []

    monkeypatch.setattr(package_app, "artifact_paths", lambda: [])
    monkeypatch.setattr(package_app, "assert_macho_has_architectures", lambda path, architectures, label: architecture_labels.append(label))
    monkeypatch.setattr(package_app, "assert_python_extension_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_tool_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_python_dependencies_are_bundled", lambda app: None)
    monkeypatch.setattr(package_app, "validate_app_resources", lambda app: None)
    monkeypatch.setattr(package_app, "assert_runtime_macho_architectures", lambda app, architectures: calls.append("runtime"))
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies", lambda app: calls.append("external"))
    monkeypatch.setattr(package_app, "assert_macho_minimum_macos", lambda paths: calls.append("minimum-macos"))
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: calls.append("codesign"))
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: calls.append("app-codesign"))

    package_app.assert_bundle_layout(app, architectures=("arm64",))
    assert architecture_labels == [
        "App executable",
        "Helper executable",
        "Bundled Python executable",
        "Bundled Python framework",
    ]
    assert calls == []

    architecture_labels.clear()
    package_app.assert_bundle_layout(app, architectures=("arm64",), full_validation=True)
    assert calls == ["runtime", "external", "minimum-macos", "codesign", "app-codesign"]


def test_copy_tools_from_sources_creates_arch_dispatch_wrappers(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    sources: dict[tuple[str, str], Path] = {}
    for tool in ("sshpass", "smbclient"):
        for architecture in ("arm64", "x86_64"):
            source = tmp_path / "kegs" / architecture / tool
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f"{tool} {architecture}", encoding="utf-8")
            sources[(tool, architecture)] = source

    resources = tmp_path / "Resources"
    copies = package_app.copy_tools_from_sources(resources, ("arm64", "x86_64"), sources)

    tools_bin = resources / "Tools" / "bin"
    assert "arm64) exec" in (tools_bin / "sshpass").read_text(encoding="utf-8")
    assert "x86_64) exec" in (tools_bin / "smbclient").read_text(encoding="utf-8")
    assert (tools_bin / "x86_64" / "smbclient").read_text(encoding="utf-8") == "smbclient x86_64"
    # Vendoring needs each copy's bottle file and architecture.
    assert copies == {tools_bin / architecture / tool: (sources[(tool, architecture)], architecture)
                      for tool in ("sshpass", "smbclient") for architecture in ("arm64", "x86_64")}
    assert all(os.access(copy, os.X_OK) for copy in copies)


def test_copy_tools_from_sources_copies_one_architecture_without_wrappers(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    sources = {}
    for tool in ("sshpass", "smbclient"):
        sources[(tool, "arm64")] = tmp_path / tool
        sources[(tool, "arm64")].write_text(tool, encoding="utf-8")

    copies = package_app.copy_tools_from_sources(tmp_path / "Resources", ("arm64",), sources)

    tools_bin = tmp_path / "Resources" / "Tools" / "bin"
    assert copies == {tools_bin / tool: (sources[(tool, "arm64")], "arm64") for tool in ("sshpass", "smbclient")}
    assert not (tools_bin / "arm64").exists()


def fake_bottles(package_app, tmp_path: Path, architectures=("arm64",), *, revision: str = "0"):
    """Unpacked kegs for the pinned tools, as prepare_homebrew_bottles returns them."""
    records: dict[str, list[dict[str, object]]] = {}
    kegs: dict[str, dict[str, Path]] = {}
    blobs: list[Path] = []
    for architecture in architectures:
        kegs[architecture] = {}
        records[architecture] = []
        for formula, version in package_app.HOMEBREW_BOTTLE_ROOTS.items():
            keg = tmp_path / "kegs" / architecture / formula / version
            (keg / "bin").mkdir(parents=True, exist_ok=True)
            for tool, tool_formula in package_app.HOMEBREW_TOOL_FORMULAE.items():
                if tool_formula == formula:
                    (keg / "bin" / tool).write_text(f"{tool} {architecture}", encoding="utf-8")
            kegs[architecture][formula] = keg
            blob = tmp_path / "blobs" / f"{architecture}-{formula}.tar.gz"
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_text(f"{formula} {revision}", encoding="utf-8")
            blobs.append(blob)
            records[architecture].append({"formula": formula, "version": version, "sha256": revision * 64})
    return package_app.HomebrewBottles(records, kegs, blobs)


def test_homebrew_bottles_tool_is_the_formulas_bin_entry(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    bottles = fake_bottles(package_app, tmp_path, ("arm64", "x86_64"))

    assert package_app.bottle_tool_sources(bottles, ("x86_64",)) == {
        ("sshpass", "x86_64"): tmp_path / "kegs" / "x86_64" / "sshpass" / "1.10" / "bin" / "sshpass",
        ("smbclient", "x86_64"): tmp_path / "kegs" / "x86_64" / "samba" / "4.24.6" / "bin" / "smbclient",
    }
    (tmp_path / "kegs" / "arm64" / "samba" / "4.24.6" / "bin" / "smbclient").unlink()
    with pytest.raises(RuntimeError, match="Bottle of samba has no bin/smbclient"):
        bottles.tool("smbclient", "arm64")


def native_layer_fixture(package_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    state = SimpleNamespace(bottles=fake_bottles(package_app, tmp_path), vendor_calls=[], prepared=[])
    dependency = tmp_path / "kegs" / "arm64" / "samba" / "4.24.6" / "lib" / "libnative.dylib"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("original", encoding="utf-8")
    state.dependency = dependency

    def fake_prepare(architectures, *, use_cache=True):
        state.prepared.append((architectures, use_cache))
        return state.bottles

    def fake_vendor(app: Path, *, kegs, tool_sources) -> set[Path]:
        state.vendor_calls.append((app, kegs, tool_sources))
        frameworks = app / "Contents" / "Frameworks"
        frameworks.mkdir(parents=True, exist_ok=True)
        (frameworks / "libnative.dylib").write_text("vendored", encoding="utf-8")
        return {dependency}

    monkeypatch.setattr(package_app, "prepare_homebrew_bottles", fake_prepare)
    monkeypatch.setattr(package_app, "vendor_macho_dependencies", fake_vendor)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_macho_bundle", lambda app: None)
    monkeypatch.setattr(package_app, "assert_tool_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_runtime_macho_architectures", lambda app, architectures: None)
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies", lambda app: None)
    monkeypatch.setattr(package_app, "assert_macho_minimum_macos", lambda paths: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: None)
    return state


def test_copy_native_tools_layer_bundles_the_bottle_tools_and_reuses_the_layer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_app = load_package_app_module()
    state = native_layer_fixture(package_app, monkeypatch, tmp_path)
    # Nothing from the packaging Mac's PATH or Homebrew may be bundled.
    monkeypatch.setattr(package_app.shutil, "which", lambda name: pytest.fail(f"looked up {name} on PATH"))

    package_app.copy_native_tools_layer(tmp_path / "First.app", ("arm64",))
    capsys.readouterr()
    package_app.copy_native_tools_layer(tmp_path / "Second.app", ("arm64",))
    captured = capsys.readouterr()

    assert len(state.vendor_calls) == 1
    _, kegs, tool_sources = state.vendor_calls[0]
    assert kegs == {"arm64": state.bottles.kegs["arm64"]}
    assert sorted((source.name, architecture) for source, architecture in tool_sources.values()) == [
        ("smbclient", "arm64"), ("sshpass", "arm64")]
    assert "Using cached native tool layer." in captured.err
    tools = tmp_path / "Second.app" / "Contents" / "Resources" / "Tools" / "bin"
    assert (tools / "smbclient").read_text(encoding="utf-8") == "smbclient arm64"
    assert (tmp_path / "Second.app" / "Contents" / "Frameworks" / "libnative.dylib").is_file()
    assert state.prepared == [(("arm64",), True), (("arm64",), True)]


def test_copy_native_tools_layer_rebuilds_for_other_bottles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    state = native_layer_fixture(package_app, monkeypatch, tmp_path)

    package_app.copy_native_tools_layer(tmp_path / "First.app", ("arm64",))
    # Another rebuild of a bottle has another sha256, so another layer.
    state.bottles.records["arm64"][0]["sha256"] = "1" * 64
    package_app.copy_native_tools_layer(tmp_path / "Second.app", ("arm64",))

    assert len(state.vendor_calls) == 2


@pytest.mark.parametrize("changed", ["vendored-library", "bottle-file"])
def test_copy_native_tools_layer_rebuilds_when_an_input_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    changed: str,
) -> None:
    package_app = load_package_app_module()
    state = native_layer_fixture(package_app, monkeypatch, tmp_path)

    package_app.copy_native_tools_layer(tmp_path / "First.app", ("arm64",))
    capsys.readouterr()
    path = state.dependency if changed == "vendored-library" else state.bottles.blobs[0]
    path.write_text("changed", encoding="utf-8")
    package_app.copy_native_tools_layer(tmp_path / "Second.app", ("arm64",))
    captured = capsys.readouterr()

    assert len(state.vendor_calls) == 2
    assert f"Rebuilding native tool layer: cached input changed: {path.resolve()}" in captured.err


def test_copy_native_tools_layer_rebuilds_when_cached_output_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_app = load_package_app_module()
    state = native_layer_fixture(package_app, monkeypatch, tmp_path)

    package_app.copy_native_tools_layer(tmp_path / "First.app", ("arm64",))
    capsys.readouterr()
    cache_entry = next((tmp_path / ".build" / "package-app" / "native-tools").iterdir())
    (cache_entry / "Contents" / "Frameworks" / "libnative.dylib").write_text("corrupt", encoding="utf-8")
    package_app.copy_native_tools_layer(tmp_path / "Second.app", ("arm64",))
    captured = capsys.readouterr()

    assert len(state.vendor_calls) == 2
    assert "Rebuilding native tool layer: cached output tree changed:" in captured.err
    assert str(cache_entry / "Contents") in captured.err


def test_copy_native_tools_layer_without_cache_builds_in_place_and_resolves_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    state = native_layer_fixture(package_app, monkeypatch, tmp_path)
    app = tmp_path / "Direct.app"

    package_app.copy_native_tools_layer(app, ("arm64",), use_cache=False)

    assert state.prepared == [(("arm64",), False)]
    assert [call[0] for call in state.vendor_calls] == [app]
    assert not (tmp_path / ".build" / "package-app" / "native-tools").exists() or not any(
        (tmp_path / ".build" / "package-app" / "native-tools").iterdir())


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeGhcr:
    """ghcr.io for one test: tag lists, manifest indexes and blobs by formula."""

    def __init__(self) -> None:
        self.tags: dict[str, list[str]] = {}
        self.indexes: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.blobs: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []

    def add(self, formula: str, reference: str, entries: dict[str, dict[str, object]]) -> None:
        self.tags.setdefault(formula, []).append(reference)
        self.indexes[(formula, reference)] = [
            {"annotations": {"org.opencontainers.image.ref.name": name, **annotations}}
            for name, annotations in entries.items()
        ]

    def json(self, formula: str, path: str, *, accept: str | None = None) -> dict:
        self.requests.append((formula, path))
        if path == "tags/list":
            return {"tags": self.tags.get(formula, [])}
        reference = path.removeprefix("manifests/")
        assert accept == "application/vnd.oci.image.index.v1+json"
        return {"manifests": self.indexes[(formula, reference)]}

    def open(self, formula: str, path: str, *, accept: str | None = None):
        self.requests.append((formula, path))
        return FakeResponse(self.blobs[path.removeprefix("blobs/sha256:")])


class OfflineGhcr:
    def json(self, *args, **kwargs):
        raise AssertionError("contacted the registry")

    open = json


def bottle_annotations(digest: str, dependencies: list[tuple[str, str]] = ()) -> dict[str, object]:
    tab = {"runtime_dependencies": [{"full_name": name, "pkg_version": version} for name, version in dependencies]}
    return {"sh.brew.bottle.digest": digest, "sh.brew.tab": json.dumps(tab)}


def test_ghcr_client_requests_a_token_per_repository_then_the_registry(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    seen: list[object] = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        if isinstance(request, str):
            return FakeResponse(json.dumps({"token": "t-" + request.rsplit(":", 2)[-2].rsplit("/", 1)[-1]}).encode())
        return FakeResponse(b'{"tags": []}')

    client = package_app.GhcrClient(urlopen=fake_urlopen)
    client.json("openssl@3", "tags/list")
    client.json("openssl@3", "manifests/3.6.3", accept="application/vnd.oci.image.index.v1+json")

    assert package_app.GhcrClient.repository("openssl@3") == "homebrew/core/openssl/3"
    assert package_app.GhcrClient.repository("libstdc++") == "homebrew/core/libstdcxx"
    assert seen[0] == "https://ghcr.io/token?service=ghcr.io&scope=repository:homebrew/core/openssl/3:pull"
    # One token per repository; every registry call carries it.
    assert [request.full_url for request in seen[1:]] == [
        "https://ghcr.io/v2/homebrew/core/openssl/3/tags/list",
        "https://ghcr.io/v2/homebrew/core/openssl/3/manifests/3.6.3",
    ]
    assert seen[1].get_header("Authorization") == "Bearer t-3"
    assert seen[2].get_header("Accept") == "application/vnd.oci.image.index.v1+json"


def test_bottle_rebuilds_lists_only_that_versions_rebuilds_newest_first() -> None:
    package_app = load_package_app_module()

    assert package_app.bottle_rebuilds(["1.10", "1.10-2", "1.10.1", "1.10-1", "1.1", "11.10"], "1.10") == [
        (2, "1.10-2"), (1, "1.10-1"), (0, "1.10")]
    assert package_app.bottle_entry_name("6.3.0", "arm64_sonoma", 0) == "6.3.0.arm64_sonoma"
    assert package_app.bottle_entry_name("6.3.0", "arm64_sonoma", 1) == "6.3.0.arm64_sonoma.1"


def test_resolve_bottle_takes_the_newest_rebuild_that_has_the_platform() -> None:
    package_app = load_package_app_module()
    registry = FakeGhcr()
    # Rebuild 2 dropped Sonoma, as Homebrew does once it stops building for it.
    registry.add("gnutls", "3.8.13_2", {"3.8.13_2.arm64_sonoma": bottle_annotations("a" * 64)})
    registry.add("gnutls", "3.8.13_2-1", {"3.8.13_2.arm64_sonoma.1": bottle_annotations("b" * 64),
                                         "3.8.13_2.sonoma.1": bottle_annotations("c" * 64)})
    registry.add("gnutls", "3.8.13_2-2", {"3.8.13_2.arm64_sequoia.2": bottle_annotations("d" * 64)})

    record, _ = package_app.resolve_bottle(registry, "gnutls", "3.8.13_2", "arm64_sonoma")

    assert record == {"formula": "gnutls", "version": "3.8.13_2", "reference": "3.8.13_2-1",
                      "bottle": "3.8.13_2.arm64_sonoma.1", "sha256": "b" * 64}


def test_resolve_bottle_falls_back_to_an_all_bottle() -> None:
    package_app = load_package_app_module()
    registry = FakeGhcr()
    registry.add("ca-certificates", "2026-08-13-1", {"2026-08-13.all.1": bottle_annotations("e" * 64)})

    record, _ = package_app.resolve_bottle(registry, "ca-certificates", "2026-08-13", "sonoma")

    assert record["bottle"] == "2026-08-13.all.1"


@pytest.mark.parametrize("case", ["no-platform", "no-version", "bad-digest"])
def test_resolve_bottle_rejects_a_formula_without_a_usable_bottle(case: str) -> None:
    package_app = load_package_app_module()
    registry = FakeGhcr()
    if case == "no-platform":
        registry.add("readline", "8.3.3", {"8.3.3.arm64_tahoe": bottle_annotations("a" * 64)})
        expected = r"No arm64_sonoma or all bottle of readline 8.3.3 \(registry tags tried: 8.3.3\)"
    elif case == "no-version":
        registry.add("readline", "8.3.6", {"8.3.6.arm64_sonoma": bottle_annotations("a" * 64)})
        expected = r"\(registry tags tried: none\)"
    else:
        registry.add("readline", "8.3.3", {"8.3.3.arm64_sonoma": bottle_annotations("not-a-digest")})
        expected = "Bottle 8.3.3.arm64_sonoma of readline has no valid digest"

    with pytest.raises(RuntimeError, match=expected):
        package_app.resolve_bottle(registry, "readline", "8.3.3", "arm64_sonoma")


def pinned_registry(tag: str = "arm64_sonoma") -> FakeGhcr:
    """samba and sshpass at their pins, with samba's recorded dependencies."""
    registry = FakeGhcr()
    registry.add("samba", "4.24.6", {f"4.24.6.{tag}": bottle_annotations(
        "1" * 64, [("gnutls", "3.8.13_2"), ("gmp", "6.3.0")])})
    registry.add("sshpass", "1.10", {f"1.10.{tag}": bottle_annotations("2" * 64)})
    registry.add("gnutls", "3.8.13_2", {f"3.8.13_2.{tag}": bottle_annotations("3" * 64, [("gmp", "6.3.0")])})
    registry.add("gmp", "6.3.0", {f"6.3.0.{tag}": bottle_annotations("4" * 64)})
    return registry


def test_resolve_homebrew_bottles_follows_the_pinned_bottles_records() -> None:
    package_app = load_package_app_module()

    records = package_app.resolve_homebrew_bottles(pinned_registry("sonoma"), "x86_64")

    assert [(record["formula"], record["version"], record["bottle"]) for record in records] == [
        ("gmp", "6.3.0", "6.3.0.sonoma"),
        ("gnutls", "3.8.13_2", "3.8.13_2.sonoma"),
        ("samba", "4.24.6", "4.24.6.sonoma"),
        ("sshpass", "1.10", "1.10.sonoma"),
    ]


@pytest.mark.parametrize("case", ["missing-indirect", "conflict", "corrupt-record"])
def test_resolve_homebrew_bottles_rejects_an_inconsistent_set(case: str) -> None:
    package_app = load_package_app_module()
    registry = pinned_registry()
    if case == "missing-indirect":
        # gnutls needs nettle, which samba's record does not name.
        registry.indexes[("gnutls", "3.8.13_2")][0]["annotations"].update(
            bottle_annotations("3" * 64, [("gmp", "6.3.0"), ("nettle", "4.0")]))
        expected = "gnutls needs nettle, which no pinned bottle records"
    elif case == "conflict":
        registry.indexes[("sshpass", "1.10")][0]["annotations"].update(bottle_annotations("2" * 64, [("gmp", "6.2.1")]))
        expected = "sshpass needs gmp 6.2.1, but 6.3.0 is already selected"
    else:
        registry.indexes[("samba", "4.24.6")][0]["annotations"]["sh.brew.tab"] = "{not json"
        expected = "Bottle of samba has no readable runtime dependency record"

    with pytest.raises(RuntimeError, match=expected):
        package_app.resolve_homebrew_bottles(registry, "arm64")


def test_cached_bottle_resolution_is_reused_offline_and_redone_without_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    registry = pinned_registry()

    first = package_app.cached_bottle_resolution(registry, "arm64", use_cache=True)
    assert package_app.cached_bottle_resolution(OfflineGhcr(), "arm64", use_cache=True) == first
    registry.requests.clear()
    assert package_app.cached_bottle_resolution(registry, "arm64", use_cache=False) == first
    assert ("samba", "tags/list") in registry.requests

    # A damaged resolution file is resolved again rather than trusted.
    cached = next((tmp_path / ".build" / "package-app" / "homebrew-bottles" / "resolved").glob("arm64-*.json"))
    cached.write_text('[{"sha256": "short"}]', encoding="utf-8")
    assert package_app.cached_bottle_resolution(registry, "arm64", use_cache=True) == first


def bottle_tarball(entries: list[tuple[str, str, object]]) -> bytes:
    """A gzip tar of (kind, name, data): file content, link target or None."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for kind, name, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o555
                archive.addfile(info)
            elif kind == "file":
                payload = str(data).encode()
                info.size, info.mode = len(payload), 0o444
                archive.addfile(info, io.BytesIO(payload))
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, str(data)
                archive.addfile(info)
            elif kind == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, str(data)
                archive.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
    return buffer.getvalue()


GOOD_BOTTLE = [
    ("dir", "talloc", None),
    ("dir", "talloc/2.5.0", None),
    ("dir", "talloc/2.5.0/lib", None),
    ("file", "talloc/2.5.0/lib/libtalloc.2.dylib", "talloc"),
    ("symlink", "talloc/2.5.0/lib/libtalloc.dylib", "libtalloc.2.dylib"),
]


def bottle_record(content: bytes, formula: str = "talloc", version: str = "2.5.0") -> dict[str, object]:
    return {"formula": formula, "version": version, "bottle": f"{version}.arm64_sonoma",
            "sha256": hashlib.sha256(content).hexdigest()}


def test_fetch_bottle_downloads_once_and_keeps_it_under_its_sha256(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    content = bottle_tarball(GOOD_BOTTLE)
    record = bottle_record(content)
    registry = FakeGhcr()
    registry.blobs[str(record["sha256"])] = content

    blob = package_app.fetch_bottle(registry, record)

    assert blob.name == f"{record['sha256']}.tar.gz"
    assert blob.read_bytes() == content
    assert package_app.fetch_bottle(OfflineGhcr(), record) == blob
    # A damaged download in the cache is fetched again.
    blob.write_bytes(b"damaged")
    assert package_app.fetch_bottle(registry, record).read_bytes() == content


def test_fetch_bottle_refuses_a_download_with_the_wrong_sha256(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    record = bottle_record(b"expected")
    registry = FakeGhcr()
    registry.blobs[str(record["sha256"])] = b"tampered"

    with pytest.raises(RuntimeError, match=r"has sha256 [0-9a-f]{64}, expected"):
        package_app.fetch_bottle(registry, record)

    assert list((tmp_path / ".build" / "package-app" / "homebrew-bottles" / "blobs").iterdir()) == []


def test_extract_bottle_unpacks_the_keg_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    content = bottle_tarball(GOOD_BOTTLE)
    blob = tmp_path / "talloc.tar.gz"
    blob.write_bytes(content)
    record = bottle_record(content)

    keg = package_app.extract_bottle(blob, record)

    assert keg == tmp_path / ".build" / "package-app" / "homebrew-bottles" / "kegs" / str(record["sha256"]) / "talloc" / "2.5.0"
    assert (keg / "lib" / "libtalloc.dylib").resolve() == (keg / "lib" / "libtalloc.2.dylib").resolve()
    # Bottles ship read-only folders; the cache must stay removable.
    assert os.access(keg / "lib", os.W_OK)
    blob.unlink()
    assert package_app.extract_bottle(blob, record) == keg


def test_extract_bottle_redoes_an_interrupted_extraction(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    content = bottle_tarball(GOOD_BOTTLE)
    blob = tmp_path / "talloc.tar.gz"
    blob.write_bytes(content)
    record = bottle_record(content)
    partial = tmp_path / ".build" / "package-app" / "homebrew-bottles" / "kegs" / str(record["sha256"]) / "talloc" / "2.5.0"
    partial.mkdir(parents=True)

    keg = package_app.extract_bottle(blob, record)

    assert (keg / "lib" / "libtalloc.2.dylib").read_text(encoding="utf-8") == "talloc"


@pytest.mark.parametrize("entry,expected", [
    (("file", "/etc/passwd", "x"), "path outside talloc/2.5.0"),
    (("file", "talloc/2.5.0/../../evil", "x"), "path outside talloc/2.5.0"),
    (("file", "tevent/0.17.2/lib/x", "x"), "path outside talloc/2.5.0"),
    (("symlink", "talloc/2.5.0/lib/escape", "../../../outside"), "links talloc/2.5.0/lib/escape outside"),
    (("symlink", "talloc/2.5.0/lib/absolute", "/usr/lib/libz.dylib"), "links talloc/2.5.0/lib/absolute outside"),
    (("hardlink", "talloc/2.5.0/lib/hard", "talloc/2.5.0/lib/libtalloc.2.dylib"), "unsupported entry"),
    (("fifo", "talloc/2.5.0/lib/pipe", None), "unsupported entry"),
])
def test_extract_bottle_refuses_entries_outside_its_keg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry: tuple[str, str, object],
    expected: str,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    content = bottle_tarball([*GOOD_BOTTLE, entry])
    blob = tmp_path / "talloc.tar.gz"
    blob.write_bytes(content)

    with pytest.raises(RuntimeError, match=re.escape(expected)):
        package_app.extract_bottle(blob, bottle_record(content))

    kegs = tmp_path / ".build" / "package-app" / "homebrew-bottles" / "kegs"
    assert [path.name for path in kegs.iterdir()] == []
    assert not (tmp_path / "evil").exists()


def test_extract_bottle_requires_the_records_keg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    content = bottle_tarball([("dir", "talloc", None)])
    blob = tmp_path / "talloc.tar.gz"
    blob.write_bytes(content)

    with pytest.raises(RuntimeError, match="does not hold talloc/2.5.0"):
        package_app.extract_bottle(blob, bottle_record(content))


def test_prepare_homebrew_bottles_unpacks_each_architectures_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(package_app, "HOMEBREW_BOTTLE_ROOTS", {"talloc": "2.5.0"})
    registry = FakeGhcr()
    for architecture, tag in (("arm64", "arm64_sonoma"), ("x86_64", "sonoma")):
        content = bottle_tarball([*GOOD_BOTTLE[:3], ("file", "talloc/2.5.0/lib/libtalloc.2.dylib", architecture)])
        digest = hashlib.sha256(content).hexdigest()
        registry.blobs[digest] = content
        registry.indexes.setdefault(("talloc", "2.5.0"), []).append(
            {"annotations": {"org.opencontainers.image.ref.name": f"2.5.0.{tag}", **bottle_annotations(digest)}})
    registry.tags["talloc"] = ["2.5.0"]

    bottles = package_app.prepare_homebrew_bottles(("arm64", "x86_64"), client=registry)

    for architecture in ("arm64", "x86_64"):
        library = bottles.kegs[architecture]["talloc"] / "lib" / "libtalloc.2.dylib"
        assert library.read_text(encoding="utf-8") == architecture
    assert len(bottles.blobs) == 2
    again = package_app.prepare_homebrew_bottles(("arm64", "x86_64"), client=OfflineGhcr())
    assert again.kegs == bottles.kegs


def test_homebrew_placeholder_path_maps_bottle_references_into_the_kegs(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    kegs = {"samba": tmp_path / "samba" / "4.24.6", "gnutls": tmp_path / "gnutls" / "3.8.13_2"}

    assert package_app.homebrew_placeholder_path(
        "@@HOMEBREW_CELLAR@@/samba/4.24.6/lib/private/liblibsmb-private-samba.dylib", kegs
    ) == kegs["samba"] / "lib" / "private" / "liblibsmb-private-samba.dylib"
    assert package_app.homebrew_placeholder_path(
        "@@HOMEBREW_PREFIX@@/opt/gnutls/lib/libgnutls.30.dylib", kegs) == kegs["gnutls"] / "lib" / "libgnutls.30.dylib"
    assert package_app.homebrew_placeholder_path("/usr/lib/libz.1.dylib", kegs) is None
    for reference, expected in [
        ("@@HOMEBREW_CELLAR@@/samba/4.25.0/lib/libsmbconf.dylib", "names no pinned keg"),
        ("@@HOMEBREW_PREFIX@@/opt/nettle/lib/libnettle.dylib", "names no pinned keg"),
        ("@@HOMEBREW_PREFIX@@/lib/libtalloc.dylib", "Unsupported bottle placeholder"),
        ("@@HOMEBREW_PERL@@", "Unsupported bottle placeholder"),
    ]:
        with pytest.raises(RuntimeError, match=expected):
            package_app.homebrew_placeholder_path(reference, kegs)


def test_vendor_macho_dependencies_resolves_bottle_placeholders_per_architecture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    kegs: dict[str, dict[str, Path]] = {}
    tool_sources: dict[Path, tuple[Path, str]] = {}
    for architecture in ("arm64", "x86_64"):
        samba = tmp_path / "kegs" / architecture / "samba" / "4.24.6"
        gnutls = tmp_path / "kegs" / architecture / "gnutls" / "3.8.13_2"
        for path, text in ((samba / "bin" / "smbclient", "tool"),
                           (samba / "lib" / "private" / "liblibsmb-private-samba.dylib", "libsmb"),
                           (gnutls / "lib" / "libgnutls.30.dylib", "gnutls")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{architecture} {text}", encoding="utf-8")
        kegs[architecture] = {"samba": samba, "gnutls": gnutls}
        copy = tools / architecture / "smbclient"
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(f"{architecture} tool", encoding="utf-8")
        copy.chmod(0o755)
        tool_sources[copy] = (samba / "bin" / "smbclient", architecture)

    cellar_lib = "@@HOMEBREW_CELLAR@@/samba/4.24.6/lib"
    dependencies = {
        "tool": ["@@HOMEBREW_CELLAR@@/samba/4.24.6/lib/private/liblibsmb-private-samba.dylib", "/usr/lib/libSystem.B.dylib"],
        # A library lists its own install name; that is not a dependency.
        "libsmb": ["@@HOMEBREW_PREFIX@@/opt/samba/lib/private/liblibsmb-private-samba.dylib",
                   "@@HOMEBREW_PREFIX@@/opt/gnutls/lib/libgnutls.30.dylib"],
        "gnutls": ["@@HOMEBREW_PREFIX@@/opt/gnutls/lib/libgnutls.30.dylib"],
    }
    own_names = {"libsmb": dependencies["libsmb"][0], "gnutls": dependencies["gnutls"][0], "tool": None}
    rpaths = {"tool": [cellar_lib, f"{cellar_lib}/private"], "libsmb": [cellar_lib], "gnutls": []}

    def kind(path: Path) -> str:
        return path.read_text(encoding="utf-8").split(" ", 1)[1]

    changes: list[list[str]] = []
    monkeypatch.setattr(package_app, "macho_dependencies", lambda path: list(dependencies[kind(path)]))
    monkeypatch.setattr(package_app, "macho_install_name", lambda path: own_names[kind(path)])
    monkeypatch.setattr(package_app, "macho_rpaths", lambda path: list(rpaths[kind(path)]))
    monkeypatch.setattr(package_app, "run_quiet",
                        lambda cmd: changes.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(package_app, "set_macho_id_if_supported", lambda path: None)

    vendored = package_app.vendor_macho_dependencies(app, kegs=kegs, tool_sources=tool_sources)

    frameworks = app / "Contents" / "Frameworks"
    assert vendored == {kegs[a][f] / sub for a in kegs for f, sub in (
        ("samba", Path("lib/private/liblibsmb-private-samba.dylib")), ("gnutls", Path("lib/libgnutls.30.dylib")))}
    arm_libsmb = frameworks / "liblibsmb-private-samba.dylib"
    x86_libsmb = next(frameworks.glob("liblibsmb-private-samba-*.dylib"))
    # Each architecture's tool gets its own architecture's libraries.
    assert arm_libsmb.read_text(encoding="utf-8") == "arm64 libsmb"
    assert x86_libsmb.read_text(encoding="utf-8") == "x86_64 libsmb"
    assert next(frameworks.glob("libgnutls-*.30.dylib")).read_text(encoding="utf-8") == "x86_64 gnutls"
    assert ["install_name_tool", "-change", dependencies["tool"][0],
            f"@loader_path/../../../../Frameworks/{x86_libsmb.name}", str(tools / "x86_64" / "smbclient")] in changes
    assert ["install_name_tool", "-change", dependencies["libsmb"][1], "@loader_path/libgnutls.30.dylib",
            str(arm_libsmb)] in changes
    # Placeholder rpaths are dropped; the own install name is not rewritten as a dependency.
    for rpath in rpaths["tool"]:
        assert ["install_name_tool", "-delete_rpath", rpath, str(tools / "arm64" / "smbclient")] in changes
    assert not any(cmd[2] == dependencies["libsmb"][0] for cmd in changes if cmd[1] == "-change")


def test_vendor_macho_dependencies_refuses_a_placeholder_without_bottles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    tool = app / "Contents" / "Resources" / "Tools" / "bin" / "smbclient"
    tool.parent.mkdir(parents=True)
    tool.write_text("tool", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setattr(package_app, "macho_dependencies",
                        lambda path: ["@@HOMEBREW_PREFIX@@/opt/talloc/lib/libtalloc.2.dylib"])
    monkeypatch.setattr(package_app, "macho_install_name", lambda path: None)
    monkeypatch.setattr(package_app, "macho_rpaths", lambda path: [])

    with pytest.raises(RuntimeError, match="references @@HOMEBREW_PREFIX@@/opt/talloc/lib/libtalloc.2.dylib but no bottle"):
        package_app.vendor_macho_dependencies(app)


def test_external_dependency_validation_rejects_a_leftover_bottle_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    fixture = external_validation_fixture(package_app, monkeypatch, tmp_path)
    fixture.dependencies[fixture.tool].append("@@HOMEBREW_PREFIX@@/opt/popt/lib/libpopt.0.dylib")

    with pytest.raises(RuntimeError, match=r"smbclient: @@HOMEBREW_PREFIX@@/opt/popt/lib/libpopt.0.dylib"):
        package_app.assert_no_external_macho_dependencies(fixture.app)


VTOOL_FAT = """{path} (architecture x86_64):
Load command 9
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 10.13
      sdk 26.4
{path} (architecture arm64):
Load command 10
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform MACOS
    minos 14.8
      sdk 14.5
   ntools 1
     tool LD
  version 1115.7.3
"""

VTOOL_THIN = """{path}:
Load command 10
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform MACOS
    minos 26.6
      sdk 26.6
   ntools 1
     tool LD
  version 1230.1
"""


def fake_vtool(package_app, monkeypatch: pytest.MonkeyPatch, outputs: dict[Path, str]) -> None:
    def fake_run(cmd, **kwargs):
        if cmd[0] != "vtool":
            raise AssertionError(cmd)
        path = Path(cmd[-1])
        if path not in outputs:
            return subprocess.CompletedProcess(cmd, 1, "", "file is not mach-o")
        return subprocess.CompletedProcess(cmd, 0, outputs[path].format(path=path), "")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})


def test_macho_minimum_macos_reads_each_architectures_load_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    fat, thin, text = tmp_path / "python3.13", tmp_path / "smbclient", tmp_path / "README"
    fake_vtool(package_app, monkeypatch, {fat: VTOOL_FAT, thin: VTOOL_THIN})

    # The linker's own "version" line inside LC_BUILD_VERSION is not a macOS version.
    assert package_app.macho_minimum_macos(fat) == {"x86_64": "10.13", "arm64": "14.8"}
    assert package_app.macho_minimum_macos(thin) == {"arm64": "26.6"}
    assert package_app.macho_minimum_macos(text) == {}


def test_assert_macho_minimum_macos_accepts_the_limit_and_rejects_newer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    fat, thin, text = tmp_path / "python3.13", tmp_path / "smbclient", tmp_path / "README"
    fake_vtool(package_app, monkeypatch, {fat: VTOOL_FAT, thin: VTOOL_THIN})

    package_app.assert_macho_minimum_macos([fat, text])
    with pytest.raises(RuntimeError, match=r"newer macOS than 14\.8:\n  - .*smbclient \(arm64\): 26\.6$"):
        package_app.assert_macho_minimum_macos([fat, thin, text])
    package_app.assert_macho_minimum_macos([thin], maximum="26.6")


OTOOL_RPATHS = """{path}:
Load command 12
          cmd LC_LOAD_DYLIB
      cmdsize 72
         name @rpath/libndr.6.dylib (offset 24)
Load command 13
          cmd LC_RPATH
      cmdsize 64
         path /opt/homebrew/Cellar/samba/4.25.0/lib/private (offset 12)
Load command 14
          cmd LC_RPATH
      cmdsize 56
         path @loader_path/../lib (offset 12)
Load command 15
          cmd LC_FUNCTION_STARTS
      cmdsize 16
"""


def test_macho_rpaths_lists_rpath_load_commands_in_search_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    binary = tmp_path / "smbclient"

    def fake_run(cmd, **kwargs):
        assert cmd == ["otool", "-l", str(binary)]
        return subprocess.CompletedProcess(cmd, 0, stdout=OTOOL_RPATHS.format(path=binary), stderr="")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)

    # A path line of another load command (the LC_LOAD_DYLIB name) is no rpath.
    assert package_app.macho_rpaths(binary) == ["/opt/homebrew/Cellar/samba/4.25.0/lib/private", "@loader_path/../lib"]


def test_macho_rpaths_is_empty_when_otool_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app.subprocess, "run",
                        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not a Mach-O"))

    assert package_app.macho_rpaths(tmp_path / "README") == []


def test_rpath_dependency_target_takes_the_first_rpath_holding_the_library(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    private = tmp_path / "Cellar" / "lib" / "private"
    lib = tmp_path / "Cellar" / "lib"
    private.mkdir(parents=True)
    (lib / "libndr.6.dylib").write_text("public", encoding="utf-8")
    (private / "libndr.6.dylib").write_text("private", encoding="utf-8")
    loader_dir = tmp_path / "Cellar" / "bin"
    loader_dir.mkdir()

    # dyld searches rpaths in load order; @loader_path is the loading binary's directory.
    assert package_app.rpath_dependency_target(
        "@rpath/libndr.6.dylib", ["/missing", "@loader_path/../lib", str(private)], loader_dir, loader_dir
    ) == loader_dir / "../lib" / "libndr.6.dylib"
    assert package_app.rpath_dependency_target(
        "@rpath/libndr.6.dylib", [str(private), str(lib)], loader_dir, loader_dir
    ) == private / "libndr.6.dylib"
    assert package_app.rpath_dependency_target("@rpath/libndr.6.dylib", ["/missing"], loader_dir, loader_dir) is None
    assert package_app.rpath_dependency_target("@rpath/libndr.6.dylib", [], loader_dir, loader_dir) is None


def rpath_vendor_fixture(package_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Homebrew's samba on both Macs: the arm64 4.25.0 bottle links its private
    libraries through @rpath into the Cellar; the x86_64 4.24.3 bottle uses
    absolute paths. Both ship a liblibsmb-private-samba.dylib."""
    app = tmp_path / "TimeCapsuleSMB.app"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    arm_tool = tools / "arm64" / "smbclient"
    x86_tool = tools / "x86_64" / "smbclient"
    for tool in (arm_tool, x86_tool):
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text(tool.parent.name, encoding="utf-8")
        tool.chmod(0o755)

    cellar = tmp_path / "opt" / "homebrew" / "Cellar" / "samba" / "4.25.0" / "lib"
    arm_private = cellar / "private"
    arm_private.mkdir(parents=True)
    arm_libsmb = arm_private / "liblibsmb-private-samba.dylib"
    arm_libndr = cellar / "libndr.6.dylib"
    arm_libsmb.write_text("arm64 libsmb", encoding="utf-8")
    arm_libndr.write_text("arm64 libndr", encoding="utf-8")
    x86_libsmb = tmp_path / "usr" / "local" / "Cellar" / "samba" / "4.24.3" / "lib" / "private" / "liblibsmb-private-samba.dylib"
    x86_libsmb.parent.mkdir(parents=True)
    x86_libsmb.write_text("x86_64 libsmb", encoding="utf-8")
    cellar_rpaths = [str(arm_private), str(cellar)]

    dependencies: dict[str, list[str]] = {
        "arm64 tool": ["@rpath/liblibsmb-private-samba.dylib", "/usr/lib/libSystem.B.dylib"],
        "x86_64 tool": [str(x86_libsmb)],
        # otool -L lists a library's own install name first.
        "arm64 libsmb": ["@rpath/liblibsmb-private-samba.dylib", "@rpath/libndr.6.dylib"],
        "arm64 libndr": [],
        "x86_64 libsmb": [],
    }
    rpaths: dict[str, list[str]] = {
        "arm64 tool": cellar_rpaths,
        "x86_64 tool": [],
        # A copy keeps the rpaths it was linked with.
        "arm64 libsmb": cellar_rpaths,
        "arm64 libndr": [],
        "x86_64 libsmb": [],
    }
    names = {"arm64 tool", "x86_64 tool"}

    def identity(path: Path) -> str:
        if path.resolve() == arm_tool.resolve():
            return "arm64 tool"
        if path.resolve() == x86_tool.resolve():
            return "x86_64 tool"
        content = path.read_text(encoding="utf-8")
        assert content in dependencies, content
        return content

    changes: list[list[str]] = []

    def fake_run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        changes.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(package_app, "macho_dependencies", lambda path: list(dependencies[identity(path)]))
    monkeypatch.setattr(package_app, "macho_rpaths", lambda path: list(rpaths[identity(path)]))
    monkeypatch.setattr(package_app, "macho_install_name",
                        lambda path: None if identity(path) in names else f"@rpath/{path.name}")
    monkeypatch.setattr(package_app, "run_quiet", fake_run_quiet)
    monkeypatch.setattr(package_app, "set_macho_id_if_supported", lambda path: None)
    return SimpleNamespace(app=app, arm_tool=arm_tool, x86_tool=x86_tool, dependencies=dependencies,
                           rpaths=rpaths, changes=changes, cellar_rpaths=cellar_rpaths,
                           sources={arm_libsmb.resolve(), arm_libndr.resolve(), x86_libsmb.resolve()})


def test_vendor_macho_dependencies_bundles_rpath_libraries_and_drops_outside_rpaths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    fixture = rpath_vendor_fixture(package_app, monkeypatch, tmp_path)

    vendored = package_app.vendor_macho_dependencies(fixture.app)

    frameworks = fixture.app / "Contents" / "Frameworks"
    assert vendored == fixture.sources
    # The arm64 library keeps its name; the x86_64 one of the same name is renamed.
    assert (frameworks / "liblibsmb-private-samba.dylib").read_text(encoding="utf-8") == "arm64 libsmb"
    assert (frameworks / "libndr.6.dylib").read_text(encoding="utf-8") == "arm64 libndr"
    x86_libsmb = next(frameworks.glob("liblibsmb-private-samba-*.dylib"))
    assert x86_libsmb.read_text(encoding="utf-8") == "x86_64 libsmb"

    def change(old: str, new: str, path: Path) -> list[str]:
        return ["install_name_tool", "-change", old, new, str(path)]

    assert change("@rpath/liblibsmb-private-samba.dylib",
                  "@loader_path/../../../../Frameworks/liblibsmb-private-samba.dylib",
                  fixture.arm_tool) in fixture.changes
    # A bundled library's own @rpath references resolve from its Cellar origin.
    assert change("@rpath/libndr.6.dylib", "@loader_path/libndr.6.dylib",
                  frameworks / "liblibsmb-private-samba.dylib") in fixture.changes
    assert change(fixture.dependencies["x86_64 tool"][0],
                  f"@loader_path/../../../../Frameworks/{x86_libsmb.name}", fixture.x86_tool) in fixture.changes
    # No Cellar rpath survives on the tool or the library copied with it.
    for path in (fixture.arm_tool, frameworks / "liblibsmb-private-samba.dylib"):
        for rpath in fixture.cellar_rpaths:
            assert ["install_name_tool", "-delete_rpath", rpath, str(path)] in fixture.changes
    # System libraries and a library's own install name are left alone.
    assert not any("/usr/lib/libSystem.B.dylib" in cmd for cmd in fixture.changes)
    assert not any("@rpath/liblibsmb-private-samba.dylib" in cmd and cmd[-1].endswith(".dylib")
                   for cmd in fixture.changes)


def test_vendor_macho_dependencies_searches_the_loaders_rpaths_too(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    fixture = rpath_vendor_fixture(package_app, monkeypatch, tmp_path)
    # Homebrew's libutil-reg: its only rpath is lib, but libgenrand is in
    # lib/private, which dyld finds through smbclient's rpaths.
    fixture.rpaths["arm64 libsmb"] = [fixture.cellar_rpaths[1]]
    private = Path(fixture.cellar_rpaths[0])
    (private / "libgenrand-private-samba.dylib").write_text("arm64 libgenrand", encoding="utf-8")
    fixture.dependencies["arm64 libsmb"].append("@rpath/libgenrand-private-samba.dylib")
    fixture.dependencies["arm64 libgenrand"] = []
    fixture.rpaths["arm64 libgenrand"] = []

    package_app.vendor_macho_dependencies(fixture.app)

    frameworks = fixture.app / "Contents" / "Frameworks"
    assert (frameworks / "libgenrand-private-samba.dylib").read_text(encoding="utf-8") == "arm64 libgenrand"
    assert ["install_name_tool", "-change", "@rpath/libgenrand-private-samba.dylib",
            "@loader_path/libgenrand-private-samba.dylib",
            str(frameworks / "liblibsmb-private-samba.dylib")] in fixture.changes


def test_vendor_macho_dependencies_keeps_loader_relative_rpaths_and_inside_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    fixture = rpath_vendor_fixture(package_app, monkeypatch, tmp_path)
    frameworks = fixture.app / "Contents" / "Frameworks"
    frameworks.mkdir(parents=True)
    (frameworks / "libinside.dylib").write_text("arm64 libndr", encoding="utf-8")
    fixture.dependencies["x86_64 tool"] = ["@rpath/libinside.dylib"]
    fixture.rpaths["x86_64 tool"] = ["@loader_path/../../../../Frameworks"]

    package_app.vendor_macho_dependencies(fixture.app)

    # Already bundled: nothing to copy or rewrite, and the rpath that finds it stays.
    assert not any(cmd[-1] == str(fixture.x86_tool) for cmd in fixture.changes)
    assert not list(frameworks.glob("libinside-*.dylib"))


def test_vendor_macho_dependencies_rejects_an_rpath_reference_it_cannot_find(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    fixture = rpath_vendor_fixture(package_app, monkeypatch, tmp_path)
    fixture.dependencies["arm64 tool"] = ["@rpath/libgone.dylib"]

    with pytest.raises(RuntimeError, match=r"@rpath/libgone.dylib referenced by .*smbclient \(rpaths: .*Cellar"):
        package_app.vendor_macho_dependencies(fixture.app)


def external_validation_fixture(package_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    app = tmp_path / "TimeCapsuleSMB.app"
    contents = app / "Contents"
    executable = contents / "MacOS" / "TimeCapsuleSMB"
    tool = contents / "Resources" / "Tools" / "bin" / "arm64" / "smbclient"
    library = contents / "Frameworks" / "liblibsmb-private-samba.dylib"
    outside = tmp_path / "Cellar" / "lib"
    outside.mkdir(parents=True)
    (outside / "libndr.6.dylib").write_text("outside", encoding="utf-8")
    for path in (executable, tool, library):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
        path.chmod(0o755)
    (contents / "Frameworks" / "libndr.6.dylib").write_text("inside", encoding="utf-8")
    dependencies = {
        # The app executable's own Swift rpaths stay: only bundled tools and
        # Frameworks are vendored.
        executable: [],
        tool: ["@loader_path/../../../../Frameworks/liblibsmb-private-samba.dylib"],
        library: ["@rpath/liblibsmb-private-samba.dylib", "@rpath/libndr.6.dylib"],
        contents / "Frameworks" / "libndr.6.dylib": [],
    }
    rpaths = {executable: ["/usr/lib/swift", "@loader_path"], tool: [], library: ["@loader_path"],
              contents / "Frameworks" / "libndr.6.dylib": []}
    monkeypatch.setattr(package_app, "macho_dependencies", lambda path: dependencies[path])
    monkeypatch.setattr(package_app, "macho_rpaths", lambda path: rpaths[path])
    monkeypatch.setattr(package_app, "macho_install_name",
                        lambda path: "@rpath/liblibsmb-private-samba.dylib" if path == library else None)
    return SimpleNamespace(app=app, tool=tool, library=library, outside=outside, dependencies=dependencies,
                           rpaths=rpaths)


def test_external_dependency_validation_accepts_rpath_references_resolved_inside_the_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    fixture = external_validation_fixture(package_app, monkeypatch, tmp_path)

    package_app.assert_no_external_macho_dependencies(fixture.app)


@pytest.mark.parametrize("case", ["outside-rpath-first", "unresolved", "tool-keeps-cellar-rpath"])
def test_external_dependency_validation_rejects_libraries_loaded_from_outside_the_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    package_app = load_package_app_module()
    fixture = external_validation_fixture(package_app, monkeypatch, tmp_path)
    if case == "outside-rpath-first":
        # dyld would load the Homebrew copy before the bundled one.
        fixture.rpaths[fixture.library] = [str(fixture.outside), "@loader_path"]
        expected = r"@rpath/libndr.6.dylib -> .*Cellar/lib/libndr.6.dylib"
    elif case == "unresolved":
        fixture.rpaths[fixture.library] = []
        expected = r"@rpath/libndr.6.dylib -> unresolved"
    else:
        # The v3.1.2 smbclient: a Cellar rpath, even with its libraries bundled.
        fixture.rpaths[fixture.tool] = ["/opt/homebrew/Cellar/samba/4.25.0/lib"]
        expected = r"smbclient: LC_RPATH /opt/homebrew/Cellar/samba/4.25.0/lib"

    with pytest.raises(RuntimeError, match=expected):
        package_app.assert_no_external_macho_dependencies(fixture.app)


def write_tool(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def test_smoke_tools_runs_each_bundled_tool(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    log = tmp_path / "ran"
    for tool in package_app.REQUIRED_HOST_TOOLS:
        write_tool(tools / tool, f'echo "{tool} $*" >> "{log}"')

    package_app.smoke_tools(app)

    assert sorted(log.read_text(encoding="utf-8").splitlines()) == ["smbclient --version", "sshpass -V"]


def test_smoke_tools_rejects_a_tool_dyld_cannot_launch(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    tools = app / "Contents" / "Resources" / "Tools" / "bin"
    write_tool(tools / "sshpass", "exit 0")
    write_tool(tools / "smbclient", 'echo "dyld: Library not loaded: @rpath/liblibsmb-private-samba.dylib" >&2; exit 134')

    with pytest.raises(RuntimeError, match=r"(?s)Bundled smbclient does not run \(rc=134\).*Library not loaded"):
        package_app.smoke_tools(app)


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


def test_developer_id_codesign_app_bundle_signs_nested_code_framework_and_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    library = app / "Contents" / "Frameworks" / "libtool.dylib"
    tool = app / "Contents" / "Resources" / "Tools" / "bin" / "smbclient"
    executable = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    framework = app / "Contents" / "Resources" / "Python" / "Runtime" / "Python.framework"
    for path in (library, tool, executable, framework / "Versions" / "3.13" / "Python"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("macho", encoding="utf-8")
    calls: list[Path] = []

    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [executable, tool, library])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})
    monkeypatch.setattr(package_app, "developer_id_codesign", lambda path, identity: calls.append(path))
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: calls.append(Path("verify-macho")))
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: calls.append(Path("verify-app")))

    package_app.developer_id_codesign_app_bundle(app, "Developer ID Application: Example (TEAMID)")

    assert calls == [
        library,
        tool,
        executable,
        framework,
        app,
        Path("verify-macho"),
        Path("verify-app"),
    ]


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


def test_assert_app_bundle_signature_valid_reports_codesign_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:5] == ["codesign", "--verify", "--deep", "--strict", "--verbose=4"]
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bundle format is ambiguous\n")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="bundle format is ambiguous"):
        package_app.assert_app_bundle_signature_valid(app)


def test_create_app_zip_uses_metadata_free_archive_and_validates_unzip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    zip_path = tmp_path / "dist" / "TimeCapsuleSMB.app.zip"
    app.mkdir()
    calls: list[list[str]] = []
    verified: list[Path] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "ditto":
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_bytes(b"zip")
        elif cmd[0] == "unzip":
            extract_dir = Path(cmd[-1])
            (extract_dir / app.name).mkdir()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(package_app, "run", fake_run)
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: verified.append(app))

    package_app.create_app_zip(app, zip_path)

    assert calls[0][:4] == ["ditto", "-c", "-k", "--keepParent"]
    assert "--norsrc" in calls[0]
    assert "--noextattr" in calls[0]
    assert "--noacl" in calls[0]
    assert "--noqtn" in calls[0]
    assert calls[1][:2] == ["unzip", "-q"]
    assert verified and verified[0].name == "TimeCapsuleSMB.app"


def test_validate_app_zip_rejects_root_appledouble_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    zip_path = tmp_path / "TimeCapsuleSMB.app.zip"
    zip_path.write_bytes(b"zip")

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["unzip", "-q"]
        extract_dir = Path(cmd[-1])
        (extract_dir / "TimeCapsuleSMB.app").mkdir()
        (extract_dir / "._TimeCapsuleSMB.app").write_text("appledouble", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(package_app, "run", fake_run)
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: None)

    with pytest.raises(RuntimeError, match="AppleDouble metadata files"):
        package_app.validate_app_zip(zip_path, "TimeCapsuleSMB.app")


def test_notarize_archive_requires_accepted_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    archive = tmp_path / "TimeCapsuleSMB-notary.zip"
    archive.write_bytes(b"zip")
    calls: list[list[str]] = []

    def fake_run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"status":"Invalid","id":"submission-id","message":"bad signature"}',
            stderr="",
        )

    monkeypatch.setattr(package_app, "run_quiet", fake_run_quiet)

    with pytest.raises(RuntimeError, match="bad signature"):
        package_app.notarize_archive(archive, "release-profile", "30m")
    assert calls[0][:4] == ["xcrun", "notarytool", "submit", str(archive)]
    assert "--keychain-profile" in calls[0]
    assert "release-profile" in calls[0]


def test_notarize_archive_returns_submission_id_on_accept(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    archive = tmp_path / "TimeCapsuleSMB-notary.zip"
    archive.write_bytes(b"zip")

    def fake_run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"status":"Accepted","id":"submission-id","message":"Processing complete"}',
            stderr="",
        )

    monkeypatch.setattr(package_app, "run_quiet", fake_run_quiet)

    assert package_app.notarize_archive(archive, "release-profile", "30m") == "submission-id"


def test_ad_hoc_codesign_app_bundle_signs_helper_before_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[Path] = []

    monkeypatch.setattr(package_app, "ad_hoc_codesign", lambda path: calls.append(path))

    package_app.ad_hoc_codesign_app_bundle(app)

    assert calls == [helper, app]


def test_package_app_signs_final_bundle_after_native_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    executable = tmp_path / "swift-build" / "TimeCapsuleSMB"
    helper_executable = tmp_path / "swift-build" / "tcapsule"
    resource_build_dir = tmp_path / "swift-build"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    helper_executable.write_text("helper", encoding="utf-8")
    helper_executable.chmod(0o755)
    calls: list[str] = []

    monkeypatch.setattr(package_app, "resolve_architectures", lambda arch: ("arm64",))
    monkeypatch.setattr(package_app, "build_swift", lambda configuration, architectures: (executable, resource_build_dir))
    monkeypatch.setattr(package_app, "build_helper", lambda configuration, architectures: helper_executable)
    monkeypatch.setattr(package_app, "copy_resources", lambda source, resources: calls.append("resources"))
    monkeypatch.setattr(package_app, "copy_helper_executable", lambda source, destination: calls.append("helper"))
    monkeypatch.setattr(package_app, "copy_python_runtime", lambda args, resources, architectures: resources / "Python" / "Runtime" / "bin" / "python3")
    monkeypatch.setattr(package_app, "create_python_packages", lambda python, resources, architectures, use_cache=True: calls.append("packages"))
    monkeypatch.setattr(package_app, "finalize_python_bundle", lambda resources: calls.append("python-sign"))
    monkeypatch.setattr(package_app, "copy_distribution", lambda resources: calls.append("distribution"))
    monkeypatch.setattr(package_app, "copy_native_tools_layer", lambda app, architectures, use_cache=True: calls.append("native"))
    monkeypatch.setattr(package_app, "remove_appledouble_files", lambda app: calls.append("clean"))
    monkeypatch.setattr(package_app, "assert_no_appledouble_files", lambda app: calls.append("assert-clean"))
    monkeypatch.setattr(package_app, "ad_hoc_codesign_app_bundle", lambda app: calls.append("app-sign"))
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: calls.append("app-verify"))
    monkeypatch.setattr(package_app, "assert_bundle_layout", lambda app, **kwargs: calls.append("assert"))

    args = SimpleNamespace(
        arch="native",
        configuration="release",
        output=tmp_path / "dist",
        icon=None,
        no_cache=False,
        full_validation=False,
        skip_smoke=True,
        codesign_identity=None,
        notarize=False,
        notary_profile="tcapsulesmb-notary",
        notary_timeout="30m",
        zip=False,
        zip_output=None,
    )

    result = package_app.package_app(args)

    assert calls[-6:] == ["native", "clean", "assert-clean", "app-sign", "app-verify", "assert"]
    assert result.app == tmp_path / "dist" / "TimeCapsuleSMB.app"
    assert result.zip_path is None
    assert result.notarization_archive is None


def test_package_app_holds_the_cache_lock_for_the_whole_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    args = SimpleNamespace()
    expected = package_app.PackageResult(app=tmp_path / "dist" / "TimeCapsuleSMB.app", zip_path=None, notarization_archive=None)
    seen: list[bool] = []

    def build(received):
        assert received is args
        seen.append(package_cache_lock_is_held(tmp_path))
        return expected

    monkeypatch.setattr(package_app, "build_app_package", build)

    assert package_app.package_app(args) is expected
    assert seen == [True]
    assert not package_cache_lock_is_held(tmp_path)


def test_package_app_releases_the_cache_lock_when_the_build_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)

    def build(args):
        raise RuntimeError("swift build failed")

    monkeypatch.setattr(package_app, "build_app_package", build)

    with pytest.raises(RuntimeError, match="swift build failed"):
        package_app.package_app(SimpleNamespace())
    assert not package_cache_lock_is_held(tmp_path)


def test_package_app_result_includes_zip_and_notarization_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    executable = tmp_path / "swift-build" / "TimeCapsuleSMB"
    helper_executable = tmp_path / "swift-build" / "tcapsule"
    resource_build_dir = tmp_path / "swift-build"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    helper_executable.write_text("helper", encoding="utf-8")
    helper_executable.chmod(0o755)

    monkeypatch.setattr(package_app, "resolve_architectures", lambda arch: ("arm64",))
    monkeypatch.setattr(package_app, "build_swift", lambda configuration, architectures: (executable, resource_build_dir))
    monkeypatch.setattr(package_app, "build_helper", lambda configuration, architectures: helper_executable)
    monkeypatch.setattr(package_app, "copy_resources", lambda source, resources: None)
    monkeypatch.setattr(package_app, "copy_helper_executable", lambda source, destination: None)
    monkeypatch.setattr(package_app, "copy_python_runtime", lambda args, resources, architectures: resources / "Python" / "Runtime" / "bin" / "python3")
    monkeypatch.setattr(package_app, "create_python_packages", lambda python, resources, architectures, use_cache=True: None)
    monkeypatch.setattr(package_app, "finalize_python_bundle", lambda resources: None)
    monkeypatch.setattr(package_app, "copy_distribution", lambda resources: None)
    monkeypatch.setattr(package_app, "copy_native_tools_layer", lambda app, architectures, use_cache=True: None)
    monkeypatch.setattr(package_app, "remove_appledouble_files", lambda app: None)
    monkeypatch.setattr(package_app, "assert_no_appledouble_files", lambda app: None)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_app_bundle", lambda app: None)
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: None)
    monkeypatch.setattr(package_app, "assert_bundle_layout", lambda app, **kwargs: None)
    monkeypatch.setattr(package_app, "developer_id_codesign_app_bundle", lambda app, identity: None)
    monkeypatch.setattr(package_app, "notarize_app", lambda app, output_dir, **kwargs: "submission-id")
    monkeypatch.setattr(package_app, "create_app_zip", lambda app, zip_path: zip_path.write_bytes(b"zip"))

    args = SimpleNamespace(
        arch="native",
        configuration="release",
        output=tmp_path / "dist",
        icon=None,
        no_cache=False,
        full_validation=False,
        skip_smoke=True,
        codesign_identity="Developer ID Application: Example (TEAMID)",
        notarize=True,
        notary_profile="tcapsulesmb-notary",
        notary_timeout="30m",
        zip=True,
        zip_output=None,
    )

    result = package_app.package_app(args)

    assert result.app == tmp_path / "dist" / "TimeCapsuleSMB.app"
    assert result.notarization_archive == tmp_path / "dist" / "TimeCapsuleSMB-notary.zip"
    assert result.zip_path == tmp_path / "dist" / "TimeCapsuleSMB.app.zip"


def test_main_prints_labeled_artifact_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    result = package_app.PackageResult(
        app=tmp_path / "TimeCapsuleSMB.app",
        notarization_archive=tmp_path / "TimeCapsuleSMB-notary.zip",
        zip_path=tmp_path / "TimeCapsuleSMB.app.zip",
    )
    monkeypatch.setattr(package_app, "package_app", lambda args: result)

    assert package_app.main([]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"App bundle: {result.app}",
        f"Notarization archive: {result.notarization_archive}",
        f"Distributable zip: {result.zip_path}",
    ]


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


def test_macho_files_under_includes_object_files_but_not_archives(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    root = tmp_path / "root"
    root.mkdir()
    object_file = root / "python.o"
    archive = root / "libpython.a"
    object_file.write_text("macho object", encoding="utf-8")
    archive.write_text("archive", encoding="utf-8")

    paths = package_app.macho_files_under([root])

    assert object_file in paths
    assert archive not in paths


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


def test_runtime_macho_architecture_validation_checks_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    helper = tmp_path / "TimeCapsuleSMB.app" / "Contents" / "Helpers" / "tcapsule"
    helper.parent.mkdir(parents=True)
    helper.write_text("helper", encoding="utf-8")

    def fake_architectures(path: Path) -> set[str]:
        if path.resolve() == helper.resolve():
            return {"arm64"}
        return set()

    monkeypatch.setattr(package_app, "macho_architectures", fake_architectures)
    monkeypatch.setattr(package_app, "macho_dependencies", lambda path: [])

    with pytest.raises(RuntimeError, match=r"tcapsule: missing x86_64"):
        package_app.assert_runtime_macho_architectures(tmp_path / "TimeCapsuleSMB.app", ("arm64", "x86_64"))


def test_python_dependency_validation_uses_bundled_python(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
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
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONPYCACHEPREFIX"] == str(tmp_path / ".build" / "package-app" / "python-bytecode")


def test_validate_app_resources_rejects_swift_resource_bundle_crash(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    executable = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho resource crash >&2\nexit 70\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(RuntimeError, match="App executable resource validation failed"):
        package_app.validate_app_resources(app)
