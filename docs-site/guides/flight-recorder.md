# The flight recorder

The [audit ledger](audit-forensics.md) answers *"who did X to Y, and
when?"* — one durable row per operation. The **flight recorder** answers
the next question down: *"what did the connector actually send, and what
came back?"* For a governed dispatch, it captures the real vendor
request/response traffic as an ordered set of **spans** and attaches them
to that dispatch's audit row. The audit row stays the slim, append-only
record of account; the trace is the deeper detail hanging off it, and it
is captured with secrets stripped so it is safe to read back.

This guide covers what a trace records, how redaction keeps it safe, who
can read it (operators and the agent itself), and how an operator turns
capture on — because it is **off by default**.

!!! note "Prerequisites, roles, and maturity"

    - A running backplane and a connected client
      ([Connect clients](../clients/index.md)).
    - Reading a trace needs the **operator** role. Turning capture on or
      off — a per-tenant policy or a per-target override — is a
      governance decision and needs **tenant_admin**.
    - The flight recorder extends the append-only
      [audit ledger](audit-forensics.md) (which is **GA**). Capture
      itself is an operator opt-in: nothing is recorded until you enable
      it, and it can be turned off globally at any time.

## What a trace records

Each captured dispatch produces one trace: an ordered list of spans, one
per meaningful step the backplane took. Three kinds of span are
instrumented, on the **same** single dispatch path the operation already
runs — there is no parallel execution path:

| Span | What it captures |
|---|---|
| **Vendor call** | the outbound request a connector made — method, URL (stripped to its request line so query-string and userinfo secrets never leak), status code, duration, and the **redacted, capped** request/response headers and bodies. Typed connectors are instrumented alongside generic ones. |
| **Composite sub-step** | a composite that fans out into child dispatches joins the **one** parent trace, so a multi-step operation reads as a single ordered story rather than several disconnected traces. |
| **JSONFlux reduction** | how a set-shaped response was reduced: input rows, kept fields, output size, and the result-handle id. |

Traces are bounded so a chatty operation can never blow up storage or a
reader's context. Each span body is capped (oversize **truncates with a
marker**, it never errors), each trace is capped in total size, and a
dispatch with very many spans collapses the overflow into counted
per-kind groups. Capture is also **best-effort by construction**: it can
never fail, block, or materially slow a dispatch, and the trace is
persisted only after the audit row has committed. If recording breaks,
the operation still returns exactly as it would have.

## Redaction is fail-closed

A trace is only safe to read back because a secret never reaches it in
the first place. Every header and body is redacted **at capture**, before
a byte is stored, and the engine is built to fail closed:

- **Header allowlist.** Only enumerated known-safe headers survive.
  Every `Authorization`, `Cookie`, `Set-Cookie`, `X-*-Token`, CSRF,
  session, and signed-URL header is stripped **unread**. An allowlist is
  used deliberately over a blocklist, so an unknown vendor header is
  dropped rather than leaked.
- **Body-path redaction.** Declarative per-connector path rules scrub
  known secret-bearing fields from request and response bodies, with the
  credential-shape detector reused underneath as a second layer.
- **Hard-excluded operation families.** Credential-read, session-mint,
  and token operations never record bodies at all, regardless of policy.
- **Redaction-uncertainty signalling.** Any span the engine cannot
  *prove* it fully redacted — an unparseable or binary body, malformed
  JSON, a body truncated mid-value — is flagged uncertain, and the whole
  trace inherits that flag. Uncertainty always resolves toward **less**
  exposure, never more (see the read surfaces below).

## Who reads a trace, and how

### Operators — REST and the console

An operator reads the trace for one audit row two ways over the same
tenant-scoped read:

```bash
# The audit row id comes from the audit ledger (see the audit guide).
curl -H "Authorization: Bearer $TOKEN" \
  https://meho.example.com/api/v1/audit/<audit_id>/trace
```

…or open the audit row in the console drawer and read its **Flight
recorder** pane (next to Lineage). Both render exactly what was stored —
the read surface never re-processes, un-redacts, or writes. Two things
worth knowing:

- **The operator plane sees everything the tenant captured**, including
  a redaction-uncertain trace — but the uncertainty is **surfaced**, as a
  banner in the pane and a `redaction_uncertain` field in the REST body,
  so you can see when a trace was withheld from agents.
- **Absence is unambiguous.** An audit row that does not exist, or lives
  in another tenant, returns `404` (never `403` — existence never leaks
  across tenants). An audit row that exists but has no captured trace —
  because capture was off, or best-effort recording skipped it — returns
  a `200` empty state.

### The agent — its own trace as a result handle

An agent can read the trace of a dispatch it ran, but only through the
narrow-waist idiom it already uses for any large result: the trace is
materialized as a set-shaped result handle and paged with the
**unchanged** `result_query` meta-tool. No new tool is registered, no
vendor-specific name reaches the agent surface, and no raw payload enters
agent context.

!!! warning "The agent read is gated separately, and closes on doubt"

    Agent readability is a **per-tenant** setting independent of operator
    access, and it is the conservative default. A trace whose redaction
    could not be *proven* complete is withheld from the agent handle
    **entirely** — while the operator plane keeps full access to it. A
    secret-bearing or redaction-uncertain span therefore never reaches an
    agent-visible handle.

## Turning capture on

Capture is a governance decision, so it lives on the operator plane —
**REST and CLI only, with no MCP tool** — and it is off until an operator
enables it. Resolution runs per-target first, then per-tenant, then the
global default.

**Per-tenant policy** (`tenant_admin`):

```bash
meho tenants flight-recorder-policy set \
  --enabled \
  --agent-readable true \
  --retention-days 14
```

- `--enabled` flips the tenant's capture default on or off.
- `--agent-readable true|false|inherit` controls whether agents may read
  their own traces (`inherit` follows the capture default).
- `--retention-days N` (bounded **1–365**) or `--clear-retention` sets
  how long traces are kept before the reaper deletes them.

**Per-target override.** A single target can force capture on or off
regardless of the tenant default — useful to record just one noisy or
sensitive system — through the target's own update path (the
`flight_recorder_capture` field on `meho targets import --update`, or the
target PATCH route). It is tri-state: force on, force off, or clear back
to inherit.

**Global kill switch.** `FLIGHT_RECORDER_ENABLED` is read fail-open: set
it off and the deployment captures nothing, without failing a single
dispatch. It is the blunt instrument for turning the whole subsystem off.

A policy change takes effect on the **next dispatch** — the resolver
caches for a minute but each change invalidates that cache, so you never
wait out a TTL or restart a pod.

## Retention

Traces are transient by design. A reaper deletes expired traces —
headers and spans together — on a bounded sweep; the default window is
short and per-tenant configurable through `--retention-days` above. The
**audit row is never touched** by the reaper: the durable record of
account outlives the detailed trace hanging off it.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| `GET …/trace` returns `200` but an empty trace | Capture was off for that dispatch, or best-effort recording skipped it — not an error. | Enable capture on the tenant (or the target) and re-run; past dispatches cannot be recorded retroactively. |
| `GET …/trace` returns `404` | The audit row does not exist in your tenant (a wrong id, or a cross-tenant row) — deliberately indistinguishable from a genuinely missing row. | Confirm the `audit_id` and that you are authenticated to the right tenant. |
| An operator sees a trace but an agent's `result_query` cannot | Either agent-readability is off for the tenant, or the trace was flagged redaction-uncertain and withheld from agents by design. | Check `--agent-readable`; a redaction-uncertain trace stays operator-only on purpose. |
| A span body ends in a truncation marker | The body exceeded the per-span cap and was truncated — working as designed, and it forces the trace's redaction-uncertain flag. | Nothing; the trace is intact up to the cap, and the truncation is why it is operator-only. |
| Nothing is ever captured, tenant policy notwithstanding | The global `FLIGHT_RECORDER_ENABLED` kill switch is off. | Set it on at the deployment level; the per-tenant policy applies underneath it. |

**Next:** [Runbooks](runbooks.md).
