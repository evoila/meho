# API shape conventions

How MEHO's REST + MCP + CLI surfaces are shaped, and the strategic
framing that picks which shapes ship at all. The conventions here
exist so a future contributor adding a new endpoint, a new connector,
or a new MCP tool doesn't have to re-litigate the same eight surface-
shape questions the v0.8.0 consumer dogfood
([`claude-rdc-hetzner-dc#771`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/771))
surfaced one at a time.

Companion documents:

- [connector-release-readiness.md](connector-release-readiness.md) — how
  to honestly describe a connector's ship state.
- [error-message-shape.md](error-message-shape.md) — how to shape error
  envelopes (sibling discipline, codified out of `#697`'s feedback).
- [`docs/planning/mvp-roadmap.md`](../planning/mvp-roadmap.md) — where
  the strategic decisions captured in §1 below get translated into
  release scope.

## 1. Strategic context — curated daily-driver + OpenAPI escape hatch

**MEHO's connector surface is hand-curated, operator-shaped wrappers.
OpenAPI-derived ingest is the escape hatch, not the eventual default.**

This is a deliberate choice surfaced by RDC #771 Finding 23 and
confirmed in the v0.9 planning conversation. The release-body framing
and the `composite_l2_missing` error envelope at v0.8.0 implied the
opposite — that running `meho connector ingest --catalog <product>/<version>`
against a vendor spec was the path to a usable connector — and the gap
between that implication and what actually works produced most of the
v0.8.0 findings stack.

### The two paths, explicitly

**Curated path (the daily-driver).** A small set of hand-coded ops per
connector, shaped for the operator's question rather than the vendor's
URL grammar. The K8s connector at v0.8.0 is the reference: 14 ops
(`k8s.node.list`, `k8s.pod.list`, `k8s.about`, `k8s.deployment.list`,
…) returning flat envelopes:

```json
{"total": 3, "rows": [
  {"name": "rke2-infra-01", "status": "Ready",
   "roles": ["control-plane", "etcd"],
   "version": "v1.34.5+rke2r1",
   "internal_ip": "10.5.50.150",
   "age_seconds": 6174825,
   "labels": {...}, "taints": []},
  ...
]}
```

These are operator-shape: the fields are what an operator types
`kubectl get nodes -o wide` to see. Sister fields like `labels` keep
apiserver fidelity for power users; the flat conveniences
(`age_seconds`, `internal_ip`, derived `roles`) save the caller from
denormalising raw API responses themselves.

**OpenAPI-derived path (the escape hatch).** Ingest the vendor's
OpenAPI spec via `meho connector ingest` → register every endpoint
as a typed `endpoint_descriptor` row → operators reach them via
`call_operation` with raw passthrough envelopes. Used when the
curated set doesn't cover what the operator needs and they're
willing to handle vendor-shape responses (`apiVersion` / `kind` /
`metadata` / `spec` / `status` nesting, plus `managedFields` /
`resourceVersion` / `finalizers` for K8s, etc.).

### Why curated is the daily-driver

- **Operator shape is the quality bar.** A flat
  `{name, status, version, internal_ip, age_seconds, labels, taints}`
  is what the agent and the human operator both want. Raw apiserver
  envelopes leak. The compose layer that would shape them lives on
  the caller side — duplicated across every script, every agent,
  every UI surface.
- **`requires_approval` only works on curated ops.** The G11.2
  approval-queue + G11.4 sanitization machinery key on per-op
  annotations (`requires_approval=true`, sanitization policy, etc.).
  An auto-ingested 1275-op surface either annotates none of them
  (no governance) or grows another maintenance dimension equal to
  manual curation.
- **Composite recursion (G0.6-T7) is the answer to "I need a
  multi-call workflow".** L2 composites compose L1 curated ops with
  full audit + approval discipline. That's the right structure for
  "list VMs in cluster → power off each → enter maintenance," not a
  raw passthrough surface.
- **Per-release progress is legible.** "v0.8.0 adds 5 ops to the
  vmware composite set" is a clean unit of progress. "v0.8.0 ingests
  the v9.0 spec and exposes 1275 ops" is a unit that doesn't survive
  contact with the consumer-shape question.

### What this changes downstream

- **Release-body framing.** Connector lines describe the curated
  scope shipped, not the OpenAPI surface area. "github-rest ships
  with 1 L1 composite + 4 approval-gated writes; full ingest is the
  escape hatch" reads honest. "github-rest ships with the GitHub
  REST API" reads aspirational.
- **`composite_l2_missing` envelope wording.** The error names the
  missing op + recommends curated-op authoring (file an issue, ask
  the meho team for the missing L1 wrapper, OR use the dispatch
  escape hatch via raw `call_operation`). The `catalog_command`
  field stays as the escape-hatch recipe; it stops being the
  recommended path.
- **`docs/cross-repo/` operator runbooks** describe what's curated.
  Operators who reach for ingest learn the escape-hatch caveats
  (raw shape, no approval annotations, possible commit-pass cost) up
  front.

### The escape hatch must still be operable

The OpenAPI-derived path can't crash the pod. The escape hatch needs
to survive real vendor specs (7+ MB OpenAPI documents with
1000+ ops). G0.16-T1 (#1303) closes that: `POST /api/v1/connectors/ingest`
defaults to `async=true` and returns `202 Accepted` + a job handle;
the heavy commit + LLM-grouping pass runs off the request thread, so
the kubelet liveness probe sees a quick request return and the pod
stays Ready. Operators poll
`GET /api/v1/connectors/ingest/jobs/{job_id}` for status. The
`dry_run=true` path stays synchronous because the parse-only leg
already returns inside the liveness budget on real-world specs
(per RDC #771 Finding 21). Full shape in
[spec-ingestion.md](spec-ingestion.md) §"Async ingest mode".

The same task reworded the `composite_l2_missing` envelope per the
strategic framing in this section: the human message stops
describing the catalog command as "the remediation path"
(operators read that as a recommendation and followed it into the
pod-restart loop) and names the curation-gap framing first, with
the L1-wrapper request as the recommended path and the
`catalog_command` retained as the escape-hatch recipe. The
structured `extras` payload (`error_code`, `missing_op_ids`,
`catalog_command`) is unchanged -- agents that branch on those
fields continue to work without migration. It's a SEV-3 ("escape
hatch shouldn't crash the pod") not a SEV-1 ("daily-driver path
broken"). The curated daily-driver works; the escape hatch now
fails gracefully.

## 2. List-endpoint envelope shape

**Every list endpoint returns `{items, next_cursor?, ...sidecars}`.**

RDC #771 Finding 3 catalogued 5 list endpoints across one OpenAPI
shipping 3 different shapes:

| Endpoint | v0.8.0 shape |
|---|---|
| `GET /api/v1/connectors` | `{"connectors": [...]}` |
| `GET /api/v1/targets` | `[...]` (bare array) |
| `GET /api/v1/conventions` | `{"budget_status": {...}, "entries": [...]}` |
| `GET /api/v1/audit/my-recent` | `{"rows": [...], "next_cursor": "..."}` |
| `GET /api/v1/broadcast/overrides` | `[...]` (bare array) |

Three shapes across five sister endpoints. A generic "list anything
from MEHO" SDK helper needs custom parsing per call site.

### The convention

```json
{
  "items": [ ... ],
  "next_cursor": "<opaque string | null>",
  "budget_status": { ... }   // optional sidecar, per-endpoint
}
```

- **`items`** — the canonical list field, never renamed per endpoint
  (`connectors` / `entries` / `rows` are all out; the surface is
  cataloguing "items of the resource at this URL", not naming the
  resource a second time).
- **`next_cursor`** — present when the resource paginates; `null`
  when this page is the last. Cursor values are opaque to the
  client (the server may use Valkey stream ids, ULIDs, offset
  encodings, …).
- **Sidecar fields** — endpoint-specific. `budget_status` for
  conventions, `total_count` for surfaces that can compute it
  cheaply, etc. Sidecars are at the top level (not nested under a
  `meta` envelope) so a client that only reads `items` doesn't
  walk extra structure.

### Bare arrays are out

Bare arrays foreclose adding pagination, sidecars, or telemetry
later without a breaking change. The cost of an extra `{"items":}`
wrap is zero in JSON bytes; the cost of "we shipped a bare array and
now need to paginate" is a v-bump breaking change.

### Migration shape

A bare-array endpoint that gains the `{items, ...}` wrap is a
breaking change. The migration shape:

1. Add a `?envelope=v2` query parameter. v2 returns the new shape;
   omit → v0.x bare-array behaviour.
2. After two release cycles, flip the default and document the
   bare-array shape as deprecated.
3. After three more release cycles, remove the bare-array path.

In practice the SEV-4 sweep that motivated this doc will batch the
migration onto a single connector-doc-versioned bump (v0.10.0?) so
adopters change every list call at once.

Code reference: G0.16-T6 Finding A (#1312) lands the reference
adoption on
[`GET /api/v1/targets`](../../backend/src/meho_backplane/api/v1/targets.py)
via the shared helper
[`backend/src/meho_backplane/api/v1/_envelope.py`](../../backend/src/meho_backplane/api/v1/_envelope.py)
(``EnvelopeVersion`` type, ``ENVELOPE_QUERY`` declaration,
``wrap_v2_envelope`` builder). All five list endpoints accept
the opt-in: `targets` (the reference) and the topology
`dependents`/`dependencies` reads in #1312, plus the four sister
endpoints (``conventions`` / ``audit/my-recent`` /
``broadcast/overrides`` / ``connectors``) in G0.18-T3 (#1356),
completing #1312 acceptance A. The two runbook list endpoints
(``GET /api/v1/runbooks/templates`` / ``GET /api/v1/runbooks/runs``)
shipped after that sweep with product-specific keyed shapes
(``{"templates": [...]}`` / ``{"runs": [...]}``) and joined the
opt-in in G0.22-T6 (#1611). The ``connectors``, ``conventions``,
``broadcast/overrides``, and both runbook lists are unpaginated, so
their ``next_cursor`` is always ``null`` under the opt-in;
``conventions`` carries ``budget_status`` as a top-level sidecar.

The sister endpoints with a named/typed v0.8.0 response model
(``conventions`` → ``ConventionListResponse``, ``audit/my-recent``
→ ``AuditQueryResult``, ``broadcast/overrides`` →
``[BroadcastOverrideRead]``, ``runbooks/templates`` →
``RunbookTemplateListResponse``, ``runbooks/runs`` →
``RunbookListRunsResponse``) keep ``response_model`` on the route and
emit the v2 envelope via a raw ``JSONResponse`` in the opt-in branch.
That preserves the named OpenAPI 200 schema — and the typed CLI
client generated from it — while still returning the unified shape
under ``?envelope=v2`` (a union return type would collapse the
schema to ``anyOf`` and break the generated Go client).

Sister-surface forwarding: the MCP list tools call the service layer
in-process and return their own list shapes (no HTTP ``?envelope=``
param to forward), and the CLI typed clients consume the v0.8.0
default shape — so neither forwards the opt-in.

## 3. Enum vocabulary discipline

**One identifier per concept across every layer that names it.**

RDC #771 Findings 6 + 7 caught two enum mismatches:

- **Finding 6: product enum.** The TargetCreate enum spelled the
  SDDC Manager product as `"sddc-manager"`; `meho connector list`
  (and the connector-listing API) emit `product: "sddc"` — the
  token `parse_connector_id("sddc-rest-9.0")` derives, which is
  load-bearing for the §11 connector_id round-trip contract and so
  cannot change. An operator copying `sddc` out of the listing into
  a target create saw a 422 saying `sddc-manager`. Resolved by a
  **product alias** (see "Aliases for split-token concepts" below),
  not by churning either spelling: RDC #789 Finding 6 re-verified
  G0.16-T6's "verified already aligned" claim as **false** (the
  split persisted on v0.8.1) and re-tracked it as #1355, which made
  the listed `sddc` token accept-equivalent at the POST validator.
- **Finding 7: preferred_impl_id enum.** TargetCreate validated
  against base impl-id names (`nsx-rest`); TargetUpdate accepted
  the versioned form (`nsx-rest-4.2`). Same field, two different
  enums depending on whether you POST or PATCH.

### The convention

Pick one identifier per concept and use it everywhere the concept
is named:

- **TargetCreate / TargetUpdate enums** are the canonical source.
  Anywhere else (catalog `product` field, connector source's
  `auth_model`, MCP tool param descriptions, …) must match.

  Code reference: the regression test
  :func:`test_catalog_product_field_matches_target_create_enum`
  in
  [`backend/tests/test_operations_ingest_catalog.py`](../../backend/tests/test_operations_ingest_catalog.py)
  pins the catalog ↔ enum convergence structurally so a future
  drift fails at unit-test time rather than surfacing as a 422
  on the operator's first POST (G0.16-T6 Finding B #1312;
  closes the residual surface of RDC #771 Finding 6).

#### Aliases for split-token concepts

One identifier per concept is the rule; the **alias** is the
escape valve for the case where a concept is *forced* to carry two
spellings on two surfaces because each surface has an independent
hard constraint. SDDC Manager is the lone instance: the v2
registry, the spec catalog, and the `TargetCreate` validator all
use `"sddc-manager"`, while `meho connector list` must emit
`"sddc"` (the token `parse_connector_id("sddc-rest-9.0")` derives —
the §11 connector_id round-trip contract pins
`parse_connector_id(connector_id)[0]` to equal the emitted
product, so the listing token cannot be changed without breaking
dispatch resolution).

The reconciliation is a one-hop alias map, not a second canonical
spelling:

- [`PRODUCT_ALIASES`](../../backend/src/meho_backplane/connectors/registry.py)
  maps the non-canonical token to the canonical registry token
  (`{"sddc": "sddc-manager"}`).
- [`canonical_product_token`](../../backend/src/meho_backplane/connectors/registry.py)
  normalises a supplied token through that map (identity for every
  non-alias token, so it is idempotent).
- `POST /api/v1/targets`
  ([`create_target`](../../backend/src/meho_backplane/api/v1/targets.py))
  and `PATCH /api/v1/targets/{name}`
  ([`update_target`](../../backend/src/meho_backplane/api/v1/targets.py))
  canonicalise the incoming `product` **before** validating against
  `registered_product_tokens` and **before** storing the row, so a
  value copied straight out of `connector list` is accept-equivalent
  and the persisted row always carries the canonical token.

The alias is intentionally *not* surfaced in the OpenAPI
`TargetCreate.product` enum (which stays the canonical set) — the
enum advertises the one spelling tooling should generate against;
the alias is a forgiving-input accommodation at the write boundary,
not a second first-class value.

Code reference: the structural drift-guard
:func:`test_list_emitted_product_token_accept_equivalent_at_targets_post`
in
[`backend/tests/test_api_v1_connectors_ingest.py`](../../backend/tests/test_api_v1_connectors_ingest.py)
pins, for **every** registered connector, that the product token the
listing emits canonicalises into the POST-accepted set — so a future
SDDC-shaped split (a new connector whose listing token diverges from
its registry token without an alias entry) fails at unit-test time
rather than as a 422 on the operator's first copy-paste (G0.18-T2
#1355; RDC #789 Finding 6, closing #1312 acceptance B — which the
v0.8.1 dogfood re-verified as not actually done).

  **Aliases (rare, narrow).** When a single concept already has two
  established spellings the codebase can't merge without breaking a
  load-bearing invariant elsewhere — the SDDC `sddc` / `sddc-manager`
  case is the live precedent — the bridge is a `PRODUCT_ALIASES` map
  in
  [`backend/src/meho_backplane/connectors/registry.py`](../../backend/src/meho_backplane/connectors/registry.py),
  consumed at the write surfaces (`POST` / `PATCH /api/v1/targets`)
  via `canonical_product_token()`. The non-canonical spelling is
  accept-equivalent on write; the canonical token is what gets
  stored, so the resolver, the audit log, every list / detail read,
  and the OpenAPI enum see one spelling regardless of which the
  operator typed. The alias map is keyed by the non-canonical
  spelling and valued by the canonical registry token, and an
  alias key is never also a canonical token (so
  `canonical_product_token` is idempotent). RDC #789 Finding 6 /
  G0.18-T2 #1355 introduced the bridge and closes #1312 acceptance B
  (which had marked Finding 6 "already aligned" without actually
  reconciling).

  This is a constrained exception, not an open invitation to add
  more synonyms. The motivating constraint for `sddc` /
  `sddc-manager` is that the `meho connector list` token is
  parser-derived from the connector id (`parse_connector_id(
  "sddc-rest-9.0")` → `"sddc"`) and round-trips through the
  G0.9.1-T1 #773 contract; changing the listing token would
  break that round-trip. Without a comparable structural
  constraint on a new product, the right move is to reconcile
  the spellings (catalog / connector class / docs) rather than
  paper over them with an alias.
- **Versioned vs base impl-ids** — pick one. The recommendation
  is **versioned** (`nsx-rest-4.2`) because:
  - Versioned is more specific (avoids ambiguity when multiple
    versions of a connector ship in one release).
  - The resolver's wildcard fan-out (G0.14-T2) already handles
    "no version specified, pick the best match" cleanly.
  - The release-readiness doc cites connectors by versioned
    impl_id throughout.

  Code reference: `_registered_impl_ids` in
  [`backend/src/meho_backplane/api/v1/targets.py`](../../backend/src/meho_backplane/api/v1/targets.py)
  and the `preferred_impl_id` branch of `_run_tie_break_ladder`
  in [`backend/src/meho_backplane/connectors/resolver.py`](../../backend/src/meho_backplane/connectors/resolver.py)
  both accept the versioned form alongside the base form
  (G0.16-T6 Finding C #1312); the resolver normalizes versioned →
  base before matching candidates.
- **Connector-protocol vocabulary stays inside the connector.**
  GitHub's "App vs PAT" is a connector-internal concern; the
  TargetCreate enum sees only the identity-model dimension
  (`shared_service_account`). The connector inspects `secret_ref`
  Vault fields to decide which protocol path to take. This is
  the recommendation for G0.16-T2.

### The enum-validation 422 envelope

Already good — see RDC #771 Finding 7's quoted envelope, which
includes the line "The resolver silently ignores unknown impl_id
overrides; this 422 surfaces the foot-gun at write time." That
style of envelope-explains-why-it-validated stays. The convention
here is about *which values* the enum carries, not how it reports
violations.

## 4. REST ↔ MCP envelope agreement

**Sister operations return the same shape on REST and MCP.**

RDC #771 Finding 10: `query_topology dependents` returns a bare
array on REST and `{kind: "dependents", nodes: [...]}` on MCP.
A script that calls both surfaces has to write two parsers.

### The convention

When a REST endpoint and an MCP tool name the same conceptual
operation, their response envelopes agree. If the REST endpoint
returns a paginated list, the MCP tool returns the same
`{items, next_cursor?, ...sidecars}`. If REST wraps with a
`kind` discriminator, MCP does too.

The MCP tool-call layer can attach a layer of MCP-specific
metadata (the `meta` block JSON-RPC defines), but the `result`
payload shape mirrors REST.

### Why MCP first, REST second

When the two diverge, the MCP shape is usually the more
considered one (more recent, more agent-facing). Migration goes
REST-toward-MCP, not the other way.

Code reference: G0.16-T6 Finding E (#1312) lands the migration
on `GET /api/v1/topology/dependents/{name}` and
`/dependencies/{name}` via the `?envelope=v2` opt-in (shared
helper from Finding A). Default response stays the v0.8.0 bare
`list[TopologyNode]` so no client breaks; the opt-in returns
`{"kind": "dependents", "nodes": [...]}` matching the MCP
`query_topology` tool's response. The wider topology endpoint
set (`path` / `edges` / `timeline` / `diff` / `history`) ships
in a follow-up Task — those endpoints already return typed
dict envelopes (no bare list) so the v2 opt-in needs an
endpoint-specific decision on whether to retain the existing
field names or migrate to the §2 `items` form.

## 5. List ↔ detail field consistency

**List endpoints return the same fields as their detail siblings.**

RDC #771 Finding 8: `GET /api/v1/targets` returned
`version: null, secret_ref: null, preferred_impl_id: null` for a
target whose `GET /api/v1/targets/{name}` returned actual values.
Adopters either write N+1 calls (list → loop → detail) or accept
silent data masking.

### The convention

If `GET /api/v1/{resource}/{id}` exposes a field, the
corresponding list endpoint exposes the same field with the same
value. No silent field masking.

When N+1 cost is a real concern (large resources, expensive
joins), the convention is to ship two separate endpoints with
explicit names, not one endpoint that silently nullifies
expensive fields:

- `GET /api/v1/{resource}` — full shape (paginated).
- `GET /api/v1/{resource}/summary` — minimal shape, explicitly
  documented as "for high-volume list views; fall through to
  detail for full state".

Documenting the projection is the convention. Silently
projecting and surfacing the same shape as detail is the
anti-pattern.

Code reference: :class:`TargetSummary` in
[`backend/src/meho_backplane/targets/schemas.py`](../../backend/src/meho_backplane/targets/schemas.py)
mirrors :class:`Target`'s field set with the two deliberate
omissions (``notes``, ``extras``) called out as
operator-authored free-form blobs that inflate the list page
without serving the common "names + routing" question. The
regression test
:func:`test_target_summary_field_set_superset_of_target` pins
the contract structurally so a future field added to
:class:`Target` without a matching :class:`TargetSummary`
update fails CI (G0.16-T6 Finding D #1312).

## 6. Event-stream discriminators

**Multi-shape event streams carry an explicit `kind` field.**

RDC #771 Finding 13: `meho:feed:{tenant_id}` carries two distinct
event shapes:

- `event_kind: "agent_announcement"` with `activity / target /
  phase` fields.
- audit-derived events with `op_id / op_class / payload` fields
  and no `event_kind`.

A `broadcast.recent` consumer that switches on `op_id` silently
nullifies the agent-authored half (no `op_id` present).

### The convention

Streams that carry multiple event shapes name each shape
explicitly:

```json
{
  "id": "1779981800453-0",
  "kind": "agent_announcement" | "operation",
  "tenant_id": "...",
  ...kind-specific fields...
}
```

Consumers switch on `kind`. No nullable-fields convention; no
"infer from which fields are populated" anti-pattern.

The migration is similar to §2: add a `kind` field to every
write, normalize consumers to switch on it, deprecate the
"infer from fields" path over two release cycles.

Code reference: G0.16-T6 Finding F (#1312) lands the `kind`
field on both writers:

- [`BroadcastEvent.kind`](../../backend/src/meho_backplane/broadcast/events.py)
  defaults to `"operation"` (the audit-derived majority shape;
  pre-migration entries lacking the field on the wire fall back to
  the same default, so the historical window doesn't need a
  data-migration sweep).
- [`AgentAnnouncementEvent.kind`](../../backend/src/meho_backplane/broadcast/agent_events.py)
  is `Literal["agent_announcement"]`; the historical `event_kind`
  field stays serialised as a backward-compat alias so v0.8.0
  in-flight stream entries continue to round-trip.
- The shared consumer
  [`broadcast.history.parse_entry`](../../backend/src/meho_backplane/broadcast/history.py)
  switches on the top-level `kind` first, falling back to
  `event_kind` for the v0.8.0 shape.

## 7. Probe ↔ dispatch path agreement

**The probe path and the dispatch path read credentials, resolve
connectors, and surface errors through the same code.**

RDC #771 Findings 4 + 17: K8s probe fails with Vault OIDC
`malformed jwt: must have three parts` while K8s dispatch
succeeds against the same target. gh-rest probe rejects the
`auth_model` enum that gh-rest dispatch accepts.

Two distinct failure mechanisms, same class: the probe path
and dispatch path diverged. They must converge.

### The convention

`POST /api/v1/probe/{target}` and `POST /api/v1/operations/call`
share one credential-loader call site, one connector-resolver
call site, and (where relevant) one connector-instance method.

The probe payload differs (it's the connector's `fingerprint`
method rather than an operator-supplied `op_id`), but the
path-shaping infrastructure underneath is one code path with
one set of failure modes.

This is what G0.16-T4 enforces for the Vault OIDC half;
G0.16-T2 enforces for the auth_model half. Both are sibling
manifestations of the same convention violation.

## 8. Docs ↔ impl parameter shape agreement

**Tool descriptions and OpenAPI parameter docs say what the
implementation actually accepts.**

RDC #771 Finding 15: MCP `broadcast.recent`'s `since` parameter
description says "ISO-8601 timestamp or Valkey stream cursor";
the implementation rejects ISO with `invalid_cursor: expected
Valkey stream id`. Adopters trust the docs.

### The convention

Two acceptable resolutions when the docs and impl disagree:

1. **Update the docs.** The cheaper fix when the impl is doing
   the right thing.
2. **Extend the impl.** When the docs describe the operator-
   useful shape (an ISO timestamp is easier to type than a
   Valkey id), the impl should grow the parser to accept it.

The anti-pattern is "the docs are aspirational; the impl is
what it is". Both are part of the API surface; they must agree.

Code reference: G0.16-T6 Finding G (#1312) reconciled the
``since`` parameter across both fronts on resolution (a)
(extend the impl). The MCP ``meho.broadcast.recent`` parser
in
[`backend/src/meho_backplane/broadcast/history.py`](../../backend/src/meho_backplane/broadcast/history.py)
(``_normalise_since`` + ``_iso8601_to_min_cursor``) already
accepted ISO; the REST ``GET /api/v1/feed`` cursor validator
in
[`backend/src/meho_backplane/api/v1/feed.py`](../../backend/src/meho_backplane/api/v1/feed.py)
(``_validate_cursor_or_400`` + ``_normalize_iso_to_cursor``)
joined it on the same dual-acceptance contract — operator
types a timestamp, both surfaces normalise to a bare-ms Valkey
cursor under the hood.

A `make check-docs-impl-agreement` CI gate is the long-game
enforcement here, generated by parsing tool descriptions and
spinning up a property-based test that exercises the documented
shapes. Out of scope for the initial sweep; flagged as a
follow-up Task.

## 9. Catalog ↔ explicit-quadruple ingest connector_id agreement

**The catalog-driven path and the explicit-quadruple path land on
one connector_id per logical connector, or document the
divergence explicitly.**

RDC #771 Finding 22: the vmware catalog entry registers as
`vmware-rest-9.0`; the explicit-quadruple ingest with the same
spec's `info.version="9.0.0.0"` produces `vmware-rest-9.0.0.0`.
Two connector_ids, two distinct catalogs, one logical connector.
The resolver routes the rdc-vcenter target to `vmware-rest-9.0`,
so the explicit-quadruple ingest doesn't help that target's
dispatch path.

### The convention

One of two resolutions per connector family:

1. **Reconcile the catalog `version` with the spec's
   `info.version`.** Operators ingesting under the catalog name
   land on the same connector_id as the catalog ships under.
   The catalog `version` field IS the spec's `info.version`.
2. **Decouple the catalog `version` from the spec's
   `info.version`.** The catalog declares
   `spec_info_versions_compatible: ["9.0.x"]`; the validator
   accepts spec ingest under the catalog label as long as the
   spec's `info.version` matches the compatibility range. This
   is what G0.16-T5 (gh/3 vs `info.version=1.1.4`) lands.

The choice is per-connector-family because GitHub-shape APIs
(where the catalog version is a product-line label, divergent
from the spec's documentation version) need (2), while
VMware-shape APIs (where the catalog version IS the product
version) work cleanly with (1).

Code reference: the catalog schema field
``spec_info_versions_compatible`` in
[`backend/src/meho_backplane/operations/ingest/catalog.py`](../../backend/src/meho_backplane/operations/ingest/catalog.py)
(plus the
``spec_info_version_matches_compatibility_specifier`` helper)
implements resolution (2). The shipped vmware entry adopts
``spec_info_versions_compatible: ["9.0.x"]`` as a
belt-and-suspenders declaration over the PEP-440 prefix-match
that already treats ``"9.0"`` ↔ ``"9.0.0.0"`` as exact
(G0.16-T6 Finding H #1312); T5 (#1307) carries the
load-bearing application on the gh-rest entry where the
divergence (``"3"`` ↔ ``"1.1.4"``) blocks ingest without it.

## 10. Intra-connector list-op request-shape parity

**Sibling list operations on one connector share one input-parameter
shape.**

RDC #771 post-cycle rolling dogfood (2026-05-29) Finding 24:
`k8s.event.list` required `namespace` and rejected `all_namespaces`,
while `k8s.pod.list` on the same connector accepted
`{all_namespaces: true}` and listed cluster-wide in one call. The
asymmetry forced an N-namespace client-side loop for "show me all
Warning events cluster-wide" — `kubectl get events -A` in one call
was impossible.

Where §2 and §5 govern list *response* shape, this governs list
*request* shape. The K8s list-op family at v0.8.0 split two ways:

| Operation | namespace | all_namespaces | label_selector | field_selector | paging |
|---|---|---|---|---|---|
| `k8s.pod.list`, `k8s.deployment.list` | XOR | ✓ | ✓ | ✓ | limit + continue_token |
| `k8s.event.list` | required | — | — | ✓ | limit |
| `k8s.service.list`, `k8s.ingress.list`, `k8s.configmap.list` | required | — | — | — | — |

All six resources are namespaced in Kubernetes, and the upstream
client exposes `list_X_for_all_namespaces` + `label_selector` +
`field_selector` for every one — so the divergence was a MEHO
authoring choice, not a vendor constraint. G0.17-T1 (#1330, merged
via #1332) factored the workload ops' private
`_LIST_BASE_PROPERTIES` + `_NAMESPACE_XOR_ALL_NAMESPACES` out into
`connectors/kubernetes/ops_listparams.py` and converged the
remaining four list ops onto that shape; `LIST_BASE_PROPERTIES` +
`NAMESPACE_XOR_ALL_NAMESPACES` are now the canonical reference.

### The convention

Sibling list operations over the same kind of scoped resource on one
connector share one input-parameter shape:

- A **cross-scope flag** (`all_namespaces`, `all_projects`,
  `--recursive`, …) is uniformly present or uniformly absent across
  the siblings — and when present, expressed the same way (here:
  `namespace` XOR `all_namespaces` via a shared `oneOf`).
- **Common server-side filters** (`label_selector`, `field_selector`)
  and paging knobs (`limit`, continue/cursor token) are offered
  consistently across siblings — or an omission is documented
  per-op, the way `event.list` documents its deliberate
  `continue_token` omission via
  `K8S_EVENT_LIST_PAGINATION_HINT` (recency-sort + truncation
  supersedes server-side paging for events).
- **The shared shape lives in one place** (`LIST_BASE_PROPERTIES` +
  the `NAMESPACE_XOR_ALL_NAMESPACES` `oneOf` in
  `ops_listparams.py`), imported by every sibling rather than
  copy-pasted, so the schema and its validation test stay in
  lockstep.

The reference shape is `k8s.pod.list`. A new list op spreads
`LIST_BASE_PROPERTIES`; an op that legitimately omits a knob
cherry-picks the individual property blocks (`NAMESPACE_PARAM` /
`ALL_NAMESPACES_PARAM` / `LABEL_SELECTOR_PARAM` /
`FIELD_SELECTOR_PARAM` / `LIMIT_PARAM` / `CONTINUE_TOKEN_PARAM`)
it does support and documents the omission in its docstring. A
genuinely cluster-scoped resource (`k8s.node.list`,
`k8s.namespace.list`) has no `namespace` / `all_namespaces` axis at
all and neither block applies.

### Migration shape

Unlike §2's response-envelope migration, adding an input parameter
is backward-compatible — it widens what's accepted, and existing
`{namespace}` calls keep working — so no `?envelope=v2` gate is
needed. Add the parameter, branch the handler to the all-namespaces
client call (`list_event_for_all_namespaces`, …), forward
`label_selector`, and add the schema-XOR test plus an
all-namespaces dispatch test mirroring the `pod.list` pair.

Paging (`limit` + `continue_token`) on `service` / `ingress` /
`configmap` was deferred under #1332 — typically O(10)/namespace
resources, mechanical to add later by spreading `LIMIT_PARAM` +
`CONTINUE_TOKEN_PARAM` from the same module.

## 11. Where the conventions live in code

When a future contributor lands a new endpoint, the conventions
above should already be visible in the adjacent code:

- **List endpoints** — `meho_backplane/api/v1/connectors.py`,
  `meho_backplane/api/v1/targets.py`,
  `meho_backplane/api/v1/conventions.py` — the §2 envelope.
- **Enum sources** — `meho_backplane/models/targets.py`
  (`TargetCreate` / `TargetUpdate` Pydantic schemas) carry the
  canonical product / `auth_model` / `preferred_impl_id` enums.
- **REST ↔ MCP sister operations** —
  `meho_backplane/mcp/tools/` and `meho_backplane/api/v1/` mirror
  each other; the §4 convention is enforced by mirroring tests.
- **Probe ↔ dispatch** —
  `meho_backplane/operations/dispatch.py` is the shared call
  site; `meho_backplane/api/v1/probe.py` calls into it.
- **Connector list-op input shapes** —
  `meho_backplane/connectors/kubernetes/ops_listparams.py`
  (`LIST_BASE_PROPERTIES` + `NAMESPACE_XOR_ALL_NAMESPACES` + the
  per-knob property building blocks) is the §10 reference; sibling
  list ops import it rather than re-declaring `namespace` /
  `all_namespaces` / `label_selector` / paging knobs.

Each section's example endpoint is the reference shape. New
endpoints copy from there.

## 12. How this doc gets updated

This file grows in the same shape as
[error-message-shape.md](error-message-shape.md): one section per
codified convention, each section citing the consumer-feedback
finding that surfaced the need. New conventions land as new
sections during the dogfood cycle they emerge from. The
convention itself is what we hold to going forward; the historical
finding is the citation, not the convention.

## 13. Route-prefix placement: `/api/v1/*` vs the `/mcp` carve-out

**Every chassis HTTP surface lives under `/api/v1/*`. The MCP
endpoint is the lone, deliberate carve-out at `/mcp` — root-mounted,
unversioned, single-path. Tool *names* are not URL path segments;
splicing a tool name onto the REST prefix produces a phantom path.**

RDC #789's `mcp-route-and-response-format-changed-v0.8.0` finding
(judged INVALID-as-framed) is the third dogfood cycle in a row where
a consumer's probe script reached for `/api/v1/mcp` (and, paired,
`/api/v1/query/topology`) on the assumption that "everything else is
`/api/v1/*` so MCP must be too" / "the MCP tool is called
`query_topology` so the REST endpoint must be
`/api/v1/query/topology`." Neither path has ever existed in any tag
of either repo (archaeology across all 10 tags + 1492 commits;
canonical MCP endpoint introduced in v0.2.0 #266 and never moved).
The routes are correct and stable; the doc gap that re-seeds the
wrong guess is what this section closes.

### The convention

- **Versioned chassis routes** — every operator-facing HTTP surface
  that the chassis owns (targets, topology, connectors, audit, feed,
  health, runbook templates / runs, kb, memory, operations, retrieve,
  broadcast overrides, agent grants, …) lives under `/api/v1/*`. The
  prefix is registered in
  [`backend/src/meho_backplane/main.py`](../../backend/src/meho_backplane/main.py)
  via per-router `app.include_router(...)` calls; each router declares
  `APIRouter(prefix="/api/v1/<resource>", ...)`.
- **MCP root-mount carve-out** — the MCP endpoint is mounted at
  bare `/mcp`, unversioned and outside the `/api/v1/*` namespace.
  The router declares
  [`APIRouter(prefix="/mcp", tags=["mcp"])`](../../backend/src/meho_backplane/mcp/server.py)
  and the single dispatch handler is
  [`@router.post("")`](../../backend/src/meho_backplane/mcp/server.py);
  mounted by `app.include_router(mcp_router)` in
  [`main.py`](../../backend/src/meho_backplane/main.py)
  with no prefix override.
- **MCP tool names are not URL path segments.** Tool names
  (`query_topology`, `call_operation`, `search_knowledge`, …) are
  JSON-RPC method-call parameters passed in the body of `POST /mcp`;
  they never appear in any URL. Their corresponding REST endpoints
  (when one exists) name the *resource*, not the *tool*:
  `query_topology` ↔ `/api/v1/topology/*`, `query_audit` ↔
  `/api/v1/audit/*`, `call_operation` ↔ `/api/v1/operations/call`.

### Why MCP is root-mounted (load-bearing — do not "fix")

The carve-out is required by the MCP 2025-06-18 transport contract
and the OAuth 2.1 resource-server pattern §4 documents:

- **MCP clients use the bare server URL.** [Claude.ai Custom
  Connector](https://modelcontextprotocol.io/docs/develop/connect-remote-servers),
  MCP Inspector, Cline, Continue, and every other spec-conformant
  client expect a single Streamable-HTTP endpoint URL — they do not
  walk a versioned API prefix. Operators paste
  `https://meho.example.com/mcp` into the connector card; the client
  speaks JSON-RPC at that URL and nowhere else.
- **The protected-resource discovery URL is built off `/mcp`.**
  [RFC 9728 §4.1](https://datatracker.ietf.org/doc/html/rfc9728#section-4.1)
  pins the `resource` claim on the metadata document at
  `/.well-known/oauth-protected-resource`; MEHO defaults
  `MCP_RESOURCE_URI=${BACKPLANE_URL}/mcp`
  ([`backend/src/meho_backplane/settings.py`](../../backend/src/meho_backplane/settings.py),
  also called out in
  [`docs/cross-repo/mcp-client-setup.md`](../cross-repo/mcp-client-setup.md)
  Step 1).
- **The OAuth `aud` claim is bound to that exact URI.** Tokens issued
  by Keycloak for MCP carry `aud=${BACKPLANE_URL}/mcp` (mandated by
  the audience-protocol-mapper recipe in
  [`docs/cross-repo/mcp-client-setup.md`](../cross-repo/mcp-client-setup.md)
  Step 1). The MCP audience is **distinct** from the chassis HTTP-API
  audience (`KEYCLOAK_AUDIENCE`) — an HTTP-API JWT does not satisfy
  the MCP route and vice versa
  ([`docs/architecture/mcp.md`](../architecture/mcp.md) §"OAuth 2.1
  resource-server pattern").

A "compat alias" via 308 redirect from `/api/v1/mcp` → `/mcp` would
not help: the OAuth token's `aud` is bound to `${BACKPLANE_URL}/mcp`
(no `/api/v1` prefix), so a client following the redirect would 401
post-redirect with `invalid_audience`. There is no useful fix on the
code side. The fix is documentation.

### Phantom paths that have never existed

For the avoidance of doubt — and for future consumer probe scripts
that grep this section before trying to derive a URL:

| Phantom path | Why guessed | What's actually there |
|---|---|---|
| `POST /api/v1/mcp` | "Everything else is `/api/v1/*` so MCP must be too." | `POST /mcp` (root-mounted, since v0.2.0 #266; never moved). A request to `/api/v1/mcp` returns 404. |
| `POST /api/v1/query/topology` | "The MCP tool is called `query_topology` so the REST endpoint must be too." | REST: `GET /api/v1/topology/{dependents,dependencies,path,…}/{name}` (since v0.2.1 #560). MCP: the `query_topology` tool dispatches under `POST /mcp` with `method="tools/call"` and `params.name="query_topology"`. The string `query_topology` is a JSON-RPC method-call parameter, not a path segment. |
| `POST /api/v1/query/audit`, `…/call/operation`, `…/search/operations`, … (any `/api/v1/<tool-name-with-underscores-rewritten-as-slashes>`) | Same splice as the topology row. | `POST /mcp` for the MCP tool; the REST sister (if any) names the *resource* (`/api/v1/audit/*`, `/api/v1/operations/call`, `/api/v1/operations/search`), not the tool. |

The complete agent-facing MCP tool surface (~17 meta-tools) is
listed in [`docs/architecture/mcp.md`](../architecture/mcp.md) "Architectural
correction (2026-05-14)"; none of these names is ever a URL path
segment.

### Response format (so consumers don't re-litigate "format changed")

- **HTTP verb + content-type.** `POST /mcp` with
  `Content-Type: application/json`. Body is a single JSON-RPC 2.0
  envelope — batch arrays are unsupported (MCP Streamable HTTP
  mandates single envelopes).
- **Response body.** JSON-RPC 2.0 over **plain JSON** (no SSE, no
  chunked streaming on the response). Stable since v0.2.0. The
  response shapes per spec §"Sending Messages to the Server" are
  catalogued in
  [`docs/architecture/mcp.md`](../architecture/mcp.md) "Transport"
  (Request → 200 + JSON envelope; Notification → 202 no body; parse
  / invalid-request errors → 200 + JSON-RPC error envelope;
  unsupported `MCP-Protocol-Version` → 400 + JSON-RPC error per spec
  MUST; etc.).
- **`GET /mcp` returns HTTP 405.** FastAPI's default for an
  unmatched method, which satisfies the spec's fallback when the
  server doesn't implement the GET-opens-SSE branch of the
  Streamable HTTP transport. The 405 is intentional — not a missing
  route, not a misconfiguration.

### Forward convention for new surfaces

- **New chassis HTTP endpoints** go under `/api/v1/*`. No new
  unversioned root mounts.
- **New MCP tools** are added under the existing `/mcp` endpoint as
  JSON-RPC method-call params (see
  [`docs/architecture/mcp.md`](../architecture/mcp.md) "Adding an
  MCP tool"). No new MCP-shaped routes, no per-tool URL endpoints,
  no `/api/v1/<tool-name>` aliases.
- **Cross-references for any future "MCP route moved" finding.**
  The MCP prefix is `"/mcp"` in
  [`backend/src/meho_backplane/mcp/server.py`](../../backend/src/meho_backplane/mcp/server.py)
  and that string has never been edited since the file was
  introduced in v0.2.0 (#266). The mount-time include in
  [`backend/src/meho_backplane/main.py`](../../backend/src/meho_backplane/main.py)
  carries no prefix override. Both anchors are CI-grep-able; before
  re-filing a "route moved" finding, confirm against these files
  directly.

## 14. Intra-MCP `tools/list` shape parity

**Sibling MCP tools share one convention per concept across the 51-tool
surface — no per-tool wire-shape dialect.**

`claude-rdc-hetzner-dc#789` N4 (the v0.8.1 dogfood cycle) catalogued
seven sibling-tool drifts on the MCP `tools/list` surface — the MCP-
side analogue of the REST/MCP sweep §1–§9 close for `/api/v1`. None
were breaking (every drift was an agent-ergonomics papercut), but the
aggregate forced a schema-driven agent to write per-tool plumbing for
shapes that wanted to be one.

G0.18-T5 (#1358) reconciled the seven drifts and pinned the
conventions structurally in
[`backend/tests/test_mcp_tools_list_shape_conventions.py`](../../backend/tests/test_mcp_tools_list_shape_conventions.py)
so a future regression fails CI rather than surfacing as the next
RDC dogfood-cycle finding.

### §14.1 — `op_class` enum: one source per filtering surface

Every MCP tool that filters by audit / broadcast `op_class` declares
the value set as a JSON-Schema `enum` sourced from the canonical
[`OP_CLASS_ENUM`](../../backend/src/meho_backplane/broadcast/history.py)
tuple. The v0.8.0 drift was prose-only on `query_audit.op_class`
(five values, missing `credential_mint`) vs JSON-enum on
`meho.broadcast.recent`/`watch` (six values). Convention: one
import, one tuple, one enum on every surface that filters on it.
Adjacent finding (not in scope here): `tool_call` is a recent
classify_op return value missing from `OP_CLASS_ENUM`; harmonising
the broadcast tuple with the dispatcher classifier output is a
follow-up.

### §14.2 — Forward-cursor parameter named `cursor` everywhere

Every MCP read tool that paginates forward names the cursor
parameter `cursor`. The v0.8.0 wire shape spelled the same concept
three ways: `since` (`meho.broadcast.recent`), `since_cursor`
(`meho.broadcast.watch`), `cursor` (`query_audit` / `query_topology` /
`list_targets`). The reconciled name is `cursor` (the lexical
shortest, present on four of the six tools); `since` and
`since_cursor` survive as **deprecated aliases** marked
`deprecated: true` in the schema. The handler enforces XOR — passing
both names rejects with `-32602`. New callers use `cursor`.

Migration shape mirrors §2's `?envelope=v2` discipline: the
alias-and-deprecate path keeps v0.8.0 callers working through the
v0.9.0 → v0.10.0 cycle; the next sweep drops the aliases.

### §14.3 — `<noun>_id` UUID parameter convention

Every MCP tool that names a resource UUID uses the `<noun>_id`
form: `trigger_id` (`meho.scheduler.cancel`), `agent_session_id`
(`query_audit`), `audit_id` (`query_audit`), `approval_request_id`
(`meho.approvals.*`). The v0.8.0 `meho.approvals.{get,approve,reject}`
used a bare `id` — the only sibling-drift left after the previous
sweeps. Canonical name post-G0.18-T5: `approval_request_id`; bare
`id` survives as a deprecated alias.

### §14.4 — Cross-tenant scope parameter named `tenant_id`

Every MCP tool that exposes a cross-tenant scope parameter names it
`tenant_id`. The v0.8.0 `list_targets` spelled the same concept
`tenant`; the admin tools (`meho.connector.*`, `meho.scheduler.create`)
spelled it `tenant_id`. Reconciled: `tenant_id` everywhere;
`list_targets.tenant` survives as a deprecated alias.

**Accepted-shape asymmetry, documented explicitly:**
`list_targets.tenant_id` accepts a slug OR a UUID, while the admin
tools accept UUID-only. The asymmetry is deliberate —
`list_targets` opens a session to resolve the slug via the
`tenants` table (slug → UUID before scoping the query), while the
admin tools' service layer doesn't expect a session for tenant
resolution (their REST counterparts pre-resolve). Migrating the
admin tools to accept slugs would require widening the service-
layer signature; out of scope for the sweep. The asymmetry is one
wire field with two accepted shapes on one tool, not two wire fields.

### §14.5 — `limit` / `offset` defaults declared in-schema

Every paginating list tool declares `default: <N>` on its `limit`
and `offset` (and on `cursor` when applicable). The v0.8.0 surface
had `default` declared in-schema for some (`meho.approvals.*`,
`query_audit`, `list_targets`, `search_operations`) but prose-only
for others (`meho.scheduler.list`, `query_topology.limit`). A
schema-driven MCP client renders the documented default; prose-
only defaults force the client to guess. Canonical: in-schema.

### §14.6 — `*.list.status` is a JSON enum

Every MCP list tool that filters by status declares the allowed
values as a JSON-Schema `enum`, not in prose. `meho.scheduler.list`
already did; `meho.approvals.list` did not (`status` was a bare
`type: string` with prose-listed values). Canonical: `enum` +
`default` on every list-tool `status` filter.

### §14.7 — Documented name alphabets are JSON-Schema patterns

When a tool's `name` field documents an alphabet (`agent identity
name (letters, digits, hyphen, underscore, dot)`), the JSON-Schema
declares that alphabet via `pattern` so an MCP client schema-
validating ahead of the call sees the constraint before the
service-layer regex fires. `meho.agents.create.name` already
carried the pattern; `meho.agent_principals.register.name` did
not. Canonical: `pattern` declared at the schema layer matches
the service-side regex.

### §14.8 — Every list tool paginates

Every list-shaped MCP tool exposes pagination knobs (`limit` +
`cursor` for keyset, `limit` + `offset` for offset). The v0.8.0
`list_operation_groups` was unpaginated (no `limit`, no `cursor`,
no `next_cursor`) while its sibling `list_targets` was keyset-
paginated. Convention: keyset on the natural sort key (`group_key`
for `list_operation_groups`, `name` for `list_targets`) with the
same `next_cursor` shape on the response.

### §14.9 — Tool names: dotted `meho.<noun>.<verb>` for multi-verb families

Every multi-verb domain family on the MCP surface uses the dotted
`meho.<noun>.<verb>` grammar (`meho.agents.*`, `meho.approvals.*`,
`meho.broadcast.*`, `meho.connector.*`, `meho.scheduler.*`,
`meho.agent_principals.*`). The runbook family was the lone flat
hold-out (`runbook_start`, `runbook_show_template`, …) until #1612
canonicalised the 11 tools as `meho.runbook.<verb>`. The flat names
stay registered as deprecated aliases — same handler object, same
schema, DEPRECATED pointer description, per-call
`mcp_tool_name_deprecated` warning log — for one release (removed in
v0.14.0). The same change unified the template identifier on
`template_slug` across all 11 tools (the template verbs previously
took `slug` while the run verbs took `template_slug`); `slug` is
accepted as a deprecated input alias on the template verbs for the
same window, and template-verb responses mirror the id as
`template_slug` so a value read from `show_template` /
`list_templates` round-trips into `meho.runbook.start` verbatim.
Alias mechanics live in `register_deprecated_mcp_tool_alias`
(`backend/src/meho_backplane/mcp/registry.py`); see also
[`mcp.md`](mcp.md) §Tool naming grammar.

### Code reference

The pack of regression tests in
[`backend/tests/test_mcp_tools_list_shape_conventions.py`](../../backend/tests/test_mcp_tools_list_shape_conventions.py)
exercises every convention above. A future drift on any of them
fails CI rather than surfacing as the next RDC dogfood-cycle
finding.
