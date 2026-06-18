# `ui/routes/agents/run` — the agent run console + live SSE bridge

Initiative [#1824](https://github.com/evoila/meho/issues/1824) (G10.8
Agents console), Task [#1829](https://github.com/evoila/meho/issues/1829)
(T2). The run console lets an operator invoke a defined agent from the
browser and watch it reason **live** — `turn` / `tool_call` /
`tool_result` / `final` / `error` frames stream into a transcript pane as
the run executes. It sits under the `/ui/agents` surface that Task #1825
(T1) scaffolded.

## Overview

Three routes, all under the per-agent `/run` sub-path:

| Method · path | Role | Purpose |
|---|---|---|
| `GET /ui/agents/{name}/run` | operator (any authenticated session reaches the page) | The console page: run form (prompt + optional `work_ref`) + the transcript mount. |
| `POST /ui/agents/{name}/run` | operator | The CSRF-gated run-initiation action. Validates the prompt, confirms the agent is runnable (404 / 409 / 429 surface here), mints a run token, returns the transcript fragment. |
| `GET /ui/agents/{name}/run/stream?token=…` | operator | The cookie-authed SSE bridge: verifies the token, lifts the operator, proxies `invoker.stream_events`. |

The console ships **without** a Stop button. "Stop watching" merely closes
the `EventSource`; it does not cancel the run. The operator run-cancel
backend (T8 [#1828](https://github.com/evoila/meho/issues/1828)) and its
Stop button (T9 [#1833](https://github.com/evoila/meho/issues/1833)) are
separate Tasks.

## Why a cookie-authed GET SSE bridge

The canonical streaming run path is `POST /api/v1/agents/{name}/run/events`
(`api/v1/agent_runs.py`) — a `POST` authenticated by the
`Authorization: Bearer <jwt>` header. The browser's `EventSource` (the
only browser primitive that speaks Server-Sent Events) issues a **GET**
with **no custom headers**: it cannot send a JWT header and it cannot POST
a body. So the console needs a cookie-authed GET bridge under `/ui/` that
the chassis `UISessionMiddleware` gates with the BFF session cookie — the
same shape `ui/routes/broadcast/stream.py` established for the activity
feed.

## Control flow

```
operator presses Run
  └─ POST /ui/agents/{name}/run  (CSRF double-submit gate, operator role)
       ├─ validate prompt (reject blank); strip work_ref
       ├─ invoker.ensure_runnable(operator, name)
       │     ├─ AgentNotFoundError  → inline 404 alert
       │     ├─ AgentDisabledError  → inline 409 alert
       │     └─ BudgetExceededError → inline 429 alert (reason shown)
       ├─ mint_run_token(session_id, name, input, work_ref)   [HMAC, 120 s TTL]
       └─ return _run_transcript.html  (sse-connect → the bridge, token in query)

browser EventSource opens GET /ui/agents/{name}/run/stream?token=…
  └─ verify_run_token(session_id from cookie, token)
       ├─ bad / expired / cross-session / name-mismatch → 403 (no stream)
       └─ ok → invoker.ensure_runnable (re-check; race guard)
                 └─ StreamingResponse over invoker.stream_events(operator, …)
                       one SSE frame per event, X-Accel-Buffering: no
```

## Tenant isolation

The broadcast bridge keys its Valkey stream by the session's tenant
(`meho:feed:{tenant_id}`); a run stream has no Valkey stream — it drives a
*fresh* run inline. The tenant-isolation lever here is the **lifted
operator**: `AgentInvoker.stream_events(operator, name, inputs)` loads only
the operator's own tenant's definition and records the run under that
tenant, identical to the REST surface. The operator is lifted from the
validated BFF session (`resolve_run_operator_or_403`), never from a
request parameter, so a crafted request cannot redirect the run to another
tenant's agent.

## The run-handoff token

Starting a run executes real tool-calls against live targets and incurs
provider cost. The console splits the flow so the **state-changing** half
stays behind the chassis CSRF double-submit gate, which exempts
safe-method GETs — and `EventSource` can only GET. A bare
`GET …/run/stream?input=…` would let any same-session GET start a run,
bypassing CSRF (the residual same-site vector the chassis CSRF design
closes).

`run_token.py` mints a signed, short-lived token binding
`(session_id, agent_name, input, work_ref, exp)`. Shape mirrors the CSRF
helper exactly (zero new deps):

```
token = b64url(payload_json) + "." + hmac_sha256_hex(secret, b64url(payload_json))
```

The HMAC secret reuses `Settings.ui_session_encryption_key` (the same key
the CSRF tokens and the session-store Fernet use). The bridge re-derives
the session id from the cookie and rejects a token whose embedded `sid`,
agent `name`, signature, or `exp` does not check out — so a leaked /
replayed / tampered token yields no stream. The token is **not** a
capability that widens access: the bridge still re-lifts the operator and
re-runs the same tenant-scoped invoker call. The run prompt comes from the
*token* (authorised by the CSRF-gated POST), not the query string, so a
tampered query cannot change what runs.

## Live streaming

SSE buffering [#1389](https://github.com/evoila/meho/issues/1389) is
**fixed** and the bridge sets `X-Accel-Buffering: no`, so frames flush
per-event and the transcript paints turn-by-turn rather than only at
completion. The bridge delegates frame formatting to the REST surface's
`_events_generator`, so the UI bridge and the REST SSE route emit
byte-identical frames (`event: <kind>` / `data: {run_id, …}`).

Client side, `static/src/app/agent-run-console.js` registers the
`agentRunConsole` Alpine controller on `alpine:init`. The HTMX `sse`
extension owns the `EventSource`; the controller hooks
`htmx:sse-before-message` for each of the five run-event kinds
(`sse-swap="turn,tool_call,tool_result,final,error"`), `preventDefault()`s
the raw swap (so a frame field containing markup is never parsed into live
DOM — the same injection class PR #1044 closed), parses the JSON, and
appends a typed transcript entry rendered via `x-text` (inert). It tracks
the `run_id` (a deep-link to run detail, T3), the terminal status pill, and
the `awaiting_approval` pause (which deep-links to `/ui/approvals`, T7,
rather than re-implementing decide).

## Key types

- `ui/routes/agents/run.py` — `render_run_console`, `submit_run`,
  `stream_run_events`, `_bridge_generator`.
- `ui/routes/agents/run_token.py` — `mint_run_token`, `verify_run_token`,
  `DecodedRunToken`, `RUN_TOKEN_TTL_SECONDS`.
- `ui/routes/agents/operator.py` — `resolve_run_operator_or_403` (the
  operator-floor gate for the run POST + bridge; distinct from the
  tenant_admin-only `resolve_operator_or_403` the definition-CRUD writes
  use).
- Templates: `agents/run_console.html`, `agents/_run_console_body.html`,
  `agents/_run_transcript.html`, `agents/_run_error.html`.

## Dependencies

- `agent/invocation.py` — `AgentInvoker.ensure_runnable` +
  `AgentInvoker.stream_events` (the generator the bridge proxies).
- `api/v1/agent_runs.py` — `AgentRunRequest` (prompt validation) +
  `_events_generator` (frame formatting reuse).
- `ui/csrf.py` — the double-submit middleware that gates the run POST.
- `ui/auth/middleware.py` — `UISessionMiddleware` / `require_ui_session`.

## Known issues / out of scope

- No operator run-cancel from this surface (Stop button is T9 over the T8
  backend).
- Cross-agent run history (work_ref / status filters, poll-after-the-fact)
  is T3 [#1830](https://github.com/evoila/meho/issues/1830); the
  transcript's `run_id` deep-links to a run-detail page T3 owns.
- The `awaiting_approval` banner is surfaced if a frame ever stamps an
  `awaiting_approval` status; the decide path itself stays on
  `/ui/approvals` (T7).
