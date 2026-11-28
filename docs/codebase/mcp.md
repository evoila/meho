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

## Session start row (`AUDIT_CLIENT_IDENTITY`, #6209)

With `AUDIT_CLIENT_IDENTITY=true` (off by default), MEHO writes one
audit row when an MCP session starts. The log can then name the tool,
for example "claude-code 2.1", next to the person.

- **When.** A successful `initialize` request that gets a new
  `Mcp-Session-Id` from MEHO. A failed `initialize`, a notification, or
  a resumed session (the client sent its own session id) writes no row.
  A session with no current tenant (a login with several tenants that
  sent no `Meho-Tenant` header) writes no row either: no audit row may
  name a tenant the person did not choose.
- **What.** `event_class = security_event`, `method = MCP`,
  `path = /mcp/initialize`, `payload.op_id = mcp.session_open`. The
  payload also holds `client_name` and `client_version` (from
  `clientInfo`), `user_agent` (the HTTP header) and
  `"self_declared": true`. Like every audit row with the switch on, it
  also holds the `client` block: the login client (`azp`), the channel
  and a short hash of the sign-in session.
- **Link.** The row's `agent_session_id` is the new session id. Every
  later row of the same session has the same `agent_session_id`.
- **Clean text.** Control and format characters, line and paragraph
  separators (U+2028, U+2029) and lone surrogates are removed, and each
  text is cut to 200 characters. A client that sends such a character
  still gets its session. A value that is not a string is stored
  as `null`.
- **Fail closed.** The row commits before the response goes back. If
  the write fails, the client gets a JSON-RPC internal error (`-32603`)
  and no session id, as for every other MCP audit write.
- **Find the rows.** `meho audit query --op-id mcp.session_open`.

**The client name is what the tool says about itself.** MEHO cannot
check it. Any program with a valid login can send any name and version.
Use it to read the log, not to decide who may do what.

Code: `_audit_session_open` in `mcp/server.py`,
`write_mcp_session_open_row` in `mcp/audit.py`. Tests:
`tests/test_mcp_session_open_audit.py`.

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
from `ToolDefinition.to_wire()`. Output schemas are not counted here.
The size check of the lean tool profiles counts them (see "Lean tool
profiles under 6,000 tokens" below).

- One tool: at most 6,000 characters.
- The whole default list (26 tools, docs add-on on): at most 70,000
  characters, about 17,500 tokens.

When #4112 set the limits, the default list went from 96,804 to 66,092
characters. The largest tool, `query_topology`, went from 16,302 to
5,537.

A client that needs fewer tools can also ask for a tool profile (see
"Tool profiles" below). The `investigate` list has about a third of the
text of the full list (23,261 characters), `operate` about half
(35,776). The full list is 69,209.

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

### Taking `query_topology` off the default list (#5980)

`query_topology` was the largest tool on the default list. Few sessions
use it, so its text mostly takes up room. Two changes:

- **Shorter description.** It went from 1,927 to about 1,050
  characters. The tool's text as the size test counts it went from
  5,537 to 4,655 characters. With its output schema, the whole
  `tools/list` entry went from 13,572 to 12,688 characters.
- **A switch to take it off the default list.** Set
  `MCP_TOPOLOGY_OFF_DEFAULT_LIST=true` (`Settings.mcp_topology_off_default_list`,
  default `false`). Then `query_topology` is on the operator surface:
  only a session with the `mcp:admin` scope lists and calls it. A call
  from another session gets the same answer as for any other operator
  tool. The switch is off by default, so nothing changes until an
  operator turns it on. On a Helm install, set it through `extraEnv`.

How it works: `registry.tool_surface(defn)` returns the surface a tool
is on in this deploy. It is `defn.surface`, except for `query_topology`
when the switch is on. The `tools/list` filter (`all_tools_for`), the
call-time check in `handlers.py` and the `surface_summary` of
`meho_status` all use it, so they agree. The registered definition does
not change, and the conformance test still pins the default list with
`query_topology` on it. `tests/test_mcp_topology_off_default_list.py`
covers the switch.

The CLI (`meho topology ...`), the REST routes and hosted agents that
name `query_topology` in their tool list are not affected.

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
recommendation. It holds the same JSON as the structured object, written
compact (no space after `,` and `:`, #4716). Tools without
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

### Each result once: `?results=structured` (#4716)

A client that reads `structuredContent` can ask for each result once. It
adds `?results=structured` to the MCP URL, for example
`https://<meho-host>/mcp?results=structured`. It works with a profile:
`?profile=operate&results=structured`. With `texts=short` no tool has an
`outputSchema` (#4714), so every result keeps the full text and this
option changes nothing.

What that session gets:

- A successful result of a tool with an `outputSchema`: `content` holds
  one short line, `The result is in structuredContent.`
  (`STRUCTURED_ONLY_TEXT` in `operations/delivery_budget.py`).
  `structuredContent` holds the full result, as before.
- A tool without an `outputSchema`: the full text, as before. It has no
  `structuredContent`.
- A result with `isError: true`: the full text and `structuredContent`,
  as before. Claude Code shows the model only the text of an error
  result.

The default is off. Without the parameter, or with an empty `results=`,
nothing changes. Any value other than `structured` (not case-sensitive)
is refused with HTTP 400 before the body is read, like an unknown
profile.

How it is built:

- The MCP route reads the parameter (`_apply_url_options` in
  `mcp/server.py`, `parse_tool_results` in `mcp/structured_results.py`)
  and sets `mcp_structured_results` on the request's `Operator`. Only
  this route sets it.
- `handle_tools_call` passes `structured_only` to `tool_result_text`.
- There is no server-wide switch on purpose. One MEHO serves many
  clients, and a client that reads only the text (for example Cursor)
  would lose every result. Only the client knows which copy it reads.
- It is a URL option, not a tool profile. A profile picks the tools and
  connectors of a session. Which copy a client reads is a property of
  the client, and it combines with any profile.

**What each client gives the model.** Checked in November 2026, from the
client source code or its public bug reports:

| Client | Successful result with both copies | Result with `isError: true` | Source |
|---|---|---|---|
| Claude Code | `structuredContent` only | the text only | anthropics/claude-code issues 79944, 86032, 103396 |
| Claude Desktop, Code tab and Cowork | `structuredContent` only | the text only | anthropics/claude-code issue 79944, anthropics/claude-ai-mcp issue 1132 |
| Claude Desktop and claude.ai chat | not documented; the reports say chat reads the full result. `structuredContent` must be there for a tool with an `outputSchema` (#2774) | not documented | anthropics/claude-ai-mcp issue 1132 |
| Codex | `structuredContent` only, as compact JSON text | `structuredContent` only | openai/codex, `codex-rs/protocol/src/models.rs`, `as_function_call_output_payload` |
| pydantic-ai `MCPToolset` (the model scoring runner, #4253) | `structuredContent` only, when every `content` block is text | `structuredContent` | `pydantic_ai/mcp.py`, `_map_mcp_call_tool_result` (2.53) |
| Cursor (through the `mcp-remote` shim) | the text only | the text only | Cursor forum, August 2026 |
| `meho` CLI | not an MCP client: it uses REST, which sends the result once | | `cli/` |

So the clients we checked already give the model one copy. The switch
saves bytes on the wire and in the client. It saves model tokens only
for a client that gives the model both copies. Do not use it with a
client that reads only the text: that client then sees only the short
line. The benchmark measures the saving before the switch is
recommended to anyone.

**Size check.** `sent_wire_bytes(result, structured_only=True)` counts
the short line plus `structuredContent`, which is what such a session
sends. The delivery ceilings do not use it. They use
`measure_delivered_wire_bytes`, which counts both copies in every
session, with the text as spaced JSON. So every session gets the same
result, the ceilings decide as before the compact text, and a
structured-only session only sends fewer bytes.

Tests: `tests/test_mcp_structured_results.py`.

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
on the `tools/call` wire by `measure_delivered_wire_bytes`
(`operations/delivery_budget.py`): the encoded text block plus the encoded
`structuredContent` copy, escaping included. It measures
both copies also for a `?results=structured` session, so every session gets
the same result. It is a byte count, not a token
count. The text block that is sent is compact JSON; the count uses the
same JSON with a space after `,` and `:`. That count is never smaller than
what is sent, and it keeps the decisions of the ceiling as they were before
the text became compact.

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
  row without searching the audit log. A call that waits for approval
  returns the id of its `approval.request` row the same way.
  `audit_id` is `null` only when no row was written or the write failed.

The full rules, bounds, and errors are in
[`result-spill.md`](result-spill.md). Tests:
`tests/test_mcp_tool_result_query.py`,
`tests/test_mcp_output_schema_conformance.py`, and
`tests/test_result_reduction_properties.py`.

### Long calls: `call_operation` async mode (#3695)

A long call (a deploy, a multi-step operation that runs for minutes) should
not hold one MCP request open the whole time. `call_operation` takes three
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
  `run_failed` / `run_cancelled`. A run of a change whose worker died gives
  `run_uncertain` with `extras.execution_outcome` `uncertain`, `retry`
  `false` and the advice in `extras.remediation`: MEHO does not know if the
  change happened, do not run it again, check the target first (#5025).
  Checking never runs the call again.
- **`wait_seconds`** (only with `run_id`, #5264) makes the check wait up to
  that many seconds for the run to end. The answer comes as soon as the run
  ends, or after the wait with `pending` / `running` as before. `0` or no
  value answers at once. An agent with only MCP tools has no shell, so it
  cannot sleep between checks; with this it does not have to send check
  after check. The limit is 50 seconds (`MAX_RUN_WAIT_SECONDS`), below the
  usual 60-second read limit of HTTP proxies. The `inputSchema` holds the
  range, so a larger value gets `-32602` with a message that names the
  maximum. `wait_seconds` without `run_id` gives
  `data.reason="wait_without_run_id"`. The wait holds no database
  connection: see
  [`async-governed-dispatch.md`](async-governed-dispatch.md#waiting-for-a-run-to-end-5264).

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

### Connectors that fit one target (#3677)

`meho_connector_list` takes an optional `target` (a target name or
alias, not its id). With it, the list keeps only the connectors that fit
that target, and the answer has a `target_fit` summary: `target`,
`product`, `version` (probed, else set by an operator, else `null`),
`preferred_impl_id`, `result` and a plain-text `hint`.

- **Same answer as a call.** The result comes from the resolver that
  runs when `call_operation` gets this target (`resolve_connector` in
  `connectors/resolver.py`, the function `resolve_connector_or_label`
  wraps). No version match is written in the MCP layer.
- **Four results**, all outcomes the resolver already has:
  - `compatible`: the resolver finds a connector. The rows are every
    listed connector that fits (`connector_serves_target`). When the
    target's version is outside every declared range, the resolver
    still picks a class through its entry that accepts every version.
    Then the rows are the listed rows of that class, and the hint says
    the version is outside their range. A row whose catalog refuses the
    target in its own check (`catalog_refuses_target`, the check a call
    runs before it answers `unqualified_target_version`) is left out.
    `compatible` never comes with an empty list: when a `status` or
    `product` filter hides the connector a call would use, or every
    row's catalog refuses the target, the result is `no_connector` with
    a hint that says why.
  - `fingerprint_unknown`: the target has no version yet. The rows are
    the connectors of its product; they may fit. The hint says to probe
    the target or set its version.
  - `no_connector`: no connector fits. No rows.
  - `ambiguous`: two or more connectors are still tied after the
    tie-break. The rows are the tied connectors; the hint says to set
    `preferred_impl_id` on the target.
- **It chooses nothing.** The answer names no chosen connector, and a
  `preferred_impl_id` does not hide the other rows. `call_operation`
  still picks the connector by target each time it runs.
- **Reads only.** The target is read in the caller's tenant. An unknown
  name is `-32602` with `error_code: no_target` (or `ambiguous_target`)
  and the near-miss names. Nothing is probed and the target row is not
  written.
- `status`, `product` and `detail` still apply. A long list still comes
  back as a handle; `target_fit` stays next to it.
- **Hosted agent.** The agent's connector allow-list runs after the
  summary is made. When it removes every row, the result becomes
  `no_connector`, and the hint says this agent may use none of the
  connectors that fit (`keep_allowed_connectors` in
  `agent/toolset_discovery.py`).

Code: `connectors_for_target` in `mcp/tools/connector_admin.py`. Tests:
`tests/test_mcp_tools_connector_admin.py` (`target_fit`) and
`tests/test_agent_toolset_discovery.py`.

### The `product` filter and other product names (#6560)

`meho_connector_list` takes an optional `product`. How it filters
depends on the switch `CONNECTOR_LIST_PRODUCT_ALIASES` (an env setting,
off by default).

- **Switch off (default).** The filter keeps only the rows whose
  `product` is exactly the given text. Case counts. A filter that matches
  nothing returns an empty `connectors` list and nothing else. This is
  the answer MEHO always gave.
- **Switch on.** The filter ignores case, and it also matches other
  names of a product. A connector class declares these names in
  `product_aliases` (for example `("vcenter", "vsphere")` on a VMware
  connector). The names of every class registered for a product count
  for that product, so one class can name them for all connectors of
  the product. The core has no vendor name of its own.
- **A wrong value never breaks the list.** A plain string in
  `product_aliases` is one name. Any other value that is not a list or
  tuple is skipped, with a warning in the log that names the class. An
  alias that is the real product name of another connector is skipped
  too, with the same kind of warning, so it never adds rows to that
  product.
- **The answer says what matched.** With the switch on, a call with a
  `product` has a `product_filter` block next to `connectors`:
  - `query`: the text that was passed;
  - `matches`: one `{product, matched_name}` per product that matched,
    for example `{"product": "vmware", "matched_name": "vcenter"}`;
  - when nothing matched, also `known_products` (the products of the
    connectors the `status` filter leaves) and a `hint` that says to ask
    again with one of them, or without `product`.
- `status`, `target` and `detail` still apply. A long list still comes
  back as a handle; `product_filter` stays next to it, like `target_fit`.
- **Long lists in the block.** `matches` and `known_products` have the
  same limits as the rows (more than 50 entries or 4 KB). A longer one
  is stored behind its own handle: the block then holds the reducer's
  summary with a `handle`, and `result_query` reads the rows
  (`known_products` rows are `{"product": name}`). See
  `reduce_product_filter`.
- **Hosted agent.** The agent's connector allow-list also narrows the
  block: `matches` keeps only products of rows the agent may use. When
  none is left, `known_products` names only the products of the
  connectors the agent may use, so the agent can ask again
  (`keep_allowed_product_filter`). When rows the agent may use match the
  product but none fits the `target`, `matches` names those products and
  the hint says that none fits the target. The rows not yet reduced carry
  `{connector_id: product}` of every listed row under
  `_listed_products` for this; `reduce_connector_list` drops it, so no
  answer shows it.
- **Why a switch.** Small models filtered by "vcenter" and gave up when
  the list came back empty. The switch goes on by default only after a
  benchmark run with it off and on, on the same build and models.

Code: `mcp/tools/_connector_product_filter.py`. Tests:
`tests/test_connector_list_product_aliases.py`.

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
  - `guidance` — only when the switch `OPERATION_CONTRACT_GUIDANCE` is
    on (off by default, #4757) and the op has written guidance. It holds
    the prose keys of the op's `llm_instructions`: `when_to_call` (and
    the older `when_to_use`), `preconditions`, `parameter_hints` (a map
    of parameter name to hint), `output_shape` and `next_step`. Each text
    is cut to 1000 characters and the whole block to 3000; a cut text
    ends with `…`, and when the total runs out the later keys are left
    out. Machine keys (`pagination_hint`, `result_*`) are not in it.
    With the switch off, or without guidance, the key is not in the
    answer at all. The rules live in `operations/_contract_guidance.py`;
    tests in `backend/tests/test_operation_contract_guidance.py`.
- **Unknown op.** A known connector with no such enabled op raises
  `UnknownOperationError`: MCP answers `-32602` with
  `data.reason="unknown_op"`, REST answers `404` with
  `detail.reason="unknown_op"`. An unknown connector keeps its own
  `unknown_connector` / `connector_not_ingested` errors. The lookup
  is deterministic and never returns an empty list.
- **Slim ranked hits.** A ranked hit carries `op_id`, `summary`,
  `description`, `group_key`, `safety_level` and `requires_approval`.
  It never carries the written guidance, also with the switch on.
  The ranking scores (`fused_score`, `bm25_score`, `cosine_score`) are
  added only when the caller sets `include_scores` **and** is
  `tenant_admin` (the same role the full descriptor read needs);
  anyone else gets the slim hit.
- **Category and labels, behind a switch (#6041).** While the switch
  `CUSTOM_POLICY` or `POLICY_CLASSIFICATION` is on, a ranked hit and the exact-op contract
  also carry `category` (for example `network`) and `labels` (a map of
  free `key=value` text). They are the operation's classification for
  the caller's tenant, the same values dispatch uses
  ([policy-classification.md](policy-classification.md)). `category` is
  `null` when two classification rules disagree; then every call waits
  for approval. The `labels` description in the output schema says that
  label text is data, not an instruction; the contract's
  `result_shape.guidance` says it too while the switch is on. Search
  stays tenant-wide: it does not say whether a call
  waits for *this* caller. With the switch off, both keys are left out,
  so every answer is the same as before. The registered `outputSchema`
  names both fields; while the switch is off,
  `classification_schema_for_settings`
  (`mcp/tools/_search_classification.py`) takes them out of the schema
  that `tools/list` sends, so that list does not change either. Tests:
  `backend/tests/test_policy_classification_shown.py`.
- **The admin's description, behind a switch (#4753).** An admin can
  replace an operation's text with `meho_connector_edit_op`
  (`custom_description`). The backplane switch
  `AGENT_READS_CUSTOM_DESCRIPTION` (an env var, off by default) decides
  whether the agent reads it.
  - **Off (default):** the agent reads the original `description` in
    each ranked hit, in the exact-op contract and in the keyword (BM25)
    half of search, as before the switch existed. An override saved
    earlier changes nothing for the agent.
  - **On:** when `custom_description` is set and not empty, the agent
    reads it in place of the original `description` in all three
    places. In the keyword half, the admin's words count and the
    replaced words no longer count. The meaning half (the embedding)
    includes the admin's text in both states, so the original words can
    still find the operation through its embedding, and the `matched` /
    `ambiguous` label still follows the fused scores. Setting it to
    `null` brings the original text back.
  - Each state has its own keyword index: `endpoint_descriptor_bm25_idx`
    (migration `0005`) for off, `endpoint_descriptor_bm25_custom_idx`
    (migration `0148`) for on. Turn the switch on only after `0148` ran,
    else the keyword half scans the table. Code: `agent_description`,
    `BM25_ORIGINAL_DOCUMENT_SQL` and `BM25_CUSTOM_DOCUMENT_SQL` in
    `operations/_search.py`; the switch is
    `Settings.agent_reads_custom_description`.
  - **Before turning it on** (measurement plan of the pull request):
    on the benchmark lane, compare the routing score with the switch
    off and on, for a small model and for Claude, on a few operations
    with a weak description and a clear `custom_description`. Then one
    live run on a test install: set a `custom_description`, search
    with words that appear only in it, check the hit shows the new
    text, set it to `null`, check the original is back.
- **How to call a hit, behind a switch (#4331).** The backplane switch
  `SEARCH_HIT_CALL_HINTS` (an env var, off by default) adds two small
  fields to each ranked hit, so an agent can make a right first call
  without a second lookup:
  - `required_params`: the names in the top-level `required` list of the
    operation's stored `parameter_schema`, in that order. `call_operation`
    checks `params` against that same schema. Names only, no schemas: the
    full schema stays in exact-op mode. An operation with no `required`
    list gives `[]`.
  - `target_required`: `true` when a call needs a `target`. It uses the
    dispatcher's own rule, `descriptor_requires_target` in
    `operations/dispatcher.py`, which the exact-op contract's
    `target.required` also uses. So the two always agree. Every generic
    (ingested) operation needs a target; a typed or multi-step operation
    needs one when its handler reaches the connector through the target.
  - **Off (default):** both keys are left out, and the handler of a hit is
    not even imported, so every answer is the same as before. The
    registered `outputSchema` names both fields; while the switch is off,
    `call_hints_schema_for_settings`
    (`mcp/tools/_search_call_hints_schema.py`) takes them out of the
    schema that `tools/list` sends. The REST `GET /api/v1/operations/search`
    answer follows the same switch; the CLI `meho operation search --json`
    prints the fields when the backplane sends them.
  - **Before turning it on** (measurement plan of the pull request): on
    the benchmark lane, compare the job score with the switch off and on,
    for a small model and for Claude, and the size of a search answer
    (it must grow by less than 10%). Turn it on only if the small model's
    score goes up and Claude's does not go down. Then one live run on a
    test install through the agent surface. Code:
    `operations/_search_call_hints.py`. Tests:
    `backend/tests/test_search_hit_call_hints.py`.

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
| `package_not_installed` | The connector's operations come from a connector package that is not installed in this MEHO. The advisory names the package. | Do not call it. An operator must install the package again. |
| `code_not_installed` | The code of the connector is not in this MEHO (its rows have no package name, and no handler imports). Also the answer in exact-op mode. | Do not call it. An operator must run a MEHO image that has this connector. |

`advisory` explains any outcome other than `matched` in one sentence.
`narrowing` lists next steps, at most `SEARCH_NARROWING_MAX` entries.

- The outcome reads the scores the search already made. Hit order does
  not change. The search always reads at least two hits from the
  retriever (`SEARCH_CLASSIFY_MIN_HITS`), even for `limit=1`, so a tied
  second hit cannot hide. The returned list is then cut to `limit`.
- The near-tie band is `SEARCH_NEAR_TIE_BAND`: one list position at the
  top of one ranking signal.
- **Plain sentences, behind a switch (#5490).** The keyword half of the
  search needs every word of the query in the operation's text. A
  sentence of several words almost never has that, so its top hit has no
  keyword score and the answer is `ambiguous`, even when the top hit is
  right. The backplane switch `SEARCH_PLAIN_SENTENCES` (an env var, off by
  default) changes only the outcome, never the hits or their order:
  - A top hit without a keyword score counts as a word match when its
    `summary` or `description` (the text the agent reads) holds any word
    of the query, with the same word rules as the keyword index (stemmed,
    without stop words). The near-tie check still applies.
  - A search with a `group` filter that leaves exactly one enabled
    operation answers `matched`. The count uses the same tenant, enabled
    and installed rules as the search.
  - Off: the same answers as before the switch existed.
  - Why the ranking does not change: a local test that let any word count
    in the ranking put the right operation first less often (hit@1 0.757
    → 0.657 when the query is the words of the op id, over the 580
    operations MEHO registers from code).
  - Code: `operations/_search_plain_sentences.py`, `_classify_hits` and
    `_run_search` in `operations/meta_tools.py`. Tests:
    `tests/test_search_plain_sentences.py` and
    `tests/integration/test_search_plain_sentences_pg.py`.
  - **Before turning it on:** compare the benchmark lane with the switch
    off and on, for a small model and for Claude, then one live run on a
    test install (see the pull request of #5490).
- **A stricter `matched`, behind a switch (#6014).** A search can answer
  `matched` when the top hit won the score list by a little, while
  another hit is closer in meaning. A live search for "list virtual
  machines" did this: the top hit was about one VM, and the right
  operation was fifth. The backplane switch `SEARCH_CLEAR_LEAD` (an env
  var, off by default) changes only the outcome, never the hits or their
  order:
  - When another hit has a better meaning score (`cosine_score`) than the
    top hit, the answer is `ambiguous`. A top hit with no meaning score
    counts as behind any hit that has one.
  - With the switch on, the search also reads the candidate with the best
    meaning score, even when it ranks below `limit` (for example eleventh,
    below ten keyword hits). The top hit is compared against it. The
    returned list is still cut to `limit`.
  - Off: the same answers as before the switch existed.
  - Why not a wider near-tie band: a band of two list positions took
    `matched` away from six right top hits for each wrong one, over the
    operations MEHO registers from code. It also cannot work with
    `SEARCH_PLAIN_SENTENCES`, because a search with no keyword hits has
    its top two exactly one list position apart.
  - Code: `operations/_search_clear_lead.py` and `_run_search` in
    `operations/meta_tools.py`. Tests: `tests/test_search_clear_lead.py`
    and `tests/integration/test_search_clear_lead_pg.py`.
  - **Before turning it on:** compare the benchmark lane with the switch
    off and on, for a small model and for Claude, then one live run on a
    test install (see the pull request of #6014).
- **Typed and multi-step operations first, behind a switch (#4712).** A
  big connector holds many more raw operations (made from a vendor API
  description, `source_kind='ingested'`) than typed and multi-step ones,
  in the same groups. So a search often finds a raw call first, even
  when a safer typed or multi-step operation exists. The backplane
  switch `SEARCH_TYPED_FIRST` (an env var, off by default) changes what
  `search_operations` and `list_operation_groups` show (MCP, REST and
  CLI the same):
  - Ranked search returns only `source_kind` `typed` and `composite`
    rows. The filter is in the candidate queries, so raw rows cannot
    push typed ones out of the top hits.
  - `include_raw: true` (REST `include_raw=true`, CLI `--include-raw`)
    adds raw operations back.
  - A connector with no typed or multi-step operation the caller can use
    (same tenant, enabled and package rules as the search) shows its raw
    operations without the flag.
  - Each answer has `raw_operations`: `included`, a count, and a `note`.
    When a typed-only search is `no_match` or `ambiguous`, the search runs
    once more with raw operations, and `hidden_matches` says how many
    raw hits were left out; the note names `include_raw`.
  - `list_operation_groups` counts typed and multi-step operations
    (`operation_count`, `enabled_op_count`) and leaves out groups of raw
    operations only (`hidden_groups`). The filter is in the query, so
    paging stays correct.
  - Not changed: exact-op mode (`op_id`) finds any operation; calling,
    policy and approval are the same. The operator console search still
    shows every operation.
  - Off: no `include_raw` input, no `raw_operations` field and no
    description line in `tools/list`
    (`mcp/tools/_include_raw.py`); the REST and CLI flag change nothing.
    Hosted agent runs follow the switch the same way, with the setting
    `agent_operation_tools_from_mcp` on or off. On: their tools are built
    for each run (`agent/toolset_operations.py`). Off: the hand-written
    tools get the `include_raw` input and the line while the switch is on
    (`with_include_raw_input`), so a hosted run can follow the note.
  - Code: `operations/_search_typed_first.py`, `typed_only` in
    `operations/_search.py`, `search_operations` and
    `list_operation_groups` in `operations/meta_tools.py`. Tests:
    `tests/test_search_typed_first.py`, and
    `test_include_raw_follows_the_typed_first_switch` in
    `tests/test_mcp_surface_conformance.py` and
    `tests/test_agent_toolset_operations.py`.
  - **Before turning it on:** compare the benchmark lane with the switch
    off and on, for a small model and for Claude, then one live run on a
    test install (see the pull request of #4712).
- **Operations in the group listing, behind a switch (#4713).** A model
  had to search to see any operation, and a search needs good words. The
  backplane switch `GROUP_LISTING_OPERATIONS` (an env var, off by default)
  lets a model walk down without a search query: connector, group,
  operation, contract (`search_operations` with `op_id`), call. When on,
  `list_operation_groups` changes like this (MCP, REST
  `GET /api/v1/operations/groups` and CLI `meho operation groups` the
  same):
  - Each group has `operations`: its typed and multi-step operations
    (`source_kind` `typed` and `composite`), by `op_id`. Each line has
    `op_id`, `summary` (the first sentence of the summary, cut to 100
    characters; `null` when there is none), `safety_level` and
    `requires_approval`. The risk level and the approval flag are the
    ones a call uses, operator overrides included. Only operations that
    are on for the tenant, in its scope, with their package installed.
  - Raw operations are never listed one by one (a big connector has
    thousands). `operation_count` still counts them when the listing
    shows them; search finds them.
  - Size limits, as compact JSON: one group under 4 KB, one answer under
    12 KB. A group with more operations than fit lists the first ones (by
    `op_id`) and says how many are left out in `operations_more`. An
    answer whose groups do not fit ends early, with `next_cursor` set to
    the last group shown, like a full page. Every group of every
    built-in connector fits whole. The text of a group (`name` and
    `when_to_use`) stays under 3 KB, so a group always has room for
    operation lines: a longer text is cut and ends with `…`. Every ASCII
    text within the limits of a group fits whole; only a long text with
    many non-ASCII characters is cut.
  - `group` (REST `group=`, CLI `--group`) lists one group. A `group`
    that matches no group gives an empty list and a `note`. The CLI also
    has `--cursor`.
  - Not changed: the operator console list and the hosted agent tools
    that have no `cursor` input (the hand-written `list_operation_groups`
    and the agent's default tool) keep the old listing
    (`list_operations=False`). The hosted tool built from the MCP tool
    (`agent_operation_tools_from_mcp` on) follows the switch.
  - Off: no `group` input, no `operations`, `operations_more` or `note`
    field and no description line in `tools/list`
    (`mcp/tools/_group_operations.py`); the REST and CLI `group` change
    nothing.
  - Code: `operations/_group_listing_operations.py`, called from
    `list_operation_groups` in `operations/meta_tools.py`. Tests:
    `tests/test_group_listing_operations.py` (with the size check over
    every connector MEHO registers at start-up), and
    `test_group_operations_follow_their_switch` in
    `tests/test_mcp_surface_conformance.py`.
  - **Before turning it on:** compare the benchmark lane with the switch
    off and on, for a small model and for Claude, then one live run on a
    test install (see the pull request of #4713).
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

## Tool profiles (#4172)

A client can ask for a smaller tool list. It adds `?profile=<name>` to
the MCP URL, for example `https://<meho-host>/mcp?profile=investigate`.
A small model then sees 9 or 13 tools, not 26, and keeps more of its
context window for the job.

| Profile | Tools |
|---|---|
| `investigate` (9) | `meho_status`, `meho_connector_list`, `list_targets`, `list_operation_groups`, `search_operations`, `call_operation`, `result_query`, `search_knowledge`, `search_memory` |
| `operate` (13) | `investigate` plus `preview_operation`, `meho_broadcast_recent`, `meho_broadcast_announce`, `meho_broadcast_watch` |

The lists are in `mcp/profiles.py`.

How it works:

- The MCP route reads the `profile` URL parameter
  (`_apply_url_options` in `mcp/server.py`) and stores it on the
  request's `Operator` as `mcp_tool_profile`. Only this route sets it.
- `all_tools_for` (`mcp/registry.py`) runs the four gates above first,
  then keeps only the profile's tools. So a profile only removes tools.
  An `mcp:admin` session with a profile still sees only the profile.
- `tools/call` checks the profile again (`handle_tools_call`). A call to
  a tool outside the profile is refused with `reason: not_in_profile`,
  and the handler does not run. The refusal is audited as a 403.
- No profile, or an empty `profile=`, gives the full list, as before.
- An unknown name, or two different names, is refused with HTTP 400
  before the body is read. A typo must not quietly give the full list.
  Names are not case-sensitive.
- The session-start text follows the profile. The broadcast band is
  there only when `meho_broadcast_announce` is on the list (#4113). A
  profile without the runbook tools gets no runbook band, and a profile
  without the docs tools gets no docs collection list
  (`_session_instructions`). Tenant conventions are tenant text and are
  not changed.
- `meho_status` shows the hidden working tools in
  `surface_summary.missing_working_tools`, because it uses the same
  filter.

Why a URL parameter and not a header: every MCP client can set the
server URL, and not every client can set a header. MEHO checks the
token audience against its configured MCP resource URI
(`mcp_resource_uri`), which has no query. So on the server side the
same token works with and without a profile. Whether each client's
login flow accepts a server URL with a query has not been tried in a
lab yet.

A built-in profile does not change what the person may do.
`call_operation` in the `investigate` profile can still run a change,
under the person's role, the operation's risk level and the approval
rules. The built-in profile only makes the tool list shorter.

### Profiles with a connector list (#6099)

An admin can add profiles that also limit what a session may do, on the
server. Two settings, both off by default:

- `MCP_PROFILE_CONNECTOR_LISTS` (`true` / `false`, default `false`) is
  the switch. When it is off, MEHO does not know the extra profile names.
  It stays off until a model test and a sandbox run show it helps.
- `MCP_CONNECTOR_PROFILES` is one JSON object of profiles. A bad value
  stops the start (`mcp_profile_config.py`):

  ```json
  {
    "k8s": {"tools": "investigate", "connectors": ["kubernetes"]},
    "k8s-ops": {"tools": "operate", "connectors": ["kubernetes"], "read_only": false}
  }
  ```

  - `tools`: the built-in list the profile starts from.
  - `connectors`: one or more connector families. A name counts for the
    connector with exactly that id and for every connector whose id
    starts with the name and a dash. So `kubernetes` counts for every
    version of every Kubernetes connector. Make each name as narrow as
    you can: `vmware` counts for `vmware-rest-9.0` and for every other
    connector whose id starts with `vmware-`; write `vmware-rest` when
    the profile needs only that one.
  - `read_only`: `true` when left out.
  - A name is lower-case letters, digits and dashes, and not
    `investigate` or `operate`.

What such a profile does (`mcp/profiles.py`):

- Every tool that takes a `connector_id` (`call_operation`,
  `preview_operation`, `search_operations`, `list_operation_groups`)
  refuses a connector outside the profile before the handler runs
  (`_refuse_connector_outside_profile` in `mcp/handlers.py`). The answer
  says "is not in your profile", with `reason: connector_not_in_profile`.
  The check compares the `connector_id` argument, so it also covers a
  raw `METHOD:/path` operation. A value that is not text is refused too.
- `meho_connector_list` shows only the profile's connectors. The filter
  runs before the list becomes a result handle. It is the same filter the
  hosted agents use for their connector list (`keep_connector_rows` in
  `mcp/tools/connector_admin.py`).
- A call runs on the connector that its target resolves to, and that
  can differ from the `connector_id` argument. So the policy gate also
  refuses a target whose connector is outside the profile, or whose
  connector cannot be found (`profile_target_denial` in
  `mcp/profiles.py`). This holds for a call, a preview, the steps of a
  multi-step operation and the checks of a runbook, because they all ask
  the policy gate. A preview on such a target is refused before it shows
  the request (`denied`), and nothing is sent and no approval request is
  made.
- `list_targets` shows only the targets whose connector is in the
  profile, with and without a `connector_id` argument. Such a page can
  hold fewer targets than `limit` and still have a `next_cursor`.
- MEHO applies the stricter of the person's rules and the profile's
  rules. The policy gate (`policy_gate` in `operations/_validate.py`)
  denies a connector outside the profile, and in a read-only profile
  every operation that changes something (a method other than `GET` or
  `HEAD`, or a typed operation that is not `safe`). The deny comes before
  the person's grants are read.
- A read-only profile also has no MEHO write tools (`add_to_memory`,
  `add_to_knowledge`, `meho_broadcast_announce`, the runbook run tools),
  because they do not check grants.
- Every audit row of a session with a profile names it in the
  `mcp_profile` payload field (`apply_client_block`). It is never put
  into `actor_sub`, which names an agent.
- A stored result keeps the profile of the session that made it
  (`result_profile_var` in `connectors/result_handle_store.py`, set by
  `handle_tools_call`). A read under another profile, or without a
  profile, is "not found". Only a profile with a connector list keeps
  its results apart (`result_profile_scope`). No profile, a built-in
  profile, and every session while the switch is off keep and read
  results as before, also results made before the upgrade or through
  REST.
- A background run of `call_operation` keeps the profile the same way
  (`operation_run.mcp_profile`, migration `0174`). Reading it with the
  `run_id` under another profile, or without a profile, gives
  `unknown_run` (`poll_call` in `mcp/tools/_call_operation_async.py`).
- After a person approves a waiting request, the approved run is not
  checked against the profile again. The approval runs outside the MCP
  session, so its result is stored without the profile.
- `/ready` says only whether `MCP_PROFILE_CONNECTOR_LISTS` is on
  (`profile_connector_lists`). It needs no login, so it does not name
  the profiles.

Not filtered: the MCP resources (`resources/list`, `resources/read`).
They hold tenant info, conventions, knowledge and memory, and no
connector data.

### A login token for one profile

A login client can be made for one profile:
`meho admin keycloak bootstrap-clients --mcp-profile <name>` makes
`meho-mcp-profile-<name>`. It has the settings of `meho-mcp`, plus a
fixed claim mapper that writes `mcp_profile: <name>` into every token.
The mapper is on the client, so a session cannot get a token without it.

- MEHO reads the claim when it checks the token
  (`auth/mcp_profile_claim.py`). A value it cannot read (also `null`) gives 403
  `malformed_mcp_profile_claim`; a profile it does not know gives 403
  `unknown_mcp_profile_claim`, at every entrance.
- `verify_jwt_for_audience` refuses such a token with 403
  `mcp_profile_scope_violation` unless the caller allows it. Only the MCP
  route allows it (`verify_mcp_jwt`). So a new entrance refuses a profile
  token by default.
- The MCP route takes it only when `?profile=` names the same profile
  (`_apply_url_options`). `/mcp` without a profile, or another profile,
  gives 403. It is 403, not 401, so the client does not start a new login.
- A token without the claim works as before, with or without
  `?profile=`. This check needs no switch: no token has the claim today.

Limit: this stops the reuse of the profile's token at another address.
It does not stop the same person from signing in again through another
login client.

Tests: `tests/test_mcp_profile_connectors.py`,
`tests/test_mcp_profile_targets.py`, `tests/test_mcp_profile_token.py`.

## The current tenant of a session (#5239)

One login can list several tenants (the `meho_tenants` claim). An MCP
session works in **one** tenant, fixed at the start by the `Meho-Tenant`
request header (a tenant name or UUID). To switch, the client changes the
setting and starts a new session. There is no tool to switch tenants, so
the working surface stays at its size. Admin guide: `docs-site/guides/several-tenants.md`.

The two connection helpers send the header (#5242): the Claude Code plugin
shim from `MEHO_TENANT` (`clients/claude-code-plugin/bin/meho-mcp-remote`),
and the Claude Desktop bundle from its optional `tenant` setting
(`clients/claude-desktop-mcpb/`, see `claude-desktop-mcpb.md`). Both pass it
to `mcp-remote` as `--header "Meho-Tenant: <value>"` and send nothing when it
is not set.

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
  `current`), `vault.checked = false` with
  `vault.detail = "tenant_not_chosen"`, the database
  state and `server_time` (#5504). It runs no tenant work (no credential check, no add-on
  pairings, no tool comparison). With a current tenant, `meho_status`
  answers as before and also lists `operator.tenants`, with `current`
  set on the current one. Inside an agent run that a person started, no
  `tenants[].tenant_role` is higher than `operator.tenant_role`, the
  run's own role (#5773): the person's login groups may give a higher
  role, but the agent never gets it.
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

Counts: **26 working + 55 operator + 1 pairing-gated automation =
82 registered tools**, plus the **3 human-only verbs** (below) that
carry no MCP registration under any claim set. Four of the 26 working
tools (`ask_docs`, `search_docs`, `read_docs`, `list_doc_collections`)
appear only when the tenant has the `meho-docs` capability; a session
without it lists 22. The 26 working and 55
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
| `add_to_knowledge` | operator | — | Write a distilled entry to the tenant knowledge base. With `KB_SIMILAR_CHECK` on (off by default), a new slug with similar entries is not written; the answer lists them (`create_anyway` writes anyway, `dry_run` writes nothing). With it off, the tool has no text about this and no `create_anyway` input. On a tenant that keeps its lessons in its own wiki, `wiki` says if the lesson also goes there. |
| `add_to_memory` | operator | — | Save a scoped memory entry (user / tenant / target). |
| `ask_docs` | operator | `meho-docs` | Synthesized, cited answer over a vendor-document collection. |
| `call_operation` | operator | — | Invoke a resolved connector operation against a target. |
| `list_doc_collections` | operator | `meho-docs` | List the documentation collections this session may search. |
| `list_operation_groups` | operator | — | List a connector's enabled operation groups. |
| `list_targets` | operator | — | List the tenant's accessible infrastructure targets. |
| `meho_broadcast_announce` | operator | — | CLI: `meho broadcast announce <activity>` publishes through the shared durable publisher. |
| `meho_broadcast_recent` | operator | — | CLI: `meho broadcast recent` reads stored agent announcements through the shared archive reader (operation, approval and check events are on the SSE feed and in `query_audit`). |
| `meho_broadcast_watch` | operator | — | CLI: `meho broadcast watch` tails the authenticated `/api/v1/feed` SSE route. |
| `meho_connector_list` | operator | — | List connectors visible to the tenant (plus built-in / global); `target` keeps only the ones that fit one target. |
| `meho_runbook_abort` | operator | — | Abort an in-progress runbook run. |
| `meho_runbook_list_runs` | operator | — | List runbook runs (operators see only their own). |
| `meho_runbook_list_templates` | operator | — | List runbook templates in the tenant. |
| `meho_runbook_next` | operator | — | Advance one step of an in-progress run (opacity contract). Optional `retry` runs a failed step's check again. |
| `meho_runbook_show_template` | operator | — | Read a runbook template's full body. |
| `meho_runbook_start` | operator | — | Start a new runbook run (self-assigned). |
| `meho_status` | read_only | — | Return caller identity + backplane dependency status, plus `granted_scopes` and `surface_summary` (#3666); since #4830 also the database migration revision (`db.revision` / `db.head`), the `/ready` checks (`readiness`), the approval settings in force (`approvals`) and the gated features (`features`). Since #5239 also the person's tenants with the role in each (`operator.tenants`); it is the one tool a session without a current tenant may call. Since #5504 also the server's time now in UTC (`server_time`, ISO 8601 to the second, for example `2026-10-31T14:34:28Z`), so an agent with no shell can write down a correct time. |
| `preview_operation` | operator | — | Preview an operation without running it. |
| `query_topology` | operator | — | Query the topology graph (dependents, blast radius, timeline). With `MCP_TOPOLOGY_OFF_DEFAULT_LIST=true` it moves to the operator surface (#5980). |
| `read_docs` | operator | `meho-docs` | Read the text around a docs hit, its whole page or its section, through the hit's `read_handle` (#3948). |
| `result_query` | operator | — | Read rows back from a JSONFlux result handle: page a window, or run a bounded structured query (filter / project / group / aggregate) server-side. |
| `search_docs` | operator | `meho-docs` | Search a vendor-document collection for an authoritative fact. |
| `search_knowledge` | operator | — | Search the tenant knowledge base, and the tenant's own wiki when one is set (`kb.md`). |
| `search_memory` | operator | — | Search the operator's accessible memories. |
| `search_operations` | operator | — | Hybrid BM25 + cosine search over a connector's operations. With `op_id` (exact-op mode) it returns that one operation's call contract instead: the stored `parameter_schema`, small examples, the target-by-name rule, the result shape and retry rules (#3668). |

**`meho_status` and the Vault check.** The tool is open to every role,
but its Vault check runs only for a caller with the `operator` role or
higher. That is the rule of `GET /api/v1/health`
(`_federation_status_for` in `api/v1/health.py`). The check logs in to
Vault as the caller and reads a secret. A `read_only` caller gets every
other part of the answer, and `vault.checked = false` with
`vault.detail = "not checked: needs the operator role"`. MEHO makes no
Vault login and no secret read for that caller. `meho status` prints
no Vault line for a `read_only` login: it reads the liveness route
instead. Only `meho doctor` (which reads the `meho_status` answer)
prints `vault not checked (needs the operator role)`.

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
| `meho_connector_edit_group` | tenant_admin | — | Edit an operation group's `when_to_use` prose or name; with `CUSTOM_POLICY` or `POLICY_CLASSIFICATION` on, ask for its category or labels. |
| `meho_connector_edit_op` | tenant_admin | — | Edit one operation's per-op overrides; with `CUSTOM_POLICY` or `POLICY_CLASSIFICATION` on, ask for its category or labels. |
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
| `meho_scheduler_create` | tenant_admin | — | Create a scheduled trigger (cron / one-off / event). While `CHECKS_AGENT_EVENTS` is on, an event trigger can take `check_source` (a Sensor or a Dashboard, optionally only when it gets worse) instead of `event_filter`; while it is off, the tool shows and accepts no `check_source`. |
| `meho_scheduler_list` | operator | — | List scheduled triggers. |
| `meho_sensor_create` | tenant_admin | — | Create a sensor (op + assertion + cadence + severity). |
| `meho_sensor_delete` | tenant_admin | — | Hard-delete a sensor by id. |
| `meho_sensor_list` | operator | — | List sensors with their latest-result projection. |
| `meho_sensor_pause` | tenant_admin | — | Pause a sensor: the runner stops running it; its history stays. |
| `meho_sensor_results` | operator | — | Per-tick evidence trend history for one sensor. |
| `meho_sensor_resume` | tenant_admin | — | Resume a paused sensor; it runs again at once. |
| `meho_targets_register` | tenant_admin | — | Register a new target in the tenant. |
| `meho_targets_update` | tenant_admin | — | Change fields of one target in the tenant, by its id. |
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
`tests/test_mcp_human_only_surface.py`. The approval groups
(MEHO-Operators, MEHO-Admins and the old groups read as those roles) decide only
*which people* may approve on REST, the command line and the web
interface; they never open an MCP path.

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

**Advertised scopes (#5514).** MEHO tells MCP clients which scopes to ask
for in two places: `scopes_supported` in `/.well-known/oauth-protected-resource`
(`api/well_known.py`) and the `scope` parameter of the 401
`WWW-Authenticate` challenge (`www_authenticate_header` in `mcp/auth.py`).
Both use `MCP_SCOPES_SUPPORTED` in `mcp/auth.py`:
`mcp:read mcp:execute offline_access`. MCP 2025-11-25 ("Scope Selection
Strategy") has a client ask for the challenge `scope` first, then for
`scopes_supported`. `offline_access` is in the list so a client gets a
login that renews itself; without it, the login ends at the realm's short
SSO idle limit. `mcp:admin` is deliberately not advertised: elevation
stays an explicit request. The `meho-mcp` client in the shipped realm
offers `offline_access` as an optional scope, so a client that asks for
it is not refused. The MEHO plugin and the Claude Desktop bundle (both
`mcp-remote` 0.1.39) still send their own scope list in the browser login
(`--static-oauth-client-metadata`, which `mcp-remote` puts into the login
URL after the advertised scopes), so `MEHO_MCP_SCOPES` with `mcp:admin`
keeps working.

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
(`caution` AGENT verdict floor → `needs-approval`) while a person in the
web interface runs it at once (a call made with a token waits, #3980).
`requires_approval=True` would make the web interface wait too; so would
`safety_level="dangerous"`. For an agent, `dangerous` waits for approval
just like `caution` (#3980); it is not refused.

These synthetic connectors follow this mould (natural key
`(product, "1.x", impl_id)` → wire `connector_id`):

| Wire `connector_id` | Op | MCP front | Since |
|---|---|---|---|
| `secret-broker-1.x` | `secret.move` | (dispatch-only) | #1577 |
| `secret-broker-1.x` | `secret.handoff.export` / `secret.handoff.import` | (dispatch-only; `dangerous` + `requires_approval`, a second person approves on each instance) | #5228 |
| `topology-graph-1.x` | `topology.create_node` / `.annotate` / … | `meho_topology_create_node` / `_annotate` / … | #2537 |
| `targets-registry-1.x` | `targets.register` | `meho_targets_register` | #2861 |
| `targets-registry-1.x` | `targets.update` | `meho_targets_update` (`caution` + `requires_approval`; the same code as `PATCH /api/v1/targets/{name}`; the target is named by its id) | — |
| `targets-registry-1.x` | `targets.remove` | (dispatch-only; `dangerous` + `requires_approval`, a second person approves) | #4623 |
| `targets-registry-1.x` | `targets.probe` | (dispatch-only; a `safe` op that detects a target's product and version and stores them, the same code as `POST /api/v1/targets/{name}/probe`) | #5653 |
| `audit-integrity-1.x` | `audit.loose_rows` | (dispatch-only; a `safe` read a Sensor pins) | #4670 |
| `spaces-records-1.x` | `spaces.assign`, `spaces.unassign`, `spaces.reassign`, `spaces.member.disable`, `spaces.overview`, `spaces.drift` | (dispatch-only; the four writes are `caution` + `requires_approval` and need `tenant_admin`; the overview and the drift check are `safe` reads) | #4933 |
| `inventory-1.x` | `inventory.export` | (dispatch-only; a `safe` read that turns stored or inline inventories into a file in a named format) | #5218 |
| `tools-admin-1.x` | `tools.sign_out` | (dispatch-only; `caution` + `requires_approval`, every call waits for approval, needs `tenant_admin`; a tenant admin signs out one tool of another person, `meho admin tools sign-out`) | #6303 |

**Per-op role floor.** `targets.register`, `targets.update`, the four `spaces.*` writes, the five `topology.*`
writes and `tools.sign_out` (`tools-admin-1.x`) need the `tenant_admin` role from a person, the same as their
REST routes and `meho_*` tools (`tools.sign_out` has no front of its own:
the floor is its only role check). Those fronts check the role at the door,
but `call_operation` (and the REST dispatch route and the CLI) needs only
`operator`, so the operations carry a role floor of their own
(`operations/role_floors.py`, registered at import by the connector
module). The policy gate (`_validate.py`) refuses a person below the
floor with `denied` and a `denied` audit row; the approval queue refuses
an approver below the floor at the decision (403, the request stays
pending), so an approved call was approved by a person with the role
whichever resume path runs it. Agents and service
logins keep their park-for-approval behaviour. `secret.move` and the
`secret.handoff.*` ops need no floor: they are `dangerous` with
`requires_approval=True`, so a person's call always waits for a second
person, and no front door of theirs asks for a higher role.
`targets.remove` checks `tenant_admin` inside its handler (see below),
because its sandbox-mode exception for a service login under a delete
grant needs the gate's verdict.

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
a service principal run it at once. That call needs no `tenant_admin`: a
service login has the `operator` role, and the delete grant is enough (#5480). It is reached through
`call_operation` only; there is no `meho_*` tool for it. See
[`targets.md`](targets.md#removing-a-target-targetsremove).

`meho_targets_update` (`mcp/tools/targets_update.py` +
`connectors/targets/update.py`) changes fields of one target. It runs the
`targets.update` operation, which finds the target by id, locks the row,
and calls `apply_target_update`, the code of
REST `PATCH /api/v1/targets/{name}`, so every check of that route applies
and none is written again. It is `caution` with `requires_approval=True`,
not the dial above: `host` and `fqdn` carry `x-meho-caller-address: true`
and the call can change `secret_ref`, so every call waits until a person
with `tenant_admin` approves it. People who change a target at once use
`PATCH`, `meho targets update` or the web console. The role floor is
`tenant_admin`, so a person with the `operator` role is refused on every
path, also through `call_operation`. The target is named by `target_id`,
never by name, so an approved call changes the target the approver saw;
the preview names that target and the fields the call sends. Only the
sent fields change, and an explicit `null` clears a nullable field. Its
`inputSchema` is the operation's `parameter_schema` (one object), and its
`outputSchema` is the parked-shape union, like `meho_targets_register`.

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

## Tool text that follows a switch (#6077)

Some features are behind a switch that is off by default. While a switch
is off, the tools say nothing about its feature, so an agent does not read
about a feature it cannot use, and a model test of the off state measures
the off state.

* A tool can set `ToolDefinition.for_settings`: a function that gets the
  registered tool and returns the tool as it is shown under the current
  settings. `tool_for_settings` (`mcp/registry.py`) applies it.
* `all_tools_for` applies it, so `tools/list` shows the switched form. The
  CLI reads the same list (`meho doctor` calls `tools/list`).
* `handle_tools_call` applies it before the `inputSchema` check, so a call
  is checked against the schema the list showed. An argument that is not
  in that schema is refused with `-32602`, and the message names it.
* With the switch on, the tool is the registered one, unchanged.
* `get_tool` returns the registered tool. The hosted-run tools are built
  from it once, when the process starts, so they keep the full text.

A parameter of an operation can wait behind a switch. Its schema carries
`x-meho-switch: "<switch name>"` (`SWITCH_MARKER` in the connector kit;
`operations/switch_params.py`). The keyword goes on a property schema,
at any depth, or on a shared schema that a property links to with a
local `$ref` (`$defs`, `definitions`, `components.schemas`). MEHO
knows one switch name today: `file_ref_params`, the setting
`FILE_REF_PARAMS` (off by default, listed by `GET /version`).

| Switch (off by default) | Where | With the switch off |
| --- | --- | --- |
| `FILE_REF_PARAMS` | `search_operations` (`required_params`), the exact-op contract, `preview_operation`, the operation read of REST, the console and the CLI | The marked parameter is left out of the shown schema: out of every `required` and `dependentRequired` list of the same object (also in `allOf`, `anyOf`, `oneOf`, `then`, `else` and a branch it links to), out of `example`, `examples` and `default` values at any depth (also map values from `additionalProperties` or `patternProperties`, and list items), and a shared schema only it used is dropped. Examples in `llm_instructions` (`examples`, `example_call`) that name it, and its `parameter_hints` entry, are not shown. A call that gives it is refused as `invalid_params`, before it can wait for approval, so no approval request is stored. The answer is the one of an unknown parameter, and no error text names a hidden parameter: an object with `additionalProperties: false` leaves it out of the check schema, any other object gets the schema `false` for it (in the object and in each branch of it). No rule requires it. An `anyOf` or `oneOf` branch that requires it matches no object, but still matches other values such as `null`, so a call without it gets the same answer as without the keyword. The hash of a call that does not give it is the same. |

* The stored schema is not changed. MEHO leaves the parameter out when it
  reads the operation row for the caller (`apply_effective_policy` and
  the operation read by id). The row keeps the schema for the call check
  in `parameter_check_schema` (not a column).
* A keyword that names another switch, or a keyword that is not on a
  property (for example on `items`, `additionalProperties` or a
  `patternProperties` schema), stops the operation from registering, for
  typed, multi-step and ingested operations
  (`operations/switch_registration.py`).
* The keyword under `if`, `then`, `else` or `not` also stops the
  operation from registering. So does a schema with the keyword that
  links in a way MEHO does not follow: `$anchor`, `$dynamicRef`, an
  `$id` below the root, or a `$ref` that is not a plain `#/` pointer
  (for example one with `%` escapes).
* These checks read every shared schema (`$defs`, `definitions`,
  `components.schemas`, also below the root) and every
  `patternProperties` or `dependentSchemas` schema, also when its name
  is a keyword such as `default`, `examples`, `enum` or `const`.
* With the switch on, the parameter shows and is accepted as usual.
* No new MCP tool and no new field.

Measurement before the switch goes on by default: run the benchmark job
list on the same build with the switch off and on, with the same models.
Record the pass rate of a small model and of Claude, and the size of an
operation read. Turn it on only if the small model does not get worse,
Claude does not get worse, and the answers grow by less than 10%. Then
run once on the sandbox through the agent surface. A later pull request
turns the switch on and links the numbers.

Three switches take text away while they are off:

| Switch (off by default) | Tool | With the switch off |
| --- | --- | --- |
| `KB_SIMILAR_CHECK` | `add_to_knowledge` | No text about the similar-entry check, no `create_anyway` input, and `dry_run` says nothing about similar entries (`mcp/tools/_kb_similar_text.py`). |
| `TEAM_KNOWLEDGE_RECALL` | `call_operation`, `preview_operation` | No `team_knowledge` field in the `outputSchema` (`mcp/tools/_operation_recall.py`). |
| `CUSTOM_POLICY` and `POLICY_ENGINE_SHADOW` | `preview_operation` | No `policy_rule` and no `approvers` field in the `outputSchema` (`mcp/tools/_preview_policy_fields.py`). |

Each older switch (`POLICY_CLASSIFICATION`, `POLICY_ENGINE_SHADOW`, `POLICY_APPROVER_ROUTING`) still turns on its own part alone.

With `CUSTOM_POLICY` or `POLICY_ENGINE_SHADOW` on, a `preview_operation` answer whose op
resolved has two more fields (#6045):

* `policy_rule`: the rule the policy evaluator matched for this call,
  `{source, rule_id, text, decided}`. `decided` is `false`: the
  evaluator runs in shadow mode, and today's checks decide the call.
  `null` when the evaluator failed.
* `approvers`: the login groups that may approve the call when
  `policy_verdict` is `needs-approval` (for example
  `["MEHO-Admins"]`), else `[]`. `null` when the
  stored routing is not valid: the preview still answers.

No new MCP tool: the default working surface stays the same. The person
who wants every rule in order uses `meho policy explain`
([policy-evaluator.md](policy-evaluator.md)). **Before turning the switch
on by default** (measurement plan, tracked in #6518): compare the benchmark lane
with the switch off and on, for a small model and for Claude, then one
live run on the sandbox MEHO: preview a call that waits and one that runs,
and check `policy_rule` and `approvers` against `meho policy explain`.

`tests/test_mcp_surface_conformance.py` checks both states of each switch.
`preview_operation` follows two switches, so its listing is the registered
one only when `TEAM_KNOWLEDGE_RECALL` is on and `CUSTOM_POLICY` or
`POLICY_ENGINE_SHADOW` is on.

A third switch works the other way: `MCP_SHORT_TOOL_TEXTS` (off by
default) gives the biggest tools a short text while it is on. One session
can also ask for these texts. See "Short tool texts" below.

A fourth switch adds an input while it is on: `MCP_REVEAL_FETCH` (off by
default). With it on, `call_operation` has the input
`approval_request_id` and a paragraph about it, added after the short
text, so both switches can be on. With it off, the tool is the registered
one and a call that sends the input is refused with `-32602`. See "Getting
an approved secret value on MCP" below.

A fifth switch, `MCP_OWN_REQUEST_STATUS` (off by default), adds the same
input for another use: reading the state of the caller's own approval
request. When both switches are on, the input has one text that covers
both. See "Reading the state of your own approval request" below.

A sixth switch changes an answer: `APPROVAL_SAME_CALL_REUSE` (off by
default). With it on, a call that must wait for approval gets the request
that already waits, when the same login in the same tenant sent the same
call. The answer has that request's `approval_request_id` and
`approval_request_reused: true`, with no key or with a different key. The
acting agent must be the same too, and the check of a step of a
multi-step operation never uses the reuse. A retry key that was linked to a
request while the switch was on keeps finding that request after it is
turned off. No tool and no input changes. See
[approvals.md](approvals.md#the-same-call-without-the-same-key-approval_same_call_reuse-off-by-default).

A seventh switch changes the `audit_id` of an answer: `APPROVAL_RETRY_OWN_AUDIT_ROW`
(off by default). With it on, a retry with the same `idempotency_key` that
gets the waiting request writes its own audit row, and the answer's
`audit_id` is that row, not the first call's. No tool and no input changes.
See [approvals.md](approvals.md#1-send-an-idempotency_key-so-a-retry-gets-the-same-request).

## Getting an approved secret value on MCP (`MCP_REVEAL_FETCH`, off by default)

A call with `reveal_secret: true` on a secret read waits for a second
person's approval, and the approval runs nothing. The person who asked
then fetches the value once, within 15 minutes of the approval
([approvals.md](approvals.md#an-approved-secret-reveal-runs-when-the-person-who-asked-fetches-it)).
On MCP that person calls:

```
call_operation(connector_id, op_id, approval_request_id)
```

- The code is in `mcp/tools/_call_operation_reveal.py`. The
  `call_operation` handler hands the call over when it has
  `approval_request_id`; it writes no call record.
- Only `connector_id`, `op_id` and `approval_request_id` may be sent.
  Any other input is refused with `-32602` (`reveal_fetch_extra_inputs`):
  what runs is the stored request, never new params.
- `connector_id` and `op_id` must be the ones of the request
  (`reveal_wrong_operation`). An agent is refused
  (`reveal_agent_refused`). These checks run before the fetch, so a
  refused call does not use up the one-time permission.
- The rest is `fetch_approved_reveal`: the same person and tenant
  (`not_request_owner`, `approval_request_not_found`), once
  (`reveal_already_used`), within the time limit (`reveal_window_passed`).
- A refused fetch answers `status: "denied"` (`isError: true`) with
  `extras.error_code` and `extras.approval_request_id`. Nothing ran.
- When the answer holds the value, it is marked
  (`no_store.mark_answer_holds_secret`; also for an operation that makes
  a new credential), and the `/mcp` route sends it
  with `Cache-Control: no-store` and `Pragma: no-cache`
  (`no_store.answer_scope`). A run that was refused or ended with an
  error holds no value and keeps the usual headers.
- With the switch off, a person who works through MCP gets the value on
  the command line (`meho secret read --approval <id> --field <name>`), in
  the console ("Show value") or with `POST /api/v1/approvals/{id}/reveal`.
- Tests: `tests/test_mcp_reveal_fetch.py`.

## Reading the state of your own approval request (`MCP_OWN_REQUEST_STATUS`, off by default)

A call that waits for approval answers `awaiting_approval` with
`extras.approval_request_id`. On the default tool list there is no other way
to follow that request: `meho_approvals_get` needs the operator role and
`mcp:admin`. With the switch on, the caller sends:

```
call_operation(connector_id, op_id, approval_request_id)
```

The caller needs the request id. It learns only the state of a request of its own login.
"Own" means the same login. So an agent that acts for a person reads that person's requests.
The read checks no agent grant.

- The code is in `mcp/tools/_call_operation_status.py`. The handler hands
  the call over when it has `approval_request_id`. The call runs nothing,
  makes no new request and writes no call record.
- With `MCP_REVEAL_FETCH` also on, an approved secret reveal of the caller
  still goes to the value fetch above. Every other request goes here. To
  pick the path, the handler first looks up the request
  (`is_approved_reveal`). This lookup runs before the checks below, also
  when the call has extra inputs. It writes no audit row and its result is
  not sent. With `MCP_REVEAL_FETCH` off, there is no such lookup.
- Only `connector_id`, `op_id` and `approval_request_id` may be sent
  (`-32602`, `own_request_status_extra_inputs`). `connector_id` and `op_id`
  must be the ones of the request (`request_wrong_operation`). For a request
  made by a step of a multi-step operation, they are the step's, not the
  operation's.
- The checks of `_call_operation_status.py` run in this order, after that
  lookup:
  1. the switch;
  2. extra inputs (`-32602`);
  3. a bad id (`-32602`);
  4. the request is the caller's own, in its tenant
     (`approval_request_not_found`);
  5. under an agent's connector list, the request's connector and stored
     target are inside the list (`connector_not_allowed`);
  6. `connector_id` and `op_id` are the ones of the request
     (`request_wrong_operation`);
  7. the read, the only step that writes the `approval.result` row;
  8. the answer.
- A request of another login, of another tenant and an unknown id all get
  `approval_request_not_found`, word for word.
- Each refusal in steps 4 to 6 writes one audit row with path
  `approval.status` and an `outcome` that names the reason, and no
  `approval.result` row. The row of `approval_request_not_found` is a row of
  the caller in its own tenant. It is linked to no request, so a request of
  another person or tenant gets nothing in its own history. The row of the
  other two refusals is linked to the request: its parent is the request's
  `approval.request` row. Its session is the session of the read, not the
  session that made the request.
- Each read that passes the checks writes one `approval.result` audit row, as
  `GET /api/v1/approvals/{id}/result` does
  (`approval_queue.read_approval_result`).
- A stored result that is not an operation result (for example one without
  `status` or `op_id`, or with an `extras` that is not an object) gives
  `status: "error"` with `extras.error_code: "approval_result_unreadable"`,
  `extras.approval_request_id`, `extras.approval_status: "approved"`,
  `extras.result_state: "uncertain"` and `extras.retry: false`. Its `error`
  text says that the request was approved and the operation may have run,
  that MEHO cannot read its stored result, that the call must not be sent
  again, and to ask a person to look at the request in the console. It
  writes one more `approval.status` row with that outcome. An unknown `status` text in
  an otherwise valid result is passed on as stored, as the REST read does.
- The answer by state: waiting gives the same `awaiting_approval` answer as
  the first call, with its `audit_id` and, when the request has the note of
  `rights_change_note` (see
  [approvals.md](approvals.md#the-rights-change-mark-an-admin-approves-it-in-the-web-console)),
  its `extras.approval_note`; approved and not finished gives `status: "pending"` with
  `extras.result_state` (`not_started` or `started`); approved and run gives
  the stored result with `extras.result_state` added (`executed`, `failed`
  or `uncertain`; with `uncertain` MEHO does not know if the change
  happened, and it never runs it again, see
  [approvals.md](approvals.md#uncertain-meho-does-not-know-if-the-change-happened-5025));
  rejected, expired and
  withdrawn give `status: "denied"` with `extras.error_code` of
  `approval_request_rejected`, `approval_request_expired` or
  `approval_request_withdrawn`, and a sentence that says to ask again.
- The stored result goes through the response size limit, with the added
  fields and the data notice counted (see "Response wire ceiling"). An `ok`
  answer over the limit keeps its status and `audit_id`. `delivery` is
  `partial` or `unavailable`, and the added `extras` fields are left out,
  as for a check of a background call.
- A hosted agent with a bound connector list gets `connector_not_allowed`
  for a request outside the list. This check runs before the
  `request_wrong_operation` check, so the answer names no connector
  outside the list, also when `connector_id` or `op_id` is wrong.
- With the switch off, the input and its text are not in the tool. The
  measurement before it goes on by default is
  [evoila/meho#6803](https://github.com/evoila/meho/issues/6803).
- Tests: `tests/test_mcp_call_operation_own_request_status.py`.

## Strict tool schemas (no `null` in an `enum`)

A strict model gateway refuses a tool schema that holds `null` inside an
`enum`, and then refuses the whole tool list. 16 fields in 11 tools are
written as `{"type": ["string", "null"], "enum": [..., null]}`. There is no
switch. For every registered tool, the copy of the input schema and of the
output schema that `tools/list` sends has no `null` in an `enum`. The registry
tests stop a tool whose sent copy would have one. One output field has the
form: `delivery` of `call_operation`.

- `_wire_safe_input_schema` in `mcp/registry.py` walks the whole schema
  (`properties`, `items`, `$defs`, and so on) and drops `None` from each
  `enum` outside the parts of an input schema that stay as they are (the next
  bullet names them). Nothing else changes: a `type` list such as `["string", "null"]`
  stays. Values under `default`, `const` and `examples` are data and stay as
  they are. The members of an `enum` are data too: only a direct `null` member
  goes, and an object among the members stays as it is. An `enum` of only
  `null` becomes `const: null`, so the sent schema does not accept more than
  the server does. When it sits next to a `const` that is not `null`, that
  `const` stays and the `enum` becomes empty, so no value passes, the same as
  in the full schema. An `enum` without `null`, also an empty one, stays. No
  registered tool has these special forms.
- In an input schema some parts stay as they are, because there a smaller
  `enum` could make the sent schema accept a value that the server refuses:
  the schema under `not`, `if` and `oneOf`, under `contains` next to
  `maxContains`, and each schema these point to with `$ref` (also through
  another `$ref`). The whole schema stays as it is in three cases: such a
  `$ref` is not a plain local path; its path steps into a list with an index
  not written the plain way (only ASCII digits with no leading zero, sign or
  space count as an index); or a schema object below the top has an `$id`.
  The rewrite never fails on the form of an index. A `$ref`
  or `$id` key does not count in two places only: inside the value of
  `default`, `const`, `enum`, `examples` or `example`, and as a key under
  `properties`, `patternProperties`, `$defs`, `definitions`,
  `dependentSchemas`, `dependentRequired`, `dependencies` or `$vocabulary`
  (there a key is a name). Every other `$ref` or `$id` key counts. The
  registry test shows any tool whose sent schema still has `null` in an
  `enum`.
- `_wire_safe_output_schema` does the same walk for `outputSchema`, with one
  difference. MEHO answers `null` for `delivery`, and a client may check the
  answer against the sent schema (the MCP Python SDK does). So `null` is not
  just dropped. The rule `{"enum": [..., null]}` becomes
  `{"anyOf": [{"enum": [...]}, {"type": "null"}]}`, and the other keywords stay.
  An `anyOf` that the schema already has stays inside an `allOf`. This keeps
  the meaning, so the walk also rewrites the `enum` under `not`, `if`,
  `oneOf` and `contains`: the parts that stay as they are apply to the input
  schema only. The full `outputSchema` on the tool does not change.
- The server checks each call against `inputSchema`, not the wire copy. A
  call that sends `null` for such a field still passes, and the handler
  treats it like a missing field.
- MEHO's own hosted agent reuses `to_wire()`, so it gets the same copy.
- No tool is added, removed or renamed.
- Tests: `tests/test_mcp_strict_output_schemas.py` checks every kind of
  `call_operation` answer against the sent output schema.
  `tests/test_mcp_strict_tool_schemas.py` runs the real handler of
  each of the 11 tools with `null` and with the field left out. Only the call
  below the handler (service or database) is a stand-in.

## Short tool texts (`MCP_SHORT_TOOL_TEXTS`, off by default)

The biggest tools on the default list have a short text. A session sees it
when the `MCP_SHORT_TOOL_TEXTS` switch is on (Helm:
`config.mcpShortToolTexts`), or when the session asks for it (see "One
session asks for the short texts" below). The switch stays off until a
model test shows it helps: a small model scores better, and Claude does
not score worse.

- **Off (default):** every tool shows its full text, byte for byte as
  before, unless the session asks for the short texts.
- **On:** 17 tools show a short description and short input texts, in
  every session. The whole default list goes from 68,280 to 43,176
  characters (37% less). No tool sends an output schema (#4714, see
  "Lean tool profiles under 6,000 tokens" below). Nothing else changes.

### One session asks for the short texts (#6293)

A client adds `?texts=short` to the MCP URL, for example
`https://<meho-host>/mcp?texts=short`. That session then sees the short
texts, also while the switch is off. Other sessions keep the full texts.
This lets a person try the short texts in daily work without changing
them for everyone.

- The MCP route reads the `texts` URL parameter (`_apply_url_options`
  in `mcp/server.py`, `parse_tool_texts` in `mcp/short_texts.py`) and
  sets `mcp_short_tool_texts` on the request's `Operator`. Only this
  route sets it.
- `short_texts_shown(operator)` is true when the switch is on or the
  session asked. Every `for_settings` function gets the session's
  `Operator` (`tool_for_settings(definition, operator)` in
  `mcp/registry.py`). So `tools/list` and the `tools/call` input check
  use the same texts.
- No `texts`, or an empty `texts=`, gives the default texts. Any value
  other than `short` (not case-sensitive) is refused with HTTP 400
  before the body is read, like an unknown profile. There is no
  `texts=full`: with the switch on, every session sees the short texts.
- It works with a profile: `?profile=operate&texts=short`.
- The `tools/call` audit row of such a session has
  `"tool_texts": "short"` in its payload, so a measurement can tell these
  sessions apart. Other rows do not have the key.
- The request usage event of every request of such a session has
  `tool_texts = short` (column added by migration `0158`). With
  `AUDIT_WRAPPER_ROWS_RETIRED=true`, a read or `call_operation` writes no
  audit row, so this event is then the record of the choice. See
  [request-usage.md](request-usage.md).
- Like the profile, the parameter is read on each request. A client
  sends the same URL with every request, so the whole session sees the
  same texts.

What stays the same in both states:

- The tools on the list, in every profile and surface.
- The inputs each tool accepts. Only `description` strings change. Every
  property, type, enum, default, limit and `required` list stays, so the
  `tools/call` input check accepts and refuses the same calls.
- The annotations.
- The result of each call. The text block holds the same JSON.

What changes besides the texts: no tool sends an `outputSchema`, and so
no `tools/call` result has `structuredContent` (#4714).

What a short text keeps: what the tool is for, when to call it, its
arguments, and the rules that stop wrong calls. What it leaves out:
return-shape details the output schema or the answer already gives,
examples, and background. The full text stays the registered one, so
the reference docs and the hosted-run tools keep it.

How it works:

- `mcp/short_texts.py` has `ShortText` (a description, and short texts per
  input path such as `query.filter.value`; `None` drops an input's
  description) and `with_short_text(short, then=...)`. The result is the
  tool's `for_settings` function (see the section above).
- `then` is a tool's other `for_settings` step. It runs first. So with
  `KB_SIMILAR_CHECK` off, `add_to_knowledge` still has no `create_anyway`
  input. The short text of the similar-entry check sits on
  `create_anyway`, so it shows only while that input exists.
- The short text keeps the tool's maturity label (`[beta]`).
- The texts are in `mcp/tools/_short_tool_texts.py`.

| Tool | Full | Short |
| --- | ---: | ---: |
| `query_topology` | 4,655 | 3,191 |
| `meho_runbook_next` | 3,791 | 1,378 |
| `add_to_knowledge` | 3,726 | 1,452 |
| `result_query` | 3,491 | 2,496 |
| `preview_operation` | 3,377 | 1,341 |
| `read_docs` | 3,351 | 1,631 |
| `search_operations` | 3,309 | 1,486 |
| `meho_broadcast_recent` | 3,234 | 1,791 |
| `call_operation` | 3,212 | 2,075 |
| `meho_broadcast_announce` | 3,181 | 1,926 |
| `search_knowledge` | 3,013 | 2,142 |
| `list_targets` | 2,945 | 1,116 |
| `search_docs` | 2,943 | 1,563 |
| `meho_broadcast_watch` | 2,701 | 1,543 |
| `search_memory` | 2,428 | 1,714 |
| `list_operation_groups` | 2,300 | 840 |
| `meho_connector_list` | 1,898 | 865 |

Sizes are characters as `tests/test_mcp_tool_text_budget.py` counts them
(name, description and input schema), with the similar-entry check off.
The first ten are the ten biggest tools. The next four are needed to
reach 30% less. The last three are in both tool profiles; they bring the
profiles under 6,000 tokens. `query_topology` and `result_query` stay
larger because their input schemas are large.

### Lean tool profiles under 6,000 tokens (#4714)

A session with a tool profile and the short texts
(`?profile=investigate&texts=short`, or `?profile=operate&texts=short`)
gets a lean tool list. Each lean profile is under 6,000 tokens, and
`query_topology` is under 6,000 bytes.

What the model sees depends on the client. A client turns each MCP tool
into a tool for the model: name, description and input schema. The
Claude API and the OpenAI-style tool format have no field for an output
schema, so most clients do not show it to the model. A client may still
put it in the context in another way. The size check therefore counts
the whole `tools/list` entry, output schema included.

So the short texts also leave out every `outputSchema`. The MCP spec
makes the output schema optional. A tool that declares one must return
`structuredContent`, or Claude Desktop fails the call (#2774). The
`tools/call` handler uses the tool as the session lists it
(`tool_for_settings` in `mcp/registry.py`), so a session without output
schemas also gets no `structuredContent`. The text block still holds the
whole JSON result. Claude Desktop and Claude Code read it as before.

Sizes are UTF-8 bytes of the whole `tools/list` entry as compact JSON.
"Before" is the full text, "after" the short text.

| Tool | Before | After |
| --- | ---: | ---: |
| `query_topology` | 13,034 | 3,272 |
| `search_operations` | 6,658 | 1,568 |
| `preview_operation` | 5,933 | 1,425 |
| `result_query` | 5,364 | 2,585 |
| `call_operation` | 4,683 | 2,129 |
| `list_targets` | 4,429 | 1,195 |
| `search_memory` | 3,741 | 1,793 |
| `list_operation_groups` | 3,560 | 919 |
| `meho_broadcast_recent` | 3,255 | 1,886 |
| `meho_broadcast_announce` | 3,216 | 1,955 |
| `search_knowledge` | 3,092 | 2,221 |
| `meho_broadcast_watch` | 2,722 | 1,637 |
| `meho_connector_list` | 1,981 | 944 |
| `meho_status` | 986 | 986 |

| List | Bytes before | Bytes after | Tokens after (Qwen2.5 / o200k / cl100k) |
| --- | ---: | ---: | ---: |
| `investigate` (9 tools) | 34,494 | 14,340 | 3,503 / 3,511 / 3,449 |
| `operate` (13 tools) | 49,620 | 21,243 | 5,199 / 5,177 / 5,093 |
| Full default list (26 tools) | 92,106 | 45,132 | 10,961 / 10,916 / 10,753 |

The tokens are counted on the list as one compact JSON text. The byte
count divided by 4 is a little higher than every tokenizer count.
`tests/test_mcp_tool_text_budget.py` fails when a lean profile is over
6,000 tokens of the test model (Qwen2.5), over 24,000 bytes (6,000
tokens at 4 bytes per token), or when `query_topology` is over 6,000
bytes. The test downloads the Qwen2.5 tokenizer file from Hugging Face at
a fixed revision and checks its SHA-256. In CI a failed download fails
the test; without network on a local machine only the token test is
skipped.

Tests:

- `tests/test_mcp_short_tool_texts.py`: with the switch off, every listed
  tool equals the tool as listed before; with it on, each changed tool is
  shorter and takes the same inputs, and no other tool changes.
- `tests/test_mcp_tool_text_budget.py`: with the switch on, the whole
  default list is at most 47,000 characters and at least 30% smaller;
  each tool profile is at most 6,000 tokens of the test model and at most
  24,000 bytes; `query_topology` is at most 6,000 bytes.
- `tests/test_mcp_structured_content.py`: with the switch on, `tools/list`
  has no output schema and `tools/call` returns no `structuredContent`.
- `tests/test_mcp_surface_conformance.py` runs every test in both states.
- `tests/test_mcp_tool_profiles.py`: a profile shows the short texts only
  with the switch on.
- `tests/test_mcp_short_tool_texts_session.py`: `?texts=short` gives one
  session the short texts while the switch is off; two sessions at the
  same time each see their own texts; `tools/call` uses the same texts
  and its audit row names the choice; the usage event names the choice
  also when no audit row is written; an unknown value is refused.
- `/ready` shows the switch as `features.mcp.short_tool_texts`.

How to measure it: run the same benchmark task set twice against one
build, once with `MCP_SHORT_TOOL_TEXTS=false` and once with `true`, with a
small model and with Claude. `/ready` tells which state a run used.
Compare the task scores and the tokens used before the first answer. Do
it for the full list and for each tool profile (`?profile=investigate`,
`?profile=operate`). Then one live run on the sandbox MEHO with
`?profile=operate&texts=short` through Claude Desktop and Claude Code:
call every tool of the profile once and check that no call fails.

## A guide to the call flow in the session-start text (`MCP_CALL_FLOW_GUIDE`, off by default, #4715)

With the `MCP_CALL_FLOW_GUIDE` switch on, the session-start text (the
`instructions` of `initialize`) starts with a short guide. It explains
once, in order, how to find and call an operation:

1. connectors and targets (`meho_connector_list`, `list_targets`; a
   target is named, not given by its id);
2. operation groups (`list_operation_groups`);
3. search, then the exact contract (`search_operations` with `query`,
   then with `op_id`);
4. preview before a change (`preview_operation`);
5. the call (`call_operation`);
6. large results come back as a handle, read with `result_query`;
7. a call that waits for a person's approval: `status` is
   `awaiting_approval`, nothing has run, do not call again. A call with
   `reveal_secret: true` is different: the approval runs nothing. With
   `MCP_REVEAL_FETCH` on, the guide says to call `call_operation` again
   with `approval_request_id` to get the value. With it off, the guide
   says the person who asked gets the value in the console or on the
   command line.

- **Off (default):** the session-start text does not change.
- **On:** the guide comes first, before the other bands. The full guide
  is about 235 words, 434 tokens by the same estimate the
  `mcp_preamble_receipt` log uses (`estimate_tokens`), and 446 tokens with
  `MCP_REVEAL_FETCH` on. A test keeps both at 450 or less.
- The text is the same for every client. It names only tools the
  session can see (`all_tools_for`, the filter `tools/list` uses). A step
  whose tool is not on the list is left out, and the steps are numbered
  again. For example, the `investigate` profile has no preview step. A
  session without `call_operation` gets no guide.
- A client may ignore `instructions`. So each tool description still
  carries the rules a correct call needs. The guide adds the overview.
- Code: `mcp/call_flow_guide.py`, used by `_session_call_flow_guide` in
  `mcp/server.py`. Tests: `tests/test_mcp_call_flow_guide.py`.

How to measure it: run the same benchmark task set twice against one
build, once with `MCP_CALL_FLOW_GUIDE=false` and once with `true`, with a
small model and with Claude. Compare the steps and the tokens to the
first correct call. Then do one live run on the sandbox MEHO with the
switch on. The switch goes on by default only in a later pull request
that links this measurement.

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
* `add_to_knowledge.expected_revision` — a UUID string, the
  `revision_id` that a read of the entry returned. Every update names
  it; a write without it only creates, and a slug that exists is
  refused with `-32602` and `data.error = "stale_revision"` (the
  current revision, who changed it and when, and what changed, with
  stored text inside the untrusted-text envelope). See
  [`kb.md`](./kb.md#every-update-names-the-revision-it-read-4947).
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
