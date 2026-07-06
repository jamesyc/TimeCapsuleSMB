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


def test_build_helper_creates_universal_helper_with_lipo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package_app = load_package_app_module()
    monkeypatch.setattr(package_app, "PACKAGE_ROOT", tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["swift", "build"]:
            architecture = cmd[cmd.index("--triple") + 1].split("-", 1)[0]
            product = cmd[cmd.index("--product") + 1]
            executable = package_app.swift_build_dir("release", architecture) / product
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text(architecture, encoding="utf-8")
            executable.chmod(0o755)
        if cmd and cmd[0] == "lipo":
            output = Path(cmd[cmd.index("-output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("universal helper", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(package_app, "run", fake_run)

    executable = package_app.build_helper("release", ("arm64", "x86_64"))

    assert executable == tmp_path / ".build" / "package-app" / "release" / "tcapsule"
    assert ["swift", "build"] == calls[0][:2]
    assert "--product" in calls[0]
    assert calls[0][calls[0].index("--product") + 1] == "tcapsule"
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
    assert calls == ["runtime", "external", "codesign", "app-codesign"]


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
    capsys: pytest.CaptureFixture[str],
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
    capsys.readouterr()
    package_app.copy_native_tools_layer(second_app, ("arm64",))
    captured = capsys.readouterr()

    assert len(vendor_calls) == 1
    assert "Using cached native tool layer." in captured.err
    assert (second_app / "Contents" / "Resources" / "Tools" / "bin" / "smbclient").is_file()
    assert (second_app / "Contents" / "Frameworks" / "libnative.dylib").is_file()


def test_copy_native_tools_layer_rebuilds_when_vendored_input_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    capsys.readouterr()
    dependency.write_text("changed", encoding="utf-8")
    package_app.copy_native_tools_layer(tmp_path / "Second.app", ("arm64",))
    captured = capsys.readouterr()

    assert len(vendor_calls) == 2
    assert "Rebuilding native tool layer: cached input changed:" in captured.err
    assert str(dependency) in captured.err


def test_copy_native_tools_layer_rebuilds_when_cached_output_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    capsys.readouterr()
    cache_entry = next((tmp_path / ".build" / "package-app" / "native-tools").iterdir())
    (cache_entry / "Contents" / "Frameworks" / "libnative.dylib").write_text("corrupt", encoding="utf-8")
    package_app.copy_native_tools_layer(tmp_path / "Second.app", ("arm64",))
    captured = capsys.readouterr()

    assert len(vendor_calls) == 2
    assert "Rebuilding native tool layer: cached output tree changed:" in captured.err
    assert str(cache_entry / "Contents") in captured.err


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
