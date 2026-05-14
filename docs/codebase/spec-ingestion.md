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
* **T5–T7 — CLI / REST / MCP surfaces** that drive the pipeline.
  T7 ships first (PR for #407) with two service classes:
  `ConnectorAdminService` (`ingest/admin_service.py`) for
  `ingest()` + `list_connectors()`, and `ReviewService`
  (`ingest/service.py`, from T4) for the read + edit + state-
  machine methods. T5 (CLI) and T6 (REST) consume the same
  service surface. The agent's daily tool list stays unchanged;
  the seven admin tools live under the `meho.connector.*`
  namespace and only `tenant_admin` operators (plus the two
  read tools at `operator` role) see them in `tools/list`.
* **T8 — vSphere canary** — ingest both vCenter specs end-to-end.
* **T9 — Docs.**

T1 produces the proto shape every other stage consumes; T2 is the
single write path into `endpoint_descriptor` for ingested rows; T3
groups them; T4 gates dispatchability behind operator review.

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
adapter (Anthropic Messages API) lands with T5 (#405) which will
also surface the model id + retry policy as `Settings` knobs.

### T7 (admin MCP tools) at a glance

`backend/src/meho_backplane/mcp/tools/connector_admin.py` registers
seven MCP tools at module import:

| Tool | Required role | Wraps |
|------|---------------|-------|
| `meho.connector.ingest` | `tenant_admin` | `ConnectorAdminService.ingest()` |
| `meho.connector.list` | `operator` | `ConnectorAdminService.list_connectors()` |
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

Each tool's handler is a thin shim that constructs the right
service class (`ConnectorAdminService` for ingest/list, `ReviewService`
for everything else), translates the JSON-Schema-validated arguments
into the service method's keyword arguments, and `model_dump(mode="json")`s
the typed response. No business logic in the handler — the service
classes are what T5 (CLI) and T6 (REST) also consume.

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
* **Non-schema `$ref` rejected.** `$ref: "#/components/parameters/X"`,
  `$ref: "#/components/requestBodies/X"`, etc. raise
  `UnsupportedSpecError`. vCenter doesn't use these; future specs
  that do will need either a pre-process pass or a v0.2.next
  extension of the resolver.
* **Cross-document `$ref` rejected.** External files
  (`other.yaml#/...`) raise `UnsupportedSpecError`. Same v0.2.next
  note.
* **`$ref` drill-down rejected.** Refs that walk into a component's
  sub-tree (`#/components/schemas/X/properties/y`) raise
  `InvalidSchemaError`.

## References

* Issue #401 — T1 task.
* Issue #403 — T2 task.
* Issue #404 — T3 task (LLM grouping; this module).
* Issue #402 — T4 task.
* Initiative #389 — G0.7 spec-ingestion pipeline.
* Goal #221 — G0 foundational substrate.
* `meho_backplane/db/models.py::EndpointDescriptor` — the ORM target.
* `meho_backplane/operations/typed_register.py` — typed-connector
  parallel pathway; same body-hash skip-re-embed contract.
* `meho_backplane/connectors/registry.py` — v2 registry where T2
  auto-registers the `GenericRestConnector` shim.
* OpenAPI 3.0.3 spec: https://spec.openapis.org/oas/v3.0.3.html
* OpenAPI 3.1.1 spec: https://spec.openapis.org/oas/v3.1.1.html
