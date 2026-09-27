# Lazy offload routing

Use `lazy route` when the handoff cost is uncertain. The router consumes only
structured task properties and emits one of three routes:

- `codex-local` for current novel judgment, repository mutation, live browser
  state, sensitive material, or a consequential effect;
- `workstation-worker` for a deterministic observation or available owner
  query;
- `chatgpt-carrier` for background, independent, long-context work that has a
  concrete retained-context advantage and no current effect or mutable-repo
  dependency.

A short follow-up may also use the carrier when it is an exact continuation of
an already-retained conversation and rehydrating that context in Codex would
cost more than the handoff. Mark that case explicitly with
`--retained-carrier-continuation`; the flag is not a general small-task offload
escape hatch and still requires `--carrier-context` plus all ordinary safety
fences.

The CLI also emits a strict zero-authority `campaign_summary` with the route
run reference, generation, route, and exact request fingerprint. Use that
summary when composing `lazy campaign`; do not guess which routing SHA binds a
later action-time confirmation.

The carrier route is deliberately conservative. It does not mean “send now.”
Browser message delivery remains a separately confirmed effect, and routine
return reads use an Elatura semantic delta. Wake on a material application
event, an operator return, or an explicit decision—not repeated transcript
reads.

For routine Codex review or research, `chatgpt-carrier` means a fresh chat
created inside one dedicated ChatGPT project reserved for carriers (configure
its name and `g-p-...` ID locally). A continuation may reuse only the
exact chat created for that dispatch. Never select a recent, idle, unrelated,
human-owned, or differently scoped chat; never move one into the project. If
the project cannot be proved or its fresh composer is not empty, fall back to
`codex-local` self-review without touching another chat.

After an attempted carrier send, run `lazy settle-route --run-id RUN_ID
--outcome committed --conversation-ref OPAQUE_REF`, or settle it as
`verified-no-effect` or `ambiguous`. Settlement is one-shot. Neither a
verified no-effect nor an ambiguous attempt authorizes a retry; the former
needs a new action-time confirmation, while the latter must reconcile the
current conversation identity and visible state first.

For retained-browser execution, prepare and issue the exact operation through
`scripts/carrier_operation.py` before calling `scripts/carrier_turn.mjs`.
First feed externally triggered, content-free capability observations to
`scripts/control_stability.py`. The default status needs three distinct
successes spanning at least 60 seconds and expires five minutes after the last
observation. Failure or expiry requests rotation; insufficient evidence waits
for another material event. It never polls or sends. Observations bind browser,
surface, project, lane generation, exact control inventory, and probe version.
`status` stores the approved-policy receipt in its private ledger; pass that
exact receipt and state root to `carrier_operation.py gate` with the inventories.

Gate acceptance is durable, expiring, and bound to the stability event set.
`issue` requires its exact digest and reprojects the control ledger at issue
time; a fabricated receipt, policy downgrade, new failure, scope drift, or
expired proof refuses before an attempt reference exists. Exact replay after
issue returns the stored attempt even after gate expiry. Only the first receipt
with `effectAttemptAdmitted=true` may enter the sender.
The helper requires the matching stable control generation, dedicated lane
generation, empty composer, explicit deadlines, operation marker, and built-in
private sink; its control result contains no answer body. If control stability,
lane, or confirmation changes before issue, cancel the still-prepared
operation; cancellation is terminal. On restart, issued/ambiguous state
reconciles only and committed/pending state observes only. Neither recovery
path navigates, fills, clicks, or re-enters the sender.

After a workstation worker returns, settle its execution once with the bounded
artifact hash and byte count. Use `failed` only for verified failure and
`ambiguous` when execution may have happened. Settlement accepts no raw output,
authorizes no redispatch, and wakes only owner acceptance/reconciliation.

## Retained lane budget

`lazy plan-lanes` consumes only opaque lane identity/generation, residency,
activity, protection, blockers, and last-use time.

The default policy keeps at most 12 responsive lanes and at most 20 retained
lanes. These are workstation dogfood ceilings, not provider quotas or product
claims. At the soft cap, the planner requests suspension of the least-recently
used safe idle lane before admission. At the hard cap, it requests closure of
safe reclaimable/suspended lanes. Protected, generating, changed, unsaved,
composing, modal, media, download, or unknown-blocker lanes are not automatic
cleanup candidates.

The planner never performs a tab/lane effect. Elatura owns lifecycle
eligibility and settlement. Stensibly owns whether a task is eligible and may
dispatch. A requested park/close must be observed and settled before the new
carrier is admitted.

Do not infer why a provider throttles or limits a surface from one symptom.
Measure actual admitted sends, bounded-read failures, material events, and
resource cost separately. The optimization target is fewer unnecessary reads
and less Codex context churn, not evasion of provider controls.

## Browser control-service rotation

Control services are runtime infrastructure, not carrier lanes. Use `lazy
plan-controls --inventory private-controls.json` with content-free service
generation, role, heartbeat, inventory-verification time, surface count,
protection, and blockers. The default freshness lease is five minutes.

Keep a fresh inventory-verified primary. When it goes stale, request a new
window or control session as a successor but retain the predecessor. Promote
the successor only after it proves browser inventory access; retire the
predecessor afterward and settle both effects. A starting-but-unverified
successor means wait, not another window. At most one temporary successor is
useful during rotation. Extension-owned control surfaces never count toward
the 12/20 carrier budget.

Freshness is only a lease, not capability proof. Record one stability event
after an actual inventory/selector/composer/send-control probe. Exact event
replay is byte-stable; changed reuse conflicts. Gate evaluation must use the
same control identity and inventory observation time. A future expiry deadline
can wake rotation, but cannot manufacture another successful observation.
