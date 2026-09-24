"""Rendering: terminal text, Markdown (for PR comments / CI summaries) and JSON."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

from .scan import ScanResult, suggest_order
from .verify import VerifyResult

CELL = {"ok": "ok", "risk": "risk", "break": "BREAK", "conflict": "CONFLICT", "red": "red", None: "-"}
COLOR = {"ok": "32", "risk": "33", "break": "1;31", "conflict": "1;35", "red": "90", "high": "1;31",
         "medium": "33", "low": "36"}


def _use_color(stream) -> bool:
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, key: str | None, color: bool) -> str:
    return f"\033[{COLOR[key]}m{text}\033[0m" if color and key in COLOR else text


def matrix(names: list[str], verdict, color: bool) -> str:
    width = max(8, *(len(n) for n in names))
    cells = [[CELL[verdict(a, b)] if a != b else "" for b in names] for a in names]
    col = max(8, *(len(n) for n in names), *(len(c) for row in cells for c in row))
    lines = [" " * width + "  " + "  ".join(n.ljust(col) for n in names)]
    for a, row in zip(names, cells):
        out = []
        for b, cell in zip(names, row):
            key = verdict(a, b) if a != b else None
            out.append(_c(cell.ljust(col), key, color))
        lines.append(a.ljust(width) + "  " + "  ".join(out))
    return "\n".join(lines)


def _scan_verdict(result: ScanResult):
    def f(a: str, b: str):
        return result.verdicts.get((a, b)) or result.verdicts.get((b, a))
    return f


def scan_text(result: ScanResult, stream=sys.stdout) -> str:
    color = _use_color(stream)
    names = [b.name for b in result.branches]
    out = [f"tangle scan: {len(names)} branches against {result.target}", ""]
    if len(names) >= 2:
        out += [matrix(names, _scan_verdict(result), color), ""]
    if not result.findings:
        out.append("No cross-branch risks found.")
    for f in result.findings:
        out.append(f"{_c(f.severity.upper().ljust(6), f.severity, color)} [{f.kind}] {f.summary}")
        out += [f"         {d}" for d in f.details]
    if any(f.severity == "high" for f in result.findings):
        out += ["", "Suggested merge order: " + " -> ".join(suggest_order(result)),
                "Rebase each later branch onto the new target and re-run `tangle verify` before merging it."]
    if result.notes:
        out += [""] + [f"note: {n}" for n in result.notes]
    return "\n".join(out)


def verify_text(result: VerifyResult, stream=sys.stdout) -> str:
    color = _use_color(stream)
    names = list(result.singles)
    out = [f"tangle verify: `{result.command}` on {result.target} + each branch, pair" +
           (" and all together" if result.combined else ""), ""]
    for n, run in result.singles.items():
        key = {"pass": "ok", "fail": "red", "conflict": "conflict", "timeout": "red"}[run.status]
        out.append(f"  {n.ljust(24)} alone: {_c(run.status, key, color)}  ({run.seconds:.1f}s)")
    out.append("")

    def verdict(a: str, b: str):
        if (a, b) in result.pairs:
            return result.pair_verdict(a, b)
        if (b, a) in result.pairs:
            return result.pair_verdict(b, a)
        return None
    out += [matrix(names, verdict, color), ""]
    breaks = [(a, b) for (a, b) in result.pairs if result.pair_verdict(a, b) == "break"]
    for a, b in breaks:
        out.append(_c(f"BREAK  {a} + {b}: both green alone, red together", "break", color))
        tail = result.pairs[(a, b)].tail
        out += [f"         | {line}" for line in tail.splitlines()[-8:]]
    if result.combined:
        out.append(f"all {len(names)} together: {result.combined.status}")
    if not breaks:
        out.append("No pair is green alone and red together.")
    return "\n".join(out)


def scan_markdown(result: ScanResult) -> str:
    names = [b.name for b in result.branches]
    v = _scan_verdict(result)
    icon = {"ok": "✅", "risk": "🟡", "break": "🔴", "conflict": "⛔", None: ""}
    lines = ["### tangle: cross-branch risk", "", "| | " + " | ".join(f"`{n}`" for n in names) + " |",
             "|---" * (len(names) + 1) + "|"]
    for a in names:
        lines.append(f"| `{a}` | " + " | ".join(icon[v(a, b)] if a != b else "" for b in names) + " |")
    lines.append("")
    for f in result.findings:
        lines.append(f"- **{f.severity}** `{f.kind}`: {f.summary}")
        lines += [f"  - {d}" for d in f.details]
    if not result.findings:
        lines.append("No cross-branch risks found.")
    return "\n".join(lines)


def scan_json(result: ScanResult) -> str:
    return json.dumps({
        "target": result.target,
        "branches": [b.name for b in result.branches],
        "pairs": [{"a": a, "b": b, "verdict": v} for (a, b), v in result.verdicts.items()],
        "findings": [asdict(f) for f in result.findings],
        "suggested_order": suggest_order(result),
        "notes": result.notes,
    }, indent=2)


def verify_json(result: VerifyResult) -> str:
    return json.dumps({
        "target": result.target,
        "command": result.command,
        "singles": {k: asdict(v) for k, v in result.singles.items()},
        "pairs": [{"a": a, "b": b, "verdict": result.pair_verdict(a, b), **asdict(r)}
                  for (a, b), r in result.pairs.items()],
        "combined": asdict(result.combined) if result.combined else None,
    }, indent=2)
