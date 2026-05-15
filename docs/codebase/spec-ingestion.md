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
  :class:`LlmClient` Protocol; production T5 wires the chassis
  Anthropic adapter, tests inject a deterministic stub.
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

### `IngestionPipelineService` (`ingest/pipeline.py`)

End-to-end orchestrator that bundles the parse → register_ingested →
run_llm_grouping pipeline for one connector. Constructed from an
`Operator` (so the service-level audit rows the helpers write carry
the originating operator's identity); the same instance is reused
across T5's CLI verbs, T6's REST routes, and T7's admin MCP tools.

The `LlmClient` Protocol is injected via a factory parameter so the
chassis can lazy-resolve it; the default factory raises
`LlmClientUnavailable` and the REST layer maps it onto HTTP 503. T5
(#405) replaces the default with the production Anthropic-Messages-
API adapter. The `embedding_service` parameter is the test seam to
inject `AsyncMock` so unit tests don't pull the fastembed ONNX
model from huggingface.co.

`ingest(..., dry_run=True)` short-circuits both the DB writes and
the LLM call: parses every spec and returns the parser's
`inserted_count` projection with `grouping=None`. Operators use
this path to validate a spec before committing.

Multi-spec merge: a single `ingest()` call processes a list of
`SpecSource` entries; each is parsed and upserted under the same
connector triple with the spec's URI as the `spec_source` tag, so
operators can distinguish "this op came from vcenter.yaml" vs "this
op came from vi-json.yaml" during review.

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

Source-kind filter excludes typed-connector rows from the operation
count — this endpoint lists *ingested* connectors only, per the
G0.7 review-queue contract; typed connectors live in the v2
registry and operators don't drive them through the review state
machine.

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
  land.
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
override the operator's tenant. Cross-tenant probes surface as 404
`ConnectorNotFoundError`, not 403 — same conflation `ReviewService`
uses to keep the operator-facing failure surface uniform.

The `op_id` path segment uses the `:path` converter so operations
whose natural key contains slashes (`"GET:/api/vcenter/cluster"`)
round-trip through URL routing intact. The route module's
`set_llm_client_factory(factory)` helper lets the production
bootstrap (G0.7-T5) install the Anthropic adapter and lets tests
inject deterministic stubs.

### `parse_openapi(spec_path_or_uri, *, spec_source=None)` (`ingest/openapi.py`)

The only public entry point. Resolves the input (file path or
`http(s)://` URL via `httpx`), sniffs YAML vs JSON via
`detect_spec_format`, decodes, validates the OpenAPI version
(3.0.x / 3.1.x), and walks `paths`. Returns a list.

The function is synchronous because callers are CLI / one-shot
ingestion endpoints that have no in-flight event loop concern. It
also keeps the surface trivially testable.

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

### T2 control flow

```text
register_ingested_operations
├─ _detect_op_id_collisions    # set scan; raise OpIdCollision (within-batch) before DB writes
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

## Known issues

* **Parameter name + location collision.** When an op has two params
  with the same `name` in different `in` locations (e.g. `cluster` as
  path **and** as query), the flat-object representation loses one.
  Real vendor specs in v0.2 scope (vCenter / NSX / SDDC Manager)
  never use this combination. T2 will log a warning if it spots a
  collision after upsert.
* **Other-bucket `$ref` rejected.** `$ref:
  "#/components/requestBodies/X"`, `$ref:
  "#/components/responses/X"`, `$ref: "#/components/headers/X"`
  raise `UnsupportedSpecError`. Not used by any currently-targeted
  vendor spec (vcenter.yaml, vi-json.yaml, NSX, SDDC Manager);
  defer until a real spec needs them. (T11 / #501 landed the
  `#/components/parameters/*` resolver — see the T8 paragraph
  above and `docs/architecture/spec-ingestion.md` §T1.)
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
* Initiative #389 — G0.7 spec-ingestion pipeline.
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
