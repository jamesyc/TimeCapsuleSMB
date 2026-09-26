#!/usr/bin/env python3
"""Edit the Samba 4.x patch series through a replay repository.

The files in build/patches/samba4x/*.patch are generated, never hand-edited.
A replay repository holds pristine Samba as commit "base", the overlay files
as commit "overlay", then one commit per `series` entry whose subject is the
patch file name:

  samba4x_replay.py init   REPLAY          clone the pinned Samba ref and replay the series
  samba4x_replay.py amend  REPLAY SUBJECT EDIT.py
                                           change what one commit produces, replay the rest
  samba4x_replay.py export REPLAY          write the patches and overlay back to the repo
  samba4x_replay.py verify REPLAY          apply the series like the build; trees must match

EDIT.py defines FILES (repo-relative paths) and edit(path, text) returning the
new text, or None to delete the file. Text is read and written without newline
translation, and bytes that are not UTF-8 survive unchanged. A path that does
not exist yet is passed as "".

To add a patch, stop the replay at the right place with `git rebase -i`, commit
the change with the new patch file name as its subject, list it in `series`,
then export. `--root` points at another checkout (the tests use it).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_DIR = Path("build/patches/samba4x")
PATCH_NAME = re.compile(r"^[0-9]{4}-[A-Za-z0-9._-]+\.patch$")
# Pinned in the replay's own config so later commands can tell a replay made
# from another Samba version.
REF_KEY = "tc-replay.ref"
# Hashes of the repo's patch and overlay files as the replay last saw them.
# export refuses when they changed since, instead of overwriting another
# session's edits with a stale replay.
INPUTS_FILE = "tc-replay-inputs.json"
# Replay-local settings that keep a user's global git config out of commits
# and diffs: signing, hooks, line-ending conversion and diff prefixes.
LOCAL_CONFIG = {
    "user.name": "replay",
    "user.email": "replay@localhost",
    "commit.gpgsign": "false",
    "core.hooksPath": "/dev/null",
    "core.autocrlf": "false",
    "core.safecrlf": "false",
}
DIFF_ARGS = ["diff", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv",
             "--src-prefix=a/", "--dst-prefix=b/", "--diff-algorithm=myers", "-U3"]


class ReplayError(Exception):
    pass


def git(repo: Path, *args: str, input: bytes | None = None, check: bool = True,
        env: dict[str, str] | None = None) -> bytes:
    p = subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True,
                       env=env)
    if check and p.returncode != 0:
        raise ReplayError(f"git {' '.join(args)} failed:\n"
                          f"{(p.stdout + p.stderr).decode(errors='replace').strip()}")
    return p.stdout


def git_text(repo: Path, *args: str) -> str:
    return git(repo, *args).decode().strip()


def series_names(root: Path) -> list[str]:
    names = []
    for line in (root / PATCH_DIR / "series").read_text().splitlines():
        if line and not line.startswith("#"):
            names.append(line.split("|", 1)[0])
    return names


def pinned_samba(root: Path) -> tuple[str, str]:
    """The build's Samba URL and ref, as build/env.sh resolves them."""
    env_sh = str(root / "build/env.sh")
    out = subprocess.run(
        ["sh", "-c", '. "$1"; printf "%s\\n%s\\n" "$SAMBA4X_GIT_URL" "$SAMBA4X_GIT_REF"',
         "sh", env_sh], env=dict(os.environ, TC_ENV_FILE="/dev/null"),
        capture_output=True, text=True, check=True).stdout.splitlines()
    return out[0], out[1]


def overlay_files(root: Path) -> list[str]:
    """Overlay paths as patch_copy_overlay copies them: regular files, no dotfiles."""
    overlay = root / PATCH_DIR / "overlay"
    if not overlay.is_dir():
        return []
    return sorted(str(p.relative_to(overlay)) for p in overlay.rglob("*")
                  if p.is_file() and not p.is_symlink() and not p.name.startswith("."))


def input_hashes(root: Path) -> dict[str, str]:
    files = [PATCH_DIR / "overlay" / f for f in overlay_files(root)]
    files += sorted(p.relative_to(root) for p in (root / PATCH_DIR).glob("*.patch"))
    return {str(f): hashlib.sha256((root / f).read_bytes()).hexdigest() for f in files}


def configure(repo: Path) -> None:
    for key, value in LOCAL_CONFIG.items():
        git(repo, "config", key, value)


def git_dir(replay: Path) -> Path:
    return Path(git_text(replay, "rev-parse", "--absolute-git-dir"))


def save_inputs(root: Path, replay: Path) -> None:
    (git_dir(replay) / INPUTS_FILE).write_text(json.dumps(input_hashes(root), indent=1))


def commit(replay: Path, subject: str) -> None:
    git(replay, "commit", "-q", "--allow-empty", "--no-verify", "-m", subject)


def subjects(replay: Path) -> list[tuple[str, str]]:
    """(commit, subject) from base to HEAD."""
    out = git_text(replay, "log", "--reverse", "--format=%H %s", "HEAD")
    pairs = [tuple(line.split(" ", 1)) for line in out.splitlines()]
    if [s for _, s in pairs[:2]] != ["base", "overlay"]:
        raise ReplayError("not a replay repository: the first commits must be base and overlay")
    return pairs


def check_ref(root: Path, replay: Path) -> None:
    recorded = git(replay, "config", "--get", REF_KEY, check=False).decode().strip()
    _, ref = pinned_samba(root)
    if recorded != ref:
        raise ReplayError(f"the replay was made from Samba {recorded or '(unknown)'}, "
                          f"but build/env.sh pins {ref}; run init again")


def init(root: Path, replay: Path) -> None:
    if replay.exists():
        raise ReplayError(f"{replay} already exists")
    try:
        replay_series(root, replay)
    except BaseException:
        # A half-built replay would only make the next init refuse.
        shutil.rmtree(replay, ignore_errors=True)
        raise


def replay_series(root: Path, replay: Path) -> None:
    url, ref = pinned_samba(root)
    # Check out the files byte for byte, whatever the user's global config says.
    p = subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", "--depth", "1",
                        "--branch", ref, url, str(replay)], capture_output=True, text=True)
    if p.returncode != 0:
        raise ReplayError(f"cannot clone Samba {ref} from {url}:\n{p.stderr.strip()}")
    # Start a history of our own: base is the pinned upstream tree as checked out.
    shutil.rmtree(replay / ".git")
    git(replay, "init", "-q", "-b", "main")
    configure(replay)
    git(replay, "config", REF_KEY, ref)
    # -f: pristine files that Samba's .gitignore matches are still Samba's.
    git(replay, "add", "-A", "-f")
    commit(replay, "base")

    overlay = root / PATCH_DIR / "overlay"
    names = overlay_files(root)
    for name in names:
        if (replay / name).exists():
            raise ReplayError(f"overlay file {name} already exists in Samba")
    for name in names:
        dst = replay / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(overlay / name, dst)
    if names:
        git(replay, "add", "-f", "--", *names)
    commit(replay, "overlay")

    for name in series_names(root):
        if not PATCH_NAME.match(name):
            raise ReplayError(f"series entry {name!r} is not a patch file name")
        # --index stages exactly the patch's paths, ignored or not.
        git(replay, "apply", "--index", str(root / PATCH_DIR / name))
        commit(replay, name)
    save_inputs(root, replay)
    print(f"replayed {len(series_names(root))} patches onto Samba {ref}")


def rebase_in_progress(replay: Path) -> bool:
    gd = git_dir(replay)
    return (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists()


def load_edit(script: Path) -> tuple[list[str], object]:
    namespace: dict[str, object] = {"__file__": str(script)}
    exec(compile(script.read_text(), str(script), "exec"), namespace)
    files, edit = namespace.get("FILES"), namespace.get("edit")
    if not isinstance(files, (list, tuple)) or not files or not callable(edit):
        raise ReplayError(f"{script} must define a non-empty FILES list and edit(path, text)")
    return list(files), edit


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)


def amend(root: Path, replay: Path, subject: str, script: Path) -> None:
    files, edit = load_edit(script)
    if rebase_in_progress(replay):
        raise ReplayError("a rebase is already in progress in the replay")
    if git(replay, "status", "--porcelain"):
        raise ReplayError("the replay has uncommitted changes")
    pairs = subjects(replay)
    matches = [i for i, (_, s) in enumerate(pairs) if s == subject]
    if len(matches) != 1:
        raise ReplayError(f"{len(matches)} commits have subject {subject!r}; need exactly one")
    index = matches[0]
    if index == 0:
        raise ReplayError("base is pristine Samba; it cannot be amended")

    head = git_text(replay, "rev-parse", "HEAD")
    backups = git_text(replay, "for-each-ref", "--format=%(refname)", "refs/replay-backups/")
    backup = f"refs/replay-backups/{len(backups.splitlines()) + 1}"
    git(replay, "update-ref", backup, head)

    todo = ["edit " + pairs[index][0]] + ["pick " + c for c, _ in pairs[index + 1:]]
    todo_file = git_dir(replay) / "tc-replay-todo"
    todo_file.write_text("\n".join(todo) + "\n")
    env = dict(os.environ, GIT_SEQUENCE_EDITOR="cp " + shlex.quote(str(todo_file)),
               GIT_EDITOR="true")
    try:
        try:
            # --empty=stop: a later patch the edit makes empty must not vanish.
            git(replay, "rebase", "-q", "-i", "--empty=stop", pairs[index - 1][0], env=env)
            changed = []
            for name in files:
                path = replay / name
                old = read_text(path)
                new = edit(name, old)
                if new is None:
                    if path.exists():
                        path.unlink()
                        changed.append(name)
                elif new != old or not path.exists():
                    write_text(path, new)
                    changed.append(name)
            if not changed:
                raise ReplayError(f"{script} changed nothing")
            # -f and explicit paths: ignore rules must not drop a file.
            git(replay, "add", "-A", "-f", "--", *changed)
            git(replay, "commit", "-q", "--amend", "--no-edit", "--no-verify")
        except BaseException:
            git(replay, "rebase", "--abort", check=False)
            raise
        git(replay, "rebase", "--continue", check=False, env=env)
    finally:
        todo_file.unlink(missing_ok=True)
    if rebase_in_progress(replay):
        status = git(replay, "status", "--short").decode(errors="replace")
        raise ReplayError(
            f"a later commit stopped the replay:\n{status}"
            "Resolve and run `git rebase --continue` in the replay, or "
            f"`git rebase --abort`. The history before the amend is {backup}.")
    print(f"{subject} amended; previous history saved as {backup}")


def check_export(root: Path, replay: Path, pairs: list[tuple[str, str]]) -> None:
    check_ref(root, replay)
    names = [s for _, s in pairs[2:]]
    for name in names:
        if not PATCH_NAME.match(name):
            raise ReplayError(f"commit subject {name!r} is not a patch file name")
    if names != series_names(root):
        raise ReplayError("the replay's patch commits differ from series; update series "
                          f"first\n series: {series_names(root)}\n replay: {names}")
    recorded_path = git_dir(replay) / INPUTS_FILE
    recorded = json.loads(recorded_path.read_text()) if recorded_path.exists() else None
    current = input_hashes(root)
    if recorded != current:
        changed = sorted(k for k in set(current) | set(recorded or {})
                         if current.get(k) != (recorded or {}).get(k))
        raise ReplayError("the repo's patch or overlay files changed since this replay last "
                          "read or wrote them; run init again:\n  " + "\n  ".join(changed))


def overlay_tree(replay: Path, base: str, overlay: str) -> dict[str, str]:
    """Overlay path -> mode, from the overlay commit; it may only add files."""
    out = git_text(replay, "diff", "--no-renames", "--name-status", base, overlay)
    files = {}
    for line in out.splitlines():
        status, path = line.split("\t", 1)
        if status != "A":
            raise ReplayError(f"the overlay commit must only add files; {status} {path}")
        mode = git_text(replay, "ls-tree", overlay, "--", path).split()[0]
        files[path] = mode
    return files


def export(root: Path, replay: Path) -> None:
    pairs = subjects(replay)
    check_export(root, replay, pairs)
    (base, _), (overlay, _) = pairs[0], pairs[1]
    ours = overlay_tree(replay, base, overlay)
    patches = {}
    for (prev, _), (rev, name) in zip(pairs[1:], pairs[2:]):
        touched = set(git_text(replay, "diff", "--no-renames", "--name-only", prev, rev)
                      .splitlines())
        if touched & set(ours):
            raise ReplayError(f"{name} edits overlay files {sorted(touched & set(ours))}; "
                              "amend the overlay commit instead")
        diff = git(replay, *DIFF_ARGS, prev, rev)
        if b"\nBinary files " in b"\n" + diff or b"\nGIT binary patch" in diff:
            raise ReplayError(f"{name} changes a binary file, which the series cannot carry")
        patches[name] = b"".join(line for line in diff.splitlines(True)
                                 if not line.startswith(b"index "))

    # Everything is checked; now write.
    pdir = root / PATCH_DIR
    for name, data in patches.items():
        (pdir / name).write_bytes(data)
    for stale in pdir.glob("*.patch"):
        if stale.name not in patches:
            stale.unlink()
    odir = pdir / "overlay"
    for path, mode in ours.items():
        dst = odir / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(git(replay, "cat-file", "blob", f"{overlay}:{path}"))
        dst.chmod(0o755 if mode == "100755" else 0o644)
    for name in overlay_files(root):
        if name not in ours:
            (odir / name).unlink()
    for directory in sorted((p for p in odir.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    save_inputs(root, replay)
    print(f"exported {len(patches)} patches and {len(ours)} overlay files")


def verify(root: Path, replay: Path) -> None:
    check_ref(root, replay)
    pairs = subjects(replay)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "src"
        work.mkdir()
        # Pristine comes from the replay's base, which check_ref tied to the
        # pinned ref; the series is applied exactly as the build applies it.
        subprocess.run(["tar", "-x", "-C", str(work)], check=True,
                       input=git(replay, "archive", "--format=tar", pairs[0][0]))
        git(work, "init", "-q")
        configure(work)
        p = subprocess.run(
            ["sh", "-c", '. "$1"; patch_apply_series Verify "$2" "$3"', "sh",
             str(root / "build/_patch_helpers.sh"), str(root / PATCH_DIR / "series"),
             str(work)], capture_output=True, text=True)
        if p.returncode != 0:
            raise ReplayError("the series does not apply:\n" + (p.stdout + p.stderr)[-2000:])
        git(work, "add", "-A", "-f")
        got = git_text(work, "write-tree")
        want = git_text(replay, "rev-parse", "HEAD^{tree}")
        if got != want:
            git(work, "fetch", "-q", str(replay), "HEAD")
            stat = git(work, "diff", "--stat", want, got).decode(errors="replace")
            raise ReplayError(f"MISMATCH: series tree {got}, replay tree {want}\n{stat}")
    print(f"MATCH: the series gives the replay tree {want}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository checkout")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "export", "verify"):
        sub.add_parser(name).add_argument("replay", type=Path)
    amend_parser = sub.add_parser("amend")
    amend_parser.add_argument("replay", type=Path)
    amend_parser.add_argument("subject")
    amend_parser.add_argument("script", type=Path)
    args = parser.parse_args(argv)
    root, replay = args.root.resolve(), args.replay.resolve()
    try:
        if args.command == "amend":
            amend(root, replay, args.subject, args.script)
        else:
            {"init": init, "export": export, "verify": verify}[args.command](root, replay)
    except ReplayError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
