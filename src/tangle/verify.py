"""Dynamic check: actually merge branches in a throwaway worktree and run the test command.

The interesting cell is "each branch is green on its own, the pair is red" - a semantic conflict
that no single agent (and no per-PR CI run) could have seen.
"""

from __future__ import annotations

import itertools
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

from . import gitutil

_IDENTITY = ("-c", "user.name=tangle", "-c", "user.email=tangle@localhost", "-c", "commit.gpgsign=false")


@dataclass
class RunResult:
    branches: tuple[str, ...]
    status: str  # pass | fail | conflict | timeout
    seconds: float
    tail: str = ""


@dataclass
class VerifyResult:
    target: str
    command: str
    singles: dict[str, RunResult]
    pairs: dict[tuple[str, str], RunResult]
    combined: RunResult | None

    def pair_verdict(self, a: str, b: str) -> str:
        run = self.pairs[(a, b)]
        if run.status == "conflict":
            return "conflict"
        if run.status == "pass":
            return "ok"
        alone_green = self.singles[a].status == "pass" and self.singles[b].status == "pass"
        return "break" if alone_green else "red"  # red = already red on its own, not the pair's fault


class _Sandbox:
    def __init__(self, target: str, cwd: str | None):
        self.cwd = cwd
        self.target = target
        self.path = tempfile.mkdtemp(prefix="tangle-")

    def __enter__(self) -> "_Sandbox":
        gitutil.git("worktree", "add", "--detach", "--force", self.path, self.target, cwd=self.cwd)
        return self

    def __exit__(self, *exc) -> None:
        gitutil.git("worktree", "remove", "--force", self.path, cwd=self.cwd, check=False)
        shutil.rmtree(self.path, ignore_errors=True)
        gitutil.git("worktree", "prune", cwd=self.cwd, check=False)

    def reset(self) -> None:
        gitutil.git("merge", "--abort", cwd=self.path, check=False)
        gitutil.git("reset", "--hard", "--quiet", self.target, cwd=self.path)
        gitutil.git("clean", "-fdq", cwd=self.path)  # keeps ignored files (venvs, node_modules)

    def merge(self, branch: str) -> bool:
        proc = gitutil.git(*_IDENTITY, "merge", "--no-edit", "--no-ff", "--quiet", branch, cwd=self.path, check=False)
        return proc.returncode == 0

    def run(self, command: str, timeout: float) -> tuple[str, str]:
        # shell=True on purpose: `command` is the user's own test command (like a CI `run:` step),
        # and users expect pipes, `&&` and env-var syntax to work.
        try:
            proc = subprocess.run(command, shell=True, cwd=self.path, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return "timeout", ""
        output = (proc.stdout + proc.stderr).strip().splitlines()
        return ("pass" if proc.returncode == 0 else "fail"), "\n".join(output[-15:])


def _attempt(box: _Sandbox, branches: tuple[str, ...], command: str, timeout: float) -> RunResult:
    start = time.monotonic()
    box.reset()
    for b in branches:
        if not box.merge(b):
            return RunResult(branches, "conflict", time.monotonic() - start, f"merge of {b} conflicted")
    status, tail = box.run(command, timeout)
    return RunResult(branches, status, time.monotonic() - start, tail)


def verify(branches: list[str], target: str, command: str, cwd: str | None = None,
           timeout: float = 600, combined: bool = True, pairs: list[tuple[str, str]] | None = None,
           progress=None) -> VerifyResult:
    todo_pairs = pairs if pairs is not None else list(itertools.combinations(branches, 2))
    singles: dict[str, RunResult] = {}
    pair_runs: dict[tuple[str, str], RunResult] = {}
    all_run = None
    with _Sandbox(target, cwd) as box:
        for b in branches:
            progress and progress(f"testing {b} on top of {target}")
            singles[b] = _attempt(box, (b,), command, timeout)
        for a, b in todo_pairs:
            progress and progress(f"testing {a} + {b}")
            pair_runs[(a, b)] = _attempt(box, (a, b), command, timeout)
        if combined and len(branches) > 2:
            progress and progress(f"testing all {len(branches)} branches together")
            all_run = _attempt(box, tuple(branches), command, timeout)
        box.reset()
    return VerifyResult(target, command, singles, pair_runs, all_run)
