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
the **absent-claim** case on either of two positive markers:

1. **Username-prefix marker (#3178).** The token's username claim (default
   `preferred_username`, `JWT_SERVICE_ACCOUNT_USERNAME_CLAIM`) bears
   Keycloak's reserved `service-account-<clientId>` prefix (default
   `service-account-`, `JWT_SERVICE_ACCOUNT_USERNAME_PREFIX`).
2. **Token-shape marker (security review S11, `_is_client_credentials_token`).**
   Independent of the username mapper: the token carries a client-identity
   claim (`azp` or the RFC 9068 `client_id`) and **none** of the
   interactive-session claims (`auth_time` / `session_state` / `sid`) that
   Keycloak stamps on an authorization-code / direct-grant token. This
   backstops marker 1 for a realm that clears the prefix
   (`JWT_SERVICE_ACCOUNT_USERNAME_PREFIX=""`) or omits/renames the
   `preferred_username` mapper — the config-dependent fail-open S11 closed
   (a false negative on marker 1 alone would have let a client-credentials
   principal auto-execute a mutating `caution`/`dangerous` op).

Fail-closed for the policy path: only a positive marker *upgrades*; any
unrecognised shape stays `user`, and an interactive human token (which
always carries a session claim) is never upgraded, so the `user`
default-allow is preserved. As a residual-drift signal, `_non_agent_verdict`
emits a WARN (`policy_gate_grant_holder_classified_non_service`) whenever a
**parking** non-service principal still holds ≥1 live grant.

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
Parking is not refusal: a service principal can still propose a
destructive op and read its result after a human approves. The steps are in
[`approvals.md`](approvals.md#an-add-on-proposes-a-governed-destroy-3478).

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
- **`revoked_reason`** (#3489) — an optional reason the operator gives when
  revoking. This mirrors create: create *requires* a `reason`, revoke
  *accepts* one. When given, it is stored on the grant row and also lands
  as `reason` in the `service_grant.revoke` audit row payload (the route
  binds `audit_reason`). A revoke without a reason stays valid; the column
  is then NULL and the audit payload has no `reason` key.

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

### Orphaned grants — the target was deleted or registered again (#3430)

A grant with a concrete `target_id` matches only that exact target id. When
the target is deleted (`DELETE /api/v1/targets/{name}` stamps `deleted_at`)
or torn down and registered again (the new registration gets a **new** id),
the old grant can never match again. The run then waits for approval on
every op the grant was meant to cover. Deleting a target does not touch its
grants, so before #3430 nothing showed this.

Now every grant read (list, show, and the create response) marks such a grant
as **orphaned**:

- `orphaned: true` — the grant is not revoked, it has a concrete `target_id`,
  and no live (`deleted_at IS NULL`) target with that id exists in the tenant.
  A target id with no row at all counts too.
- `orphan_remediation` — a plain-language fix: create a new grant with a
  target selector (`target_name_pattern` set to the old target name when MEHO
  still knows it, or `target_product`), which matches by name or product and
  so survives a new registration; then revoke the orphaned grant. The
  suggested pattern escapes `*`, `?` and `[` in the old name (`glob.escape`),
  so it matches that name literally and not a look-alike target.

Rules:

- **Read-time check only.** `orphaned` is not a stored column. It is worked
  out on each read by `service_grants.classify_orphaned_grants` (one query for
  live targets, plus one for old names only when something is orphaned).
- **Never auto-revoked.** MEHO does not revoke or change an orphaned grant.
  Cleanup stays an operator decision.
- **Selector and targetless grants are never orphaned** — they do not point
  at one target id. Revoked grants are not flagged either (they are already
  cleaned up).
- **Surfaces.** REST returns the two fields on `ServiceGrantRead`. The CLI
  `grants list` adds an `ORPHANED` column and a count line; `grants show`
  prints `orphaned: yes` and the remediation. The console `/ui/grants` shows
  an `orphaned` status badge with the remediation under it.

A named grant profile attached at target registration (so a grant set is
applied again by name on a new registration) is **not built**. The selector
grant already survives a new registration, so it covers the same need.

### Delete-shaped guardrail

A grant is the **floor** of what runs unattended, never a bypass of a
modeled destructive gate, so `ServicePrincipalGrantService.create` refuses
delete-shaped ops:

- **by configured pattern** (`Settings.service_grant_delete_shaped_patterns`,
  env `SERVICE_GRANT_DELETE_SHAPED_PATTERNS`, `fnmatchcase` over the op id;
  default `DELETE:*`, `*.delete`, `*.destroy`, `*.remove`, `*.purge`),
- **by vendor action name** (#4632): an ingested op id whose last path part
  starts with `Destroy`, `Delete`, `Remove` or `Unregister`, for example
  `POST:/VirtualMachine/{moId}/Destroy_Task`. VI-JSON (the JSON form of
  vCenter's older object API) sends every method as a `POST`, so neither
  the HTTP method nor a pattern sees these deletes. The word list and the
  rule are the ingest parser's, in `meho_backplane/delete_names.py`; the
  parser, the grant service, the flight recorder and the stored-delete
  review all import that one module. This sign does **not** depend on the
  pattern setting: a changed `SERVICE_GRANT_DELETE_SHAPED_PATTERNS` keeps
  it. Lookalikes such as `Destroyer_Task` or `Reset_Task` stay grantable,
  and
- **by descriptor** (best-effort when a descriptor resolves via
  `lookup_descriptor`): the `destructive` safety tier (#3183), the HTTP
  `DELETE` verb, or a hand-authored `destructive` tag on a typed op.

The first two signs need only the op id (`delete_shaped_refusal_reason`),
so they also work for an op with no stored row. The action-name sign and
`_delete_shaped_reason_by_descriptor` are consulted at **both** ends:
create-time (above) and dispatch-time (`consult_and_record_grant`, so a
stale grant predating an op's promotion into the `destructive` tier, or a
grant row written for a VI-JSON delete, can never satisfy it). An inner
step of a multi-step operation reaches the gate as an in-memory record with
only its declared level — no method, no tags — so for it the op id is the
only sign, and the action name is what catches `Destroy_Task`. That is what
makes "a standing grant can never satisfy a destructive op" hold whether
the grant is being created or consulted. A grant row that already exists
for such an op stays in the list but never lets the op run; revoke it with
`meho service-principals grants revoke <id>`. The one exception is an
instance in sandbox mode; see [Sandbox mode](#sandbox-mode-4622) below.

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

The row also carries lineage ids (#3695): `request_id` (the same value the
call's `DISPATCH` row carries), `agent_session_id` (agent-run or MCP
session; NULL for a plain REST call) and `parent_audit_id` (the multi-step
parent's audit id when a child step was cleared). Details:
[`approvals.md`](approvals.md#service-principal-standing-grants--safety_level-gate-3151--3152).

## Sandbox mode (#4622)

Decision record: [`docs/decisions/sandbox-mode.md`](../decisions/sandbox-mode.md).
Sandbox mode is one setting for the whole instance. It says that the
instance holds only disposable test systems: systems built for one test
and deleted after it. On such an instance a tenant admin may give a
service principal a **delete grant**: a standing grant for an operation
with a delete sign (the `destructive` level, the HTTP verb `DELETE`, the
`destructive` tag, a delete name, or a vendor delete action name), or for
a delete step of a multi-step operation. So an automated test run can
build, change and delete its own systems with no wait for a person. Every
other instance keeps the rules above.

What the code does:

- **The switch.** `MEHO_SANDBOX_MODE` and `MEHO_SANDBOX_MODE_CONFIRM` are
  `Settings` fields read at boot only. `meho_backplane/sandbox_mode.py`
  checks them (`check_sandbox_settings`): the mode on needs the confirm
  value, it must equal the host of the MCP resource URI, and that host
  needs a dot-separated part that is exactly `sandbox`. Then the mode is
  stored in the one-row table `sandbox_mode_state` with one insert that
  only one process can win, and read back (`store_or_check_sandbox_mode`);
  a stored mode that differs from the setting refuses the start. The mode
  is fixed for the life of the database. Every runtime reader uses the
  process flag `sandbox_mode_active()`, set once at boot. Migration `0115`
  stores `false` in every database that existed before it.
- **Where delete grants live.** In their own table, `sandbox_delete_grant`
  (`SandboxDeleteGrant`, same shape and indexes as the ordinary table). An
  older build never reads it, and MEHO with the mode off never reads or
  writes it (`_grant_models()` returns the ordinary table alone). So after
  a rollback, and on every other instance, every delete and every delete
  step waits for a person again.
- **Creating one.** `ServicePrincipalGrantService.create(...,
  from_console=True)`. With the mode on, an op with a delete sign
  (`delete_grant_reason`) becomes a delete grant instead of a refusal. It
  needs the console mark, which only the console route sets in server code
  (no request field, header or token claim can set it; otherwise
  `DeleteGrantConsoleOnlyError`, HTTP 403 `delete_grant_console_only` on
  REST), and an expiry within 30 days (`SANDBOX_DELETE_GRANT_MAX_DAYS`).
  The CLI refuses a delete op before it calls the server. The console
  route itself runs only for a tenant admin who is a person
  (`is_human_principal`): a console session that holds an agent, service
  or runner token gets 403 `human_principal_required`, as on REST.
  Ordinary grants keep today's rules in every mode. List, get, revoke, the
  orphan check and the console page cover both tables; a read entry
  carries `delete_grant=True` for a delete grant.
- **Renewing a set.** `ServicePrincipalGrantService.renew_delete_grants(...,
  from_console=True)` renews the live delete grants (not revoked, not
  expired) of one service principal together, with one new expiry. Each
  grant passes the same review as a new grant (`_review`): the mode is
  on, the call comes from the console, the op still has a delete sign,
  and the expiry is within 30 days. One failed check renews nothing. Then,
  in one transaction, each old row is revoked with the reason `renewed`
  and a new row takes its place. The console grant page shows the form
  (`grants/_renew.html`, `POST /ui/grants/service/renew-delete-grants`)
  only with the mode on; there is no REST or CLI path. MEHO never renews
  a grant by itself.
- **Using one.** `consult_and_record_grant` gets the call's `preview_hash`
  and `params` from the gate (`policy_gate(..., preview_hash, params)`,
  passed by the dispatcher; the step gate passes the params only). With
  the mode on and a delete sign on the op, `_consult_delete_grant` looks
  up a live delete grant first. A `destructive` call then runs the same
  two checks the park path runs (`dispatcher.destructive_binding_evidence`:
  the preview hash must match a fresh server-side preview, and the blast
  radius must be valid) **before** the grant-use row is written; a failed
  check writes no row and the park path returns the exact refusal
  (`preview_binding_required`, `preview_hash_mismatch`,
  `blast_radius_required`). A step gate or a child call presents no hash,
  so a `destructive` step or child stays refused. A delete below
  `destructive` needs no hash. With no delete grant the call falls through
  to the rules above, so an ordinary grant never changes its verdict.
- **The grant-use row.** `payload.delete_grant = true`, plus
  `preview_hash` and `blast_radius` for a `destructive` call, or
  `proposed_effect` for a delete below it. A step's row names its
  multi-step operation through `parent_audit_id`.
- **The step list.** `GET /api/v1/operations/governed-subops` marks delete
  steps grantable with the mode on.
- **Display.** `/ready` has a top-level `sandbox_mode` (`"on"` / `"off"`)
  for every caller and `features.approval_queue.sandbox_mode`;
  `GET /api/v1/health` has `sandbox_mode`; `meho status` prints one line
  when it is on; every console page shows a banner, with the number of
  delete grants that expire within 7 days.
- **The audit mark.** `audit_log.sandbox_mode` (boolean, not null,
  database default `false`). One model default reads the process flag, so
  every writer stamps it.
- **Chart.** `sandboxMode.enabled` and `sandboxMode.confirmHost`; the
  render fails when the host is not `ingress.host`; the notes warn.

Tests: `test_sandbox_mode_boot.py`, `test_sandbox_mode_grants.py`,
`test_sandbox_mode_calls.py`, `test_sandbox_mode_verdict_table.py` (with
the checked-in mode-off table `tests/data/sandbox_mode_verdict_table_off.json`),
`test_sandbox_mode_audit_mark.py`, `test_sandbox_mode_display.py`,
`test_chart_sandbox_mode.py`, `migrations/test_migration_0115_add_sandbox_mode.py`.

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
`DELETE:/vcenter/vm/{vm}`, which rides `_SUB_OPS_VM_CREATE`, or
`vm.destroy`'s VI-JSON step `POST:/VirtualMachine/{moId}/Destroy_Task`,
which only its action name marks as a delete, #4632) is flagged
`grantable=false` with the same refusal reason `create` would raise, so the
operator knows up front that a **partial failure of that composite still
requires a human** (the delete rollback can never run unattended). Grantable
classification single-sources `service_grants.delete_shaped_refusal_reason`,
so discovery and create-time refusal cannot drift.

## REST surface (`api/v1/service_grants.py`)

Role: **`operator`** (not `tenant_admin`) — a standing grant is the
persistent form of the approve decision an operator already makes on the
approval queue.

Create and revoke need a person. A token of an agent, a service principal
or a runner gets 403 `human_principal_required` (the same check
`require_human_principal` in `auth/rbac.py` that the approval decision
routes and the agent-grant create / elevate routes use, #4616). List and
show keep the role check alone.

| Verb | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/service-principals/grants` | List (`principal_sub`, `include_revoked`, `include_expired` (default `true`), `limit`, `offset`). |
| `GET` | `/api/v1/service-principals/grants/{id}` | Show one. 404 cross-tenant. |
| `POST` | `/api/v1/service-principals/grants` | Create (the review; `reason` required). 422 on wildcard / delete-shaped / past expiry / duplicate / `target_id`+selector. |
| `DELETE` | `/api/v1/service-principals/grants/{id}` | Revoke (soft-delete). Optional `reason` query parameter (1–2048 chars, same limit as create), stored as `revoked_reason` and on the audit row. 404 when absent / already revoked / cross-tenant. |

The route surface (grants + `GET /api/v1/operations/governed-subops`) enters
`cli/api/openapi.json` and the generated Go client (regenerate with `cd cli
&& make snapshot-openapi && make generate` after any signature change).

## CLI surface (`cli/internal/cmd/service-principal/`)

Each REST route has one `meho service-principals grants` verb. The verbs
call the generated client bindings, so they use the same `operator` role,
audit, and error rules as REST. There is no MCP tool for this plane.

| CLI verb | REST route | Notes |
|---|---|---|
| `meho service-principals grants list` | `GET /api/v1/service-principals/grants` | `--principal`, `--include-revoked`, `--include-expired`, `--limit`, `--offset`, `--json`. Shows only active grants unless you pass `--include-expired` (REST includes expired grants by default). |
| `meho service-principals grants show <grant-id>` | `GET /api/v1/service-principals/grants/{id}` | `--json`. |
| `meho service-principals grants create` | `POST /api/v1/service-principals/grants` | `--principal`, `--op-id`, `--connector-id`, `--reason` (all required); either `--target`, or `--target-product` and/or `--target-name-pattern`, or none (targetless); `--expires`; `--json`. A 422 from the server is shown as a one-line error. |
| `meho service-principals grants revoke <grant-id>` | `DELETE /api/v1/service-principals/grants/{id}` | Asks before it revokes; `--confirm` skips the question. `--json`. |

The full flag list is in the generated CLI reference
(`docs-site/reference/cli.md`). `test_service_grants_rest_plane_has_cli_parity`
in `backend/tests/test_mcp_surface_conformance.py` fails CI when a REST
route on this router has no matching CLI verb in that reference.

## Scope boundaries (this PR)

- **Console surface** — `/ui/grants` lists service-principal grants beside
  agent grants. Operators see their tenant's service-grant rows read-only;
  `tenant_admin` additionally sees agent-grant rows and may issue or revoke
  either kind through the session BFF. Service principals have no registry or
  stored display-name field, so their JWT `sub` is deliberately shown as the
  fail-open identity; concrete target UUIDs resolve to a tenant-local target
  name when available, while selector and targetless grants remain explicit.
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
  `classify_orphaned_grants` (#3430, read-time orphan flag) +
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
