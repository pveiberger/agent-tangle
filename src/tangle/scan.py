"""Static cross-branch analysis: predict which branches break each other, without running anything.

For every branch we compute, relative to its merge-base with the target:
  * breaks    - symbols it removed or whose signature it changed
  * new_refs  - symbols referenced on lines it added
  * modified  - symbols whose body it changed
Then for every pair (A, B) we look for:
  * textual   - git itself would conflict (git merge-tree)
  * semantic  - A breaks symbol S while B adds new uses of S. Git merges this cleanly; the result is broken.
  * overlap   - both edited the same symbol in different hunks. Git merges it; nobody tested the combination.
  * hotspot   - both touched a collision-prone file (lockfile, manifest, migrations, schema).
"""

from __future__ import annotations

import fnmatch
import itertools
from dataclasses import dataclass, field

from . import gitutil
from .symbols import Definition, extract, language_of

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

HOTSPOT_PATTERNS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "poetry.lock", "uv.lock",
    "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock",
    "package.json", "pyproject.toml", "requirements*.txt", "go.mod", "Cargo.toml",
    "*.sql", "schema.prisma", "*.graphql", "openapi*.y*ml", "openapi*.json",
)
MIGRATION_DIRS = ("migrations", "migrate", "alembic", "db/migrate")


@dataclass
class Break:
    symbol: Definition
    path: str
    change: str  # "removed" | "signature"
    new_signature: str | None = None


@dataclass
class BranchAnalysis:
    name: str
    base: str
    files: list[gitutil.FileChange]
    breaks: dict[str, list[Break]] = field(default_factory=dict)  # short name -> breaks
    new_refs: dict[str, list[tuple[str, int, str]]] = field(default_factory=dict)  # name -> (path, line, strength)
    modified: set[tuple[str, str]] = field(default_factory=set)  # (path, qualname)
    added_names: set[str] = field(default_factory=set)
    unparsed: list[str] = field(default_factory=list)


@dataclass
class Finding:
    severity: str
    kind: str
    branches: tuple[str, str]
    summary: str
    details: list[str] = field(default_factory=list)
    symbol: str | None = None


@dataclass
class ScanResult:
    target: str
    branches: list[BranchAnalysis]
    findings: list[Finding]
    verdicts: dict[tuple[str, str], str]  # pair -> conflict | break | risk | ok
    notes: list[str] = field(default_factory=list)


def analyze_branch(name: str, target: str, cwd: str | None = None) -> BranchAnalysis:
    base = gitutil.merge_base(target, name, cwd)
    files = gitutil.changed_files(base, name, cwd)
    ba = BranchAnalysis(name, base, files)

    base_defs: dict[str, dict[str, Definition]] = {}
    head_defs: dict[str, dict[str, Definition]] = {}
    head_refs = {}
    for fc in files:
        if not language_of(fc.path):
            continue
        before = extract(fc.old_path, None if fc.status == "A" else gitutil.file_at(base, fc.old_path, cwd))
        after = extract(fc.path, None if fc.status == "D" else gitutil.file_at(name, fc.path, cwd))
        if not before.parsed or not after.parsed:
            ba.unparsed.append(fc.path)
        base_defs[fc.path] = before.defs
        head_defs[fc.path] = after.defs
        head_refs[fc.path] = after.refs

    # A symbol that disappears from one file but reappears (same short name) in another was moved, not removed.
    names_after = {d.name for defs in head_defs.values() for d in defs.values()}
    names_before = {d.name for defs in base_defs.values() for d in defs.values()}
    ba.added_names = names_after - names_before

    for path, before in base_defs.items():
        after = head_defs.get(path, {})
        for qual, old in before.items():
            new = after.get(qual)
            if new is None:
                if old.name not in names_after:
                    ba.breaks.setdefault(old.name, []).append(Break(old, path, "removed"))
                continue
            if old.signature is not None and not signature_compatible(old.signature, new.signature):
                ba.breaks.setdefault(old.name, []).append(Break(old, path, "signature", new.signature))
            # Classes are skipped: their methods are tracked individually, and a class "changes"
            # whenever any method does, which would double-report every overlap.
            if new.body_hash != old.body_hash and new.kind != "class":
                ba.modified.add((path, qual))
        for qual, new in after.items():
            if qual not in before and new.kind != "class":
                ba.modified.add((path, qual))

    for path, refs in head_refs.items():
        added = gitutil.added_lines(base, name, path, cwd)
        for ref in refs:
            if ref.line in added and not ref.name.startswith("__"):
                ba.new_refs.setdefault(ref.name, []).append((path, ref.line, ref.strength))
    return ba


def _split_params(sig: str) -> tuple[list[str], str]:
    """'(a, b: dict[str, int] = {}) -> X' -> (['a', 'b: dict[str, int] = {}'], '-> X')."""
    if not sig.startswith("("):
        return [], sig
    depth, i = 0, 0
    for i, ch in enumerate(sig):
        depth += ch in "([{<"
        depth -= ch in ")]}>"
        if depth == 0:
            break
    inner, rest = sig[1:i], sig[i + 1 :].strip()
    params, buf, depth = [], "", 0
    for ch in inner:
        if ch == "," and depth == 0:
            params.append(buf.strip())
            buf = ""
            continue
        depth += ch in "([{<"
        depth -= ch in ")]}>"
        buf += ch
    if buf.strip():
        params.append(buf.strip())
    return params, rest


def signature_compatible(old: str, new: str | None) -> bool:
    """True if every existing call site of `old` still works against `new`.

    Conservative: only "same parameters, plus new trailing parameters that have defaults
    (or are *args/**kwargs/optional?)" counts as compatible. Return-type changes are ignored.
    """
    if new is None:
        return False
    old_params, _ = _split_params(old)
    new_params, _ = _split_params(new)
    if new_params[: len(old_params)] != old_params:
        return False
    for extra in new_params[len(old_params) :]:
        name = extra.split(":", 1)[0].split("=", 1)[0].strip()
        if not ("=" in extra or name.startswith("*") or name.endswith("?") or extra.startswith("...")):
            return False
    return True


def _semantic(a: BranchAnalysis, b: BranchAnalysis) -> list[Finding]:
    """A breaks something that B starts using."""
    findings = []
    for name, breaks in a.breaks.items():
        uses = b.new_refs.get(name)
        if not uses or name in b.added_names:
            continue
        for brk in breaks:
            sym = brk.symbol
            is_member = sym.kind == "method"
            strong = [u for u in uses if u[2] == "direct"] if not is_member else [u for u in uses if u[2] == "attribute"]
            if not strong:
                continue
            severity = "medium" if is_member else "high"
            if brk.change == "removed":
                what = f"removed `{sym.qualname}`"
            else:
                what = f"changed the signature of `{sym.qualname}` {sym.signature} -> {brk.new_signature}"
            locs = [f"{path}:{line}" for path, line, _ in strong[:5]]
            more = f" (+{len(strong) - 5} more)" if len(strong) > 5 else ""
            findings.append(
                Finding(
                    severity,
                    "semantic",
                    (a.name, b.name),
                    f"{a.name} {what}; {b.name} adds new uses of it",
                    [f"defined in {brk.path}:{sym.line} on the base",
                     f"new uses in {b.name}: {', '.join(locs)}{more}"],
                    sym.qualname,
                )
            )
    return findings


def _is_hotspot(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, pat) for pat in HOTSPOT_PATTERNS)


def _migration_dir(path: str) -> str | None:
    parts = path.split("/")
    for i in range(len(parts) - 1):
        joined = "/".join(parts[: i + 1])
        if parts[i] in MIGRATION_DIRS or joined.endswith(MIGRATION_DIRS):
            return joined
    return None


def analyze_pair(a: BranchAnalysis, b: BranchAnalysis, cwd: str | None = None) -> tuple[list[Finding], str]:
    findings: list[Finding] = []
    conflicted = gitutil.textual_conflicts(a.name, b.name, cwd)
    if conflicted:
        findings.append(
            Finding("high", "textual", (a.name, b.name), f"git cannot merge {a.name} and {b.name}",
                    [f"conflicting: {', '.join(conflicted[:8])}" + (" ..." if len(conflicted) > 8 else "")])
        )
    conflicted_set = set(conflicted or [])

    findings += _semantic(a, b)
    findings += _semantic(b, a)

    for path, qual in sorted(a.modified & b.modified):
        if path in conflicted_set:
            continue
        findings.append(
            Finding("medium", "overlap", (a.name, b.name),
                    f"both branches edit `{qual}` in {path}; git merges it cleanly, the combination is untested",
                    symbol=qual)
        )

    a_paths = {fc.path for fc in a.files}
    b_paths = {fc.path for fc in b.files}
    for path in sorted((a_paths & b_paths) - conflicted_set):
        if _is_hotspot(path):
            findings.append(Finding("low", "hotspot", (a.name, b.name), f"both branches change {path}"))
    a_mig = {d for p in a_paths if (d := _migration_dir(p))}
    b_mig = {d for p in b_paths if (d := _migration_dir(p))}
    for d in sorted(a_mig & b_mig):
        findings.append(Finding("medium", "hotspot", (a.name, b.name),
                                f"both branches add or change migrations in {d}/ (ordering / numbering collision)"))

    if conflicted:
        verdict = "conflict"
    elif any(f.severity == "high" for f in findings):
        verdict = "break"
    elif findings:
        verdict = "risk"
    else:
        verdict = "ok"
    return findings, verdict


def scan(branches: list[str], target: str, cwd: str | None = None) -> ScanResult:
    analyses = [analyze_branch(b, target, cwd) for b in branches]
    findings: list[Finding] = []
    verdicts: dict[tuple[str, str], str] = {}
    notes: list[str] = []
    for a, b in itertools.combinations(analyses, 2):
        pair_findings, verdict = analyze_pair(a, b, cwd)
        findings += pair_findings
        verdicts[(a.name, b.name)] = verdict
    for a in analyses:
        if a.unparsed:
            notes.append(f"{a.name}: could not parse {', '.join(a.unparsed)} (syntax error?) - symbol checks skipped there")
        if not a.files:
            notes.append(f"{a.name}: no changes relative to {target}")
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.kind, f.branches))
    return ScanResult(target, analyses, findings, verdicts, notes)


def suggest_order(result: ScanResult) -> list[str]:
    """Merge order that lands the least-entangled branches first.

    Branches involved in no high-severity finding go first; the rest follow ordered by how many
    pairs they break. Each later branch should be rebased onto the target (and re-verified) before merging.
    """
    names = [b.name for b in result.branches]
    weight = {n: 0 for n in names}
    for f in result.findings:
        if f.severity == "high":
            for n in f.branches:
                weight[n] += 1
    return sorted(names, key=lambda n: (weight[n], names.index(n)))
