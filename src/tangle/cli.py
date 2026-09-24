"""`tangle` command line."""

from __future__ import annotations

import argparse
import fnmatch
import sys

from . import __version__, gitutil, report
from .scan import SEVERITY_ORDER, scan
from .verify import verify


def _branches(args, root: str, target: str) -> list[str]:
    if args.branches:
        missing = [b for b in args.branches if not gitutil.rev_exists(b, root)]
        if missing:
            sys.exit(f"tangle: unknown branch(es): {', '.join(missing)}")
        return args.branches
    names = [b for b in gitutil.local_branches(root) if b != target]
    if args.glob:
        names = [b for b in names if fnmatch.fnmatch(b, args.glob)]
    # Branches already merged into the target have nothing left to collide.
    return [b for b in names if gitutil.rev_parse(b, root) != gitutil.merge_base(target, b, root)]


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("branches", nargs="*", help="branches to compare (default: all local branches not yet merged)")
    p.add_argument("-t", "--target", help="branch they will be merged into (default: main/master)")
    p.add_argument("-g", "--glob", help="only branches matching this pattern, e.g. 'agent/*' or 'claude/*'")
    p.add_argument("-f", "--format", choices=("text", "markdown", "json"), default="text")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        prog="tangle",
        description="Find the parallel agent branches that break each other - before you merge them.",
    )
    parser.add_argument("--version", action="version", version=f"tangle {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="static prediction: textual, semantic, overlap and hotspot conflicts (fast, runs nothing)")
    _common(p_scan)
    p_scan.add_argument("--fail-on", choices=("high", "medium", "low", "never"), default="high",
                        help="exit 1 if any finding is at least this severe (default: high)")

    p_ver = sub.add_parser("verify", help="merge each branch and each pair in a throwaway worktree and run your tests")
    _common(p_ver)
    p_ver.add_argument("--test", required=True, help='test command, e.g. "pytest -q" or "npm test"')
    p_ver.add_argument("--pairs", choices=("all", "flagged"), default="all",
                       help="test every pair, or only pairs `tangle scan` flags (much faster for many branches)")
    p_ver.add_argument("--timeout", type=float, default=600, help="seconds per test run (default 600)")
    p_ver.add_argument("--no-combined", action="store_true", help="skip the all-branches-together run")

    args = parser.parse_args(argv)
    try:
        root = gitutil.repo_root()
    except gitutil.GitError:
        sys.exit("tangle: not inside a git repository")
    target = args.target or gitutil.default_target(root)
    branches = _branches(args, root, target)
    if len(branches) < 2:
        print(f"tangle: need at least two unmerged branches to compare against {target} (found {len(branches)}).")
        return 0

    try:
        if args.cmd == "scan":
            result = scan(branches, target, root)
            print({"text": report.scan_text, "markdown": report.scan_markdown, "json": report.scan_json}[args.format](result))
            if args.fail_on == "never":
                return 0
            limit = SEVERITY_ORDER[args.fail_on]
            return 1 if any(SEVERITY_ORDER[f.severity] <= limit for f in result.findings) else 0

        pairs = None
        if args.pairs == "flagged":
            flagged = scan(branches, target, root)
            pairs = [p for p, v in flagged.verdicts.items() if v != "ok"]
        progress = (lambda msg: print(f"  ... {msg}", file=sys.stderr)) if args.format == "text" else None
        result = verify(branches, target, args.test, root, args.timeout, not args.no_combined, pairs, progress)
        print(report.verify_json(result) if args.format == "json" else report.verify_text(result))
        return 1 if any(result.pair_verdict(a, b) in ("break", "conflict") for a, b in result.pairs) else 0
    except gitutil.GitError as e:
        sys.exit(f"tangle: {e}")


if __name__ == "__main__":
    sys.exit(main())
