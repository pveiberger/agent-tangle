# tangle in CI

Run tangle on every push to an agent branch and publish the cross-branch matrix as a job summary.
tangle needs every agent branch locally, so fetch them all.

```yaml
# .github/workflows/tangle.yml
name: tangle
on:
  push:
    branches: ["agent/**", "claude/**", "codex/**"]

jobs:
  cross-branch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install git+https://github.com/pveiberger/agent-tangle
      - name: create local branches for every remote agent branch
        run: |
          for ref in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin/ | grep -E '^origin/(agent|claude|codex)/'); do
            git branch --force "${ref#origin/}" "$ref"
          done
          git branch --force main origin/main
      - name: static scan (fails the job on high-severity findings)
        run: |
          tangle scan --glob '*/*' --format markdown --fail-on never >> "$GITHUB_STEP_SUMMARY"
          tangle scan --glob '*/*' --fail-on high
```

Add a `tangle verify --pairs flagged --test "<your test command>"` step if your test suite is fast enough.
It only runs the pairs the scan flagged.
