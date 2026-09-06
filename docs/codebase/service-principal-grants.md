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

**Classifying `SERVICE` (#3178).** An explicit `principal_kind` claim
always wins, but a Keycloak client-credentials client often carries **no**
such claim (no `principal_kind` mapper), which historically fell back to
the `user` default and left this whole gate — and any standing grants —
silently inert. `_extract_principal_kind` therefore infers `service` for
the **absent-claim** case when the token is positively identified as an IdP
service account: its username claim (default `preferred_username`,
`JWT_SERVICE_ACCOUNT_USERNAME_CLAIM`) bears Keycloak's reserved
`service-account-<clientId>` prefix (default `service-account-`,
`JWT_SERVICE_ACCOUNT_USERNAME_PREFIX`). Fail-closed for the policy path:
only a positive marker *upgrades*; any unrecognised shape stays `user`. As
a residual-drift signal, `_non_agent_verdict` emits a WARN
(`policy_gate_grant_holder_classified_non_service`) whenever a **parking**
non-service principal still holds ≥1 live grant.

**"Mutating" is grounded in existing descriptor fields** (no new column,
`_is_mutating`): an ingested op is read-class iff its HTTP `method` ∈
`{GET, HEAD}` (the `READ_HTTP_METHODS` set); a typed / composite op
carries no `method`, so its curated `safety_level` stands in (`safe` =
read, `caution` / `dangerous` / `destructive` = write). `destructive`
parks always **and is never grant-satisfiable** (#3183); `dangerous`
parks always; a `caution` op parks only when mutating.

When a `SERVICE` op would park, the gate consults a live standing grant
(`connector_id` must be supplied so the grant's connector scope can be
matched — the dispatcher and the composite sub-op gate both pass it; the
gateway path is safe-only and never needs it). A match auto-approves the
op and records the use; absent a match, the op parks for a human decision.
The one exception is the `destructive` tier: `consult_and_record_grant`
refuses it **before** any grant lookup, so even a stale grant row cannot
auto-approve a destructive op — the tier always parks for a human.

### The grant (`db/models.py::ServicePrincipalGrant`, `operations/service_grants.py`)

One row authorises exactly one `(principal_sub, op_id, connector_id,
target-scope)` tuple in a tenant to run unattended. **Every scope is
explicit — no wildcards** on `principal_sub`, `op_id`, or `connector_id`
(contrast `AgentPermission`, whose `op_pattern` is a glob for the *agent*
model). Creating the grant IS the operator's upfront review, deny-by-default
absent a match.

The **target scope** is one of three shapes:

- a concrete `target_id` — the op on exactly that target (matched
  **exactly**);
- **neither `target_id` nor a selector** — a targetless / tenant-wide op
  (`target_id IS NULL`). A null `target_id` is matched **literally**, not as
  an any-target wildcard;
- a **target selector** (#3349) — `target_product` and/or
  `target_name_pattern`, resolved at dispatch time against the target
  fingerprint (see below).

### Null-target is not a wildcard — the runtime hint (#3349)

A grant created with `target_id=null` in the belief that null means "any
target" never matches a target-scoped dispatch — null is matched literally.
Before #3349 that misfire was silent: the op parked with a generic
service-principal-park reason and no signal the null was matched literally
(exactly how a provisioning run misfired). Now, when a service-principal
dispatch would park and the **only** live candidate grant for the same
`(principal_sub, op_id, connector_id)` is a **pure null-target** grant (no
selector), the gate appends a hint to the park reason naming the mismatch —
"a null-target standing grant does not match a target-scoped dispatch … scope
the grant to this target, or use a target selector". The hint lookup
(`service_grants.null_target_only_park_hint`) runs only on the park path (the
slow path), never on the auto-execute hot path.

### Target selectors — grants for targets that do not exist yet (#3349)

A **selector** grant carries `target_product` and/or `target_name_pattern`
in place of a concrete `target_id`, so an operator can authorise "this op, on
any target with `product=<x>` whose name matches `<pattern>`, for this
principal" **before** those targets exist. This is what lets a blueprint that
registers its own appliances mid-run execute unattended without a per-target
grant. At dispatch (`find_live_grant`):

- a concrete-`target_id` grant is tried **first** (a specific grant always
  wins over a broad selector);
- absent that, selector grants for the same exact
  `(tenant, principal_sub, op_id, connector_id)` are evaluated against the
  dispatch's target fingerprint — `target_product` matched exactly,
  `target_name_pattern` an `fnmatchcase` glob over the target name (a NULL
  dimension is "don't care", but at least one is set so a selector is never a
  blanket any-target match);
- a **targetless** dispatch (no target) never matches a selector — a selector
  needs a fingerprint.

The selector is still exact on `(tenant, principal_sub, op_id, connector_id)`,
still deny-by-default, still honours revocation + expiry, and — crucially —
**delete-shaped ops stay un-grantable regardless of the selector** (the
create-time refusal fires before the target scope is even considered). The
wildcard is requested **explicitly** via the selector fields; it is never
implied by a NULL `target_id`. `target_id` and a selector are mutually
exclusive (a 422 at the schema boundary). A selector match is **visibly
distinct** on the grant-read surface (`target_product` /
`target_name_pattern` are non-null) and in the auto-approval audit row
(`matched_by="selector"` + the selector predicate under `target_selector`).

An `op_id` **may** carry a literal query string (e.g.
`POST:/vcenter/vm/{vm}/power?action=start`,
`POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy`): several
governed vCenter ops key each `?action=` verb as its own exact op id, so the
`?` there is a literal part of the id, not a glob. Such an op id is accepted
verbatim and matched by exact string equality; a `*` anywhere — or a
malformed `?` (no `key=value`) — is still refused as a wildcard. Without
this, a service principal could never hold a standing grant for the
`vm.power` / `vm.deploy_from_library` / host-software composite sub-ops, so
those composites always parked for service principals.

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
`…_targetless` — split because the nullable `target_id` would defeat a single
unique index on the targetless case (`NULL != NULL`), and scoped to
`revoked_at IS NULL` so a revoked scope can be re-granted. Since #3349 the
`…_targetless` index is narrowed (`WHERE target_id IS NULL AND target_product
IS NULL AND target_name_pattern IS NULL AND revoked_at IS NULL`) so a selector
grant — which also keys `target_id IS NULL` — is not forced into the single
pure-targetless slot. Selector-grant uniqueness ("at most one active selector
per `(key, target_product, target_name_pattern)`") is enforced in the CRUD
layer (`ServicePrincipalGrantService.create`, NULL-safe on the nullable
selector columns) rather than by a partial index, sidestepping the awkward
NULL-vs-NULL portability across Postgres / SQLite.

### Delete-shaped guardrail

A grant is the **floor** of what runs unattended, never a bypass of a
modeled destructive gate, so `ServicePrincipalGrantService.create` refuses
delete-shaped ops:

- **by configured pattern** (`Settings.service_grant_delete_shaped_patterns`,
  env `SERVICE_GRANT_DELETE_SHAPED_PATTERNS`, `fnmatchcase` over the op id;
  default `DELETE:*`, `*.delete`, `*.destroy`, `*.remove`, `*.purge`), and
- **by descriptor** (best-effort when a descriptor resolves via
  `lookup_descriptor`): the `destructive` safety tier (#3183), the HTTP
  `DELETE` verb, or a hand-authored `destructive` tag on a typed op.

`_delete_shaped_reason_by_descriptor` single-sources that descriptor-level
classification and is consulted at **both** ends: create-time (above) and
dispatch-time (`consult_and_record_grant`, so a stale grant predating an
op's promotion into the `destructive` tier can never satisfy it). That is
what makes "a standing grant can never satisfy a destructive op" hold
whether the grant is being created or consulted.

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
decision's `approval.approved`. A **selector** match (#3349) additionally
carries `matched_by="selector"` and the selector predicate under
`target_selector`, so the ledger shows the op ran on a runtime-created target
the selector authorised (a concrete / targetless match records
`matched_by="target"`).

## Governed-subop discovery (#3349)

A composite fans out to child ops, and child gating consults the grant plane
on the **child** op id with **no parent→child inheritance** (by design —
`docs/architecture/operations-substrate.md`), so a service principal running
a composite unattended needs a standing grant for **each** governed child. To
assemble that grant set an operator previously had to read the connector's
`_SUB_OPS_*` / `_VIM_SUB_OPS_*` manifests in source. The supported
alternative:

| Verb | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/operations/governed-subops?op_id=<composite>&connector_id=<id>` | List the governed child ops a composite may dispatch (role: `operator`). |

Each entry carries the child `op_id`, a best-effort `safety_level`
(descriptor-resolved; `null` for a code-shipped vim sub-op with no
descriptor), a `grantable` flag, and an `ungrantable_reason`. The child set
is **derived from the manifests** — a connector registers, per composite op
id, the manifest tuples it fans out to via
`operations.governed_subops.register_governed_subops`
(the vmware-rest map lives in
`connectors/vmware_rest/composites/_governed_subops.py`, referencing the
`_SUB_OPS_*` constants, never re-typing them) — not hand-maintained. A
**delete-shaped** rollback / delete leg (e.g. `vm.create`'s
`DELETE:/vcenter/vm/{vm}`, which rides `_SUB_OPS_VM_CREATE`) is flagged
`grantable=false` with the same refusal reason `create` would raise, so the
operator knows up front that a **partial failure of that composite still
requires a human** (the delete rollback can never run unattended). Grantable
classification single-sources `service_grants.delete_shaped_refusal_reason`,
so discovery and create-time refusal cannot drift.

## REST surface (`api/v1/service_grants.py`)

Role: **`operator`** (not `tenant_admin`) — a standing grant is the
persistent form of the approve decision an operator already makes on the
approval queue.

| Verb | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/service-principals/grants` | List (`principal_sub`, `include_revoked`, `limit`, `offset`). |
| `GET` | `/api/v1/service-principals/grants/{id}` | Show one. 404 cross-tenant. |
| `POST` | `/api/v1/service-principals/grants` | Create (the review; `reason` required). 422 on wildcard / delete-shaped / past expiry / duplicate / `target_id`+selector. |
| `DELETE` | `/api/v1/service-principals/grants/{id}` | Revoke (soft-delete). 404 when absent / already revoked / cross-tenant. |

The route surface (grants + `GET /api/v1/operations/governed-subops`) enters
`cli/api/openapi.json` and the generated Go client (regenerate with `cd cli
&& make snapshot-openapi && make generate` after any signature change).

## Scope boundaries (this PR)

- **No UI console work** — the console can list grants via the REST surface
  later; the audit ledger already surfaces grant uses under `method=APPROVAL`
  / `path=approval.decision`.
- **No agent-path change** — `resolve_verdict` and the `AgentPermission`
  model are untouched; agents keep their own grant model.

## Dependencies

- `db/models.py` — `ServicePrincipalGrant` ORM model (incl. the #3349
  `target_product` / `target_name_pattern` selector columns + narrowed
  targetless index).
- `alembic/versions/0078_create_service_principal_grant.py` — table +
  lookup index + two partial unique indexes.
- `alembic/versions/0098_add_service_principal_grant_target_selector.py`
  (#3349) — selector columns + narrowed targetless partial unique index.
- `operations/service_grants.py` — CRUD service + `find_live_grant`
  (selector matching) + `null_target_only_park_hint` (#3349) +
  `consult_and_record_grant` + delete-shaped classification
  (`delete_shaped_refusal_reason` public alias).
- `operations/service_grant_schemas.py` — REST request / response shapes
  (selector fields + mutual-exclusion validator).
- `operations/governed_subops.py` (#3349) — connector-agnostic discovery
  registry + `build_governed_subops_response` + grantability classifier;
  `connectors/vmware_rest/composites/_governed_subops.py` populates it from
  the manifests.
- `api/v1/operations.py` — `GET /api/v1/operations/governed-subops` route.
- `operations/_validate.py` — `_non_agent_verdict` (safety_level consult +
  grant consultation + #3349 null-target hint via `_service_grant_verdict`),
  `policy_gate` (`connector_id` param).
- `operations/dispatcher.py` / `operations/composite.py` — pass
  `connector_id` into `policy_gate`.
- `settings.py` — `service_grant_delete_shaped_patterns`.

## Known limitations

- There is **no server-side service-principal registry** to validate
  `principal_sub` against (a service principal is any Keycloak
  client-credentials client classified `principal_kind=service` — by an
  explicit claim or the #3178 service-account marker), so — unlike
  `AgentGrantService`, which rejects an unregistered agent principal — a
  grant whose `principal_sub` never authenticates simply sits inert.
