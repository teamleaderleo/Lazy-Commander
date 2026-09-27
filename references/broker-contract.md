# Lazy Commander broker contract

The broker turns repetitive workstation observation into durable private state.
It does not acquire work authority and it never performs a deferred browser,
message, write, or create effect.

## State and wake ownership

The default state root is `~/.codex/state/lazy-commander`. Directories are mode
`0700`; requests, decisions, claims, receipts, bounded views, and raw recovery
logs are mode `0600`. SQLite is the authority for task state. The Unix datagram
socket is only an edge notification to a live worker and can be lost without
losing work.

A task moves through:

```text
queued -> leased -> running -> complete | failed | attention
   ^          |
   +----------+ expired observation lease
```

An operator may additionally settle a non-running task as `ambiguous`,
`complete`, or requeue it at an exact lower-bound time. Claims contain a random
lease token; only its hash is stored in SQLite. A stale, expired, or wrong-attempt
claim fails closed.

## Low-friction commands

Run one command now through admission and bounded output:

```sh
lazy run -- command arg ...
```

The current directory is the default. Add `--cwd /absolute/workspace` only
when the target differs from the tool's working directory.

Persist a proven observation-only command for an exact future time:

```sh
lazy defer --at 2026-09-01T08:00:00Z \
  --cwd /absolute/workspace -- command arg ...
```

`defer` rejects commands whose lack of effects is not mechanically proven.
`lazy tick --worker NAME` collapses due discovery, claim, bounded execution,
and settlement into one transition. `lazy worker --worker NAME` waits on the
next exact deadline or an enqueue notification; it does not poll or invoke a
shell sleep.

`lazy status` is the routine content-free projection: state counts, due count,
next eligibility time, bounded attention IDs, and worker-socket presence. A
state-root lock admits only one resident worker. Execution failures move the
task to attention and malformed work backs off instead of creating a hot loop.

Compile one cross-owner campaign transition without granting execution
authority:

```sh
lazy campaign --input /private/task/snapshot.json
```

The default result is hash-addressed under `campaigns/` in the private state
root. See `campaign-controller.md` for the strict snapshot and replay fences.

Route a proposed browser or tool observation before reading it:

```sh
lazy observe --kind browser --operation domSnapshot --semantic-delta
lazy observe --kind browser --operation locator --selector-bounded \
  --expected-chars 1200
```

The first example refuses the raw DOM path in favor of a semantic delta. The
second admits a small scoped locator read. The command records content-free
request, decision, and receipt artifacts; the browser host still performs the
selected observation.

## Worker boundary

The user service runs only requests admitted at enqueue time with
`observation_only: true`. Immediate `lazy run` remains an explicit caller action
and retains the caller's authority. Browser observations are decisions, not
browser execution. Consequential effects must be performed by their owning
host and settled as committed, verified no effect, or ambiguous before any
retry.

The service is deliberately user-scoped and low-priority. Restart recovery
reopens the same private database and mechanically requeues expired observation
leases. A scheduled task may be used as a coarse orphan-recovery check, but is
not the progress owner or the ordinary wake mechanism.
