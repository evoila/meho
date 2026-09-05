# Management-plane break-glass: an offline-anchored, edge-expired, recorded emergency reach path (decision)

**Status:** **proposed — awaiting Damir's determination.** This record states the
problem, the candidate mechanisms with honest trade-offs, and a recommendation;
the mechanism pick is the operator's. Nothing here is implemented — the
implementation seams are filed as post-decision Tasks under the parent Initiative.
**Date:** 2026-09-05
**Goal:** management-plane lockdown (evoila-bosnia/meho-internal#234) — its
enforcement ratchet **cannot reach stage 3 (deny-with-log) without this**: the Goal
makes "break-glass live and tested first" an explicit prerequisite for denying
operator-direct reach to management planes.
**Initiative:** evoila-bosnia/meho-internal#248 — management-plane break-glass,
backplane-independent emergency operator reach (this record **is** the mechanism
decision that Initiative's implementation Tasks depend on).
**Task:** evoila-bosnia/meho-internal#251 — decide the mechanism + specify the
backplane-independent issuance and audit-import contract (this file).
**Composes with:** [docs/decisions/satellite-write-path.md](satellite-write-path.md)
(scoped-hybrid remote writes, fail-closed) and
[docs/decisions/governed-delete-operations.md](governed-delete-operations.md)
(delete-shaped ops behind a dedicated destructive tier, fail-closed). Both name a
**central-or-break-glass** arm for the hardest work; this decision defines what
that break-glass arm *is*.

## The problem

In the lockdown end-state (Goal #234, stages 3–4) an operator laptop has **no
network route to any appliance management plane** — the backplane (central instance
+ satellite runners) is the sole management-plane ingress, which is what makes the
audit log the *complete* record of estate mutations and the policy/approvals plane
an actual perimeter. That end-state has one unavoidable failure mode: **the
backplane itself goes down** (control-plane outage, database failure, a bad deploy,
certificate expiry, a partition to the central instance). If a dead backplane locks
operators out of the estate it governs, MEHO is a single point of failure over the
entire management surface — and, decisively, **stage-3 deny can never be safely
enabled**, because the only sanctioned way to recover a down backplane would have
been removed. Denying operator-direct reach is only defensible if there is a
documented, cold-tested answer to "what do we do when MEHO is down?"

So the lockdown needs an **emergency reach path** that is time-boxed,
human-authorized, session-recorded, and — the load-bearing constraint — whose
**issuance does not depend on the backplane being up**.

### The organizing asymmetry

The satellite write-path decision was organised around **read/write asymmetry**.
The organizing fact here is different: **the governor is the thing that is
unavailable.** The control plane that would normally govern an access grant —
policy, approvals, Keycloak, the audit write — is exactly what is down at the moment
break-glass is needed. So the grant *cannot be governed synchronously by that plane
at the moment of issuance*. As with the satellite effect audit, true
synchronous-at-centre governance is structurally impossible for this path, and the
security therefore cannot come from making the emergency path more trusted. It comes
from three levers, each mirroring the satellite composition:

1. **Minimise what the path can reach** — the emergency profile routes only to a
   recording chokepoint, never a broad standing route (the analogue of the
   satellite per-runner allowlist as blast-radius bound).
2. **Minimise how long a grant is useful** — a hard, short TTL enforced *at the
   edge*, not by operator discipline (the analogue of short-lived per-work-item
   credentials).
3. **Guarantee every use is captured and reconciled** — every session is recorded
   and imported into `audit_log` after the fact under a break-glass provenance
   marker, with a missing import raising an alarm (the analogue of the
   store-and-forward effect audit + the un-reported-mint dead-man).

## The candidate mechanisms (honest trade-offs)

### Option A — Short-lived client/VPN certificate issued by an offline anchor

An **offline anchor** — a small, sealed, backplane-independent certificate issuer
whose signing key is held out-of-band (split-custody / sealed / offline) — mints a
**short-lived client certificate** that the perimeter edge accepts for an
emergency-only VPN profile scoped to management networks. The certificate carries a
hard, short `notAfter`.

- **Strengths.** Issuance is genuinely backplane-independent: the anchor makes no
  call to the backplane's database, policy, approvals, or identity provider.
  **The edge enforces the TTL by construction** — an expired certificate simply
  stops authenticating on the next handshake/rekey, so time-box is a *role*, not
  operator discipline. **Revocation-by-expiry** needs no CRL/OCSP dependency on the
  backplane. The certificate serial is a stable, non-repudiable **session anchor**
  for the after-the-fact audit import.
- **Weaknesses.** The offline anchor is a **standing high-value secret**: if its
  signing key leaks, an attacker mints their own emergency certificates and the
  break-glass becomes the softest target in the whole lockdown. A certificate
  authorizes *reach* but does not itself *record the session* — raw tunnel
  authorization yields connection/netflow-level records, not action-level ones, so
  a cert alone cannot satisfy the audit-import contract. A tunnel established just
  before expiry can outlive `notAfter` unless the edge also enforces a **session
  hard-cap** (max lifetime + forced rekey that re-validates the cert).

### Option B — Recorded bastion / jump host

An always-available, backplane-independent **bastion** sits in-path to the
management networks. Operators reach management planes only by authenticating to the
bastion; the bastion **records the session** (terminal/keystroke capture or proxied
protocol logging) and tears it down at a per-session TTL.

- **Strengths.** **Session recording is native and rich** — the bastion captures the
  actual commands and proxied requests, which is a far better audit-import source
  than netflow and is what keeps `audit_log` a *complete* record of what was
  mutated. TTL is bastion-enforced.
- **Weaknesses.** A bastion is a **standing, always-on host with standing reach into
  every management network** — it partially re-introduces the very standing route
  stage-4 exists to eliminate, and it is itself a concentrated high-value target. It
  adds its own availability dependency: if the bastion is down during a backplane
  outage, operators are **doubly** locked out, so it needs its own HA story. And it
  does not answer the hard half of the problem on its own — the operator still needs
  a **backplane-independent way to be authorized onto the bastion** when the
  backplane is down, which is the offline-issuance problem again.

### Option C — Sealed recovery account

A pre-provisioned local emergency account (a sealed admin/root credential per
management plane or edge), password sealed under split-knowledge / offline envelope,
released out-of-band under approval; the operator connects directly.

- **Strengths.** Ultimate backplane-independence and operational simplicity — the
  classic break-the-glass envelope; works even when cert or bastion infrastructure
  is itself degraded.
- **Weaknesses.** Weak on every governance axis this decision cares about. **No TTL
  enforcement at all** — a released password is valid until someone manually rotates
  it, i.e. "time-box by operator discipline," which the requirement explicitly
  forbids. **No native session recording** — direct appliance access leaves only
  whatever the appliance itself logs. Revocation means rotating the credential on
  every appliance by hand. Blast radius is typically full admin. It fails the
  edge-enforced-TTL and recording contracts outright.

### Option D (the better one) — the composed hybrid

Options A and B are **not competitors** — they answer different halves of the
requirement, exactly as the satellite decision's four mechanisms each cover a
distinct lever. A answers *"how do you get backplane-independent, edge-expired
reach"*; B answers *"how do you get a record rich enough to keep the audit log
complete."* Neither is sufficient alone. The composition:

1. **Reach is authorized by a short-lived certificate from the offline anchor**
   (Option A): backplane-independent to issue, edge-enforced to expire, with a
   session hard-cap so no tunnel outlives its certificate.
2. **The emergency profile terminates on a recording chokepoint, not directly on
   the appliances** (Option B's recording, demoted from "standing route" to
   "the only thing the emergency cert can reach"): the operator touches management
   planes only through the chokepoint, which records the session as the
   audit-import source. A recording bastion is the recommended chokepoint; the exact
   recording surface is implementation, not this decision.
3. **The sealed recovery account (Option C) is retained only as the deep
   double-failure fallback** — for when even the anchor or the chokepoint is
   unavailable. Its use is the loudest possible alarm and the highest-friction path.

This is the recommendation. It scopes the bastion's standing-route weakness down to
"reachable only by a live emergency certificate" and scopes the certificate's
weak-recording weakness away by routing every session through the chokepoint.

## Contract 1 — backplane-independence and offline issuance

The mechanism is worthless if its issuance quietly depends on the backplane. The
contract:

- **The offline anchor has zero runtime dependency on the backplane.** Minting an
  emergency certificate makes no call to the backplane's Postgres, policy engine,
  approvals queue, identity provider, or API, and does not run in the backplane's
  cluster. It is a sealed, independently-hosted (or offline) component.
- **Edge enforcement is edge-local.** Certificate validation, TTL, the session
  hard-cap, and the emergency profile's route-scope are configured on the perimeter
  edge and hold with the backplane down — nothing about honouring an already-issued
  certificate consults the backplane.
- **Two issuance paths, and they are not interchangeable:**
  - **Backplane up ⇒ the approvals-plane path is mandatory** (Contract 4). The
    offline path is **not** a convenience shortcut when governance is reachable — if
    it were, every issuance would route around the approvals plane.
  - **Backplane down ⇒ the offline path is sanctioned**, under out-of-band human
    authorization (co-signed / split-custody release), deliberately higher-friction
    and louder than the normal path.
- **The offline path always raises a reconciliation obligation.** Any offline
  issuance creates a debt that must be discharged by the after-the-fact audit import
  (Contract 3); an offline issuance with no reconciled import past a deadline is a
  **security alarm**, directly mirroring the satellite decision's un-reported-mint
  dead-man.

## Contract 2 — TTL enforced at the edge, as a role

The access is **time-boxed and auto-expiring, enforced at the edge, not by operator
discipline** — the exact phrasing the parent Goal and Initiative require:

- The certificate's short `notAfter` bounds authentication; **plus** an edge session
  hard-cap (maximum tunnel lifetime with a forced rekey that re-validates the
  certificate) so a session cannot outlive its certificate by holding a tunnel open.
- An expired certificate stops authenticating and an over-length session is torn
  down **by the edge** — the operator is never trusted to disconnect on time.
- **The exact TTL value is an operator veto point on review** (as the satellite
  decision left its Stage-1 op-class and revocation-latency knobs to operator
  determination). The recommendation is a short window on the order of one hour,
  extendable only by re-issuance through the same gate — never by silently widening
  an active grant.

## Contract 3 — after-the-fact audit-log import with a break-glass provenance marker

The audit log must remain the **complete** record of estate mutations even when a
mutation happened during a backplane outage. As with the satellite **effect** audit,
synchronous-at-centre audit is structurally impossible here (the backplane was
down), so this decision **consciously records the exception** and closes it by
import:

- **Every break-glass session is recorded at the chokepoint** — a tamper-evident
  session capture / connection record carrying the certificate serial (the session
  anchor), operator identity, the issuance-authorization reference (an
  `ApprovalRequest` id for the backplane-up path, or the out-of-band authorization
  token for the backplane-down path), start/end timestamps, and the captured
  actions.
- **When the backplane returns, the record is imported into `audit_log` as
  append-only rows carrying a distinct break-glass provenance marker** — a dedicated
  source/marker value that makes these rows unmistakably *imported-after-the-fact
  emergency access*, never confusable with a synchronous governed dispatch. This
  preserves v0.1-spec §6's "audit is the complete record" while honestly marking
  that these rows were reconciled, not committed-before-effect.
- **Transit integrity.** The recorded session is hash-chained / tamper-evident in
  transit so a dropped or altered record is detectable on import — the same posture
  as satellite mechanism 4.
- **The import is mandatory and its absence alarms.** A break-glass issuance with no
  reconciled `audit_log` import past a deadline fires a **security** event (a
  dead-man sweep over issued-but-unreconciled grants), distinct from any liveness
  signal. Recording makes the emergency session's silence *observable*; it does not
  make a lying operator honest — that residual is bounded by the minimal
  chokepoint-only route (Contract 1) and the short edge TTL (Contract 2).

## Contract 4 — the approvals-plane gate for the backplane-up case

When the backplane **is** up — the common case for a *scoped* emergency (a single
appliance is wedged while MEHO itself is healthy) — break-glass issuance is governed
by the existing approvals plane, unchanged in mechanism:

- Issuance is requested and **parked as an `ApprovalRequest`**; a human clears it on
  a REST/CLI/console surface, exactly as any governed park. Approval is a **human
  decision** — the decision verbs have **no MCP path under any claim set**
  (v0.1-spec §7), so no agent session can self-issue break-glass.
- The committed approval **is** the issuance authorization; its id is stamped on the
  recorded session and rides through to the imported `audit_log` rows, so a
  backplane-up break-glass is fully governed **before** effect and needs no
  after-the-fact exception — the import in Contract 3 is then a completeness
  formality, not a reconciliation of an ungoverned grant.
- This reuses the approvals substrate; it does not fork it and invents no second
  approval mechanism (the same discipline the governed-delete decision holds to).

## Contract 5 — fail-closed-override composition

Break-glass is the **sanctioned human override for *unreachable* governance — never
for *inconvenient* governance.** Stated against the two sibling decisions:

- **Satellite write path** (satellite-write-path.md): satellite writes are minted
  only within a minimal per-runner allowlist, and **delete-shaped operations are
  never minted to a satellite** — the tier is **fail-closed**. Break-glass does not
  loosen that. When the backplane is up, that gate is the path. Break-glass is only
  the arm for when the governance plane cannot be consulted **at all**.
- **Governed delete operations** (governed-delete-operations.md): delete-shaped ops
  require mandatory human approval + preview-hash binding + a blast-radius statement,
  behind a dedicated `destructive` tier excluded by default everywhere — also
  **fail-closed**. Both sibling decisions explicitly name a **central-or-break-glass**
  arm for the hardest work (deletes "stay central-or-break-glass"). **This decision
  defines that arm**: it is exactly this offline-anchored, edge-expired, recorded,
  audit-imported *human* path — **not** a standing "drop to local tools" escape.
- **The reversal this enables.** Today the informal break-glass *is* "drop to local
  vendor tools" over the operator VPN. That informal escape is precisely what
  stage-3 deny removes. This decision **replaces the informal escape with a governed
  one**, which is what makes removing operator-direct reach safe — the whole reason
  the Goal gates stage 3 on "break-glass live and tested first."
- **No standing bypass.** Break-glass creates no standing route and no standing
  credential: it is time-boxed (Contract 2), chokepoint-scoped (Contract 1),
  recorded and reconciled (Contract 3). It exists to be used rarely, loudly, and
  briefly. The one-line invariant: **when governance is reachable, fail-closed means
  fail-closed; break-glass is only the answer when governance is unreachable.**

## Recommendation

**Adopt Option D:** the **offline-anchored short-lived certificate** as the
break-glass *authorization/reach* mechanism — backplane-independent to issue,
edge-enforced to expire — **terminating on a recording chokepoint** (a recording
bastion is the recommended surface) so every emergency session imports into
`audit_log` under a break-glass provenance marker; with the **sealed recovery
account retained only as the deep double-failure fallback.**

The reasoning, tied to the contracts:

- The certificate is the only candidate that satisfies **backplane-independent
  issuance *and* edge-enforced TTL** together (Contracts 1–2) — the two hardest,
  most load-bearing requirements. A bastion alone still needs a backplane-independent
  way to authorize the operator onto it (the issuance problem returns), and a sealed
  account has no edge-enforced TTL at all.
- The bastion/chokepoint is not discarded — it is the answer to the **recording**
  contract (Contract 3): a certificate authorizes reach but records only netflow,
  which cannot keep `audit_log` complete. Routing the emergency profile *only* to the
  chokepoint both supplies the action-level record and shrinks the bastion's
  standing-route weakness to "reachable only by a live emergency certificate."
- The sealed account survives as the fallback for a double failure (anchor *or*
  chokepoint unavailable) because ultimate independence is worth keeping for the
  worst day — but it is demoted below the governed path precisely because it fails
  the TTL and recording contracts.

## Cold-test acceptance (the proof this decision must earn)

Mirroring the parent Initiative's cold-test DoD — the decision is only vindicated
when this passes end-to-end on one lab domain:

1. **Stop the backplane.**
2. **Obtain access through the offline-issuance path** — mint a short-lived
   certificate via the offline anchor under out-of-band human authorization, with the
   backplane provably down.
3. **Reach a management plane through the recording chokepoint** and perform a
   management action.
4. **Confirm the edge expires the session at TTL** without any operator action
   (certificate `notAfter` + session hard-cap).
5. **Bring the backplane back.**
6. **Confirm the recorded session imports into `audit_log`** under the break-glass
   provenance marker, and that a *missing* import would have fired the security
   alarm.

## Relation to the self-approval break-glass (one line, as required)

This decision does **not** modify the existing self-approval break-glass
(`single_operator_break_glass` / `APPROVAL_ALLOW_SELF_APPROVAL`, guide
`docs-site/guides/approvals-and-break-glass.md`) — that switch lets a *solo operator
clear the approval queue* and is orthogonal; this decision is about emergency
network/access *reach* when the whole backplane is unavailable.

## Scope / non-goals

- **This decision records the design, not the code.** The implementation seams are
  the post-decision Tasks under evoila-bosnia/meho-internal#248, unimplemented. This
  file is the design/decision home.
- **The mechanism pick is the operator's.** Status is *proposed — awaiting Damir*;
  the exact TTL value and the recording-surface choice are operator veto points on
  review, as the satellite decision left its analogous knobs.
- **Management planes only.** Data-plane / workload-network emergency access is out
  of scope (Goal #234 non-goal).
- **No standing privileged VPN.** Break-glass is the opposite of standing access —
  time-boxed, approved, recorded.
- **No change to the self-approval break-glass** (above).
- **The offline path is not a shortcut.** It is sanctioned only when the backplane is
  provably unreachable; using it while the backplane is up is a governance violation,
  not a convenience.

## Implementation shape (for the post-decision Tasks)

Filed concrete once the mechanism is picked; the parent Initiative's execution-tasks
checklist is updated to these at accept time:

1. **The offline anchor + edge emergency profile** — a sealed, backplane-independent
   certificate issuer (split-custody signing key, issuance alarm) and the edge
   emergency VPN profile that accepts its certificates, enforces `notAfter` + a
   session hard-cap, and routes only to the recording chokepoint.
2. **The recording chokepoint** — the bastion (or equivalent) that terminates the
   emergency profile, records the session tamper-evidently, and enforces the
   per-session TTL.
3. **The approvals-plane issuance gate (backplane-up) + the out-of-band release
   (backplane-down)** — park/approve issuance through the existing approvals plane
   when up; the co-signed / split-custody release when down, always raising a
   reconciliation obligation.
4. **The after-the-fact audit import** — import recorded sessions into `audit_log`
   with the distinct break-glass provenance marker, hash-chained in transit, plus the
   dead-man sweep that alarms on an issued-but-unreconciled grant past its deadline.
5. **The cold test** — run the six-step acceptance above end-to-end on one lab
   domain and document it as the operator runbook (kb entry in the lab repo + a
   `docs-site` guide section distinguishing it from the self-approval break-glass),
   per the parent Initiative's DoD.

## References

- Parent Goal evoila-bosnia/meho-internal#234 (management-plane lockdown — the
  Break-glass section that requires this) and Initiative
  evoila-bosnia/meho-internal#248 (the design/build/cold-test home); Task
  evoila-bosnia/meho-internal#251 (this decision).
- Sibling decisions this composes with: [satellite-write-path.md](satellite-write-path.md)
  (fail-closed scoped-hybrid remote writes; the store-and-forward effect audit +
  un-reported-mint dead-man this decision's import contract mirrors) and
  [governed-delete-operations.md](governed-delete-operations.md) (fail-closed
  destructive tier; the central-or-break-glass arm this decision defines).
- v0.1-spec §6 (audit is the complete record — the invariant the after-the-fact
  import preserves) and §7 (approval is a human decision — the approvals-plane gate
  this reuses).
- Distinguished-from self-approval break-glass:
  `docs-site/guides/approvals-and-break-glass.md`,
  `backend/src/meho_backplane/settings.py`, `backend/src/meho_backplane/features.py`.
