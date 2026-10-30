# MCP server

The Model Context Protocol surface in `backend/src/meho_backplane/mcp/`
is MEHO's primary agent-facing transport. Spec-conforming clients
connect via Streamable HTTP at `POST /mcp`, complete the
`initialize` handshake, and then issue `tools/call` /
`resources/read` requests against the connector + reference tool
catalogue.

Most of the surface-level architecture (router mount, dispatch table,
audit-row shape, capability advertisement) lives in
[`backend.md`](backend.md) under the entries tagged
"MCP Streamable HTTP transport entrypoint", "MCP per-operation audit",
"MCP reference tool + resource", and the broadcast-feed resources.
This doc covers cross-cutting concerns that don't fit cleanly into a
single Task entry.

## Overview

* Entry-point module: `backend/src/meho_backplane/mcp/server.py`
* Wire schemas: `backend/src/meho_backplane/mcp/schemas.py`
* Tool / resource registries: `register_mcp_tool` /
  `register_mcp_resource` (plus the #1612
  `register_deprecated_mcp_tool_alias`) in `mcp/registry.py`
* Audit hook: `backend/src/meho_backplane/mcp/audit.py`

The dispatch table lives at module scope and is populated at import
time; reloading via the test fixture
`tests/mcp_test_fixtures.isolated_registry` is the only supported way
to mutate it between requests.

## Protocol version negotiation

`PROTOCOL_VERSION` in
[`backend/src/meho_backplane/mcp/schemas.py`](../../backend/src/meho_backplane/mcp/schemas.py)
pins the MCP revision the server implements
(currently `"2025-06-18"`). It is a build-time constant — not a
settings field, not env-configurable. Bumping it is an explicit
release-cycle decision (CHANGELOG entry + release-body callout, per
the v0.6.0 honesty-callout precedent in PR #1159).

### Server behaviour

The server **always** responds with
`InitializeResponse.protocolVersion == PROTOCOL_VERSION` regardless
of what the client sent. From the MCP 2025-06-18 spec's perspective
this is compliant: "the server MUST respond with its own
`protocolVersion`. If the server does not support the requested
protocol version, it MUST respond with a version it does support
(typically the latest version supported by the server)." MEHO
supports exactly one revision at a time; that revision is the one
in the response.

What the spec does **not** require — and what MEHO historically did
not provide — is a server-side signal that a downgrade or upgrade
just occurred. A client pinned to `"2025-03-26"` receiving a
`"2025-06-18"` response had no way to know which revision the server
agreed to, and the operator's log stream stayed silent on the
mismatch. The consumer-side closed-loop reporting flagged this as
`mcp-initialize-protocol-version-silent-upgrade`
(signal 15), and the v0.6.0 release body was amended (PR #1159) to
call out the gap explicitly as observation-not-commitment.

### G0.14-T13: observability-only

[Task #1202](https://github.com/evoila/meho/issues/1202) lands the
observability half of the gap, deliberately scoped narrow:

* `_initialize` at
  [`mcp/server.py`](../../backend/src/meho_backplane/mcp/server.py)
  compares the validated client `protocolVersion` against
  `PROTOCOL_VERSION` after `InitializeRequest.model_validate(...)`
  succeeds. On mismatch it emits a single structured
  `WARNING` log event:

  ```
  _log.warning(
      "mcp_initialize_protocol_version_mismatch",
      client_protocol_version=<wire value>,
      server_protocol_version=PROTOCOL_VERSION,
      operator_sub=operator.sub,
  )
  ```

  The shape mirrors `mcp_unsupported_protocol_version` — the existing
  WARNING that `_validate_protocol_version_header` already emits when
  a non-`initialize` request carries a stale `MCP-Protocol-Version`
  header. Operators get a uniform event-name family for both
  handshake-time and post-handshake mismatches.

* `HealthResponse` at
  [`api/v1/health.py`](../../backend/src/meho_backplane/api/v1/health.py)
  carries `mcp_protocol_version: str = PROTOCOL_VERSION` (mirrors the
  `mcp_session_id_capture` precedent from G0.14-T6 #1147 — single
  field, single source of truth, surfaced on the authenticated GET).

* `/ready`'s `features` block at
  [`features.py`](../../backend/src/meho_backplane/features.py) gains
  an `mcp` entry of shape
  `{"configured": true, "protocol_version": PROTOCOL_VERSION, "missing_env": []}`.
  Mirrors the `audit_replay` block's shape (no missing env vars
  because the field is a build-time constant, not a deploy-time
  knob). Operators get a single unauthenticated GET that answers
  "which MCP revision does this deploy pin?".

The handshake response body itself is **unchanged**. This is
observability-only: a pinned client requesting `"2025-03-26"` still
gets `"2025-06-18"` back; the server just no longer keeps that
mismatch silent.

### Out of scope (deliberately)

What this task **does not** do:

* **Refusing mismatched clients.** Returning a JSON-RPC error on
  mismatch would break the broad install base of clients that
  currently negotiate against the older revision and silently
  upgrade-tolerate.
* **Down-negotiating to a supported older revision.** MEHO supports
  exactly one revision per server build. Multi-version
  capability-advertisement is a much larger surface change.
* **Version-conditional capability advertisement.** Some MCP
  capabilities (`resources.subscribe`, `prompts`) flip in/out across
  revisions; advertising them conditionally on the client's
  requested version is a per-revision compatibility matrix MEHO
  hasn't committed to maintaining yet.

Future work in any of these directions is gated on **demand
evidence** from real deployments — the observability landed here is
the precondition for measuring whether mismatch is actually
happening in the wild. Operators noticing recurring
`mcp_initialize_protocol_version_mismatch` events for the same
client identity should file a follow-up Task on
[`evoila/meho`](https://github.com/evoila/meho) describing the
mismatch pattern and what behaviour change would unblock them.

## Tool naming grammar and deprecation aliases

Multi-verb tool families on the MCP surface are named with the
underscore `meho_<noun>_<verb>` grammar — `meho_agents_create`,
`meho_approvals_approve`, `meho_connector_ingest`,
`meho_runbook_start`, etc. An agent that has internalised one family
can predict the rest. Every published name must match the Anthropic
tool-name pattern `^[a-zA-Z0-9_-]{1,64}$` (#2745): Claude's frontend
validates remote-MCP tool names against it and rejects the whole
toolset all-or-nothing when one name fails, so dots are banned and the
constraint is pinned by `tests/test_published_tool_schema_shape.py`.
(The grammar was dotted `meho.<noun>.<verb>` from #1612 until the
#2745 rename — a hard rename, since MCP has no alias mechanism.) (A handful of single-purpose reference tools
predating the grammar — `search_docs`, `add_to_memory`,
`call_operation`, … — remain flat.)

One flat pair carries a deliberate cross-surface name split: the kb
meta-tools are `search_knowledge` / `add_to_knowledge` on MCP, but every
other surface names the substrate `kb` — REST `/api/v1/kb`, CLI
`meho kb`, console `/ui/kb`. The names were pinned to the consumer
agent-prompt contract (#417 / #331, "CLAUDE.md naming aligned") and the
alias machinery a rename would need was removed in #1625, so the split is
intentional and not a rename candidate. Because `/api/v1/knowledge` does
not exist, an agent that writes via `add_to_knowledge` and then reaches
for a REST cleanup path 404s unless it knows the substrate is `kb`; the
two tool descriptions therefore cross-reference the `kb` REST/CLI surface
and note that entry deletion is REST/CLI-only (`DELETE /api/v1/kb/{slug}`
or `meho kb delete` — there is no MCP delete tool).

### No issue numbers or planning codes in model text

A model reads every tool description, every schema field description and
every operation-group text. It cannot open an issue link, and a code like
`G0.18-T5` means nothing to it. So these texts hold no issue numbers
(`#1234`), planning codes (`G11.2-T6`) or design-doc section marks
(`§6`). Say the meaning in plain words instead. A change-ticket example
such as `gh:org/repo#7` is fine: the `#` follows a word.
`tests/test_model_text_no_internal_refs.py` checks every tool's
`to_wire()` text and every group registered at start-up. It uses the
same pattern (`_INTERNAL_REF`) that
`backend/scripts/generate_reference_docs.py` uses to strip these codes
from the public reference page.

### Proactive-trigger wording on the coordination levers (#3132)

The three coordination levers — knowledge (`search_knowledge` /
`add_to_knowledge`), memory (`search_memory` / `add_to_memory`), and
broadcast (`meho_broadcast_recent` / `meho_broadcast_announce`) — are
*optional* capabilities: an agent can complete an operation without
ever touching them. Current Claude models under-reach for optional
capabilities, and Anthropic's tool-use guidance is that a prescriptive
"call this when …" trigger in the description measurably lifts
should-call rates. So each of these six descriptions carries an
explicit **proactive-trigger** sentence in its when-to-call part that
names the *unprompted* case — search before answering, capture the
moment something is learned, read the broadcast window before the first
`call_operation`, announce before a mutating op — not only the reactive
case where the operator explicitly asked. The read-before-start /
announce-before-mutate / write-back-after-learning discipline was
previously stated only in the tenant preamble and Layer-2 skills
(advisory context the model does not re-read at call time); moving the
trigger into the per-tool description puts it where the model actually
consults it. The wording is load-bearing per the `operations.py`
description contract — a future edit that drops the proactive sentence
is a behavioural regression, and the substring guards in
`tests/test_mcp_tools_knowledge.py` / `test_mcp_tools_memory.py` pin
the load-bearing pieces. The maturity prefix (#2675) and the kb
REST/CLI cross-references (#2486) are unchanged by this wording pass.

### Tool text size limit on the default list (#4112)

Every session gets the default tool list before the user's question. The
tool text (name, description and input schema) uses up the model's
context window. A small open model often has a 32,768-token window, so
this text must stay short.

`tests/test_mcp_tool_text_budget.py` sets two limits. It measures a tool
as its name plus description plus input schema as compact JSON, taken
from `ToolDefinition.to_wire()`. Output schemas are not counted.

- One tool: at most 6,000 characters.
- The whole default list (26 tools, docs add-on on): at most 70,000
  characters, about 17,500 tokens.

When #4112 set the limits, the default list went from 96,804 to 66,092
characters. The largest tool, `query_topology`, went from 16,302 to
5,537.

What a description keeps:

- what the tool is for, and when to call it (the proactive-trigger
  sentences above stay);
- its arguments, and which are required for which option, because the
  wire schema drops top-level `allOf` / `anyOf`;
- the one or two rules that stop wrong calls.

What moved out:

- Return shapes. Tools with an output schema already describe them there.
- Help that matters only after a certain answer. Example: the
  `query_topology` closure answer with `status: "node_untracked"` now
  carries a `detail` text that says the answer is not "safe to delete"
  and how to add the missing data. The description keeps only the
  short rule.
- Background and history (issue numbers, internal table names, server
  internals). These stay in this file and in the code comments.

If the test fails, shorten the text first. Raise a limit only with a
reason in the pull request.

### Runbook family canonicalisation (#1612)

The 11 runbook tools were the last flat multi-verb family
(`runbook_start`, `runbook_show_template`, …) and additionally split
the template identifier across two field names (`slug` on the five
template verbs, `template_slug` on the run verbs). Both were unified
in #1612:

* **Canonical names** became `meho.runbook.<verb>` for all 11 verbs
  (`draft_template`, `edit_template`, `publish_template`,
  `deprecate_template`, `list_templates`, `show_template`, `start`,
  `next`, `abort`, `reassign`, `list_runs`) — since renamed to
  `meho_runbook_<verb>` by #2745 (Anthropic tool-name conformance).
* **Canonical field** for the template id is `template_slug` on every
  input, and every template-verb response mirrors the model's `slug`
  key as `template_slug` — so an id read from
  `meho_runbook_show_template` or a `list_templates` summary is
  accepted by `meho_runbook_start` verbatim, no rename.

### Alias window (closed — removed in v0.15.0, #1625)

Renames on an agent-facing wire surface always ship with a one-release
alias window (the `content`→`body`, `since`→`cursor`, and
`id`→`approval_request_id` precedents). #1612 added two alias kinds for
the runbook family; both have now been **removed in v0.15.0** by #1625.
The removal was originally pinned to v0.14.0, but v0.14.0 was tagged
with the aliases still registered and no release-notes line, so #1702
re-pinned the deadline to v0.15.0 and published a `### Deprecated`
erratum on the v0.14.0 CHANGELOG section. (There was never a runtime
version gate — `removal_version` was warning/description text only,
because `__version__` is build-metadata-dependent; see #1698 — so the
removal is enforced by #1625 and the release checklist, not by dispatch
logic.)

* **Tool-name aliases (removed).** The flat `runbook_*` names were
  registered alongside their dotted canonical tools via a
  `register_deprecated_mcp_tool_alias` helper that shared the canonical
  handler object and schema and marked the alias with a MEHO-internal
  `ToolDefinition.deprecated_alias_for` field; `handle_tools_call`
  emitted a per-call `mcp_tool_name_deprecated` warning so operators
  could watch consumers migrate. #1625 deleted the registrations, the
  helper, the field, and the warning — a flat name now falls through to
  the dispatcher's standard unknown-tool error. The runbook family was
  the only adopter of the alias machinery, so it was removed wholesale.
* **Field alias (removed).** The five template verbs accepted `slug` as
  a deprecated alias for `template_slug`, guarded by a top-level `anyOf`
  and a handler-level XOR resolver that logged
  `runbook_template_slug_field_deprecated`. #1625 removed the `slug`
  property and the `anyOf`; `template_slug` is now an ordinary required
  field, and supplying `slug` is an unknown property rejected by the
  schema's `additionalProperties: false` gate. The response mirror —
  template-verb responses carry `template_slug` alongside the model's
  native `slug` — stays: it is canonical post-#1612 behaviour, not part
  of the alias window.

The conventions are structurally pinned in
[`backend/tests/test_mcp_tools_list_shape_conventions.py`](../../backend/tests/test_mcp_tools_list_shape_conventions.py)
(§14.9) and documented alongside the other tools/list shape rules in
[`api-shape-conventions.md`](api-shape-conventions.md) §14.

### List-row identifier mirrors (#2471)

The `template_slug` response mirror generalises to a surface-wide rule
(§14.10): **every list row carries the qualified name its sibling verb
accepts, alongside the model-native key.** #2471 closed the last four
mismatches — where a family's list row spelled its id/state one way
while the sibling `show` / `cancel` / `poll` / filter verb required
another, so the obvious round-trip failed with `-32602`.

Two molds apply, by where the mismatch sits:

* **Input arg** (a resource UUID off the `<noun>_id` grammar) →
  canonical-name + deprecated-alias, the §14.3 approvals mold.
  `meho_agents_run_status` took `handle`; #2471 renamed it to the
  canonical `run_id` (the key its sibling `meho_agents_run` /
  `meho_agents_list_runs` rows already return) and kept `handle` as a
  `deprecated: true` alias for one cycle, guarded by an `anyOf` and a
  handler-level XOR resolver that speaks `run_id` in every error string.
* **Response key** on a REST-shared row → additive mirror, the
  `_mirror_template_slug` mold. The row carries **both** the native key
  and the qualified name, equal values; the REST surface and shared
  Pydantic model are untouched. #2471 added `_mirror_grant_id` (`id` →
  `grant_id` on `meho_agents_grant_{list,show,create,elevate}`),
  `_mirror_trigger_id` (`id` → `trigger_id` on `meho_scheduler_list`),
  and `_mirror_run_status` (`state` → `status` on
  `meho_runbook_list_runs` summaries — the filter itself stays `status`
  per §14.6, so the mirror makes the row value a member of the filter's
  own enum).

The native key is never removed (the #1612 precedent keeps both), so a
consumer reading either name keeps working.

## Structured tool results (`structuredContent`, #2774)

`handle_tools_call` (`mcp/handlers.py`) packs every successful result
as a single JSON-serialised `text` content block. When the called
tool's `ToolDefinition` declares an `outputSchema`, the handler's dict
is **additionally** emitted as `structuredContent` — MCP 2025-06-18
§Tools/Output Schema mandates that a declaring tool "MUST return
structured results that conform to this schema", and Claude's frontend
enforces it client-side: before #2774 every declaring tool hard-failed
in Claude Desktop with a bare "Tool execution failed" while the server
logged 200 and wrote its audit row (found by the #2666 Desktop smoke
re-run; the third Anthropic-conformance class after #2644 and #2745).
The text block stays alongside per the spec's backwards-compatibility
recommendation, byte-identical to the structured object. Tools without
an `outputSchema` get no `structuredContent` key — nothing to conform
to, no wire bloat.

Conformance is validated at **emission time in tests, not per-request
in prod**:

* `tests/test_mcp_structured_content.py` — emission rules, schema
  well-formedness (valid Draft 2020-12, `type: object` root), the
  pinned declaring-tool roster, and `to_wire` publishing the schema
  verbatim.
* `tests/test_mcp_output_schema_conformance.py` — every declaring
  tool's **real handler payload** driven through the full dispatcher
  and validated against its declared schema, including the non-happy
  first-class shapes (`awaiting_approval` parked writes,
  `status="unavailable"` previews, the `query_audit` `shape="tree"`
  replay envelope — the drift #2774 actually found, fixed by widening
  that schema to a `oneOf` flat|replay union). It also covers the
  `list_targets` over-threshold page: a JSONFlux summary plus `handle`,
  with no inline `targets` (#3858, see [`targets.md`](targets.md)).

A tool that cannot honestly declare its output shape should drop the
declaration (spec-legal — proven by the ~60 non-declaring tools)
rather than publish a schema its payloads violate. `call_operation` exposes an
`audit_id` only after its dispatcher audit row commits; a null value promises no
audit receipt. This holds for every answer that has a row: a completed call, a
failed call, and a call MEHO refused before it ran (a deny grant, the announce
gate, a rate limit, a target version the catalog does not serve, a refused
approval request; #4669). Its nullable `delivery` is about response shaping, not how much
upstream data a result handle retained: `complete` means the shaped response was
delivered, `unavailable` means execution completed but shaping failed and callers
must inspect audit/status evidence without re-invoking it, and `partial` means
the response was too large to deliver but the `handle` still reaches the caller.

### `call_operation` outcomes and `isError` (#3675)

The `call_operation` `status` enum lists every status the dispatcher can
return at this boundary: `ok`, `error`, `denied`, `awaiting_approval` and
`rate_limited`. It also lists `pending` and `running`, the answers of the
async mode (#3695, see the section on long calls below). `already_resumed`
is not in it: only the human approval path (the approve endpoint resuming
a waiting call) sees it.

`rate_limited` means the caller is over its rate limit or its limit on
operations running at the same time (#3500, see
[`rate-limiting.md`](rate-limiting.md)). The operation did not run. The
envelope carries `extras.retry_after_seconds`; the caller decides when to
try again. MEHO never retries on its own.

`handle_tools_call` sets the MCP `isError` flag from the envelope
`status` for the two tools that return an `OperationResult` envelope,
`call_operation` and `preview_operation`
(`_tool_result_is_error` in `mcp/handlers.py`):

| `status` | `isError` |
|---|---|
| `error`, `denied`, `rate_limited` | `true` |
| `ok`, `awaiting_approval`, anything else (for example `unavailable`) | `false` |

This follows MCP 2025-06-18 §Tools/Error Handling: a failure the tool
caught (API failure, business rule) is a tool execution error, reported
inside a normal JSON-RPC result with `isError: true`. The
`structuredContent` and the text block are sent either way, so the
caller still reads `extras`. A call that waits for approval
(`awaiting_approval`) is a successful hand-off: the request is stored
and audited, so it is not an error and must not be retried. Every other
tool keeps `isError: false`.

The REST route `POST /api/v1/operations/call` does not change: every
dispatcher outcome, `rate_limited` included, is HTTP 200 with the
envelope (#136 / #2110). The back-off signal is
`extras.retry_after_seconds` in the envelope, not an HTTP 429.

Tests: `tests/test_operations_meta_tools.py` (enum lock),
`tests/test_mcp_output_schema_conformance.py`
(`test_call_operation_rate_limited_envelope_conforms`),
`tests/test_mcp_structured_content.py`
(`test_is_error_follows_operation_result_status`) and
`tests/test_api_v1_operations.py`
(`test_post_call_rate_limited_rides_200_envelope`).

### The audit status of `call_operation` and `preview_operation` (#4751)

Some calls fail before any operation runs: the `op_id` or the connector
does not exist, or the params do not match the schema. The dispatcher
writes no operation row for them. So the MCP tool-call row must say that
the call failed. `handle_tools_call` sets the row's `status_code` from
the envelope `status`, with the same rule as `isError`
(`_operation_result_audit` in `mcp/handlers.py`):

| `status` | `extras.error_code` | row `status_code` |
|---|---|---|
| `ok`, `awaiting_approval`, anything else | any | `200` |
| `error`, with an `audit_id` (the call ran) | any | `500` |
| `error`, no `audit_id` | `unknown_op`, `unknown_connector` | `404` |
| `error`, no `audit_id` | any other code | `400` |
| `denied` | any | `403` |
| `rate_limited` | any | `429` |

The row's payload also carries `result_status` (the envelope `status`)
and `error_code` (from `extras.error_code`, when there is one), so a
query can filter on them. A call that did run keeps its own operation
row from the dispatcher; that row still holds the exact result. When
such a call fails (for example the vendor times out), the result carries
the `audit_id` of that row, and the tool-call row says `500`, the same
as the operation row. Every other tool keeps `status_code = 200` when
its handler returns.

The reflex adoption numbers and the working-surface check query (later
in this file) read only `status_code = 200` tool-call rows, so they no
longer count a `call_operation` that failed or was refused. The
retrieval usage numbers do not change: they filter on
`status_code = 200` only for the search tools.

With `AUDIT_WRAPPER_ROWS_RETIRED=true` the cut-over rules in
`audit_cutover.py` decide what is written:

- `500`: no tool-call row. The dispatcher already wrote its operation
  row for the caller.
- `404`, `400` and `429`: no tool-call row (`call_operation` is a
  routine tool, and other 4xx write nothing). The request usage event
  holds the real outcome: `not_found`, `client_error` or
  `rate_limited`.
- `403`: no tool-call row when the dispatcher already wrote its policy
  row for the caller. Otherwise one `policy_decision` row.
- `200`: no tool-call row. A call that ran or waits for approval has
  the dispatcher's own row.

Tests: `tests/test_mcp_call_operation_audit_status.py`.

### Response wire ceiling (#3641)

A completed `call_operation` response is also checked against a wire ceiling,
`RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES` (default 256 KiB). The size is measured
on the real `tools/call` wire by `measure_delivered_wire_bytes`
(`operations/delivery_budget.py`): the encoded text block plus the encoded
`structuredContent` copy, escaping included. It is a byte count, not a token
count. The MCP dispatcher builds the text block with the same
`tool_result_text` function, so the measurement cannot drift from what is sent.

When an `ok` response is over the ceiling, `bound_complete_response` drops the
inline `result` and the extras and keeps the status `ok`:

- `delivery="partial"` when the `handle` can be read back and the receipt with
  the handle still fits. The caller reads the result through `result_query`. A
  handle counts as readable only when its `fetch_more.drill_in.available` is
  true: the reducer also mints handles whose rows were never stored (store
  down, no tenant context, admission limit), and those cannot be read.
- `delivery="unavailable"` otherwise. Only the `audit_id` receipt and the
  remediation text remain; an unreadable handle is dropped too.

`extras.remediation` says the operation already ran and must not be re-invoked,
and `extras.retry` is `false` so a program can see the same rule;
`extras.delivered_bytes` and `extras.delivery_max_bytes` give the numbers. A
completed write is never reported as an error because its response was too
large. Non-`ok` envelopes are not rewritten. The REST `/call` twin shares the
same check; REST sends the payload once, so its real size is always smaller.

The receipt must always reach the caller. Two checks make sure it can:

- The ceiling has a floor, `RESULT_COMPLETE_RESPONSE_WIRE_MIN_BYTES` (2048
  bytes, `settings.py`). `Settings` rejects a smaller
  `RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES` at startup, before any operation
  runs. The floor holds the receipt for an ASCII `op_id` of 256 characters.
- The receipt grows with the `op_id`, and non-ASCII characters cost more
  because the text block escapes them twice. So `call_operation` runs
  `refuse_undeliverable_receipt` before dispatch. It measures the widest
  receipt the `op_id` can produce (`receipt_wire_bytes`: the real `op_id`, the
  widest possible `audit_id`, `duration_ms` and byte counts, same encoders as
  the response). A call whose receipt could not fit the ceiling is refused
  with `status="error"` and `extras.error_code="receipt_too_large"`, and the
  operation never runs. The refusal says so and that calling again is safe.

`bound_complete_response` still measures the final `unavailable` response. Only
a caller that skipped the pre-check could push it over the ceiling; then it is
delivered anyway with a `delivery_receipt_over_ceiling` warning, because
dropping the `audit_id` of a completed write would be worse.

### Reading result handles, and the call receipt

A large `call_operation` result comes back as a `handle`. Three facts
shape what the caller sees and how it reads more:

- **Result field catalog (#3631, #3635).** The handle's `schema_` comes
  from a catalog built over every captured row, not from the first row or
  the sample. It has one property per top-level key, sorted by name. When
  `result_query` runs a query, it builds the same catalog from the stored
  rows and checks each field the query names. A field with mixed kinds,
  objects, lists, only nulls, or integers past 64 bits cannot be used in
  `filter`, `order_by`, `group_by`, or `aggregate`, but `select` still
  returns it. Two cases still count as one kind: nulls next to one
  scalar kind (the field is nullable, not mixed), and whole numbers next
  to decimals, which query as one decimal column as long as every whole
  number is within ±2^53. A flat query returns the stored rows exactly
  as captured.
- **`path` reads (#3633).** A large payload with no single list is kept
  whole as a document record. `result_query` reads part of it with
  `query={"path": "<RFC 6901 JSON Pointer>"}` (`""` is the whole
  document; `~1` means `/` and `~0` means `~`). `path` cannot be combined
  with the other query fields. A text value comes back in parts (#4994):
  `offset` and `limit` count characters, `value` holds the part, and
  `returned_chars`, `total_chars` and `truncated` say where it ends. For
  that reason the `limit` argument has no static maximum in the
  `inputSchema`; the core refuses more than 500 rows or array items.
  The `result_query` `outputSchema` covers both the rows shape and the
  `path` shape.
- **The `audit_id` receipt (#3636).** `call_operation` returns
  `audit_id` only after the dispatch audit row commits (`delivery` is
  explained above, under "Response wire ceiling"). If the operation ran but its result could
  not be shaped, the call stays `ok` with `delivery="unavailable"` and
  the `audit_id`, so the caller knows not to run it again. A failed call
  (#3864) and a refused call (#4669) return the id of their `error` or
  `denied` row the same way, so an agent can link a refusal to its audit
  row without searching the audit log. `audit_id` is `null` only when no
  row was written or the write failed.

The full rules, bounds, and errors are in
[`result-spill.md`](result-spill.md). Tests:
`tests/test_mcp_tool_result_query.py`,
`tests/test_mcp_output_schema_conformance.py`, and
`tests/test_result_reduction_properties.py`.

### Long calls: `call_operation` async mode (#3695)

A long call (a deploy, a multi-step operation that runs for minutes) should
not hold one MCP request open the whole time. `call_operation` takes two
optional arguments for this. They are MCP controls and never reach the
dispatcher:

- **`async: true`** starts the call in the background and answers at once
  with `status="pending"` and `extras.run_id`. It uses the same
  `operation_run` row and background runner as `POST /api/v1/operations/call`
  with `async: true` (see [`async-governed-dispatch.md`](async-governed-dispatch.md)),
  so the policy checks, approvals and audit rows are the same as for a normal
  call.
- **`run_id`** (with the same `connector_id` and `op_id`) checks that run.
  While it runs: `status="pending"` or `"running"`, with
  `extras.started_at` and `duration_ms` counted from the start. When it is
  done: the stored `OperationResult` of the call, with `extras.run_id` added.
  The answer is checked against the wire limit again after the run details
  are added, like a normal call: a result that no longer fits keeps
  `status="ok"` and its `audit_id` and gets `delivery="partial"` or
  `"unavailable"`. A failed or cancelled run gives `status="error"` and `extras.error_code`
  `run_failed` / `run_cancelled`. Checking never runs the call again.

An unknown run, a run of another tenant, or a run of a different
`connector_id` / `op_id` gives the same `-32602` error with
`data.reason="unknown_run"`. Sending `async: true` and `run_id` together
gives `data.reason="async_with_run_id"`. No tool is added: the working
surface stays at 26 tools, and the `outputSchema` status enum gains
`pending` and `running`. Code: `mcp/tools/_call_operation_async.py`. Tests:
`tests/test_mcp_call_operation_async.py`.

### Wrong connector or op id (#4031)

`call_operation` and `preview_operation` look up the op on the named
connector first. When that lookup finds nothing, the error says why and
what to do next:

- **`unknown_op`** — the connector exists, but it has no enabled op with
  this `op_id`. MEHO then looks for the same `op_id` on the other
  connectors the caller can see. `extras.connectors_with_op` lists them
  (sorted, at most 10), and the message names them, for example "This
  operation exists on connector(s) vmware-rest-9.0". When the list is
  empty, the message tells the caller to use `search_operations` on the
  connector. When the call has a target, a connector whose catalog check
  refuses that target is not listed (#5069): for example
  `vmware-rest-9.0` for a vCenter 8.0.x target, for a generic call that
  only its catalog has. `extras.known_op_count` still carries the connector's op
  count. A common cause is the other version of the same product; op ids
  can also be spelled differently between versions.
- **`unknown_connector`** — the `connector_id` matches no connector the
  caller can see: no operation rows and no registered connector class.
  This is the same test `list_operation_groups` and `search_operations`
  use. `extras.connector_id` echoes the id. Before #4031 this case was an
  `unknown_op` with `known_op_count: 0`.

The search for other connectors uses the same tenant rule as
dispatch (`visible_scope_clause` in `operations/_lookup.py`), applied to
each connector: a connector of another tenant is never named, and a
built-in op hidden by the tenant's own connector row is not named
either. The extra lookups run only on this error path
(`operations/_op_miss.py`). Tests: `tests/test_operations_op_miss.py`.

### Close ids for an unknown connector (#4136)

Every unknown-connector error names up to 5 close connector ids, so a
model that guessed a near-miss id (`vmware-rest`, `linux-ssh-1.0`) can
fix its call at once. It does not have to read the whole
`meho_connector_list` first.

- **Which ids.** `close_connector_ids` in `operations/_lookup.py`. An id
  is close when it starts with the given text, or has a close spelling
  (`difflib`, ratio 0.6 or more). Case is ignored. The closest come first.
- **Who sees what.** Only built-in connectors and the caller's own
  tenant, the same rule as `connector_exists`. A connector of another
  tenant is never named. A built-in connector class with no operation
  rows yet is named too: a hand-coded class, or the profiled class that
  startup stamps for a catalog row shipped with a profile (for example
  `mehoauto-rest-0.1.0` before its first ingest). The class that ingest
  registers (an auto-shim, or a profiled class for a row the catalog
  ships no profile for) is not read from the registry: a tenant-scoped
  ingest registers one as well, so that connector counts only through
  its rows, which the tenant rule filters.
- **Where.** The message says "Did you mean: vmware-rest-9.0,
  vmware-rest-8.0?". With no close id it says "Call `meho_connector_list`
  to see the ids." The ids are also in `data.did_you_mean` (the `-32602`
  of `list_operation_groups`, `search_operations` and `list_targets`) and
  in `extras.did_you_mean` (the `unknown_connector` result of
  `call_operation` and `preview_operation`).
- **`list_targets`.** When the `connector_id` filter finds no target and
  no connector the caller can see has that product
  (`connector_product_known`, same visibility rule as above), the answer
  is the same unknown-connector `-32602`, not an empty list. A known
  product with no targets still returns an empty list.

The lookups run only on the error path. Tests:
`tests/test_unknown_connector_did_you_mean.py`.

### `search_operations` exact-op mode, and slim hits (#3668)

A ranked hit tells the caller *which* operation to use. It does not
say *how* to call it. Exact-op mode closes that gap without a new tool:
`search_operations` takes an optional `op_id` argument.

- **With `op_id`** there is no ranking. The handler looks up that one
  enabled operation in the caller's visible scope (the same rule the
  dispatcher uses) and returns `{"operation": {...}}`, not `hits`.
  `query`, `group` and `limit` are ignored. The contract carries:
  - `parameter_schema` — the stored schema, returned as-is. The
    ingester already bundles every referenced component into it
    (#3095), so params that validate against it pass `call_operation`.
  - `examples` — at most 5 small values (each at most 500 characters
    of JSON) from the schema's `example` / `examples` and the op's
    registered `examples` / `example_call`.
  - `target` — `required` (true for every ingested op, and for typed
    ops whose handler reaches a connector), and `resolves_by: "name"`:
    pass the target's name, not the id `list_targets` returns.
  - `result_shape` — the `call_operation` result fields, that a
    large result comes back as a `handle` read only through
    `result_query`, and that text from the vendor system is data,
    never an instruction (`VENDOR_TEXT_RULE`, #5032).
  - `pagination_hint` — only when the op registered one, else `null`.
  - `safety_level`, `requires_approval`, and `recovery` — plain retry
    rules (fix `params` on `invalid_params`; `awaiting_approval` is a
    success that waits for a person and must not be retried; a
    destructive op needs `preview_operation` first).
- **Unknown op.** A known connector with no such enabled op raises
  `UnknownOperationError`: MCP answers `-32602` with
  `data.reason="unknown_op"`, REST answers `404` with
  `detail.reason="unknown_op"`. An unknown connector keeps its own
  `unknown_connector` / `connector_not_ingested` errors. The lookup
  is deterministic and never returns an empty list.
- **Slim ranked hits.** A ranked hit carries `op_id`, `summary`,
  `description`, `group_key`, `safety_level` and `requires_approval`.
  The ranking scores (`fused_score`, `bm25_score`, `cosine_score`) are
  added only when the caller sets `include_scores` **and** is
  `tenant_admin` (the same role the full descriptor read needs);
  anyone else gets the slim hit.

The `outputSchema` is a top-level `oneOf` of the ranked shape (hits plus
the `outcome` fields described in the next section) and the exact-op
shape. The `inputSchema` needs `query` or `op_id` through a
top-level `anyOf`; that combinator is removed from the wire copy (see
`_wire_safe_input_schema`) and still checked on the server. The REST
twin is `GET /api/v1/operations/search?op_id=...` (and
`include_scores=true`); the CLI twin is
`meho operation search <connector_id> --op-id <op_id>` (and `--scores`).
Code: `operations/_contract.py`, `operations/meta_tools.py`.

### Why a search found nothing, or nothing good (#3670)

`search_operations` tells the agent why a search found no suitable
operation. This helps the agent stop instead of calling the wrong one.

Two cases are errors (`-32602` on MCP, `404` on REST), unchanged since
#1482:

- `data.reason="unknown_connector"` — no connector has this id.
- `data.reason="connector_not_ingested"` — the connector exists, but its
  operations are not loaded yet. `data.next_step.verb` says what to run.

Every other result has an `outcome` field:

| `outcome` | Meaning | What the agent should do |
|---|---|---|
| `matched` | The top hit matched the query's words and is clearly ahead. | Check `safety_level` and `requires_approval`, then call it. |
| `ambiguous` | Hits came back, but the top hit matched no query word (it is only close in meaning), or the top two are nearly tied. | Do not call any hit. Narrow the search first. |
| `no_match` | The connector has enabled operations, but none matched. | Do not call anything. Search again with other words. |
| `unknown_group` | The `group` filter names no visible group. | Retry with a group key from `narrowing`. |
| `zero_enabled_operations` | The connector is loaded, but no operation is enabled for this caller. | Do not call it. An operator must enable operations. |

`advisory` explains any outcome other than `matched` in one sentence.
`narrowing` lists next steps, at most `SEARCH_NARROWING_MAX` entries.

- The outcome reads the scores the search already made. Hit order does
  not change. The search always reads at least two hits from the
  retriever (`SEARCH_CLASSIFY_MIN_HITS`), even for `limit=1`, so a tied
  second hit cannot hide. The returned list is then cut to `limit`.
- The near-tie band is `SEARCH_NEAR_TIE_BAND`: one list position at the
  top of one ranking signal.
- The enabled-operation check and the group list use the same tenant
  filter as dispatch, and count enabled rows only. So the text never
  names a disabled operation or another tenant's operation.

Code: `operations/meta_tools.py` (`search_operations`, `_run_search`,
`_classify_hits`). Tests: `tests/test_operations_meta_tools.py`.

## Tool hints: read-only tools (`annotations`, #4202)

A `tools/list` entry can carry MCP tool hints in `annotations`
(MCP 2025-06-18 `ToolAnnotations`). Clients use them to decide whether
to ask the user before a call. For example, VS Code Copilot runs a tool
with `readOnlyHint: true` without asking.

- `ToolDefinition.annotations` takes a `ToolAnnotations` model
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`).
  It forbids unknown keys, so a typo fails at startup.
- A tool that only reads passes `annotations=READ_ONLY`. On the wire this
  is `"annotations": {"readOnlyHint": true}`.
- `ToolDefinition.meta` is the tool's MCP `_meta` object. No tool sets it
  yet.
- `to_wire()` sends `annotations` and `_meta` only when they are set.
  A tool without hints keeps the spec defaults: it may change things and
  may delete things, so the client asks first.
- These working-surface tools carry the read-only hint: `list_targets`,
  `meho_connector_list`, `list_operation_groups`, `search_operations`,
  `preview_operation`, `result_query`, `query_topology`,
  `search_knowledge`, `search_memory`, `meho_broadcast_recent`,
  `meho_broadcast_watch`, the four docs tools, `meho_runbook_list_runs`,
  `meho_runbook_list_templates`, `meho_runbook_show_template` and
  `meho_status`.
- `call_operation` has no hint, because it can run writes. Tools that
  add, change or start things have no hint either.
- Hints are only hints. The spec says a client must not trust them from
  an untrusted server. MEHO's own role, grant and approval checks run on
  every call, whatever a tool declares.

Code: `mcp/registry.py` (`ToolAnnotations`, `READ_ONLY`,
`ToolDefinition.to_wire`). Tests: `tests/test_mcp_surface_conformance.py`
(`READ_ONLY_HINT_TOOLS`, the exact set on the wire) and
`tests/test_mcp_registry.py` (the wire shape).

## Agent-surface scoping (working vs. operator, #3154)

`tools/list` is filtered by **four orthogonal gates**, all AND-composed
in `all_tools_for` (`mcp/registry.py`) and re-checked at `tools/call` in
`handle_tools_call` (`mcp/handlers.py`):

1. **role** — the session's `TenantRole` must meet the tool's
   `required_role` (`role_at_least`).
2. **capability** — a tool declaring `required_capability` (only the
   `meho-docs` docs tools today) is absent unless the tenant provisioned
   that key (`capability_satisfied`).
3. **surface** — every tool carries an explicit
   `ToolSurface` (`working` | `operator`); `surface_visible` admits the
   working surface to everyone and the operator surface only to an
   **elevated** session (Initiative #3153).
4. **add-on family** — a tool declaring `required_addon_family` (only
   the `automation` family today, Task #3029) is absent unless a
   **paired, contract-healthy** add-on advertises a `meta_tool_family`
   capability of that name for the tenant (`addon_family_active`). Unlike
   the static `required_capability` claim, this axis tracks **live
   pairing state**: the listing handler resolves the tenant's active
   families once per request from
   `AddonCapabilityService.active_meta_tool_families` (the add-on
   capability plane, `addon-pairing.md`) and passes them to
   `all_tools_for`; the tool appears only while the add-on is paired and
   healthy and disappears cleanly on unpair. The resolution is guarded by
   `has_addon_gated_tools()` so a registry with no add-on-gated tool pays
   no DB round-trip, and at `tools/call` it runs only for a tool that
   actually declares the gate — the hot path stays DB-free.

All four are *true-absence* gates: a tool a session may not use never
appears in its `tools/list`, and naming it directly at `tools/call` is
rejected 403-class (INVALID_PARAMS, handler never runs) — the listing
filter is enforced, not cosmetic.

### The two surfaces

`ToolSurface` is a **required, no-default** field on `ToolDefinition`
(like `feature`): a registration that omits it raises a pydantic
`ValidationError` at construction — i.e. at module-import time, so an
unclassified tool fails app boot / test collection loudly rather than
defaulting into a silent bucket. Every registered tool is classified
explicitly.

* **`working`** — the default narrow-waist surface every session lists,
  honouring CLAUDE.md postulate 5: status, discovery, execution,
  knowledge, memory, docs (capability-gated), the broadcast trio,
  target/topology **reads**, and the runbook **run** family.
* **`operator`** — the governance / lifecycle planes, listed only after
  `mcp:admin` elevation: connector lifecycle + ingest, agents /
  principals / grants, scheduler, sensors, runbook **template
  authoring**, broadcast overrides, topology **mutations**, target
  registration, doc-collection create/delete, memory promotion,
  approvals **read**, and audit query.

## The current tenant of a session (#5239)

One login can list several tenants (the `meho_tenants` claim). An MCP
session works in **one** tenant, fixed at the start by the `Meho-Tenant`
request header (a tenant name or UUID). To switch, the client changes the
setting and starts a new session. There is no tool to switch tenants, so
the working surface stays at its size.

- `verify_mcp_jwt` (`mcp/auth.py`) reads the header and calls
  `verify_jwt_for_audience(..., requested_tenant=..., allow_unchosen=True)`.
  The rules are the ones of REST (`auth/current_tenant.py`): a header for a
  tenant the person is not in is `403 tenant_not_member`; a platform admin
  may name any existing tenant (`403 tenant_not_found` for an unknown one).
  These refusals are 403, not 401, so they carry no `WWW-Authenticate`.
- A login with several tenants and **no** header gets an `Operator` with
  `tenant_chosen=False`. `mcp/tenant_gate.py` then allows only
  `initialize`, `ping`, `notifications/*`, `tools/list` and
  `tools/call` of `meho_status`. Every other request gets a JSON-RPC
  `-32600` error whose message starts with `tenant_not_chosen:` and names
  the person's tenants. The check is an allow list: a new method or tool
  is refused until it is added there.
- The allowed requests do no tenant work. The bind step
  (`verify_mcp_jwt_and_bind`) binds nothing for such a session, so the
  chassis audit middleware writes no row and the usage collector no
  event. `initialize` skips the per-tenant session cap and the tenant
  preamble; its `instructions` text is the `tenant_not_chosen` message
  plus how to pick a tenant. `tools/call` of `meho_status` writes no
  wrapper row and reads no broadcast setting. The operator's tenant
  fields hold only a placeholder (the first entry) and are never
  recorded.
- `tools/list` for such a session lists only `meho_status`.
- `meho_status` (and `GET /api/v1/health`) then answers with
  `operator.tenant_id` and `operator.tenant_role` set to `null`,
  `operator.tenants` (each tenant's `tenant_id`, `name`, `tenant_role`,
  `current`), `vault.detail = "tenant_not_chosen"` and the database
  state. It runs no tenant work (no credential check, no add-on
  pairings, no tool comparison). With a current tenant, `meho_status`
  answers as before and also lists `operator.tenants`, with `current`
  set on the current one.
- MEHO keeps no MCP session store (the session id only correlates audit
  rows), so nothing is kept per session that could mix tenants. Result
  links are stored per tenant and per person
  (`connectors/result_handle_store.py`), so a link made in tenant A is a
  miss while the session works in tenant B.

Tests: `tests/test_mcp_tenant_gate.py`, `tests/test_auth_current_tenant.py`.

## Dual-surface tool inventory

This is the **authoritative enumeration** of the MCP agent surface —
every registered tool, its surface, its gating claim, and a one-line
summary. It is the single home for "what does an MCP session actually
expose", replacing the per-Task partial listings that were scattered
across this doc. The membership is machine-pinned two ways, so this
table can never silently drift from the code:

* `tests/test_mcp_surface_filter.py` (#3154) pins the working / operator
  partition as frozensets off `all_tools_for` (registry state) and
  proves it is exhaustive + disjoint.
* `tests/test_mcp_surface_conformance.py` (#3157) pins the exact
  **sorted** `tools/list` wire listing per claim shape — driven through
  `handle_tools_list` and the real `POST /mcp` route — so a
  reclassification or an un-pinned addition fails CI at the listing path
  a client actually observes.

Counts: **26 working + 54 operator + 1 pairing-gated automation =
81 registered tools**, plus the **3 human-only verbs** (below) that
carry no MCP registration under any claim set. Four of the 26 working
tools (`ask_docs`, `search_docs`, `read_docs`, `list_doc_collections`)
appear only when the tenant has the `meho-docs` capability; a session
without it lists 22. The 26 working and 54
operator counts are the **unpaired baseline** — a session with every
capability provisioned but no add-on paired — so they stay byte-identical
to a build that never carried the automation family; the single
pairing-gated tool (`meho_automation_list`, #3029) is absent from that
baseline and joins the working surface only while its add-on is paired
and contract-healthy.

**Name authority ↔ consumer docs (#3150).** The tool *names* in this
inventory are the authority the consumer-facing docs/skills
name-conformance work (#3150) validates against: its guard
(`scripts/ci/check_consumer_tool_names.py`, run by the
`consumer-tool-name-check` workflow) derives the same registered set
from the tool source and fails CI when a consumer doc, onboarding
template, or plugin skill references an MCP tool by a name that is not
registered (e.g. a bare `broadcast_announce` instead of
`meho_broadcast_announce`, or a nonexistent `search_connectors` /
`result_aggregate`). The two are reciprocal, not divergent — names are
defined here; the guard enforces them there. This inventory owns *what
is exposed*; #3150 owns *how consumer docs spell it*.

Gating recap — a tool appears in a session's `tools/list` iff **role**
`≥` its minimum **AND** the tenant holds any required **capability**
**AND** the session clears the **surface** gate (working = always;
operator = `mcp:admin`-elevated) **AND** any required **add-on family**
is active (a paired, contract-healthy add-on advertises it). The "Role"
and "Extra gate" columns below are the per-tool half; the surface's scope
requirement is stated in each table heading. The "Extra gate" column
carries both the `meho-docs` capability keys and the `automation (paired)`
add-on-family gate.

### Working surface (default — every session, no elevation)

| Tool | Role | Extra gate | What it does |
|---|---|---|---|
| `add_to_knowledge` | operator | — | Write a distilled entry to the tenant knowledge base. |
| `add_to_memory` | operator | — | Save a scoped memory entry (user / tenant / target). |
| `ask_docs` | operator | `meho-docs` | Synthesized, cited answer over a vendor-document collection. |
| `call_operation` | operator | — | Invoke a resolved connector operation against a target. |
| `list_doc_collections` | operator | `meho-docs` | List the documentation collections this session may search. |
| `list_operation_groups` | operator | — | List a connector's enabled operation groups. |
| `list_targets` | operator | — | List the tenant's accessible infrastructure targets. |
| `meho_broadcast_announce` | operator | — | CLI: `meho broadcast announce <activity>` publishes through the shared durable publisher. |
| `meho_broadcast_recent` | operator | — | CLI: `meho broadcast recent` reads stored agent announcements through the shared archive reader (operation, approval and check events are on the SSE feed and in `query_audit`). |
| `meho_broadcast_watch` | operator | — | CLI: `meho broadcast watch` tails the authenticated `/api/v1/feed` SSE route. |
| `meho_connector_list` | operator | — | List connectors visible to the tenant (plus built-in / global). |
| `meho_runbook_abort` | operator | — | Abort an in-progress runbook run. |
| `meho_runbook_list_runs` | operator | — | List runbook runs (operators see only their own). |
| `meho_runbook_list_templates` | operator | — | List runbook templates in the tenant. |
| `meho_runbook_next` | operator | — | Advance one step of an in-progress run (opacity contract). |
| `meho_runbook_show_template` | operator | — | Read a runbook template's full body. |
| `meho_runbook_start` | operator | — | Start a new runbook run (self-assigned). |
| `meho_status` | read_only | — | Return caller identity + backplane dependency status, plus `granted_scopes` and `surface_summary` (#3666); since #4830 also the database migration revision (`db.revision` / `db.head`), the `/ready` checks (`readiness`), the approval settings in force (`approvals`) and the gated features (`features`). Since #5239 also the person's tenants with the role in each (`operator.tenants`); it is the one tool a session without a current tenant may call. |
| `preview_operation` | operator | — | Preview an operation without running it. |
| `query_topology` | operator | — | Query the topology graph (dependents, blast radius, timeline). |
| `read_docs` | operator | `meho-docs` | Read the text around a docs hit, its whole page or its section, through the hit's `read_handle` (#3948). |
| `result_query` | operator | — | Read rows back from a JSONFlux result handle: page a window, or run a bounded structured query (filter / project / group / aggregate) server-side. |
| `search_docs` | operator | `meho-docs` | Search a vendor-document collection for an authoritative fact. |
| `search_knowledge` | operator | — | Search the tenant knowledge base. |
| `search_memory` | operator | — | Search the operator's accessible memories. |
| `search_operations` | operator | — | Hybrid BM25 + cosine search over a connector's operations. With `op_id` (exact-op mode) it returns that one operation's call contract instead: the stored `parameter_schema`, small examples, the target-by-name rule, the result shape and retry rules (#3668). |

### Operator surface (`mcp:admin` elevation required)

Every tool below is listed / callable **only** on an `mcp:admin`-elevated
session (on top of the Role + Extra-gate columns).

| Tool | Role | Extra gate | What it does |
|---|---|---|---|
| `create_doc_collections` | tenant_admin | `meho-docs` | Register a new documentation collection for search routing. |
| `delete_doc_collections` | tenant_admin | `meho-docs` | Deregister a disabled, tenant-owned documentation collection. |
| `update_doc_collections` | tenant_admin | `meho-docs` | Repoint an existing collection's backend ref (and other mutable fields) in place; a global row also needs `platform_admin`. |
| `meho_agent_principals_list` | operator | — | List agent principals registered for the tenant. |
| `meho_agent_principals_register` | tenant_admin | — | Register a new agent principal. |
| `meho_agent_principals_revoke` | tenant_admin | — | Revoke an agent principal (kill switch). |
| `meho_agents_create` | tenant_admin | — | Create an agent definition. |
| `meho_agents_delete` | tenant_admin | — | Delete an agent definition by name. |
| `meho_agents_edit` | tenant_admin | — | Apply a partial update to an agent definition. |
| `meho_agents_grant_create` | tenant_admin | — | Grant a permission to an agent principal. |
| `meho_agents_grant_list` | tenant_admin | — | List agent permission grants. |
| `meho_agents_grant_revoke` | tenant_admin | — | Revoke a permission grant by id. |
| `meho_agents_grant_show` | tenant_admin | — | Fetch one agent permission grant by id. |
| `meho_agents_list` | operator | — | List agent definitions. |
| `meho_agents_list_runs` | operator | — | List the tenant's agent runs, newest first. |
| `meho_agents_run` | operator | — | Run a named agent (sync or async). |
| `meho_agents_run_status` | operator | — | Poll an agent run's durable status by `run_id`. |
| `meho_agents_show` | operator | — | Fetch one agent definition by name. |
| `meho_approvals_get` | operator | — | Inspect a single approval request (read-only). |
| `meho_approvals_list` | operator | — | List approval requests for the tenant (read-only). |
| `meho_audit_replay` | tenant_admin | — | Reconstruct another session's full operation trace (forensic). |
| `meho_broadcast_overrides_list` | tenant_admin | — | List broadcast-detail override rules. |
| `meho_broadcast_overrides_remove` | tenant_admin | — | Delete a broadcast-detail override rule by id. |
| `meho_broadcast_overrides_set` | tenant_admin | — | Create a broadcast-detail override rule. |
| `meho_connector_delete` | tenant_admin | — | Delete a connector's ingested rows / auto-registered shim. |
| `meho_connector_disable` | tenant_admin | — | Disable every group of a connector (regression roll-back). |
| `meho_connector_edit_group` | tenant_admin | — | Edit an operation group's `when_to_use` prose or name. |
| `meho_connector_edit_op` | tenant_admin | — | Edit one operation's per-op overrides. |
| `meho_connector_enable` | tenant_admin | — | Enable every group of a connector. |
| `meho_connector_enable_reads` | tenant_admin | — | Bulk-enable a connector's read-class operations. |
| `meho_connector_ingest` | tenant_admin | — | Ingest one or more OpenAPI specs into a connector. |
| `meho_connector_ingest_status` | operator | — | Poll an async connector-ingest job's status. |
| `meho_connector_review` | operator | — | Get a connector's full review payload (groups + per-op flags). |
| `meho_memory_promote` | tenant_admin | — | Promote a memory to a strictly broader scope. |
| `meho_runbook_deprecate_template` | tenant_admin | — | Mark a published runbook version deprecated. |
| `meho_runbook_discard_template` | tenant_admin | — | Delete an unpublished draft template. |
| `meho_runbook_draft_template` | tenant_admin | — | Create the first draft of a new template. |
| `meho_runbook_edit_template` | tenant_admin | — | Edit a template (in-place for drafts, fork-on-published). |
| `meho_runbook_publish_template` | tenant_admin | — | Publish a draft template. |
| `meho_runbook_reassign` | tenant_admin | — | Reassign an in-progress run to another operator. |
| `meho_scheduler_cancel` | tenant_admin | — | Cancel a scheduled trigger by id. |
| `meho_scheduler_create` | tenant_admin | — | Create a scheduled trigger (cron / one-off / event). |
| `meho_scheduler_list` | operator | — | List scheduled triggers. |
| `meho_sensor_create` | tenant_admin | — | Create a sensor (op + assertion + cadence + severity). |
| `meho_sensor_delete` | tenant_admin | — | Hard-delete a sensor by id. |
| `meho_sensor_list` | operator | — | List sensors with their latest-result projection. |
| `meho_sensor_results` | operator | — | Per-tick evidence trend history for one sensor. |
| `meho_targets_register` | tenant_admin | — | Register a new target in the tenant. |
| `meho_topology_annotate` | tenant_admin | — | Assert a curated graph edge auto-discovery can't infer. |
| `meho_topology_bulk_import` | tenant_admin | — | Batch-assert curated graph edges in one atomic pass. |
| `meho_topology_create_node` | tenant_admin | — | Manually seed a graph node. |
| `meho_topology_delete_node` | tenant_admin | — | Hard-delete a manually-seeded graph node. |
| `meho_topology_unannotate` | tenant_admin | — | Delete a curated graph edge (re-promotes any superseded auto edge). |
| `query_audit` | operator | — | Query the audit log for forensic reconstruction. |

### Pairing-gated — automation family (paired add-on required, #3029)

The first surface gated on **add-on pairing state** rather than a static
tenant capability (Initiative #2900). The family is on the working
surface (no `mcp:admin` elevation) but absent from every session's
listing until a paired, contract-healthy add-on advertises the
`automation` `meta_tool_family` capability; it disappears cleanly on
unpair. The `automation` identifier is a **family** name — a paired
add-on's own entity identifiers (blueprint names, workflow names) stay
data in results, never tool names (CLAUDE.md postulate 5). The family is
seeded with one discovery tool and grows behind the same gate. CLI twin:
`meho automation list`; REST twin: `GET /api/v1/automation`; console:
`/ui/automation`.

| Tool | Role | Extra gate | What it does |
|---|---|---|---|
| `meho_automation_list` | read_only | `automation` (paired) | List the paired automation add-on(s) and the surface each advertises (families, panels, event kinds) with live health. |

### Human-only (no MCP path under any claim set)

Three decision verbs are **de-registered** from the MCP surface entirely
(#3155) — absent from `tools/list` for every role / capability /
elevation, and a direct `tools/call` is denied *before* the registry
lookup with a remediation naming the console / CLI path. They are not
"operator-surface tools you can elevate into"; they have no MCP
registration at all.

| Verb | Human path |
|---|---|
| `meho_approvals_approve` | operator console approvals queue, or `meho approvals approve <request-id>` |
| `meho_approvals_reject` | operator console approvals queue, or `meho approvals reject <request-id> --reason <text>` |
| `meho_agents_grant_elevate` | operator console, or `meho agent grant elevate …` |

The map + remediation strings live in `mcp/human_only.py`; the
registry-absence + wire-behaviour + static re-add guards are in
`tests/test_mcp_human_only_surface.py`.

### Elevation: the `mcp:admin` scope

Elevation is a **per-session, explicit opt-in**, modelled as an OAuth 2.0
scope rather than a tenant capability. `Operator.scopes` (a `frozenset`)
is lifted from the standard `scope` claim (RFC 9068 §2.2.3, space-
delimited; configurable via `JWT_SCOPES_CLAIM_NAME`, default `scope`) by
`_extract_scopes` in `auth/jwt.py`, fail-closed to the empty set. A
session whose set carries `MCP_ADMIN_SCOPE` (`"mcp:admin"`, a
`mcp/registry.py` constant) additionally lists the operator planes; every
other session sees only the working surface.

The scope shape is deliberate: `capabilities` are tenant-wide provisioned
add-ons (shared by every session of the tenant) and `platform_admin` is a
standing cross-tenant flag, but elevation must be requested per session —
the client asks for it via `MEHO_MCP_SCOPES` (#3156) and the realm mints
it into the token's `scope` claim. Modelling elevation as a standing role
or capability would re-open the self-approval hole the initiative closes:
a session must *choose* to hold the operator planes, not carry them by
default on the strength of its role.

The surface gate **AND-composes** with the capability gate: an elevated
session that has *not* provisioned `meho-docs` still does not see the docs
tools (working or operator: the four working docs tools plus the three
operator-surface doc-collection lifecycle tools all stay hidden). The
gates are independent axes.

### Blast radius

The surface filter lives entirely in `all_tools_for` (the only
`tools/list` feeder) and the `handle_tools_call` re-check. The
hosted-agent toolset bridge (`agent/toolset.py`, its own
`_META_TOOL_CATALOG`), the CLI, and the REST routes do **not** consult
`all_tools_for`, so operators keep the full surface on those fronts —
only the MCP agent listing narrows. The human-only approval/grant-elevate
verbs (`meho_approvals_approve`, `meho_approvals_reject`,
`meho_agents_grant_elevate`) carry **no MCP registration under any
surface** — sibling #3155 (landed via #3159) de-registered them, so they
never reach the surface gate to be classified. What remains is the
remediation map in `human_only.py` and the pre-registry human-only denial
branch in `handle_tools_call`, which returns a 404 naming the console / CLI
path *before* the registry lookup and surface gate ever run.

### Verifying the split against real usage (pre-cutover audit)

The cutover's safety claim is: **narrowing the default listing to the
working surface loses nothing agents actually used.** That is falsifiable
directly from `audit_log`, because every `tools/call` writes one
synchronous, session-tagged row (`method='MCP'`,
`path='/mcp/tools/call/<tool_name>'`, `agent_session_id` set when the
client negotiated an `Mcp-Session-Id`). The check is: over a trailing
window, is the **set of MCP-session-invoked tool names** a subset of the
26-name working surface? Any name outside it is an operator-plane tool a
real session reached — an exception to record and disposition before
cutover.

The query reuses the #3134 reflex-KPI seam conventions — the
`MCP_TOOL_PATH_PREFIX = '/mcp/tools/call/'` filter, the
`agent_session_id IS NOT NULL` "agent surface" split, and successful
rows only (`status_code = 200`). The working-surface allowlist it checks
against is the same 26 names the conformance suite pins, so the SQL and
the CI pin cannot drift.

Authoritative SQL (Postgres, per tenant, trailing 30 days):

```sql
WITH mcp_calls AS (
  SELECT DISTINCT split_part(path, '/mcp/tools/call/', 2) AS tool_name
  FROM audit_log
  WHERE method = 'MCP'
    AND path LIKE '/mcp/tools/call/%'
    AND agent_session_id IS NOT NULL          -- negotiated MCP sessions only
    AND status_code = 200                     -- acted, not a rejected attempt
    AND occurred_at >= now() - interval '30 days'
    -- AND tenant_id = '<tenant-uuid>'         -- scope to one tenant if desired
)
SELECT tool_name
FROM mcp_calls
WHERE tool_name NOT IN (
  'add_to_knowledge','add_to_memory','ask_docs','call_operation',
  'list_doc_collections','list_operation_groups','list_targets',
  'meho_broadcast_announce','meho_broadcast_recent','meho_broadcast_watch',
  'meho_connector_list','meho_runbook_abort','meho_runbook_list_runs',
  'meho_runbook_list_templates','meho_runbook_next','meho_runbook_show_template',
  'meho_runbook_start','meho_status','preview_operation','query_topology',
  'read_docs','result_query','search_docs','search_knowledge','search_memory',
  'search_operations'
)
ORDER BY tool_name;
```

**Zero rows returned ⇒ the invoked-tool set ⊆ working surface** — the
cutover loses nothing. Any returned row is an operator-plane tool a real
agent session invoked pre-cutover; record it and its disposition (it now
requires `mcp:admin`).

Operator alternative via the CLI (no direct DB access needed) — list the
window's MCP tool-call rows and reduce to the distinct tool names, then
eyeball against the working table above:

```bash
meho audit query --since 30d --json \
  | jq -r '.rows[] | select(.method == "MCP" and (.path | startswith("/mcp/tools/call/")))
           | .path | sub("^/mcp/tools/call/"; "")' \
  | sort -u
```

The recorded pre-cutover run for this deployment is on
[issue #3157](https://github.com/evoila/meho/issues/3157) (Initiative
#3153): what ran locally vs. what is operator-run against the live
`audit_log`, and the disposition of any exception.

## Admin registry writes route through the dispatcher

The `meho_*` admin namespace holds the `tenant_admin` write tools
(`op_class="write"`) — outside CLAUDE.md's ~17-tool daily agent surface
by design. A subset of them govern **registry** writes (create a graph
node, register an agent principal, register a target). Where the write
must be **approvable** — a human `tenant_admin` executes immediately
while an agent-principal call parks for review — the MCP front does not
call its service directly; it routes through
`meho_backplane.operations.dispatcher.dispatch` so the single G11.2
policy gate (`operations/_validate.py`) runs per call. The write is
modelled as a **targetless typed op on a synthetic connector** (no
vendor connector backs it), registered with `safety_level="caution"` +
`requires_approval=False` — the dial that parks agent principals only
(`caution` AGENT verdict floor → `needs-approval`) while a human rides
the default-allow immediate branch. `requires_approval=True` would park
humans too; `safety_level="dangerous"` would deny agents rather than
park them.

Five synthetic connectors follow this mould (natural key
`(product, "1.x", impl_id)` → wire `connector_id`):

| Wire `connector_id` | Op | MCP front | Since |
|---|---|---|---|
| `secret-broker-1.x` | `secret.move` | (dispatch-only) | #1577 |
| `secret-broker-1.x` | `secret.handoff.export` / `secret.handoff.import` | (dispatch-only; `dangerous` + `requires_approval`, a second person approves on each instance) | #5228 |
| `topology-graph-1.x` | `topology.create_node` / `.annotate` / … | `meho_topology_create_node` / `_annotate` / … | #2537 |
| `targets-registry-1.x` | `targets.register` | `meho_targets_register` | #2861 |
| `targets-registry-1.x` | `targets.remove` | (dispatch-only; `dangerous` + `requires_approval`, a second person approves) | #4623 |
| `audit-integrity-1.x` | `audit.loose_rows` | (dispatch-only; a `safe` read a Sensor pins) | #4670 |
| `migration-assess-1.x` | `migration.inventory.csv` | (dispatch-only; a `safe` read of stored or inline inventories) | #5099 |

`meho_targets_register` (#2861,
`mcp/tools/targets_register.py` + `connectors/targets/`) closes the last
CRUD-shaped registry with no agent-surface write path: it reuses the
`create_target` service that REST `POST /api/v1/targets` calls, so the
product-token check, the `secret_ref` tenant-scope guard, the SSRF guard
on `host`/`fqdn`, and the JWT-derived `tenant_id` all apply unchanged —
none are re-implemented. `tenant_id` always comes from the caller
identity; `additionalProperties: false` rejects a smuggled `tenant_id`
and the server-managed `fingerprint` at the schema layer. Its
`inputSchema` mirrors `TargetCreate` field-for-field (shared with the
typed op's `parameter_schema` so the two validation layers cannot
drift), and its `outputSchema` is the parked-shape `oneOf` union
(`with_parked_shape`) so both the executed `{target_id, name, product,
host, tenant_id}` payload and the `awaiting_approval` envelope conform
(#2774). The dispatch shim flattens the service's `HTTPException` and the
model's `ValidationError` to `ValueError` at its boundary so the
dispatcher's `connector_error` maps them to `-32602` with a clean
message.

`targets.remove` (#4623) is the opposite write and does not follow the
`caution` dial above. Like `DELETE /api/v1/targets/{name}`, it needs the
`tenant_admin` role; after approval, the approver's role counts. It is
`dangerous` with `requires_approval=True`, and
its name ends in `.remove`, so the rules for deletes apply: a person's call
waits until a second person approves it, an agent login is refused, a
service principal waits for a person and cannot get a standing grant for
it, and only in sandbox mode can a delete grant made in the web console let
a service principal run it at once. It is reached through
`call_operation` only; there is no `meho_*` tool for it. See
[`targets.md`](targets.md#removing-a-target-targetsremove).

## Audit URI redaction for query-bearing resources

The `resources/read` dispatcher
(`handle_resources_read` in `mcp/handlers.py`) records the concrete
read URI in the audit row's `path` (`/mcp/resources/read/<uri>`) and
`payload.uri`. For most resources the URI variable is an opaque
identifier (a kb slug, a docs chunk id, a tenant UUID), so persisting
it is harmless and useful for forensic queries.

`meho://retrieve/{query}` is the exception: its variable is a
free-form retrieval query that leaks operator intent, so the raw query
must not land in the audit trail. The resource template opts into
`audit_redact_uri=True` (a field on
`ResourceTemplateDefinition`); when set, the dispatcher substitutes a
query-stripped sentinel — the template prefix up to the first `{var}`
plus `<redacted>`, i.e. `meho://retrieve/<redacted>` — for both the
audit `path` and `payload.uri`. The substitution runs only after the
URI matches a registered template, so an unmatched URI (404) still
records the attempted value.

Correlatability is preserved through the `audit_*` contextvar
convention: the retrieve handler binds `audit_query_hash` (the
SHA-256 of the decoded query, byte-for-byte identical to the
`POST /api/v1/retrieve` hash) plus `audit_hit_count` before/after the
retrieval call. `write_mcp_audit_row` merges those into the row's
`payload`, so the persisted row is fully attributable (tenant,
operator, `query_hash`, `hit_count`) without carrying the query text.
This mirrors the HTTP route's privacy posture documented in
[`retrieval.md`](retrieval.md).

**Tool arguments kept off the broadcast feed (#3948).** A `tools/call`
whose broadcast detail is `full` copies the raw tool arguments into the
event every co-tenant feed subscriber reads (the audit row itself stores
only `params_hash`). A tool whose argument is as sensitive as data the
caller alone is entitled to lists it in `ToolDefinition.broadcast_omit_args`;
`handle_tools_call` leaves those keys out of the broadcast params (and the
override-rule scope matching). `read_docs` uses it for `read_handle` and
`cursor`, which carry a few words of a docs hit. Empty by default, and
dropped from the wire shape like the other MEHO-internal fields.

The redaction helper is `redacted_audit_uri(template)` in
`mcp/registry.py`. New query-bearing resources reuse the same flag +
contextvar pattern rather than special-casing the dispatcher per
resource.

## Empty optional text on read tools (#4110)

Small models often send an unused filter as empty text, for example
`list_targets` with `connector_id: ""`. A filter on empty text matches
nothing, so the tool used to answer with an empty list and no warning.

`handle_tools_call` (`mcp/handlers.py`) now drops these values for read
tools (`op_class == "read"`) just before the `inputSchema` check:

* A top-level argument whose value is empty text, or only spaces, is
  removed, so the handler sees it as "not given".
* A key in the schema's top-level `required` list is kept, so an empty
  required field is still refused with `-32602`. A key that a
  conditional rule (`anyOf` / `if`-`then`) needs is removed and then
  fails the schema check as missing, for example `search_operations`
  with only `query: ""`.
* Write tools, the REST API, and the CLI are not changed.
* The audit row's `params_hash` is still computed over the arguments
  the client sent.

## Request-size bounds

The `/mcp` transport bounds every request body at
`_MAX_REQUEST_BODY_BYTES` (1 MiB, `mcp/server.py`). The dispatch
pipeline reads the body through `_read_body_capped` rather than
`request.body()`: an over-cap declared `Content-Length` rejects before
any byte is read, and bodies without a trustworthy declared length
(chunked transfer) are consumed incrementally via `request.stream()`
and rejected as soon as the running total crosses the cap. Both arms
return HTTP 413 with a JSON-RPC `INVALID_REQUEST` envelope
(`id: null` — the body was never parsed).

Below the transport cap, free-text tool inputs carry `maxLength` caps
in their `inputSchema` so oversized strings are refused by the
dispatcher's JSON-Schema gate (`-32602`) before they reach the
retrieval substrate's tsvector + embedding indexing:

* `search_memory.query` / `search_knowledge.query` — 256 chars
  (`_MAX_QUERY_CHARS`), the same class as the other tool slices'
  free-text caps.
* `add_to_memory.body` (and its deprecated `content` alias) /
  `add_to_knowledge.body` — 64 KiB (`_MAX_BODY_CHARS`), aligned with
  the operator console's `BODY_MAX_LENGTH` so a body writable through
  the UI is writable through MCP and vice versa.

The transport cap is deliberately sized with headroom above the
largest legitimate tool payload (64 KiB body + envelope + JSON
escaping) so the schema-level caps, not the transport, are the
operative limit for well-formed calls.

## Extra arguments on read tools with no arguments (#4137)

Small models often send arguments to a tool that takes none, for
example `meho_status` with `{"input": {}}` or `{"": ""}`. The schema
(`properties: {}`, `additionalProperties: false`) used to refuse each
call with `-32602`, and the model had to try again.

`handle_tools_call` (`mcp/handlers.py`) now replaces the arguments with
`{}` just before the `inputSchema` check when both are true:

* the tool is a read tool (`op_class == "read"`), and
* its `inputSchema` has no `properties`.

Today this covers `meho_status`, `meho_automation_list`, and
`meho_agents_list`. Tools that take arguments, and every write tool,
still refuse an unknown argument with `-32602`: a wrong argument name
there can mean a wrong answer. The tool schemas, the REST API, and the
CLI are not changed. The audit row's `params_hash` is still computed
over the arguments the client sent.

## Dependencies

* `structlog` — every MCP log line goes through
  `_log = structlog.get_logger()` at module scope, sharing
  contextvar-bound fields (`operator_sub`, request-id, MCP session id
  when present) with the rest of the request scope.
* `pydantic` v2 — the wire schemas (`JsonRpcRequest`,
  `InitializeRequest`, `InitializeResponse`, `ServerCapabilities`)
  are frozen `BaseModel`s with `extra="allow"` on the envelopes per
  MCP's forward-extensibility contract.
* `fastapi` — the router is mounted from `mcp/server.py::router`;
  the JWT verification + operator binding runs as a `Depends(...)`
  on `mcp_dispatch`.

## Known issues

* The MCP `initialize` flow currently does not negotiate
  capabilities (`tools`, `resources`) — they are advertised
  unconditionally based on build-time registry state. A client
  declining a capability on the request side has no effect; the
  capability still appears in the response. Tracking this is
  appropriate scope for a future Task once multi-version
  negotiation lands.
* `_validate_protocol_version_header` accepts an absent
  `MCP-Protocol-Version` header on post-handshake calls as
  transitional lenience. The strict-mode contract (header required,
  missing → HTTP 400) is gated behind a future settings flag whose
  rollout requires consumer migration ahead.
* **A newly-shipped tool is invisible to an already-connected
  client until it re-initializes.** The registry is populated once at
  startup (`eager_import_mcp_modules`) and never mutates at runtime, so
  `initialize` advertises `tools.listChanged: false` and the server
  never emits `notifications/tools/list_changed`. `handle_tools_list`
  builds the response fresh per request (`all_tools_for(operator)` →
  `to_wire()`, RBAC-filtered — no `lru_cache`/`@cache` anywhere in
  `mcp/`), so the staleness is entirely client-side: a client that
  cached the `tools/list` from its `initialize` handshake has no signal
  to refetch until it re-initializes (a transport reconnect on the same
  session is not enough). This is by design for a per-process-immutable
  registry; the operator-facing remediation (re-initialize the client;
  also confirm the upgrade actually replaced the backplane process) is
  in [`docs/cross-repo/mcp-client-setup.md` § Newly-shipped tools don't
  appear after a backplane upgrade](../cross-repo/mcp-client-setup.md#newly-shipped-tools-dont-appear-after-a-backplane-upgrade).

## References

* MCP spec (2025-06-18) §Initialization:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
* MCP spec (2025-06-18) §Streamable HTTP / Protocol Version Header:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
* G0.14-T6 (#1147) — `mcp_session_id_capture` field on
  `HealthResponse` (the precedent G0.14-T13 mirrors).
* G0.14-T7 (#1148) — `/ready` `features` block (the home for
  feature-gate visibility).
* G0.14-T13 (#1202) — protocol-version mismatch observability
  (this section).
* G0.22-T7 (#1612) — runbook tool-name + `template_slug`
  canonicalisation; deprecated flat aliases + `slug` input alias
  removed in v0.15.0 by #1625 (deadline deferred once from v0.14.0 by
  #1702).
* MCP spec (2025-06-18) §Tools (Tool object shape — no first-class
  deprecation field):
  https://modelcontextprotocol.io/specification/2025-06-18/server/tools
* v0.6.0 release-body amendment — PR #1159, G0.13-T6 #1136.
