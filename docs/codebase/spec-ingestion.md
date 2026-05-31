# `backend/src/meho_backplane/operations/ingest/` — spec ingestion

> Durable map of the spec-ingestion pipeline. Update in lock-step with
> code changes; stale entries are bugs.

## Overview

The spec-ingestion pipeline reads vendor API specifications (OpenAPI
3.0 / 3.1 in v0.2; GraphQL SDL / WSDL / proto deferred) and turns
each operation into a row in `endpoint_descriptor` (G0.6-T1) that the
dispatcher (G0.6-T5) and the agent's `search_operations` meta-tool
can resolve.

The pipeline is broken into work items per Initiative #389:

* **T1 — OpenAPI parser** (`ingest/openapi.py`). Pure-function. Input:
  a path or URL. Output: a list of `EndpointDescriptorProto`. No DB
  session, no LLM call.
* **T2 — `register_ingested_operations()`** (`ingest/register_ingested.py`).
  Bulk-upserts proto rows into `endpoint_descriptor` with
  `source_kind='ingested'`, `is_enabled=False`. Multi-spec merge
  (vCenter's `vcenter.yaml` + `vi-json.yaml` under one connector) via
  per-row `spec:<source>` tag marker. Body-hash skip-re-embed on
  unchanged rows (parallel to the `typed_register` precedent).
  Auto-registers a `GenericRestConnector` shim
  (`ingest/connector_registration.py`) against the v2 connector
  registry on first ingest of a `(product, version, impl_id)` triple;
  the shim raises `NotImplementedError` on `auth_headers` / `execute`
  until a per-G3.x Initiative replaces it with a hand-coded subclass.
* **T3 — LLM-summarised grouping** (`ingest/llm_groups.py` +
  `ingest/_llm_grouping_internals.py` + `ingest/prompts/`). Two-pass
  LLM run: (1) propose 8–15 groups from the full op list, (2) assign
  each op to a group in batches of 50. Proposed groups land
  `review_status='staged'`; each per-op `group_id` is set in the same
  transaction as the audit row. The LLM is injected as the
  :class:`LlmClient` Protocol; tests inject a deterministic stub, and
  FastAPI lifespan startup wires the production Anthropic-backed client
  (#1386) — see [LLM-client wiring](#llm-client-wiring) below for the
  `ANTHROPIC_API_KEY` requirement that gates non-dry-run `--catalog`
  ingest on deployed backplanes.
* **T4 — Review-queue state machine** (`ingest/service.py`). Operators
  move connectors through `staged → enabled` (and `disabled` for
  regression rollback) before any op becomes dispatchable.
* **T5–T7 — CLI / REST / MCP surfaces** that drive the pipeline. T6
  (REST routes) lands the seven `/api/v1/connectors*` endpoints —
  `POST /ingest`, `GET /` (list), `GET /{id}/review`, `PATCH
  /{id}/groups/{key}`, `PATCH /{id}/operations/{op_id:path}`, `POST
  /{id}/enable`, `POST /{id}/disable`. T6 also factors the
  cross-T5/T7-shared service layer
  (`IngestionPipelineService`, `list_ingested_connectors`,
  `api_schemas.*` Pydantic models) into the package so the CLI and
  admin MCP tools consume the same Python surface without hitting
  the network round-trip.
  * **T5 (#405)** — `meho connector ingest/list/review/edit-group/
    edit-op/enable/disable` cobra verb tree at
    `cli/internal/cmd/connector/`. Thin client over T6's REST routes;
    no service-layer access. Operator-facing role: tenant_admin for
    write verbs, operator for `list` / `review`. Enable/disable
    routes return HTTP 204 No Content — the CLI skips JSON decode
    on 204 and prints a success line.
  * **T7 (#407)** — admin MCP tools (`meho.connector.*`) that wrap
    the same canonical service layer — no parallel service class, no
    parallel Pydantic models. The agent's daily tool list stays
    unchanged; the seven admin tools live under the
    `meho.connector.*` namespace and only `tenant_admin` operators
    (plus the two read tools at `operator` role) see them in
    `tools/list`.
* **T8 — vSphere canary** (`tests/acceptance/test_g07_vsphere_canary.py`).
  Acceptance test that drives the full pipeline against the consumer's
  vCenter REST spec (~1,275 ops): parse → register → group → review →
  enable → 10-query govc-parity benchmark over `search_operations`.
  Ships with `vcenter.yaml` only; `vi-json.yaml` parses end-to-end
  after T11 (#501) extended the parser to resolve
  `$ref: "#/components/parameters/*"` (the load-bearing rejection
  in `refs.py` is gone), and the parser smoke test at
  `tests/integration/test_operations_ingest_vi_json.py` proves it.
  Full vi-json ingestion (~2,195 rows persisted + LLM grouping +
  operator review + benchmark expansion) is tracked under #227 G3.1
  T3 — out of scope for the canary itself. Three of 10 queries
  currently `xfail` pending a T3 per-op `llm_instructions`
  enhancement. See the [vSphere canary
  runbook](../cross-repo/g07-vsphere-canary.md) for the operator
  procedure.
* **T9 — Docs** (#409). Two new docs:
  [docs/architecture/spec-ingestion.md](../architecture/spec-ingestion.md)
  (canonical architecture reference) and
  [docs/cross-repo/connector-ingestion.md](../cross-repo/connector-ingestion.md)
  (operator runbook for adding a new vendor surface). Cross-link
  updates to `connectors.md` correction header and this codebase
  doc.

All T1–T8 substrate work merged to `main` before T9 (#409); the
pipeline is shipped and ready for per-G3.x consumer Initiatives
to drive ingestion against their target vendor surfaces.

### Catalog-driven REST ingest (G0.14-T9 / #1150)

`POST /api/v1/connectors/ingest` accepts a second request shape
beyond the explicit-quadruple `(product, version, impl_id, specs[])`:
a body of `{"catalog_entry": "<product>/<version>"}` resolves the
catalog entry server-side (`load_catalog().get(product, version)`)
and routes through the same `_run_ingest_with_http_mapping` path as
if the caller had supplied the resolved quadruple. The two shapes
are mutually exclusive — a `@model_validator(mode="after")` on
`IngestRequest` rejects mixed bodies with `catalog_entry_conflict`
(422) and empty bodies with `ingest_request_underspecified` (422)
per the T11 [error-message-shape](error-message-shape.md) convention.

Why server-side: a REST-native agent runtime (no shell-out to the
CLI) needs an actionable REST surface that mirrors the discoverable
`GET /api/v1/connectors/catalog` shape. Before #1150, only the CLI
could resolve a catalog entry; the REST endpoint required the
already-resolved quadruple. Moving the resolver server-side means
the CLI's `--catalog` flag is now a thin shell that POSTs the
catalog-driven body shape directly — one canonical resolution
path, no client-side catalog cache to drift against the server's
package data.

The four pre-fetch catalog-side validation outcomes
(`catalog_entry_malformed`, `catalog_entry_not_found`,
`catalog_entry_typed_connector`, `catalog_entry_templated_upstream`)
ship through `build_catalog_entry_*_detail` helpers in
`error_envelopes.py` so the REST 422 envelope can't drift from any
future MCP equivalent (same shared-builder pattern G0.9.1-T5 / #777
used for `VersionMismatchError`).

The fifth catalog-side outcome surfaces at fetch time:
`catalog_entry_upstream_not_spec` (G0.15-T2 / #1211). The catalog's
`vmware/9.0` and `sddc-manager/9.0` upstream URLs point at Broadcom
Developer Portal landing pages -- HTML, not raw OpenAPI YAML/JSON --
so the route's `httpx.get` succeeds with a 2xx response whose
`Content-Type` is `text/html`. Before #1211 the bytes fell through to
the YAML decoder and surfaced as `could not decode spec: while
scanning for the next token found character that cannot start any
token in '<file>', line 33, column 1` (HTML doctype at line 1,
opening tags around line 33) -- a true statement about the bytes but
a useless one for the operator. `_load_spec_bytes` in `openapi.py`
now inspects `Content-Type` against an allow-list
(`application/json`, `application/yaml`,
`application/x-yaml`, `text/yaml`, `text/x-yaml`, `text/plain` for
`raw.githubusercontent.com` mirrors) and raises
`UpstreamNotSpecError` -- caught in the route and mapped to HTTP 422
with the `build_catalog_entry_upstream_not_spec_detail` envelope
(catalog reference, upstream URL, Content-Type, remediation: fetch
the spec manually, pass via the explicit-quadruple shape).
Explicit-quadruple requests that hit the same trap get the bare
`build_upstream_not_spec_detail` envelope without the
`catalog_entry` field.

The catalog's `notes` on the two affected entries carry the
"HTML-portal upstream; manual ingest required" warning, mirroring
the `harbor/2.x` Swagger-2.0 precedent. The only other
`spec_info_version: null` catalog entries (`nsx/4.2`, `vault/1.x`,
`k8s/1.x`, `bind9/9.x`) refuse the catalog-driven shape earlier --
NSX via `catalog_entry_templated_upstream` (the URL is FQDN-templated),
the three typed connectors via `catalog_entry_typed_connector`
(`upstream: null`) -- so they never reach the fetch path that
`UpstreamNotSpecError` guards.

T1 produces the proto shape every other stage consumes; T2 is the
single write path into `endpoint_descriptor` for ingested rows; T3
groups them; T4 gates dispatchability behind operator review; T6
exposes the whole thing over HTTP with tenant_admin / operator RBAC.

### T3 (LLM grouping) at a glance

`run_llm_grouping()` opens its own transaction and runs:

1. **Pass 1 (group derivation)** — only when no `operation_group`
   rows yet exist for the connector triple. Sends every unassigned
   op's `(op_id, summary, tags)` to the LLM and asks for an array of
   `{group_key, name, when_to_use}` proposals. Output is validated
   against `GroupProposal` (snake_case key, non-empty fields, bounded
   lengths, no duplicate keys) and persisted as
   `OperationGroup` rows in `review_status='staged'`.
2. **Pass 2 (per-op assignment)** — splits the unassigned-op set
   into batches of `batch_size` (default 50). Each batch asks the LLM
   for a JSON object mapping `op_id` to `group_key`, where
   `group_key` is either one of the Pass-1 keys or the sentinel
   `"none"`. Each row's `EndpointDescriptor.group_id` is set to the
   matching group's UUID; sentinel and unknown-key entries leave
   `group_id=NULL`.

The function commits exactly once at the end of both passes, in the
same transaction as a single `meho.connector.llm_grouping` audit row
that records `{connector_id, groups_created, operations_assigned,
operations_unassigned, llm_call_count, batch_size}`. Partial failure
(LLM output that fails schema validation) raises
`LlmOutputInvalid`, rolls back the transaction, and leaves the
connector in whatever state preceded the call — operator retries via
the CLI verb T5 will ship.

Idempotency:

* No unassigned ops → true no-op, zero LLM calls, no audit row.
* Existing `operation_group` rows present but some ops still
  unassigned → Pass 1 skipped, Pass 2 only runs against the
  unassigned-op subset using the existing groups verbatim. This is
  the "partial-regrouping" branch.
* Fully fresh connector → both passes run, all groups + assignments
  persist in one transaction.

The LLM client is injected as a `LlmClient` `Protocol` (one async
method, `generate_json`). Tests pass a deterministic stub from
`tests/fixtures/llm_groups/{small,medium}_corpus.py`. The chassis
adapter (Anthropic Messages API) ships with T3 itself (#404, commit
864ef68f); the model id + retry policy live in `Settings`. T5
(#405) is purely the operator-facing CLI verb tree
(`cli/internal/cmd/connector/`) that drives the ingest → review →
enable workflow over the T6 REST routes.

### T7 (admin MCP tools) at a glance

`backend/src/meho_backplane/mcp/tools/connector_admin.py` registers
seven MCP tools at module import:

| Tool | Required role | Wraps |
|------|---------------|-------|
| `meho.connector.ingest` | `tenant_admin` | `IngestionPipelineService.ingest()` |
| `meho.connector.list` | `operator` | `list_ingested_connectors()` |
| `meho.connector.review` | `operator` | `ReviewService.get_review_payload()` |
| `meho.connector.edit_group` | `tenant_admin` | `ReviewService.edit_group()` |
| `meho.connector.edit_op` | `tenant_admin` | `ReviewService.edit_op()` |
| `meho.connector.enable` | `tenant_admin` | `ReviewService.enable_connector()` |
| `meho.connector.disable` | `tenant_admin` | `ReviewService.disable_connector()` |

These are administrative tools per CLAUDE.md's "What MEHO is NOT"
note — distinct from the agent-surface meta-tools (`search_connectors`,
`call_operation`, etc.). The registry's
`all_tools_for(operator)` filter hides them from `tools/list` for
operators whose role doesn't meet the `required_role` rank, and the
`handle_tools_call` dispatcher re-checks the rank at invocation time
so a client that guesses a hidden name is still rejected.

Each tool's handler is a thin shim that wraps the canonical service
layer the REST routes (T6) and CLI verbs (T5) also consume:
`IngestionPipelineService` for `ingest`, `list_ingested_connectors`
for `list`, and `ReviewService` for the five review / edit / enable /
disable verbs. There is **no parallel admin service class**; the
handler converts the JSON-Schema-validated `arguments` dict into the
canonical `IngestRequest` Pydantic model (from `api_schemas`) and
calls the service directly. Responses are `model_dump(mode="json")`-
ed onto the wire.

PATCH handlers (`edit_group`, `edit_op`) preserve PATCH-semantic
intent: the handler builds the kwargs dict from `if "field" in
arguments` key-presence checks so omitted fields never reach
`ReviewService` (an explicit `null` would otherwise be
indistinguishable from an omission with `arguments.get(...)`). Only
fields the operator explicitly named are forwarded.

The `ingest` handler additionally maps `VersionMismatchError` and
`UncoveredVersionLabel` to JSON-RPC `-32602 Invalid Params` with
the structured detail on `error.data` (G0.9.1-T5 #777). Both
exceptions describe caller-input mistakes — the operator's `version`
label disagrees with the supplied spec, or falls outside every
registered class's advertised range — so `-32602` is the right code
(not `-32603 Internal Error`, which the pre-fix generic catch-all
emitted). The structured `data` payload is built by the shared
helpers in `operations/ingest/error_envelopes.py` so the REST 422
detail and the MCP `error.data` member share one source of truth.

## Key types

### `EndpointDescriptorProto` (`ingest/schemas.py`)

Frozen Pydantic v2 model. One per operation. Maps 1:1 to a subset of
`EndpointDescriptor` columns (the parser-populated subset):

| Proto field | ORM column | Notes |
|---|---|---|
| `op_id` | `op_id` | `f"{METHOD}:{path}"`; the connector-side natural key |
| `method` | `method` | Upper-case HTTP verb |
| `path` | `path` | URL template, `{var}` placeholders |
| `summary` | `summary` | Verbatim from spec |
| `description` | `description` | Verbatim from spec |
| `tags` | `tags` | Spec tags + optional `spec:<source>` marker |
| `parameter_schema` | `parameter_schema` | Flattened JSON Schema 2020-12 with `x-meho-param-loc` |
| `response_schema` | `response_schema` | Success-response schema or `None` |
| `safety_level` | `safety_level` | HTTP-verb heuristic, operator-overridable at review |
| `requires_approval` | `requires_approval` | Always `False` at parse time |

T2 owns the rest of the ORM columns: `tenant_id`, `source_kind`
(always `'ingested'`), `product`, `version`, `impl_id`, `embedding`,
`is_enabled` (always `False` on ingest), `handler_ref` (always
`None` on ingested rows — the dispatcher uses `method`+`path`). T3
owns `group_id` (NULL until grouping runs). T4 owns
`custom_description`, `custom_notes`, `llm_instructions` (operator-
authored overrides at review time).

### `GroupProposal` / `GroupingResult` / `GroupingConfig` (`ingest/llm_groups.py`)

Pydantic `frozen=True` model + two frozen-slotted dataclasses, one
per role in the T3 grouping run:

* `GroupProposal` — the per-group dict the LLM emits in Pass 1
  (`group_key`, `name`, `when_to_use`). Snake-case key enforced via
  validator; oversized prose rejected via bounded `max_length`.
* `GroupingResult` — counts + timings the orchestrator returns
  (`groups_created`, `operations_assigned`, `operations_unassigned`,
  `llm_call_count`, `llm_duration_ms`). Surfaced in the operator-
  facing CLI / API at T5.
* `GroupingConfig` — tunable knobs (`batch_size`, `min_groups`,
  `max_groups`). Constructed from the keyword arguments to
  `run_llm_grouping()`; `validate()` raises `ValueError` before any
  LLM or DB I/O.

`LlmOutputInvalid` (in `exceptions.py`) raises when either pass
returns malformed JSON or output that fails schema validation. It
carries `pass_name` (`"propose_groups"` / `"assign_ops"`),
`raw_output` (the verbatim model response, capped in the message
preview), and `parse_error` (the underlying `ValidationError` or
`JSONDecodeError`).

### `IngestionResult` (`ingest/register_ingested.py`)

Frozen dataclass returned from `register_ingested_operations()`.
Carries per-call counts (`inserted_count`, `updated_count`,
`skipped_count`) plus two flags (`connector_registered`,
`operations_grouped`) the CLI / API caller surfaces in operator
output. `operations_grouped` is always `False` in v0.2 — T3
flips it after the LLM-grouping pass runs.

### `GenericRestConnector` (`ingest/connector_registration.py`)

Auto-generated `HttpConnector` subclass synthesised on first ingest
of a `(product, version, impl_id)` triple. Concrete `auth_headers`
raises `NotImplementedError` with operator-readable guidance; the
review-queue gate (T4) keeps every ingested op `is_enabled=False`
until the operator replaces the shim with a hand-rolled per-G3.x
subclass that adds the auth path. The shim makes the connector
resolvable through the v2 registry so spec ingestion can proceed
before the per-product Initiative work lands.

### `check_version_covered_by_registered_class()` (`ingest/connector_registration.py`)

G0.9-T9 (#741) pre-flight that the operator's `version` label is
dispatchable against at least one already-registered class for
`(product, impl_id)`. Mirrors the
`resolver.resolve_connector` PEP 440 `SpecifierSet` check at ingest
time so orphan-at-ingest is caught at the operator's call site,
not at the first `call_operation` against the resulting orphan
rows. Two branches:

* **At least one class registered** for `(product, impl_id)` but
  none accepts the label → raise `UncoveredVersionLabel` (mapped
  to HTTP 422 in the REST router). The exception names every
  candidate class and its advertised range so the operator-facing
  detail tells them exactly what to fix.
* **No class registered** for `(product, impl_id)` → log
  `connector_ingest_orphaned_class` at info level and proceed.
  This is the v0.4-staging path where ops land before the class
  exists; the dispatcher will surface the gap at the first
  `call_operation` and the ingest-time warning is the upstream
  signal.

Called from `register_ingested_operations` (real path) and from
`IngestionPipelineService._run_dry_run` (dry-run path) so an
operator's `dry_run=True` validation sees the same 422 the real
path would.

### `IngestionPipelineService` (`ingest/pipeline.py`)

End-to-end orchestrator that bundles the parse → register_ingested →
run_llm_grouping pipeline for one connector. Constructed from an
`Operator` (so the service-level audit rows the helpers write carry
the originating operator's identity); the same instance is reused
across T5's CLI verbs, T6's REST routes, and T7's admin MCP tools.

The `LlmClient` Protocol is injected via a factory parameter so the
chassis can lazy-resolve it; the fail-closed default factory raises
`LlmClientUnavailable` and the REST layer maps it onto HTTP 503.
FastAPI lifespan startup installs the production factory
(`build_anthropic_ingest_llm_client`) via `set_llm_client_factory` (in
`api/v1/connectors_ingest.py`), reusing `settings.anthropic_api_key`
(#1386) — so a deploy with the key set groups for real, and a keyless
deploy keeps the 503. See
[LLM-client wiring](#llm-client-wiring)
for the operator-facing framing and the resolver-routing follow-up.

The `embedding_service` parameter is the test seam to inject
`AsyncMock` so unit tests don't pull the fastembed ONNX model from
huggingface.co.

`ingest(..., dry_run=True)` short-circuits both the DB writes and
the LLM call: parses every spec and returns the parser's
`inserted_count` projection with `grouping=None`. Operators use
this path to validate a spec before committing.

Multi-spec merge: a single `ingest()` call processes a list of
`SpecSource` entries; each is parsed and upserted under the same
connector triple with the spec's URI as the `spec_source` tag, so
operators can distinguish "this op came from vcenter.yaml" vs "this
op came from vi-json.yaml" during review.

**Spec-vs-label cross-check (G0.9-T8).** Before parse/register/group
runs, `_validate_spec_versions` reads each spec's `info.version` via
the lightweight `read_spec_info_version` helper and compares it
against the operator-supplied `IngestRequest.version` label using the
same `packaging.version.Version` semantics the resolver uses at
dispatch time. Three outcomes:

* **Exact** (`spec=9.0.3`, `label=9.0.3` / `9.0` / `9`) — proceed.
* **Compatible** (same major, different minor, e.g. `spec=9.0.3`,
  `label=9.1`) — proceed and emit a structured
  `connector_ingest_version_drift` log event naming both values.
* **Incompatible** (different major) — raise `VersionMismatchError`,
  mapped to HTTP 422 with a structured detail naming both
  `spec_info_versions` and `requested_version` so the operator-
  facing error message tells them exactly what to fix.

Multi-spec ingests (vcenter.yaml + vi-json.yaml) are additionally
cross-checked for internal consistency: two specs disagreeing on
the major version surface as `VersionMismatchError` with
`kind="multi_spec_inconsistent"`. Specs missing `info.version`
entirely skip the check (older spec dialects keep ingesting).

**Catalog-driven opt-in: `spec_info_versions_compatible` (G0.16-T5
#1307).** Some catalog rows carry a `version` label that is
semantically distinct from the spec's `info.version` (the GitHub
REST catalog row's `version="3"` is the product-line label
github.com calls the API; the live OpenAPI spec's `info.version`
is `1.1.4`, regenerated daily on `rest-api-description/main`). For
these rows the catalog declares a compatibility range
(`spec_info_versions_compatible: ["1.x.x"]`); the catalog-entry
resolver in `api/v1/connectors_ingest.py` passes it through to
`IngestionPipelineService.ingest(spec_info_versions_compatible=...)`,
which forwards it to `_validate_spec_versions`. Per-spec
classification then bypasses the verbatim/major-band check for any
spec whose `info.version` matches a pattern in the range, emitting
`connector_ingest_version_label_decoupled` so the audit trail still
records the decision. The explicit-quadruple shape doesn't carry
the catalog row and therefore can't opt in — operators using it
implicitly accept the historical strict check. See
[`docs/cross-repo/connector-catalog.md`](../cross-repo/connector-catalog.md#label-vs-spec-decoupling-spec_info_versions_compatible)
for the field definition and pattern syntax.

### Shared error-envelope builders (`ingest/error_envelopes.py`)

The REST route at `POST /api/v1/connectors/ingest` and the MCP
`meho.connector.ingest` tool both need to surface
`VersionMismatchError` and `UncoveredVersionLabel` as caller-input
validation errors carrying structured diagnostic detail (expected-
vs-received versions, the list of advertised
`supported_version_range` strings) so the operator — or the agent
acting on the operator's behalf — can self-correct without re-
prompting.

* `build_version_mismatch_detail(exc)` — REST embeds the returned
  dict in the `HTTPException(status_code=422).detail` field; MCP
  embeds it in the JSON-RPC `error.data` member (spec §5.1).
* `build_uncovered_version_label_detail(exc)` — MCP-only for now;
  REST emits `str(exc)` for backward compatibility but can switch
  to the structured builder later without changing the wire shape
  in a non-additive way.

Pre-G0.9.1-T5 (#777) the MCP path had no typed handling for either
exception — both fell through to the dispatcher's generic
`except Exception` arm in `meho_backplane.mcp.server`, which
surfaced `-32603 "internal error: VersionMismatchError"` and
discarded the (already-detailed) exception message. The shared
builders sit in `operations/ingest/error_envelopes.py` so the REST
422 body and the MCP `-32602` `data` member can't drift again.

### `list_ingested_connectors()` (`ingest/list_connectors.py`)

Aggregate query for `GET /api/v1/connectors`. Returns one
`ConnectorListItem` per connector visible to the operator's tenant
(operator's-tenant rows + built-ins, i.e. `tenant_id IS NULL`). The
optional `status` filter narrows by aggregated review status:
`staged` (≥1 staged group), `enabled` / `disabled` (every group
uniform), or `all` (no filter). The implementation uses portable
`CASE WHEN ... THEN 1 ELSE 0 END SUM` expressions rather than PG-
only `FILTER` clauses so the same query runs against SQLite in
tests.

The op-count rollup counts every `source_kind` (`ingested`,
`typed`, `composite`) — the visibility driver is the paired groups
query, which has never filtered on `source_kind`. The earlier
filter that excluded typed/composite rows from the count (G0.7-era
artefact) was the cause of Signal #4 in the 2026-05-20 RDC dogfood:
typed connectors surfaced with `group_count > 0` but
`operation_count: 0`, the asymmetry between the two paired queries.
The renamer "list_*ingested*_connectors" is now misleading and is a
follow-up cleanup; the function lists every connector with at least
one visible :class:`OperationGroup` row.

Class-side registrations from the v2 connector registry that have
no DB-side state yet (T5 #733 — "State 0.5" connectors registered
via `register_connector_v2` but without any rows in
`operation_group` / `endpoint_descriptor`) are unioned into the
response with `group_count: 0, operation_count: 0` and
`state: "registered"` so operators see `connector registered ⇒
visible in list` but the agent knows the dispatcher won't resolve
calls against them yet. Class-only rows are always built-in
(`tenant_id IS NULL`); under an explicit `status` narrowing they're
filtered out (no groups ⇒ nothing to review). v1-compat shim
entries (`(product, "", "")` rows the v1 `register_connector`
writes into the v2 table) are excluded — they double-list every v1
connector and aren't separately registered.

#### Listing-integrity contract (G0.9.1-T1 / #773)

Every `connector_id` the function emits is guaranteed to round-trip
through the dispatcher's resolve path: for every row with `state:
"ingested"`,
`connector_exists(*parse_connector_id(connector_id))` returns
`True`; for rows with `state: "registered"`, `connector_exists`
returns `False` honestly (no descriptor rows yet) and the agent
reads `state` to know it cannot dispatch. Rows whose emitted
`connector_id` would not round-trip at all are dropped before the
response is built and a structured
`dropped_unresolvable_connector_id` log line is emitted per drop —
two shapes:

* **Stale-rename DB rows.** `endpoint_descriptor` / `operation_group`
  rows survived an `impl_id` rename (G3.2 #320's
  `kubernetes-asyncio → k8s` rename) but no migration cleaned them
  up. `build_connector_id` emits e.g. `"kubernetes-asyncio-1.x"`,
  `parse_connector_id` derives `product="kubernetes"`, and
  `connector_exists` cannot find the row because the rows now live
  under `product="k8s"`. The listing drops the stale row and the
  log line names both the row's natural-key triple and the parsed
  triple so the operator can clean up with a single SQL `DELETE`.
* **v2-registry product disagreement.** A class-side-only entry
  registered with `product != impl_id.split("-")[0]` whose
  `connector_id` parses to a different `(version, impl_id)` than
  the registry advertises is dropped (impossible to recover from
  inside the listing). The SDDC case — registry
  `product="sddc-manager"`, parser derives `product="sddc"` — is
  *not* dropped because `(version, impl_id)` survives the round-
  trip and `SDDC_PRODUCT="sddc"` already writes DB rows the
  dispatcher reaches via the parsed product; the listing emits
  `product="sddc"` (parser-derived) so the wire shape is
  consistent with what the dispatcher will derive.

Regression test:
`tests/test_api_v1_connectors_ingest.py::test_list_every_connector_id_round_trips_through_dispatcher`
asserts the contract over a seeded DB that includes a stale-rename
row and a class-side-only opless connector.

#### `next_step` workflow-completion hint (G0.13-T3 / #1133, G0.18-T8 / #1361)

`state="registered"` rows carry a `next_step: NextStep` object that
points at the verb that closes the workflow gap surfaced by the
v0.6.0 RDC dogfood (signal 11: half-registered connectors fail
lookup with no in-product hint about what verb closes the workflow).
`state="ingested"` rows set `next_step` to `null` because the
dispatcher already resolves operations against them — there is no
operator action remaining.

The hint comes from `_next_step_for_registered` in `list_connectors.py`.
It consults the connector-spec catalog (`ingest/catalog.py`, #743) and
branches on the catalog entry's declarative
`catalog_ingest: "supported" | "spec-only"` field (default
`"supported"`; the VCF-family rows opt into `"spec-only"` —
G0.18-T8 / #1361, RDC #789 N8). Three branches:

* **Catalog hit, `catalog_ingest="supported"`** — verb points at
  `meho connector ingest --catalog <product>/<version>`. Rationale
  says the spec is available in the catalog. The CLI's
  `meho connector ingest --catalog ...` form (G0.7-T5 / #405) drives
  the rest of the workflow.
* **Catalog hit, `catalog_ingest="spec-only"`** — verb points at the
  explicit-quadruple manual-mode form `meho connector ingest
  --product <p> --version <v> --impl <i> --spec <concrete-openapi-uri>`
  using the catalog's native `(product, version, impl_id)` triple.
  Rationale calls out that the catalog row exists but its upstream
  is HTML-portal or fqdn-templated, so a `--catalog` POST would
  422 on the route's `catalog_entry_upstream_not_spec` /
  `catalog_entry_templated_upstream` branches — the operator must
  fetch the raw OpenAPI spec from the appliance themselves. The
  three VCF-family rows (`vmware/9.0`, `sddc-manager/9.0`, `nsx/4.2`)
  ride this branch; the previous "spec available in catalog; run
  ingest" hint over-promised for all three. The triple matches what
  the operator would have used after a successful `--catalog`
  resolve, so the verb still copies-and-runs once the operator
  sources the spec URI.
* **Catalog miss** — verb points at `meho connector ingest --product
  <p> --version <v> --impl <i> --spec <upstream-openapi-uri>`.
  Rationale calls out the missing catalog entry so the operator
  knows they need to source the OpenAPI spec themselves. Manual
  mode is the same path G0.7-T5 already supports for one-off /
  not-yet-curated specs (see `ingest.go`'s mode dispatch).

The catalog lookup uses the **registry's** `(product, version)`, not
the parser-derived shortening. The SDDC case is canonical: the
catalog stores `product="sddc-manager"`, the listing emits
`product="sddc"`, but the hint says `--catalog sddc-manager/9.0`
because that is what `meho connector ingest --catalog ...` resolves
against. Looking up the parsed product would always miss for SDDC.

If `load_catalog()` raises `CatalogError` at listing time (only
possible mid-test-monkeypatch or mid-reload — startup parse failures
crash the lifespan), the helper degrades to the manual-mode
rationale rather than 500ing the route. A `next_step_catalog_load_failed`
log line is emitted so the observability trail flags the degraded
path.

Regression tests:
`tests/test_api_v1_connectors_ingest.py::test_list_registered_row_carries_catalog_next_step_hint`
(catalog-hit / `supported` branch incl. SDDC's registry-vs-parsed
asymmetry),
`::test_list_registered_row_spec_only_catalog_entry_points_at_spec`
(catalog-hit / `spec-only` branch — pins the explicit-quadruple
`--spec` verb + the upstream-shape rationale for VCF-family rows;
G0.18-T8 / #1361),
`::test_list_registered_row_without_catalog_entry_points_at_manual_mode`
(catalog-miss branch), and
`::test_list_ingested_row_omits_next_step_hint`
(ingested-row contract: field present, value `null`).

### API request / response models (`ingest/api_schemas.py`)

The shared Pydantic-v2 surface T5 (CLI), T6 (REST), and T7 (MCP) all
consume so the wire contract is defined once:

* `IngestRequest` / `IngestResponse` — body for `POST /ingest` and
  its return shape. `IngestResponse.grouping` is `None` for the dry-
  run path. `SpecSource` wraps one spec URI with room for future
  per-spec knobs (auth headers, dialect pinning).
* `ConnectorListItem` / `ConnectorListResponse` — one row per
  visible connector + the wrapper for the list endpoint. The wrapper
  keeps the JSON shape stable when future paging / cursor fields
  land. `ConnectorListItem.state` (G0.9.1-T1 / #773) is `"ingested"`
  for DB-backed rows the dispatcher can resolve and `"registered"`
  for class-side-only rows the dispatcher cannot resolve yet — see
  the listing-integrity contract section above.
  `ConnectorListItem.next_step` (G0.13-T3 / #1133) is the workflow-
  completion hint: a `NextStep` object (verb + rationale) on
  `state="registered"` rows, `null` on `state="ingested"` rows — see
  the next-step hint section above.
* `NextStep` (G0.13-T3 / #1133) — `{verb, rationale}` pair surfaced
  on `state="registered"` rows. `verb` is a copy/pasteable
  `meho connector ingest ...` invocation; `rationale` is one
  sentence explaining why that verb is the right next step
  (catalog-hit vs catalog-miss).
* `EditGroupBody` / `EditOpBody` — PATCH bodies for the per-group
  and per-op edit verbs. Pydantic enforces the bounded enum for
  `safety_level` and the empty-body rejection lands as a service-
  layer `ValueError` mapped to 400.
* `IngestionResultModel` / `GroupingResultModel` — Pydantic
  projections of the underlying frozen dataclasses, with an
  added `connector_id` echo for round-trip clarity.

### REST routes (`api/v1/connectors_ingest.py`)

The seven `/api/v1/connectors*` routes that wire the service layer
to the operator-facing HTTP surface. RBAC: read paths (GET /, GET
/{id}/review) require `operator` role minimum; write paths
(`POST /ingest`, `PATCH /groups`, `PATCH /operations`, `POST
/enable`, `POST /disable`) require `tenant_admin`. Tenant scoping
derives from the JWT — there is no body / query parameter that can
override the operator's tenant.

Both read paths apply the same "operator's-tenant rows + built-ins
(`tenant_id IS NULL`)" scope: the listing query does it via a
single `WHERE tenant_id IS NULL OR tenant_id = X` clause, and
`ReviewService.get_review_payload` mirrors it through a two-pass
lookup (own-tenant probe first, then built-in fallback when the
caller's tenant_id matches `operator.tenant_id`). G0.13-T5 (#1135)
landed the review-route fallback after the v0.6.0 RDC dogfood
flagged that every global connector in the catalog returned 404
on review even though the listing surfaced them. Cross-tenant
probes (`tenant_id` ≠ operator's own) still surface as 404
`ConnectorNotFoundError` — same conflation `ReviewService` uses
to keep the operator-facing failure surface uniform and stop
status-code differential from enumerating other tenants.

The PATCH editing routes (`/groups`, `/operations`) deliberately
keep their single-pass lookup against the operator's `tenant_id`:
"do tenant_admins get to edit built-ins?" is a policy choice
distinct from the read-visibility bug, and the route gate is
`tenant_admin`-only — built-in writes already have an explicit
MCP / CLI affordance (`ReviewService` accepts `tenant_id=None`
under the `TENANT_ADMIN` role).

The `op_id` path segment uses the `:path` converter so operations
whose natural key contains slashes (`"GET:/api/vcenter/cluster"`)
round-trip through URL routing intact. The route module's
`set_llm_client_factory(factory)` helper is the wire-up seam the
FastAPI lifespan startup calls to install the production
:class:`LlmClient` adapter (`build_anthropic_ingest_llm_client`,
#1386); tests call it too with a deterministic stub. The route reads
the active factory via the `get_llm_client_factory` dependency — see
[LLM-client wiring](#llm-client-wiring)
for the operator-facing framing.

### `parse_openapi(spec_path_or_uri, *, spec_source=None)` (`ingest/openapi.py`)

The only public entry point for the full walk. Resolves the input
(file path or `http(s)://` URL via `httpx`), sniffs YAML vs JSON via
`detect_spec_format`, decodes, validates the OpenAPI version
(3.0.x / 3.1.x), and walks `paths`. Returns a list.

The function is synchronous because callers are CLI / one-shot
ingestion endpoints that have no in-flight event loop concern. It
also keeps the surface trivially testable.

`read_spec_info_version(spec_path_or_uri)` is the companion helper
the G0.9-T8 cross-check uses. It runs the same load / decode /
version-gate steps but returns the spec's `info.version` string
(or `None` when absent) without walking `paths` — so the
pipeline can fail the spec-vs-label check in milliseconds rather
than after spending CPU on a 2,000-op spec walk.

## Control flow

```text
parse_openapi
├─ _load_spec_bytes        # file:// or http(s)://; httpx with a 30s timeout
├─ _decode_spec            # CSafeLoader-preferred YAML, stdlib JSON
├─ _validate_openapi_version
└─ _iter_operations
   └─ _build_proto         # per (method, path) verb under paths
      ├─ _build_parameter_schema
      │  ├─ _resolve_shallow_ref      # $ref → #/components/schemas/X
      │  ├─ _build_param_property     # one property per path/query/header
      │  └─ _build_body_property      # requestBody under "body" key
      └─ _extract_response_schema     # picks 200 > 201 > 202 > ... > 2XX
```

`_resolve_shallow_ref` is the load-bearing helper. It inlines exactly
one level of `$ref` into the parameter / response / body schema and
preserves any nested `$ref` strings verbatim. The intent is that the
parameter_schema is self-contained enough for the dispatcher's
JSON-Schema validator to validate the immediate parameter shape;
deeper schema dereferencing (chasing nested `$ref`s) is the
dispatcher's concern (G0.6-T5 + T2's tracking of `components.schemas`).

The four supported component buckets are: `#/components/schemas/*`,
`#/components/parameters/*` (vi-json.yaml's shared `moId`),
`#/components/responses/*` (the GitHub REST spec's 1.9k shared
response envelopes — `accepted`, `not_found`, `validation_failed`
etc), and `#/components/requestBodies/*` (parity bucket for future
vendor specs; not yet used in the v0.x catalogue). Each opts in via
a separate kwarg on `resolve_shallow_ref`; `parse_openapi` threads
all four dicts uniformly so the full pipeline never trips the
opt-out branch.

### T2 control flow

```text
register_ingested_operations
├─ _detect_op_id_collisions    # set scan; raise OpIdCollision (within-batch) before DB writes
├─ check_version_covered_by_registered_class    # G0.9-T9 (#741) pre-flight
│  ├─ all_connectors_v2()       # snapshot v2 registry
│  ├─ filter by (product, impl_id) # version label is the thing being checked
│  ├─ for each class: Version(label) in SpecifierSet(supported_version_range)?
│  ├─ no class for (product, impl_id) → log connector_ingest_orphaned_class, proceed
│  ├─ class(es) exist but none accepts label → raise UncoveredVersionLabel (→ HTTP 422)
│  └─ at least one accepts → return
├─ ensure_connector_class_registered
│  ├─ all_connectors_v2()       # check v2 registry for (product, version, impl_id)
│  ├─ type(cls_name, ...)       # synthesise GenericRestConnector subclass
│  └─ register_connector_v2()   # G0.6-T2 entry point
└─ _register_in_session         # caller-owned or helper-owned session
   └─ _upsert_one_operation     # per proto
      ├─ build_embedding_text   # canonical text per typed-register parity
      ├─ compute_embedding_text_hash
      ├─ natural-key lookup     # (product, version, impl_id, op_id)
      │                         # + partial tenant_id index match
      ├─ cross-call collision   # existing row's spec:<src> tag != ctx.spec_source
      │                         # → raise OpIdCollision (cross-call branch)
      ├─ skip-re-embed path     # hash matches persisted row
      ├─ re-embed path          # row exists, embedding text changed
      └─ first-register path    # brand-new row, embedding computed
```

The pre-flight runs **before** `ensure_connector_class_registered`
on purpose: the auto-shim's `supported_version_range` is derived
from the operator's own `version` label (via
`derive_supported_version_range`), so a post-shim check would
always pass vacuously. The dry-run path in `IngestionPipelineService`
calls the same helper so an operator validating a spec sees the
422 they would see on the real path.

The skip-re-embed path is the operationally critical branch on spec
re-ingest: an unchanged 3,000-op vCenter spec must not re-embed
3,000 operations. Hash comparison runs against the persisted row's
recomposed text (via `build_embedding_text`), so no `body_hash`
column is needed in v0.2 — the cost is one recompose-and-hash per
op, well under the ONNX inference budget.

`OpIdCollision` fires from two distinct sites: the up-front within-
batch set scan (two ops in one call share `op_id`) and the per-row
cross-call check (this call's `spec_source` differs from the
persisted row's `spec:<src>` tag for the same natural key). Both
sites use the same exception type so callers can write one
`except OpIdCollision`; the cross-call site fills
`existing_spec_source` and `incoming_spec_source` so the operator-
facing message names both colliding specs. Same-`spec_source`
re-ingest of an unchanged spec stays on the skip-re-embed path —
the cross-call check only fires on a true `spec_source` mismatch.

### T3 control flow

```text
run_llm_grouping
├─ GroupingConfig.validate     # bounds check on min/max/batch_size
├─ load_unassigned_ops         # group_id IS NULL + scope match
│  └─ early return GroupingResult(...zeros...) if none
├─ _resolve_groups_for_pass2
│  ├─ load_existing_groups
│  ├─ if existing → project rows into GroupProposal list (skip Pass 1)
│  └─ else
│     ├─ render_propose_groups_prompt
│     ├─ llm_client.generate_json   # Pass 1 LLM call
│     ├─ parse_proposal_response   # GroupProposal schema validation
│     └─ _persist_proposed_groups  # session.add per row + flush
├─ _assign_ops_in_batches
│  └─ for each batch of `batch_size` ops:
│     ├─ render_assign_ops_prompt
│     ├─ llm_client.generate_json   # Pass 2 LLM call
│     └─ parse_assignment_response  # filter unknown ops + coerce unknown keys
├─ _apply_assignments_to_rows   # mutate EndpointDescriptor.group_id
├─ _write_grouping_audit_row    # meho.connector.llm_grouping
└─ session.commit               # atomic: groups + assignments + audit
```

The two passes use distinct system prompts (`PROPOSE_GROUPS_SYSTEM_PROMPT`
/ `ASSIGN_OPS_SYSTEM_PROMPT`) so each pass's cacheable prefix on the
Anthropic Messages API stays stable across batches; per-call dynamism
lives in the user-prompt body rendered from the Jinja templates.

## Dependencies

* **PyYAML 6.0+** — already a transitive dep; we exercise
  `yaml.load(..., Loader=CSafeLoader | SafeLoader)`. The C loader is
  ~5-10× faster on multi-MB specs; the pure-Python fallback works
  identically on platforms without LibYAML.
* **httpx 0.27+** — fetch for `http(s)://` URLs. The chassis already
  depends on httpx for Keycloak JWKS + connector adapters.
* **Pydantic v2** — `EndpointDescriptorProto` uses `ConfigDict(frozen=True)`.
* **Jinja2 3.1+** — renders the T3 prompt templates from `ingest/prompts/`.
  Used with `StrictUndefined` so any missing template variable raises
  immediately, and `autoescape` disabled because the rendered output
  goes to an LLM (not HTML).

No spec-side validation library (e.g. `openapi-spec-validator`) is
pulled in. The parser tolerates partial / underspecified docs and
relies on T4's review queue to surface ambiguities to a human before
operations go live.

## Async ingest mode (G0.16-T1 / #1303)

`POST /api/v1/connectors/ingest` defaults to `async=true`: the route
fires the pipeline off the request thread via `asyncio.create_task`
and returns `202 Accepted` + a job handle:

```json
{
  "job_id": "0c4b7e8f-...",
  "status": "running",
  "poll_url": "/api/v1/connectors/ingest/jobs/0c4b7e8f-..."
}
```

Operators poll the handle for completion:

```text
GET /api/v1/connectors/ingest/jobs/{job_id}
→ 200 + IngestJobStatusResponse
```

The polling response carries the originating request descriptors,
lifecycle timestamps, and -- on completion -- one of:

* `status="succeeded"` + populated `ingestion` (+ optional `grouping`)
* `status="failed"` + `error_class` + capped `error` message

`status="running"` leaves both clusters `None`. Clients branch on
`status` rather than checking presence.

**Why async by default.** The OpenAPI ingest path is the escape
hatch per [api-shape-conventions.md §1](api-shape-conventions.md) --
operators reach for it when the curated daily-driver doesn't cover
what they need and they're willing to handle vendor-shape responses.
Real-world vendor specs are large: `vmware/9.0.0.0` is 7.55 MB / 1275
typed REST ops, and a synchronous ingest call blocks the event loop
for ~30 s in the register + LLM-grouping phases -- past the kubelet
liveness probe deadline (default 25 s). RDC #771 Finding 20 caught
the pod restart in production; G0.16-T1 (#1303) replaced the
synchronous default with the 202 + job-handle shape so an operator
reaching for the escape hatch doesn't kill the pod.

**`dry_run=true` stays synchronous.** The parse-only leg is the
fast path (~30 s walltime for the same vmware spec, but with no DB
or LLM hops and steady event-loop yields between operations). It
returns the legacy `IngestResponse` at 200 with `grouping=None`.

**`async=false` keeps the legacy blocking shape.** Small-spec
callers (CI tests with ≤ 100-op fixtures, ad-hoc shell scripts, the
v0.8.x clients that pre-date the async shape) opt into the
synchronous path by setting `async=false` in the request body. The
domain-error → HTTP-status mapping documented at the route
(`UpstreamNotSpecError` → 422, `VersionMismatchError` → 422,
`LlmClientUnavailable` → 503, etc.) is only available on this path;
the async path surfaces those failures via `error_class` on the
polling response instead.

**Job storage is process-local.** The `IngestJobRegistry` keeps
in-memory rows in an `OrderedDict` behind an `asyncio.Lock`,
bounded at 256 terminal jobs (oldest evicted first; live jobs
exempt). A pod restart blows the registry away on purpose -- a job
whose pod died was never going to finish. Durable cross-restart
jobs are a v0.9 follow-up (the same migration that lands
operator-cancellable jobs).

## LLM-client wiring

The grouping pass (T3, `run_llm_grouping` in
`operations/ingest/llm_groups.py`) needs an injected `LlmClient`
Protocol implementation. The chassis exposes the wire-up seam
(`set_llm_client_factory` in
[`api/v1/connectors_ingest.py`](../../backend/src/meho_backplane/api/v1/connectors_ingest.py))
and a fail-closed default (`default_llm_client_factory` in
[`operations/ingest/pipeline.py`](../../backend/src/meho_backplane/operations/ingest/pipeline.py)).
As of #1386, **FastAPI lifespan startup wires a production
`LlmClient`**: `build_anthropic_ingest_llm_client` (in
[`operations/ingest/anthropic_client.py`](../../backend/src/meho_backplane/operations/ingest/anthropic_client.py))
reuses `settings.anthropic_api_key` — the same key the agent runtime
reads — and the same `_split_model_id(settings.agent_default_model)`
prefix handling, talking to the Anthropic Messages API directly (the
one-shot `system + user -> raw JSON` shape the grouping pass wants,
rather than the pydantic-ai `Model` the agent loop uses).

Operationally this means non-dry-run ingest of an un-grouped
connector — whether via the CLI (`meho connector ingest --catalog
<product>/<version>`), the REST route
(`POST /api/v1/connectors/ingest`), or the admin MCP tool
(`meho.connector.ingest`) — **groups successfully on a deploy with
`ANTHROPIC_API_KEY` set**. All three surfaces read the same
lifespan-wired factory: the REST route via the
`get_llm_client_factory` dependency, the MCP tool by calling
`get_llm_client_factory()` directly (it does not pin the default), and
the CLI through the REST route. A deploy that configured **no key**
keeps the fail-closed posture: `build_anthropic_ingest_llm_client`
raises `LlmClientUnavailable`, which the route maps onto HTTP 503 and
the CLI / MCP surfaces render as their own operator-facing variant.
CI / unit tests inject a deterministic stub via
`IngestionPipelineService(..., llm_client_factory=...)` (or
`set_llm_client_factory(...)`) so the grouping pass stays hermetic.

The `composite_l2_missing` error envelope
(`operations/_errors.py:result_composite_l2_missing`) surfaces a
catalog-ingest command as the escape hatch from a missing L2 sub-op;
that escape hatch now completes the ingest when the key is set, and
its envelope text names the `ANTHROPIC_API_KEY` requirement (and the
503 a keyless deploy still gets) so operators know the prerequisite.

**Out of scope (#1386).** The grouping pass talks to Anthropic
directly rather than routing through the G11.5 per-tenant model
resolver (Bedrock / vLLM / VCF PAIF, egress-aware). Ingest grouping is
a build-time operator action with no per-tenant tier or egress context
today, and the resolver returns pydantic-ai `Model`s shaped for the
agent tool-use loop, not the `generate_json` seam — so routing ingest
through it is a separate, larger change. A keyless air-gapped deploy
(agent runtime on an on-prem backend, no Anthropic key) therefore still
gets the 503 on `--catalog` grouping until that work lands.

## Known issues

* **`--catalog` ingest grouping requires `ANTHROPIC_API_KEY`.** The
  grouping pass reuses the agent runtime's Anthropic key (wired at
  lifespan startup, #1386 — see
  [LLM-client wiring](#llm-client-wiring) above). A deploy that set no
  key fails closed with 503 / `LlmClientUnavailable`. Air-gapped
  deploys that route the agent runtime to an on-prem backend (no
  Anthropic key) cannot group spec ingests until grouping is routed
  through the G11.5 resolver — tracked as the out-of-scope follow-up
  noted above.
* **Async-mode jobs don't survive pod restart.** The G0.16-T1
  `IngestJobRegistry` lives in process memory. A pod restart
  during a long-running ingest leaves the operator's client
  polling 404 on a job that won't resume. v0.9 follow-up: persist
  job rows in Postgres and resume on pod startup.
* **Parameter name + location collision.** When an op has two params
  with the same `name` in different `in` locations (e.g. `cluster` as
  path **and** as query), the flat-object representation loses one.
  Real vendor specs in v0.2 scope (vCenter / NSX / SDDC Manager)
  never use this combination. T2 will log a warning if it spots a
  collision after upsert.
* **Other-bucket `$ref` rejected.** `$ref:
  "#/components/headers/X"`, `$ref: "#/components/securitySchemes/X"`,
  `$ref: "#/components/links/X"`, `$ref:
  "#/components/callbacks/X"`, and `$ref: "#/components/examples/X"`
  raise `UnsupportedSpecError` when they appear in a parser-traversed
  slot. None of these buckets are used by a currently-targeted vendor
  spec in a parser-traversed position (most appear inside
  `responses.<code>.headers.<name>` or `content.<media>.examples`,
  which the parser doesn't walk); defer until a real spec needs them.
  G3.11-T7 #1241 landed the `#/components/responses/*` and
  `#/components/requestBodies/*` resolvers (unblocked the GitHub REST
  spec's live ingest); T11 #501 landed the
  `#/components/parameters/*` resolver — see the T8 paragraph above
  and `docs/architecture/spec-ingestion.md` §T1.
* **Cross-document `$ref` rejected.** External files
  (`other.yaml#/...`) raise `UnsupportedSpecError`. Same v0.2.next
  note.
* **`$ref` drill-down rejected.** Refs that walk into a component's
  sub-tree (`#/components/schemas/X/properties/y`,
  `#/components/parameters/X/schema`) raise `InvalidSchemaError`.

## References

* Issue #401 — T1 task.
* Issue #403 — T2 task.
* Issue #404 — T3 task (LLM grouping).
* Issue #402 — T4 task (review-queue state machine).
* Issue #405 — T5 task (CLI verbs).
* Issue #406 — T6 task (REST routes; this module's HTTP surface).
* Issue #407 — T7 task (admin MCP tools).
* Issue #408 — T8 task (vSphere canary).
* Issue #409 — T9 task (this doc + the architecture / operator-runbook pair).
* Issue #740 — G0.9-T8 spec-vs-label cross-check + REST 422 envelope.
* Issue #741 — G0.9-T9 `UncoveredVersionLabel` pre-flight.
* Issue #777 — G0.9.1-T5 shared error-envelope builders + MCP `-32602`
  mapping for `VersionMismatchError` / `UncoveredVersionLabel`.
* Issue #1303 — G0.16-T1 async ingest mode (202 + job handle, in-memory
  `IngestJobRegistry`). The "Async ingest mode" section above is the
  authoritative shape; the issue body carries the consumer-side
  pod-restart repro.
* Initiative #389 — G0.7 spec-ingestion pipeline.
* Initiative #772 — G0.9.1 v0.3.2 dogfood hardening (the rollup that
  parents #777).
* Goal #221 — G0 foundational substrate.
* [docs/architecture/spec-ingestion.md](../architecture/spec-ingestion.md) —
  canonical architecture doc; companion to this codebase map.
* [docs/cross-repo/connector-ingestion.md](../cross-repo/connector-ingestion.md) —
  operator runbook for adding a new vendor surface end-to-end.
* [docs/cross-repo/g07-vsphere-canary.md](../cross-repo/g07-vsphere-canary.md) —
  the worked-example canary procedure operators reproduce locally.
* `meho_backplane/db/models.py::EndpointDescriptor` — the ORM target.
* `meho_backplane/operations/typed_register.py` — typed-connector
  parallel pathway; same body-hash skip-re-embed contract.
* `meho_backplane/connectors/registry.py` — v2 registry where T2
  auto-registers the `GenericRestConnector` shim.
* OpenAPI 3.0.3 spec: https://spec.openapis.org/oas/v3.0.3.html
* OpenAPI 3.1.1 spec: https://spec.openapis.org/oas/v3.1.1.html
