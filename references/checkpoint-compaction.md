# Checkpoint compaction

Goal: roll context at cheap, safe task boundaries. Keep state, drop transcript.

## Decision

Run `scripts/compaction_advisor.py` after a state transition, never on a timer.

```text
unsettled effect                         -> reconcile
boundary yes + fresh checkpoint          -> compact now, at any window use
boundary yes + no fresh checkpoint       -> checkpoint now, then compact
boundary uncertain                       -> cheap now/soon/keep classifier
boundary no + low pressure                -> continue
70%+, no fresh durable checkpoint        -> checkpoint now
70%+, fresh checkpoint                   -> compact as a pressure backstop
same checkpoint generation               -> don't compact it twice
```

Semantic expiry is the primary trigger; percentage is only a safety backstop.
Record quiet decisions. Tune from receipts. `usedWindowPct` is total model
input; it is not Codex's `body_after_prefix` charge.

Classifier sees only a small closed card such as:

```json
{
  "checkpointFresh": true,
  "priorPhaseComplete": true,
  "phaseChanged": true,
  "nextActionPresent": true,
  "unsettledEffect": false,
  "longRunningGoalActive": false,
  "sameTaskFamily": true
}
```

Owner checkpoints may add up to eight `boundaryEvidence` enums through
`compaction_owner_checkpoint.py --evidence ENUM`; free-form evidence is
rejected. The resident proxy runs uncertain classification asynchronously,
rechecks the exact idle turn and generation, and retains one closed result per
generation.

Classifier `codex exec` disables `apps`, `plugins`, `shell_tool`, and
`image_generation`. The closed schema cannot use those capabilities. Its
receipt retains fixed input/cache/output token counters; don't infer input cost
from the old text `tokens used` total.

Luna distinguishes boundary from active phase. Code derives boundary timing:
ready fresh checkpoint becomes `now`; missing recovery state becomes `soon`.
The receipt retains Luna's closed answer and whether code normalized it.

Ask `compact before the next expensive turn?`; accept only:

```text
now  + stable-boundary  -> compact if checkpoint fresh; otherwise checkpoint
soon + checkpoint-first -> latch once; fresh generation advances it to compact
keep + active-phase     -> continue
```

`soon` is not permission for unrelated work. Version 1 supports only the
observable `checkpoint-first` prerequisite; add another prerequisite only
with a deterministic completion field and regression test. Do not send
transcript, raw logs, tool output, or repository content. The classifier
neither summarizes nor writes the checkpoint. Deterministic state owns
objective, completed effects, active assumptions, identities, blockers,
evidence, and next action.

## Execute

Compact only while the thread is idle. Controller flow:

```text
turn completed -> checkpoint generation -> advise
compact -> thread/compact/start -> thread/compacted -> settle generation
```

`compaction_advisor.py --classification RESULT` resolves the closed classifier
result. If it emits `pending`, retain that exact record. After the checkpoint
event, `--pending RECORD` advances the latch; it authorizes compaction only when
the generation moved and the checkpoint is ready.

"Listen" on meaningful events: a turn completes, a goal or subgoal changes
phase, an effect settles, a checkpoint generation advances, or a new request
arrives after completion. Do not poll a timer or classify every tool call.
Single-flight the decision and deduplicate by checkpoint generation.

For Codex 0.151, a project `Stop` hook is the smallest content-safe event
adapter. Pipe its JSON stdin through `scripts/compaction_stop_event.py`. It
retains exact thread/turn identity and message hashes, never bodies. The hook
runs before Codex clears its active-turn slot, so its receipt deliberately says
`idleProven: false` and never authorizes compaction. It also reserves one
idempotent monotonic neutral checkpoint generation. Structured phase evidence
promotes the effective boundary to uncertain; an ordinary Stop stays quiet. A
runtime caller must still prove idle on the same app-server connection.

For the resident same-connection adapter, read
`references/per-thread-compaction-listener.md`. It describes the transparent
Desktop proxy and per-root-thread passive reducer. Do not reopen this reference
for ordinary compaction decisions.

A long-running goal does not pin cold transcript. At an internal phase boundary,
checkpoint the next action and compact while idle; the durable goal resumes in
the new window. Never interrupt an active model or tool turn to compact.

No automatic retry after timeout or ambiguous notification. Reconcile thread
and window identity first.

Codex 0.151 facts:

- `model_auto_compact_token_limit` and scope `total|body_after_prefix` exist.
- Manual app-server compaction is `thread/compact/start`.
- `PreCompact` can veto; `PostCompact` observes settlement. Neither chooses the
  boundary.
- Experimental token-budget mode exposes `new_context`; that tool starts a
  fresh window without summarizing history. Do not enable it globally. Its
  extra prompt/tool contract and checkpoint quality need a controlled trial.

Measure a rollout without reading bodies:

```bash
python3 scripts/compaction_activity_view.py ROLLOUT --output-dir RECEIPT_DIR
```

The projector emits aggregate timing, input ratios, reclaimed tokens, bounded
rows, omissions, and hashes. Raw conversation content stays private.

## Research handoff

Use the existing offload router. Narrow exact lookup stays local. Unknown or
large multi-repository/web synthesis can use a retained carrier when it earns
the handoff. Give it a fixed question and bounded evidence contract; consume a
semantic delta, not its whole transcript. Carrier output larger than the
direct evidence is a routing failure worth retaining.
