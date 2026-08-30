# Satellite write path is a scoped hybrid, not a per-customer install (decision)

**Status:** decided — operator determination of record; governs the satellite write path
**Date:** 2026-08-29
**Goal:** governed remote mutations — reachability prerequisite of the management-plane-lockdown Goal (evoila-bosnia/meho-internal#234)
**Initiative:** [#2901](https://github.com/evoila/meho/issues/2901) — satellite write path, security-review-first design
**Composes with:** [#3183](https://github.com/evoila/meho/issues/3183) (delete-shaped operations — mandatory-human-approval + preview-hash + blast-radius + dedicated safety tier)
**Design:** [docs/research/2901-satellite-write-path.md](../research/2901-satellite-write-path.md) (threat model T1–T8, the four mechanisms, `file:line` grounding)

## The determination

> The satellite runner will execute **write** operations against estates the
> central backplane cannot dial via a **scoped hybrid**: writes are minted to a
> satellite **only** for an enumerated set of low-risk op-classes, on **per-runner
> enrollment allowlists**, rolled out in stages, with **delete-shaped operations
> never minted to a satellite**. We do **not** default to a per-customer
> full-backplane install (that remains a fallback only for a customer whose risk
> posture rejects the composed control set). The push-only/poll boundary
> ([#2877](https://github.com/evoila/meho/issues/2877)) is preserved: no inbound
> listener on any runner.

The write tier is gated by a **composition of four mechanisms** — none is
sufficient alone; the security comes from the composition (design doc §3):

1. **Signed, single-use, expiring work items** verified at the edge
   (**write-tier only**), plus centrally-parked approval binding for the caution
   sub-tier.
2. **Per-work-item short-lived, response-wrapped Vault credentials** — no
   standing runner creds; this closes the empty-JWT edge-credential gap.
3. **Per-runner enrollment allowlist** as the blast-radius bound — checked at
   mint *and* re-checked at the edge.
4. **Audit §6 recorded exception** — tamper-evident store-and-forward effect
   audit + a security alarm on minted writes unreported past expiry.

## Why not a per-customer install (the default rejected)

- #2901's scope update (2026-08-12) committed to the **execution-point model**:
  environments own their execution point, and a satellite is the *second kind* of
  execution point resolved per-environment. A per-customer full backplane
  duplicates the entire control plane (Postgres, policy, approvals, audit,
  Keycloak) per customer and contradicts "no hand-managed satellite inventory at
  fleet scale."
- The management-plane-lockdown Goal (evoila-bosnia/meho-internal#234) names the
  **satellite write path** — not a per-customer install — as its keystone: its
  enforcement stages cannot deny operator-direct reach anywhere the central
  instance does not dial directly until governed remote mutations exist.
- Per-customer install is not deleted from the option set — it stays the
  **fallback** for a customer estate whose risk posture judges the four composed
  controls insufficient.

## Why the four mechanisms, and why the #2500 reversal for the write tier

The organising fact is **read/write asymmetry** (design doc §2): a read at a
compromised, silent edge changes nothing; a write at a compromised edge mutates
the estate the instant the handler runs, off-net, before any result is posted.
The single-use consume latch protects against double *acceptance*, not against
*execution-without-report*. So the write path cannot be secured by making the
edge more trustworthy — only by minimising what any edge can be authorised to do,
minimising how long a captured credential is useful, and guaranteeing the centre
always knows a capability was granted and can detect a missing report.

- **Signature reversal (mechanism 1) — deliberate reversal of #2500 for the write
  tier only.** [#2500](https://github.com/evoila/meho/issues/2500) decided the
  capability token is a bare DB-row PK, not a JWT, because for a `safe` read an
  edge-verifiable signature bought nothing over the central DB latch. For a
  **write** the consequence changed: the signature gives the edge an offline
  integrity + freshness + target-scope check (against T2) and a non-repudiation
  anchor for the store-and-forward effect audit (mechanism 4). The reversal is
  scoped to the write tier; the DB consume latch stays for at-most-once acceptance
  — a signature does not replace central state, so the #2500 rationale still holds
  for *that* property.
- **No standing runner creds (mechanism 2).** Standing broad write credentials on
  a fenced host are the T3 worst case. Per-work-item, single-target-scoped,
  response-wrapped credentials (TTL ≤ capability expiry, unwrapped just-in-time)
  bound the exposure to one target for minutes, and the just-in-time unwrap is an
  outbound fetch — push-only preserved. This also closes the empty-JWT gap where
  a credentialed op cannot resolve its secret on the runner today (design doc
  §1.4).
- **Allowlist as blast radius (mechanism 3).** A compromised runner can do
  anything *within* its allowlist and nothing outside it, so the allowlist **is**
  the blast-radius definition (T1, T8) and must be minimal per environment and
  bound at enrollment (not self-widenable by the runner).
- **Audit §6 exception (mechanism 4).** True synchronous-at-centre audit of the
  remote **effect** is structurally impossible (the executing side has no DB, the
  mutation is off-net), so v0.1-spec §6 cannot hold for the effect. This decision
  **consciously records the exception**: the mint stays synchronously audited at
  authorization, the effect is store-and-forward with a hash chain (transit
  tampering and dropped records detectable), and an un-reported-mint alarm
  (a `deadman.py`-mould sweeper over minted writes past `expires_at` with
  `consumed_at IS NULL`) fires a **security** event distinct from the liveness
  flip. Audit makes the edge's silence and tampering *observable*; it does not
  make a lying edge honest — that residual is bounded by the composition (allowlist
  × credential TTL).

## Tiered mint + staged rollout (the mechanics of record)

`mint_gateway_command` grows from a binary safe-wall into a tier ladder:

- `safe` → mint on `AUTO_EXECUTE` (unchanged).
- `remote-write` (enumerated low-risk classes) → mint **only** when the op-class
  is on the runner's allowlist **and** (policy `AUTO_EXECUTE` for the idempotent
  subset, later stages) **or** a committed `ApprovalRequest` (caution subset).
  Work item is **signed**.
- `dangerous` / delete-shaped → **never minted to a satellite** in this rollout;
  stays central-or-break-glass per #3183.

Stages (a domain moves one at a time, mirroring #234's ratchet): **Stage 0**
reads only (today) → **Stage 1** one idempotent reversible class, one environment,
per-write human approval mandatory → **Stage 2** the enumerated class list,
`AUTO_EXECUTE` for the idempotent subset only → **Stage 3** standing capability
within-allowlist, delete-shaped permanently central-or-break-glass.

## Recommendations the operator vetoes on review

Three sub-points are recorded here as recommendations, resolved on PR review:

1. **Stage-1 op-class** — **one** idempotent, reversible op-class as the single
   Stage-1 class (e.g. a tag/annotation set). The operator picks the exact class.
2. **Programmatic-enrollment write scope** (T7) — programmatic enrollment **can
   never grant write capability at birth**; a write allowlist requires a separate
   human step after enrollment.
3. **Revocation latency** (T8) — **TTL + dead-man initially**, with a **per-mint
   revocation check** as the **Stage-3 gate**, so a revoked runner cannot execute
   an already-minted write once the tier reaches steady state.

## Scope / non-goals

- **This decision records the design, not the code.** The implementation seams are
  filed as sibling Tasks under #2901, unimplemented. This initiative is the
  design/decision home.
- **No relaxation of the read path.** The three safe-only walls (central mint,
  edge executor, assignment materialiser) stay; the write tier is a *new* gate
  alongside them, not a loosening of the safe wall.
- **Push-only stands.** Nothing here adds an inbound listener to a runner
  (#2877's boundary).
- **Delete-shaped remote writes are out.** Governed remote deletes, if ever
  needed, are a separate future decision built on #3183 — explicitly not this
  rollout.
- Consumer scenarios in the meho-automation design doc
  (evoila-bosnia/meho-internal#223) are updated to match under the implementation
  Tasks, not here.
