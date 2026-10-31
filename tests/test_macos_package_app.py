from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import io
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


PACKAGE_SCRIPT = Path(__file__).resolve().parents[1] / "macos" / "TimeCapsuleSMB" / "tools" / "package_app.py"


PACKAGES = "certifi==2026.10.23\nifaddr==0.2.0\n"

def load_package_app_module():
    spec = importlib.util.spec_from_file_location("package_app", PACKAGE_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("failure", [None, RuntimeError("step failed"), KeyboardInterrupt()])
def test_timed_step_reports_start_and_elapsed_time_without_suppressing_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException | None,
) -> None:
    package_app = load_package_app_module()
    ticks = iter([100.0, 102.25])
    monkeypatch.setattr(package_app.time, "perf_counter", lambda: next(ticks))

    def execute_step() -> None:
        with package_app.timed_step("Checking bundle"):
            # The user sees what is running before the work finishes.
            assert capsys.readouterr().err == "[package] Checking bundle...\n"
            if failure is not None:
                raise failure

    if failure is None:
        execute_step()
    else:
        with pytest.raises(type(failure)) as raised:
            execute_step()
        assert raised.value is failure
    output = capsys.readouterr()
    status = "done" if failure is None else "failed"
    assert output.err == f"[package] Checking bundle: {status} (2.2s)\n"
    assert output.out == ""


def test_timed_step_decorator_times_each_call_and_preserves_return_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_app = load_package_app_module()
    ticks = iter([10.0, 11.0, 20.0, 23.5])
    monkeypatch.setattr(package_app.time, "perf_counter", lambda: next(ticks))

    @package_app.timed_step("Copying layer")
    def copy_layer(value: str) -> str:
        return value

    assert copy_layer("first") == "first"
    assert copy_layer("second") == "second"
    assert capsys.readouterr().err.splitlines() == [
        "[package] Copying layer...",
        "[package] Copying layer: done (1.0s)",
        "[package] Copying layer...",
        "[package] Copying layer: done (3.5s)",
    ]


PLURALS_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict></dict></plist>
"""
# Swift Build writes the test target's resource bundle beside the app's.
TEST_RESOURCE_BUNDLE_NAME = "TimeCapsuleSMBMac_TimeCapsuleSMBAppTests.bundle"


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
    create_fake_sparkle(app / "Contents" / "Frameworks", xpc_services=False)


def create_fake_sparkle(directory: Path, *, xpc_services: bool = True) -> Path:
    """Sparkle.framework as swift build leaves it: a versioned bundle whose
    top level is symlinks into Versions/Current."""
    framework = directory / "Sparkle.framework"
    version = framework / "Versions" / "B"
    (version / "Updater.app" / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (version / "Sparkle").write_text("sparkle", encoding="utf-8")
    (version / "Autoupdate").write_text("autoupdate", encoding="utf-8")
    (version / "Updater.app" / "Contents" / "MacOS" / "Updater").write_text("updater", encoding="utf-8")
    (framework / "Versions" / "Current").symlink_to("B")
    (framework / "Sparkle").symlink_to("Versions/Current/Sparkle")
    if xpc_services:
        (version / "XPCServices" / "Installer.xpc").mkdir(parents=True)
        (framework / "XPCServices").symlink_to("Versions/Current/XPCServices")
    return framework


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


def test_assert_bundle_layout_checks_helper_python_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, distribution / "bin" / "payloads"):
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
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: None)
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
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, distribution / "bin"):
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
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, distribution / "bin"):
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
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, executable.parent, python_packages, distribution / "bin"):
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
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, distribution / "bin"):
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
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, distribution / "bin"):
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
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: None)
    monkeypatch.setattr(package_app, "validate_app_resources", lambda app: None)

    package_app.assert_bundle_layout(app)


@pytest.mark.parametrize("extra_bundle", [TEST_RESOURCE_BUNDLE_NAME, "Other.bundle"])
def test_assert_bundle_layout_rejects_resource_bundles_the_app_does_not_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_bundle: str
) -> None:
    package_app = load_package_app_module()
    app = app_with_deep_resource_bundle(package_app, tmp_path)
    (app / "Contents" / "Resources" / extra_bundle / "Contents" / "Resources" / "Fixtures").mkdir(parents=True)
    monkeypatch.setattr(package_app, "artifact_paths", lambda: [])
    monkeypatch.setattr(package_app, "assert_python_dependencies_are_bundled", lambda app: None)
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies", lambda app: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid", lambda app: None)
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: None)
    monkeypatch.setattr(package_app, "validate_app_resources", lambda app: None)

    with pytest.raises(RuntimeError, match=f"the app does not use:\n  - {re.escape(extra_bundle)}$"):
        package_app.assert_bundle_layout(app)


def test_assert_bundle_layout_requires_plurals_in_the_swift_build_resource_bundle(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    app = app_with_deep_resource_bundle(package_app, tmp_path)
    plurals = (app / "Contents" / "Resources" / package_app.RESOURCE_BUNDLE_NAME / "Contents" / "Resources"
               / "en.lproj" / "Localizable.stringsdict")
    plurals.unlink()

    with pytest.raises(RuntimeError, match="missing Swift resource bundle plural localizations"):
        package_app.assert_bundle_layout(app)


def fake_swift_build(package_app, layout: str, calls: list[list[str]]):
    """swift build as each build system lays out its products under the
    scratch path: the native one in apple/Products/Release for several
    architectures and <arch>-apple-macosx/release for one, Swift Build in
    out/Products/Release. Several --arch values build universal products.
    Like the real build, it writes the test target's resource bundle beside
    the app's."""

    def bin_dir(cmd: list[str]) -> Path:
        scratch = Path(cmd[cmd.index("--scratch-path") + 1])
        architectures = [cmd[index + 1] for index, word in enumerate(cmd) if word == "--arch"]
        if layout == "swiftbuild":
            return scratch / "out" / "Products" / "Release"
        if len(architectures) > 1:
            return scratch / "apple" / "Products" / "Release"
        return scratch / f"{architectures[0]}-apple-macosx" / "release"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert cmd[:2] == ["swift", "build"], cmd
        assert kwargs.get("cwd") == package_app.PACKAGE_ROOT
        if "--show-bin-path" in cmd:
            assert kwargs.get("capture") is True
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{bin_dir(cmd)}\n")
        architectures = " ".join(cmd[index + 1] for index, word in enumerate(cmd) if word == "--arch")
        for product in (package_app.PRODUCT_NAME, package_app.HELPER_PRODUCT_NAME):
            executable = bin_dir(cmd) / product
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text(f"{product} {architectures}", encoding="utf-8")
            executable.chmod(0o755)
        (bin_dir(cmd) / package_app.RESOURCE_BUNDLE_NAME).mkdir(exist_ok=True)
        if not (bin_dir(cmd) / "Sparkle.framework").exists():
            create_fake_sparkle(bin_dir(cmd))
        fixtures = bin_dir(cmd) / TEST_RESOURCE_BUNDLE_NAME / "Contents" / "Resources" / "Fixtures"
        fixtures.mkdir(parents=True, exist_ok=True)
        (fixtures / "fixture.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    return fake_run


@pytest.mark.parametrize("layout", ["native", "swiftbuild"])
def test_build_swift_builds_every_architecture_in_one_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, layout: str
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(package_app, "run", fake_swift_build(package_app, layout, calls))

    executable, helper, resource_build_dir = package_app.build_swift("release", ("arm64", "x86_64"))

    scratch = tmp_path / ".build" / "package-swift" / "arm64-x86_64"
    build = ["swift", "build", "-c", "release", "--scratch-path", str(scratch), "--arch", "arm64", "--arch", "x86_64"]
    # One build makes the universal products; the bin path query names the same build.
    assert calls == [build, [*build, "--show-bin-path"]]
    assert resource_build_dir.is_relative_to(scratch)
    assert executable == resource_build_dir / "TimeCapsuleSMB"
    assert helper == resource_build_dir / "tcapsule"
    assert executable.read_text() == "TimeCapsuleSMB arm64 x86_64"
    assert helper.read_text() == "tcapsule arm64 x86_64"
    assert (resource_build_dir / package_app.RESOURCE_BUNDLE_NAME).is_dir()


@pytest.mark.skipif(shutil.which("clang") is None or sys.platform != "darwin", reason="needs Apple's clang, nm and strip")
def test_strip_debug_map_removes_the_build_paths_and_keeps_symbols(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "main.c").write_text("int helper_entry(void) { return 0; }\nint main(void) { return helper_entry(); }\n", encoding="utf-8")
    executable = tmp_path / "tcapsule"
    subprocess.run(["clang", "-g", "-o", str(executable), str(checkout / "main.c")], cwd=checkout, check=True)
    assert str(checkout).encode() in executable.read_bytes()

    package_app.strip_debug_map(executable)

    assert str(checkout).encode() not in executable.read_bytes()
    symbols = subprocess.run(["nm", str(executable)], capture_output=True, text=True, check=True).stdout
    assert "_helper_entry" in symbols
    assert subprocess.run([str(executable)], check=False).returncode == 0


def test_build_swift_keeps_each_architecture_set_in_its_own_scratch_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Building another architecture in the same scratch path makes Swift Build
    # recompile everything next time; universal and native builds must not share.
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(package_app, "run", fake_swift_build(package_app, "swiftbuild", calls))

    universal, _, _ = package_app.build_swift("release", ("arm64", "x86_64"))
    native, _, _ = package_app.build_swift("release", ("arm64",))
    universal_again, _, _ = package_app.build_swift("release", ("arm64", "x86_64"))

    scratch_paths = [Path(cmd[cmd.index("--scratch-path") + 1]) for cmd in calls if "--show-bin-path" not in cmd]
    assert scratch_paths == [
        tmp_path / ".build" / "package-swift" / "arm64-x86_64",
        tmp_path / ".build" / "package-swift" / "arm64",
        tmp_path / ".build" / "package-swift" / "arm64-x86_64",
    ]
    assert universal == universal_again
    assert universal.read_text() == "TimeCapsuleSMB arm64 x86_64"
    assert native.read_text() == "TimeCapsuleSMB arm64"
    # Neither build touched swift test's own .build products.
    assert not (tmp_path / ".build" / "out").exists()


def test_build_swift_ignores_a_stale_native_build_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A native build from before the switch to Swift Build is still on disk;
    # packaging it shipped a months-old app and a bundle without plurals.
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    scratch = tmp_path / ".build" / "package-swift" / "arm64"
    stale = scratch / "arm64-apple-macosx" / "release" / "TimeCapsuleSMB"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(package_app, "run", fake_swift_build(package_app, "swiftbuild", calls))

    executable, helper, resource_build_dir = package_app.build_swift("release", ("arm64",))

    assert executable.read_text(encoding="utf-8") == "TimeCapsuleSMB arm64"
    assert helper.read_text() == "tcapsule arm64"
    assert resource_build_dir == scratch / "out" / "Products" / "Release"
    assert stale.read_text() == "stale"


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


def test_build_swift_reports_an_empty_bin_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(package_app, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout=""))

    with pytest.raises(RuntimeError, match="printed nothing for arm64, x86_64 release"):
        package_app.build_swift("release", ("arm64", "x86_64"))


def test_build_swift_rejects_a_missing_helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    fake_run = fake_swift_build(package_app, "swiftbuild", [])

    def missing_helper(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = fake_run(cmd, **kwargs)
        if "--show-bin-path" not in cmd:
            (tmp_path / ".build" / "package-swift" / "arm64" / "out" / "Products" / "Release" / "tcapsule").unlink()
        return result

    monkeypatch.setattr(package_app, "run", missing_helper)
    with pytest.raises(RuntimeError, match="did not produce .*tcapsule"):
        package_app.build_swift("release", ("arm64",))


def swift_products_with_both_resource_bundles(package_app, build_dir: Path) -> None:
    """A Swift Build products directory: the app's bundle in Swift Build's
    layout, the test target's bundle with its fixtures, and the executables."""
    english = build_dir / package_app.RESOURCE_BUNDLE_NAME / "Contents" / "Resources" / "en.lproj"
    english.mkdir(parents=True)
    (english / "Localizable.strings").write_text('"screen.readiness" = "Readiness";\n', encoding="utf-8")
    (english / "Localizable.stringsdict").write_text(PLURALS_PLIST, encoding="utf-8")
    fixtures = build_dir / TEST_RESOURCE_BUNDLE_NAME / "Contents" / "Resources" / "Fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "fixture.json").write_text("{}", encoding="utf-8")
    (build_dir / package_app.PRODUCT_NAME).write_text("app", encoding="utf-8")


def test_copy_resources_copies_only_the_app_resource_bundle(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    build_dir = tmp_path / "Release"
    swift_products_with_both_resource_bundles(package_app, build_dir)
    resources = tmp_path / "Resources"
    # A copy from an earlier run is replaced, not merged into.
    stale = resources / package_app.RESOURCE_BUNDLE_NAME / "stale.strings"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")

    package_app.copy_resources(build_dir, resources)

    assert sorted(path.name for path in resources.iterdir()) == [package_app.RESOURCE_BUNDLE_NAME]
    english = package_app.resource_bundle_localization(resources / package_app.RESOURCE_BUNDLE_NAME, "en")
    assert (english / "Localizable.stringsdict").read_text(encoding="utf-8") == PLURALS_PLIST
    assert not stale.exists()


def test_copy_resources_rejects_a_build_without_the_app_resource_bundle(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    build_dir = tmp_path / "Release"
    swift_products_with_both_resource_bundles(package_app, build_dir)
    shutil.rmtree(build_dir / package_app.RESOURCE_BUNDLE_NAME)
    resources = tmp_path / "Resources"
    resources.mkdir()

    # The test target's bundle alone must not pass for the app's resources.
    with pytest.raises(RuntimeError, match=f"did not produce .*{re.escape(package_app.RESOURCE_BUNDLE_NAME)}"):
        package_app.copy_resources(build_dir, resources)
    assert list(resources.iterdir()) == []


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("architecture", ["arm64", ""])
def test_macho_inspection_cache_is_scoped_and_discards_results_after_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool, architecture: str,
) -> None:
    package_app = load_package_app_module()
    binary = tmp_path / "binary"
    binary.write_text(architecture)
    alias = tmp_path / "alias"
    alias.symlink_to(binary)
    calls: list[Path] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        path = Path(cmd[-1])
        calls.append(path)
        return subprocess.CompletedProcess(cmd, 0, stdout=path.read_text(), stderr="")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)
    expected = {architecture} if architecture else set()
    with pytest.raises(RuntimeError) if fail else package_app.contextlib.nullcontext():
        with package_app.macho_inspection_session():
            assert package_app.macho_architectures(binary) == expected
            assert package_app.macho_architectures(alias) == expected
            assert len(calls) == 1
            if fail:
                raise RuntimeError("validation failed")

    # Signing/replacement happens outside an immutable validation session.
    binary.write_text("x86_64")
    with package_app.macho_inspection_session():
        assert package_app.macho_architectures(binary) == {"x86_64"}
        assert package_app.macho_architectures(binary) == {"x86_64"}
    assert len(calls) == 2
    package_app.macho_architectures(binary)
    package_app.macho_architectures(binary)
    assert len(calls) == 4


def counting_macho_tools(package_app, monkeypatch: pytest.MonkeyPatch, outputs: dict[str, tuple[int, str]]) -> list[list[str]]:
    """Fake Mach-O tools whose output depends only on the file's bytes, like
    the real ones; {path} in an output is the path the tool was given."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        returncode, stdout = outputs[Path(cmd[-1]).read_text()]
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout.format(path=cmd[-1]), stderr="tool error")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)
    return calls


def test_macho_tool_cache_reuses_output_across_runs_until_the_bytes_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    binary = tmp_path / "app" / "libfoo.dylib"
    binary.parent.mkdir()
    binary.write_text("arm64 build")
    calls = counting_macho_tools(package_app, monkeypatch, {
        "arm64 build": (0, "arm64\n"), "universal build": (0, "x86_64 arm64\n"), "text": (1, ""),
    })

    with package_app.macho_tool_cache():
        assert package_app.macho_architectures(binary) == {"arm64"}
    # A later run, with nothing left in memory, reads the stored result.
    with package_app.macho_tool_cache():
        assert package_app.macho_architectures(binary) == {"arm64"}
    assert len(calls) == 1

    # A rewrite in place (install_name_tool, codesign) is new bytes.
    binary.write_text("universal build")
    with package_app.macho_tool_cache():
        assert package_app.macho_architectures(binary) == {"arm64", "x86_64"}
        assert package_app.macho_architectures(binary) == {"arm64", "x86_64"}
    assert len(calls) == 2

    # A file the tool rejects is not Mach-O for the same bytes either.
    text = tmp_path / "app" / "README"
    text.write_text("text")
    for _ in range(2):
        with package_app.macho_tool_cache():
            assert package_app.macho_architectures(text) == set()
    assert len(calls) == 3

    # Outside a packaging run every call runs the tool.
    package_app.macho_architectures(binary)
    assert len(calls) == 4


def test_macho_tool_cache_lets_the_tool_report_a_path_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Validation names the app's executables whether or not they are there;
    # an app without them must reach the tool's own error, not a crash.
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="can't open input file")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)
    staging = tmp_path / "entry"
    (staging / "Contents" / "Resources").mkdir(parents=True)

    with package_app.macho_tool_cache():
        assert package_app.macho_architectures(staging / "Contents" / "MacOS" / "TimeCapsuleSMB") == set()
        package_app.assert_runtime_macho_architectures(staging, ("arm64",))
    assert calls
    # Nothing is kept for a path without bytes.
    cache_dir = tmp_path / ".build" / "package-app" / package_app.MACHO_TOOL_CACHE_DIR
    assert not cache_dir.exists() or not any(cache_dir.iterdir())


def test_macho_tool_cache_shares_a_record_between_copies_at_other_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    staged, bundled = tmp_path / "staging" / "python3.13", tmp_path / "TimeCapsuleSMB.app" / "python3.13"
    for path in (staged, bundled):
        path.parent.mkdir(parents=True)
        path.write_text("fat python")
    calls = counting_macho_tools(package_app, monkeypatch, {"fat python": (0, VTOOL_FAT)})
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})

    with package_app.macho_tool_cache():
        assert package_app.macho_minimum_macos(staged) == {"x86_64": "10.13", "arm64": "14.0"}
    with package_app.macho_tool_cache():
        # vtool names the file in its output; the record gives back this path.
        assert package_app.macho_minimum_macos(bundled) == {"x86_64": "10.13", "arm64": "14.0"}
    assert len(calls) == 1
    record = json.loads(next((tmp_path / ".build" / "package-app" / package_app.MACHO_TOOL_CACHE_DIR).iterdir()).read_text())
    assert str(staged) not in json.dumps(record)


def test_macho_tool_cache_keeps_install_names_that_match_the_inspected_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # A dylib linked with its build path as its install name, then copied into
    # the bundle unchanged: the copy still names the build path, and
    # validation must see that, not the copy's own path.
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    staged, bundled = tmp_path / "build" / "libfoo.dylib", tmp_path / "TimeCapsuleSMB.app" / "libfoo.dylib"
    for path in (staged, bundled):
        path.parent.mkdir(parents=True)
        path.write_text("absolute install name")
    calls = counting_macho_tools(package_app, monkeypatch, {"absolute install name": (0, "")})
    staged_name = str(staged)
    outputs = {
        "otool -D": f"{{path}}:\n{staged_name}\n",
        "otool -L": (f"{{path}} (architecture arm64):\n\t{staged_name} (compatibility version 1.0.0)\n"
                     f"\t{staged_name}.framework/Foo (compatibility version 1.0.0)\n"),
        "otool -l": f"{{path}}:\nLoad command 3\n          cmd LC_ID_DYLIB\n         name {staged_name} (offset 24)\n",
    }

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=outputs[" ".join(cmd[:2])].format(path=cmd[-1]), stderr="")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)
    with package_app.macho_tool_cache():
        uncached = {args: package_app.macho_tool(args.split(), staged).stdout for args in outputs}
    with package_app.macho_tool_cache():
        cached = {args: package_app.macho_tool(args.split(), bundled).stdout for args in outputs}
        assert package_app.macho_install_name(bundled) == staged_name
        # Not the copy itself, so not skipped as its own ID: a path outside the bundle.
        assert package_app.macho_dependencies(bundled) == [staged_name, staged_name + ".framework/Foo"]
    assert len(calls) == 3
    # Only the header that names the inspected file follows the copy.
    for args in outputs:
        assert cached[args] == uncached[args].replace(f"{staged_name}:", f"{bundled}:", 1).replace(
            f"{staged_name} (architecture", f"{bundled} (architecture", 1)
        assert cached[args].count(staged_name) == uncached[args].count(staged_name) - 1


def test_macho_tool_cache_names_the_copy_in_otools_not_an_object_file_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Executable scripts are candidates too; otool exits 0 for them.
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    staged, bundled = tmp_path / "build" / "tool", tmp_path / "TimeCapsuleSMB.app" / "tool"
    for path in (staged, bundled):
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh")
    calls = counting_macho_tools(package_app, monkeypatch, {"#!/bin/sh": (0, "{path}: is not an object file\n")})
    with package_app.macho_tool_cache():
        package_app.macho_tool(["otool", "-L"], staged)
    with package_app.macho_tool_cache():
        assert package_app.macho_tool(["otool", "-L"], bundled).stdout == f"{bundled}: is not an object file\n"
        assert package_app.macho_dependencies(bundled) == []
        assert package_app.macho_install_name(bundled) is None
    assert len(calls) == 2  # otool -D for the install name; -L came from the record


def test_macho_tool_cache_keeps_embedded_signature_passes_but_rechecks_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    good, bad, objfile = tmp_path / "good.dylib", tmp_path / "bad.dylib", tmp_path / "python.o"
    good.write_text("signed")
    bad.write_text("tampered")
    # codesign keeps an object file's signature in extended attributes: the
    # same bytes pass while signed and fail once a copy has dropped them.
    objfile.write_text("object")
    object_signed = True
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        content = Path(cmd[-1]).read_text()
        if cmd[:2] == ["otool", "-l"]:
            load_commands = "" if content == "object" else "Load command 9\n      cmd LC_CODE_SIGNATURE\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=load_commands, stderr="")
        passes = content == "signed" or (content == "object" and object_signed)
        return subprocess.CompletedProcess(cmd, 0 if passes else 1, stdout="", stderr="code object is not signed at all")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})

    for _ in range(2):
        with package_app.macho_tool_cache():
            package_app.assert_macho_code_signatures_valid_for_paths([good, objfile])
            with pytest.raises(RuntimeError, match="bad.dylib: code object is not signed at all"):
                package_app.assert_macho_code_signatures_valid_for_paths([bad])
    verified = [Path(cmd[-1]).name for cmd in calls if cmd[0] == "codesign"]
    assert verified == ["good.dylib", "python.o", "bad.dylib", "python.o", "bad.dylib"]

    object_signed = False
    with package_app.macho_tool_cache():
        with pytest.raises(RuntimeError, match="python.o: code object is not signed at all"):
            package_app.assert_macho_code_signatures_valid_for_paths([objfile])


def test_macho_tool_cache_ignores_an_unreadable_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    binary = tmp_path / "libfoo.dylib"
    binary.write_text("arm64 build")
    calls = counting_macho_tools(package_app, monkeypatch, {"arm64 build": (0, "arm64\n")})
    record = tmp_path / ".build" / "package-app" / package_app.MACHO_TOOL_CACHE_DIR / f"{hashlib.sha256(b'arm64 build').hexdigest()}.json"
    record.parent.mkdir(parents=True)

    for content in ("{not json", '["a list"]', '{"lipo -archs": "arm64"}'):
        record.write_text(content)
        with package_app.macho_tool_cache():
            assert package_app.macho_architectures(binary) == {"arm64"}
    assert len(calls) == 3
    with package_app.macho_tool_cache():
        assert package_app.macho_architectures(binary) == {"arm64"}
    assert len(calls) == 3


def test_macho_tool_cache_is_off_without_cache_and_keeps_only_recent_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(package_app, "PACKAGE_CACHE_KEEP_ENTRIES", 2)
    binary = tmp_path / "libfoo.dylib"
    binary.write_text("arm64 build")
    calls = counting_macho_tools(package_app, monkeypatch, {"arm64 build": (0, "arm64\n")})
    root = tmp_path / ".build" / "package-app" / package_app.MACHO_TOOL_CACHE_DIR

    with package_app.macho_tool_cache(enabled=False):
        package_app.macho_architectures(binary)
    assert not root.exists()

    # Records of files from older builds; this run reads one file, so keeps two.
    root.mkdir(parents=True)
    for age, name in enumerate(["newer", "older", "oldest"]):
        (root / f"{name}.json").write_text("{}")
        os.utime(root / f"{name}.json", (1_000_000 - age, 1_000_000 - age))
    with package_app.macho_tool_cache():
        package_app.macho_architectures(binary)
    assert sorted(path.name for path in root.iterdir()) == [f"{hashlib.sha256(b'arm64 build').hexdigest()}.json", "newer.json"]
    assert len(calls) == 2


SECURITY_IDENTITIES = """  1) 09448A53AC6A5C1DD4C0DC10232BA4DA149DD50A "Developer ID Application: Example (TEAMID)"
  2) 1111111111111111111111111111111111111111 "Apple Development: Example (OTHER)"
  3) 2222222222222222222222222222222222222222 "Apple Development: Example (THIRD)"
     3 valid identities found
"""


@pytest.mark.parametrize(("identity", "expected"), [
    ("Developer ID Application: Example (TEAMID)", "09448A53AC6A5C1DD4C0DC10232BA4DA149DD50A"),
    ("Developer ID Application", "09448A53AC6A5C1DD4C0DC10232BA4DA149DD50A"),
    ("Apple Development", None),
    ("Developer ID Application: Someone Else", None),
])
def test_codesign_certificate_sha1_takes_the_one_identity_codesign_would_use(
    monkeypatch: pytest.MonkeyPatch, identity: str, expected: str | None,
) -> None:
    package_app = load_package_app_module()

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd == ["security", "find-identity", "-v", "-p", "codesigning"]
        return subprocess.CompletedProcess(cmd, 0, stdout=SECURITY_IDENTITIES, stderr="")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)
    assert package_app.codesign_certificate_sha1(identity) == expected


def test_codesign_certificate_sha1_accepts_a_hash_and_survives_security_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app.subprocess, "run",
                        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 44, stdout="", stderr="no keychain"))

    assert package_app.codesign_certificate_sha1("09448a53ac6a5c1dd4c0dc10232ba4da149dd50a") == "09448A53AC6A5C1DD4C0DC10232BA4DA149DD50A"
    assert package_app.codesign_certificate_sha1("Developer ID Application: Example (TEAMID)") is None


def signature_cache_app(package_app, root: Path, leaves: dict[str, str]) -> tuple[Path, list[Path]]:
    app = root / "TimeCapsuleSMB.app"
    package_app.bundled_python_framework(app).mkdir(parents=True)
    paths = []
    for relative, content in leaves.items():
        path = app / "Contents" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        paths.append(path)
    return app, paths


def fake_developer_id_signing(package_app, monkeypatch: pytest.MonkeyPatch, certificate: str | None):
    signed: list[Path] = []

    def sign(path: Path, identity: str) -> None:
        assert identity == "Developer ID Application: Example (TEAMID)"
        signed.append(path)
        if path.is_file():
            path.write_text(f"{path.read_text()} signed by {certificate} as {path.name}")

    monkeypatch.setattr(package_app, "developer_id_codesign", sign)
    monkeypatch.setattr(package_app, "codesign_certificate_sha1", lambda identity: certificate)
    monkeypatch.setattr(package_app, "macho_validation_roots",
                        lambda app: sorted(path for path in (app / "Contents").rglob("*") if path.is_file()))
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})
    return signed


def test_developer_id_signatures_are_reused_for_the_same_bytes_name_and_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    identity = "Developer ID Application: Example (TEAMID)"
    leaves = {"Frameworks/libexample.dylib": "example", "Resources/Python/site-packages/zopfli/zopfli.so": "zopfli"}
    signed = fake_developer_id_signing(package_app, monkeypatch, "CERT1")
    app, (library, extension) = signature_cache_app(package_app, tmp_path / "first", leaves)
    package_app.developer_id_codesign_app_bundle(app, identity, use_cache=True)
    framework = package_app.bundled_python_framework(app)
    assert sorted(signed[:-2]) == sorted([library, extension]) and signed[-2:] == [framework, app]
    first_bytes = {path.name: path.read_text() for path in (library, extension)}

    # The next build has the same files: each signed copy is reused, and only
    # the enclosing bundles are signed again.
    signed.clear()
    app, (library, extension) = signature_cache_app(package_app, tmp_path / "second", leaves)
    package_app.developer_id_codesign_app_bundle(app, identity, use_cache=True)
    assert signed == [package_app.bundled_python_framework(app), app]
    assert {path.name: path.read_text() for path in (library, extension)} == first_bytes

    # Other bytes, another name (the default identifier) or another certificate sign again.
    signed.clear()
    app, (library, extension) = signature_cache_app(
        package_app, tmp_path / "third", {"Frameworks/libexample.dylib": "example 2", "Resources/Python/site-packages/zopfli/_zopfli.so": "zopfli"},
    )
    package_app.developer_id_codesign_app_bundle(app, identity, use_cache=True)
    assert sorted(signed[:-2]) == sorted([library, extension])
    signed = fake_developer_id_signing(package_app, monkeypatch, "CERT2")
    app, (library, extension) = signature_cache_app(package_app, tmp_path / "fourth", leaves)
    package_app.developer_id_codesign_app_bundle(app, identity, use_cache=True)
    assert sorted(signed[:-2]) == sorted([library, extension])
    assert library.read_text() == "example signed by CERT2 as libexample.dylib"


def test_developer_id_signatures_are_not_reused_without_cache_or_a_known_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    identity = "Developer ID Application: Example (TEAMID)"
    leaves = {"Frameworks/libexample.dylib": "example"}
    # The same file twice in each mode: nothing is stored, so it is signed every time.
    for build, (certificate, use_cache) in enumerate([("CERT1", False), ("CERT1", False), (None, True), (None, True)]):
        signed = fake_developer_id_signing(package_app, monkeypatch, certificate)
        app, (library,) = signature_cache_app(package_app, tmp_path / str(build), leaves)
        package_app.developer_id_codesign_app_bundle(app, identity, use_cache=use_cache)
        assert signed == [library, package_app.bundled_python_framework(app), app]
    assert not (tmp_path / ".build" / "package-app" / package_app.DEVELOPER_ID_SIGNATURE_CACHE_DIR).exists()


def test_signatures_codesign_keeps_outside_the_bytes_are_never_copied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # codesign signs an object file (python.o) in its extended attributes and
    # leaves its bytes alone; a copy of those bytes reached Apple unsigned.
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    signed = fake_developer_id_signing(package_app, monkeypatch, "CERT1")
    monkeypatch.setattr(package_app, "developer_id_codesign", lambda path, identity: signed.append(path))
    leaves = {"Resources/Python/config/python.o": "object"}
    for build in range(2):
        app, (objfile,) = signature_cache_app(package_app, tmp_path / str(build), leaves)
        package_app.developer_id_codesign_app_bundle(app, "Developer ID Application: Example (TEAMID)", use_cache=True)
        assert signed[-3:] == [objfile, package_app.bundled_python_framework(app), app]
    assert list((tmp_path / ".build" / "package-app" / package_app.DEVELOPER_ID_SIGNATURE_CACHE_DIR).iterdir()) == []


def test_failed_developer_id_signing_stores_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    fake_developer_id_signing(package_app, monkeypatch, "CERT1")
    monkeypatch.setattr(package_app, "developer_id_codesign", lambda path, identity: (_ for _ in ()).throw(RuntimeError("timestamp service unavailable")))
    app, _ = signature_cache_app(package_app, tmp_path, {"Frameworks/libexample.dylib": "example"})

    with pytest.raises(RuntimeError, match="timestamp service unavailable"):
        package_app.developer_id_codesign_app_bundle(app, "Developer ID Application: Example (TEAMID)", use_cache=True)
    assert list((tmp_path / ".build" / "package-app" / package_app.DEVELOPER_ID_SIGNATURE_CACHE_DIR).iterdir()) == []


def test_developer_id_signature_cache_keeps_a_few_builds_of_signatures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(package_app, "PACKAGE_CACHE_KEEP_ENTRIES", 2)
    signed = fake_developer_id_signing(package_app, monkeypatch, "CERT1")
    root = tmp_path / ".build" / "package-app" / package_app.DEVELOPER_ID_SIGNATURE_CACHE_DIR
    # Each build changes the helper; two builds of one file each are kept.
    for build in range(4):
        app, _ = signature_cache_app(package_app, tmp_path / str(build), {"Helpers/tcapsule": f"helper {build}"})
        package_app.developer_id_codesign_app_bundle(app, "Developer ID Application: Example (TEAMID)", use_cache=True)
        for index, entry in enumerate(sorted(root.iterdir(), key=lambda path: path.stat().st_mtime)):
            os.utime(entry, (1_000_000 + build * 10 + index,) * 2)
    assert sorted(path.read_text() for path in root.iterdir()) == [
        "helper 2 signed by CERT1 as tcapsule", "helper 3 signed by CERT1 as tcapsule",
    ]
    assert len(signed) == 4 * 3


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

    def fake_runtime_source(args: object) -> tuple[Path, dict[str, object]]:
        return source, {"source_sha256": "pkg"}

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

    # --no-cache rebuilds the entry in place, for later builds to reuse.
    rebuilt = package_app.prepared_python_framework(args, ("arm64", "x86_64"), reuse=False)
    assert rebuilt == first
    assert len(calls) == 2
    assert package_app.prepared_python_framework(args, ("arm64", "x86_64")) == rebuilt
    assert len(calls) == 2


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

    monkeypatch.setattr(package_app, "run", fake_run)
    monkeypatch.setattr(package_app, "remove_optional_zeroconf_extensions", lambda site_packages: None)

    monkeypatch.setattr(package_app, "uv_executable", lambda: "/opt/uv")
    package_app.build_python_packages("python3", tmp_path / "site-packages", PACKAGES)

    assert len(calls) == 2
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


def test_create_python_packages_reuses_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    package_app = load_package_app_module()
    cache_entry = tmp_path / "cache" / "site"
    calls: list[Path] = []

    def fake_build(python: str, site_packages: Path, packages: str) -> None:
        assert packages == PACKAGES
        calls.append(site_packages)
        package = site_packages / "timecapsulesmb"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# cached package\n", encoding="utf-8")
        create_python_bytecode(site_packages)

    monkeypatch.setattr(package_app, "resolve_python_packages", lambda python: PACKAGES)
    monkeypatch.setattr(package_app, "python_site_packages_cache_entry", lambda python, architectures, packages: cache_entry)
    monkeypatch.setattr(package_app, "build_python_packages", fake_build)

    first_resources = tmp_path / "FirstResources"
    second_resources = tmp_path / "SecondResources"

    assert package_app.create_python_packages("python3", first_resources, ("arm64",)) == PACKAGES
    assert package_app.create_python_packages("python3", second_resources, ("arm64",)) == PACKAGES

    assert len(calls) == 1
    # Each build lists what it bundles, from the cache too.
    assert capsys.readouterr().err.count("Bundled Python packages: certifi==2026.10.23, ifaddr==0.2.0\n") == 2
    assert (first_resources / "Python" / "site-packages" / "timecapsulesmb" / "__init__.py").is_file()
    assert (second_resources / "Python" / "site-packages" / "timecapsulesmb" / "__init__.py").is_file()
    assert_no_python_bytecode(first_resources / "Python" / "site-packages")
    assert_no_python_bytecode(second_resources / "Python" / "site-packages")
    assert_no_python_bytecode(cache_entry / "site-packages")

    # --no-cache rebuilds the complete entry, and later builds reuse the rebuilt one.
    third_resources = tmp_path / "ThirdResources"
    package_app.create_python_packages("python3", third_resources, ("arm64",), use_cache=False)
    assert len(calls) == 2
    assert (third_resources / "Python" / "site-packages" / "timecapsulesmb" / "__init__.py").is_file()
    assert_no_python_bytecode(third_resources / "Python" / "site-packages")
    package_app.create_python_packages("python3", tmp_path / "FourthResources", ("arm64",))
    assert len(calls) == 2


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
    root = tmp_path / "python-site-packages"
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
cache = package_app.package_cache_dir("python-site-packages")
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
    make_cache_entries(tmp_path / ".build" / "package-app" / "python-site-packages", ["b", "c", "a"])
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
    assert sorted(path.name for path in (tmp_path / ".build" / "package-app" / "python-site-packages").iterdir()) == []


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
    root = tmp_path / ".build" / "package-app" / "python-site-packages"
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

    def fake_build(python: str, site_packages: Path, packages: str) -> None:
        (site_packages / "timecapsulesmb").mkdir(parents=True)

    monkeypatch.setattr(package_app, "resolve_python_packages", lambda python: PACKAGES)
    monkeypatch.setattr(package_app, "build_python_packages", fake_build)
    monkeypatch.setattr(package_app, "assert_macho_architectures_for_roots", lambda *args: None)
    monkeypatch.setattr(package_app, "assert_no_external_macho_dependencies_for_roots", lambda roots: None)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_site_packages", lambda path: None)
    monkeypatch.setattr(package_app, "assert_macho_code_signatures_valid_for_roots", lambda roots: None)

    entries = []
    for revision in range(6):
        source["revision"] = revision
        package_app.create_python_packages("python3", tmp_path / f"Resources{revision}", ("arm64",))
        entry = package_app.python_site_packages_cache_entry("python3", ("arm64",), PACKAGES)
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
    monkeypatch.setattr(package_app, "resolve_python_packages", lambda python: PACKAGES)
    monkeypatch.setattr(package_app, "python_site_packages_cache_entry", lambda python, architectures, packages: cache_entry)
    monkeypatch.setattr(package_app, "build_python_packages", lambda *args: pytest.fail("cache was not reused"))

    resources = tmp_path / "Resources"
    package_app.create_python_packages("python3", resources, ("arm64",))

    assert (resources / "Python" / "site-packages" / "timecapsulesmb" / "__init__.py").is_file()
    assert_no_python_bytecode(resources / "Python" / "site-packages")


@pytest.mark.parametrize("sign", [True, False])
def test_finalize_python_bundle_cleans_before_optional_resigning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sign: bool,
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
    package_app.finalize_python_bundle(resources, sign=sign)

    assert calls == (["framework", "site-packages"] if sign else [])
    assert_no_python_bytecode(resources)


def test_package_args_defaults_and_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TCAPSULE_CODESIGN_IDENTITY", raising=False)
    monkeypatch.delenv("TCAPSULE_NOTARY_PROFILE", raising=False)
    monkeypatch.delenv("TCAPSULE_NOTARY_TIMEOUT", raising=False)
    package_app = load_package_app_module()

    args = package_app.parse_args([])
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
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, distribution / "bin"):
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
    distribution = app / "Contents" / "Resources" / "Distribution"
    for directory in (helper.parent, python_packages, distribution / "bin"):
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
        "Sparkle framework",
    ]
    assert calls == ["codesign", "app-codesign"]

    architecture_labels.clear()
    calls.clear()
    package_app.assert_bundle_layout(app, architectures=("arm64",), full_validation=True)
    assert calls == ["runtime", "external", "minimum-macos", "codesign", "app-codesign"]


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
    minos 14.0
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
    fat, thin, text = tmp_path / "python3.13", tmp_path / "newer.so", tmp_path / "README"
    fake_vtool(package_app, monkeypatch, {fat: VTOOL_FAT, thin: VTOOL_THIN})

    # The linker's own "version" line inside LC_BUILD_VERSION is not a macOS version.
    assert package_app.macho_minimum_macos(fat) == {"x86_64": "10.13", "arm64": "14.0"}
    assert package_app.macho_minimum_macos(thin) == {"arm64": "26.6"}
    assert package_app.macho_minimum_macos(text) == {}


def test_assert_macho_minimum_macos_accepts_the_limit_and_rejects_newer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    fat, thin, text = tmp_path / "python3.13", tmp_path / "newer.so", tmp_path / "README"
    fake_vtool(package_app, monkeypatch, {fat: VTOOL_FAT, thin: VTOOL_THIN})

    package_app.assert_macho_minimum_macos([fat, text])
    with pytest.raises(RuntimeError, match=r"newer macOS than 14\.0:\n  - .*newer\.so \(arm64\): 26\.6$"):
        package_app.assert_macho_minimum_macos([fat, thin, text])
    package_app.assert_macho_minimum_macos([thin], supported="26.6")


def test_python_version_is_the_interpreters_own() -> None:
    package_app = load_package_app_module()
    assert package_app.python_version(sys.executable) == platform.python_version()


def test_the_scripts_run_on_the_python_series_the_app_bundles() -> None:
    # PYTHON_RUNTIME_SERIES picks the bundled Python; the scripts' headers
    # pick the one uv runs them with. Moving one without the others fails here.
    package_app = load_package_app_module()
    major, minor = map(int, package_app.PYTHON_RUNTIME_SERIES.split("."))
    for script in (PACKAGE_SCRIPT, PACKAGE_SCRIPT.with_name("sparkle_appcast.py")):
        header = re.findall(r'^# requires-python = "(.*)"$', script.read_text(encoding="utf-8"), re.MULTILINE)
        assert header == [f">={major}.{minor},<{major}.{minor + 1}"], script.name


def test_minimum_macos_matches_the_swift_package_platform() -> None:
    # Info.plist and the Mach-O check use MINIMUM_MACOS; swift build uses
    # Package.swift. Raising one without the other ships an app whose
    # LSMinimumSystemVersion is not what its executables were built for.
    package_app = load_package_app_module()
    manifest = (package_app.PACKAGE_ROOT / "Package.swift").read_text(encoding="utf-8")
    platforms = re.findall(r"\.macOS\(\.v(\d+)(?:_(\d+))?\)", manifest)
    assert len(platforms) == 1, platforms
    major, minor = platforms[0]
    assert package_app.MINIMUM_MACOS == f"{major}.{minor or 0}"


OTOOL_RPATHS = """{path}:
Load command 12
          cmd LC_LOAD_DYLIB
      cmdsize 72
         name @rpath/Sparkle.framework/Versions/B/Sparkle (offset 24)
Load command 13
          cmd LC_RPATH
      cmdsize 64
         path /usr/lib/swift (offset 12)
Load command 14
          cmd LC_RPATH
      cmdsize 56
         path @executable_path/../Frameworks (offset 12)
Load command 15
          cmd LC_FUNCTION_STARTS
      cmdsize 16
"""


def test_macho_rpaths_lists_rpath_load_commands_in_search_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    binary = tmp_path / "TimeCapsuleSMB"

    def fake_run(cmd, **kwargs):
        assert cmd == ["otool", "-l", str(binary)]
        return subprocess.CompletedProcess(cmd, 0, stdout=OTOOL_RPATHS.format(path=binary), stderr="")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)

    # A path line of another load command (the LC_LOAD_DYLIB name) is no rpath.
    assert package_app.macho_rpaths(binary) == ["/usr/lib/swift", "@executable_path/../Frameworks"]


def test_macho_rpaths_is_empty_when_otool_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app.subprocess, "run",
                        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not a Mach-O"))

    assert package_app.macho_rpaths(tmp_path / "README") == []


def test_rpath_dependency_target_takes_the_first_rpath_holding_the_library(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    private = tmp_path / "outside" / "lib" / "private"
    lib = tmp_path / "outside" / "lib"
    private.mkdir(parents=True)
    (lib / "libexample.dylib").write_text("public", encoding="utf-8")
    (private / "libexample.dylib").write_text("private", encoding="utf-8")
    loader_dir = tmp_path / "outside" / "bin"
    loader_dir.mkdir()

    # dyld searches rpaths in load order; @loader_path is the loading binary's directory.
    assert package_app.rpath_dependency_target(
        "@rpath/libexample.dylib", ["/missing", "@loader_path/../lib", str(private)], loader_dir, loader_dir
    ) == loader_dir / "../lib" / "libexample.dylib"
    assert package_app.rpath_dependency_target(
        "@rpath/libexample.dylib", [str(private), str(lib)], loader_dir, loader_dir
    ) == private / "libexample.dylib"
    assert package_app.rpath_dependency_target("@rpath/libexample.dylib", ["/missing"], loader_dir, loader_dir) is None
    assert package_app.rpath_dependency_target("@rpath/libexample.dylib", [], loader_dir, loader_dir) is None


SPARKLE_INSTALL_NAME = "@rpath/Sparkle.framework/Versions/B/Sparkle"


def external_validation_fixture(package_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The app executable as swift build links it: Sparkle through @rpath, found
    by the rpath add_framework_rpath gives it, beside its own Swift rpath."""
    app = tmp_path / "TimeCapsuleSMB.app"
    contents = app / "Contents"
    executable = contents / "MacOS" / "TimeCapsuleSMB"
    sparkle = contents / "Frameworks" / "Sparkle.framework" / "Versions" / "B" / "Sparkle"
    extension = contents / "Resources" / "Python" / "site-packages" / "zopfli" / "zopfli.so"
    outside = tmp_path / "Homebrew" / "Frameworks"
    (outside / "Sparkle.framework" / "Versions" / "B").mkdir(parents=True)
    (outside / "Sparkle.framework" / "Versions" / "B" / "Sparkle").write_text("outside", encoding="utf-8")
    for path in (executable, sparkle, extension):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
        path.chmod(0o755)
    dependencies = {
        executable: [SPARKLE_INSTALL_NAME, "/usr/lib/swift/libswiftCore.dylib", "/usr/lib/libSystem.B.dylib"],
        # otool -L lists a library's own install name first.
        sparkle: [SPARKLE_INSTALL_NAME, "/System/Library/Frameworks/AppKit.framework/Versions/C/AppKit"],
        extension: ["/usr/lib/libSystem.B.dylib"],
    }
    rpaths = {executable: ["/usr/lib/swift", "@executable_path/../Frameworks"], sparkle: [], extension: []}
    monkeypatch.setattr(package_app, "macho_dependencies", lambda path: dependencies[path])
    monkeypatch.setattr(package_app, "macho_rpaths", lambda path: rpaths[path])
    monkeypatch.setattr(package_app, "macho_install_name", lambda path: SPARKLE_INSTALL_NAME if path == sparkle else None)
    return SimpleNamespace(app=app, executable=executable, sparkle=sparkle, extension=extension, outside=outside,
                           dependencies=dependencies, rpaths=rpaths)


def test_external_dependency_validation_accepts_rpath_references_resolved_inside_the_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    fixture = external_validation_fixture(package_app, monkeypatch, tmp_path)

    package_app.assert_no_external_macho_dependencies(fixture.app)


@pytest.mark.parametrize(
    "case", ["outside-rpath-first", "unresolved", "absolute-outside-path", "framework-rpath-outside"],
)
def test_external_dependency_validation_rejects_libraries_loaded_from_outside_the_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    package_app = load_package_app_module()
    fixture = external_validation_fixture(package_app, monkeypatch, tmp_path)
    if case == "outside-rpath-first":
        # dyld would load the outside copy before the bundled one.
        fixture.rpaths[fixture.executable] = [str(fixture.outside), "@executable_path/../Frameworks"]
        expected = r"TimeCapsuleSMB: @rpath/Sparkle.framework/Versions/B/Sparkle -> .*Homebrew/Frameworks/Sparkle.framework"
    elif case == "unresolved":
        fixture.rpaths[fixture.executable] = ["/usr/lib/swift"]
        expected = r"TimeCapsuleSMB: @rpath/Sparkle.framework/Versions/B/Sparkle -> unresolved"
    elif case == "framework-rpath-outside":
        # A build path left in Sparkle: missing here, so nothing resolves
        # through it, but a user's Mac may have that folder.
        fixture.rpaths[fixture.sparkle] = ["/Users/builder/DerivedData/Build/Products", "@loader_path/Frameworks"]
        expected = r"Sparkle: LC_RPATH /Users/builder/DerivedData/Build/Products$"
    else:
        # A wheel linked against a library a user's Mac does not have.
        fixture.dependencies[fixture.extension].append("/opt/homebrew/opt/zlib/lib/libz.1.dylib")
        expected = r"zopfli.so: /opt/homebrew/opt/zlib/lib/libz.1.dylib$"

    with pytest.raises(RuntimeError, match=expected):
        package_app.assert_no_external_macho_dependencies(fixture.app)


def test_developer_id_codesign_app_bundle_signs_nested_code_framework_and_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    library = app / "Contents" / "Frameworks" / "libtool.dylib"
    extension = app / "Contents" / "Resources" / "Python" / "site-packages" / "zopfli" / "zopfli.so"
    executable = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    framework = app / "Contents" / "Resources" / "Python" / "Runtime" / "Python.framework"
    for path in (library, extension, executable, framework / "Versions" / "3.13" / "Python"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("macho", encoding="utf-8")
    calls: list[Path] = []

    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [executable, extension, library])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})
    monkeypatch.setattr(package_app, "developer_id_codesign", lambda path, identity: calls.append(path))
    package_app.developer_id_codesign_app_bundle(app, "Developer ID Application: Example (TEAMID)")

    assert set(calls[:-2]) == {library, extension}
    assert calls[-2:] == [framework, app]


def test_signing_leaves_excludes_the_enclosing_bundles_main_executables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "App.app"
    framework = package_app.bundled_python_framework(app)
    version = framework / "Versions" / "3.13"
    version.mkdir(parents=True)
    (framework / "Versions" / "Current").symlink_to("3.13")
    python_main = version / "Python"
    app_main = app / "Contents" / "MacOS" / package_app.PRODUCT_NAME
    leaf = version / "extension.so"
    calls: list[Path] = []
    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [app_main, python_main, leaf])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})
    monkeypatch.setattr(package_app, "developer_id_codesign", lambda path, identity: calls.append(path))
    package_app.developer_id_codesign_app_bundle(app, "identity")
    assert calls == [leaf, framework, app]


def test_parallel_signing_waits_for_leaves_and_bounds_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "App.app"
    framework = package_app.bundled_python_framework(app)
    framework.mkdir(parents=True)
    leaves = [app / f"leaf-{index}.dylib" for index in range(8)]
    script = app / "wrapper"
    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [*leaves, script])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"} if path in leaves else set())
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    completed: list[Path] = []
    containers: list[Path] = []
    active = peak = 0

    def sign(path: Path, identity: str) -> None:
        nonlocal active, peak
        assert identity == "identity"
        if path in leaves:
            with lock:
                active += 1
                peak = max(peak, active)
            # Two batches of four overlap without relying on a sleep/race.
            barrier.wait(timeout=10)
            with lock:
                active -= 1
                completed.append(path)
        else:
            assert set(completed) == set(leaves)
            assert active == 0
            containers.append(path)

    monkeypatch.setattr(package_app, "developer_id_codesign", sign)
    package_app.developer_id_codesign_app_bundle(app, "identity")
    assert peak == 4
    assert containers == [framework, app]
    assert script not in completed


def test_parallel_signing_failure_prevents_container_signing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "App.app"
    package_app.bundled_python_framework(app).mkdir(parents=True)
    leaves = [app / "bad.dylib", app / "good.dylib"]
    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: leaves)
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})
    failure = RuntimeError("signing failed")
    calls: list[Path] = []

    def sign(path: Path, identity: str) -> None:
        calls.append(path)
        if path == leaves[0]:
            raise failure

    monkeypatch.setattr(package_app, "developer_id_codesign", sign)
    with pytest.raises(RuntimeError, match="signing failed") as raised:
        package_app.developer_id_codesign_app_bundle(app, "identity")
    assert raised.value is failure
    assert set(calls).issubset(set(leaves))


def test_assert_macho_code_signatures_valid_reports_invalid_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    contents = app / "Contents"
    sparkle = contents / "Frameworks" / "Sparkle.framework" / "Versions" / "B" / "Sparkle"
    extension = contents / "Resources" / "Python" / "site-packages" / "zopfli" / "zopfli.so"
    executable = contents / "MacOS" / "TimeCapsuleSMB"
    helper = contents / "Helpers" / "tcapsule"
    for path in (sparkle, extension, executable, helper):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("macho", encoding="utf-8")
    verified: list[str] = []

    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [executable, helper, sparkle, extension])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "codesign":
            verified.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="invalid signature\n")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=r"invalid Mach-O code signature\(s\):\n  - .*Sparkle: invalid signature\n  - .*zopfli.so: invalid signature$"):
        package_app.assert_macho_code_signatures_valid(app)
    # The app's and the helper's own code is checked with the app's signature.
    assert verified == [str(sparkle), str(extension)]


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


# What notarytool log prints for a refused submission (abridged).
NOTARY_LOG = """{
  "status": "Invalid",
  "issues": [
    {
      "severity": "error",
      "path": "TimeCapsuleSMB-notary.zip/TimeCapsuleSMB.app/Contents/Helpers/tcapsule",
      "message": "The signature does not include a secure timestamp."
    }
  ]
}
"""


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
        if cmd[2] == "log":
            return subprocess.CompletedProcess(cmd, 0, stdout=NOTARY_LOG, stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"status":"Invalid","id":"submission-id","message":"bad signature"}',
            stderr="",
        )

    monkeypatch.setattr(package_app, "run_quiet", fake_run_quiet)

    with pytest.raises(RuntimeError, match="bad signature") as raised:
        package_app.notarize_archive(archive, "release-profile", "30m")
    assert calls[0][:4] == ["xcrun", "notarytool", "submit", str(archive)]
    assert "--keychain-profile" in calls[0]
    assert "release-profile" in calls[0]
    # Apple's log names the file it refused, which the status message does not.
    assert calls[1] == ["xcrun", "notarytool", "log", "submission-id", "--keychain-profile", "release-profile"]
    assert "does not include a secure timestamp" in str(raised.value)


def test_a_refused_notarization_still_fails_when_its_log_cannot_be_fetched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    archive = tmp_path / "TimeCapsuleSMB-notary.zip"
    archive.write_bytes(b"zip")

    def fake_run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[2] == "log":
            raise subprocess.CalledProcessError(69, cmd, output="", stderr="Submission log is not available yet.")
        return subprocess.CompletedProcess(cmd, 0, stdout='{"status":"Invalid","id":"submission-id","message":"bad signature"}')

    monkeypatch.setattr(package_app, "run_quiet", fake_run_quiet)

    with pytest.raises(RuntimeError, match=r"(?s)bad signature.*log could not be fetched: Submission log is not available yet"):
        package_app.notarize_archive(archive, "release-profile", "30m")


def test_a_notarization_without_a_submission_asks_for_no_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    archive = tmp_path / "TimeCapsuleSMB-notary.zip"
    archive.write_bytes(b"zip")
    calls: list[list[str]] = []

    def fake_run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"status":"In Progress","message":"timed out"}')

    monkeypatch.setattr(package_app, "run_quiet", fake_run_quiet)

    with pytest.raises(RuntimeError, match="'In Progress'"):
        package_app.notarize_archive(archive, "release-profile", "30m")
    assert [cmd[2] for cmd in calls] == ["submit"]


def test_notarize_archive_returns_submission_id_on_accept(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_app = load_package_app_module()
    archive = tmp_path / "TimeCapsuleSMB-notary.zip"
    archive.write_bytes(b"zip")
    ticks = iter([100.0, 115.5])
    monkeypatch.setattr(package_app.time, "perf_counter", lambda: next(ticks))

    def fake_run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        assert capsys.readouterr().err == "[package] Submitting archive and waiting for Apple notarization...\n"
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"status":"Accepted","id":"submission-id","message":"Processing complete"}',
            stderr="",
        )

    monkeypatch.setattr(package_app, "run_quiet", fake_run_quiet)

    assert package_app.notarize_archive(archive, "release-profile", "30m") == "submission-id"
    assert capsys.readouterr().err.splitlines() == [
        "Notarization accepted: submission-id",
        "[package] Submitting archive and waiting for Apple notarization: done (15.5s)",
    ]


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


@pytest.mark.parametrize("configuration", ["release", "debug"])
@pytest.mark.parametrize("identity", [None, "Developer ID Application: Test"])
def test_package_app_signs_the_final_bundle_after_copying_sparkle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    identity: str | None,
    configuration: str,
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
    for language in ("de", "en"):
        (resource_build_dir / package_app.RESOURCE_BUNDLE_NAME / "Contents" / "Resources" / f"{language}.lproj").mkdir(parents=True)
    calls: list[str] = []

    monkeypatch.setattr(package_app, "resolve_architectures", lambda arch: ("arm64",))
    monkeypatch.setattr(package_app, "uv_executable", lambda: "uv")
    monkeypatch.setattr(package_app, "build_swift", lambda configuration, architectures: (executable, helper_executable, resource_build_dir))
    monkeypatch.setattr(package_app, "copy_resources", lambda source, resources: calls.append("resources"))
    monkeypatch.setattr(package_app, "copy_helper_executable", lambda source, destination: calls.append("helper"))
    monkeypatch.setattr(package_app, "copy_python_runtime", lambda args, resources, architectures: resources / "Python" / "Runtime" / "bin" / "python3")
    monkeypatch.setattr(package_app, "python_version", lambda python: "3.15.2")
    monkeypatch.setattr(package_app, "create_python_packages", lambda python, resources, architectures, use_cache=True: calls.append("packages") or PACKAGES)
    python_signing: list[bool] = []
    monkeypatch.setattr(package_app, "finalize_python_bundle", lambda resources, *, sign: python_signing.append(sign))
    monkeypatch.setattr(package_app, "copy_distribution", lambda resources: calls.append("distribution"))
    monkeypatch.setattr(package_app, "add_framework_rpath", lambda executable: calls.append("rpath"))
    monkeypatch.setattr(package_app, "strip_debug_map", lambda executable: calls.append(f"strip {executable.name}"))
    monkeypatch.setattr(package_app, "copy_sparkle_framework", lambda build_dir, app: calls.append("sparkle"))
    monkeypatch.setattr(package_app, "remove_appledouble_files", lambda app: calls.append("clean"))
    monkeypatch.setattr(package_app, "assert_no_appledouble_files", lambda app: calls.append("assert-clean"))
    monkeypatch.setattr(package_app, "ad_hoc_codesign_app_bundle", lambda app: calls.append("app-sign"))
    signature_caching: list[bool] = []
    monkeypatch.setattr(package_app, "developer_id_codesign_app_bundle",
                        lambda app, identity, *, use_cache: (calls.append("developer-sign"), signature_caching.append(use_cache)))
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: calls.append("app-verify"))
    monkeypatch.setattr(package_app, "assert_bundle_layout", lambda app, **kwargs: calls.append("assert"))

    args = SimpleNamespace(
        arch="native",
        configuration=configuration,
        output=tmp_path / "dist",
        icon=None,
        no_cache=False,
        full_validation=False,
        skip_smoke=True,
        codesign_identity=identity,
        notarize=False,
        notary_profile="tcapsulesmb-notary",
        notary_timeout="30m",
        zip=False,
        zip_output=None,
    )

    result = package_app.package_app(args)

    # Sparkle goes in before signing; the rpath goes on the executable right away.
    assert calls[-5:] == ["sparkle", "clean", "assert-clean", "developer-sign" if identity else "app-sign", "assert"]
    assert calls.index("rpath") < calls.index("resources")
    # A release build's app and helper lose the debug map before signing; a
    # debug build keeps it for lldb.
    stripped = [call for call in calls if call.startswith("strip ")]
    if configuration == "release":
        assert stripped == ["strip TimeCapsuleSMB", "strip tcapsule"]
        assert calls.index("strip tcapsule") < calls.index("developer-sign" if identity else "app-sign")
    else:
        assert stripped == []
    assert "app-verify" not in calls  # The layout validation owns the final verification.
    assert python_signing == [identity is None]
    assert signature_caching == ([True] if identity else [])
    assert result.app == tmp_path / "dist" / "TimeCapsuleSMB.app"
    assert result.zip_path is None
    assert result.notarization_archive is None
    # The plist declares the languages of the resource bundle the build made.
    with (result.app / "Contents" / "Info.plist").open("rb") as handle:
        assert plistlib.load(handle)["CFBundleLocalizations"] == ["de", "en"]


def test_package_app_ships_only_the_app_resource_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The real build_swift and copy_resources run against a Swift Build that
    # also wrote the test target's bundle; the steps after them are stubbed.
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(package_app, "run", fake_swift_build(package_app, "swiftbuild", []))
    monkeypatch.setattr(package_app, "resolve_architectures", lambda arch: ("arm64",))
    monkeypatch.setattr(package_app, "copy_helper_executable", lambda source, destination: None)
    monkeypatch.setattr(package_app, "copy_python_runtime", lambda args, resources, architectures: resources / "Python" / "Runtime" / "bin" / "python3")
    monkeypatch.setattr(package_app, "python_version", lambda python: "3.15.2")
    monkeypatch.setattr(package_app, "create_python_packages", lambda python, resources, architectures, use_cache=True: PACKAGES)
    monkeypatch.setattr(package_app, "finalize_python_bundle", lambda resources, *, sign: None)
    monkeypatch.setattr(package_app, "copy_distribution", lambda resources: None)
    monkeypatch.setattr(package_app, "add_framework_rpath", lambda executable: None)
    monkeypatch.setattr(package_app, "strip_debug_map", lambda executable: None)
    monkeypatch.setattr(package_app, "remove_appledouble_files", lambda app: None)
    monkeypatch.setattr(package_app, "assert_no_appledouble_files", lambda app: None)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_app_bundle", lambda app: None)
    shipped: list[list[str]] = []
    monkeypatch.setattr(
        package_app,
        "assert_bundle_layout",
        lambda app, **kwargs: shipped.append(sorted(path.name for path in (app / "Contents" / "Resources").iterdir())),
    )
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

    assert shipped == [[package_app.RESOURCE_BUNDLE_NAME]]
    assert (result.app / "Contents" / "Resources" / package_app.RESOURCE_BUNDLE_NAME).is_dir()
    # The test bundle was in the build products but stayed there.
    products = tmp_path / ".build" / "package-swift" / "arm64" / "out" / "Products" / "Release"
    assert (products / TEST_RESOURCE_BUNDLE_NAME).is_dir()


def test_notarization_requires_identity_before_building(monkeypatch: pytest.MonkeyPatch) -> None:
    package_app = load_package_app_module()

    def unexpected_build(*args: object) -> None:
        pytest.fail("invalid notarization arguments must be rejected before building")

    monkeypatch.setattr(package_app, "build_swift", unexpected_build)
    with pytest.raises(RuntimeError, match="--notarize requires --codesign-identity"):
        package_app.build_app_package(SimpleNamespace(notarize=True, codesign_identity=None))


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
    capsys: pytest.CaptureFixture[str],
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
    monkeypatch.setattr(package_app, "uv_executable", lambda: "uv")
    monkeypatch.setattr(package_app, "build_swift", lambda configuration, architectures: (executable, helper_executable, resource_build_dir))
    monkeypatch.setattr(package_app, "copy_resources", lambda source, resources: None)
    monkeypatch.setattr(package_app, "copy_helper_executable", lambda source, destination: None)
    monkeypatch.setattr(package_app, "copy_python_runtime", lambda args, resources, architectures: resources / "Python" / "Runtime" / "bin" / "python3")
    monkeypatch.setattr(package_app, "python_version", lambda python: "3.15.2")
    monkeypatch.setattr(package_app, "create_python_packages", lambda python, resources, architectures, use_cache=True: PACKAGES)
    monkeypatch.setattr(package_app, "finalize_python_bundle", lambda resources, *, sign: None)
    monkeypatch.setattr(package_app, "copy_distribution", lambda resources: None)
    monkeypatch.setattr(package_app, "add_framework_rpath", lambda executable: None)
    monkeypatch.setattr(package_app, "strip_debug_map", lambda executable: None)
    monkeypatch.setattr(package_app, "copy_sparkle_framework", lambda build_dir, app: None)
    monkeypatch.setattr(package_app, "remove_appledouble_files", lambda app: None)
    monkeypatch.setattr(package_app, "assert_no_appledouble_files", lambda app: None)
    monkeypatch.setattr(package_app, "ad_hoc_codesign_app_bundle", lambda app: None)
    monkeypatch.setattr(package_app, "assert_app_bundle_signature_valid", lambda app: None)
    monkeypatch.setattr(package_app, "assert_bundle_layout", lambda app, **kwargs: None)
    monkeypatch.setattr(package_app, "developer_id_codesign_app_bundle", lambda app, identity, *, use_cache: None)
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
    # CI audits exactly what was bundled, from this file beside the app, and
    # keys its package cache on it, the bundled Python included.
    assert (tmp_path / "dist" / "python-packages.txt").read_text(encoding="utf-8") == f"# Python 3.15.2\n{PACKAGES}"
    assert "Bundled Python runtime: 3.15.2\n" in capsys.readouterr().err


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


def test_info_plist_names_the_sparkle_feed_and_key(tmp_path: Path) -> None:
    package_app = load_package_app_module()

    package_app.write_info_plist(tmp_path, languages=["de", "en"])

    with (tmp_path / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleLocalizations"] == ["de", "en"]
    assert info["SUFeedURL"] == package_app.SPARKLE_FEED_URL
    assert info["SUPublicEDKey"] == package_app.SPARKLE_PUBLIC_ED_KEY
    assert info["SUEnableAutomaticChecks"] is True
    assert info["SUVerifyUpdateBeforeExtraction"] is True


@pytest.mark.parametrize("layout", ["Contents/Resources", "."])
def test_the_app_declares_every_language_its_resource_bundle_has(tmp_path: Path, layout: str) -> None:
    # Swift Build nests the lproj folders in Contents/Resources; the native
    # build system leaves them at the bundle's top level.
    package_app = load_package_app_module()
    bundle = tmp_path / package_app.RESOURCE_BUNDLE_NAME
    for language in ("zh-Hans", "en", "lt"):
        (bundle / layout / f"{language}.lproj").mkdir(parents=True)
    (bundle / layout / "Assets.car").write_bytes(b"")

    assert package_app.resource_bundle_languages(bundle) == ["en", "lt", "zh-Hans"]


def test_sparkle_is_copied_as_symlinks_without_its_xpc_services(tmp_path: Path) -> None:
    package_app = load_package_app_module()
    build_dir = tmp_path / "build"
    create_fake_sparkle(build_dir)
    app = tmp_path / "TimeCapsuleSMB.app"

    package_app.copy_sparkle_framework(build_dir, app)

    framework = package_app.sparkle_framework(app)
    assert (framework / "Versions" / "Current").is_symlink()
    assert os.readlink(framework / "Versions" / "Current") == "B"
    assert (framework / "Sparkle").is_symlink()
    assert (framework / "Versions" / "B" / "Sparkle").read_text(encoding="utf-8") == "sparkle"
    assert (framework / "Versions" / "B" / "Updater.app" / "Contents" / "MacOS" / "Updater").is_file()
    assert not os.path.lexists(framework / "XPCServices")
    assert not os.path.lexists(framework / "Versions" / "B" / "XPCServices")
    # The build output keeps its own copy whole.
    assert (build_dir / "Sparkle.framework" / "Versions" / "B" / "XPCServices").is_dir()


def test_sparkle_copy_fails_when_swift_build_made_no_framework(tmp_path: Path) -> None:
    package_app = load_package_app_module()

    with pytest.raises(RuntimeError, match="did not produce .*Sparkle.framework"):
        package_app.copy_sparkle_framework(tmp_path / "build", tmp_path / "TimeCapsuleSMB.app")


def test_app_executable_gets_the_frameworks_rpath(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    commands: list[list[str]] = []
    monkeypatch.setattr(package_app, "run_quiet", lambda cmd: commands.append(cmd))
    executable = tmp_path / "TimeCapsuleSMB"

    package_app.add_framework_rpath(executable)

    assert commands == [["install_name_tool", "-add_rpath", "@executable_path/../Frameworks", str(executable)]]


def test_ad_hoc_signing_signs_sparkle_inside_out_before_the_helper_and_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    helper = app / "Contents" / "Helpers" / "tcapsule"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    framework = create_fake_sparkle(app / "Contents" / "Frameworks", xpc_services=False)
    create_fake_python_runtime(app)
    calls: list[Path] = []
    monkeypatch.setattr(package_app, "ad_hoc_codesign", lambda path: calls.append(path))

    package_app.ad_hoc_codesign_app_bundle(app)

    # Python.framework is signed where it is added, not here.
    assert calls == [
        framework / "Versions" / "B" / "Autoupdate",
        framework / "Versions" / "B" / "Updater.app",
        framework,
        helper,
        app,
    ]


def test_developer_id_signing_signs_sparkle_bundles_after_its_leaves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    framework = create_fake_sparkle(app / "Contents" / "Frameworks", xpc_services=False)
    executable = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    executable.parent.mkdir(parents=True)
    executable.write_text("macho", encoding="utf-8")
    version = framework / "Versions" / "B"
    autoupdate = version / "Autoupdate"
    updater_main = version / "Updater.app" / "Contents" / "MacOS" / "Updater"
    sparkle_main = version / "Sparkle"
    calls: list[Path] = []
    monkeypatch.setattr(package_app, "macho_validation_roots", lambda app: [executable, autoupdate, updater_main, sparkle_main])
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"arm64"})
    monkeypatch.setattr(package_app, "developer_id_codesign", lambda path, identity: calls.append(path))

    package_app.developer_id_codesign_app_bundle(app, "identity")

    # A bundle's main executable is signed with its bundle, never alone first.
    assert calls == [autoupdate, version / "Updater.app", framework, app]


def record_smoke_requests(
    monkeypatch: pytest.MonkeyPatch,
    package_app,
    *,
    failing_prefix: list[str] | None = None,
) -> list[list[str]]:
    """Record each helper request smoke_test makes, answering ok unless its
    command starts with failing_prefix."""
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        operation = json.loads(kwargs["input_text"])["operation"]  # type: ignore[arg-type]
        ok = failing_prefix is None or cmd[: len(failing_prefix)] != failing_prefix
        result = json.dumps({"type": "result", "operation": operation, "ok": ok})
        return subprocess.CompletedProcess(cmd, 0, stdout=result + "\n", stderr="")

    monkeypatch.setattr(package_app, "run", fake_run)
    monkeypatch.setattr(package_app, "validate_app_resources", lambda app, *, architecture=None: commands.append(["app", str(architecture)]))
    monkeypatch.setattr(
        package_app,
        "assert_python_dependencies_are_bundled",
        lambda app, *, architecture=None: commands.append(["python", str(architecture)]),
    )
    return commands


def test_smoke_test_runs_the_helper_again_as_x86_64_under_rosetta(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    commands = record_smoke_requests(monkeypatch, package_app)
    monkeypatch.setattr(package_app, "native_architecture", lambda: "arm64")
    monkeypatch.setattr(package_app, "rosetta_available", lambda: True)

    package_app.smoke_test(tmp_path / "TimeCapsuleSMB.app", ("arm64", "x86_64"))

    helper = str(tmp_path / "TimeCapsuleSMB.app" / "Contents" / "Helpers" / "tcapsule")
    assert commands == [
        [helper, "api"],
        [helper, "api"],
        ["arch", "-x86_64", helper, "api"],
        ["arch", "-x86_64", helper, "api"],
        # Only the x86_64 pass launches the app and imports: assert_bundle_layout
        # already did both natively.
        ["app", "x86_64"],
        ["python", "x86_64"],
    ]


@pytest.mark.parametrize(
    ("host", "architectures"),
    # An x86_64-only build's own pass already runs under Rosetta on Apple silicon.
    [("arm64", ("arm64",)), ("arm64", ("x86_64",)), ("x86_64", ("arm64", "x86_64")), ("x86_64", ("x86_64",))],
)
def test_smoke_test_runs_one_pass_unless_apple_silicon_builds_universal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: str,
    architectures: tuple[str, ...],
) -> None:
    package_app = load_package_app_module()
    commands = record_smoke_requests(monkeypatch, package_app)
    monkeypatch.setattr(package_app, "native_architecture", lambda: host)
    monkeypatch.setattr(package_app, "rosetta_available", lambda: pytest.fail("Rosetta was probed"))

    package_app.smoke_test(tmp_path / "TimeCapsuleSMB.app", architectures)

    helper = str(tmp_path / "TimeCapsuleSMB.app" / "Contents" / "Helpers" / "tcapsule")
    assert commands == [[helper, "api"], [helper, "api"]]


def test_smoke_test_without_rosetta_skips_the_x86_64_pass_and_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    commands = record_smoke_requests(monkeypatch, package_app)
    monkeypatch.setattr(package_app, "native_architecture", lambda: "arm64")
    monkeypatch.setattr(package_app, "rosetta_available", lambda: False)

    package_app.smoke_test(tmp_path / "TimeCapsuleSMB.app", ("arm64", "x86_64"))

    helper = str(tmp_path / "TimeCapsuleSMB.app" / "Contents" / "Helpers" / "tcapsule")
    assert commands == [[helper, "api"], [helper, "api"]]
    err = capsys.readouterr().err
    assert "Skipped the x86_64 smoke pass" in err
    assert "softwareupdate --install-rosetta" in err


def test_a_failing_x86_64_request_fails_the_smoke_test(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    commands = record_smoke_requests(monkeypatch, package_app, failing_prefix=["arch", "-x86_64"])
    monkeypatch.setattr(package_app, "native_architecture", lambda: "arm64")
    monkeypatch.setattr(package_app, "rosetta_available", lambda: True)

    with pytest.raises(RuntimeError, match=r"capabilities \(x86_64\) smoke test failed"):
        package_app.smoke_test(tmp_path / "TimeCapsuleSMB.app", ("arm64", "x86_64"))
    assert commands[-1][:2] == ["arch", "-x86_64"]


@pytest.mark.parametrize(("returncode", "available"), [(0, True), (1, False)])
def test_rosetta_is_available_when_an_x86_64_true_runs(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    available: bool,
) -> None:
    package_app = load_package_app_module()
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode)

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)

    assert package_app.rosetta_available() is available
    assert commands == [["arch", "-x86_64", "/usr/bin/true"]]


def python_runtime_args(**overrides: object) -> SimpleNamespace:
    values = {"python_runtime_url": None}
    values.update(overrides)
    return SimpleNamespace(**values)


PYTHON_INDEX = (
    '<a href="3.14.9/">3.14.9/</a>\n<a href="3.15.2/">3.15.2/</a>\n'
    '<a href="3.15.10/">3.15.10/</a>\n<a href="3.16.0/">3.16.0/</a>\n<a href="3.15.2/">3.15.2/</a>\n'
)
INDEX = "https://www.python.org/ftp/python/"


def serve_python_index(monkeypatch: pytest.MonkeyPatch, package_app, existing: set[str]) -> list[str]:
    """python.org's folder index, and which installers exist; returns the
    installers asked about, in order."""
    asked: list[str] = []
    monkeypatch.setattr(package_app.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(PYTHON_INDEX.encode()))
    monkeypatch.setattr(package_app, "url_exists", lambda url: asked.append(url) or url in existing)
    return asked


def test_the_runtime_is_the_newest_final_release_of_its_series(monkeypatch: pytest.MonkeyPatch) -> None:
    package_app = load_package_app_module()
    # 3.15.10/ holds only a prerelease so far; 3.14 and 3.16 are other series.
    asked = serve_python_index(monkeypatch, package_app, {f"{INDEX}3.15.2/python-3.15.2-macos11.pkg"})

    assert package_app.newest_python_runtime_url("3.15") == f"{INDEX}3.15.2/python-3.15.2-macos11.pkg"
    # Newest first, numerically: 3.15.10 before 3.15.2, each folder asked once.
    assert asked == [f"{INDEX}3.15.10/python-3.15.10-macos11.pkg", f"{INDEX}3.15.2/python-3.15.2-macos11.pkg"]


def test_a_series_with_no_final_installer_stops_packaging(monkeypatch: pytest.MonkeyPatch) -> None:
    package_app = load_package_app_module()
    serve_python_index(monkeypatch, package_app, set())

    with pytest.raises(RuntimeError, match="has no final Python 3.15 macOS installer"):
        package_app.newest_python_runtime_url("3.15")


@pytest.mark.parametrize("status,exists", [(200, True), (404, False)])
def test_url_exists_asks_with_a_head_request(monkeypatch: pytest.MonkeyPatch, status: int, exists: bool) -> None:
    package_app = load_package_app_module()
    requests: list[object] = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        if status != 200:
            raise package_app.urllib.error.HTTPError(request.full_url, status, "Not Found", {}, None)
        return io.BytesIO(b"")

    monkeypatch.setattr(package_app.urllib.request, "urlopen", fake_urlopen)

    assert package_app.url_exists("https://x.test/a.pkg") is exists
    assert requests[0].get_method() == "HEAD"


def test_url_exists_reports_other_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    package_app = load_package_app_module()

    def fake_urlopen(request, timeout):
        raise package_app.urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, None)

    monkeypatch.setattr(package_app.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(package_app.urllib.error.HTTPError):
        package_app.url_exists("https://x.test/a.pkg")


PSF = "Developer ID Installer: Python Software Foundation (BMM5U3QVKW)"


def fake_signature_tools(
    monkeypatch: pytest.MonkeyPatch, package_app, *, signer: str = PSF, gatekeeper_accepts: bool = True,
) -> list[list[str]]:
    """pkgutil and spctl as they answer for a python.org installer."""
    checks: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        checks.append(cmd)
        if cmd[0] == "pkgutil":
            return subprocess.CompletedProcess(cmd, 0, (
                "Package \"python.pkg\":\n   Status: signed by a developer certificate issued by Apple for distribution\n"
                f"   Notarization: trusted by the Apple notary service\n   Certificate Chain:\n    1. {signer}\n"
            ), "")
        if gatekeeper_accepts:
            return subprocess.CompletedProcess(cmd, 0, "", f"{cmd[-1]}: accepted\nsource=Notarized Developer ID\n")
        return subprocess.CompletedProcess(cmd, 3, "", f"{cmd[-1]}: rejected\n")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)
    return checks


class CutShortResponse(io.BytesIO):
    """A download whose connection drops after its first bytes."""

    def read(self, size: int = -1) -> bytes:
        if self.tell():
            raise ConnectionResetError(54, "Connection reset by peer")
        return super().read(4)


def test_a_download_cut_short_leaves_nothing_under_its_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    destination = tmp_path / "python-3.15.2-macos11.pkg"
    monkeypatch.setattr(package_app.urllib.request, "urlopen", lambda url, timeout: CutShortResponse(b"python installer"))

    with pytest.raises(ConnectionResetError):
        package_app.download_file("https://x.test/python.pkg", destination)
    assert list(tmp_path.iterdir()) == []


def test_a_complete_download_replaces_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    destination = tmp_path / "cache" / "python-3.15.2-macos11.pkg"
    monkeypatch.setattr(package_app.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"python installer"))

    package_app.download_file("https://x.test/python.pkg", destination)
    assert destination.read_bytes() == b"python installer"
    assert [path.name for path in destination.parent.iterdir()] == [destination.name]


def test_only_the_runtime_installer_in_use_stays_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    fake_python_runtime_download(monkeypatch, package_app, b"python")
    fake_signature_tools(monkeypatch, package_app)
    cache = package_app.package_cache_dir("python-runtime")
    for name in ("python-3.13.13-macos11.pkg", "python-3.15.1-macos11.pkg", "python-3.15.2-macos11.pkg.partial"):
        (cache / name).write_bytes(b"older")

    in_use = package_app.python_runtime_pkg(python_runtime_args())

    assert sorted(path.name for path in cache.iterdir()) == [in_use.name] == ["python-3.15.2-macos11.pkg"]


def fake_python_runtime_download(monkeypatch: pytest.MonkeyPatch, package_app, payload: bytes) -> list[str]:
    downloads: list[str] = []

    def fake_download(url: str, destination: Path) -> None:
        downloads.append(url)
        destination.write_bytes(payload)

    monkeypatch.setattr(package_app, "download_file", fake_download)
    monkeypatch.setattr(package_app, "newest_python_runtime_url", lambda: f"{INDEX}3.15.2/python-3.15.2-macos11.pkg")
    return downloads


def test_the_runtime_is_downloaded_once_and_its_signature_checked_on_every_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    downloads = fake_python_runtime_download(monkeypatch, package_app, b"python")
    checks = fake_signature_tools(monkeypatch, package_app)

    first = package_app.python_runtime_pkg(python_runtime_args())
    cached = package_app.python_runtime_pkg(python_runtime_args())

    assert first == cached
    assert first.name == "python-3.15.2-macos11.pkg"
    assert downloads == [f"{INDEX}3.15.2/python-3.15.2-macos11.pkg"]
    assert checks == [
        ["pkgutil", "--check-signature", str(first)], ["spctl", "-a", "-t", "install", str(first)],
    ] * 2


@pytest.mark.parametrize("signer,gatekeeper_accepts,reason", [
    ("Developer ID Installer: Python Software Foundation (XXXXXXXXXX)", True, "does not name " + re.escape(PSF)),
    (PSF, False, "Gatekeeper refuses it"),
])
def test_a_runtime_download_that_is_not_the_psfs_is_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, signer: str, gatekeeper_accepts: bool, reason: str,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    fake_python_runtime_download(monkeypatch, package_app, b"lookalike")
    fake_signature_tools(monkeypatch, package_app, signer=signer, gatekeeper_accepts=gatekeeper_accepts)

    with pytest.raises(RuntimeError, match=reason):
        package_app.python_runtime_pkg(python_runtime_args())
    # The next run downloads it again instead of trusting the cached copy.
    assert list((tmp_path / ".build" / "package-app" / "python-runtime").iterdir()) == []


def test_a_signature_check_that_does_not_finish_keeps_the_installer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    downloads = fake_python_runtime_download(monkeypatch, package_app, b"python")
    fake_signature_tools(monkeypatch, package_app)
    real_run = package_app.subprocess.run
    timeouts: list[object] = []

    def stalled_spctl(cmd: list[str], **kwargs: object):
        if cmd[0] == "spctl":
            timeouts.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(package_app.subprocess, "run", stalled_spctl)

    with pytest.raises(RuntimeError, match="could not check the signature of .*: spctl -a -t install did not finish within 120 s"):
        package_app.python_runtime_pkg(python_runtime_args())
    assert timeouts == [package_app.PYTHON_RUNTIME_SIGNATURE_TIMEOUT]
    # The installer may be fine; the next run checks it again without downloading it.
    cached = tmp_path / ".build" / "package-app" / "python-runtime" / "python-3.15.2-macos11.pkg"
    assert cached.read_bytes() == b"python"

    fake_signature_tools(monkeypatch, package_app)
    assert package_app.python_runtime_pkg(python_runtime_args()) == cached
    assert downloads == [f"{INDEX}3.15.2/python-3.15.2-macos11.pkg"]


def test_a_runtime_url_the_caller_gives_replaces_the_lookup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    downloads = fake_python_runtime_download(monkeypatch, package_app, b"older")
    monkeypatch.setattr(package_app, "newest_python_runtime_url", lambda: pytest.fail("looked up the newest Python"))
    checks = fake_signature_tools(monkeypatch, package_app)

    pinned = package_app.python_runtime_pkg(python_runtime_args(python_runtime_url=f"{INDEX}3.15.1/python-3.15.1-macos11.pkg"))

    assert (pinned.name, pinned.read_bytes()) == ("python-3.15.1-macos11.pkg", b"older")
    assert downloads == [f"{INDEX}3.15.1/python-3.15.1-macos11.pkg"]
    # It must be the PSF's like any other.
    assert checks[0] == ["pkgutil", "--check-signature", str(pinned)]


def record_python_package_installs(monkeypatch: pytest.MonkeyPatch, package_app, *, fail_first: bool = False) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if fail_first:
            raise subprocess.CalledProcessError(2, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(package_app, "run", fake_run)
    monkeypatch.setattr(package_app, "remove_optional_zeroconf_extensions", lambda site_packages: None)
    return calls


def test_python_packages_are_two_uv_installs_for_the_bundled_python(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    installed: list[str] = []
    calls = record_python_package_installs(monkeypatch, package_app)
    real_run = package_app.run

    def keep_requirements(cmd: list[str], **kwargs: object):
        if "-r" in cmd:
            installed.append(Path(cmd[cmd.index("-r") + 1]).read_text(encoding="utf-8"))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(package_app, "run", keep_requirements)
    monkeypatch.setattr(package_app.shutil, "which", lambda name: "/opt/bin/uv" if name == "uv" else None)
    site_packages = tmp_path / "site-packages"

    package_app.build_python_packages("/App/Python/bin/python3", site_packages, PACKAGES)

    # The builder's uv configuration never applies, and only releases at
    # least a day old are taken.
    install = [
        "/opt/bin/uv", "pip", "install", "--no-config", "--exclude-newer", "1 day",
        "--python", "/App/Python/bin/python3", "--target", str(site_packages),
    ]
    assert calls[0][:-1] == [*install, "--no-build", "-r"]
    assert calls[1] == [*install, "--no-deps", str(package_app.REPO_ROOT)]
    # Exactly the resolved packages, wheels only.
    assert installed == [PACKAGES]


def test_python_packages_resolve_for_the_bundled_python_with_wheels_only(monkeypatch: pytest.MonkeyPatch) -> None:
    package_app = load_package_app_module()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, PACKAGES, "")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)
    monkeypatch.setattr(package_app, "uv_executable", lambda: "/opt/bin/uv")

    assert package_app.resolve_python_packages("/App/Python/bin/python3") == PACKAGES
    cmd, kwargs = calls[0]
    assert cmd == [
        "/opt/bin/uv", "pip", "compile", "--no-config", "--exclude-newer", "1 day", "--quiet", "--no-header",
        "--no-annotate", "--only-binary", ":all:", "--python", "/App/Python/bin/python3",
        str(package_app.REPO_ROOT / "pyproject.toml"), "-",
    ]
    # certifi beside the project's dependencies: python.org's Python has no CA certificates.
    assert kwargs["input"] == "certifi\n"
    assert kwargs["check"] is True


def test_python_packages_leave_no_build_machine_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    site_packages = tmp_path / "site-packages"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        # What the two installs write, build-machine paths included.
        (site_packages / "bin").mkdir(parents=True, exist_ok=True)
        (site_packages / "bin" / "tcapsule").write_text("#!/Users/someone/dist/python3\n", encoding="utf-8")
        for name in ("timecapsulesmb-3.7.0", "zeroconf-0.151.5"):
            dist_info = site_packages / f"{name}.dist-info"
            dist_info.mkdir(exist_ok=True)
            (dist_info / "METADATA").write_text("Name: x\n", encoding="utf-8")
        (site_packages / "timecapsulesmb-3.7.0.dist-info" / "direct_url.json").write_text('{"url": "file:///Users/someone/repo"}', encoding="utf-8")
        (site_packages / "timecapsulesmb").mkdir(exist_ok=True)
        (site_packages / "timecapsulesmb" / "__init__.py").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(package_app, "uv_executable", lambda: "uv")
    monkeypatch.setattr(package_app, "run", fake_run)

    package_app.build_python_packages("python3", site_packages, PACKAGES)

    assert not (site_packages / "bin").exists()
    assert not (site_packages / "timecapsulesmb-3.7.0.dist-info" / "direct_url.json").exists()
    assert (site_packages / "timecapsulesmb-3.7.0.dist-info" / "METADATA").is_file()
    assert (site_packages / "zeroconf-0.151.5.dist-info" / "METADATA").is_file()
    assert (site_packages / "timecapsulesmb" / "__init__.py").is_file()


def test_python_packages_without_uv_stop_before_running_anything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    calls = record_python_package_installs(monkeypatch, package_app)
    monkeypatch.setattr(package_app.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="brew install uv"):
        package_app.build_python_packages("python3", tmp_path / "site-packages", PACKAGES)
    assert calls == []


def test_packaging_without_uv_stops_before_the_swift_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app.shutil, "which", lambda name: None)
    monkeypatch.setattr(package_app, "build_swift", lambda configuration, architectures: pytest.fail("Swift built"))
    args = SimpleNamespace(arch=["native"], notarize=False, codesign_identity=None)

    with pytest.raises(RuntimeError, match="uv, which is not on PATH"):
        package_app.build_app_package(args)


def test_a_failing_dependency_install_stops_before_the_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    calls = record_python_package_installs(monkeypatch, package_app, fail_first=True)
    monkeypatch.setattr(package_app, "uv_executable", lambda: "uv")

    with pytest.raises(subprocess.CalledProcessError):
        package_app.build_python_packages("python3", tmp_path / "site-packages", PACKAGES)
    assert len(calls) == 1
    assert "--no-build" in calls[0]


def test_site_packages_cache_key_follows_the_resolved_packages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(package_app, "python_cache_identity", lambda python: {"python": python})
    monkeypatch.setattr(package_app, "python_package_source_fingerprint", lambda: {})

    def key(packages: str) -> str:
        return package_app.python_site_packages_cache_entry("python3", ("arm64",), packages).name

    # A new release of any package is a new entry; the same set reuses one.
    assert key("ifaddr==0.2.0\n") == key("ifaddr==0.2.0\n")
    assert key("ifaddr==0.2.0\n") != key("ifaddr==0.2.1\n")


def write_fake_bundled_packages(site_packages: Path) -> None:
    """Packages laid out as the app bundles them, as plain modules."""
    modules = [
        "certifi/__init__.py",
        "Crypto/__init__.py",
        "Crypto/Cipher/__init__.py",
        "Crypto/Cipher/AES.py",
        "ifaddr/__init__.py",
        "timecapsulesmb/__init__.py",
        "zeroconf/__init__.py",
        "zopfli/__init__.py",
        "zopfli/gzip.py",
    ]
    for module in modules:
        path = site_packages / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    askpass = site_packages / "timecapsulesmb" / "transport" / "ssh-askpass"
    askpass.parent.mkdir()
    askpass.write_text("#!/bin/sh\n", encoding="utf-8")
    askpass.chmod(0o755)


def use_this_python_as_bundled(monkeypatch: pytest.MonkeyPatch, package_app, tmp_path: Path) -> None:
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(package_app, "bundled_python_executable", lambda app: Path(sys.executable))
    monkeypatch.setattr(package_app, "bundled_python_home", lambda app: Path(sys.base_prefix))


def test_bundled_python_check_loads_the_compiled_modules_the_app_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    use_this_python_as_bundled(monkeypatch, package_app, tmp_path)
    app = tmp_path / "TimeCapsuleSMB.app"
    site_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    write_fake_bundled_packages(site_packages)

    package_app.assert_python_dependencies_are_bundled(app)

    # Firmware decryption's AES module, not just its package.
    (site_packages / "Crypto" / "Cipher" / "AES.py").unlink()
    with pytest.raises(RuntimeError, match=r"(?s)not importable.*Crypto\.Cipher\.AES"):
        package_app.assert_python_dependencies_are_bundled(app)


def test_check_app_launches_the_apps_code_without_building(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    app = tmp_path / "TimeCapsuleSMB.app"
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(package_app, "validate_app_resources", lambda path: calls.append(("app", path)))
    monkeypatch.setattr(package_app, "assert_python_dependencies_are_bundled", lambda path: calls.append(("python", path)))
    monkeypatch.setattr(package_app, "smoke_test", lambda path, architectures: calls.append(("helper", path, architectures)))
    monkeypatch.setattr(package_app, "macho_architectures", lambda path: {"x86_64", "arm64"} if path.name == "TimeCapsuleSMB" else set())
    monkeypatch.setattr(package_app, "package_app", lambda args: pytest.fail("an app was built"))

    assert package_app.main(["--check-app", str(app)]) == 0

    assert calls == [("app", app.resolve()), ("python", app.resolve()), ("helper", app.resolve(), ("arm64", "x86_64"))]
    assert capsys.readouterr().out == f"Checked {app}\n"


def test_check_app_fails_when_the_app_does_not_launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()

    def crashed(path: Path) -> None:
        raise RuntimeError("App executable resource validation failed: dyld: Library not loaded")

    monkeypatch.setattr(package_app, "validate_app_resources", crashed)
    monkeypatch.setattr(package_app, "smoke_test", lambda path, architectures: pytest.fail("the helper ran after the app failed"))

    assert package_app.main(["--check-app", str(tmp_path / "TimeCapsuleSMB.app")]) == 1
    assert "Library not loaded" in capsys.readouterr().err


@pytest.mark.parametrize("architecture", [None, "x86_64"])
def test_app_launch_and_python_imports_run_under_the_architecture_given(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    architecture: str | None,
) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    app = tmp_path / "TimeCapsuleSMB.app"
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(package_app.subprocess, "run", fake_run)

    package_app.validate_app_resources(app, architecture=architecture)
    package_app.assert_python_dependencies_are_bundled(app, architecture=architecture)

    prefix = ["arch", f"-{architecture}"] if architecture else []
    assert commands[0] == [*prefix, str(app / "Contents" / "MacOS" / "TimeCapsuleSMB"), "--validate-resources"]
    assert commands[1][: len(prefix) + 2] == [*prefix, str(package_app.bundled_python_executable(app)), "-c"]


def test_a_failure_under_x86_64_names_the_architecture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(
        package_app.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="dyld: Library not loaded"),
    )
    app = tmp_path / "TimeCapsuleSMB.app"

    with pytest.raises(RuntimeError, match=r"resource validation failed \(x86_64\)"):
        package_app.validate_app_resources(app, architecture="x86_64")
    with pytest.raises(RuntimeError, match=r"not importable from the app package \(x86_64\)"):
        package_app.assert_python_dependencies_are_bundled(app, architecture="x86_64")


def test_a_bundled_file_naming_the_checkout_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_app = load_package_app_module()
    runner = tmp_path / "Users" / "runner"
    checkout = runner / "work" / "TimeCapsuleSMB"
    monkeypatch.setattr(package_app, "REPO_ROOT", checkout)
    app = tmp_path / "TimeCapsuleSMB.app"
    binary = app / "Contents" / "MacOS" / "TimeCapsuleSMB"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xcf\xfa\xed\xfe\0SO " + str(checkout / "Sources").encode() + b"\0")
    # A PyPI wheel built on another runner names that runner's checkout, not ours.
    wheel_module = app / "Contents" / "Resources" / "Python" / "site-packages" / "Crypto" / "_raw_aes.abi3.so"
    wheel_module.parent.mkdir(parents=True)
    wheel_module.write_bytes(b"\xcf\xfa\xed\xfe\0OSO " + str(runner / "work" / "pycryptodome" / "AES.o").encode() + b"\0")
    # A sibling folder whose name starts the same way is not this checkout.
    (app / "Contents" / "Info.plist").write_text(f"{checkout}2/elsewhere", encoding="utf-8")
    # A link is not read through.
    (app / "Contents" / "Link").symlink_to(binary)

    with pytest.raises(RuntimeError, match=r"(?s)name this checkout.*Contents/MacOS/TimeCapsuleSMB") as raised:
        package_app.assert_no_build_machine_paths(app)
    assert "_raw_aes" not in str(raised.value)
    assert "Info.plist" not in str(raised.value)
    assert "Link" not in str(raised.value)

    binary.write_bytes(b"\xcf\xfa\xed\xfe\0SO Sources/\0")
    package_app.assert_no_build_machine_paths(app)
