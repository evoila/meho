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

### Shipped-spec / profile on-ramp (#1964 T1 #1975)

A catalog row may carry two optional fields naming MEHO-authored
package data instead of relying on a fetchable `upstream`:

* `spec_resource` — a `.yaml` / `.json` file under
  `meho_backplane.operations.ingest.specs` (constant
  `SPEC_RESOURCE_PACKAGE` in `catalog.py`).
* `profile_resource` — an `ExecutionProfile` document under
  `meho_backplane.connectors.profiles` (`PROFILE_RESOURCE_PACKAGE`).

Both resolve via `importlib.resources.files(...).joinpath(name)` — the
same wheel-and-source-portable shape `load_catalog()` uses for the
catalog YAML. The field validator pins each value to a single path
segment (no `/`, `\`, or `..`) so a resource name can't escape its
package root.

**Why:** `vmware/9.0` and `sddc/9.0` have an `upstream` the backend
can't dereference (HTML developer portal / fqdn-templated appliance
URL) — the `catalog_entry_upstream_not_spec` /
`catalog_entry_templated_upstream` 422s above. The on-ramp ships a
MEHO-authored spec as package data so catalog-driven ingest works
end to end without an operator hand-fetch.

**Route behaviour** (`_catalog_entry_specs` in `connectors_ingest.py`):
when a row carries `spec_resource`, the route reads the bytes via
`load_spec_resource()` and builds a single
`SpecSource(uri="spec:<resource>", content=<bytes>)`. Because
`content` is set, the ingest pipeline uses the bytes verbatim
(size-capped) and skips the fetch + https/SSRF guard entirely — the
bytes are trusted MEHO package data, not a remote URL. A
`spec_resource` row is exempt from `_reject_unusable_entry`'s
typed-/templated-upstream 422s for the same reason (the whole point is
to serve products whose `upstream` is un-fetchable).

**Validator exemption:** a row carrying `profile_resource` is a
profile-backed row whose `requires_connector_class` names a synthesised
`ProfiledRestConnector` subclass materialised from the reviewed profile
(T5 #1971) — it need not pre-exist in the v2 registry when the
boot-time `validate_catalog_registry_coverage()` runs. Both the
class-presence (axis 1) and triple-registration (axis 2) checks skip
profile-backed rows.

**Boot-time dry-run parse:** `validate_shipped_artifacts()` (the fourth
boot guard in `main.py`, after the catalog parse, registry-coverage
check, and per-profile scheme load) walks every row and parses each
shipped artifact with the **same** parser the live path uses —
`parse_openapi(...)` for a spec, `ExecutionProfile.model_validate(...)`
+ `validate_execution_profile(...)` for a profile. A malformed shipped
artifact raises `CatalogError` and crashes the lifespan (CI's app-boot
smoke fails) rather than 500-ing the first `--catalog` ingest that
touches the row. Parsing a spec with the real parser — not a cheap
YAML well-formedness check — is deliberate: a spec that decodes but has
no `paths`, a wrong OpenAPI version, or an unsupported `$ref` is
exactly the "ships fine, fails at ingest" bug this guard catches.

**Packaging:** the two resource dirs live inside the package tree
under `src/meho_backplane/`, so hatch's `packages` glob already
collects their data files into the wheel (same as the packaged
`catalog.yaml` and the `.j2` grouping prompts). `backend/pyproject.toml`
lists them in `[tool.hatch.build.targets.wheel].artifacts` to make the
non-`.py` inclusion explicit; they are NOT in `force-include` (that
table is for trees *outside* the package, like `backend/alembic` —
re-including an in-package path there is a duplicate-archive build
error).

T1 (#1975) ships the mechanism plus a `_fixture/1.0` profile-backed
row pointing at `_fixture_minimal.yaml` in each resource package, so
the boot validator and the catalog-driven ingest path are exercised
end to end.

T2 (#1976) authored the real artifacts:

* `vmware/9.0` → `specs/vmware_rest_minimal.yaml` (9 vCenter inventory
  read ops under `/api`) + `profiles/vmware_rest_minimal.yaml`
  (`session_login` auth, `/api/about` fingerprint).
* `sddc/9.0` → `specs/sddc_manager_minimal.yaml` (9 SDDC Manager
  inventory + lifecycle read ops under `/v1`) +
  `profiles/sddc_manager_minimal.yaml` (`basic` auth,
  `/v1/releases/system` fingerprint).

Both are minimal, self-contained, `$ref`-local OpenAPI 3.0 documents
carrying the SPDX `Apache-2.0` header — only the read ops MEHO surfaces,
vendor-neutral descriptions, the vendor's verbatim path/param/field
names (which the dispatcher must use). The rows flip from
`catalog_ingest: spec-only` to `supported` and drop their `upstream`
(now provenance pointers in `notes`), so `meho connector ingest
--catalog vmware/9.0` / `sddc/9.0` works without a forced `--spec`
upload. The named auth schemes match `docs/codebase/
connector-auth-coverage.md`; the typed `VmwareRestConnector` /
`SddcManagerConnector` still own runtime dispatch. The full vendor
specs stay the `upstream` provenance pointers for a full-surface
re-ingest off the appliance.

Known limitation: the `session_login` named extractor (#1970) is
currently hardcoded to the vRLI login shape (`POST /api/v2/sessions`,
JSON body, `sessionId` → Bearer), which differs from vCenter's
`POST /api/session` (Basic, `vmware-api-session-id` header). The
vmware profile ingests and passes the boot validator, but full profiled
*dispatch* parity for vCenter needs the session extractor to grow a
vCenter variant — owned by the profiled-dispatch wiring
(#1971/#1972), not this data task.

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

The admin MCP tools register at module import across two files, split
by responsibility so neither grows past the code-quality file-size
budget:

* `backend/src/meho_backplane/mcp/tools/connector_ingest.py` — the
  two ingest-pipeline tools (`ingest` + `ingest_status`).
* `backend/src/meho_backplane/mcp/tools/connector_admin.py` — the six
  review / edit / state-machine tools.
* `backend/src/meho_backplane/mcp/tools/_connector_shared.py` — the
  `connector_id` / `tenant_id` schema snippets, op-class strings, and
  the JSON-safe serialiser both tool modules import.

| Tool | Required role | Wraps |
|------|---------------|-------|
| `meho.connector.ingest` | `tenant_admin` | `IngestionPipelineService.ingest()` (+ `IngestJobRegistry` on async) |
| `meho.connector.ingest_status` | `operator` | `IngestJobRegistry.get()` |
| `meho.connector.list` | `operator` | `list_ingested_connectors()` |
| `meho.connector.review` | `operator` | `ReviewService.get_review_payload()` |
| `meho.connector.edit_group` | `tenant_admin` | `ReviewService.edit_group()` |
| `meho.connector.edit_op` | `tenant_admin` | `ReviewService.edit_op()` |
| `meho.connector.enable` | `tenant_admin` | `ReviewService.enable_connector()` |
| `meho.connector.enable_reads` | `tenant_admin` | `ReviewService.enable_reads()` |
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

**Async offload on the MCP path (G3.5-T2 #1531).** `meho.connector.ingest`
carries the same #1303 async-202 offload the REST route has: with
`async=true` (and `dry_run=false`) the handler creates a job row in the
shared `IngestJobRegistry`, fires the pipeline off the request via
`asyncio.create_task`, and returns an `IngestJobHandle` immediately —
well inside the agent's tool-call deadline. The agent polls
`meho.connector.ingest_status` with the returned `job_id` until the
status is `succeeded` (carries the final ingestion + grouping counts),
`degraded` (the pipeline ran but persisted nothing dispatchable —
carries the counts **and** `error_class="ingested_not_dispatchable"` +
`error`; see "Dispatchability postcondition" below), or `failed`
(the pipeline raised — carries `error_class` + `error`). Because both surfaces
share `get_job_registry()`, a run started over MCP is poll-able over
the REST `GET /api/v1/connectors/ingest/jobs/{job_id}` endpoint and
vice versa. `dry_run=true` and `async` unset keep the inline shape —
the pipeline runs on the request and the full `IngestResponse` returns
synchronously (no regression for small-spec / CI callers). This
parallels the `meho.agents.run` + `meho.agents.run_status` async
precedent (#811).

The `ingest` handler additionally maps **every typed `SpecError`
sibling** to JSON-RPC `-32602 Invalid Params` with the structured
detail on `error.data` **on the inline path**: `VersionMismatchError`
and `UncoveredVersionLabel` (the G0.9.1-T5 #777 originals),
`UpstreamNotSpecError`, `UnsupportedSpecError`, `InvalidSpecError`,
`InvalidSchemaError`, `OpIdCollision`, and `LlmOutputInvalid` (#1534).
Each describes a caller-input mistake — the operator's `version` label
disagrees with the supplied spec, the URL served HTML instead of a
spec, the document is the wrong OpenAPI flavour or structurally
invalid, two ops collide on an `op_id`, or the grouping LLM returned
invalid output — so `-32602` is the right code (not `-32603 Internal
Error`). Before #1534 only the first two were caught here; the other
six fell through to the dispatcher's generic `except Exception` and
surfaced as a bare `-32603 "internal error: <ClassName>"` with the
diagnostic message discarded — while the REST surface already attached
the detail for all of them, so this closes the MCP↔REST asymmetry.
(#1534's REST detail was still the bare `str(exc)` string for the
five parser-family siblings; #1610 upgraded that 400 to the same
structured envelopes, so both surfaces now ship the builders' dicts.)
The structured `data` payload is built by the shared helpers in
`operations/ingest/error_envelopes.py` (one `build_*_detail` per
class) so the REST 4xx detail and the MCP `error.data` member share
one source of truth; the MCP-side dispatch table that maps each class
to its builder lives in `mcp/tools/_connector_shared.py`
(`SPEC_ERROR_TYPES` + `raise_invalid_params_for_spec_error`), and the
REST-side five-way 400 dispatch lives in
`api/v1/connectors_ingest.py` (`_spec_error_http_exception`, #1610;
the 422-mapped siblings keep their per-class `except` arms). On the
**async** path the handle has already returned by the time the
pipeline raises, so the same failures surface via `error` /
`error_class` on the `ingest_status` poll response instead (the
trade-off the REST async path also makes).

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

Two operator-facing surfaces flag an unreplaced shim:

* **Dispatch-time** (G0.23-T1 #1627) — the shim's
  `NotImplementedError` maps to the structured
  `connector_unsupported` error with
  `extras.cause='unreplaced_auto_shim'` (see
  `docs/codebase/error-message-shape.md`). G0.25-T2 (#1753) adds
  `extras.sibling_impl_id`: when a hand-rolled class for the same
  `(product, version)` already ships under a different `impl_id`
  (i.e. the shim is shadowing it — the near-miss footgun), the
  remediation names that sibling and says "re-ingest under it"
  instead of "write a subclass" (one already exists). Resolved via
  `sibling_handrolled_impl_id()`.
* **Enable-time** (G0.23-T4 #1630) — `ReviewService.edit_op` with
  `is_enabled=True` probes `resolved_auto_shim_class()` (same
  module): a resolver replay against the op's `(product, version)`
  label that returns the winning class's name when the production
  tie-break ladder would still land on a `GenericRestConnector`
  subclass. The PATCH `…/operations/{op_id}` route then returns 200
  with `warnings=[{code='unreplaced_auto_shim', connector_class,
  message}]` (it returned 204 before #1630), the
  `meho.connector.edit_op` MCP tool mirrors the same `warnings`
  list, and `meho connector edit-op --enable` prints
  `warning (unreplaced_auto_shim): …` to stderr. Advisory only —
  the flag is still set (a shim-backed op may be pre-enabled ahead
  of its subclass landing), and resolver misses/ties fail soft to
  "no warning" rather than blocking the write.

### `ProfiledRestConnector` + the tri-state `shim_kind` predicate (G0.28-T1 #1967)

`ProfiledRestConnector` (`connectors/profiled.py`) is the **sibling** of
`GenericRestConnector` — an `HttpConnector` subclass, **not** a
`GenericRestConnector` subclass — that a reviewed declarative
`ExecutionProfile` plugs into to make an ingested REST connector
dispatchable (Initiative #1965; the profile schema/machinery land in
T3–T7). T1 ships the class + the classification only; its `auth_headers`
/ `fingerprint` / `probe` / `execute` raise `NotImplementedError` until
the profile machinery is wired.

The gate that makes this possible is the **tri-state dispatchability
classifier** that replaces the binary `issubclass(GenericRestConnector)`
predicate. Each connector class advertises a `_shim_kind`
(`connectors/base.py`), read everywhere via the `shim_kind()` helper —
never `issubclass`:

| `shim_kind` | Class | Dispatchable? | Resolver tier |
|---|---|---|---|
| `"none"` | hand-coded (default) | yes | highest — a bespoke class always wins |
| `"profiled"` | `ProfiledRestConnector` | yes (once profiled) | middle — beats a bare shim, loses to a hand-coded class |
| `"bare"` | `GenericRestConnector` auto-shim | no (`auth_headers` raises) | lowest — demoted whenever any dispatchable candidate exists |

`"profiled"` is its own tier (not folded into `"none"`) because a
profiled connector carries a bounded `supported_version_range` derived
from the ingested spec that can be *narrower* than a hand-coded class's
broad range. Were it classified identically to a hand-coded class, the
resolver's most-specific-version-match step would let a profiled
connector out-specific — and so shadow — a bespoke connector for the same
`(product, version)`, reinstating the #1750/#1798 product-shadowing
footgun. The resolver's tier-demotion rung
(`_demote_lower_dispatch_tiers`, `connectors/resolver.py`) runs *before*
the specificity step and keeps `none > profiled > bare`, so a hand-coded
class always wins regardless of version-range span or `priority`.

The six former binary-predicate sites all read `shim_kind` now:
`resolver._demote_lower_dispatch_tiers` (tri-state ladder),
`dispatcher` (`is_auto_shim` = `shim_kind == "bare"` so only a bare shim
yields `cause='unreplaced_auto_shim'`; a profiled connector that raises
gets the generic `unsupported_feature`), `handrolled_class_for_impl_id`
and `sibling_handrolled_impl_id` (defer to / name any *dispatchable*
class, `shim_kind != "bare"`), `resolved_auto_shim_class` (warns only on
a `"bare"` resolve), and `delete_connector._auto_shim_keys_for_triple`
(auto-deregisters only `"bare"` shims; a profiled connector's
registration lifecycle is owned by the profile-stamping path, T5 #1971).
The `register_connector_v2` product↔impl_id round-trip hard-fail is
class-agnostic, so it still rejects a divergent profiled registration.

### Profile review-gate interlock (G0.28-T5 #1971)

Stamping an `ExecutionProfile` makes a connector **dispatchable** but must
never **auto-enable** dispatch — that property is security-load-bearing.
`ReviewService.record_profile_stamp(connector_id, *, tenant_id,
connector_class)` (`ingest/service.py`) is the stamp seam:

- It registers the `ProfiledRestConnector` (carrying the vetted profile)
  under the connector's `(product, version, impl_id)` v2 key, making it
  the resolved class for dispatch.
- It does **not** touch any op's `is_enabled` or any group's
  `review_status`. Every ingested op stays `is_enabled=False` /
  `review_status='staged'` exactly as ingested.
- It writes one `meho.connector.profile_stamp` (`OP_PROFILE_STAMP`) audit
  row on the **first** stamp; a re-stamp of an already-registered triple
  is idempotent (returns `False`, no duplicate row). Passing a non-profiled
  class (`shim_kind != "profiled"`) raises `TypeError`.

The interlock that blocks dispatch is the same one that blocks a staged
bare-shim op: `lookup_descriptor` (`operations/_lookup.py`) hard-filters
`is_enabled = TRUE`, so a staged op is invisible to dispatch regardless of
whether its connector is a bare shim, a profiled connector, or a
hand-coded class. Registering a profiled connector changes *what class
dispatch would resolve to*; it changes *nothing* about which ops are
callable. An operator clears the gate per-op via `edit_op(..., is_enabled=
True)` (or connector-wide via `enable_connector`), exactly as for any
ingested connector.

`edit_op`'s enable-time advisory (`enable_time_auto_shim_warnings`) is
tri-state to match: a `"bare"` resolve still yields the
`unreplaced_auto_shim` dead-end advisory; a `"profiled"` resolve yields a
`profiled_but_unreviewed` advisory (`EditOpWarning.code`) confirming the
enable — not the stamp — is what cleared the review gate and made the op
callable. Both advisories decorate a write that already landed; neither
blocks it.

### Authoring-mode `kind` on the list / review surfaces (G0.28-T6 #1979)

The enable-time advisory above surfaces the connector tier only at the
moment an op is enabled. The list and review **read** surfaces carry the
same classification as a standing field so an operator (or the operator
console / CLI) can tell a working profiled connector from a dead bare
shim without enabling anything.

`resolve_authoring_kind(*, product, version, enabled_operation_count)`
(`ingest/connector_registration.py`) replays the production resolver for
the row's `(product, version)` line — the same tie-break ladder dispatch
and the enable-time probes run — and projects the resolved class's
`shim_kind` tier, crossed with the review-gate state, onto a wire
vocabulary returned as `(kind, dispatchable)`:

| `shim_kind` | gate | `kind` | `dispatchable` |
|---|---|---|---|
| `"none"` | n/a | `typed` | `True` |
| `"bare"` (or resolver miss) | n/a | `ingested-shim` | `False` |
| `"profiled"` | cleared (`enabled_operation_count > 0`) | `profiled` | `True` |
| `"profiled"` | closed (zero enabled ops) | `profiled-but-unreviewed` | `False` |

The four values land on two surfaces as **additive** fields — the
existing dispatch-resolution `state` Literal (`ingested` / `registered`)
is left unchanged, because `state` answers "do descriptor rows exist"
while `kind` answers "what backs the connector and can it execute", and
the two move independently:

- `ConnectorListItem` (`ingest/api_schemas.py`), populated in
  `list_connectors.py`. DB-backed `state="ingested"` rows derive
  `kind` / `dispatchable` from the resolver replay; class-side
  `state="registered"` rows derive `kind` from the registered class but
  pin `dispatchable=False` (no descriptor rows ⇒ the dispatcher can't
  resolve a call yet).
- `ConnectorReviewPayload` (`ingest/payload.py`), populated in
  `ReviewService._render_payload`; `enabled_operation_count` is computed
  from the rendered ops.

The list route (`GET /api/v1/connectors`) is untyped (returns a bare
`dict` for per-row UUID serialisation), so the Go CLI's hand-maintained
`listEntry` struct (`cli/internal/cmd/connector/list.go`) mirrors the two
new keys; the review route is typed, so its CLI render reads the
oapi-codegen'd fields. Both surfaces flag a non-dispatchable connector
with a trailing `*` marker in the human table.

The per-scheme **auth** detail of the `ExecutionProfile` is deliberately
**not** surfaced on the review payload yet — deferred until #1969 freezes
that schema (secret-handling sensitivity).

### `check_version_covered_by_registered_class()` (`ingest/connector_registration.py`)

G0.9-T9 (#741) pre-flight that the operator's `version` label is
dispatchable against at least one already-registered class for
`(product, impl_id)`. Mirrors the
`resolver.resolve_connector` PEP 440 `SpecifierSet` check at ingest
time so orphan-at-ingest is caught at the operator's call site,
not at the first `call_operation` against the resulting orphan
rows. Three branches:

* **At least one class registered** for `(product, impl_id)` but
  none accepts the label → raise `UncoveredVersionLabel` (mapped
  to HTTP 422 in the REST router). The exception names every
  candidate class and its advertised range so the operator-facing
  detail tells them exactly what to fix.
* **No class registered** for `(product, impl_id)` but a
  hand-rolled class exists for the same `(product, version)`
  under a **different** `impl_id` → log
  `connector_ingest_near_miss_impl_id` at *warning* level naming
  the sibling `impl_id` and proceed (G0.25-T2 #1753). This is the
  one-token-off footgun (`nsx-rest-probe` ingested when `nsx-rest`
  already ships a hand-rolled class): the shim about to be
  scaffolded is non-dispatchable and may shadow the working
  sibling at resolve time (the resolver tie-break T1 #1750 fixes
  load-bearingly). The guard is defense-in-depth + messaging, so
  it warns rather than refuses — the ingest proceeds unchanged.
  The sibling lookup is `sibling_handrolled_impl_id()` (same
  module), reused by the dispatch-time `connector_unsupported`
  error so both surfaces name the same sibling. The warning log
  carries `sibling_impl_id` as a structured field.
* **No class registered** for `(product, impl_id)` and no sibling
  for `(product, version)` → log
  `connector_ingest_orphaned_class` at info level and proceed.
  This is the v0.4-staging path where ops land before the class
  exists; the dispatcher will surface the gap at the first
  `call_operation` and the ingest-time info log is the upstream
  signal. A genuinely novel `(product, version)` triple is
  unchanged by #1753.

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
records the decision. See
[`docs/cross-repo/connector-catalog.md`](../cross-repo/connector-catalog.md#label-vs-spec-decoupling-spec_info_versions_compatible)
for the field definition and pattern syntax.

**Manual `--spec` opt-in: `IngestRequest.spec_info_versions_compatible`
(T1 #1646).** The explicit-quadruple shape carries the same opt-in as
an optional body field (`meho connector ingest
--spec-info-versions-compatible <band>`, repeatable or comma-separated)
so a self-versioning vendor spec ingests on the manual path too — no
catalog row required. The motivating case
(claude-rdc-hetzner-dc#1136): the version-stable vRLI `/api/v2`
surface reports `info.version="v2"` while the seeded `VcfLogsConnector`
label is `9.0`; ingesting under `--version 9.0
--spec-info-versions-compatible 2.x` decouples the cross-check (`v2`
normalizes to `2`, inside the `2.x` → `>=2,<3` band) while the
class-range pre-flight stays green (`9.0` ∈ `>=9.0,<10.0`). The route
folds the body field together with the catalog-resolved band into the
single value it hands `IngestionPipelineService.ingest` — the two are
mutually exclusive (`IngestRequest` rejects a body that sets both
`catalog_entry` and `spec_info_versions_compatible` with
`catalog_entry_conflict`). Each entry is a glob (`2.x` / `9.0.x`) or a
PEP 440 specifier set (`>=2,<3`); the field validator rejects any other
shape (a bare `v2`, a typo) at request-validation time, so the operator
gets the diagnostic before any spec is fetched. Omitting the field
keeps the historical strict check — the opt-in is explicit, never
default.

### Shared error-envelope builders (`ingest/error_envelopes.py`)

The REST route at `POST /api/v1/connectors/ingest` and the MCP
`meho.connector.ingest` tool both need to surface the typed
`SpecError` siblings as caller-input validation errors carrying
structured diagnostic detail (expected-vs-received versions, the
list of advertised `supported_version_range` strings, the detected
content type, the colliding `op_id`s, the failing grouping pass)
so the operator — or the agent acting on the operator's behalf —
can self-correct without re-prompting. Each builder returns a stable
snake-case `detail` classifier plus a `message` (`str(exc)`), and
the type-specific machine-resolvable fields on top.

The MCP inline path catches the full sibling set (the `except
SPEC_ERROR_TYPES` arm in
`meho_backplane.mcp.tools.connector_ingest`), and
`raise_invalid_params_for_spec_error` (in
`mcp/tools/_connector_shared.py`) dispatches each class to its
builder before raising `McpInvalidParamsError(str(exc),
data=detail)` — surfaced as JSON-RPC `-32602` with the detail on
`error.data` (spec §5.1). The REST sync route catches the five
parser-family siblings in one `except` arm and dispatches them
through `_spec_error_http_exception` (in
`api/v1/connectors_ingest.py`, #1610) onto an
`HTTPException(400, detail=<builder dict>)` — the same envelopes on
the `detail` key; the 422-mapped siblings (`VersionMismatchError`,
`UncoveredVersionLabel`, `UpstreamNotSpecError`) keep their earlier
per-class `except` arms. The eight siblings and their builders:

* `build_version_mismatch_detail(exc)` — `VersionMismatchError`.
  REST embeds the returned dict in the
  `HTTPException(status_code=422).detail` field; MCP embeds it in
  the JSON-RPC `error.data` member.
* `build_uncovered_version_label_detail(exc)` —
  `UncoveredVersionLabel`. MCP carries the structured detail; REST
  emits `str(exc)` for backward compatibility but can switch to the
  structured builder later without changing the wire shape in a
  non-additive way.
* `build_upstream_not_spec_detail(...)` — `UpstreamNotSpecError`
  (the #1211 builder, explicit-quadruple variant for the always-
  explicit MCP path). Names the upstream URL and the detected
  `content_type` that wasn't a spec (e.g. an HTML login page).
* `build_unsupported_spec_detail(exc)` — `UnsupportedSpecError`
  (wrong OpenAPI flavour / unsupported dialect).
* `build_invalid_spec_detail(exc)` — `InvalidSpecError`
  (structurally invalid root document).
* `build_invalid_schema_detail(exc)` — `InvalidSchemaError`
  (a broken `$ref` or invalid embedded JSON Schema — the narrower
  domain, dispatched before `InvalidSpecError`).
* `build_op_id_collision_detail(exc)` — `OpIdCollision`. Adds the
  machine-resolvable colliding `op_id`s plus `product` / `version`
  / `impl_id` and the existing-vs-incoming `spec_source`.
* `build_llm_output_invalid_detail(exc)` — `LlmOutputInvalid`.
  Surfaces `pass_name` (`propose_groups` / `assign_ops`) and
  deliberately omits the verbatim `raw_output` (debug-log material,
  not operator-facing).

The first two are the G0.9.1-T5 (#777) originals; the remaining six
complete the pattern in #1534. Pre-#1534 the MCP path caught only
`VersionMismatchError` / `UncoveredVersionLabel` — the other six
fell through to the dispatcher's generic `except Exception` arm in
`meho_backplane.mcp.server`, surfaced `-32603 "internal error:
<ClassName>"`, and discarded the (already-detailed) exception
message while REST had attached its detail all along. The shared
builders sit in `operations/ingest/error_envelopes.py` so the REST
4xx body and the MCP `-32602` `data` member stay one source of
truth, and the `SPEC_ERROR_TYPES` tuple co-located with
`raise_invalid_params_for_spec_error` keeps the `except` target and
the isinstance dispatch in lockstep — adding a sibling means
touching both, so the two surfaces can't drift.

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

Since G0.23-T5 (#1636) the op rollup also splits enabled-vs-total,
mirroring the group rollup's `CASE WHEN` technique:
`enabled_operation_count` counts the rows whose per-op `is_enabled`
flag is set (the dispatchable subset) while `operation_count` stays
the total over the same unfiltered `source_kind` universe. The two
`enabled_*` fields count different axes — `enabled_group_count`
buckets groups by `review_status`; `enabled_operation_count` reads
the per-op dispatchability bit — so an operator (or an LLM browsing
the catalog) can tell "~2,211 ops ingested" from "the fraction
actually callable" on a `vmware-rest-9.0` row without drilling into
`/review`.

Class-side registrations from the v2 connector registry that have
no DB-side state yet (T5 #733 — "State 0.5" connectors registered
via `register_connector_v2` but without any rows in
`operation_group` / `endpoint_descriptor`) are unioned into the
response with every count zeroed (`group_count: 0,
operation_count: 0, enabled_operation_count: 0`) and
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
  <p> --version <v> --impl <i> --spec <upstream-openapi-uri>` where
  `<p>` is the **registry** product (the spelling the connector class
  registers under, e.g. `sddc`). Rationale calls out the missing catalog
  entry so the operator knows they need to source the OpenAPI spec
  themselves. Manual mode is the same path G0.7-T5 already supports for
  one-off / not-yet-curated specs (see `ingest.go`'s mode dispatch). The
  registry product is the right spelling because the ingest write path
  keys two safety steps on the supplied `--product` —
  `check_version_covered_by_registered_class` (the version-coverage
  pre-flight) and `ensure_connector_class_registered` — so it must find
  the real `SddcManagerConnector`. Post-#1814 the registry product
  *equals* the parser-derived product (the family was realigned to short,
  dispatch-canonical tokens), so the emitted `--product` round-trips its
  connector_id and the ingest is dispatchable directly — no register-time
  reconciliation. (Before #1814 the registry product was a long token
  like `sddc-manager` while rows reconciled down to `sddc`; #1817 retired
  that bridge once the family realigned. A divergent `--product` is now
  rejected at the ingest boundary with a `422`; see "Product identity at
  the ingest boundary" below.)

The **catalog lookup** uses the **registry's** `(product, version)`.
Post-#1814 that equals the parser-derived product for every connector
(the catalog stores `product="sddc"` and the listing emits `"sddc"`),
so `--catalog sddc/9.0` resolves cleanly and the operator's ingest
matches the registered class and dispatches without any reconciliation.

#### Product identity at the ingest boundary (claude-rdc-hetzner-dc#1136, Initiative #1810)

The dispatch/query surface derives the product from the connector_id
(`parse_connector_id("sddc-rest-9.0") -> "sddc"`), so the only product
spelling that dispatches is the one the connector_id round-trips to.
Historically the VCF family registered under a *long* product
(`SddcManagerConnector.product = "sddc-manager"`) that diverged from
that derived spelling, so an ingest under the long product landed rows
the dispatcher never queried — the listing's round-trip integrity gate
dropped them and the catalog reported `registered, 0 ops` even though
the rows existed. The six historical splits were
`hetzner-robot/hetzner`, `sddc-manager/sddc`, `vcf-automation/vcfa`,
`vcf-fleet/fleet`, `vcf-operations/vrops`, and `vcf-logs/vrli`.

That divergence is now **closed at the source**, not bridged:

- #1798 realigned vRLI and #1814 (Initiative #1810) realigned the other
  five so every connector registers under its short, dispatch-canonical
  product directly.
- #1816 promoted `register_connector_v2`'s product↔impl_id round-trip
  check to a hard-fail, so a connector can no longer register under a
  divergent product at all.
- #1817 added a round-trip guard at the ingest route boundary
  (`_assert_product_round_trips` in `api/v1/connectors_ingest.py`) that
  rejects a supplied product not equal to the connector_id's
  parser-derived product with a `422 product_impl_id_mismatch`, before
  any spec is fetched or row written. With divergent ingests rejected
  up front, the old register-time row reconciliation
  (`_reconciled_row_product` / `dispatch_product`) became dead and was
  retired: `register_ingested_operations` now persists rows under the
  supplied product verbatim, and the grouping pass keys on the same
  spelling.

So the supplied product is the dispatch-canonical product on every
accepted ingest — descriptors, groups, the auto-shim, and the
version-coverage pre-flight all key on one spelling. Regression
coverage:
`tests/test_operations_register_ingested.py::test_aligned_product_ingest_persists_supplied_product`,
`::test_divergent_product_ingest_trips_registration_hard_fail` (the
backstop), the boundary 422 in
`tests/test_api_v1_connectors_ingest.py::test_ingest_divergent_product_rejected_with_422`,
and the verb round-trip in
`tests/test_operations_ingest_catalog.py::test_registered_next_step_verb_round_trips_to_dispatchable_ingest`.

#### Dispatchability postcondition on async jobs (claude-rdc-hetzner-dc#1136)

A background pipeline coroutine that returns without raising is **not**
sufficient evidence the ingest succeeded: it can persist rows under a
mis-keyed product (above), leaving nothing dispatchable. `run_ingest_job`
(`ingest/jobs.py`) therefore consults a `dispatchability_check` closure
(the route's `connector_exists` probe under the parser-derived natural
key, scoped to the originating tenant) before flipping the job to
`succeeded`. The job ends `degraded` carrying
`error_class="ingested_not_dispatchable"` and the counts that landed
when the run is genuinely non-dispatchable — either `inserted_count == 0`
on a connector the probe cannot resolve (an empty/first-run spec), or
`inserted_count > 0` yet the probe returns `False` (the mis-keyed-product
case). Crucially, the zero-insert branch is **not** unconditional: a
benign idempotent re-run skips every op (`_upsert.upsert_one_operation`
returns `"skipped"`, so `inserted_count == 0`) on an already-dispatchable
connector, and the probe keeps that `succeeded` rather than flipping a
no-op re-run into a non-zero CLI failure. A probe that *raises* fails
open to `succeeded` (a transient DB blip must not strand or degrade a
completed pipeline). Regression coverage:
`tests/test_operations_ingest_jobs.py`.

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
  `ConnectorListItem.enabled_operation_count` (G0.23-T5 / #1636)
  splits the op rollup enabled-vs-total next to the existing
  `operation_count` — naming mirrors the `*_group_count` family
  (unprefixed = total, `enabled_`-prefixed = subset), kept additive
  so existing `operation_count` consumers don't break.
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

The `/api/v1/connectors*` routes that wire the service layer to the
operator-facing HTTP surface. RBAC: read paths (GET /, GET
/{id}/review) require `operator` role minimum; write paths
(`POST /ingest`, `PATCH /groups`, `PATCH /operations`, `POST
/enable`, `POST /enable-reads`, `POST /disable`, `DELETE /{id}`)
require `tenant_admin`. Tenant scoping derives from the JWT — there
is no body / query parameter that can override the operator's tenant.

**Bulk read-class enable (G0.25-T7 #1749).** `POST
/{id}/enable-reads` flips `is_enabled=true` on every *ingested*
operation whose HTTP `method` is `GET` or `HEAD`
(`READ_HTTP_METHODS`), leaving every write-shaped verb
(POST / PUT / PATCH / DELETE) and every typed / composite op
(`method` NULL) default-deny — writes keep their per-op / composite
curation by design (the governance boundary). The point is broad
governed *read* coverage on big ingested surfaces (`vmware-rest-9.0`
is 3000+ ops, mostly staged GETs) without a per-op death-march.
`EndpointDescriptor` carries no `op_class` column — the read/write
taxonomy the MCP tool registry uses lives on `ToolDefinition`, not
the descriptor row — so HTTP method *is* the per-row read-class
signal for ingested ops. The route returns `200` with
`{connector_id, ops_enabled}` (not the `204` the enable / disable
transitions return) so the count of flipped ops rides the wire;
unlike `enable`, it does **not** move any group's `review_status`
(it is a per-op flip, so there is no state-machine guard / transition
409). One `meho.connector.enable_reads` audit row is written when
at least one op flips; idempotent — a re-run flips nothing, writes
no audit row, and returns `ops_enabled=0`. The bulk UPDATE lives in
`bulk_enable_read_ops()` (`ingest/_internals.py`), called by
`ReviewService.enable_reads()`; the CLI (`meho connector
enable-reads`) and the MCP tool (`meho.connector.enable_reads`,
optional `tenant_id` for the built-in scope) wrap the same service
method, the single-source discipline the rest of the surface
follows. Scope resolution shares `_resolve_existing_scope` with the
`/review` read path (see "shared scope resolution" below), so a
built-in-only label enables its reads via the global fallback
instead of 404'ing, and a tenant+built-in ambiguous label raises a
409 `connector_scope_ambiguous` instead of silently flipping one
scope (G0.26-T1 #1801).

**Cross-surface write-scope contract (#1699).** The two ingest
surfaces intentionally default to *different* write scopes:
`POST /ingest` (and the `meho connector ingest` CLI verb that drives
it) always writes under the calling operator's `tenant_id`, while
the MCP tool `meho.connector.ingest` accepts an optional `tenant_id`
argument and targets the built-in / global scope
(`tenant_id IS NULL`) when the argument is omitted (tenant_admin
only). The dedup lookup in `operations/ingest/_upsert.py` scopes its
natural-key match by `tenant_id`, so re-ingesting the same spec
under the other scope matches nothing and re-inserts every operation
as a shadow copy in the other namespace — by design (the namespaces
are isolated), but surprising when an operator mixes surfaces
expecting an idempotent re-ingest. Both surfaces document the
contract (the route docstring, the MCP tool description, and the
registered-row `next_step` rationale all name the right surface per
scope); the cross-surface behaviour is pinned by
`test_cross_surface_reingest_under_global_scope_creates_shadow_copy`
in `tests/test_api_v1_connectors_ingest.py`.

**Shared scope resolution (`get_review_payload` + `enable_reads`,
G0.13-T5 #1135 / G0.26-T1 #1801).** Every read/enable-reads path
applies the same "operator's-tenant rows + built-ins (`tenant_id IS
NULL`)" scope. The listing query does it inline via a single `WHERE
tenant_id IS NULL OR tenant_id = X` clause. The single-connector read
(`get_review_payload`) and the bulk write (`enable_reads`) both route
through one shared helper — `ReviewService._resolve_existing_scope` —
so they can never diverge on *which row* they act on. The resolver:

- parses + authorises via `_resolve_scope` (a cross-tenant `tenant_id`
  ≠ the operator's own is collapsed to a 404 `ConnectorNotFoundError`
  here, preserving the no-enumeration conflation);
- for an explicit built-in probe (`tenant_id=None`, the MCP admin
  path) does a single-pass existence check — no fallback, no
  ambiguity;
- for the operator's own tenant, probes **both** the tenant scope and
  the built-in scope with `scope_has_groups` and then:
  - both exist → raises `AmbiguousConnectorScopeError` (the route maps
    it to a 409 `connector_scope_ambiguous` enumerating the candidate
    rows — neither a silent pick nor a bare 404);
  - only the tenant row → the tenant scope;
  - only the built-in row → the built-in scope (the **#1135 global
    fallback**, now shared with the write path);
  - neither → 404 `ConnectorNotFoundError`.

G0.13-T5 (#1135) first landed the fallback on the *read* route after
the v0.6.0 RDC dogfood flagged that every global connector returned
404 on review even though the listing surfaced them. G0.26-T1 (#1801)
extended it to the write path and added the disambiguation after the
v0.16.0 dogfood hit `POST /{id}/enable-reads` 404'ing on a label
`GET /{id}/review` resolved happily — the read/write resolution
asymmetry. `bulk_enable_read_ops` and the `/review` payload render
both run against the scope the shared resolver returns.

The PATCH editing routes (`/groups`, `/operations`) and the
connector-level `enable` / `disable` / `delete` transitions still
keep their single-pass lookup against the operator's `tenant_id` via
`_resolve_scope`: "do tenant_admins get to edit / transition
built-ins?" is a policy choice distinct from the read/enable-reads
visibility surface, and the route gate is `tenant_admin`-only —
built-in writes already have an explicit MCP / CLI affordance
(`ReviewService` accepts `tenant_id=None` under the `TENANT_ADMIN`
role). They are free to adopt `_resolve_existing_scope` later if the
same global-fallback / disambiguation is wanted there.

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

## Security: SSRF and local-file guard (G0.16-T8, #95)

`_load_spec_bytes` (the fetch sink shared by `parse_openapi` and
`read_spec_info_version`) enforces two invariants before any network
activity:

1. **Scheme allowlist.** Only `https://` is accepted on the
   network-facing ingest path. `http://`, `file://`, and bare filesystem
   paths are rejected with `InvalidSpecError`. The restriction covers both
   the REST `POST /api/v1/connectors/ingest` and the MCP
   `meho.connector.ingest` tool, both of which are `TENANT_ADMIN`-gated.

2. **Pre-connect destination guard (`_assert_fetchable_remote_url`).** Before
   opening any socket, the hostname is resolved with `socket.getaddrinfo`
   and every returned address is checked against the private / loopback /
   link-local / ULA / reserved ranges using `ipaddress`. Any candidate IP in
   `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
   `169.254.0.0/16` (cloud metadata), `::1`, `fc00::/7`, or `fe80::/10` is
   rejected before the transport opens a connection.

3. **Per-hop redirect re-validation.** `_fetch_spec_bytes` (invoked by
   `_load_spec_bytes` when no content is uploaded) uses
   `follow_redirects=False`, resolves each `Location` against the current
   URL (`urljoin`, so relative hops aren't wrongly rejected), and calls
   `_assert_fetchable_remote_url` on the resolved target before issuing the
   next request. A redirect from a public host to a private IP is rejected
   at the hop — the private-target socket is never opened.

4. **Response size cap.** The response body is streamed and rejected if it
   exceeds 20 MiB (`_MAX_SPEC_BYTES`), preventing a redirect to a large
   internal endpoint from exhausting pod memory.

5. **Oracle-free error messages.** Error messages never echo the
   operator-supplied URI or OS-level error text. Error text is
   intentionally terse and path-free.

Pre-#95: `http`/`https` URIs were fetched with `follow_redirects=True` and
no IP check; `file://` URIs and bare paths were read via `Path(...).read_bytes()`;
OS errors were echoed verbatim. The fix removes the filesystem branch from this
network-facing function entirely.

## Control flow

```text
parse_openapi
├─ _load_spec_bytes        # CLI-uploaded content (capped) OR https:// fetch
│  ├─ content present       # docs:/file:// bytes uploaded by CLI; no fetch, no guard (#102)
│  ├─ docs:<...> + no content rejected with UnsupportedSpecError (#1535)
│  └─ _assert_fetchable_remote_url  # https-only SSRF guard; DNS resolve, IP allowlist
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

## Async ingest mode (G0.16-T1 / #1303; MCP carry G3.5-T2 / #1531)

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

**The Go CLI consumes the handle (G0.22-T4 / #1609).** `meho
connector ingest` (`cli/internal/cmd/connector/ingest.go`) treats
202 as a first-class success: by default it polls the job to a
terminal status every 2s and renders the same summary / `--json`
`IngestResponse` shape the sync 200 path renders, so script
consumers see one stable success document regardless of how the
backplane ran the pipeline; `--no-wait` exits 0 with the handle
instead. A failed job renders `error_class` + the capped `error`
as `unexpected_response` (exit 4); a 404 on the poll (pod restart /
eviction) tells the operator to check `meho connector list`
**before** re-running ingest. Pre-#1609 CLIs rendered the 202
itself as a fatal `unexpected_response`, which baited operators
into retrying and double-ingesting. `--dry-run` is unaffected
(always sync, see above), and `--no-wait --dry-run` is rejected
client-side.

**Re-attaching after `--no-wait` (#1621).** `meho connector
ingest-status <job-id> [--wait] [--json]`
(`cli/internal/cmd/connector/ingest_status.go`) closes the loop the
`--no-wait` exit deliberately left open: once an operator has
detached, or lost the waiting session, this verb re-reads the same
`GET /api/v1/connectors/ingest/jobs/{job_id}` route. Without `--wait`
it reads one snapshot -- a `running` job prints its identity +
lifecycle echo (job_id, status, the originating request descriptors,
`started_at`) and exits 0 (`--json` emits the raw
`IngestJobStatusResponse`); a terminal job renders exactly what the
waiting-ingest path renders. With `--wait` it re-attaches the same
2s poll loop until terminal. Terminal rendering and the poll loop are
**shared** with `ingest` (the extracted `renderIngestTerminal` switch
+ `pollIngestJob`), not duplicated, so succeeded / failed / degraded /
undocumented-status / 401 / 403 / 404 all behave identically across
the two verbs. A non-UUID `<job-id>` fails fast client-side as
`unexpected_response`. The verb is the CLI twin of the MCP
`meho.connector.ingest_status` poll tool (#1531); the `--no-wait`
output and the poll-phase error guidance now name it instead of only
the raw poll URL.

**The MCP surface shares the offload (G3.5-T2 / #1531).** The
`meho.connector.ingest` admin MCP tool carries the same async shape:
`async=true` (with `dry_run=false`) creates a job in the **same**
`IngestJobRegistry` (via `get_job_registry()`), fires the pipeline off
the request with `asyncio.create_task`, and returns an `IngestJobHandle`
inside the agent's tool-call deadline; the agent polls
`meho.connector.ingest_status` (which reads the registry through the
same accessor) until the job is `succeeded` / `failed`. Because both
surfaces resolve the one process-wide registry, a job started over MCP
is poll-able over `GET /api/v1/connectors/ingest/jobs/{job_id}` and
vice versa. The MCP path defaults `async=false` (inline) so existing
small-spec / CI callers are unaffected; it is the agent-facing surface
that real vendor specs blocked past the tool-call timeout before this
carry. See the "T7 (admin MCP tools) at a glance" section above for
the per-tool wiring + the inline-vs-async error-surface split.

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

## Connector deletion (G0.25-T2 #1700)

Aborted ingests leave junk behind: a spec that parses to zero
operations (or a batch that fails mid-way) has already registered its
`GenericRestConnector` auto-shim before the upsert loop ran, so the
catalog shows a permanent `state="registered"` stub with nothing to
dispatch. The DELETE surface removes it:

* **REST** — `DELETE /api/v1/connectors/{connector_id}` → `204 No
  Content`. `tenant_admin` role. Always scoped to the calling
  operator's tenant (the #1699 contract: the route exposes no
  `tenant_id` parameter).
* **MCP** — `meho.connector.delete(connector_id, tenant_id=None)` →
  `{ok: true, deleted: {...}, warnings: [...]}`. `tenant_id` omitted
  targets the built-in / global scope (the only path that can remove
  `tenant_id IS NULL` rows), mirroring `meho.connector.ingest`.

Both delegate to `ReviewService.delete_connector`
(`operations/ingest/delete_connector.py` is the engine). Semantics:

* **Row removal, not a status flag.** The scoped
  `endpoint_descriptor` + `operation_group` rows are deleted in one
  transaction together with one `meho.connector.delete` audit row
  (group keys, op counts, enabled-op count, deregistration flag — the
  forensic trail). The task's original `review_status='deleted'`
  sketch would have required widening the
  `ck_operation_group_review_status` CHECK (an Alembic migration) and
  an "exclude deleted" filter on every reader; row removal needs
  neither, and the scoped-out undo path was already "re-ingest brings
  it back" (deviation recorded on #1700).
* **Auto-shims only, and only when nothing remains.** The v2-registry
  class is popped only when (a) it is a `GenericRestConnector`
  subclass and (b) no rows remain for the triple under *any* tenant
  scope after the delete (the registry is process-global; popping it
  while another tenant still has rows would break that tenant's
  dispatch). Hand-coded classes are never deregistered — they
  re-register at every boot, so the connector reverts to the truthful
  `state="registered"` listing row instead.
* **Zero-op stubs are registry-only deletes.** No rows anywhere + a
  matching auto-shim → pop + audit + 204. The registry match uses the
  parsed-natural-key round-trip; post-#1814 every connector (and its
  auto-shim) registers under the short, dispatch-canonical product, so
  the rows and the shim share one spelling (`sddc` rows ↔ `sddc` shim).
* **404 conflation.** Unknown id, cross-tenant probe, rows visible
  only under a scope the caller did not name, and repeat deletes all
  return the same 404 the other connector routes use.
* **Enabled ops warn, never block.** The delete completes; the
  advisory rides the MCP response body (`warnings[]`, the `edit_op`
  #1630 discipline), the `connector_delete_enabled_ops` log event,
  and the audit payload. The REST response stays a body-less 204.
* **Re-ingest revives.** A later ingest of the same triple
  re-registers the shim (`connector_registered=True`) and re-lands
  rows from scratch.
* **Process-local deregistration.** The registry pop applies to the
  serving pod; sibling replicas keep the class until their next
  restart (no startup path re-registers ingest shims, so restarts
  converge every pod on the post-delete state). The DB rows are the
  durable truth and are gone everywhere immediately.

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
* **Swagger 2.0 rejected with an actionable remedy.** A spec declaring
  `swagger: "2.0"` (no `openapi` key) raises `UnsupportedSpecError`
  whose message names the conversion path — convert to OpenAPI 3.x
  (`swagger2openapi` / `converter.swagger.io`) and re-ingest the 3.x
  output. The parser stays 3.x-only on purpose: the maintained 2.0→3.0
  converters are Node/web-service tools (`swagger2openapi`/oas-kit,
  `converter.swagger.io`), and an in-house converter is a large
  correctness surface the review queue can't backstop, so the
  documented decision (#1532) is to reject-with-remedy rather than
  convert in-process. The `harbor/2.x` catalog row and
  [`harbor-onboarding.md`](../cross-repo/harbor-onboarding.md#spec-ingest-swagger-20--openapi-3x-conversion)
  carry the operator-facing conversion runbook for the exemplar
  2.0-only surface.
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
