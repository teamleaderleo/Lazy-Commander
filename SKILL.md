---
name: lazy-commander
description: Bound noisy command output and resume long-running jobs. Use for noisy logs or durable waits, not as a default workflow for ordinary coding or project coordination.
---

# Lazy Commander

Use the smallest part that helps: `lc` for noisy output, durable state for a job that must survive a disconnect. Handle ordinary coding and bounded commands directly.

## Keep work moving

Start the next useful authorized action promptly. This skill should not add a ledger, broker,
router, carrier, or scheduling ceremony to a task that can be completed directly.

While a build, review, or download runs, continue independent implementation, verification,
artifact preparation, or another useful task inside the user's scope. Use available parallel
workers when authorized and the work is independent. A pending job is a dependency for its
consumers, not a reason to idle the whole project.

When only waiting remains, retain the job identity and a continuation; resume on completion
or a meaningful deadline. Do not replace progress with repeated unchanged checks, or end an
active assignment at submission when its requested artifact can still be delivered.

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

Use the [mechanism router](references/mechanism-routing.md) only when choosing a durable
wait or advanced orchestration mechanism. A normal command does not need routing.

- [Broker, leases, privacy](references/broker-contract.md)
- [Carrier controls](references/offload-routing.md)
- [Multi-owner campaigns](references/campaign-controller.md)
- [Owner profiles](references/owner-profiles.md)
