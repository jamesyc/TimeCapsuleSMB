#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.core.release import CLI_VERSION, CLI_VERSION_CODE  # noqa: E402

APP_NAME = "TimeCapsuleSMB"
PRODUCT_NAME = "TimeCapsuleSMB"
APP_VERSION = CLI_VERSION
APP_VERSION_CODE = str(CLI_VERSION_CODE)
APP_ICON_FILE = f"{PRODUCT_NAME}.icns"
APP_ICON_NAME = PRODUCT_NAME
DEFAULT_ICON_SOURCE = PACKAGE_ROOT / "Assets" / "AppIcon" / "tcs.jpg"
DEFAULT_RUNTIME_PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
ARTIFACT_MANIFEST = REPO_ROOT / "src" / "timecapsulesmb" / "assets" / "artifact-manifest.json"
BONJOUR_SERVICE_TYPES = [
    "_airport._tcp",
    "_smb._tcp",
    "_adisk._tcp",
    "_device-info._tcp",
]


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        input=input_text,
        text=True,
        check=True,
        stdout=subprocess.PIPE if input_text is not None else None,
        stderr=subprocess.PIPE if input_text is not None else None,
    )


def run_quiet(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def build_swift(configuration: str) -> Path:
    run(["swift", "build", "-c", configuration, "--product", PRODUCT_NAME], cwd=PACKAGE_ROOT)
    executable = PACKAGE_ROOT / ".build" / configuration / PRODUCT_NAME
    if not executable.is_file():
        raise RuntimeError(f"Swift build did not produce {executable}")
    return executable


def copy_resources(configuration: str, resources_dir: Path) -> None:
    build_dir = PACKAGE_ROOT / ".build" / configuration
    for resource_bundle in build_dir.glob("*.bundle"):
        destination = resources_dir / resource_bundle.name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(resource_bundle, destination)


def write_info_plist(contents_dir: Path, *, icon_name: str | None = None) -> None:
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": PRODUCT_NAME,
        "CFBundleIdentifier": "com.timecapsulesmb.TimeCapsuleSMB",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION_CODE,
        "LSMinimumSystemVersion": "14.0",
        "NSBonjourServices": BONJOUR_SERVICE_TYPES,
        "NSHighResolutionCapable": True,
        "NSLocalNetworkUsageDescription": "TimeCapsuleSMB discovers and connects to Time Capsule devices on your local network.",
    }
    if icon_name:
        info["CFBundleIconFile"] = icon_name
    with (contents_dir / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    (contents_dir / "PkgInfo").write_text("APPL????", encoding="utf-8")


def create_app_icon(source: Path, resources_dir: Path) -> None:
    if not source.is_file():
        raise RuntimeError(f"App icon source does not exist: {source}")

    icon_path = resources_dir / APP_ICON_FILE
    icon_entries = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]

    with tempfile.TemporaryDirectory(prefix="timecapsulesmb-iconset-") as tmp:
        iconset = Path(tmp) / f"{APP_ICON_NAME}.iconset"
        iconset.mkdir()
        for filename, size in icon_entries:
            run([
                "sips",
                "-s",
                "format",
                "png",
                "-z",
                str(size),
                str(size),
                str(source),
                "--out",
                str(iconset / filename),
            ])
        run(["iconutil", "-c", "icns", str(iconset), "-o", str(icon_path)])

    if not icon_path.is_file():
        raise RuntimeError(f"App icon generation did not produce {icon_path}")


def write_helper_wrapper(helper_path: Path) -> None:
    helper_path.write_text(
        """#!/bin/sh
set -eu

CONTENTS_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
PYTHON="${TCAPSULE_APP_PYTHON:-/usr/bin/python3}"
PYTHON_PACKAGES="$RESOURCES_DIR/Python/site-packages"

if [ -z "${TCAPSULE_STATE_DIR:-}" ]; then
    export TCAPSULE_STATE_DIR="$HOME/Library/Application Support/TimeCapsuleSMB"
fi
if [ -z "${TCAPSULE_CONFIG:-}" ]; then
    export TCAPSULE_CONFIG="$TCAPSULE_STATE_DIR/.env"
fi
if [ -z "${TCAPSULE_DISTRIBUTION_ROOT:-}" ]; then
    export TCAPSULE_DISTRIBUTION_ROOT="$RESOURCES_DIR/Distribution"
fi

mkdir -p "$TCAPSULE_STATE_DIR"
export PATH="$RESOURCES_DIR/Tools/bin:${PATH:-/usr/bin:/bin:/usr/sbin:/sbin}"
export PYTHONPATH="$PYTHON_PACKAGES${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONNOUSERSITE=1

exec "$PYTHON" -m timecapsulesmb.cli.main "$@"
""",
        encoding="utf-8",
    )
    helper_path.chmod(0o755)


def python_major_minor(python: str) -> tuple[int, int]:
    code = "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    completed = subprocess.run(
        [python, "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    major, minor = completed.stdout.strip().split(".", 1)
    return int(major), int(minor)


def create_python_packages(python: str, resources_dir: Path) -> None:
    python_root = resources_dir / "Python"
    if python_root.exists():
        shutil.rmtree(python_root)
    python_root.mkdir()
    site_packages = python_root / "site-packages"
    site_packages.mkdir()

    major, minor = python_major_minor(python)
    if (major, minor) < (3, 9):
        raise RuntimeError(f"TimeCapsuleSMB.app requires Python 3.9 or newer, got {major}.{minor} from {python}")

    with tempfile.TemporaryDirectory(prefix="timecapsulesmb-package-python-") as tmp:
        build_venv = Path(tmp) / "venv"
        run([python, "-m", "venv", str(build_venv)])
        build_python = build_venv / "bin" / "python"
        run([str(build_python), "-m", "pip", "install", "-U", "pip"])
        generated_build_lib = REPO_ROOT / "build" / "lib"
        build_lib_existed = generated_build_lib.exists()
        try:
            run([str(build_python), "-m", "pip", "install", "--target", str(site_packages), str(REPO_ROOT)])
        finally:
            if not build_lib_existed and generated_build_lib.exists():
                shutil.rmtree(generated_build_lib)


def copy_distribution(resources_dir: Path) -> None:
    distribution = resources_dir / "Distribution"
    if distribution.exists():
        shutil.rmtree(distribution)
    distribution.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "bin", distribution / "bin")
    shutil.copy2(ARTIFACT_MANIFEST, distribution / "artifact-manifest.json")
    assert_distribution_artifacts(distribution)


def artifact_paths() -> list[str]:
    data = json.loads(ARTIFACT_MANIFEST.read_text(encoding="utf-8"))
    artifacts = data.get("artifacts", {})
    paths: list[str] = []
    for record in artifacts.values():
        if isinstance(record, dict) and isinstance(record.get("path"), str):
            paths.append(record["path"])
    return sorted(paths)


def assert_distribution_artifacts(distribution: Path) -> None:
    missing = [path for path in artifact_paths() if not (distribution / path).is_file()]
    if missing:
        joined = "\n  - ".join(missing)
        raise RuntimeError(f"Bundled distribution is missing payload artifact(s):\n  - {joined}")


def copy_tool(name: str, tools_bin: Path) -> bool:
    source = shutil.which(name)
    if not source:
        return False
    destination = tools_bin / name
    shutil.copy2(source, destination)
    destination.chmod(0o755)
    return True


def copy_tools(resources_dir: Path, require_tools: bool) -> None:
    tools_bin = resources_dir / "Tools" / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)
    missing = [tool for tool in ("sshpass", "smbclient") if not copy_tool(tool, tools_bin)]
    if missing and require_tools:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing required host tool(s) for bundling: {joined}")
    if missing:
        print(f"warning: missing optional bundled tool(s): {', '.join(missing)}", file=sys.stderr)


def macho_dependencies(path: Path) -> list[str] | None:
    completed = subprocess.run(
        ["otool", "-L", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return None
    dependencies: list[str] = []
    resolved_path = path.resolve()
    for line in completed.stdout.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        dependency = stripped.split(" ", 1)[0]
        if dependency.startswith("/") and Path(dependency).resolve() == resolved_path:
            continue
        dependencies.append(dependency)
    return dependencies


def is_system_macho_dependency(dependency: str) -> bool:
    return (
        dependency.startswith("/usr/lib/")
        or dependency.startswith("/System/Library/")
        or dependency.startswith("@executable_path/")
        or dependency.startswith("@loader_path/")
        or dependency.startswith("@rpath/")
    )


def is_external_macho_dependency(dependency: str) -> bool:
    return dependency.startswith("/") and not is_system_macho_dependency(dependency)


def bundled_dependency_name(source: Path, used_names: set[str]) -> str:
    name = source.name
    if name not in used_names:
        used_names.add(name)
        return name
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:10]
    suffix = "".join(source.suffixes)
    stem = source.name[: -len(suffix)] if suffix else source.name
    candidate = f"{stem}-{digest}{suffix}"
    used_names.add(candidate)
    return candidate


def loader_path_reference(loader: Path, dependency: Path, frameworks_dir: Path) -> str:
    relative_frameworks = os.path.relpath(frameworks_dir, loader.parent)
    relative_dependency = Path(relative_frameworks) / dependency.name
    return f"@loader_path/{relative_dependency.as_posix()}"


def is_library_like_macho(path: Path) -> bool:
    return path.suffix in {".dylib", ".so"} or ".framework" in path.parts or path.parent.name in {"lib", "private"}


def set_macho_id_if_supported(path: Path) -> None:
    if not is_library_like_macho(path):
        return
    subprocess.run(
        ["install_name_tool", "-id", f"@loader_path/{path.name}", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def files_under(roots: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                candidates.append(path)
    return candidates


def macho_vendor_roots(app: Path) -> list[Path]:
    contents = app / "Contents"
    return files_under([
        contents / "Resources" / "Tools" / "bin",
        contents / "Frameworks",
    ])


def macho_validation_roots(app: Path) -> list[Path]:
    contents = app / "Contents"
    return files_under([
        contents / "MacOS",
        contents / "Resources" / "Tools" / "bin",
        contents / "Resources" / "Python" / "site-packages",
        contents / "Frameworks",
    ])


def vendor_macho_dependencies(app: Path) -> None:
    frameworks_dir = app / "Contents" / "Frameworks"
    frameworks_dir.mkdir()
    source_to_bundle: dict[Path, Path] = {}
    used_names: set[str] = set()
    queue = macho_vendor_roots(app)
    visited: set[Path] = set()

    while queue:
        current = queue.pop(0)
        current_resolved = current.resolve()
        if current_resolved in visited:
            continue
        visited.add(current_resolved)

        dependencies = macho_dependencies(current)
        if dependencies is None:
            continue

        for dependency in dependencies:
            if not is_external_macho_dependency(dependency):
                continue
            source = Path(dependency).resolve()
            if not source.is_file():
                raise RuntimeError(f"Mach-O dependency does not exist: {dependency} referenced by {current}")
            bundled = source_to_bundle.get(source)
            if bundled is None:
                bundled = frameworks_dir / bundled_dependency_name(source, used_names)
                shutil.copy2(source, bundled)
                bundled.chmod(bundled.stat().st_mode | 0o200)
                source_to_bundle[source] = bundled
                queue.append(bundled)
            run_quiet([
                "install_name_tool",
                "-change",
                dependency,
                loader_path_reference(current, bundled, frameworks_dir),
                str(current),
            ])

        set_macho_id_if_supported(current)


def assert_no_external_macho_dependencies(app: Path) -> None:
    external: list[str] = []
    for path in macho_validation_roots(app):
        dependencies = macho_dependencies(path)
        if dependencies is None:
            continue
        for dependency in dependencies:
            if is_external_macho_dependency(dependency):
                external.append(f"{path}: {dependency}")
    if external:
        joined = "\n  - ".join(external)
        raise RuntimeError(f"App bundle contains non-system Mach-O dependency reference(s):\n  - {joined}")


def assert_python_dependencies_are_bundled(app: Path) -> None:
    env = os.environ.copy()
    site_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    env["PYTHONPATH"] = str(site_packages)
    env["PYTHONNOUSERSITE"] = "1"
    code = (
        "import Crypto, ifaddr, pexpect, timecapsulesmb, zeroconf, zopfli.gzip\n"
        "paths = [Crypto.__file__, ifaddr.__file__, pexpect.__file__, timecapsulesmb.__file__, zeroconf.__file__, zopfli.__file__]\n"
        "bad = [p for p in paths if not p or '/Contents/Resources/Python/site-packages/' not in p]\n"
        "raise SystemExit('\\n'.join(bad) if bad else 0)\n"
    )
    completed = subprocess.run(
        ["/usr/bin/python3", "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Bundled Python dependencies are not importable from the app package:\n{completed.stderr}")


def parse_helper_events(stdout: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def smoke_request(helper: Path, operation: str, state_dir: Path) -> None:
    env = os.environ.copy()
    env["TCAPSULE_STATE_DIR"] = str(state_dir)
    env["TCAPSULE_CONFIG"] = str(state_dir / ".env")
    request = json.dumps({"operation": operation, "params": {}})
    completed = run([str(helper), "api"], input_text=request, env=env)
    result_event = next(
        (
            event
            for event in parse_helper_events(completed.stdout)
            if event.get("operation") == operation and event.get("type") == "result"
        ),
        None,
    )
    if result_event is None:
        raise RuntimeError(f"{operation} smoke test did not emit a result event:\n{completed.stdout}\n{completed.stderr}")
    if result_event.get("ok") is not True:
        raise RuntimeError(f"{operation} smoke test failed:\n{completed.stdout}\n{completed.stderr}")


def assert_bundle_layout(app: Path, *, icon_name: str | None = None) -> None:
    helper = app / "Contents" / "Helpers" / "tcapsule"
    info_plist = app / "Contents" / "Info.plist"
    distribution = app / "Contents" / "Resources" / "Distribution"
    artifact_manifest = distribution / "artifact-manifest.json"
    tools_bin = app / "Contents" / "Resources" / "Tools" / "bin"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    required_executables = [helper]
    missing_executables = [path for path in required_executables if not path.is_file() or not os.access(path, os.X_OK)]
    if missing_executables:
        joined = "\n  - ".join(str(path) for path in missing_executables)
        raise RuntimeError(f"App bundle is missing required executable(s):\n  - {joined}")
    if not python_packages.is_dir():
        raise RuntimeError(f"App bundle is missing bundled Python packages: {python_packages}")
    if not (distribution / "bin").is_dir():
        raise RuntimeError(f"App bundle is missing bundled payload directory: {distribution / 'bin'}")
    if not artifact_manifest.is_file():
        raise RuntimeError(f"App bundle is missing bundled artifact manifest: {artifact_manifest}")
    if not tools_bin.is_dir():
        raise RuntimeError(f"App bundle is missing bundled tools directory: {tools_bin}")
    if icon_name:
        icon_file = app / "Contents" / "Resources" / f"{icon_name}.icns"
        if not icon_file.is_file():
            raise RuntimeError(f"App bundle is missing app icon: {icon_file}")
        with info_plist.open("rb") as handle:
            info = plistlib.load(handle)
        if info.get("CFBundleIconFile") != icon_name:
            raise RuntimeError(f"Info.plist does not reference app icon {icon_name}")
    assert_distribution_artifacts(distribution)
    assert_python_dependencies_are_bundled(app)
    assert_no_external_macho_dependencies(app)


def smoke_test(app: Path) -> None:
    helper = app / "Contents" / "Helpers" / "tcapsule"
    with tempfile.TemporaryDirectory(prefix="timecapsulesmb-package-smoke-") as tmp:
        state_dir = Path(tmp)
        smoke_request(helper, "capabilities", state_dir)
        smoke_request(helper, "validate-install", state_dir)


def package_app(args: argparse.Namespace) -> Path:
    executable = build_swift(args.configuration)
    output_dir = args.output.resolve()
    app = output_dir / f"{APP_NAME}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    helpers = contents / "Helpers"
    resources = contents / "Resources"

    if app.exists():
        shutil.rmtree(app)
    macos.mkdir(parents=True)
    helpers.mkdir()
    resources.mkdir()

    icon_name = APP_ICON_NAME if args.icon else None
    write_info_plist(contents, icon_name=icon_name)
    shutil.copy2(executable, macos / PRODUCT_NAME)
    copy_resources(args.configuration, resources)
    if args.icon:
        create_app_icon(args.icon.resolve(), resources)
    write_helper_wrapper(helpers / "tcapsule")
    create_python_packages(args.python, resources)
    copy_distribution(resources)
    copy_tools(resources, args.require_tools)
    vendor_macho_dependencies(app)
    assert_bundle_layout(app, icon_name=icon_name)

    if not args.skip_smoke:
        smoke_test(app)
    return app


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a self-contained TimeCapsuleSMB.app bundle.")
    parser.add_argument("--output", type=Path, default=PACKAGE_ROOT / "dist", help="Directory that will receive TimeCapsuleSMB.app.")
    parser.add_argument("--configuration", choices=("debug", "release"), default="release", help="Swift build configuration.")
    parser.add_argument(
        "--icon",
        type=Path,
        default=DEFAULT_ICON_SOURCE,
        help="Source image to convert into the app bundle .icns icon.",
    )
    parser.add_argument("--python", default=DEFAULT_RUNTIME_PYTHON, help="Python interpreter used to build app-bundled packages; defaults to macOS /usr/bin/python3.")
    parser.add_argument("--require-tools", action="store_true", help="Fail if sshpass or smbclient cannot be copied into the app bundle.")
    parser.add_argument("--skip-smoke", action="store_true", help="Skip bundled helper capabilities and validate-install smoke tests.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        app = package_app(parse_args(argv or sys.argv[1:]))
    except subprocess.CalledProcessError as exc:
        print(f"command failed with exit code {exc.returncode}: {exc.cmd}", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return exc.returncode or 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
