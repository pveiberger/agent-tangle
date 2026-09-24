# tangle as a coding-agent hook

The cheapest moment to fix a cross-branch break is *before* the agent reports "done". At that point the
agent still has the full context of its change and can adapt (for example, call `total(items, rate, currency)`
instead of the old signature, or leave a note for whoever merges).

[`examples/hooks/stop_hook.py`](../examples/hooks/stop_hook.py) is a small, agent-agnostic hook. It:

1. finds the branch the agent is on,
2. runs `tangle scan` against its sibling branches (same `--glob`),
3. keeps only the high-severity findings that involve *this* branch,
4. blocks the stop with an explanation the agent can act on (exit code 2 + stderr), or lets it stop (exit 0).

It lets the stop through if the hook already fired once in this turn, so an unfixable collision cannot trap
the agent in a loop.

## Claude Code

`.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "python examples/hooks/stop_hook.py --glob 'claude/*'" }
        ]
      }
    ]
  }
}
```

## Other agents

Any agent runner that can execute a command at the end of a task and read its stderr can use the same
script. It reads optional JSON on stdin and only looks at `stop_hook_active`, so it works with no stdin at all.
Codex CLI, Gemini CLI, and Cursor all offer comparable hook points.
