# tangle

**Find the parallel AI-agent branches that break each other before you merge them.**

You run three coding agents in three worktrees. Each one finishes green. Git merges all three without a single conflict. `main` is red.

That failure mode, *green alone, red together*, is what tangle catches:

```
$ tangle scan
tangle scan: 3 branches against main

                agent/checkout  agent/currency  agent/docs
agent/checkout                  BREAK           ok
agent/currency  BREAK                           ok
agent/docs      ok              ok

HIGH   [semantic] agent/currency changed the signature of `total` (items, tax_rate) -> (items, tax_rate, currency); agent/checkout adds new uses of it
         defined in shop/pricing.py:1 on the base
         new uses in agent/checkout: shop/checkout.py:1, shop/checkout.py:6

Suggested merge order: agent/docs -> agent/checkout -> agent/currency
```

`agent/currency` made a parameter required and fixed every caller that existed on *its* base. `agent/checkout` wrote a brand-new caller against the *old* signature. Neither agent could have seen the other. No textual conflict. Per-PR CI is green twice. tangle flags it statically in well under a second and then proves it:

```
$ tangle verify --test "python -m unittest -q"
  agent/checkout           alone: pass  (0.6s)
  agent/currency           alone: pass  (0.5s)
  agent/docs               alone: pass  (0.7s)

BREAK  agent/checkout + agent/currency: both green alone, red together
         | TypeError: total() missing 1 required positional argument: 'currency'
all 3 together: fail
```

## Why this exists

Parallel agents are now the default workflow. Claude Code, Codex CLI, Cursor and JetBrains all ship worktree-per-agent modes. Worktrees isolate agents *while they work*. They do nothing about what happens when the work comes back together.

- **27.7 %** of 142k AI-agent pull requests contained merge conflicts ([AgenticFlict, 2026](https://arxiv.org/pdf/2604.03551)).
- **41.7 %** conflict rate between PRs from *different* agents, versus 19.8 % between PRs from the same agent. **79.4 %** of agent PRs competed for the same merge target at the same time ([33,596-PR study](https://codex.danielvaughan.com/2026/07/28/agent-pr-merge-conflicts-concurrent-coding-agents-codex-cli-worktree-isolation-coordination-defence/)).
- Those numbers only count the conflicts *git can see*. Semantic interference without textual overlap is named as an open problem in the research ([Claim Plane, 2026](https://arxiv.org/pdf/2607.21909)). Existing conflict predictors compare changed paths, hunks, or the *same* symbol edited twice. None of them asks "did A change something B has just started to depend on?"
- Reviewing, not writing, is now the bottleneck of AI-assisted development ([Qodo, 2026](https://www.qodo.ai/blog/state-of-ai-code-quality-report-2026/)). A reviewer looking at one PR at a time cannot catch a bug that only exists in the combination of two.

tangle is the missing step between "every agent is done" and "merge".

## Install

```bash
pip install git+https://github.com/<you>/agent-tangle
```

Zero dependencies, Python ≥ 3.9, git ≥ 2.38 (for `git merge-tree --write-tree`).

Try it on the bundled demo:

```bash
python examples/make_demo.py /tmp/tangle-demo && cd /tmp/tangle-demo
tangle scan
tangle verify --test "python -m unittest -q"
```

## What it checks

### `tangle scan`: static, fast, runs nothing

For every branch, relative to its merge-base with the target, tangle parses the changed files before and after the change and records:

| per branch | meaning |
|---|---|
| **breaks** | symbols it removed, or whose signature changed incompatibly |
| **new uses** | symbols referenced on lines the branch added |
| **modified** | functions/methods whose body changed |

Then for every pair of branches:

| finding | severity | what it means |
|---|---|---|
| `textual` | high | git itself will conflict (`git merge-tree`, no checkout needed) |
| `semantic` | high | A removed or incompatibly changed `S`; B adds new calls to `S`. **Git merges cleanly, the result is broken.** |
| `semantic` (method) | medium | same, for methods matched through `obj.method` (less certain) |
| `overlap` | medium | both edited the same function in different hunks: merged silently, never tested together |
| `hotspot` | low/medium | both touched a lockfile, manifest, schema, or added migrations to the same directory |

It doesn't cry wolf on the easy cases:
- Adding a parameter **with a default** (`def load(path, encoding="utf-8")`) is compatible and is not flagged.
- A function **moved** to another file is not a removal.
- A branch that **defines its own** `S` isn't matched against someone else's `S`.

Languages: **Python** (stdlib `ast`, precise) and **JavaScript/TypeScript** (conservative regex parser). Every other file still gets textual and hotspot checks.

### `tangle verify`: dynamic ground truth

Creates one throwaway `git worktree`, then for each branch, each pair (and all branches together) resets to the target, merges, and runs your test command. The pair verdicts:

| verdict | meaning |
|---|---|
| `ok` | pair passes |
| `BREAK` | **both branches pass alone, the pair fails.** This is the one you care about |
| `CONFLICT` | git could not merge the pair |
| `red` | pair fails, but so does one of the branches on its own (not an interaction) |

The worktree is removed afterwards, and your checkout is never touched. With many branches, `--pairs flagged` only tests the pairs `scan` flagged, which turns O(n²) test runs into a handful.

## Use it where agents work

**Before merging a batch of agent branches**

```bash
tangle scan --glob 'claude/*'          # or 'codex/*', 'agent/*', ...
tangle verify --glob 'claude/*' --test "pytest -q" --pairs flagged
```

**In CI, as a PR summary.** See [docs/ci.md](docs/ci.md):

```yaml
- run: tangle scan --glob 'agent/*' --format markdown --fail-on never >> "$GITHUB_STEP_SUMMARY"
```

**As a coding-agent hook.** Let the agent itself find out that a sibling branch changed the ground under it before it says "done". See [docs/agent-hooks.md](docs/agent-hooks.md).

Exit codes: `scan` exits 1 if any finding reaches `--fail-on` (default `high`). `verify` exits 1 on any `BREAK` or `CONFLICT`. Output formats: `text`, `markdown`, `json`.

## Limitations (honest list)

- Static analysis is name-based. Two unrelated functions with the same name in different modules can produce a false positive, and dynamic dispatch (`getattr`, `obj[name]()`) is invisible. `verify` is the ground truth. `scan` tells you where to look.
- Only committed work is compared. Agents that haven't committed yet are invisible (most agent tools commit per task).
- JS/TS parsing is regex-based. It handles functions, arrow functions, classes, methods, and types, but not every syntax form. A tree-sitter backend is the next step.
- Behavioural changes that keep the signature (same parameters, different meaning) are only caught by `verify`, if your tests cover them.

## Roadmap

- [ ] tree-sitter backend (Go, Rust, Java, full TS)
- [ ] `tangle watch`: live warnings while agents are still running, fed from worktree state
- [ ] type-aware checks: changed return types, renamed fields in dataclasses / interfaces
- [ ] GitHub App: comment on every open agent PR that collides with another open PR
- [ ] dataset: replay tangle over public agent PRs and measure how many "clean" merges were semantic breaks

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The end-to-end tests build real throwaway git repositories and cover every finding type, plus the full scan → verify loop on the demo.

## License

MIT
