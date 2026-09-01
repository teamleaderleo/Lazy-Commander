# Semantic command replay benchmark — 2026-08-31

This receipt records the benchmark behind Lazy Commander's first public output-compression claim.

## Corpus

A three-day Codex history replay on a heavily used Linux workstation contained **14,146 shell executions** and **110.4 million output characters**.

- median command output: 1,084 characters;
- p90 command output: 20,205 characters;
- maximum command output: 1,048,603 characters;
- compound shell commands: 54.64% of the initial 14,106-command census.

## Promoted treatment

The semantic-command layer stores an admitted shell command in private mode-0600 state, runs it locally, retains exact raw output privately, and returns a bounded semantic receipt.

Admission is deliberately conservative. It skips interactive commands and approval-bearing sessions, and leaves content-bearing source, diff, log, and GitHub-body reads alone when that content is the evidence the worker needs.

The promoted replay admitted **2,437 commands (17.23%)**.

| Metric | Raw | Semantic view |
| --- | ---: | ---: |
| Total characters | 13,578,014 | 913,664 |
| Mean characters per command | 5,571.6 | 374.9 |
| Reduction | — | **93.27%** |
| Maximum projected view | — | **< 4,000 characters** |

## Reserved-signal checks

Across admitted commands, the projection retained:

- plain errors: **431 / 431**;
- test summaries: **391 / 391**;
- current branch results: **716 / 716**;
- structured GitHub state/status/conclusion: **250 / 250**;
- nonempty successful output with an empty projection: **0**.

A further **564** body/diff-style reads were deliberately left unwrapped.

## Rejected treatment

The first broad admission pass wrapped 3,534 commands and compressed their output by **93.19%**, but it could hide content-bearing `git diff`, `git log`, and GitHub body reads. Tail-priority selection could also miss earlier reserved signals.

That treatment was rejected. Admission was narrowed and reserved signal slots became part of the projection before promotion.

## Live check

A live pull-request + branch + head readback rendered as four semantic lines behind one receipt id while keeping exact output private. A fresh Codex session also accepted the workspace-generated hook and rewrote a Git read through the same receipt path.

## Interpretation

This benchmark measures **model-visible command-output reduction**, not command execution speed. The result supports a narrower claim: for the admitted command family on this replay corpus, Lazy Commander reduced automatic visible output by 93.27% while retaining every checked reserved signal and keeping exact raw evidence recoverable.
