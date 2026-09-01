# Lazy Commander

**Keep exact command output recoverable while giving coding agents only the part worth their attention.**

Lazy Commander is a local-first command and task layer for coding-agent workflows. It can run noisy or persistent work, keep exact raw output in private local receipts, and return a bounded semantic view for the agent to consume. The short `lc` path is designed for ordinary shell work; longer-running orchestration keeps durable state and explicit finish conditions.

The core idea is simple:

```text
command / task
-> run locally
-> retain exact raw evidence privately
-> classify and compress routine output
-> surface errors, test summaries, state changes, and other useful signals
-> keep the raw receipt recoverable by id
```

## Measured result

A three-day replay on a heavily used Linux workstation contained **14,146 shell executions and 110.4 million output characters**. The promoted conservative admission policy selected 2,437 commands whose output was suitable for semantic projection.

| Metric | Raw | Lazy Commander view |
| --- | ---: | ---: |
| Output characters for admitted commands | 13,578,014 | 913,664 |
| Mean characters per admitted command | 5,571.6 | 374.9 |
| Reduction | — | **93.27%** |
| Maximum projected view | — | **< 4,000 characters** |

Reserved-signal replay checks retained:

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

## Command model

The compact shell path is `lc`:

```text
lc 'COMMAND'
lc -C /path/to/repo 'COMMAND'
lc -r 'COMMAND'
```

By default, Lazy Commander emits a semantic receipt and keeps the complete raw output private. `-r` exposes raw output inline only within the configured bound. A receipt id can be reopened when exact evidence is needed.

Persistent work uses durable state with an objective, finish condition, current phase, evidence, and next action. Repeated effects are settled explicitly so an ambiguous write/send/create attempt is reconciled before another attempt.

## Design principles

- **Raw evidence stays recoverable.** Compression is a view, never destruction of the underlying command result.
- **Bound the automatic view.** Unknown or noisy output should not receive an unlimited model-context budget.
- **Keep consequential signals.** Errors, failed tests, state changes, and explicit status are selected before routine success noise.
- **Leave content-bearing reads alone.** Source, diffs, logs, and message bodies are often the evidence itself.
- **Use ordinary tools for ordinary work.** Persistence and orchestration are for genuinely long-lived or repeated work, not every shell command.
- **Measure the treatment.** A smaller projection that hides needed information loses.

## Status

Lazy Commander is being extracted from a heavily dogfooded private coding-agent environment into this public repository. The benchmark above comes from that running implementation; the public packaging and standalone installation surface are being separated from private workstation and conversation-specific policy.
