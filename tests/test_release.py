from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from timecapsulesmb.core.release import CLI_VERSION, CLI_VERSION_CODE, RELEASE_TAG
from timecapsulesmb.services.version_check import DEFAULT_DOWNLOAD_URL, parse_version_metadata


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK_RELEASE_SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release.py"
PASSING_LINES = [
    "PASS tag: v4.1.0 is release.py's tag",
    "PASS version code: 40100 is above the newest release's 40000",
]


@pytest.fixture(autouse=True)
def outside_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under GitHub Actions these point at the real step; a test must never
    # write to that step's outputs or depend on how CI runs it.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def github_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    return path


def load_check_release_module():
    spec = importlib.util.spec_from_file_location("check_release", CHECK_RELEASE_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_source(*, version: str = '"4.1.0"', code: str = "40100") -> str:
    return (
        "from __future__ import annotations\n\n"
        f"CLI_VERSION = {version}\n"
        'RELEASE_TAG = f"v{CLI_VERSION}"\n'
        f"CLI_VERSION_CODE = {code}\n"
    )


def run_check(tmp_path: Path, tag: str, *, current_source: str | None = None,
              newest_source: str = "CLI_VERSION_CODE = 40000\n") -> int:
    current = tmp_path / "release.py"
    current.write_text(release_source() if current_source is None else current_source, encoding="utf-8")
    newest = tmp_path / "newest_release.py"
    newest.write_text(newest_source, encoding="utf-8")
    module = load_check_release_module()
    return module.main([tag, "--newest-release-py", str(newest), "--release-py", str(current)])


def test_release_tag_is_derived_from_cli_version() -> None:
    # check_release.py compares the pushed tag with "v" + CLI_VERSION; deploy,
    # telemetry and doctor use RELEASE_TAG. A hand-typed tag would split them.
    assert RELEASE_TAG == f"v{CLI_VERSION}"


def test_checked_in_version_json_is_valid_for_clients() -> None:
    # The parser returns None, which clients take as "never block", once the
    # minimum passes the current version; old apps follow download_url.
    payload = json.loads((REPO_ROOT / "version.json").read_text(encoding="utf-8"))

    assert parse_version_metadata(payload) is not None
    assert payload["download_url"] == DEFAULT_DOWNLOAD_URL


def test_release_above_the_newest_passes_and_is_not_a_prerelease(
    tmp_path: Path, github_output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_check(tmp_path, "v4.1.0") == 0

    assert capsys.readouterr().out.splitlines() == PASSING_LINES
    assert github_output.read_text(encoding="utf-8") == "prerelease=false\n"


def test_hidden_build_above_the_newest_passes_as_a_prerelease(
    tmp_path: Path, github_output: Path
) -> None:
    source = release_source(version='"4.1.0-1"', code="40101")

    assert run_check(tmp_path, "v4.1.0-1", current_source=source) == 0

    assert github_output.read_text(encoding="utf-8") == "prerelease=true\n"


def test_output_is_appended_to_what_earlier_steps_wrote(tmp_path: Path, github_output: Path) -> None:
    github_output.write_text("earlier=1\n", encoding="utf-8")

    assert run_check(tmp_path, "v4.1.0") == 0

    assert github_output.read_text(encoding="utf-8") == "earlier=1\nprerelease=false\n"


def test_tag_that_is_not_release_py_tag_fails(
    tmp_path: Path, github_output: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_check(tmp_path, "v4.1.1") == 1

    # The other check still runs, so one run reports every problem.
    assert capsys.readouterr().out.splitlines() == [
        "FAIL tag: v4.1.1 is not release.py's tag v4.1.0",
        "PASS version code: 40100 is above the newest release's 40000",
    ]
    assert not github_output.exists()


@pytest.mark.parametrize("code", ["40000", "39999"])
def test_code_not_above_the_newest_fails(
    tmp_path: Path, github_output: Path, capsys: pytest.CaptureFixture[str], code: str
) -> None:
    assert run_check(tmp_path, "v4.1.0", current_source=release_source(code=code)) == 1

    assert f"FAIL version code: {code} is not above the newest release's 40000" in capsys.readouterr().out
    assert not github_output.exists()


@pytest.mark.parametrize(
    ("newest_source", "reason"),
    [
        ('CLI_VERSION = "4.0.0"\n', "CLI_VERSION_CODE is not assigned"),
        ('CLI_VERSION_CODE = "40000"\n', "CLI_VERSION_CODE is '40000', not int"),
        ("CLI_VERSION_CODE = True\n", "CLI_VERSION_CODE is True, not int"),
        ('CLI_VERSION_CODE = int("40000")\n', "CLI_VERSION_CODE is not a literal"),
        ("CLI_VERSION_CODE = OTHER\n", "CLI_VERSION_CODE is not a literal"),
        ("CLI_VERSION_CODE = (\n", "newest_release.py"),
    ],
)
def test_unreadable_newest_release_code_fails(
    tmp_path: Path, github_output: Path, capsys: pytest.CaptureFixture[str], newest_source: str, reason: str
) -> None:
    assert run_check(tmp_path, "v4.1.0", newest_source=newest_source) == 1

    out = capsys.readouterr().out
    assert "FAIL version code: newest release:" in out
    assert reason in out
    assert not github_output.exists()


@pytest.mark.parametrize(
    ("newest_source", "passes"),
    [
        # Python keeps the last assignment, so the check must read the same value.
        ("CLI_VERSION_CODE = 50000\nCLI_VERSION_CODE = 40000\n", True),
        ("CLI_VERSION_CODE = 40000\nCLI_VERSION_CODE = 50000\n", False),
        ("CLI_VERSION_CODE: int = 40000\n", True),
        ("CLI_VERSION_CODE: int\nCLI_VERSION_CODE = 40000\n", True),
    ],
)
def test_newest_release_code_is_the_value_python_would_see(
    tmp_path: Path, newest_source: str, passes: bool
) -> None:
    assert run_check(tmp_path, "v4.1.0", newest_source=newest_source) == (0 if passes else 1)


def test_assignments_inside_blocks_do_not_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = "if False:\n    CLI_VERSION_CODE = 40000\n"

    assert run_check(tmp_path, "v4.1.0", newest_source=source) == 1

    assert "CLI_VERSION_CODE is not assigned" in capsys.readouterr().out


def test_newest_release_file_is_never_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    source = f'CLI_VERSION_CODE = __import__("pathlib").Path({str(marker)!r}).touch()\n'

    assert run_check(tmp_path, "v4.1.0", newest_source=source) == 1

    assert not marker.exists()


def test_missing_newest_release_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    current = tmp_path / "release.py"
    current.write_text(release_source(), encoding="utf-8")
    module = load_check_release_module()

    assert module.main(["v4.1.0", "--newest-release-py", str(tmp_path / "absent.py"), "--release-py", str(current)]) == 1

    assert "FAIL version code: newest release:" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("current_source", "reason"),
    [
        (release_source(code='"40100"'), "CLI_VERSION_CODE is '40100', not int"),
        (release_source(version="4.1"), "CLI_VERSION is 4.1, not str"),
        ("CLI_VERSION_CODE = 40100\n", "CLI_VERSION is not assigned"),
        ('CLI_VERSION = "4.1.0"\n', "CLI_VERSION_CODE is not assigned"),
    ],
)
def test_unreadable_release_py_fails_before_any_check(
    tmp_path: Path, github_output: Path, capsys: pytest.CaptureFixture[str], current_source: str, reason: str
) -> None:
    assert run_check(tmp_path, "v4.1.0", current_source=current_source) == 1

    assert capsys.readouterr().out.splitlines() == [
        f"FAIL release.py: {tmp_path / 'release.py'}: {reason}",
    ]
    assert not github_output.exists()


def test_failures_are_annotated_under_github_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert run_check(tmp_path, "v4.1.1") == 1

    assert capsys.readouterr().out.splitlines() == [
        "FAIL tag: v4.1.1 is not release.py's tag v4.1.0",
        "::error title=Release check::tag: v4.1.1 is not release.py's tag v4.1.0",
        "PASS version code: 40100 is above the newest release's 40000",
    ]


def test_annotations_encode_what_workflow_commands_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A literal "%0A" in a message would otherwise become a line break, and a
    # real line break would end the annotation early.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert run_check(tmp_path, "v4.1.0%0A\r\n::warning::x") == 1

    annotations = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::error")]
    assert annotations == [
        "::error title=Release check::tag: v4.1.0%250A%0D%0A::warning::x is not release.py's tag v4.1.0",
    ]


def test_passing_checks_are_not_annotated_under_github_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert run_check(tmp_path, "v4.1.0") == 0

    assert capsys.readouterr().out.splitlines() == PASSING_LINES


def test_failures_are_not_annotated_outside_github_actions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_check(tmp_path, "v4.1.1") == 1

    assert "::error" not in capsys.readouterr().out


def test_without_github_output_nothing_is_written(tmp_path: Path) -> None:
    assert run_check(tmp_path, "v4.1.0") == 0

    assert sorted(path.name for path in tmp_path.iterdir()) == ["newest_release.py", "release.py"]


def test_default_release_py_is_this_checkout(tmp_path: Path, github_output: Path) -> None:
    newest = tmp_path / "newest_release.py"
    newest.write_text(f"CLI_VERSION_CODE = {CLI_VERSION_CODE - 1}\n", encoding="utf-8")
    module = load_check_release_module()

    assert module.main([f"v{CLI_VERSION}", "--newest-release-py", str(newest)]) == 0

    expected = "true" if "-" in CLI_VERSION else "false"
    assert github_output.read_text(encoding="utf-8") == f"prerelease={expected}\n"
