# `ui/routes/agents/run` — the agent run console + live SSE bridge

Initiative [#1824](https://github.com/evoila/meho/issues/1824) (G10.8
Agents console), Task [#1829](https://github.com/evoila/meho/issues/1829)
(T2). The run console lets an operator invoke a defined agent from the
browser and watch it reason **live** — `turn` / `tool_call` /
`tool_result` / `final` / `error` frames stream into a transcript pane as
the run executes. It sits under the `/ui/agents` surface that Task #1825
(T1) scaffolded.

## Overview

Four routes under the per-agent `/run` sub-path, and one under
`/ui/agents/runs`:

| Method · path | Role | Purpose |
|---|---|---|
| `GET /ui/agents/{name}/run` | operator (`require_ui_operator`; a `read_only` session gets 403) | The console page: run form (prompt + optional `work_ref`) + the transcript mount. With `?run=<id>` it shows that run: its saved events, then the live ones. |
| `POST /ui/agents/{name}/run` | operator | The CSRF-gated run-initiation action. Validates the prompt, confirms the agent is runnable (404 / 409 / 429 surface here), mints a run token, returns the transcript fragment. |
| `GET /ui/agents/{name}/run/stream?token=…` | operator | The cookie-authed SSE bridge: verifies the token, records its first use (a repeated GET gets 204 and starts no run), lifts the operator, starts the run in the background and streams its saved events. |
| `POST /ui/agents/{name}/run/{handle}/cancel` | operator | The Stop button's CSRF-gated cancel proxy (T9 [#1833](https://github.com/evoila/meho/issues/1833)): lifts the operator and drives `invoker.cancel` — the same service path the REST cancel route uses. 204 / 404 / 409 / 403. |
| `GET /ui/agents/runs/{handle}/events` | operator (`resolve_run_reader`) | The run's saved events as SSE: the events so far, then each new one, then `end`. 404 for another tenant's run. |

The console carries a **Stop button** (T9
[#1833](https://github.com/evoila/meho/issues/1833)) over the operator
run-cancel backend (T8
[#1828](https://github.com/evoila/meho/issues/1828)). Because
`EventSource` cannot carry a JWT, the browser holds a cookie session, not
the Bearer token the REST cancel route
(`POST /api/v1/agents/runs/{handle}/cancel`) requires — so the Stop button
posts to a cookie-authed BFF proxy, `POST /ui/agents/{name}/run/{handle}/cancel`,
which lifts the operator (operator floor) and drives the *same* in-process
`invoker.cancel` the REST route does. The `run_id` it cancels is the one
the SSE stream surfaces (the Alpine controller learns it from the first
frame's `run_id`), so the affordance only appears once a run is live and
vanishes the moment a terminal frame lands (a terminal run is not
cancellable — the proxy would 409 it). The cancel POST is CSRF-double-
submit-gated; the Alpine controller echoes the token in the `X-CSRF-Token`
header explicitly because the page-level `hx-headers` directive is an HTMX
construct and is **not** inherited by an Alpine `fetch`. Outcomes map to
inline feedback: 409 (already terminal) transitions the console to
`cancelled` silently, 404 means the run no longer exists, 403 means the
operator may not cancel it.

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
       ├─ mint_run_token(session_id, name, input, work_ref)   [Fernet, 120 s TTL]
       └─ return _run_transcript.html  (sse-connect → the bridge, token in query)

browser EventSource opens GET /ui/agents/{name}/run/stream?token=…
  └─ verify_run_token(session_id from cookie, token)
       ├─ bad / expired / cross-session / name-mismatch → 403 (no stream)
       └─ ok → claim_run_token(token id)   [Valkey SET NX EX 120 s]
                 ├─ already used → 204 (no run; EventSource stops reconnecting)
                 ├─ Valkey error → 503 (no run)
                 └─ first use → invoker.ensure_runnable (re-check; race guard)
                 └─ invoker.run(operator, …, async_mode=True)   [background run]
                 └─ StreamingResponse over tail_frames(operator, run_id)
                       the run's saved events, one SSE frame each,
                       then `end`; X-Accel-Buffering: no

browser reloads the page (the address now has ?run=<id>)
  └─ GET /ui/agents/{name}/run?run=<id> → transcript bound to
       GET /ui/agents/runs/{id}/events → the saved events, then the live ones
```

## Saved events, reload and `end`

The run saves each event in the session store while it runs
(`docs/codebase/session-store.md`, "Hosted agent runs"). The console does
not get the events from the run directly: it reads them from the store
(`ui/routes/agents/runs/events.py`, `tail_frames`). It reads the events
after the last one it sent, every 0.5 s, until the run reaches its end
status.

- **The run goes on when the page closes.** The bridge starts the run in
  the background (`invoker.run(..., async_mode=True)`), so closing the
  page or losing the connection does not stop it. The Stop button stops
  it.
- **Reload.** Once the run id is known, the controller puts `?run=<id>`
  in the page address (`history.replaceState`). A reload then opens
  `GET /ui/agents/runs/{id}/events`: the events so far, then the live
  ones. A visit days later shows the whole run.
- **No gaps, no doubles.** Each frame has `id: <seq>`. One run has one
  writer, so the events commit in `seq` order. When the browser's
  `EventSource` connects again, it sends `Last-Event-ID`, and the stream
  goes on after that event.
- **`end`.** The last frame is `event: end` with `{status, state,
  last_seq}`. `state` is `ended` when the run saved its final (or error)
  event, and `unfinished` when it stopped without one (a crash, a restart,
  a lost lease or a cancel). The console then shows "unfinished" and the
  last saved event. It never makes up an ending.
- **Hidden data.** A frame has `withheld: true` instead of the data when
  the reader may not read the run's data (a direct run another person
  started), and `quarantined: true` when the secret check was not sure
  and the reader is not a tenant admin.
- **Audit.** Opening a stream writes one `sessions.run_events.read`
  audit row. The reads while the stream is open write none.

The REST route `POST /api/v1/agents/{name}/run/events` still runs the agent
inline in the request; it saves the same events, and a client that closes
the stream stops that run.

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

`run_token.py` mints an encrypted, short-lived token binding
`(session, agent_name, input, work_ref)`. It is a Fernet token (zero new
deps; the same primitive the session store uses):

```
token = fernet.encrypt_at_time(json({"id", "sd", "name", "input", "work_ref"}), now)
```

- `id` is a random value that names this one token.

- The Fernet key is `Settings.ui_session_encryption_key` (the same key
  the CSRF tokens and the session store use).
- The token is opaque. The run prompt cannot be read from the stream URL.
- `sd` is a keyed HMAC-SHA256 digest of the console session id. The
  session id itself is never in the token, so the stream URL holds no
  value that works as a session cookie.
- The bridge decrypts with a 120 s TTL, recomputes the digest from the
  cookie session, and checks the agent `name`. A token from another
  session, another agent, or older than the TTL yields no stream (403).

**One token starts one run.** On the first accepted GET the bridge
records the token `id` in Valkey (`claim_run_token`: `SET
meho:ui:agent-run-token:<id> 1 NX EX 120`). The key is written only
when it is absent, so one GET wins, across all workers. Every later GET
with the same token gets `204 No Content` and starts nothing. `204` is
the status that tells a browser `EventSource` to stop reconnecting. When
Valkey cannot record the use, the bridge answers `503` and starts no run.

The token is **not** a capability that widens access: the bridge still
re-lifts the operator from the cookie session and re-runs the same
tenant-scoped invoker call. The run prompt comes from the *token*
(authorised by the CSRF-gated POST), not the query string, so a tampered
query cannot change what runs.

## Live streaming

SSE buffering [#1389](https://github.com/evoila/meho/issues/1389) is
**fixed** and the bridge sets `X-Accel-Buffering: no`, so frames flush
per-event and the transcript paints turn-by-turn rather than only at
completion. The frames have the shape of the REST SSE route
(`event: <kind>` / `data: {run_id, …}`), plus `id: <seq>` and a `seq`
field in the data.

Client side, `static/src/app/agent-run-console.js` registers the
`agentRunConsole` Alpine controller on `alpine:init`. The HTMX `sse`
extension owns the `EventSource`; the controller hooks
`htmx:sse-before-message` for each of the five run-event kinds and `end`
(`sse-swap="turn,tool_call,tool_result,final,error,end"`), `preventDefault()`s
the raw swap (so a frame field containing markup is never parsed into live
DOM — the same injection class PR #1044 closed), parses the JSON, and
appends a typed transcript entry rendered via `x-text` (inert). It tracks
the `run_id` (a deep-link to run detail, T3), the terminal status pill, and
the `awaiting_approval` pause (which deep-links to `/ui/approvals`, T7,
rather than re-implementing decide).

The controller closes the stream when a `final`, `error` or `end` frame
arrives (`closeStream`). Two things would reconnect otherwise: the browser's
`EventSource` reconnects by itself when the server ends the stream, and
the sse extension (2.2.4) opens a new `EventSource` after a closed one
errors. `closeStream` removes the `sse-connect` attribute first (the
extension's reconnect timer reads it and stops when it is gone), then
calls `close()` on the `EventSource`. The token starts one run only: a
reconnect to the bridge gets `204`. A reload follows the run through
`?run=<id>`.

## Key types

- `ui/routes/agents/run.py` — `render_run_console`, `submit_run`,
  `stream_run_events`, `_start_run`.
- `ui/routes/agents/runs/events.py` — `tail_frames`, `first_read`,
  `event_frame`, the `GET /ui/agents/runs/{handle}/events` route.
- `agent/run_events.py` — `read_run_events` (the store read, with the
  audit row and the visibility rules).
- `ui/routes/agents/run_token.py` — `mint_run_token`, `verify_run_token`,
  `claim_run_token`, `DecodedRunToken`, `RUN_TOKEN_TTL_SECONDS`.
- `ui/routes/agents/operator.py` — `resolve_run_operator_or_403` (the
  operator-floor gate for the run POST + bridge; distinct from the
  tenant_admin-only `resolve_operator_or_403` the definition-CRUD writes
  use).
- Templates: `agents/run_console.html`, `agents/_run_console_body.html`,
  `agents/_run_transcript.html`, `agents/_run_error.html`.

## Dependencies

- `agent/invocation.py` — `AgentInvoker.ensure_runnable` +
  `AgentInvoker.run` (the background run the bridge starts).
- `api/v1/agent_runs.py` — `AgentRunRequest` (prompt validation) +
  `_bound_work_ref` (binds the run's `work_ref`).
- `session_store/hosted_runs.py` — where the run saves its events.
- `agent/invocation.py` — `AgentInvoker.cancel` (the cancel proxy's
  service path); `operations.agent_run` — `IllegalTransitionError` (409) /
  `UnauthorizedCancellationError` (403) mapped by the cancel proxy.
- `ui/csrf.py` — the double-submit middleware that gates the run POST and
  the cancel POST.
- `ui/auth/middleware.py` — `UISessionMiddleware` / `require_ui_session`.
- `broadcast/client.py` — `get_broadcast_client` (the Valkey client that
  holds the single-use record of each run token).

## Known issues / out of scope

- The cancel records the durable `cancelled` state (via `invoker.cancel`)
  and stops the run's loop: the loop task is cancelled at once on the
  worker that runs it, and a loop on another worker stops when its
  heartbeat finds the lease gone. The console flips to the `cancelled`
  pill and closes the `EventSource` immediately.
- The run list has no cancel button. Only the run console can stop a
  run.
- The run list for all agents is `GET /ui/agents/runs`, with `status`
  and `work_ref` filters (`ui/routes/agents/runs/list_view.py`). The
  transcript's `run_id` links to the run detail page
  `GET /ui/agents/runs/{handle}` (`ui/routes/agents/runs/detail.py`).
  That page lists the run's saved events and shows "unfinished" for a run
  that stopped without a final event. A live run links back to the
  console with `?run=<id>`.
- The stream reads the store every 0.5 s, so a live event shows up to
  half a second late.
- The `awaiting_approval` banner is surfaced if a frame ever stamps an
  `awaiting_approval` status; the decide path itself stays on
  `/ui/approvals` (T7).
