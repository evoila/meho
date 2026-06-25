# Dispatch request preview (read-only would-be HTTP request)

**Initiative:** #1666 — G0.24 v0.13.0 log-sentry extended dogfood hardening
**Task:** #1683 — expose the literal would-be HTTP request for a dispatch
**Consumer signal:** `claude-rdc-hetzner-dc#1138`

## Overview

When an ingested-L2 **write** dispatch fails upstream (a gh-rest `422`
validation failure, a `403` scope rejection), an operator needs to read
back *what meho actually put on the wire* to diagnose it. They cannot get
that from the audit trail: the operation audit persists only a **hashed**
`params_hash`, never the resolved method / path / body. The hash is an
intentional privacy + row-size choice — full args may carry secrets, and
the read-broadcast safety note in `mcp/tools/docs.py` depends on the raw
query never reaching the feed. During the #1656 dogfood the consumer had
to "bisect payload shapes from the outside" to discover the request body
was being sent wrapped (`{"body": {...}}`), because nothing inside meho
exposed the constructed request.

The **dispatch request preview** closes that gap with the lowest-friction
shape consistent with the dumb-substrate posture: a read-only path that
resolves an op + params to the literal request and **returns** it —
`{method, resolved_path, query, redacted_body}` — instead of sending it.
It is the observability counterpart to the two functional siblings in the
same Initiative:

- **#1656** (T5) — unwrap the ingested requestBody container on the wire
  (the bug whose payload shape this surface now makes visible).
- **#1649** (T4) — map upstream `403` / `422` to a structured connector
  error with actionable detail.

This surface is **request-time observability, not a new persisted-secret
surface**: nothing is written to the audit row, the `params_hash` design
is untouched, and the body is redacted through the **same**
connector-boundary pipeline the response path uses.

## Scope

In scope: the literal would-be request for an `source_kind='ingested'`
op, redacted, returned without sending.

Out of scope (honouring #1683's dispositions):

- **No persisting the raw body.** `params_hash` stays the audit field.
- **No replay.** Inspecting a *would-be* request only; re-dispatching a
  past audited request is a separate governance concern (Goal #1651).
- **Non-ingested ops.** A `typed` / `composite` op runs a Python handler
  (which may make zero or many HTTP calls) and has no single literal HTTP
  request — the preview returns `status="unavailable"` rather than
  fabricating one.
- **Error-echo on the dispatch path** is deferred (see Known issues).

## Key types

- `meho_backplane.operations._branches.IngestedRequest` — a frozen
  dataclass holding the artefacts a `source_kind='ingested'`
  dispatch puts on the wire: `method`, `path` (placeholders substituted
  *and* the connector's `mount_op_path` prefix applied), `query`
  (the httpx `params=` value, or `None`), `body` (the **raw**,
  unwrapped JSON request body, or `None`), and `headers` (the
  header-located params bucket forwarded to the transport as
  `extra_headers=`, or `None`). The preview envelope surfaces the first
  four; `headers` is consumed by `dispatch_ingested` only.
- `meho_backplane.operations._branches.resolve_ingested_request(...)` —
  the single source of truth for "what method/path/query/body does an
  ingested dispatch send". Shared verbatim by `dispatch_ingested` (which
  then *sends* it) and the preview (which *returns* it), so the previewed
  request can never drift from the real one.
- `meho_backplane.operations._request_preview.preview_dispatch(...)` —
  the read-only orchestration. Mirrors `dispatch`'s Steps 1–3 (parse
  connector id, look up descriptor, validate params) and Step 5 (resolve
  connector instance), then resolves the literal request and redacts the
  body. Returns a structured envelope; never sends, never audits, never
  parks.
- `meho_backplane.operations.meta_tools.preview_operation(...)` /
  `PreviewOperationBody` — the shared meta-tool funnel both transports
  call. Same `{connector_id, op_id, target, params}` argument shape as
  `call_operation` (so an operator re-issues the exact failed arguments).

## Control flow

```text
preview_operation(operator, {connector_id, op_id, target, params})
        │  (REST POST /api/v1/operations/preview, MCP preview_operation)
        ▼
resolve target by name (resolve_target) — in-memory fqdn override only;
        │  NO audit_target_id contextvar bind (no audit row to enrich)
        ▼
preview_dispatch(operator, connector_id, op_id, target, params)
        │
        ├─ parse_connector_id → (product, version, impl_id)
        ├─ lookup_descriptor → None ? → status=error error_code=unknown_op
        ├─ source_kind != 'ingested' ? → status=unavailable (not_ingested)
        ├─ validate_params → errors ? → status=error error_code=invalid_params
        ├─ resolve_connector_or_label → label ? → status=error (no_connector /
        │       ambiguous_connector)
        ├─ get_or_create_connector_instance(cls)   (cached singleton)
        ├─ resolve_ingested_request(...)   ◄── SAME resolver dispatch_ingested uses
        │       → IngestedRequest{method, path, query, body}
        └─ body is not None ?
               → apply_connector_boundary_redaction(body, connector_id,
                     tenant, op).redacted   ◄── SAME pipeline the response path uses
        ▼
{status: ok, op_id, connector_id, source_kind, method,
 resolved_path, query, redacted_body}
```

The HTTP transport (`HttpConnector._post_json` / `_request_json`) is
**never** called: there is no network egress, no audit row, no broadcast
event, no policy-gate park. The policy gate (dispatch Step 4) is
deliberately skipped — a preview reveals only what *would* be sent (and
the body is redacted), so it carries no side effect to authorize, the
same posture as `search_operations` over the same descriptors. Both
surfaces stay `OPERATOR`-gated at the route / tool layer.

## Envelope shape

| `status` | meaning | extra fields |
|---|---|---|
| `ok` | request resolved | `method`, `resolved_path`, `query` (object/null), `redacted_body` (object/null), `source_kind` |
| `error` | structured failure | `error` (`"<code>: …"`), `extras.error_code` (`unknown_op` / `invalid_params` / `no_connector` / `ambiguous_connector` / `dispatch_error`) + per-code detail |
| `unavailable` | not an HTTP-ingested op | `source_kind`, `extras.error_code=preview_unavailable`, `extras.reason=not_ingested` |

Operator-input faults come back **inside** the envelope (not as
exceptions), mirroring the dispatcher's never-raises contract so the REST
route and MCP tool keep one uniform shape. The only exception the
meta-tool raises is the missing-`target.name` `ValueError`, which the
route maps to a `400` (identical to `/call`).

The shared resolver `resolve_ingested_request` deliberately *raises* on a
path-template fault — `KeyError` for an unsubstituted path var, `RuntimeError`
for a descriptor missing its method/path — because the **execute** path relies
on the dispatcher's generic `except` to convert them to a structured
`connector_error`. The preview path has no such wrapper, so
`_build_ingested_preview` catches exactly those two and maps them to a
`dispatch_error` envelope (#2066). Before that wrap they escaped uncaught and
surfaced as MCP `-32603` / HTTP 500 — violating the never-raises contract this
table documents. `resolve_ingested_request` itself is unchanged (the
execute-path contract must keep raising), so the two surfaces stay aligned via
their respective wrappers, not a shared swallow.

## Redaction

The body is redacted through `apply_connector_boundary_redaction(body,
connector_id=..., tenant=..., op=...)` — the exact seam
`dispatcher._apply_redaction_middleware` calls on the connector response.
It resolves the per-`(connector_id, tenant, op)` `RedactionPolicy`
(falling through to the conservative default) and runs the Tier-1 engine
(plus Tier-2 Presidio when the policy carries a `tier2` block). A body
value the redactor masks in a real response is masked identically in the
preview — no new raw-secret surface.

Note the engine matches on the **string value shape** (a bearer token /
JWT / labelled-credential string in a leaf value), not on the dict key
name: a bare `{"password": "…"}` field is not masked by the default
policy unless its *value* matches a named pattern; a `{"upstream_auth":
"Bearer eyJ…"}` value is. See `docs/codebase/redaction.md` for the
named-pattern catalogue and policy resolution.

## Surfaces

- **REST:** `POST /api/v1/operations/preview` (`OPERATOR`), body
  `PreviewOperationBody` (same fields as `CallOperationBody`).
- **MCP:** the `preview_operation` tool (`op_class="read"` — a read-only
  inspection, unlike `call_operation`'s `"tool_call"` envelope).
- **CLI:** regenerated from the OpenAPI snapshot (`cd cli && make
  snapshot-openapi && make generate`).

## Dependencies

- `operations._branches` — the shared request resolver.
- `operations._lookup` — `parse_connector_id`, `lookup_descriptor`,
  `count_known_ops`.
- `operations._validate` — `validate_params`.
- `operations._handler_resolve` — `get_or_create_connector_instance`.
- `connectors.resolve_connector_or_label` — the same yes/no/ambiguous
  connector resolution the dispatcher and the target-probe route use.
- `redaction.apply_connector_boundary_redaction` — the connector-boundary
  pipeline.

## Known issues / deferred

- **Error-echo on the dispatch error path is deferred.** #1683's AC4 made
  attaching the redacted request to a 4xx dispatch error's `extras`
  optional ("otherwise explicitly note it deferred"). The dedicated
  read-only preview is the keystone diagnosis surface and is sufficient
  to recover the wire shape; threading the constructed request out of
  `dispatch_ingested` through the exception unwind into the error result
  is a larger, riskier change for marginal benefit (an operator who hit a
  4xx re-issues the same args against `/preview`). It is recorded here as
  a possible follow-up rather than shipped in this change.
- The preview covers `source_kind='ingested'` only; previewing the
  *effective* request a composite handler would synthesize per sub-op is
  out of scope (composites already carry the park-time `proposed_effect`
  semantic preview, #1608 / #1628).

## References

- Task #1683; Initiative #1666 (G0.24); Goal #221.
- Siblings: #1656 (requestBody unwrap), #1649 (structured `403`/`422`).
- Prior art: the park-time `proposed_effect` preview
  (`operations/_preview.py`, #1437 / #1504 / #1628).
- `docs/codebase/redaction.md` — the redaction pipeline this reuses.
- `docs/codebase/error-message-shape.md` — the structured-error
  convention the `error` strings follow.
