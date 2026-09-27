# Cost-aware campaign controller

`lazy campaign` composes owner summaries into one replay-safe transition. It
does not replace Stensibly work/claim/run authority, Elatura lane and semantic
delta truth, Alalana connector cursors, research identity, or a workspace
portfolio view.

## Input boundary

Use schema `lazy-campaign-controller-snapshot/v2`. The strict input contains:

- campaign and step opaque references, generations, and lifecycle status;
- expected input/output character counts and a Codex token budget;
- zero-authority route, lane-plan, and control-plan summaries with exact
  generations and SHA-256 fingerprints;
- one owner event and, only for an exact deadline event, `deadlineAt`;
- one effect settlement: `none`, `issued`, `ambiguous`, `committed`, or
  `verified-no-effect`;
- one optional material semantic-delta generation/fingerprint; and
- an owner-scoped cursor vector of up to 32 unique
  `ownerRef + generation + fingerprint` records, normalized by owner identity;
  and
- one optional action-time confirmation generation bound to the exact route
  request fingerprint and, on retry after verified-no-effect, the exact prior
  effect reference and generation.

`actionTimeConfirmation` has the exact fields `confirmed`, `ref`, `generation`,
`requestFingerprint`, `afterEffectRef`, and `afterEffectGeneration`. An
unconfirmed record sets the other five fields to null. An initial confirmation
sets the prior-effect pair to null; a post-verified-no-effect confirmation sets
both to the settled effect identity.

Commands, prompts, conversation text, tool payloads, paths, credentials, and
provider pricing are outside this contract. The portable estimate is
`ceil((expectedInputChars + expectedOutputChars) / 4)`. It is a reservation
unit, not a billing or price claim.

The cursor-vector fingerprint is bound into the snapshot and acceptance
proposal. One owner advancing therefore rotates the proposal without pretending
that unrelated owners advanced. Legacy v1 snapshots remain readable as an empty
cursor vector for receipt recovery; create new campaigns with v2.

`lazy route`, `lazy plan-lanes`, and `lazy plan-controls` each emit a
`campaign_summary` that can be copied directly into the corresponding strict
snapshot field. Do not invent generations or choose between request and
decision hashes manually.

## Safety order

The compiler returns one of `wake`, `observe`, `effect`, `reconcile`,
`complete`, or `blocked`.

1. Terminal and inactive campaign state settle first.
2. Codex-local work must fit the supplied token budget.
3. Issued or ambiguous effects reconcile before any retry.
4. A committed effect waits for a changed material semantic fingerprint.
5. An exact future deadline returns one `wake` with that timestamp; a due
   deadline requests one bounded owner observation.
6. A ChatGPT carrier requires a ready lane, a ready control service, and a
   current confirmation. A deterministic worker does not depend on browser
   health.
7. Wall-clock `observedAt` does not change semantic identity. When
   `previousSnapshotSha256` matches the normalized snapshot, an otherwise
   eligible observation or effect becomes `wake`; exact replay never
   redispatches. A deadline crossing rotates the separate decision fingerprint
   exactly once, so a due owner observation is not suppressed as a replay.

Effect reconciliation outranks token-budget refusal: an existing ambiguous
attempt must never disappear behind a resource gate. A confirmation with a
mismatched request fingerprint or partial prior-effect binding fails input
validation rather than becoming effect advice.

Every view and receipt fixes `rawContentEmitted`, `authorizesWork`,
`authorizesEffects`, and `authorizesDispatch` to false. The actual owner must
reserve and perform any advised effect, then feed the exact settlement into a
new generation.

Each compiled view also carries an `acceptanceProposal` binding the transition
ID to the evaluated campaign generation, normalized snapshot fingerprint, and
requested Codex reservation. To exercise the workstation-local acceptance
contract:

```sh
lazy accept-campaign --view /private/task/result/view.json \
  --owner-generation 7 --owner-fingerprint SHA256 \
  --budget-ref budget:campaign --budget-generation 4 \
  --budget-limit 50000 --budget-reserved 1200 --budget-consumed 8000
```

The broker uses one immediate SQLite transaction to compare-and-swap the
budget and insert the transition acceptance. Exact replay is idempotent; a
different view or stale owner/budget input conflicts without another
reservation. The ledger is a local proof and recovery surface, not a substitute
for Stensibly's canonical cross-project acceptance authority. `lazy status`
shows bounded budget counters and the acceptance count without exposing task
content.

## Durable use

```sh
lazy campaign --input /private/task/snapshot.json
```

Without `--output-dir`, artifacts are written under the private Lazy state root
at `campaigns/<decision-fingerprint>/`. Exact replay returns the existing artifacts;
a changed result at the same explicit output directory fails as a conflict.
Each result directory is mode `0700`; `view.json`, `view.md`, and
`receipt.json` are mode `0600`.

Views record only the material temporal class, not the raw observation
timestamp. Timestamp-only refreshes therefore reuse identical artifacts;
deadline-future and deadline-due decisions remain distinct.

Use `view.md` for routine reasoning. Use `view.json` only when another
deterministic owner needs the structured counters or transition. Do not read
the source owner payload again when the snapshot is unchanged.
