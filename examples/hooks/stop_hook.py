"""Block an agent's "done" while its branch semantically collides with a sibling agent branch.

Exit 0 = allow stop. Exit 2 = block; stderr is shown to the agent as the reason.
Usage: python stop_hook.py --glob 'claude/*'
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--glob", default="*/*", help="pattern matching the sibling agent branches")
    parser.add_argument("--target", default=None)
    args = parser.parse_args()

    try:
        payload = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
    except json.JSONDecodeError:
        payload = {}
    if payload.get("stop_hook_active"):
        return 0  # we already blocked once this turn; never trap the agent in a loop

    branch = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
    if not branch:
        return 0

    cmd = [sys.executable, "-m", "tangle", "scan", "--glob", args.glob, "--format", "json", "--fail-on", "never"]
    if args.target:
        cmd += ["--target", args.target]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return 0  # fewer than two branches, or tangle unavailable: nothing to say

    mine = [f for f in report["findings"] if f["severity"] == "high" and branch in f["branches"]]
    if not mine:
        return 0
    lines = [f"tangle: your branch `{branch}` collides with work on sibling branches:"]
    for f in mine:
        lines.append(f"- {f['summary']}")
        lines += [f"    {d}" for d in f["details"]]
    lines.append("Adapt your change if you can (e.g. use the new signature), or state the collision in your summary.")
    print("\n".join(lines), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
