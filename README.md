# Lazy Commander

**Remove repetitive command output while preserving the evidence agents need to act.**

Lazy Commander is a local-first command and task layer for coding-agent workflows. It can run noisy or persistent work, keep exact raw output in private local receipts, and return a bounded semantic view for the agent to consume. The short `lc` path is designed for ordinary shell work; longer-running orchestration keeps durable state and explicit finish conditions.

The core idea is simple:

```text
command / task
-> run locally
-> retain exact raw evidence privately
-> preserve ordered content and fold reversible repetition
-> surface errors, test summaries, state changes, and other useful signals
-> keep the raw receipt recoverable by id
```

## Historical projection replay

A three-day replay on a heavily used Linux workstation contained **14,146 shell executions and 110.4 million output characters**. The promoted conservative admission policy selected 2,437 commands whose output was suitable for semantic projection.

| Metric | Raw | Lazy Commander view |
| --- | ---: | ---: |
| Output characters for admitted commands | 13,578,014 | 913,664 |
| Mean characters per admitted command | 5,571.6 | 374.9 |
| Reduction | — | **93.27%** |
| Maximum projected view | — | **< 4,000 characters** |

The older projector’s reserved-signal replay checks retained:

- **431 / 431** plain errors;
- **391 / 391** test summaries;
- **716 / 716** current-branch results;
- **250 / 250** structured GitHub state/status/conclusion signals;
- **0** nonempty successful outputs collapsed to an empty projection.

The broader first pass selected 3,534 commands and also compressed output by about 93%, but it could hide content-bearing `git diff`, `git log`, and GitHub body reads. That treatment was rejected. The promoted path deliberately leaves those reads unwrapped and reserves signal slots before trimming routine output.

This benchmark measures model-visible command output, not command execution speed. Exact raw output remains locally recoverable. See the [full replay receipt](docs/benchmarks/semantic-command-replay-2026-08-31.md).

## What it is good at

Lazy Commander is aimed at command families where exact output is valuable for recovery but wasteful as automatic model context:

- test and build output;
- package-manager and setup logs;
- repeated status/check polling;
- Git and GitHub state queries;
- searches with many repeated matches;
- long-running or repeatedly observed work;
- bounded task receipts that another agent can resume from.

It deliberately avoids compressing source-, diff-, log-, or body-bearing reads when the content itself is the evidence the agent needs.

These figures describe the earlier projector, not a current fidelity guarantee. Later auditing found
that its allowlists and line limits could discard requested bodies, identities, and diagnostic
context despite passing those checks. The current implementation uses a faithful bounded view and
explicit expansion instead; the historical reduction percentage is not a target.

## Command model

The compact shell path is `lc`:

```text
lc 'COMMAND'
lc -C /path/to/repo 'COMMAND'
lc -r 'COMMAND'
lc s -r ID --offset 0 --limit 7000
```

The ordinary view preserves ordered output, requested JSON values, identities, locations, and diagnostic context within a total character budget. It folds adjacent repetition and path prefixes reversibly, and marks any gaps. `-r` exposes raw output inline within the configured bound. Use the receipt id to page through captured bytes without executing again; continuation commands appear on stderr. `lc s -r ID` recovers all captured bytes. A capture-limit stop is explicitly reported as incomplete.

Persistent work uses durable state with an objective, finish condition, current phase, evidence, and next action. Repeated effects are settled explicitly so an ambiguous write/send/create attempt is reconciled before another attempt.

## Design principles

- **Raw evidence stays recoverable.** Compression is a view, never destruction of the underlying command result.
- **Bound the automatic view.** Unknown or noisy output should not receive an unlimited model-context budget.
- **Preserve the evidence.** Requested content and diagnostic context stay visible; key names, counts, and a few error lines are not substitutes.
- **Leave content-bearing reads alone.** Source, diffs, logs, and message bodies are often the evidence itself.
- **Use ordinary tools for ordinary work.** Persistence and orchestration are for genuinely long-lived or repeated work, not every shell command.
- **Measure the treatment.** A smaller projection that hides needed information loses.

## Install `lc`

Python 3.10+ and nothing else. From a clone:

```bash
git clone https://github.com/teamleaderleo/Lazy-Commander.git ~/Projects/Lazy-Commander
python3 ~/Projects/Lazy-Commander/scripts/semantic_command.py install --user
```

This links `~/.local/bin/lc` to the clone and adds an opt-in Codex `PreToolUse` hook to
`~/.codex/hooks.json` that routes noisy shell commands through `lc`. Use
`install --workspace PATH` instead to scope the hook to one project. Any agent (or you) can also
just call `lc 'COMMAND'` directly; Claude Code needs no hook for that.

Receipts live under `~/.codex/state/lazy-command` (override with `LAZY_COMMAND_STATE_ROOT`).
[docs/shell-output.md](docs/shell-output.md) documents the view notation and stored expansion.

Run the tests with `python3 -m unittest discover -s tests`.

## Status

The `lc` shell path above is published here and is what the author runs daily. Lazy Commander's
persistent-work layer (durable task state, brokers, compaction) is still being extracted from a
private coding-agent environment and will follow.
