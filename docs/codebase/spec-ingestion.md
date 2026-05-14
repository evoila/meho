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

* **T1 (this module) — OpenAPI parser.** Pure-function. Input: a path
  or URL. Output: a list of `EndpointDescriptorProto`. No DB session,
  no LLM call.
* **T2 — `register_ingested_operations()`.** Bulk-upserts proto rows
  into `endpoint_descriptor`, plus multi-spec merge (vCenter's
  `vcenter.yaml` + `vi-json.yaml` under one connector).
* **T3 — LLM-summarised grouping.** Proposes operation groups and
  assigns each op to one, writing `operation_group` rows.
* **T4 — Review-queue state machine.** Operators move connectors
  through `staged → enabled` (and `disabled` for regression
  rollback) before any op becomes dispatchable.
* **T5–T7 — CLI / REST / MCP surfaces** that drive the pipeline.
* **T8 — vSphere canary** — ingest both vCenter specs end-to-end.
* **T9 — Docs.**

T1 is the foundation: nothing else in the pipeline runs without its
output shape.

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

T2 owns the rest of the ORM columns (`tenant_id`, `source_kind`,
`product`, `version`, `impl_id`, `group_id`, `embedding`, `is_enabled`,
`custom_description`, `custom_notes`, `handler_ref`,
`llm_instructions`).

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

## Dependencies

* **PyYAML 6.0+** — already a transitive dep; we exercise
  `yaml.load(..., Loader=CSafeLoader | SafeLoader)`. The C loader is
  ~5-10× faster on multi-MB specs; the pure-Python fallback works
  identically on platforms without LibYAML.
* **httpx 0.27+** — fetch for `http(s)://` URLs. The chassis already
  depends on httpx for Keycloak JWKS + connector adapters.
* **Pydantic v2** — `EndpointDescriptorProto` uses `ConfigDict(frozen=True)`.

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
* Initiative #389 — G0.7 spec-ingestion pipeline.
* Goal #221 — G0 foundational substrate.
* `meho_backplane/db/models.py::EndpointDescriptor` — the ORM target.
* OpenAPI 3.0.3 spec: https://spec.openapis.org/oas/v3.0.3.html
* OpenAPI 3.1.1 spec: https://spec.openapis.org/oas/v3.1.1.html
