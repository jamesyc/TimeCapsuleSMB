from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


PACKAGE_SCRIPT = Path(__file__).resolve().parents[1] / "macos" / "TimeCapsuleSMB" / "tools" / "package_app.py"


def load_package_app_module():
    spec = importlib.util.spec_from_file_location("package_app", PACKAGE_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)
    (distribution / "artifact-manifest.json").write_text('{"artifacts":{}}', encoding="utf-8")

    monkeypatch.setattr(package_app, "artifact_paths", lambda: ["bin/payloads/one", "bin/payloads/two"])
    monkeypatch.setattr(package_app, "assert_python_dependencies_are_bundled", lambda app: None)
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
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)

    with pytest.raises(RuntimeError, match="missing bundled Python packages"):
        package_app.assert_bundle_layout(app)
