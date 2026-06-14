#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from timecapsulesmb.core.release import CLI_VERSION, CLI_VERSION_CODE  # noqa: E402

APP_NAME = "TimeCapsuleSMB"
PRODUCT_NAME = "TimeCapsuleSMB"
HELPER_PRODUCT_NAME = "tcapsule"
APP_VERSION = CLI_VERSION
APP_VERSION_CODE = str(CLI_VERSION_CODE)
APP_ICON_FILE = f"{PRODUCT_NAME}.icns"
APP_ICON_NAME = PRODUCT_NAME
DEFAULT_ICON_SOURCE = PACKAGE_ROOT / "Assets" / "AppIcon" / "tcs.jpg"
ARTIFACT_MANIFEST = REPO_ROOT / "src" / "timecapsulesmb" / "assets" / "artifact-manifest.json"
RESOURCE_BUNDLE_NAME = "TimeCapsuleSMBMac_TimeCapsuleSMBApp.bundle"
PYTHON_RUNTIME_VERSION = "3.13.13"
PYTHON_RUNTIME_URL = f"https://www.python.org/ftp/python/{PYTHON_RUNTIME_VERSION}/python-{PYTHON_RUNTIME_VERSION}-macos11.pkg"
PYTHON_FRAMEWORK_NAME = "Python.framework"
APP_BUNDLED_PYTHON_REQUIREMENTS = ("certifi>=2024.8.30",)
DEFAULT_ARCHITECTURES = ("arm64", "x86_64")
CACHE_KEY_VERSION = 1
PYTHON_RUNTIME_CACHE_VERSION = 2
PYTHON_SITE_PACKAGES_CACHE_VERSION = 2
APP_ICON_CACHE_VERSION = 1
NATIVE_TOOLS_CACHE_VERSION = 1
CACHE_COMPLETE_MARKER = ".complete"
CACHE_MANIFEST_FILE = "manifest.json"
PACKAGE_CACHE_IGNORED_NAMES = {"__pycache__", ".DS_Store"}
PACKAGE_CACHE_IGNORED_SUFFIXES = {".pyc", ".pyo"}
PYTHON_SUBPROCESS_BYTECODE_CACHE = "python-bytecode"
APP_ICON_ENTRIES = [
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
SWIFT_TRIPLES = {
    "arm64": "arm64-apple-macosx14.0",
    "x86_64": "x86_64-apple-macosx14.0",
}
REQUIRED_HOST_TOOLS = ("sshpass", "smbclient")
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


def native_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "arm64e"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    raise RuntimeError(f"Unsupported macOS build architecture: {machine}")


def resolve_architectures(values: list[str] | None) -> tuple[str, ...]:
    requested = values or ["universal"]
    architectures: list[str] = []
    for value in requested:
        if value == "universal":
            candidates = list(DEFAULT_ARCHITECTURES)
        elif value == "native":
            candidates = [native_architecture()]
        else:
            candidates = [value]
        for candidate in candidates:
            if candidate not in SWIFT_TRIPLES:
                raise RuntimeError(f"Unsupported architecture: {candidate}")
            if candidate not in architectures:
                architectures.append(candidate)
    return tuple(architectures)


def swift_build_dir(configuration: str, architecture: str) -> Path:
    return PACKAGE_ROOT / ".build" / f"{architecture}-apple-macosx" / configuration


def build_swift_product(configuration: str, architectures: tuple[str, ...], product_name: str) -> tuple[Path, list[Path]]:
    executables: list[Path] = []
    build_dirs: list[Path] = []
    for architecture in architectures:
        run([
            "swift",
            "build",
            "-c",
            configuration,
            "--triple",
            SWIFT_TRIPLES[architecture],
            "--product",
            product_name,
        ], cwd=PACKAGE_ROOT)
        build_dir = swift_build_dir(configuration, architecture)
        executable = build_dir / product_name
        if not executable.is_file():
            raise RuntimeError(f"Swift build did not produce {executable}")
        executables.append(executable)
        build_dirs.append(build_dir)

    if len(executables) == 1:
        return executables[0], build_dirs

    universal_dir = PACKAGE_ROOT / ".build" / "package-app" / configuration
    universal_dir.mkdir(parents=True, exist_ok=True)
    universal_executable = universal_dir / product_name
    run(["lipo", "-create", *[str(path) for path in executables], "-output", str(universal_executable)])
    universal_executable.chmod(0o755)
    return universal_executable, build_dirs


def build_swift(configuration: str, architectures: tuple[str, ...]) -> tuple[Path, Path]:
    executable, build_dirs = build_swift_product(configuration, architectures, PRODUCT_NAME)
    return executable, build_dirs[0]


def build_helper(configuration: str, architectures: tuple[str, ...]) -> Path:
    executable, _build_dirs = build_swift_product(configuration, architectures, HELPER_PRODUCT_NAME)
    return executable


def copy_resources(build_dir: Path, resources_dir: Path) -> None:
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
        "NSLocalNetworkUsageDescription": "TimeCapsuleSMB discovers and connects to Apple AirPort devices on your local network.",
    }
    if icon_name:
        info["CFBundleIconFile"] = icon_name
    with (contents_dir / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    (contents_dir / "PkgInfo").write_text("APPL????", encoding="utf-8")


def app_icon_cache_entry(source: Path) -> Path:
    key = cache_key({
        "kind": "app-icon",
        "version": APP_ICON_CACHE_VERSION,
        "source": str(source.resolve()),
        "source_sha256": sha256_file(source),
        "entries": APP_ICON_ENTRIES,
    })
    return package_cache_dir("app-icon") / f"{key}.icns"


def create_app_icon(source: Path, resources_dir: Path, *, use_cache: bool = True) -> None:
    if not source.is_file():
        raise RuntimeError(f"App icon source does not exist: {source}")

    icon_path = resources_dir / APP_ICON_FILE
    if use_cache:
        cached_icon = app_icon_cache_entry(source)
        if cached_icon.is_file():
            print("Using cached app icon.", file=sys.stderr)
            shutil.copy2(cached_icon, icon_path)
            return

    with tempfile.TemporaryDirectory(prefix="timecapsulesmb-iconset-") as tmp:
        iconset = Path(tmp) / f"{APP_ICON_NAME}.iconset"
        iconset.mkdir()
        for filename, size in APP_ICON_ENTRIES:
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
    if use_cache:
        cached_icon = app_icon_cache_entry(source)
        cached_icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icon_path, cached_icon)


def copy_helper_executable(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(0o755)


def python_major_minor(python: str) -> tuple[int, int]:
    code = "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    completed = subprocess.run(
        [python, "-c", code],
        env=python_subprocess_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    major, minor = completed.stdout.strip().split(".", 1)
    return int(major), int(minor)


def package_cache_dir(name: str) -> Path:
    path = PACKAGE_ROOT / ".build" / "package-app" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def python_subprocess_env(
    env: dict[str, str] | None = None,
    *,
    python_home: Path | None = None,
    pycache_prefix: Path | None = None,
) -> dict[str, str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    if python_home is not None:
        merged["PYTHONHOME"] = str(python_home)
    prefix = pycache_prefix or package_cache_dir(PYTHON_SUBPROCESS_BYTECODE_CACHE)
    prefix.mkdir(parents=True, exist_ok=True)
    merged["PYTHONPYCACHEPREFIX"] = str(prefix)
    merged["PYTHONNOUSERSITE"] = "1"
    merged["PYTHONDONTWRITEBYTECODE"] = "1"
    return merged


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def should_ignore_cache_path(path: Path) -> bool:
    return (
        path.name in PACKAGE_CACHE_IGNORED_NAMES
        or path.suffix in PACKAGE_CACHE_IGNORED_SUFFIXES
        or any(part in PACKAGE_CACHE_IGNORED_NAMES for part in path.parts)
    )


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if should_ignore_cache_path(path):
            continue
        if path.is_symlink():
            digest.update(f"link:{relative}\0{os.readlink(path)}\0".encode("utf-8"))
        elif path.is_file():
            digest.update(f"file:{relative}\0".encode("utf-8"))
            digest.update(sha256_file(path).encode("ascii"))
            digest.update(b"\0")
    return digest.hexdigest()


def cache_key(data: dict[str, object]) -> str:
    encoded = json.dumps(
        {"cache_key_version": CACHE_KEY_VERSION, **data},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def cache_is_complete(entry: Path, required_path: Path) -> bool:
    return (entry / CACHE_COMPLETE_MARKER).is_file() and required_path.exists()


def mark_cache_complete(entry: Path) -> None:
    (entry / CACHE_COMPLETE_MARKER).write_text("ok\n", encoding="utf-8")


def replace_path(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def copy_path(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def cache_manifest_path(entry: Path) -> Path:
    return entry / CACHE_MANIFEST_FILE


def input_fingerprint(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {"path": str(resolved), "sha256": sha256_file(resolved)}


def input_fingerprints(paths: list[Path] | set[Path]) -> list[dict[str, str]]:
    return [input_fingerprint(path) for path in sorted({path.resolve() for path in paths})]


def write_cache_manifest(entry: Path, manifest: dict[str, object]) -> None:
    cache_manifest_path(entry).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_cache_manifest(entry: Path) -> dict[str, object] | None:
    path = cache_manifest_path(entry)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def cache_manifest_inputs_current(entry: Path) -> bool:
    return cache_manifest_inputs_miss_reason(entry) is None


def cache_manifest_inputs_miss_reason(entry: Path) -> str | None:
    manifest = read_cache_manifest(entry)
    if manifest is None:
        return "cache manifest is missing"
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        return "cache manifest inputs are invalid"
    for record in inputs:
        if not isinstance(record, dict):
            return "cache manifest input record is invalid"
        path_value = record.get("path")
        sha256_value = record.get("sha256")
        if not isinstance(path_value, str) or not isinstance(sha256_value, str):
            return "cache manifest input record is incomplete"
        path = Path(path_value)
        if not path.is_file():
            return f"cached input is missing: {path}"
        current_sha256 = sha256_file(path)
        if current_sha256 != sha256_value:
            return f"cached input changed: {path}"
    return None


def cache_manifest_output_current(entry: Path, output_root: Path) -> bool:
    return cache_manifest_output_miss_reason(entry, output_root) is None


def cache_manifest_output_miss_reason(entry: Path, output_root: Path) -> str | None:
    manifest = read_cache_manifest(entry)
    if manifest is None:
        return "cache manifest is missing"
    expected = manifest.get("output_tree_sha256")
    if not isinstance(expected, str):
        return "cache manifest output hash is invalid"
    if not output_root.is_dir():
        return f"cached output directory is missing: {output_root}"
    if sha256_tree(output_root) != expected:
        return f"cached output tree changed: {output_root}"
    return None


def download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def python_runtime_pkg(args: argparse.Namespace) -> Path:
    if args.python_runtime_pkg:
        return args.python_runtime_pkg.resolve()
    cache_dir = package_cache_dir("python-runtime")
    filename = Path(args.python_runtime_url).name or f"python-{PYTHON_RUNTIME_VERSION}-macos11.pkg"
    destination = cache_dir / filename
    if not destination.is_file():
        print(f"Downloading bundled Python runtime: {args.python_runtime_url}", file=sys.stderr)
        download_file(args.python_runtime_url, destination)
    return destination


def python_framework_executable(framework: Path) -> Path:
    return framework_version_dir(framework) / "bin" / "python3"


def python_framework_dylib(framework: Path) -> Path:
    return framework_version_dir(framework) / "Python"


def python_runtime_source(args: argparse.Namespace) -> tuple[str, Path, dict[str, object]]:
    if args.python_runtime_framework:
        source = args.python_runtime_framework.resolve()
        return (
            "framework",
            source,
            {
                "source": str(source),
                "tree_sha256": sha256_tree(source),
            },
        )
    source = python_runtime_pkg(args)
    return (
        "pkg",
        source,
        {
            "source": str(source),
            "source_sha256": sha256_file(source),
            "url": args.python_runtime_url,
        },
    )


def prepared_python_framework(args: argparse.Namespace, architectures: tuple[str, ...]) -> Path:
    source_kind, source, source_fingerprint = python_runtime_source(args)
    cache_root = package_cache_dir("python-framework")
    key = cache_key({
        "kind": "python-framework",
        "version": PYTHON_RUNTIME_CACHE_VERSION,
        "source_kind": source_kind,
        "source_fingerprint": source_fingerprint,
        "architectures": architectures,
    })
    entry = cache_root / key
    framework = entry / PYTHON_FRAMEWORK_NAME

    if cache_is_complete(entry, framework / "Versions" / "Current" / "bin" / "python3"):
        print("Using cached Python.framework.", file=sys.stderr)
        return framework

    with tempfile.TemporaryDirectory(prefix=f"{key}.tmp-", dir=cache_root) as tmp:
        staging = Path(tmp) / "entry"
        staging.mkdir()
        staged_framework = staging / PYTHON_FRAMEWORK_NAME
        if source_kind == "framework":
            shutil.copytree(source, staged_framework, symlinks=True)
        else:
            extract_python_framework(source, staged_framework)
        prune_python_runtime(staged_framework)
        remove_python_bytecode(staged_framework)
        remove_appledouble_files(staged_framework)
        rewrite_python_framework_install_names(staged_framework)
        assert_macho_has_architectures(python_framework_executable(staged_framework), architectures, "Bundled Python executable")
        assert_macho_has_architectures(python_framework_dylib(staged_framework), architectures, "Bundled Python framework")
        assert_macho_architectures_for_roots([staged_framework], architectures, "Bundled Python runtime architecture")
        assert_no_external_macho_dependencies_for_roots([staged_framework])
        ad_hoc_codesign_python_framework(staged_framework)
        assert_macho_code_signatures_valid_for_roots([staged_framework])
        mark_cache_complete(staging)
        replace_path(staging, entry)
        return framework


def extract_python_framework(pkg_path: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    with tempfile.TemporaryDirectory(prefix="timecapsulesmb-python-runtime-") as tmp:
        expanded = Path(tmp) / "expanded"
        run(["pkgutil", "--expand-full", str(pkg_path), str(expanded)])
        payload = expanded / "Python_Framework.pkg" / "Payload"
        if (payload / "Versions" / "Current" / "Python").is_file():
            shutil.copytree(payload, destination, symlinks=True)
            return destination
        frameworks = [path for path in expanded.rglob(PYTHON_FRAMEWORK_NAME) if (path / "Versions" / "Current" / "Python").is_file()]
        if not frameworks:
            raise RuntimeError(f"Python runtime package does not contain {PYTHON_FRAMEWORK_NAME}: {pkg_path}")
        shutil.copytree(frameworks[0], destination, symlinks=True)
    return destination


def copy_python_runtime(args: argparse.Namespace, resources_dir: Path, architectures: tuple[str, ...]) -> Path:
    runtime_dir = resources_dir / "Python" / "Runtime"
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True)
    framework = runtime_dir / PYTHON_FRAMEWORK_NAME

    if getattr(args, "no_cache", False):
        if args.python_runtime_framework:
            shutil.copytree(args.python_runtime_framework.resolve(), framework, symlinks=True)
        else:
            extract_python_framework(python_runtime_pkg(args), framework)
        prune_python_runtime(framework)
        remove_python_bytecode(framework)
        remove_appledouble_files(framework)
        rewrite_python_framework_install_names(framework)
        assert_macho_architectures_for_roots([framework], architectures, "Bundled Python runtime architecture")
        assert_no_external_macho_dependencies_for_roots([framework])
        ad_hoc_codesign_python_framework(framework)
        assert_macho_code_signatures_valid_for_roots([framework])
    else:
        cached_framework = prepared_python_framework(args, architectures)
        shutil.copytree(cached_framework, framework, symlinks=True)
    python_executable = bundled_python_executable_from_resources(resources_dir)
    if not python_executable.is_file():
        raise RuntimeError(f"Bundled Python executable is missing: {python_executable}")
    assert_macho_has_architectures(python_executable, architectures, "Bundled Python executable")
    assert_macho_has_architectures(bundled_python_dylib_from_resources(resources_dir), architectures, "Bundled Python framework")
    return python_executable


def bundled_python_home(app: Path) -> Path:
    return bundled_python_framework(app) / "Versions" / "Current"


def bundled_python_framework(app: Path) -> Path:
    return app / "Contents" / "Resources" / "Python" / "Runtime" / PYTHON_FRAMEWORK_NAME


def bundled_python_executable(app: Path) -> Path:
    return bundled_python_home(app) / "bin" / "python3"


def bundled_python_dylib(app: Path) -> Path:
    return bundled_python_home(app) / "Python"


def bundled_python_executable_from_resources(resources_dir: Path) -> Path:
    return resources_dir / "Python" / "Runtime" / PYTHON_FRAMEWORK_NAME / "Versions" / "Current" / "bin" / "python3"


def bundled_python_dylib_from_resources(resources_dir: Path) -> Path:
    return resources_dir / "Python" / "Runtime" / PYTHON_FRAMEWORK_NAME / "Versions" / "Current" / "Python"


def framework_version_dir(framework: Path) -> Path:
    current = framework / "Versions" / "Current"
    if (current / "Python").exists():
        return current.resolve()
    versions = [path for path in (framework / "Versions").iterdir() if path.is_dir() and (path / "Python").is_file()]
    if not versions:
        raise RuntimeError(f"Bundled Python framework has no version directory: {framework}")
    return versions[0]


def loader_relative_reference(loader: Path, dependency: Path) -> str:
    return f"@loader_path/{os.path.relpath(dependency, loader.parent)}"


def rewrite_python_framework_install_names(framework: Path) -> None:
    version_dir = framework_version_dir(framework)
    original_prefix = f"/Library/Frameworks/{PYTHON_FRAMEWORK_NAME}/Versions/{version_dir.name}/"
    changed: set[Path] = set()
    for path in macho_files_under([framework]):
        if not macho_architectures(path):
            continue
        dependencies = macho_dependencies(path)
        if dependencies is None:
            continue
        for dependency in dependencies:
            if not dependency.startswith(original_prefix):
                continue
            bundled_dependency = version_dir / dependency.removeprefix(original_prefix)
            if not bundled_dependency.exists():
                continue
            run_quiet([
                "install_name_tool",
                "-change",
                dependency,
                loader_relative_reference(path, bundled_dependency),
                str(path),
            ])
            changed.add(path)
        if path.resolve() == (version_dir / "Python").resolve():
            run_quiet([
                "install_name_tool",
                "-id",
                f"@rpath/{PYTHON_FRAMEWORK_NAME}/Versions/{version_dir.name}/Python",
                str(path),
            ])
            changed.add(path)
        elif is_library_like_macho(path) and path.suffix not in {".a", ".so"}:
            run_quiet(["install_name_tool", "-id", f"@loader_path/{path.name}", str(path)])
            changed.add(path)
    for path in changed:
        ad_hoc_codesign(path)


def prune_python_runtime(framework: Path) -> None:
    version_dir = framework_version_dir(framework)
    for path in (version_dir / "bin").glob("*-intel64"):
        path.unlink()
    for relative_path in (
        "Frameworks/Tcl.framework",
        "Frameworks/Tk.framework",
        "lib/tcl8",
        "lib/tcl8.6",
        "lib/tk8.6",
        "lib/python3.13/idlelib",
        "lib/python3.13/tkinter",
        "lib/python3.13/test",
    ):
        path = version_dir / relative_path
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    for path in (version_dir / "lib" / "python3.13" / "lib-dynload").glob("_tkinter*.so"):
        path.unlink()


def remove_python_bytecode(root: Path) -> None:
    if not root.exists():
        return
    pycache_dirs = [
        path
        for path in root.rglob("__pycache__")
        if path.is_dir() and not path.is_symlink()
    ]
    for path in sorted(pycache_dirs, key=lambda candidate: len(candidate.parts), reverse=True):
        shutil.rmtree(path)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in PACKAGE_CACHE_IGNORED_SUFFIXES:
            path.unlink()


def appledouble_files_under(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("._*") if path.name.startswith("._"))


def remove_appledouble_files(root: Path) -> None:
    for path in sorted(appledouble_files_under(root), key=lambda candidate: len(candidate.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def assert_no_appledouble_files(root: Path) -> None:
    files = appledouble_files_under(root)
    if files:
        joined = "\n  - ".join(str(path) for path in files[:20])
        extra = "" if len(files) <= 20 else f"\n  - ... and {len(files) - 20} more"
        raise RuntimeError(f"App bundle contains AppleDouble metadata files:\n  - {joined}{extra}")


def python_cache_identity(python: str) -> dict[str, object]:
    code = (
        "import json, platform, sys, sysconfig\n"
        "print(json.dumps({\n"
        "  'version': sys.version,\n"
        "  'cache_tag': sys.implementation.cache_tag,\n"
        "  'platform': sysconfig.get_platform(),\n"
        "  'machine': platform.machine(),\n"
        "}, sort_keys=True))\n"
    )
    completed = subprocess.run(
        [python, "-c", code],
        env=python_subprocess_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(completed.stdout)


def python_package_source_fingerprint() -> dict[str, object]:
    inputs = {
        "pyproject.toml": sha256_file(REPO_ROOT / "pyproject.toml"),
        "requirements.txt": sha256_file(REPO_ROOT / "requirements.txt") if (REPO_ROOT / "requirements.txt").is_file() else None,
        "src/timecapsulesmb": sha256_tree(SRC_ROOT / "timecapsulesmb"),
    }
    return inputs


def python_site_packages_cache_entry(python: str, architectures: tuple[str, ...]) -> Path:
    key = cache_key({
        "kind": "python-site-packages",
        "version": PYTHON_SITE_PACKAGES_CACHE_VERSION,
        "python": python_cache_identity(python),
        "architectures": architectures,
        "bundled_requirements": APP_BUNDLED_PYTHON_REQUIREMENTS,
        "source": python_package_source_fingerprint(),
    })
    return package_cache_dir("python-site-packages") / key


def build_python_packages(python: str, site_packages: Path) -> None:
    major, minor = python_major_minor(python)
    if (major, minor) < (3, 9):
        raise RuntimeError(f"TimeCapsuleSMB.app requires Python 3.9 or newer, got {major}.{minor} from {python}")

    with tempfile.TemporaryDirectory(prefix="timecapsulesmb-package-python-") as tmp:
        build_venv = Path(tmp) / "venv"
        env = python_subprocess_env(pycache_prefix=Path(tmp) / "pycache")
        run([python, "-m", "venv", str(build_venv)], env=env)
        build_python = build_venv / "bin" / "python"
        run([str(build_python), "-m", "pip", "install", "-U", "pip"], env=env)
        generated_build_lib = REPO_ROOT / "build" / "lib"
        build_lib_existed = generated_build_lib.exists()
        try:
            run([str(build_python), "-m", "pip", "install", "--target", str(site_packages), str(REPO_ROOT)], env=env)
            run([str(build_python), "-m", "pip", "install", "--target", str(site_packages), *APP_BUNDLED_PYTHON_REQUIREMENTS], env=env)
        finally:
            if not build_lib_existed and generated_build_lib.exists():
                shutil.rmtree(generated_build_lib)
    remove_optional_zeroconf_extensions(site_packages)
    remove_python_bytecode(site_packages)
    remove_appledouble_files(site_packages)


def create_python_packages(
    python: str,
    resources_dir: Path,
    architectures: tuple[str, ...],
    *,
    use_cache: bool = True,
) -> None:
    python_root = resources_dir / "Python"
    site_packages = python_root / "site-packages"
    if site_packages.exists():
        shutil.rmtree(site_packages)
    python_root.mkdir(parents=True, exist_ok=True)

    if use_cache:
        entry = python_site_packages_cache_entry(python, architectures)
        cached_site_packages = entry / "site-packages"
        if cache_is_complete(entry, cached_site_packages):
            print("Using cached Python site-packages.", file=sys.stderr)
            shutil.copytree(cached_site_packages, site_packages)
            remove_python_bytecode(site_packages)
            return

        cache_root = entry.parent
        cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{entry.name}.tmp-", dir=cache_root) as tmp:
            staging = Path(tmp) / "entry"
            staged_site_packages = staging / "site-packages"
            staged_site_packages.mkdir(parents=True)
            build_python_packages(python, staged_site_packages)
            remove_python_bytecode(staged_site_packages)
            remove_appledouble_files(staged_site_packages)
            assert_macho_architectures_for_roots([staged_site_packages], architectures, "Bundled Python package architecture")
            assert_no_external_macho_dependencies_for_roots([staged_site_packages])
            ad_hoc_codesign_site_packages(staged_site_packages)
            assert_macho_code_signatures_valid_for_roots([staged_site_packages])
            mark_cache_complete(staging)
            replace_path(staging, entry)
        shutil.copytree(cached_site_packages, site_packages)
        return

    site_packages.mkdir()
    build_python_packages(python, site_packages)
    remove_python_bytecode(site_packages)
    remove_appledouble_files(site_packages)
    assert_macho_architectures_for_roots([site_packages], architectures, "Bundled Python package architecture")
    assert_no_external_macho_dependencies_for_roots([site_packages])
    ad_hoc_codesign_site_packages(site_packages)
    assert_macho_code_signatures_valid_for_roots([site_packages])


def remove_optional_zeroconf_extensions(site_packages: Path) -> None:
    # zeroconf's Cython modules are optional. PyPI currently publishes arm64-only
    # macOS wheels for CPython 3.9, so keep the app bundle portable by using the
    # pure-Python modules that ship in the same package.
    zeroconf = site_packages / "zeroconf"
    if not zeroconf.is_dir():
        return
    for extension in zeroconf.rglob("*.so"):
        extension.unlink()


def copy_distribution(resources_dir: Path) -> None:
    distribution = resources_dir / "Distribution"
    if distribution.exists():
        shutil.rmtree(distribution)
    distribution.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "bin", distribution / "bin")
    shutil.copy2(ARTIFACT_MANIFEST, distribution / "artifact-manifest.json")
    remove_appledouble_files(distribution)
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


def macho_architectures(path: Path) -> set[str]:
    completed = subprocess.run(
        ["lipo", "-archs", str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return set()
    return set(completed.stdout.strip().split())


def tool_env_names(name: str, architecture: str) -> list[str]:
    tool = name.upper().replace("-", "_")
    arch = architecture.upper().replace("-", "_")
    return [
        f"TCAPSULE_PACKAGE_{tool}_{arch}",
        f"TCAPSULE_PACKAGE_{tool}",
    ]


def unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def tool_candidates(name: str, architecture: str) -> list[Path]:
    paths: list[Path] = []
    for env_name in tool_env_names(name, architecture):
        value = os.getenv(env_name)
        if value:
            paths.append(Path(value))

    preferred_prefixes = {
        "arm64": [Path("/opt/homebrew/bin")],
        "x86_64": [Path("/usr/local/bin")],
    }
    paths.extend(prefix / name for prefix in preferred_prefixes.get(architecture, ()))
    if found := shutil.which(name):
        paths.append(Path(found))
    paths.extend([
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
    ])
    return unique_paths(paths)


def find_tool_for_architecture(name: str, architecture: str) -> Path | None:
    for candidate in tool_candidates(name, architecture):
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if architecture in macho_architectures(candidate):
            return candidate
    return None


def copy_arch_tool(source: Path, tools_bin: Path, name: str, architecture: str) -> None:
    destination = tools_bin / architecture / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(0o755)


def write_tool_arch_wrapper(tools_bin: Path, name: str, architectures: tuple[str, ...]) -> None:
    cases = "\n".join(
        f"    {architecture}) exec \"$tool_dir/{architecture}/{name}\" \"$@\" ;;"
        for architecture in architectures
    )
    wrapper = tools_bin / name
    wrapper.write_text(
        f"""#!/bin/sh
set -eu
tool_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
arch="$(/usr/bin/uname -m)"
case "$arch" in
{cases}
esac
echo "{name} is not bundled for architecture $arch" >&2
exit 127
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


def resolve_tool_sources(architectures: tuple[str, ...]) -> dict[tuple[str, str], Path]:
    sources: dict[tuple[str, str], Path] = {}
    missing: list[str] = []

    for tool in REQUIRED_HOST_TOOLS:
        for architecture in architectures:
            source = find_tool_for_architecture(tool, architecture)
            if source is None:
                missing.append(f"{tool} ({architecture})")
                continue
            sources[(tool, architecture)] = source

    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing required host tool(s) for bundling: {joined}")
    return sources


def tool_source_records(sources: dict[tuple[str, str], Path]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for tool, architecture in sorted(sources):
        fingerprint = input_fingerprint(sources[(tool, architecture)])
        records.append({
            "tool": tool,
            "architecture": architecture,
            **fingerprint,
        })
    return records


def copy_tools_from_sources(
    resources_dir: Path,
    architectures: tuple[str, ...],
    sources: dict[tuple[str, str], Path],
) -> None:
    tools_bin = resources_dir / "Tools" / "bin"
    tools_bin.mkdir(parents=True, exist_ok=True)

    if len(architectures) == 1:
        architecture = architectures[0]
        for tool in REQUIRED_HOST_TOOLS:
            source = sources[(tool, architecture)]
            destination = tools_bin / tool
            shutil.copy2(source, destination)
            destination.chmod(0o755)
        return

    for tool in REQUIRED_HOST_TOOLS:
        for architecture in architectures:
            copy_arch_tool(sources[(tool, architecture)], tools_bin, tool, architecture)
        write_tool_arch_wrapper(tools_bin, tool, architectures)


def copy_tools(resources_dir: Path, architectures: tuple[str, ...]) -> None:
    copy_tools_from_sources(resources_dir, architectures, resolve_tool_sources(architectures))


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


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def resolve_macho_dependency(loader: Path, app: Path, dependency: str) -> Path | None:
    if dependency.startswith("@loader_path/"):
        return (loader.parent / dependency.removeprefix("@loader_path/")).resolve()
    if dependency.startswith("@executable_path/"):
        return (app / "Contents" / "MacOS" / dependency.removeprefix("@executable_path/")).resolve()
    if dependency.startswith("/"):
        return Path(dependency).resolve()
    return None


def bundled_dependency_name(source: Path, used_names: set[str], *, preferred_name: str | None = None) -> str:
    name = preferred_name or source.name
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
    framework_binary = path.parent.name.endswith(".framework") and path.name == path.parent.stem
    return path.suffix in {".dylib", ".so"} or framework_binary or path.parent.name in {"lib", "private"}


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
            if path.is_symlink():
                continue
            if path.is_file():
                candidates.append(path)
    return candidates


def is_macho_candidate(path: Path) -> bool:
    if path.suffix == ".a":
        return False
    return path.name == "Python" or path.suffix in {".dylib", ".so"} or os.access(path, os.X_OK)


def macho_files_under(roots: list[Path]) -> list[Path]:
    return [path for path in files_under(roots) if is_macho_candidate(path)]


def macho_vendor_roots(app: Path) -> list[Path]:
    contents = app / "Contents"
    return macho_files_under([
        contents / "Resources" / "Tools" / "bin",
        contents / "Frameworks",
    ])


def macho_validation_roots(app: Path) -> list[Path]:
    contents = app / "Contents"
    return macho_files_under([
        contents / "MacOS",
        contents / "Helpers",
        contents / "Resources" / "Tools" / "bin",
        contents / "Resources" / "Python" / "Runtime",
        contents / "Resources" / "Python" / "site-packages",
        contents / "Frameworks",
    ])


def ad_hoc_codesign(path: Path) -> None:
    run_quiet(["codesign", "--force", "--sign", "-", str(path)])


def nested_helper_code_paths(app: Path) -> list[Path]:
    helper = app / "Contents" / "Helpers" / "tcapsule"
    return [helper] if helper.is_file() else []


def ad_hoc_codesign_app_bundle(app: Path) -> None:
    for path in nested_helper_code_paths(app):
        ad_hoc_codesign(path)
    ad_hoc_codesign(app)


def ad_hoc_codesign_macho_roots(roots: list[Path]) -> None:
    for path in sorted(macho_files_under(roots), key=str):
        if macho_architectures(path):
            ad_hoc_codesign(path)


def ad_hoc_codesign_python_framework(framework: Path) -> None:
    ad_hoc_codesign_macho_roots([framework])
    if framework.is_dir():
        ad_hoc_codesign(framework)


def ad_hoc_codesign_site_packages(site_packages: Path) -> None:
    ad_hoc_codesign_macho_roots([site_packages])


def finalize_python_bundle(resources_dir: Path) -> None:
    framework = resources_dir / "Python" / "Runtime" / PYTHON_FRAMEWORK_NAME
    site_packages = resources_dir / "Python" / "site-packages"
    remove_python_bytecode(framework)
    remove_python_bytecode(site_packages)
    remove_appledouble_files(framework)
    remove_appledouble_files(site_packages)
    if framework.is_dir():
        ad_hoc_codesign_python_framework(framework)
    if site_packages.is_dir():
        ad_hoc_codesign_site_packages(site_packages)
    assert_macho_code_signatures_valid_for_roots([framework, site_packages])


def codesign_order(path: Path, app: Path) -> tuple[int, str]:
    try:
        relative = path.resolve().relative_to(app.resolve())
    except ValueError:
        return (50, str(path))
    parts = relative.parts
    if len(parts) >= 3 and parts[:2] == ("Contents", "MacOS"):
        return (90, str(path))
    if len(parts) >= 2 and parts[:2] == ("Contents", "Frameworks"):
        return (10, str(path))
    return (20, str(path))


def should_codesign_packaged_macho(path: Path, app: Path) -> bool:
    try:
        relative = path.resolve().relative_to(app.resolve())
    except ValueError:
        return False
    parts = relative.parts
    return len(parts) >= 2 and parts[:2] in {
        ("Contents", "Frameworks"),
        ("Contents", "Resources"),
    }


def ad_hoc_codesign_macho_bundle(app: Path) -> None:
    for path in sorted(macho_validation_roots(app), key=lambda candidate: codesign_order(candidate, app)):
        if should_codesign_packaged_macho(path, app) and macho_architectures(path):
            ad_hoc_codesign(path)
    framework = bundled_python_framework(app)
    if framework.is_dir():
        ad_hoc_codesign(framework)


def vendor_macho_dependencies(app: Path) -> set[Path]:
    frameworks_dir = app / "Contents" / "Frameworks"
    frameworks_dir.mkdir(exist_ok=True)
    source_to_bundle: dict[Path, Path] = {}
    bundle_to_source: dict[Path, Path] = {}
    vendored_sources: set[Path] = set()
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
            preferred_name: str
            if is_external_macho_dependency(dependency):
                source_path = Path(dependency)
                source = source_path.resolve()
                preferred_name = source_path.name
            elif dependency.startswith("@loader_path/") and current_resolved in bundle_to_source:
                relative_dependency = dependency.removeprefix("@loader_path/")
                source = (bundle_to_source[current_resolved].parent / relative_dependency).resolve()
                preferred_name = Path(relative_dependency).name
                if not source.is_file():
                    resolved_dependency = resolve_macho_dependency(current, app, dependency)
                    if resolved_dependency is None or not resolved_dependency.exists():
                        raise RuntimeError(f"Mach-O dependency does not exist: {dependency} referenced by {current}")
                    continue
            else:
                continue
            if not source.is_file():
                raise RuntimeError(f"Mach-O dependency does not exist: {dependency} referenced by {current}")
            vendored_sources.add(source)
            bundled = source_to_bundle.get(source)
            if bundled is None:
                bundled = frameworks_dir / bundled_dependency_name(source, used_names, preferred_name=preferred_name)
                shutil.copy2(source, bundled)
                bundled.chmod(bundled.stat().st_mode | 0o200)
                source_to_bundle[source] = bundled
                bundle_to_source[bundled.resolve()] = source
                queue.append(bundled)
            run_quiet([
                "install_name_tool",
                "-change",
                dependency,
                loader_path_reference(current, bundled, frameworks_dir),
                str(current),
            ])

        set_macho_id_if_supported(current)
    return vendored_sources


def native_tools_cache_entry(
    architectures: tuple[str, ...],
    sources: dict[tuple[str, str], Path],
) -> Path:
    key = cache_key({
        "kind": "native-tools",
        "version": NATIVE_TOOLS_CACHE_VERSION,
        "architectures": architectures,
        "tool_sources": tool_source_records(sources),
    })
    return package_cache_dir("native-tools") / key


def native_tools_cache_is_complete(entry: Path) -> bool:
    return native_tools_cache_miss_reason(entry) is None


def native_tools_cache_miss_reason(entry: Path) -> str | None:
    tools_bin = entry / "Contents" / "Resources" / "Tools" / "bin"
    frameworks = entry / "Contents" / "Frameworks"
    contents = entry / "Contents"
    if not cache_is_complete(entry, tools_bin):
        return "cache entry is incomplete or missing bundled tools"
    if not frameworks.is_dir():
        return f"cached Frameworks directory is missing: {frameworks}"
    if reason := cache_manifest_inputs_miss_reason(entry):
        return reason
    if reason := cache_manifest_output_miss_reason(entry, contents):
        return reason
    return None


def write_native_tools_manifest(
    entry: Path,
    architectures: tuple[str, ...],
    sources: dict[tuple[str, str], Path],
    dependency_sources: set[Path],
) -> None:
    input_paths = set(sources.values()) | dependency_sources
    write_cache_manifest(entry, {
        "schema_version": 1,
        "kind": "native-tools",
        "cache_version": NATIVE_TOOLS_CACHE_VERSION,
        "architectures": list(architectures),
        "tool_sources": tool_source_records(sources),
        "inputs": input_fingerprints(input_paths),
        "output_tree_sha256": sha256_tree(entry / "Contents"),
    })


def prepared_native_tools_layer(architectures: tuple[str, ...]) -> Path:
    sources = resolve_tool_sources(architectures)
    entry = native_tools_cache_entry(architectures, sources)
    miss_reason = native_tools_cache_miss_reason(entry)
    if miss_reason is None:
        print("Using cached native tool layer.", file=sys.stderr)
        return entry
    print(f"Rebuilding native tool layer: {miss_reason}", file=sys.stderr)

    cache_root = entry.parent
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{entry.name}.tmp-", dir=cache_root) as tmp:
        staging = Path(tmp) / "entry"
        resources = staging / "Contents" / "Resources"
        resources.mkdir(parents=True)
        copy_tools_from_sources(resources, architectures, sources)
        dependency_sources = vendor_macho_dependencies(staging)
        remove_appledouble_files(staging)
        ad_hoc_codesign_macho_bundle(staging)
        assert_tool_architectures(staging, architectures)
        assert_runtime_macho_architectures(staging, architectures)
        assert_no_external_macho_dependencies(staging)
        assert_macho_code_signatures_valid(staging)
        write_native_tools_manifest(staging, architectures, sources, dependency_sources)
        mark_cache_complete(staging)
        replace_path(staging, entry)
    return entry


def copy_native_tools_layer(app: Path, architectures: tuple[str, ...], *, use_cache: bool = True) -> None:
    contents = app / "Contents"
    if use_cache:
        layer = prepared_native_tools_layer(architectures)
        copy_path(layer / "Contents" / "Resources" / "Tools", contents / "Resources" / "Tools")
        copy_path(layer / "Contents" / "Frameworks", contents / "Frameworks")
        return

    copy_tools(contents / "Resources", architectures)
    vendor_macho_dependencies(app)
    remove_appledouble_files(app)
    ad_hoc_codesign_macho_bundle(app)
    assert_tool_architectures(app, architectures)
    assert_runtime_macho_architectures(app, architectures)
    assert_no_external_macho_dependencies(app)
    assert_macho_code_signatures_valid(app)


def assert_macho_code_signatures_valid_for_paths(paths: list[Path]) -> None:
    failures: list[str] = []
    for path in paths:
        if not macho_architectures(path):
            continue
        completed = subprocess.run(
            ["codesign", "--verify", "--verbose=4", str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            reason = detail[-1] if detail else f"codesign verification failed with rc={completed.returncode}"
            failures.append(f"{path}: {reason}")
    if failures:
        joined = "\n  - ".join(failures)
        raise RuntimeError(f"App bundle contains invalid Mach-O code signature(s):\n  - {joined}")


def assert_macho_code_signatures_valid_for_roots(roots: list[Path]) -> None:
    assert_macho_code_signatures_valid_for_paths(macho_files_under(roots))


def assert_macho_code_signatures_valid(app: Path) -> None:
    paths = [
        path
        for path in macho_validation_roots(app)
        if should_codesign_packaged_macho(path, app)
    ]
    assert_macho_code_signatures_valid_for_paths(paths)


def assert_app_bundle_signature_valid(app: Path) -> None:
    completed = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=4", str(app)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "App bundle code signature verification failed"
            + (f":\n{detail}" if detail else f" with rc={completed.returncode}")
        )


def assert_no_external_macho_dependencies_for_paths(paths: list[Path]) -> None:
    external: list[str] = []
    for path in paths:
        dependencies = macho_dependencies(path)
        if dependencies is None:
            continue
        for dependency in dependencies:
            if is_external_macho_dependency(dependency):
                external.append(f"{path}: {dependency}")
    if external:
        joined = "\n  - ".join(external)
        raise RuntimeError(f"App bundle contains non-system Mach-O dependency reference(s):\n  - {joined}")


def assert_no_external_macho_dependencies_for_roots(roots: list[Path]) -> None:
    assert_no_external_macho_dependencies_for_paths(macho_files_under(roots))


def assert_no_external_macho_dependencies(app: Path) -> None:
    assert_no_external_macho_dependencies_for_paths(macho_validation_roots(app))


def assert_python_dependencies_are_bundled(app: Path) -> None:
    site_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    python_home = bundled_python_home(app)
    env = python_subprocess_env(
        {"PYTHONPATH": str(site_packages)},
        python_home=python_home,
    )
    code = (
        "from pathlib import Path\n"
        "import certifi, Crypto, ifaddr, pexpect, timecapsulesmb, zeroconf, zopfli.gzip\n"
        f"site = Path({str(site_packages)!r}).resolve()\n"
        "paths = [certifi.__file__, Crypto.__file__, ifaddr.__file__, pexpect.__file__, timecapsulesmb.__file__, zeroconf.__file__, zopfli.__file__]\n"
        "bad = [p for p in paths if not p or not Path(p).resolve().is_relative_to(site)]\n"
        "raise SystemExit('\\n'.join(bad) if bad else 0)\n"
    )
    completed = subprocess.run(
        [str(bundled_python_executable(app)), "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Bundled Python dependencies are not importable from the app package:\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


def bundled_ca_certificate_path(app: Path) -> Path:
    return app / "Contents" / "Resources" / "Python" / "site-packages" / "certifi" / "cacert.pem"


def assert_bundled_ca_certificates(app: Path) -> None:
    ca_certificates = bundled_ca_certificate_path(app)
    if not ca_certificates.is_file():
        raise RuntimeError(f"App bundle is missing bundled CA certificates: {ca_certificates}")


def assert_macho_has_architectures(path: Path, architectures: tuple[str, ...], label: str) -> None:
    actual = macho_architectures(path)
    missing = [architecture for architecture in architectures if architecture not in actual]
    if missing:
        raise RuntimeError(
            f"{label} is missing architecture(s) {', '.join(missing)}: {path} "
            f"(found: {', '.join(sorted(actual)) or 'none'})"
        )


def assert_python_extension_architectures(app: Path, architectures: tuple[str, ...]) -> None:
    site_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    failures: list[str] = []
    for path in site_packages.rglob("*"):
        if not path.is_file() or path.suffix not in {".so", ".dylib"}:
            continue
        actual = macho_architectures(path)
        missing = [architecture for architecture in architectures if architecture not in actual]
        if missing:
            failures.append(f"{path}: missing {', '.join(missing)} (found: {', '.join(sorted(actual)) or 'none'})")
    if failures:
        joined = "\n  - ".join(failures)
        raise RuntimeError(f"Bundled Python extension(s) are missing required architecture(s):\n  - {joined}")


def assert_tool_architectures(app: Path, architectures: tuple[str, ...]) -> None:
    tools_bin = app / "Contents" / "Resources" / "Tools" / "bin"
    failures: list[str] = []
    for tool in REQUIRED_HOST_TOOLS:
        if len(architectures) == 1:
            candidate = tools_bin / tool
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                failures.append(f"{candidate}: missing")
                continue
            if candidate.is_file() and macho_architectures(candidate):
                missing = [architecture for architecture in architectures if architecture not in macho_architectures(candidate)]
                if missing:
                    failures.append(f"{candidate}: missing {', '.join(missing)}")
            continue

        wrapper = tools_bin / tool
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            failures.append(f"{wrapper}: missing architecture dispatch wrapper")
        for architecture in architectures:
            candidate = tools_bin / architecture / tool
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                failures.append(f"{candidate}: missing")
                continue
            if architecture not in macho_architectures(candidate):
                failures.append(
                    f"{candidate}: missing {architecture} "
                    f"(found: {', '.join(sorted(macho_architectures(candidate))) or 'none'})"
                )
    if failures:
        joined = "\n  - ".join(failures)
        raise RuntimeError(f"Bundled tool architecture validation failed:\n  - {joined}")


def assert_macho_architectures_for_roots(roots: list[Path], architectures: tuple[str, ...], label: str) -> None:
    failures: list[str] = []
    for path in macho_files_under(roots):
        actual = macho_architectures(path)
        if not actual:
            continue
        missing = [architecture for architecture in architectures if architecture not in actual]
        if missing:
            failures.append(f"{path}: missing {', '.join(missing)} (found: {', '.join(sorted(actual)) or 'none'})")
    if failures:
        joined = "\n  - ".join(failures)
        raise RuntimeError(f"{label} validation failed:\n  - {joined}")


def runtime_architecture_roots(app: Path, architectures: tuple[str, ...]) -> list[tuple[Path, str]]:
    contents = app / "Contents"
    roots: list[tuple[Path, str]] = []
    executable = contents / "MacOS" / PRODUCT_NAME
    helper = contents / "Helpers" / HELPER_PRODUCT_NAME
    roots.extend((executable, architecture) for architecture in architectures)
    roots.extend((helper, architecture) for architecture in architectures)
    python_runtime = contents / "Resources" / "Python" / "Runtime"
    roots.extend(
        (path, architecture)
        for path in macho_files_under([python_runtime])
        for architecture in architectures
    )

    site_packages = contents / "Resources" / "Python" / "site-packages"
    if site_packages.is_dir():
        for path in site_packages.rglob("*"):
            if path.is_file() and path.suffix in {".so", ".dylib"}:
                roots.extend((path, architecture) for architecture in architectures)

    tools_bin = contents / "Resources" / "Tools" / "bin"
    for tool in REQUIRED_HOST_TOOLS:
        if len(architectures) == 1:
            roots.append((tools_bin / tool, architectures[0]))
            continue
        roots.extend((tools_bin / architecture / tool, architecture) for architecture in architectures)
    return roots


def assert_runtime_macho_architectures(app: Path, architectures: tuple[str, ...]) -> None:
    failures: list[str] = []
    queue = runtime_architecture_roots(app, architectures)
    visited: set[tuple[Path, str]] = set()

    while queue:
        path, architecture = queue.pop(0)
        resolved_path = path.resolve()
        key = (resolved_path, architecture)
        if key in visited:
            continue
        visited.add(key)

        actual = macho_architectures(path)
        if actual and architecture not in actual:
            failures.append(f"{path}: missing {architecture} (found: {', '.join(sorted(actual)) or 'none'})")
            continue

        dependencies = macho_dependencies(path)
        if dependencies is None:
            continue
        for dependency in dependencies:
            dependency_path = resolve_macho_dependency(path, app, dependency)
            if dependency_path is None:
                if is_system_macho_dependency(dependency):
                    continue
                continue
            if not is_inside(dependency_path, app):
                continue
            if not dependency_path.is_file():
                failures.append(f"{path}: missing bundled dependency {dependency} -> {dependency_path}")
                continue
            queue.append((dependency_path, architecture))

    if failures:
        joined = "\n  - ".join(failures)
        raise RuntimeError(f"Bundled Mach-O runtime architecture validation failed:\n  - {joined}")


def validate_app_resources(app: Path) -> None:
    executable = app / "Contents" / "MacOS" / PRODUCT_NAME
    completed = subprocess.run(
        [str(executable), "--validate-resources"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "App executable resource validation failed:\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


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


def assert_bundle_layout(
    app: Path,
    *,
    icon_name: str | None = None,
    architectures: tuple[str, ...] = (),
    full_validation: bool = False,
) -> None:
    executable = app / "Contents" / "MacOS" / PRODUCT_NAME
    helper = app / "Contents" / "Helpers" / "tcapsule"
    python_executable = bundled_python_executable(app)
    python_dylib = bundled_python_dylib(app)
    info_plist = app / "Contents" / "Info.plist"
    resource_bundle = app / "Contents" / "Resources" / RESOURCE_BUNDLE_NAME
    distribution = app / "Contents" / "Resources" / "Distribution"
    artifact_manifest = distribution / "artifact-manifest.json"
    tools_bin = app / "Contents" / "Resources" / "Tools" / "bin"
    python_packages = app / "Contents" / "Resources" / "Python" / "site-packages"
    assert_no_appledouble_files(app)
    required_executables = [executable, helper, python_executable]
    missing_executables = [path for path in required_executables if not path.is_file() or not os.access(path, os.X_OK)]
    if missing_executables:
        joined = "\n  - ".join(str(path) for path in missing_executables)
        raise RuntimeError(f"App bundle is missing required executable(s):\n  - {joined}")
    if not python_dylib.is_file():
        raise RuntimeError(f"App bundle is missing bundled Python framework: {python_dylib}")
    if architectures:
        assert_macho_has_architectures(executable, architectures, "App executable")
        assert_macho_has_architectures(helper, architectures, "Helper executable")
        assert_macho_has_architectures(python_executable, architectures, "Bundled Python executable")
        assert_macho_has_architectures(python_dylib, architectures, "Bundled Python framework")
        assert_python_extension_architectures(app, architectures)
        assert_tool_architectures(app, architectures)
        if full_validation:
            assert_runtime_macho_architectures(app, architectures)
    if not (resource_bundle / "en.lproj" / "Localizable.strings").is_file():
        raise RuntimeError(f"App bundle is missing Swift resource bundle localizations: {resource_bundle}")
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
    assert_bundled_ca_certificates(app)
    assert_python_dependencies_are_bundled(app)
    if full_validation:
        assert_no_external_macho_dependencies(app)
        assert_macho_code_signatures_valid(app)
        assert_app_bundle_signature_valid(app)
    validate_app_resources(app)


def smoke_test(app: Path) -> None:
    helper = app / "Contents" / "Helpers" / "tcapsule"
    with tempfile.TemporaryDirectory(prefix="timecapsulesmb-package-smoke-") as tmp:
        state_dir = Path(tmp)
        smoke_request(helper, "capabilities", state_dir)
        smoke_request(helper, "validate-install", state_dir)


def validate_app_zip(zip_path: Path, app_name: str) -> None:
    with tempfile.TemporaryDirectory(prefix="timecapsulesmb-package-zip-") as tmp:
        extract_dir = Path(tmp)
        run(["unzip", "-q", str(zip_path), "-d", str(extract_dir)])
        extracted_app = extract_dir / app_name
        if not extracted_app.is_dir():
            raise RuntimeError(f"App zip did not extract {app_name}: {zip_path}")
        assert_no_appledouble_files(extract_dir)
        assert_app_bundle_signature_valid(extracted_app)


def create_app_zip(app: Path, zip_path: Path) -> None:
    assert_no_appledouble_files(app)
    if zip_path.exists():
        zip_path.unlink()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ditto",
        "-c",
        "-k",
        "--keepParent",
        "--norsrc",
        "--noextattr",
        "--noacl",
        "--noqtn",
        str(app),
        str(zip_path),
    ])
    validate_app_zip(zip_path, app.name)


def package_app(args: argparse.Namespace) -> Path:
    architectures = resolve_architectures(args.arch)
    executable, resource_build_dir = build_swift(args.configuration, architectures)
    helper_executable = build_helper(args.configuration, architectures)
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
    (macos / PRODUCT_NAME).chmod(0o755)
    copy_resources(resource_build_dir, resources)
    if args.icon:
        create_app_icon(args.icon.resolve(), resources, use_cache=not args.no_cache)
    copy_helper_executable(helper_executable, helpers / HELPER_PRODUCT_NAME)
    python_executable = copy_python_runtime(args, resources, architectures)
    create_python_packages(str(python_executable), resources, architectures, use_cache=not args.no_cache)
    finalize_python_bundle(resources)
    copy_distribution(resources)
    copy_native_tools_layer(app, architectures, use_cache=not args.no_cache)
    remove_appledouble_files(app)
    assert_no_appledouble_files(app)
    ad_hoc_codesign_app_bundle(app)
    assert_app_bundle_signature_valid(app)
    assert_bundle_layout(
        app,
        icon_name=icon_name,
        architectures=architectures,
        full_validation=args.full_validation,
    )

    if not args.skip_smoke:
        smoke_test(app)
    if getattr(args, "zip", False) or getattr(args, "zip_output", None):
        zip_path = (args.zip_output or (output_dir / f"{APP_NAME}.app.zip")).resolve()
        create_app_zip(app, zip_path)
    return app


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a self-contained TimeCapsuleSMB.app bundle.")
    parser.add_argument("--output", type=Path, default=PACKAGE_ROOT / "dist", help="Directory that will receive TimeCapsuleSMB.app.")
    parser.add_argument("--configuration", choices=("debug", "release"), default="release", help="Swift build configuration.")
    parser.add_argument(
        "--arch",
        action="append",
        choices=("universal", "native", "arm64", "x86_64"),
        help="Architecture to build; repeat for multiple architectures. Defaults to universal.",
    )
    parser.add_argument(
        "--icon",
        type=Path,
        default=DEFAULT_ICON_SOURCE,
        help="Source image to convert into the app bundle .icns icon.",
    )
    parser.add_argument(
        "--python-runtime-framework",
        type=Path,
        default=Path(os.environ["TCAPSULE_PACKAGE_PYTHON_FRAMEWORK"]) if os.getenv("TCAPSULE_PACKAGE_PYTHON_FRAMEWORK") else None,
        help="Existing universal Python.framework to copy into the app bundle.",
    )
    parser.add_argument(
        "--python-runtime-pkg",
        type=Path,
        default=Path(os.environ["TCAPSULE_PACKAGE_PYTHON_PKG"]) if os.getenv("TCAPSULE_PACKAGE_PYTHON_PKG") else None,
        help="Universal python.org macOS installer package to extract into the app bundle.",
    )
    parser.add_argument(
        "--python-runtime-url",
        default=os.getenv("TCAPSULE_PACKAGE_PYTHON_URL", PYTHON_RUNTIME_URL),
        help="Universal python.org macOS installer URL used when no local runtime source is provided.",
    )
    parser.add_argument("--skip-smoke", action="store_true", help="Skip bundled helper capabilities and validate-install smoke tests.")
    parser.add_argument("--no-cache", action="store_true", help="Rebuild package-only cached artifacts instead of reusing them.")
    parser.add_argument("--full-validation", action="store_true", help="Run the full Mach-O dependency and signature validation pass even for trusted cached layers.")
    parser.add_argument("--zip", action="store_true", help="Create and validate TimeCapsuleSMB.app.zip next to the app bundle.")
    parser.add_argument("--zip-output", type=Path, help="Zip output path; implies --zip.")
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
