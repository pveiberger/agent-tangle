"""Thin wrappers around the git CLI. Everything tangle knows about a repo comes from here."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc


def repo_root(cwd: str | None = None) -> str:
    return git("rev-parse", "--show-toplevel", cwd=cwd).stdout.strip()


def rev_exists(rev: str, cwd: str | None = None) -> bool:
    return git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", cwd=cwd, check=False).returncode == 0


def default_target(cwd: str | None = None) -> str:
    for name in ("main", "master", "trunk", "develop"):
        if rev_exists(name, cwd):
            return name
    return "HEAD"


def local_branches(cwd: str | None = None) -> list[str]:
    out = git("for-each-ref", "--format=%(refname:short)", "refs/heads/", cwd=cwd).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def merge_base(a: str, b: str, cwd: str | None = None) -> str:
    return git("merge-base", a, b, cwd=cwd).stdout.strip()


def rev_parse(rev: str, cwd: str | None = None) -> str:
    return git("rev-parse", rev, cwd=cwd).stdout.strip()


@dataclass
class FileChange:
    status: str  # A, M, D, R
    path: str  # path in head
    old_path: str  # path in base (differs only for renames)


def changed_files(base: str, head: str, cwd: str | None = None) -> list[FileChange]:
    out = git("diff", "--name-status", "-M", "--no-color", base, head, cwd=cwd).stdout
    changes: list[FileChange] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        code = parts[0][0]
        if code == "R" and len(parts) >= 3:
            changes.append(FileChange("R", parts[2], parts[1]))
        elif len(parts) >= 2:
            changes.append(FileChange(code, parts[1], parts[1]))
    return changes


def file_at(rev: str, path: str, cwd: str | None = None) -> str | None:
    proc = git("show", f"{rev}:{path}", cwd=cwd, check=False)
    return proc.stdout if proc.returncode == 0 else None


def added_lines(base: str, head: str, path: str, cwd: str | None = None) -> set[int]:
    """Line numbers (1-based, in the head version of `path`) that the diff adds or rewrites."""
    out = git("diff", "-U0", "--no-color", "-M", base, head, "--", path, cwd=cwd).stdout
    lines: set[int] = set()
    for line in out.splitlines():
        m = _HUNK.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            lines.update(range(start, start + count))
    return lines


def textual_conflicts(a: str, b: str, cwd: str | None = None) -> list[str] | None:
    """Files that would conflict when merging a and b, using `git merge-tree` (no worktree touched).

    Returns [] for a clean merge, a list of paths for conflicts, None if git is too old (< 2.38).
    """
    proc = git("merge-tree", "--write-tree", "--name-only", "--no-messages", a, b, cwd=cwd, check=False)
    if proc.returncode == 0:
        return []
    if proc.returncode == 1:
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        return sorted(set(lines[1:]))  # first line is the tree oid
    if "usage" in proc.stderr.lower() or "unknown option" in proc.stderr.lower():
        return None
    raise GitError(f"git merge-tree failed: {proc.stderr.strip()}")
