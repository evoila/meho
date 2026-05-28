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
1000+ ops). G0.16-T1 covers that. But it's a SEV-3 ("escape hatch
shouldn't OOM") not a SEV-1 ("daily-driver path broken"). The
curated daily-driver works; the escape hatch needs to not crash.

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

## 3. Enum vocabulary discipline

**One identifier per concept across every layer that names it.**

RDC #771 Findings 6 + 7 caught two enum mismatches:

- **Finding 6: product enum.** The TargetCreate enum spelled the
  SDDC Manager product as `"sddc-manager"`; the catalog connector
  (`sddc-rest-9.0`) advertised `product: "sddc"`. An operator
  reading the catalog sees `sddc`, then sees a 422 saying
  `sddc-manager`. Cognitive friction.
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

## 10. Where the conventions live in code

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

Each section's example endpoint is the reference shape. New
endpoints copy from there.

## 11. How this doc gets updated

This file grows in the same shape as
[error-message-shape.md](error-message-shape.md): one section per
codified convention, each section citing the consumer-feedback
finding that surfaced the need. New conventions land as new
sections during the dogfood cycle they emerge from. The
convention itself is what we hold to going forward; the historical
finding is the citation, not the convention.
