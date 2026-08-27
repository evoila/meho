# Service-principal standing grants (#3151 + #3152)

**Issues:** #3151 (standing scoped auto-approval grants for service
principals) + #3152 (non-agent verdict consults `safety_level`). The two
define one policy model and shipped together.

## Why

A paired consumer add-on drives long multi-step workflows through the
backplane as a **service principal** (OAuth2 client-credentials,
`principal_kind = service`). Its workflow model has a single deliberate
human-approval gate near the end, but the run never reaches it unattended:
the non-agent policy gate parked **every** `requires_approval` op, so a
~10-step substrate build parked a dozen times before the modeled gate —
degrading "review the plan once" into "click approve eleven times" and
training exactly the wrong reflex.

Separately (#3152), the non-agent gate keyed **only** on
`requires_approval` and never consulted `safety_level`, so a **mutating**
`caution` op carrying `requires_approval=False` executed completely
ungated for a service principal (e.g. `POST:/vcenter/vm/{vm}/hardware/disk`).

## The model (two decisions, one PR)

**Operator decisions recorded at ship time (2026-08-27):**

1. Standing scoped auto-approval grants for service principals are
   approved as a capability.
2. #3152 resolves as its **option 1**: the non-agent gate now consults
   `safety_level`, with standing grants as the sanctioned path for
   unattended mutations.

### The gate (`operations/_validate.py::_non_agent_verdict`)

Enforcement is **service-principal-only**. The branch structure:

| Principal | `requires_approval` | mutating `caution` / `dangerous` | safe / read |
|---|---|---|---|
| `USER` (human, interactive) | park (queue) | **auto-execute** (unchanged) | auto-execute |
| `SERVICE` (client-credentials) | park unless granted | **park unless granted** (#3152) | auto-execute |
| `AGENT` | `resolve_verdict` (agent-permission model) — untouched | | |

A human `USER` operator keeps the v0.2 default-allow contract (they are
their own approver via the approval queue); `safety_level` is consulted
**only** for a `SERVICE` principal. That is how "operator-interactive"
is distinguished from "service principal": the `principal_kind` claim
(`user` vs `service`, `auth/jwt.py::_extract_principal_kind`). Agents are
unaffected — they keep the `AgentPermission` verdict model.

**"Mutating" is grounded in existing descriptor fields** (no new column,
`_is_mutating`): an ingested op is read-class iff its HTTP `method` ∈
`{GET, HEAD}` (the `READ_HTTP_METHODS` set); a typed / composite op
carries no `method`, so its curated `safety_level` stands in (`safe` =
read, `caution` / `dangerous` = write). `dangerous` parks always; a
`caution` op parks only when mutating.

When a `SERVICE` op would park, the gate consults a live standing grant
(`connector_id` must be supplied so the grant's connector scope can be
matched — the dispatcher and the composite sub-op gate both pass it; the
gateway path is safe-only and never needs it). A match auto-approves the
op and records the use; absent a match, the op parks for a human decision.

### The grant (`db/models.py::ServicePrincipalGrant`, `operations/service_grants.py`)

One row authorises exactly one `(principal_sub, op_id, connector_id,
target_id)` tuple in a tenant to run unattended. **Every scope is
explicit — no wildcards** on `principal_sub`, `op_id`, or `connector_id`
(contrast `AgentPermission`, whose `op_pattern` is a glob for the *agent*
model). `target_id` is matched **exactly**, including the targetless
(`NULL`) case — no any-target wildcard. Creating the grant IS the
operator's upfront review, deny-by-default absent a match.

- **`reason` is required** (the review flow — the body carries the
  operator's justification).
- **`expires_at`** optional (a standing grant is permanent by default).
- **`revoked_at` / `revoked_by_sub`** — revocation is a **soft-delete**:
  the row is retained so the grant history stays visible (same forensic
  visibility as a human approval decision). Expiry and revocation are both
  honoured **at dispatch time** by the lookup filter (`revoked_at IS NULL
  AND (expires_at IS NULL OR expires_at > now)`); no sweeper is needed — an
  expired / revoked row simply stops matching.

Uniqueness ("at most one active grant per fully-scoped key") is enforced by
two **partial** unique indexes — `uq_service_principal_grant_targeted`
(`WHERE target_id IS NOT NULL AND revoked_at IS NULL`) and
`…_targetless` (`WHERE target_id IS NULL AND revoked_at IS NULL`) — split
because the nullable `target_id` would defeat a single unique index on the
targetless case (`NULL != NULL`), and scoped to `revoked_at IS NULL` so a
revoked scope can be re-granted.

### Delete-shaped guardrail

A grant is the **floor** of what runs unattended, never a bypass of a
modeled destructive gate, so `ServicePrincipalGrantService.create` refuses
delete-shaped ops:

- **by configured pattern** (`Settings.service_grant_delete_shaped_patterns`,
  env `SERVICE_GRANT_DELETE_SHAPED_PATTERNS`, `fnmatchcase` over the op id;
  default `DELETE:*`, `*.delete`, `*.destroy`, `*.remove`, `*.purge`), and
- **by descriptor** (best-effort when a descriptor resolves via
  `lookup_descriptor`): the HTTP `DELETE` verb, or a hand-authored
  `destructive` tag on a typed op.

Ops the workflow models as its human gate (e.g. a bring-up start) are
expected to stay ungranted — the grant list is the floor, not a bypass.

### Grant-use audit (same visibility as a human decision)

Every grant use writes one audit row via
`service_grants._record_grant_use`, mirroring an approval **decision** row
exactly (`method='APPROVAL'`, `path='approval.decision'`, `status_code=200`)
so it is indistinguishable in the ledger from a human Approve — except
`reviewed_by` reads `grant:<id>` and the payload carries
`decision='auto-approved'` + `grant_id` + `reason="auto-granted by standing
grant <id>"`. It is written in its own committed transaction before the op
runs (the synchronous-audit invariant), and a fail-open
`approval.auto_approved` broadcast is published for parity with a human
decision's `approval.approved`.

## REST surface (`api/v1/service_grants.py`)

Role: **`operator`** (not `tenant_admin`) — a standing grant is the
persistent form of the approve decision an operator already makes on the
approval queue.

| Verb | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/service-principals/grants` | List (`principal_sub`, `include_revoked`, `limit`, `offset`). |
| `GET` | `/api/v1/service-principals/grants/{id}` | Show one. 404 cross-tenant. |
| `POST` | `/api/v1/service-principals/grants` | Create (the review; `reason` required). 422 on wildcard / delete-shaped / past expiry / duplicate. |
| `DELETE` | `/api/v1/service-principals/grants/{id}` | Revoke (soft-delete). 404 when absent / already revoked / cross-tenant. |

The route surface enters `cli/api/openapi.json` and the generated Go
client (regenerate with `cd cli && make snapshot-openapi && make generate`
after any signature change).

## Scope boundaries (this PR)

- **No UI console work** — the console can list grants via the REST surface
  later; the audit ledger already surfaces grant uses under `method=APPROVAL`
  / `path=approval.decision`.
- **No agent-path change** — `resolve_verdict` and the `AgentPermission`
  model are untouched; agents keep their own grant model.

## Dependencies

- `db/models.py` — `ServicePrincipalGrant` ORM model.
- `alembic/versions/0078_create_service_principal_grant.py` — table +
  lookup index + two partial unique indexes.
- `operations/service_grants.py` — CRUD service + `find_live_grant` +
  `consult_and_record_grant` + delete-shaped classification.
- `operations/service_grant_schemas.py` — REST request / response shapes.
- `operations/_validate.py` — `_non_agent_verdict` (safety_level consult +
  grant consultation), `policy_gate` (`connector_id` param).
- `operations/dispatcher.py` / `operations/composite.py` — pass
  `connector_id` into `policy_gate`.
- `settings.py` — `service_grant_delete_shaped_patterns`.

## Known limitations

- There is **no server-side service-principal registry** to validate
  `principal_sub` against (a service principal is any Keycloak
  client-credentials client carrying `principal_kind=service`), so — unlike
  `AgentGrantService`, which rejects an unregistered agent principal — a
  grant whose `principal_sub` never authenticates simply sits inert.
