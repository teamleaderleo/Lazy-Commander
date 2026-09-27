# Per-thread compaction listener

Use one passive listener on the existing Codex app-server connection. It is
resident plumbing, not a scheduled job:

```text
Desktop <-> transparent proxy <-> Codex app-server
                 |
                 +-- one small reducer state per root thread
```

The reducer watches events already crossing the connection. A completed root
turn arms a candidate. The following `thread/status/changed: idle` emits one
content-free wake. It ignores subagent threads, active turns, failed turns,
compaction turns, and duplicate idle events. `idle` proves that an effect is
safe to attempt; it is not a polling interval.

A user-wide Codex `Stop` hook reserves the fresh generation just before idle.
It hashes the final message and transcript identity but retains neither body.
The listener projects actual plan/goal tool calls into enums such as
`phase-changed`; a quote/comment-aware scanner ignores commands that merely
mention those tool names. A routine Stop defaults to boundary `no`; only
structured phase evidence promotes it to classifier-worthy `uncertain`.

The proxy then joins that wake to the owning thread's latest monotonic durable
checkpoint. Deterministic state resolves obvious boundaries. Only an uncertain
boundary may call the closed `now | soon | keep` classifier. Revalidate that
the same candidate is still idle before applying a classifier result.

Uncertain classification runs on a background thread; app-server forwarding
does not wait for Luna. The card contains booleans, effect state, goal-active
state, and at most eight boundary-evidence enums. It contains no conversation,
repository content, or tool output. A newer turn, checkpoint generation,
compaction, or non-idle status makes the result stale and non-authorizing.

## Components

- `scripts/compaction_thread_listener.py`: JSON-RPC event reducer.
- `scripts/compaction_owner_checkpoint.py`: atomic private checkpoint writer.
- `scripts/compaction_app_server_proxy.py`: transparent listener and guarded
  `thread/compact/start` caller.
- `scripts/compaction_advisor.py`: deterministic policy and classifier result
  validation.
- `scripts/compaction_classifier.py`: bounded Luna runner and private metrics.

Private state defaults to
`~/.codex/state/lazy-commander/compaction-proxy`. Receipts contain identifiers,
generations, decisions, and effect settlement only. They never retain message
bodies, raw tool output, or repository content.

## Desktop wiring

Desktop honors `CODEX_CLI_PATH`. Point it to the proxy and explicitly point the
proxy at the current standalone Codex binary:

```bash
CODEX_CLI_PATH=/absolute/path/compaction_app_server_proxy.py \
LAZY_REAL_CODEX_CLI=$HOME/.local/bin/codex \
chatgpt
```

Non-app-server commands pass straight through. Observe mode is the default.
`LAZY_COMPACTION_APPLY=1` permits a deterministic `compact` decision to issue
the request. Do not enable apply until an observe replay is clean.

The Linux Desktop bundle inspected on 2026-08-31 contains Codex
`0.150.0-alpha.12.2`; the machine standalone is `0.151.0`. The proxy must chain
to `0.151.0`, not the older bundle. Generated protocol schemas confirm the
listener's root notification envelopes are stable across those versions, and
an isolated `0.151.0` initialize handshake succeeds through the proxy.

Post-restart proof:

```bash
python3 scripts/compaction_proxy_status.py \
  --require-active --require-apply --require-version 0.151.0 \
  --require-root-idle
```

One JSON row. `rootIdleObserved:true` proves a real root turn traversed the
listener; the receipt retains only a thread hash. Nonzero means rollout not
proven. Read raw process/log state only to recover that failure.

## Effect safety

Write `issued` before sending. Commit only after the internal request is
accepted, the context-compaction item and turn complete, and that thread
returns idle. An error, timeout, restart with issued-but-uncommitted state, or
unclear notification is ambiguous: reconcile; never retry automatically.

One direct Luna CLI probe reported about 12K tokens used for a tiny card. That
is a retained negative result. Do not invoke Luna on every wake. Resolve
obvious `yes`/`no` state deterministically and reserve asynchronous Luna calls
for genuinely uncertain boundaries.

Controlled 0.151/Luna replay on 2026-08-31 returned all three contract states
and the live automatic phase-change case:

```text
fresh + phase-complete        -> now  (11,639 reported tokens; 10.2s)
fresh + active-phase          -> keep (11,840 reported tokens; 8.6s)
missing + phase-complete      -> soon (11,891 reported tokens; 9.6s)
active + detected plan change -> now  (11,841 reported tokens; 8.7s)
```

The exact classification is retained once per checkpoint generation and reused
rather than paying Luna again. `soon` persists an immutable checkpoint-first
latch; only a newer ready generation advances it to compaction.
