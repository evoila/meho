# Dispatch flight recorder: per-dispatch request/response traces, operator-plane by default and agent-readable only through the narrow waist (decision)

**Status:** decided — operator determination of record (2026-08-30/31);
establishes the security-review-first design for per-dispatch trace capture
**Date:** 2026-08-30
**Goal:** operator-facing run transparency — an operator (and a paired add-on's
run UI) must be able to see, for one governed dispatch, the actual vendor
request/response traffic and how the backplane processed it, without ever
exposing that traffic to the agent narrow waist as raw payload
**Initiative:** [#3207](https://github.com/evoila/meho/issues/3207) — dispatch
flight recorder, security-review-first design (this record **is** that review;
capture code is filed as sibling Tasks, unimplemented)
**Security posture:** security-review-first — the #2901 posture. No capture code
lands before this decision; response bodies can carry credentials, session
tokens, and PII, so redaction/caps/retention/access/exclusions are fixed here
first.
**Composes with:**
[#2901/#3187](https://github.com/evoila/meho/issues/2901)
([docs/decisions/satellite-write-path.md](satellite-write-path.md)),
[#3183/#3195](https://github.com/evoila/meho/issues/3183)
([docs/decisions/governed-delete-operations.md](governed-delete-operations.md)),
audit parent-linkage ([#3028](https://github.com/evoila/meho/issues/3028)),
async governed dispatch ([#3079](https://github.com/evoila/meho/issues/3079)).

## The determination

> A governed dispatch gains a **flight recorder**: an ordered set of **spans**
> attached to the dispatch's `audit_log` row id, capturing the vendor call(s) a
> connector made (method, URL, status, duration, and **redacted, capped**
> headers/bodies), the composite sub-steps, and the JSONFlux reduction
> (input size → kept fields → output size → handle id). Capture is **fail-closed
> on secrets** (F2), **best-effort and unable to fail or slow a dispatch** (F7),
> **bounded** (F3) and **short-lived** (F4). The trace is an **operator-plane**
> artefact — `GET /api/v1/audit/{id}/trace`, a console pane, and paired add-ons —
> and, per the operator override on F5, is **also readable by agents**, but only
> through the existing narrow-waist result-handle idiom: a trace is exposed as a
> **reduced, paged `ResultHandle`** read back over `result_query`, never as a new
> per-op tool and never as a raw payload in agent context. Any dispatch whose
> redaction is uncertain degrades its trace to **operator-only**.

The security is the **composition**: no single control is sufficient. Fail-closed
redaction (F2) is the load-bearing guarantee that makes agent access (F5)
admissible at all; the caps (F3), retention (F4), failure invariant (F7), and
storage (F6) each bound a distinct axis of exposure or blast radius.

## Context — the gap this closes

Operator debugging of governed automation dead-ends at the audit row. An operator
investigating "what actually happened" on a dispatch sees `op_id`, target,
`params_hash`, `result_status`, and duration on the `audit_log` row
(`backend/src/meho_backplane/db/models.py:410`) — and nothing below it: not the
vendor HTTP call(s) the connector made, not their statuses or bodies, not how a
composite processed intermediate results, not what JSONFlux kept versus discarded.
All of that exists transiently inside the connector layer and is discarded by
design.

The driving consumer (operator interview, 2026-08-29, paired automation add-on):
*"I want to see everything down to actual requests being made to various APIs,
what were the responses, how we processed them."* The add-on's run UI can render
exactly that — but only if the backplane captures it. This is the single hard
dependency for request/response-level run transparency.

## The eight decisions (ratified)

### F1 — Capture default: **per-tenant, ON for lab-class, OFF elsewhere**

Capture defaults **ON for lab-class tenants, OFF otherwise**, with a **per-target
override in both directions** and a **global kill switch**. Rationale: opt-in
everywhere guarantees the trace is missing exactly when an operator first needs
it; a per-tenant default with per-target override keeps the debugging exhaust
where it is wanted and off where it is not. This plumbs in as a typed per-tenant
policy column following the `Tenant.announce_gate_enabled` precedent
(`db/models.py:703-708` — Boolean, `server_default=false`, read by a cache-aware,
fail-open resolver), with per-target overrides riding the `Target.extras` JSON
escape hatch (`db/models.py:921-923`) or a first-class column, and the same
per-tenant/glob-scoped override shape as `BroadcastOverride` (`db/models.py:2180`)
where per-op capture-detail rules are needed. The kill switch is a global settings
flag (`settings.py`), read fail-open (kill = capture nothing, never fail a
dispatch).

### F2 — Redaction model: **fail-closed capture**

Capture is **fail-closed**, three composed rules:

1. **Header allowlist, not blocklist.** Only known-safe headers are recorded;
   everything else — including every `Authorization`, `Cookie`, `X-*-Token`,
   session, and CSRF header the connector auth layer sets — is stripped
   **unread**. A blocklist is rejected on principle: it fails *open* on an unknown
   vendor field.
2. **Per-connector body-path redaction config.** Each connector declares the body
   paths that must be scrubbed before a body is stored. This reuses the redaction
   precedent already shipped for preview: `preview_operation` returns a
   `redacted_body`, never the raw body
   (`operations/_request_preview.py`, `operations/_preview.py`).
3. **Hard-excluded op families.** Credential-, session-mint-, and token-issuing op
   families **never record bodies at all**, regardless of config. The classifier
   is single-sourced with the delete-shaped/destructive classifier
   (`operations/service_grants.py:136` / `:155`) that #3195 promotes into the
   `destructive` safety tier (see Interactions), so the exclusion set and the
   safety tier cannot drift.

Fail-closed is the load-bearing property: an unclassifiable header or an
uncertain body-path match is **dropped**, not recorded (see Security posture).

### F3 — Caps: **as proposed**

**64 KB per span body** (with a truncation marker), **1 MB per trace**, **~50
spans per dispatch** — long polls **collapse into counted span groups**, the same
idiom the reduction UI uses. **Oversize never errors the dispatch — it truncates.**
The caps bound the store's footprint on every axis and are enforced at capture
time, before anything is persisted.

### F4 — Retention: **as proposed**

**14 days lab-class / 7 days default**, per-tenant configurable, enforced by a
retention job. Traces are **debugging exhaust, not records of account**: the
`audit_log` row remains the permanent, append-only record; the trace store is a
short-lived side channel referenced from — never embedded in — that row. The
retention job follows the reaper precedent (`operations/operation_run_reaper.py`;
the un-reported-mint sweeper mould in the satellite decision).

### F5 — Access: **OVERRIDDEN — traces readable by agents too**

The proposal restricted traces to the operator claim and explicitly kept them off
the agent surface. **The operator overrode this**, verbatim condition:

> *"as long as there are no secrets in there."*

Design consequence, and the invariant this decision is built to honour:

- Agent access is **conditional on F2's fail-closed guarantees.** The operator's
  condition is discharged *only* by F2 — the header allowlist, the per-connector
  body-path scrub, and the hard-excluded op families — plus the redaction-
  uncertainty degrade below. This is why F2 is load-bearing rather than merely
  prudent.
- Agent access ships **through the narrow-waist result-handle idiom**, preserving
  postulates 5 and 6. A trace is exposed to an agent as a **reduced, paged
  `ResultHandle`** (`connectors/schemas.py`) spilled to the read-back store
  (`connectors/result_handle_store.py:137`, Valkey, tenant + operator scoped,
  TTL-bounded) and read back through the existing `result_query` core
  (`operations/result_query.py:76` `read_result_window`, `MAX_LIMIT=500`), which
  the MCP tool (`mcp/tools/result_query.py:59`) and the REST route
  (`POST /api/v1/operations/result-query`) already share. There is **no new
  per-op tool**, **no vendor-specific name on the agent surface**, and **no
  megabyte payload into agent context** — the agent pages the trace exactly as it
  pages any set-shaped result.
- Agent access is **policy-gateable per tenant** and follows the F1 default
  (lab-on). The **operator plane keeps full access** as originally proposed
  (console + REST + paired add-ons), independent of the agent gate.
- **Redaction-uncertainty degrades to operator-only.** Any dispatch whose trace
  cannot be proven fully redacted — an unclassified header, a body-path match the
  connector could not resolve, an op family the classifier could not place — has
  its trace marked redaction-uncertain and **withheld from the agent handle
  entirely**; the operator plane may still read it. The default on doubt is *less*
  agent exposure, never more.

### F6 — Storage: **dedicated Postgres table + retention job (v1)**

A **dedicated Postgres trace table** (spans + a per-dispatch trace header),
referenced by the dispatch's `audit_log.id` and **never embedded in** the audit
row (which stays slim and append-only, carrying only its own columns —
`db/models.py:410`, and note `audit_log` has **no** `broadcast_event_id` column;
the linkage points the other way via `BroadcastEvent.audit_id`,
`broadcast/events.py:481`). An object store is considered only if volume proves it
— revisited at first pain, not preemptively.

### F7 — Failure semantics: **invariant (stated, not tunable)**

The recorder is strictly **best-effort**: bounded overhead per dispatch, **drops
spans under pressure**, and **a recorder failure can never fail, block, or
materially slow a dispatch.** This is a correctness invariant, not a knob. It
follows the fail-open discipline already proven for the result-handle spill
(`connectors/result_handle_store.py:59-66`: every method swallows store/serialize
errors and degrades to the no-spill path — "a reduce must never fail because the
spill backend is unreachable"). The flight recorder holds the same contract for
the dispatch: capture is decoupled from the dispatch result path, and any capture
error is logged and swallowed, never propagated.

### F8 — Instrumentation scope: **OVERRIDDEN — "all of them" in v1**

The proposal shipped the typed-connector span API in v1 but deferred instrumenting
individual typed handlers to a demand-driven follow-up wave. **The operator
overrode this to "all of them":** typed connectors are **instrumented in v1** —
the typed span API ships **with** handler instrumentation across the typed
families, not as a later wave. An un-instrumented handler is a **defect, not an
accepted state**. The opaque-span fallback (an un-instrumented handler yielding a
single opaque span) remains **only as a transitional artefact** while the v1 wave
lands, not as a permanent honest-gap posture.

The v1 wave covers the typed families enumerated in the Implementation shape
below, keyed on **`impl_id`, not `product`**, so both implementations of each
dual-impl product are instrumented.

## Security posture — fail-closed redaction is the load-bearing guarantee

Response bodies can carry credentials, session tokens, and PII. The whole design
turns on one property: **a trace never contains a secret.** That property is what
makes the F5 agent-access override admissible — the operator's condition
(*"as long as there are no secrets in there"*) is not a hope, it is F2 enforced
fail-closed:

- **Allowlist, not blocklist** (F2.1): an unknown header is stripped unread, so a
  vendor that invents a new auth header tomorrow leaks nothing today.
- **Hard-excluded op families** (F2.3): the ops whose bodies *are* secrets
  (credential/session-mint/token) never record a body under any config.
- **Redaction-uncertainty degrade** (F5): when redaction cannot be *proven*
  complete for a dispatch, its trace is withheld from the agent handle and kept
  operator-only. Doubt reduces exposure.

The operator plane retains full access regardless; the agent gate is the strictly
narrower surface, and it is off by default outside lab-class tenants. This is the
#2901 security-review-first posture applied end to end: the review fixes the
redaction rule set, caps, retention, access scoping, and exclusions **before** any
capture code is written.

## The capture seam, grounded in real seams

Capture rides the existing single dispatch path — no parallel execution path is
introduced. The public entry is `dispatch()`
(`operations/dispatcher.py:2117`); execution + audit converge in
`_execute_and_audit` (`operations/dispatcher.py:656`), and the source-kind
fan-out (`operations/dispatcher.py:567-619`) selects one branch:

- **Generic (OpenAPI-ingested) vendor-call spans.** `dispatch_ingested`
  (`operations/_branches.py:358`) resolves the literal request and calls the
  shared httpx seam on `HttpConnector`: `_request_json` for idempotent verbs
  (`connectors/adapters/http.py:693`) and `_post_json` for mutating verbs
  (`connectors/adapters/http.py:787`). The `client.request(...)` at those two
  sites is where a vendor-call span wraps — method, URL, status, duration, and
  redacted/capped headers+bodies.
- **Composite sub-step spans.** Composites re-enter the identical dispatch path:
  `dispatch_composite` (`operations/_branches.py:505`) builds a child dispatcher
  (`operations/composite.py:269` / `:330`) whose inner call is `await dispatch(...)`
  (`operations/composite.py:351`), binding `parent_audit_id_var` for the audit
  tree. Each sub-step therefore captures its own spans under the parent trace with
  no new machinery — the same reuse #3028 relies on for out-of-process lineage.
- **JSONFlux reduction span.** `JsonFluxReducer.reduce()`
  (`operations/jsonflux_reducer.py:373`) detects the collection, applies the
  threshold (`operations/jsonflux_reducer.py:350-351`: row 50 / byte 4096),
  materializes and mints the handle id (`_materialize` `:436`), spills the full
  set (`_spill` `:480`), and assembles the reduced envelope (`_assemble` `:566`).
  The reduction span records input size → kept fields → output size → handle id,
  read from the `context` dict the reducer already carries
  (`op_id`, `operator_sub`, `tenant_id`, `target_id`, `source_kind`).
- **Typed-connector spans (F8).** Handlers are registered via
  `register_typed_operation` (`operations/typed_register.py:931`;
  `TypedOpHandler` alias `:466`) and invoked through `dispatch_typed`
  (`operations/_branches.py:458`, resolving `import_handler` at
  `operations/dispatcher.py:577`/`:586`). A small span API — a context manager
  handlers open around each SDK/vendor interaction — is added on this seam; the
  v1 wave threads it through every typed family.
- **Correlation.** Spans hang off the dispatch's `audit_log.id`; the ambient
  correlation key is the `request_id` + structlog contextvars already bound by
  `RequestContextMiddleware` (`middleware.py:219`). Audit remains synchronous and
  append-only (`operations/_audit.py` `audit_and_broadcast_safe`); the trace is
  written on the recorder's own best-effort path (F7), never gating the audit
  commit.

## Agent access without breaking the narrow waist

Postulates 5 (narrow waist of meta-tools) and 6 (JSONFlux + result handles) are
preserved exactly:

- **No new tool.** The agent reads a trace through `result_query` — an existing
  working-surface meta-tool — never a `flight_recorder.*` or vendor-specific tool.
- **Trace as a reduced handle.** The trace is materialized as a set-shaped
  result (ordered spans) and reduced through the same path as any large response:
  spilled to `ResultHandleStore` (`connectors/result_handle_store.py:137`) and
  paged via `read_result_window` (`operations/result_query.py:76`), tenant +
  operator scoped, TTL-bounded, `MAX_LIMIT=500` per page. No agent ever receives
  the raw megabytes — it receives an inline sample plus a handle, exactly as
  postulate 6 requires.
- **Gate, not surface.** Agent readability is a per-tenant policy gate on top of
  the shared read core, defaulting to the F1 lab-on posture, with the
  redaction-uncertainty degrade withholding any doubtful trace from the handle.

## Interactions with sibling decisions

Both sibling decisions have **landed** on `main`; this decision interacts with
each. The reciprocal cross-reference amendments to those two docs (adding a
pointer back to this record) are a **follow-up, deliberately not made in this
docs-only PR** — this PR touches only the new file plus the CHANGELOG.

- **Satellite write path** ([satellite-write-path.md](satellite-write-path.md),
  #2901/#3187). Once the satellite path executes vendor calls off-net, that
  traffic must be **recorded under the same redaction/caps rules fixed here** — the
  F2 allowlist, F3 caps, and F4 retention are normative for anything the satellite
  path records remotely (the store-and-forward effect audit is a *separate*,
  permanent §6 record; the flight recorder is the short-lived request/response
  exhaust). Pending cross-ref amendment: note this recording contract in the
  satellite decision.
- **Governed delete-shaped operations**
  ([governed-delete-operations.md](governed-delete-operations.md), #3183/#3195).
  The **destructive-op family joins the F2 hard-exclusion list**: the redaction
  engine single-sources its excluded-family classifier with the delete-shaped/
  destructive classifier (`operations/service_grants.py:136`/`:155`) that #3195
  promotes into the `destructive` safety tier, so the two lists cannot drift.
  Pending cross-ref amendment: note the shared classifier in the governed-delete
  decision.
- **Audit parent-linkage** (#3028) — complementary. Traces become far more
  navigable when audit rows carry run/step lineage (`parent_audit_id`,
  `db/models.py:483`): a paired add-on's orchestration subtree can present one
  trace per dispatch under one run. Not blocking.
- **Async governed dispatch** (#3079) — complementary. Long-op polling traces
  reference the same run handle; the F3 poll-collapse idiom keeps a long poll from
  blowing the span cap.
- **Observability initiative** (#2884) — OTel infra tracing was **parked** there
  and there is **no OTel instrumentation in the backend today** (only the Tempo
  *connector*, which reads a vendor trace backend, is unrelated). This flight
  recorder is **operator-facing product observability**, not infra tracing; reuse
  the instrumentation seam if it later falls out naturally, but the two deliveries
  are not coupled.

## Replay-fixture consumer note

A second consumer registered during the review: the paired automation add-on will
consume flight-recorder traces as **replay fixtures** for its blueprint *simulate*
mode (evoila-bosnia/meho-automation#159) — a real run's traces become the
simulated-vendor responses for CI regression runs of pack changes. The design
consequence is explicit and recorded here so it is not "fixed" later on the replay
use case's behalf: **fixtures are exported by the consumer at capture time** into
its own test assets. The trace store's short retention (F4) is a debugging window,
**not a fixture archive**; no retention extension is requested or granted for the
replay use case.

## Scope / non-goals

- **This decision records the design, not the code.** Implementation seams are
  filed as sibling Tasks under #3207, unimplemented. This initiative is the
  design/decision home and stays open through implementation.
- **The agent narrow waist is not widened.** Agent trace access is a new *use* of
  the existing `result_query` idiom, not a new tool, name, or payload shape.
- **The audit row is untouched as the record of account.** Traces are referenced
  from it, never embedded; `audit_log` stays slim and append-only.
- **No infra tracing.** OTel/OTLP internal instrumentation is out of scope
  (parked under #2884); this is product observability on the operator plane.
- **No retention extension for replay.** The replay consumer exports fixtures at
  capture time (see above).

## Implementation shape (for the follow-up Tasks)

1. **Capture seam** — generic-connector httpx spans (`adapters/http.py:693`/`:787`),
   composite sub-step spans (`composite.py:351`), the JSONFlux reduction span
   (`jsonflux_reducer.py:373`), and the F7 invariant machinery (best-effort,
   bounded overhead, drop-under-pressure, never fail/slow a dispatch).
2. **Redaction engine** — header allowlist, per-connector body-path config,
   hard-excluded op families (single-sourced with the destructive classifier),
   and redaction-uncertainty flagging.
3. **Storage + retention** — trace tables (F6), the retention reaper (F4), the
   per-tenant capture config + F1 capture-default plumbing, and the global kill
   switch.
4. **Operator read surface** — `GET /api/v1/audit/{id}/trace` behind the operator
   claim (alongside `GET /api/v1/audit/show/{audit_id}`, `api/v1/audit.py:376`;
   router prefix `api/v1/audit.py:146`; RBAC `_require_operator` `:152`) and a
   console trace pane in the audit drawer (`ui/templates/audit/_drawer.html`, new
   section after Lineage; route `ui/routes/audit/routes.py:760`).
5. **Agent read surface (F5)** — trace as a reduced `ResultHandle` read via the
   existing `result_query` core (`operations/result_query.py:76`), the per-tenant
   policy gate, and the redaction-uncertainty conditional-degrade wiring.
6. **Typed-connector span API + v1 instrumentation wave (F8)** — the span API on
   the typed handler seam (`typed_register.py:931`), threaded across **all** typed
   families: argocd, bind9, fleet_lcm, gcloud, github, harbor, hetzner_robot,
   holodeck, keycloak, kubernetes, loki, mongodb, nsx, pfsense, postgres,
   prometheus, proxmox, rabbitmq, rke2, sddc_manager, sddc_vcf5, tempo, vault, vcd,
   vcf_automation, vra8, vcf_fleet, vcf_installer, vcf_logs, vrli8, vcf_operations,
   vrops8, vmware_rest, windows_dns, plus the builtin `product.*` families (net,
   mail, secret, topology, targets). Dual-impl products are keyed on `impl_id`
   (sddc, vcfa, vrli, vrops, fleet) so both implementations are instrumented.
   Transports covered: REST (`adapters/http.py`), SSH (`adapters/ssh.py`), and the
   SDK/wire families (hvac, googleapiclient, kubernetes, pymongo, postgres).
7. **Consumer proof** — the paired automation add-on's run UI renders a real
   dispatch trace end-to-end, plus the replay-fixture export path
   (evoila-bosnia/meho-automation#159).

## References

- Dispatch path: `operations/dispatcher.py:2117` (`dispatch`), `:656`
  (`_execute_and_audit`), `:567-619` (source-kind fan-out), `:743`
  (`_reduce_and_audit_success`); branches `operations/_branches.py:358`/`:458`/`:505`;
  composite `operations/composite.py:269`/`:330`/`:351`.
- Generic wire seam: `connectors/adapters/http.py:693` (`_request_json`), `:787`
  (`_post_json`); base `HttpConnector`.
- JSONFlux: `operations/jsonflux_reducer.py:373` (`reduce`), `:350-351`
  (thresholds), `:436` (`_materialize`), `:480` (`_spill`), `:566` (`_assemble`);
  spill store `connectors/result_handle_store.py:137` (fail-open `:59-66`).
- Result-handle read-back (F5 idiom): `operations/result_query.py:76`
  (`read_result_window`, `MAX_LIMIT` `:55`); MCP tool `mcp/tools/result_query.py:59`;
  REST `POST /api/v1/operations/result-query`; schema `connectors/schemas.py`.
- Audit model: `db/models.py:410` (`AuditLog`), PK `:427-431`, `parent_audit_id`
  `:483`; `BroadcastEvent.audit_id` `broadcast/events.py:481`; audit write
  `operations/_audit.py`.
- Typed span seam (F8): `operations/typed_register.py:931`
  (`register_typed_operation`), `:466` (`TypedOpHandler`); base `connectors/base.py:54`
  (`Connector`), `:51`/`:97` (`ShimKind`/`_shim_kind`); `import_handler`
  `operations/dispatcher.py:577`/`:586`.
- Per-tenant config precedents: `db/models.py:703-708`
  (`Tenant.announce_gate_enabled`), `:2180` (`BroadcastOverride`), `:921-923`
  (`Target.extras`); global settings `settings.py`.
- Operator read surface: `api/v1/audit.py:146` (router), `:376`
  (`/show/{audit_id}`), `:152`/`:159` (RBAC); console `ui/templates/audit/_drawer.html`,
  route `ui/routes/audit/routes.py:760` (`_drawer_handler`), `:359`
  (`_build_drawer_context`).
- Correlation seam: `middleware.py:219` (`RequestContextMiddleware`).
- Redaction precedent: `operations/_request_preview.py`, `operations/_preview.py`
  (`redacted_body`); excluded-family classifier
  `operations/service_grants.py:136`/`:155`.
- Sibling decisions: [satellite-write-path.md](satellite-write-path.md)
  (#2901/#3187), [governed-delete-operations.md](governed-delete-operations.md)
  (#3183/#3195); complementary #3028, #3079; parked infra tracing #2884.
