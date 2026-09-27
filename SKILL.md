---
name: lazy-commander
description: Run long-lived or noisy work with durable state and bounded observation. Use for repeated wait/observe/effect loops, not ordinary bounded steps.
---

# Lazy Commander

Use for persistent work, repeated orchestration, or noisy output; use ordinary tools for known bounded steps.

## State

- Use a task-owned workspace/worktree. Record objective, finish condition, owner, phase, evidence,
  and next action; never borrow another worker's checkout.
- Create a resumable goal only when explicitly requested.
- State owns progress. Events and deadlines wake it; schedules recover orphaned state. A wake
  grants no authority.

## Observe and settle

Use the smallest authoritative observation. Bound unfamiliar first views to 8,000 characters;
keep raw evidence private. Preserve requested values, identities, locations, order, and diagnostic
context. Compress repetition into self-contained blocks; do not infer which content is expendable.

For noisy shell output, use `lc 'COMMAND'`; see [shell output](references/shell-output.md) for
local prefixes, repeats, explicit deltas, and stored expansion.

After a write, send, create, or browser attempt, settle the effect once as `committed`,
`verified-no-effect`, or `ambiguous`. Reconcile ambiguity before retrying. Mechanism never widens authority.

Compact at a fresh durable boundary after settling effects; see
[checkpoint compaction](references/checkpoint-compaction.md). Automate stable repetition as code
with a regression test. Complete a goal only when its finish condition is true.

## Routing

Start with the [mechanism router](references/mechanism-routing.md), then load only the selected path.

- [Broker, leases, privacy](references/broker-contract.md)
- [Carrier controls](references/offload-routing.md)
- [Multi-owner campaigns](references/campaign-controller.md)
- [Owner profiles](references/owner-profiles.md)
