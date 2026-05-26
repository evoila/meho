# Error message shape (G0.14-T11)

**Initiative:** #1139 — G0.14 v0.6.0 post-validate extended dogfood hardening
**Task:** #1141 (T11 — convention + audit pass)

## Overview

MEHO's operator-facing error surfaces (HTTP 4xx / 5xx responses that
reach a CLI, agent, or UI session) follow a single convention, codified
here. The goal is one shape an operator — human or agent — can read,
parse, act on, and self-correct from, without paging the MEHO maintainer
or attaching a debugger.

The convention emerged organically from a handful of well-shaped error
sites the consumer (`claude-rdc-hetzner-dc#697`) flagged as the pattern
they wished the rest of the API followed. Three gold-standard sites
crystallise it:

- **`/ui/auth/login` 503** — names the exact missing env vars and points
  at the doc that explains how to populate them.
- **`POST /api/v1/targets/{name}/probe` 501** — names the missing
  registration with the offending value (`product='kubernetes'`)
  embedded in the message.
- **`POST /api/v1/connectors/ingest` 422** on `spec_label_mismatch` —
  ships a structured `kind`-keyed payload alongside the human message
  so an agent can branch on the diagnostic without re-parsing the
  string.

The non-compliant sites are the symmetric inverse: they name the
domain (`keycloak_admin_not_configured`) without the remediation, or
they collapse to a bare 500 with no JSON body at all. Per-surface
fixes for the catalogued non-compliant entries live in sibling Tasks
(T1 #1142 dispatcher; T5 #1146 feed; T7 #1148 agent-principals
symmetry) and cite this doc as their convention reference.

This is a learning artefact for both new contributors writing new
error surfaces and reviewers auditing PRs that touch existing ones.
It is **not** a wholesale exception-handler middleware spec — the
convention is per-route discipline, not framework infrastructure.
Mechanization (a unit test that walks error-emitting sites and
asserts compliance) is a stretch goal; the convention text comes
first.

## The shape

Every operator-facing HTTP 4xx / 5xx error response carries three
fields. Two are mandatory; the third is conditional on whether the
client needs to branch programmatically on the diagnostic.

### 1. A short code (mandatory, stable, machine-readable)

A `snake_case` string the client matches against. Stable across
releases so callers can pattern-match without re-parsing prose.
Examples in current use:

- `ui_oauth_not_configured`
- `keycloak_admin_not_configured`
- `invalid_audience` / `invalid_issuer` / `missing_sub` /
  `signature_invalid` / `token_expired` / `token_not_yet_valid`
- `spec_label_mismatch` / `multi_spec_inconsistent` /
  `uncovered_version_label`
- `no_connector` (dispatcher resolver miss)

Naming convention: domain prefix (`ui_`, `keycloak_`, `connector_`)
when ambiguity is possible across surfaces; bare verb-noun otherwise.

The code is the **identity** of the error — adding a new code is an
additive API change; renaming one is a breaking one and bumps the
contract version. Treat code names with the same discipline as
column names in a stable schema.

### 2. A human-readable message (mandatory, actionable)

The message names three things, in order:

- **(a) The specific values involved** — `product='kubernetes'`,
  `UI_KEYCLOAK_CLIENT_ID / UI_KEYCLOAK_CLIENT_SECRET`,
  `(product='k8s', version=None)`, etc. — unless those values would
  leak infrastructure internals (see *info-leak boundary* below).
- **(b) The remediation step** — which env var to set, which doc to
  read, which verb to use, which preferred value to choose.
  Imperative, not descriptive. *"Render the credentials from Vault
  per `docs/cross-repo/...`"* is what we want, not *"credentials are
  unset"*.
- **(c) A documentation reference** — relative path
  (`docs/cross-repo/...`, `docs/codebase/...`) or a URL — that
  explains *why* and *how*. The path lives in the operator's
  checked-out clone or rendered docs site; it should not be
  abbreviated to a slug the operator has to expand.

The gold-standard template (`/ui/auth/login` 503):

```text
ui_oauth_not_configured: UI_KEYCLOAK_CLIENT_ID /
UI_KEYCLOAK_CLIENT_SECRET are unset. Render the confidential
client credentials from Vault per
docs/cross-repo/keycloak-web-client.md before serving /ui/auth/*.
```

Reads as: **code prefix → diagnostic values → remediation imperative
→ doc reference.** Four sentence-clauses, one paragraph. The same
information is in the structured log line, but the *response body*
itself is what an operator without a kubectl-logs handle sees.

### 3. Optional structured `data` payload (conditional)

When the operator (or the agent acting for the operator) needs to
**programmatically resolve** the error — retry with corrected params,
present a structured form, drop a misnamed field — the response also
carries a `data` payload with machine-actionable fields. The
reference example is the ingest pipeline's `spec_label_mismatch`
shape (`backend/src/meho_backplane/operations/ingest/error_envelopes.py`):

```json
{
  "kind": "spec_label_mismatch",
  "requested_version": "1.30",
  "spec_info_versions": [
    {"spec_uri": "https://k8s.io/openapi/...", "info_version": "1.31.2"}
  ],
  "message": "ingest version label '1.30' contradicts spec info.version '1.31.2' at https://k8s.io/openapi/..."
}
```

- `kind` is the **structured equivalent of the code field** — same
  stability discipline, same naming convention. Callers branch on
  it without parsing the message.
- The remaining fields are **values the client cannot re-derive** —
  if the agent can fetch them from a prior request it already made,
  do not duplicate them here. The agent already knows what it sent.
- `message` is carried verbatim so a client that ignores the
  structured fields still gets the human-readable detail. Same
  text as field #2 above; not a different one.

**When to ship a `data` payload:**

- The error is recoverable by an automated agent (retry with
  different params) — ship `data`.
- The remediation requires structured information beyond a doc
  link (a list of supported version ranges, a list of registered
  connectors, an expected schema fragment) — ship `data`.
- The error is *purely diagnostic* (caller misconfigured an env
  var, infra is down) — `data` is optional; the message-plus-code
  shape is enough.

**Field placement.** REST embeds `data` inside the
`HTTPException.detail` field (so the response body is
`{"detail": {"kind": ..., ...}}`); MCP embeds it in the JSON-RPC
`error.data` member (spec §5.1). The shared
`build_*_detail` helpers under
`backend/src/meho_backplane/operations/ingest/error_envelopes.py`
exist precisely so both transports emit the same shape — G0.9.1-T5
#777 lifted these out of the REST route and the MCP tool because
the two were drifting (the MCP path used to lose the structured
detail entirely through the dispatcher's generic INTERNAL_ERROR
handler).

## The info-leak boundary

Some diagnostic values name infrastructure internals an attacker
could mine: expected `aud` claim strings reveal the realm topology,
expected `iss` strings reveal the IdP hostname, internal database
PKs reveal table cardinality. These values are **structured log
material, not response-body material**.

The precedent is G0.9.1-T12 #797 (decode-stage JWT classifier):
the *code* lands in the response body (`invalid_audience`), but
the *expected and received audience values* land only in the
`structlog` record. The route handler — when the request reaches it
— logs the structured field via the existing `log_with_request_id`
binding; the response body is the code alone.

**Rule:** if a diagnostic value names infrastructure topology
(realm names, internal hostnames, database PKs, internal token
signing keys), it goes to the structured log. The response body
carries the classifier code. The remediation in the response body
points at the doc (`docs/cross-repo/keycloak-web-client.md`,
`docs/codebase/auth.md`, etc.); the doc names the value-resolution
procedure operators with cluster access can run, off the request
path.

This rule is **convention**, not a hard policy. A surface that
exposes a value the operator *already knows from their own
configuration* (an env var name they themselves set) is not
leaking — the operator just needs to be reminded. The gold-standard
`/ui/auth/login` 503 names `UI_KEYCLOAK_CLIENT_ID` because the
operator set that env var; it does **not** name the resolved
client-id value the Vault render produced.

## When the response is intentionally bare

A handful of errors stay deliberately code-only because the
remediation is genuinely either "this is a programmer error
(file a bug)" or "the upstream identity provider chose to reject
this credential and we shouldn't speculate about why on its
behalf".

The catalogued intentionally-bare exceptions (current as of
v0.6.0):

- **`invalid_token` residual** at `auth/jwt.py` — after the
  G0.9.1-T12 #797 decode-stage classifier landed
  (`invalid_audience` / `invalid_issuer` / `missing_sub` /
  `signature_invalid` / `token_expired` / `token_not_yet_valid`),
  the remaining `invalid_token` cases are genuinely
  unclassifiable: JWS decode errors, fetch-JWKS failures with no
  retryable cause, JOSE library exceptions we don't reflect to the
  operator because they leak library internals. The structured
  log carries `reason` (`jws_decode_error` / `jose_error` /
  `exception=<type-name>`). G0.13-T1 #1131 tracks whether the
  residual deserves further decomposition.
- **`keycloak_admin_error`** at `api/v1/agent_principals.py` —
  the 502 response when the Keycloak admin client fails for a
  reason that is not "the admin client was never configured".
  The exact upstream cause is in the structured log; the
  response body stays bare because the operator's remediation
  is identical regardless of the upstream HTTP code (file a
  Keycloak ops ticket, check the admin client's permissions).

A non-compliant bare error is one that *should* have been
shaped but wasn't. The audit table below distinguishes the two.

## Audit table (v0.6.0)

The audit covers the consumer-cited entries from
`claude-rdc-hetzner-dc#697` plus a sweep over `api/v1/`,
`mcp/tools/`, and `connectors/`. Each row is one error-emitting
site (a route, an MCP tool, or a log message the consumer
specifically referenced).

| Surface | Current shape | Verdict | Tracked by |
|---|---|---|---|
| `/ui/auth/login` 503 (no client credentials) | `ui_oauth_not_configured: UI_KEYCLOAK_CLIENT_ID / UI_KEYCLOAK_CLIENT_SECRET are unset. Render the confidential client credentials from Vault per docs/cross-repo/keycloak-web-client.md before serving /ui/auth/*.` | compliant (gold-standard) | — |
| `POST /api/v1/targets/{name}/probe` 501 (no connector for product) | `no connector registered for product='kubernetes'` | compliant (gold-standard) | — |
| `POST /api/v1/connectors/ingest` 422 (spec_label_mismatch) | structured `detail` with `kind`, `requested_version`, `spec_info_versions[]`, `message` (`build_version_mismatch_detail` in `error_envelopes.py`) | compliant (gold-standard, structured) | — |
| `POST /api/v1/connectors/ingest` 422 (uncovered_version_label, **REST**) | `detail=str(exc)` — the `UncoveredVersionLabel.__str__` text only; no structured `kind` / `product` / `version` / `registered_classes[]` payload (see `api/v1/connectors_ingest.py:302-305`) | partial — message is human-readable but REST does not emit the structured envelope the MCP path does. The shared `build_uncovered_version_label_detail` builder exists in `error_envelopes.py`; REST is not yet wired to it | follow-up (no ticket yet) — call `build_uncovered_version_label_detail(exc)` in the REST route for parity with `VersionMismatchError` (sibling row), the existing G0.9.1-T5 #777 shared-builder pattern |
| `connector_admin.ingest_connector` MCP tool 422 (uncovered_version_label, **MCP**) | JSON-RPC `-32602` with structured `data` containing `product`, `version`, `impl_id`, `registered_classes[]`, `message` (`build_uncovered_version_label_detail` at `mcp/tools/connector_admin.py:235-239`) | compliant (structured) | — |
| `AmbiguousConnectorResolution` log message (resolver tie-break failure) | `resolution ambiguous after tie-break ladder for (product='k8s', version=None); candidates=[...]; set target.preferred_impl_id to one of them` | compliant *as a log message*, but **the diagnostic never reaches the operator** because the dispatcher swallows the exception; the response body collapses to a bare 500 envelope (signal 8) | T1 #1142 (catch and surface in `extras.exception_message`) |
| `GET /api/v1/health` 401 (residual bare `invalid_token`) | `{"detail":"invalid_token"}` | partial (post-#797 the common cases are classified; the residual case is intentionally bare per the *intentionally bare* section above, pending G0.13-T1 follow-up) | G0.13-T1 #1131 (reproduce + extend OR document residual) |
| `GET /api/v1/feed` 500 (Redis xread failure) | `500 Internal Server Error` with no JSON body | non-compliant (no code, no message, no remediation) | T5 #1146 (catch `RedisError`, emit sentinel SSE or structured 503) |
| Dispatcher composite-handler failures (resolver exceptions in typed/composite branch) | bare 500 envelope (signal 8); `NoMatchingConnector` / `AmbiguousConnectorResolution` propagate uncaught | non-compliant (no code, no remediation) | T1 #1142 (catch + label `no_connector`; surface `extras.exception_message`) |
| `POST /api/v1/agent-principals` 503 (Keycloak admin not configured) | `{"detail":"keycloak_admin_not_configured"}` | partial — names the domain but not the env vars or the doc | T7 #1148 (symmetrize with the `/ui/auth/login` shape) |
| `POST /api/v1/agent-principals` 502 (Keycloak admin runtime error) | `{"detail":"keycloak_admin_error"}` | intentionally bare (upstream failure; remediation is identical regardless of upstream code; structured log carries detail) | — |
| `/api/v1/auth-config` features visibility (whether agent-runtime is configured) | implicit — the agent-runtime configuration state is not surfaced anywhere on `/ready` or `/api/v1/auth-config` (signal 17) | non-compliant *as a discoverability gap* — when the surface emits `keycloak_admin_not_configured` the operator has no way to discover the feature was supposed to be configured | T7 #1148 (`/ready` features block with `configured: bool` + `missing_env`) |
| `GET /api/v1/health` audit-replay capture state (signal 11) | `mcp_session_id_capture: "always" \| "enforced"` field on `HealthResponse` — `"always"` is the default (capture-if-present, no rejection); `"enforced"` lights up when `MCP_REQUIRE_SESSION_ID=true` (additionally rejects header-less calls). Sourced from `mcp_session_id_capture_mode()` in `mcp/server.py`. | compliant *as a discoverability surface* — landed in T6 #1147 alongside the capture-vs-enforcement decouple. T7 #1148's `/ready` features block reads from the same helper for the richer `audit_replay` block. | T6 #1147 |
| `POST /api/v1/targets` 422 (`unknown_product` — typo at create time) | structured `detail` with `kind="unknown_product"`, `product`, `valid_products[]`, `message` (`_build_unknown_product_detail` in `api/v1/targets.py`); also exposed proactively as a JSON Schema enum on `TargetCreate.product` via `build_openapi_schema` in `main.py` | compliant (structured + discoverable) — sibling **Option A** (enum in OpenAPI) and **Option C** (recovery-time 422) from the task body; T4 #1145 ships the same shape at PATCH time | T3 #1144 |
| `PATCH /api/v1/targets/{name}` 422 (unknown_product) | structured `detail` with `kind='unknown_product'`, `product`, `valid_products[]`, `message` (`api/v1/targets.py` `update_target`) | compliant (structured) — landed in T4 #1145 | — |
| `DELETE /api/v1/targets/{name}` 409 (target_has_references) | structured `detail` with `kind='target_has_references'`, `graph_node_refs`, `message` naming the `?force=true` remediation (`api/v1/targets.py` `delete_target`) | compliant (structured) — landed in T4 #1145 | — |

The table is the audit artefact's deliverable, not a separate
spreadsheet. Adding new error surfaces against the convention
extends this table at the same time the surface lands.

## How to apply the convention

When you write a new error surface (REST route, MCP tool, error log
that an operator will see):

1. **Pick the code first.** Stable, `snake_case`, domain-prefixed
   when necessary. Add it to the catalogue (this doc's audit table)
   in the same PR that introduces the surface.
2. **Write the message in three clauses.** Diagnostic values
   (subject to info-leak rule) → remediation imperative → doc
   reference. Mentally read it aloud as if reading it to an operator
   who has never seen the surface before.
3. **Decide on a `data` payload.** Yes only if the client needs to
   programmatically resolve the error. Default no; do not ship
   `data` for diagnostic-only errors.
4. **Decide on info-leak.** Does the diagnostic value name
   infrastructure topology? If yes, log it via `structlog`, do not
   emit it in the response body. Re-read the *info-leak boundary*
   section above when uncertain.
5. **Cite this doc** in the PR description if the route is new or
   the shape was non-compliant before. (Reviewers cross-check
   against the audit table.)

When you review a PR that touches an existing error surface or adds
a new one:

1. Cross-check the surface against the audit table. If the surface
   was previously non-compliant, the audit row's `tracked_by`
   column should reference the PR fixing it.
2. Audit the message against the three-clause rule. The most common
   miss is the **doc reference** — operators new to MEHO need it
   even when MEHO maintainers can guess from the code alone.
3. Audit the `data` payload (if any) against the structured-vs-
   diagnostic split. A payload with three or four fields the
   client already knows is a code smell — the client doesn't need
   to be reminded of its own request.

## Mechanization (stretch — not in v0.6.0)

A unit test that walks every `HTTPException`-raising and
structured-error-emitting site, asserts the body matches the
convention or appears on the intentionally-bare allowlist (this
doc's *intentionally bare* section, machine-readable), and fails
the test on a new bare error sneaking in.

The audit table above is small enough that the cost-benefit hasn't
flipped to "automate it" yet. The mechanization is filed as a
post-v0.6.0 follow-up once T1 / T5 / T7 land and the audit
non-compliant count drops to zero (or to the intentionally-bare
set).

## Dependencies

- `backend/src/meho_backplane/api/v1/ui_auth.py` (route handler for
  `/ui/auth/login`) + `backend/src/meho_backplane/ui/auth/flow.py`
  (`MISSING_CLIENT_SECRET_DETAIL` constant).
- `backend/src/meho_backplane/api/v1/targets.py` (`/probe` 501
  message; `POST /api/v1/targets` `unknown_product` 422 via
  `_build_unknown_product_detail`, T3 #1144).
- `backend/src/meho_backplane/connectors/registry.py` —
  `registered_product_tokens()`, the canonical source-of-truth for
  the `TargetCreate.product` enum and the validators in
  `create_target` (POST) + `update_target` (PATCH, T4 #1145).
- `backend/src/meho_backplane/main.py` — `build_openapi_schema`
  hook that injects the live product enum into the OpenAPI
  document so generator tooling surfaces it (T3 #1144 Option A).
- `backend/src/meho_backplane/api/v1/connectors_ingest.py` (REST
  ingest 422 + 4xx error shape via shared builders).
- `backend/src/meho_backplane/operations/ingest/error_envelopes.py`
  — `build_version_mismatch_detail` /
  `build_uncovered_version_label_detail`, the shared structured-
  detail builders used by both REST and MCP ingest surfaces.
- `backend/src/meho_backplane/operations/ingest/exceptions.py` —
  `VersionMismatchError.kind`, `UncoveredVersionLabel.candidates`,
  the underlying exception types the builders consume.
- `backend/src/meho_backplane/auth/jwt.py` — decode-stage classifier
  emitting `invalid_audience` / `invalid_issuer` / `missing_sub` /
  `signature_invalid` / `token_expired` / `token_not_yet_valid` /
  residual `invalid_token`.
- `backend/src/meho_backplane/connectors/resolver.py` —
  `NoMatchingConnector` (`no_connector` classifier) and
  `AmbiguousConnectorResolution` (diagnostic message landing in the
  resolver's exception; T1 wires it to the dispatcher's response
  envelope).
- `backend/src/meho_backplane/operations/dispatcher.py` — the typed
  and composite branches that currently swallow resolver exceptions
  (signal 8; T1 #1142).

## Known issues

- **Signal 8** — dispatcher's typed/composite branch returns
  `(None, None)` on `NoMatchingConnector` instead of the ingested
  branch's `(None, "no_connector")` label; combined with no catch
  for `AmbiguousConnectorResolution`, the resolver's well-shaped
  diagnostic never reaches the response envelope. T1 #1142 mirrors
  the ingested branch's label and adds the
  `AmbiguousConnectorResolution` catch.
- **Signal 10** — `/api/v1/feed` does not catch `RedisError` from
  `xread`; an unavailable broadcast stream yields a bare 500 with
  no JSON body. T5 #1146 catches and emits either an empty SSE
  stream with a sentinel event or a structured 503.
- **Signal 16** — `POST /api/v1/agent-principals` 503 surfaces the
  domain code `keycloak_admin_not_configured` but not the env vars
  or the doc reference. T7 #1148 symmetrizes with the
  `/ui/auth/login` 503 shape.
- **Signal 17** — `/ready` and `/api/v1/auth-config` do not enumerate
  the gated features (`agent_runtime`, `ui_surface`, `audit_replay`,
  `approval_queue`) with `configured: bool` and `missing_env`, so
  the operator cannot discover *what* needs to be configured to
  satisfy a `*_not_configured` error. T7 #1148 adds the features
  block.
- **Signal 5** — `POST /api/v1/targets` accepts any `product`
  string but the resolver matches on exact connector-class tokens,
  so a single typo (`'kubernetes'` instead of `'k8s'`) silently
  creates a permanent broken row. T3 #1144 ships boot-time enum
  generation (Option A: JSON Schema enum on `TargetCreate.product`
  populated from the live registry, surfaces in Swagger /
  OpenAPI-driven tooling) plus a structured 422 with
  `valid_products: [...]` on miss (Option C: recovery-time net).
  Both layers share `registered_product_tokens()` as the
  source-of-truth so they cannot disagree.

## References

- **Consumer feedback (canonical):** `claude-rdc-hetzner-dc#697` —
  the original *"That's the error shape operators want everywhere"*
  flag.
- **Parent Goal / Initiative:** [#221](https://github.com/evoila/meho/issues/221) (G0)
  / [#1139](https://github.com/evoila/meho/issues/1139) (G0.14).
- **Info-leak boundary precedent:** G0.9.1-T12 #797 — decode-stage
  classifier landing the **code** in the response and the
  **values** in `structlog`.
- **Cross-transport shared envelopes:** G0.9.1-T5 #777 —
  `build_version_mismatch_detail` /
  `build_uncovered_version_label_detail` lifted into a shared module
  so REST and MCP emit the same shape.
- **Sibling Tasks that cite this doc:**
  T1 [#1142](https://github.com/evoila/meho/issues/1142) (dispatcher),
  T5 [#1146](https://github.com/evoila/meho/issues/1146) (feed),
  T7 [#1148](https://github.com/evoila/meho/issues/1148)
  (agent-principals + `/ready` features block).
- **Related but distinct standard:** RFC 7807 *Problem Details for
  HTTP APIs* — MEHO's convention is shape-compatible (a code, a
  message, optional structured data) but does not claim RFC 7807
  conformance (no `type` URI, no `instance` field, content-type
  stays `application/json` not `application/problem+json`). If a
  future consumer needs RFC 7807 specifically, the convention can
  be extended additively without breaking the current shape.
