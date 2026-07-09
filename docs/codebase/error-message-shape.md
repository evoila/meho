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
  `missing_exp` / `signature_verification_failed` / `token_expired` /
  `token_not_yet_valid` / `malformed_jws`
- `spec_label_mismatch` / `multi_spec_inconsistent` /
  `uncovered_version_label`
- `no_connector` (dispatcher resolver miss)
- `target_required` (dispatcher: connector-bound typed/composite op
  invoked with no `target` — #1506)
- `connector_scope_ambiguous` (connector review/enable-reads: a
  `connector_id` resolves to both a tenant row and a built-in row —
  #1801; the message names the `prefer=tenant|builtin` retry — #2029)

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

**Free-text diagnostics from the dispatch error builders are Tier-1
redacted, then capped.** Every free-text field the
`operations/_errors.py` builders emit from a `str(exc)` or an
upstream response body — `extras.exception_message`,
`extras.upstream_message`, `extras.detail`, and the `error` summary
tails built from them — passes through `_sanitize_free_text`: a
Tier-1 redaction pass with the packaged default policy
(credential-shaped patterns only, so hosts / statuses / remediation
prose stay legible), then the `_EXC_MESSAGE_CAP=256` truncation.
Where the rows below say "capped" for these fields, read
"redacted, then capped": the cap alone is a length bound, not a
secrecy bound — a credential inside the first 256 chars used to ride
the envelope verbatim. Redaction runs first so a secret straddling
the cap boundary cannot survive truncation as a cleartext fragment
the patterns no longer match. See `docs/codebase/redaction.md`.

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
  `missing_exp` / `signature_verification_failed` / `token_expired` /
  `token_not_yet_valid` / `malformed_jws`),
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
| `POST /api/v1/targets/{name}/probe` 500 (resolved connector's `fingerprint` raises) | structured `detail` with `error='fingerprint_failed'`, `connector_id`, `target_name`, `exception_class`, capped `exception_message`, `docs` (route handler in `api/v1/targets.py`; cap at `_PROBE_EXC_MESSAGE_CAP=256`) | compliant (structured) — landed in T1 #1210, mirrors the dispatcher's `_execute_and_audit` `connector_error` envelope at the route boundary. Closes the bare-500 hole G0.14-T1 #1142 left on the resolvable-target fingerprint path (sub-signal A of `claude-rdc-hetzner-dc#753`). Full exception stacktrace lands in the structured log via `_log.exception('probe_fingerprint_failed', ...)`; only the capped message reaches the response body | — |
| `POST /api/v1/connectors/ingest` 422 (spec_label_mismatch) | structured `detail` with `kind`, `requested_version`, `spec_info_versions[]`, `message` (`build_version_mismatch_detail` in `error_envelopes.py`) | compliant (gold-standard, structured) | — |
| `POST /api/v1/connectors/ingest` 422 (uncovered_version_label, **REST**) | structured `detail` dict with `product`, `version`, `impl_id`, `registered_classes[]` (each `{class_name, version, impl_id, supported_version_range}`), `message` (`build_uncovered_version_label_detail` in `error_envelopes.py`, called by the `except UncoveredVersionLabel` arm in `api/v1/connectors_ingest.py`) | compliant (structured) — landed in #1624, wiring the REST route to the shared builder for parity with `VersionMismatchError` (sibling row, G0.9.1-T5 #777). The REST 422 `detail` and the MCP `-32602` `error.data` member (row below) are now the same builder output and can't drift; the last bare `detail=str(exc)` arm in the ingest route's typed-exception table (the #1610 400-family parity leftover) | — |
| `meho.connector.ingest` MCP tool (uncovered_version_label, **MCP**) | JSON-RPC `-32602` with structured `data` containing `product`, `version`, `impl_id`, `registered_classes[]`, `message` (`build_uncovered_version_label_detail`, dispatched by `raise_invalid_params_for_spec_error` in `mcp/tools/_connector_shared.py`; the inline handler lives in `mcp/tools/connector_ingest.py` after the #1531 split out of `connector_admin.py`) | compliant (structured) | — |
| `meho.connector.ingest` MCP tool (SpecError siblings, **MCP**) — `UnsupportedSpecError` / `InvalidSpecError` / `UpstreamNotSpecError` / `InvalidSchemaError` / `OpIdCollision` / `LlmOutputInvalid` | JSON-RPC `-32602` with structured `data` carrying a stable `detail` classifier (`unsupported_spec` / `invalid_spec` / `upstream_not_spec` / `invalid_schema` / `op_id_collision` / `llm_output_invalid`) + the rendered `message`; `OpIdCollision` and `UpstreamNotSpecError` add machine-resolvable fields (op-ids + spec-sources; upstream URL + content-type). Built by the matching `build_*_detail` helpers in `error_envelopes.py`, dispatched by `raise_invalid_params_for_spec_error` (`mcp/tools/_connector_shared.py`) | compliant (structured) — landed in #1534, completing the #777 envelope pattern. Before #1534 these six fell through the dispatcher's generic `except Exception` to a bare `-32603 "internal error: <ClassName>"` with the message discarded, while REST attached the detail (the MCP↔REST asymmetry #1534 closed) | — |
| `POST /api/v1/connectors/ingest` 400 (SpecError parser family, **REST**) — `UnsupportedSpecError` / `InvalidSpecError` / `InvalidSchemaError` / `OpIdCollision` / `LlmOutputInvalid` | structured `detail` dict carrying the same stable classifier (`unsupported_spec` / `invalid_spec` / `invalid_schema` / `op_id_collision` / `llm_output_invalid`) + the rendered `message`; `OpIdCollision` adds op-ids + spec-sources, `LlmOutputInvalid` adds `pass_name`. Same `build_*_detail` builders as the MCP row above, dispatched by `_spec_error_http_exception` in `api/v1/connectors_ingest.py` (a Swagger 2.0 spec yields `unsupported_spec` plus the swagger2openapi conversion remediation in `message`) | compliant (structured) — landed in #1610, closing the REST half of the parity (#1534 closed the MCP half; before #1610 these five collapsed to a bare `detail=str(exc)` 400 on REST, so SDK/CLI callers had to re-parse prose) | — |
| `GET /api/v1/connectors/{id}/review` 409 **and** `POST /api/v1/connectors/{id}/enable-reads` 409 (`connector_scope_ambiguous`) | structured `detail` dict with `detail="connector_scope_ambiguous"`, `connector_id`, `candidates[]` (each `{product, version, impl_id, tenant_id}` — `tenant_id` is `null` for the built-in row, the operator's own tenant UUID-string for the tenant-curated row), `message`. Built by `build_connector_scope_ambiguous_detail` (`operations/ingest/error_envelopes.py`) from `AmbiguousConnectorScopeError`, raised by the shared `ReviewService._resolve_existing_scope` and caught by the `except AmbiguousConnectorScopeError` arm in both route handlers in `api/v1/connectors_ingest.py`. The operator's own `tenant_id` is not an info-leak — it is the operator's own value (same posture the `/ui/auth/login` 503 takes naming env vars the operator set); the *other* tenant's rows stay invisible (cross-tenant probes are still 404-conflated upstream). | compliant (structured) — landed in #1801 (G0.26-T1). Closes the read/write resolution asymmetry: `/review` had a tenant→built-in global fallback (#1135) that `/enable-reads` lacked, so a built-in-only label 200'd on read but 404'd on write, and a tenant+built-in label silently resolved to *different* rows. Both now share one resolver: a built-in-only label resolves to the built-in row on both paths (200 / reads-enabled), and a tenant+built-in ambiguous label raises this 409 on both instead of a silent pick. 409 (not 422) keeps the plain dict-`detail` body — the label resolves, just to >1 row, a conflict the operator settles. #2029 made the 409 *actionable*: both routes take an optional `prefer=tenant\|builtin` query param that resolves directly to the named scope (skipping the ambiguity probe), and the rendered `message` literally names `retry with prefer=tenant or prefer=builtin`. `prefer` does not weaken the default — omitted, the fail-loud 409 is byte-identical; `prefer=builtin` writes stay `tenant_admin`-gated at the route (`_require_admin`). | — |
| `meho.connector.review` **and** `meho.connector.enable_reads` MCP tools (`connector_scope_ambiguous`, **MCP**) | JSON-RPC `-32602` with structured `data` carrying the same `build_connector_scope_ambiguous_detail` envelope as the REST 409 row above — `detail="connector_scope_ambiguous"`, `connector_id`, `candidates[]` (each `{product, version, impl_id, tenant_id}`), `message`. Dispatched by `raise_invalid_params_for_ambiguous_scope` (`mcp/tools/_connector_shared.py`), caught by the `except AmbiguousConnectorScopeError` arms in `_review_handler` / `_enable_reads_handler` (`mcp/tools/connector_admin.py`). One builder shared with REST, so the MCP `error.data` member and the REST 409 `detail` can't drift. | compliant (structured) — landed in #1910. Before #1910 these two fell through the dispatcher's generic `except Exception` to a bare `-32603 "internal error: AmbiguousConnectorScopeError"` with the candidate list discarded, while REST attached the detail (the MCP↔REST asymmetry #1910 closed, same shape #1534 closed for the ingest SpecError siblings). MCP's only structured handler-error channel is `-32602`, so the wire *code* differs from REST's 409 while the `data` envelope is identical — an agent reads `error.data.candidates` and re-issues with the disambiguating `prefer=tenant\|builtin` arg the two tool schemas added in #2029 (the closed-set selector the message names; `tenant_id` still selects which tenant the operator acts under). | — |
| `AmbiguousConnectorResolution` log message (resolver tie-break failure) | `resolution ambiguous after tie-break ladder for (product='k8s', version=None); candidates=[...]; set target.preferred_impl_id to one of them` | compliant *as a log message*, but **the diagnostic never reaches the operator** because the dispatcher swallows the exception; the response body collapses to a bare 500 envelope (signal 8) | T1 #1142 (catch and surface in `extras.exception_message`) |
| `GET /api/v1/health` 401 (residual bare `invalid_token`) | `{"detail":"invalid_token"}` | partial (post-#797 the common cases are classified; the residual case is intentionally bare per the *intentionally bare* section above, pending G0.13-T1 follow-up) | G0.13-T1 #1131 (reproduce + extend OR document residual) |
| `GET /api/v1/feed` 500 (Redis xread failure) | `500 Internal Server Error` with no JSON body | non-compliant (no code, no message, no remediation) | T5 #1146 (catch `RedisError`, emit sentinel SSE or structured 503) |
| Dispatcher composite-handler failures (resolver exceptions in typed/composite branch) | bare 500 envelope (signal 8); `NoMatchingConnector` / `AmbiguousConnectorResolution` propagate uncaught | non-compliant (no code, no remediation) | T1 #1142 (catch + label `no_connector`; surface `extras.exception_message`) |
| `POST /api/v1/agent-principals` 503 (Keycloak admin not configured) | `keycloak_admin_not_configured: KEYCLOAK_ADMIN_URL / KEYCLOAK_ADMIN_CLIENT_ID / KEYCLOAK_ADMIN_CLIENT_SECRET are unset. Provision the confidential admin client per docs/cross-repo/keycloak-agent-client.md before defining agent principals.` (constant `KEYCLOAK_ADMIN_NOT_CONFIGURED_DETAIL` in `auth/keycloak_admin.py`) | compliant — landed in T7 #1148 with the symmetric `/ui/auth/login` shape | — |
| `POST /api/v1/agent-principals` 502 (Keycloak admin runtime error) | `{"detail":"keycloak_admin_error"}` | intentionally bare (upstream failure; remediation is identical regardless of upstream code; structured log carries detail) | — |
| `/api/v1/auth-config` features visibility (whether agent-runtime is configured) | implicit — the agent-runtime configuration state was not surfaced anywhere on `/ready` or `/api/v1/auth-config` (signal 17) | landed compliant via T7 #1148's `/ready` features block (see the next row) — kept here as the historical "before" record | T7 #1148 |
| `/ready` features visibility (whether each gated feature is configured) | structured `features` block enumerating `agent_runtime`, `ui_surface`, `audit_replay`, `approval_queue` with `configured: bool`, `missing_env: [...]`, `docs: "..."` (transitive features carry `depends_on` instead of `docs`); built by `meho_backplane.features.build_features_block` | compliant *as a discoverability surface* — landed in T7 #1148. The `features` block sits on `/ready` (both 200 and 503 branches) so an operator's single GET answers "which features will work out of the box on my deploy?" before they trip a downstream `*_not_configured` error | — |
| `GET /api/v1/health` audit-replay capture state (signal 11) | `mcp_session_id_capture: "always" \| "enforced"` field on `HealthResponse` — `"always"` is the default (capture-if-present, no rejection); `"enforced"` lights up when `MCP_REQUIRE_SESSION_ID=true` (additionally rejects header-less calls). Sourced from `mcp_session_id_capture_mode()` in `mcp/server.py`. | compliant *as a discoverability surface* — landed in T6 #1147 alongside the capture-vs-enforcement decouple. T7 #1148's `/ready` features block reads from the same helper for the richer `audit_replay` block. | T6 #1147 |
| `POST /api/v1/targets` 422 (`unknown_product` — typo at create time) | structured `detail` with `kind="unknown_product"`, `product`, `valid_products[]`, `message` (`_build_unknown_product_detail` in `api/v1/targets.py`); also exposed proactively as a JSON Schema enum on `TargetCreate.product` via `build_openapi_schema` in `main.py` | compliant (structured + discoverable) — sibling **Option A** (enum in OpenAPI) and **Option C** (recovery-time 422) from the task body; T4 #1145 ships the same shape at PATCH time | T3 #1144 |
| `PATCH /api/v1/targets/{name}` 422 (unknown_product) | structured `detail` with `kind='unknown_product'`, `product`, `valid_products[]`, `message` (`api/v1/targets.py` `update_target`) | compliant (structured) — landed in T4 #1145 | — |
| `POST /api/v1/targets` / `PATCH /api/v1/targets/{name}` 422 (`secret_ref_outside_tenant_scope` — an explicit `secret_ref` outside the operator's tenant subtree, #2091) | structured `detail` with `kind='secret_ref_outside_tenant_scope'`, `secret_ref` (the offending value), `tenant_prefix` (the rendered mount-pinned prefix, e.g. `secret/tenants/<uuid>/`), `expected_secret_ref` (the exact `tenants/<tenant_id>/<name>` path derived via `tenant_secret_ref`), `message` naming the constraint + the convention + the stage-the-credential remediation + the "do NOT widen the deploy-owned Vault policy" warning (`_build_secret_ref_outside_tenant_scope_detail` / `_enforce_secret_ref_tenant_scope` in `api/v1/targets.py`). Segment-boundary semantics mirror `enforce_tenant_scope` (`connectors/vault/tenant_scope.py`); no-op when the guard is disabled (`VAULT_KV_TENANT_SCOPE_PREFIX=""`). A target with an out-of-subtree ref imports clean and then fails every dispatch with an opaque Vault `permission denied` (the `connector_vault_forbidden` dispatch row below) — this gate fails fast at write time instead. Only an *explicit* ref is checked: the derived per-tenant default (#1723) and the explicit-null clear are untouched. | compliant (structured) — landed in #2091 | — |
| `DELETE /api/v1/targets/{name}` 409 (target_has_references) | structured `detail` with `kind='target_has_references'`, `graph_node_refs`, `message` naming the `?force=true` remediation (`api/v1/targets.py` `delete_target`) | compliant (structured) — landed in T4 #1145 | — |
| Connector `NotImplementedError` on dispatch (any connector; the RDC cycle-8 `vmware-l2-dispatch-notimplemented` dead end) | structured `extras` with `error_code='connector_unsupported'`, `cause` (`unsupported_feature` — a hand-rolled connector rejects the target's `auth_model` / mode; `unreplaced_auto_shim` — the resolved connector is the ingest-time `GenericRestConnector` shim), `connector_class`, `detail` (the raise-site message verbatim, length-capped), and (G0.25-T2 #1753) `sibling_impl_id`; the `error` message promotes the raise-site text and appends the per-cause remediation imperative + doc reference (`docs/architecture/connector-auth.md` / `docs/codebase/spec-ingestion.md`). For `unreplaced_auto_shim` the remediation now forks on `sibling_impl_id`: when a hand-rolled class for the same `(product, version)` already ships under a different `impl_id` (the near-miss footgun #1751 — the shim is shadowing it), `extras.sibling_impl_id` names it and the message says "re-ingest under it / delete the stray shim's ops, do NOT write a subclass" (one exists); when `sibling_impl_id` is `None` the message keeps the original "register the per-product subclass — re-ingesting will NOT replace the shim". Built by `result_connector_unsupported` in `operations/_errors.py`; caught by the dispatcher's `_run_branch_with_error_handling` ahead of the generic `connector_error` catch. Cause classified via `isinstance(connector_instance, GenericRestConnector)` — precise, not message-fragile; the sibling resolved via `sibling_handrolled_impl_id()` (`operations/ingest/connector_registration.py`, the same registry scan the ingest near-miss warning uses so both surfaces name the same sibling). Before #1627 this flattened to a bare `connector_error: NotImplementedError` with the (already descriptive) message buried in `extras.exception_message`. | compliant (structured) — landed in #1627, extended in #1753 | — |
| Connector upstream **403 Forbidden** on dispatch (any connector; the gh-rest write under an App with `issues: read` but not `issues: write`, consumer `claude-rdc-hetzner-dc#1138`) | structured `extras` with `error_code='connector_http_403'`, `http_status=403`, `upstream_message` (the upstream body's `message` field when JSON, else capped raw text, `null` when the body was empty), `permission_headers` (the present subset of `X-Accepted-GitHub-Permissions` / `x-oauth-scopes`, echoed verbatim — empty `{}` for a non-GitHub 403); the `error` names the likely insufficient-permission cause **connector-agnostically** (the credential authenticated but lacks the op's scope — a target-credential matter, not a transport fault), appends the grant-and-retry remediation + this doc reference, and tails the upstream message when present. Built by `result_connector_http_403` in `operations/_errors.py` (reading `exc.response`); caught by the dispatcher's `_run_branch_with_error_handling` **scoped to 403** (alongside the 422 sibling below, in the same `httpx.HTTPStatusError` arm) ahead of the generic `connector_error` catch — 401 now routes to the `connector_auth_failed` row below (#1804), and every other `HTTPStatusError` status (429, 5xx) falls through unchanged. Extends #1627's dispatch structured-cause pattern to the transport-error sibling. Before #1649 this flattened to a bare `connector_error: HTTPStatusError` with GitHub's actionable 403 body + headers buried in / lost from `extras.exception_message`. | compliant (structured) — landed in #1649 | 401 (auth) landed in #1804 (`connector_auth_failed` row below); 429 (rate-limit) is a deliberate follow-up, not this surface |
| Connector upstream **422 Unprocessable Entity** on dispatch (any connector; the gh-rest write whose request body the upstream rejected as invalid — the requestBody-mangling bug T5 #1656, consumer `claude-rdc-hetzner-dc#1138`) | structured `extras` with `error_code='connector_http_422'`, `http_status=422`, `upstream_message` (the upstream body's `message` field when JSON, else capped raw text, `null` when the body was empty), `validation_errors` (the upstream body's GitHub-style `errors[]` field-level array, echoed verbatim when present — empty `[]` for a non-GitHub 422 or one whose body carried no list `errors`); the `error` names the invalid-payload cause **connector-agnostically** (the upstream parsed the request but rejected its content — a request-shape matter, not a transport or permission fault), appends the inspect-`validation_errors`-correct-and-retry remediation + this doc reference, and tails the upstream message when present. Built by `result_connector_http_422` in `operations/_errors.py` (reading `exc.response`, sharing the `_http_upstream_message` extractor with the 403 builder); caught by the dispatcher's `_run_branch_with_error_handling` **scoped to 422** in the same `httpx.HTTPStatusError` arm as the 403 sibling, ahead of the generic `connector_error` catch. The 422 *detail* is complementary to the functional fix T5 #1656 (which stops the happy-path gh-write 422-ing); this row still covers genuine validation 422s after #1656 lands. Before #1649 this flattened to a bare `connector_error: HTTPStatusError` with GitHub's `Validation Failed` body + `errors[]` buried in / lost from `extras.exception_message`. | compliant (structured) — landed in #1649 | 401 (auth) landed in #1804 (`connector_auth_failed` row below); 429 (rate-limit) is a deliberate follow-up, not this surface |
| Connector **TLS certificate verification failure** on dispatch (any connector; a self-signed / internal-CA appliance — the log-sentry dogfood against a nested-lab vRLI, Initiative #1774) | structured `extras` with `error_code='connector_tls_verify_failed'`, `host` (the `target.host` the operator configured — not an info-leak, the operator's own value), `exception_class` (`ConnectError`), `exception_message` (the raw `[SSL: CERTIFICATE_VERIFY_FAILED]...` string preserved, capped at `_EXC_MESSAGE_CAP=256`), `remediation_secure`, `remediation_last_resort`; the `error` names the host then **both** remediations in preference order — the secure `SSL_CERT_FILE` / chart trust-bundle path **first** (verification stays on), then `verify_tls=false` as the audited per-target last resort with the MITM / credential-exposure caveat (the opt-in T1 #1780 adds) — and tails the doc reference. Built by `result_connector_tls_verify_failed` in `operations/_errors.py`; caught by the dispatcher's `_run_branch_with_error_handling` in an `except httpx.ConnectError` arm ahead of the generic `connector_error` catch, **narrowed** to TLS-verify failures via `isinstance(exc.__cause__, ssl.SSLCertVerificationError)` (with a `'CERTIFICATE_VERIFY_FAILED'` substring fallback). A `ConnectError` has no `.response`, so it skips the `HTTPStatusError` arm; before #1782 it flattened to a bare `connector_error: ConnectError` that discarded the SSL cause, leaving the operator with `[SSL: CERTIFICATE_VERIFY_FAILED]` and no guidance. Non-SSL `ConnectError`s (DNS, connection-refused, connect-timeout) fall through to `connector_error` unchanged — never mislabelled TLS. | compliant (structured) — landed in #1782 | a friendlier `connector_unreachable` code for non-SSL `ConnectError`s is a deliberate follow-up, not this surface |
| Connector upstream **auth/session failure** on dispatch (any connector; the vRLI dispatch the operator saw as opaque `connector_error (440)`, #1798) | structured `extras` with `error_code='connector_auth_failed'`, `http_status` (the actual auth-class status the upstream returned — `401` or `440`, not a hard-coded value), `host` (the `target.host` the operator configured — not an info-leak, the operator's own value, same posture as the TLS row and `/ui/auth/login`), `upstream_message` (the upstream body's `message` field when JSON, else capped raw text, `null` when the body was empty — shared `_http_upstream_message` extractor with the 403/422 builders); the `error` names the host, the status, the likely cause **connector-agnostically** (a session/credential expiry or a misconfigured `auth_model` — the **dispatch path** re-logs-in and retries once on a session-expiry status (`401` or vRLI's `440`) when the connector advertises `invalidate_session` (#2067), so when this row is *returned* the re-login *also* failed), appends the verify-the-Vault-credential/`auth_model`-and-retry remediation + a `docs/architecture/connector-auth.md` ref + this doc ref, and tails the upstream message when present. The recognised auth-status set depends on the connector: a **typed (hand-coded)** connector uses the module constant `_AUTH_FAILED_STATUSES = {401, 440}` (`401` load-bearing; `440` is vRLI's session-expiry status the team opted to recognise); a **profiled** connector instead declares its set once on `ExecutionProfile.expiry_statuses` (default `{401}`; vRLI `{401, 440}`) — the single source #1973 unifies across the session-retry harness and this classification arm — which the dispatcher threads into `is_auth_failed_status(status_code, expiry_statuses)` via `_profile_expiry_statuses(connector_instance)` (returns the profile's set, or `None` → the typed global). No per-status remediation grammar is introduced; the profile only parameterises the closed status set. Built by `result_connector_auth_failed` in `operations/_errors.py` (reading `exc.response`); the dispatcher's `_run_branch_with_error_handling` catches the `httpx.HTTPStatusError` and delegates to `_handle_http_status_error`, which (#2067) attempts the invalidate-and-retry-once recovery on an auth-class status before classifying — `result_connector_auth_failed` is returned only after the retry is exhausted (re-login failed), or immediately when the connector has no `invalidate_session` hook; the 403/422 siblings are classified in the same `_classify_http_status_error` map, **after** the 403/422 checks and **ahead of** the generic `connector_error` catch. The error audit row is deferred until a failure is actually returned, so a *recovered* call writes exactly one **success** row and no spurious error row. Retry-once is safe for non-idempotent verbs because a `401`/`440` is rejected pre-execution (no side effect); a 5xx/timeout is never retried. Before #1804 a 401/440 flattened to a bare `connector_error: HTTPStatusError` with the auth cause buried in `extras.exception_message` — exactly the diagnosability gap that made #1798's `connector_error (440)` look like a stub-auth problem; before #2067 the classification was correct but recovery was missing, so an expired vCenter (401) / vRLI (440) session hard-failed until a backplane restart. Every other `HTTPStatusError` status (404, 5xx, 429) falls through to `connector_error` unchanged. | compliant (structured) — landed in #1804; profile-declared set #1973; dispatch-path recovery #2067 | 429 (rate-limit) is a deliberate follow-up, not this surface |
| Connector **Vault permission denied** on dispatch (any connector; the vcf-logs target whose `secret_ref` pointed at a local-wrapper path outside the per-tenant subtree — consumer signal `target-secretref-outside-secret-meho-forbidden-no-failfast`, #2091) | structured `extras` with `error_code='connector_vault_forbidden'`, `secret_ref` (the target's configured ref, `null` for a target-less dispatch), `expected_secret_ref` (the exact `tenants/<tenant_id>/<name>` path the dispatcher derives via `tenant_secret_ref`, `null` when underivable), `exception_class` (`Forbidden`), `exception_message` (hvac's `permission denied, on GET <url>` — the operator-supplied path, never secret material; capped at `_EXC_MESSAGE_CAP=256`); the `error` names the target's `secret_ref`, the likely out-of-subtree cause, the `tenants/<tenant_id>/<name>` convention (#1723), the exact expected path, the stage-the-credential remediation, and the explicit **"do NOT widen the backplane's Vault policy"** warning (the policy is deploy-owned and re-applied on every upgrade — widening is the wrong fix the bare shape invited), tailing hvac's message as `Vault said: ...`. A target-less denial (a typed `vault.*` op rejected by the Vault ACL itself) gets a generic Vault-authorization shape with no fabricated `secret_ref` diagnosis. Built by `result_connector_vault_forbidden` in `operations/_errors.py`; caught by the dispatcher's `_run_branch_with_error_handling` in an `except hvac.exceptions.Forbidden` arm ahead of the generic `connector_error` catch. Login-phase denials never reach the arm (`vault_client_for_operator` wraps them into the `VaultClientError` family first); every other hvac error (`InvalidPath`, ...) falls through to `connector_error` unchanged. Before #2091 this flattened to a bare `connector_error: Forbidden` that read exactly like a missing Vault grant. The write-time sibling (`secret_ref_outside_tenant_scope` 422 above) rejects the misconfiguration at import; this row covers targets that predate the gate or a genuine Vault-policy drift. | compliant (structured) — landed in #2091 | — |
| `PATCH /api/v1/connectors/{id}/operations/{op_id}` 200 with `is_enabled=true` on an op whose resolved connector is the unconfigured ingest auto-shim (the *proactive* counterpart of the `connector_unsupported` dispatch row above) | structured `warnings[]` on the 200 `EditOpResponse` (the route returned 204 before #1630): `code='unreplaced_auto_shim'` (same vocabulary as the dispatch error's `extras.cause`), `connector_class`, `message` (what was applied, the guaranteed `connector_unsupported` dead end ahead, the register-the-per-product-subclass remediation + `docs/codebase/spec-ingestion.md` ref). Built by `enable_time_auto_shim_warnings` in `operations/ingest/_internals.py` over the `resolved_auto_shim_class` resolver replay (`connector_registration.py`); mirrored verbatim by the `meho.connector.edit_op` MCP tool's `warnings` key and rendered to stderr by `meho connector edit-op` as `warning (unreplaced_auto_shim): ...`. Advisory only — the write lands, warnings or not (a shim-backed op may be pre-enabled ahead of its subclass). | compliant (structured) — landed in #1630 | — |
| `POST /api/v1/runbooks/runs` 422 (`missing_params`) and `POST /api/v1/runbooks/runs/{run_id}/next` 422 (`verify_response_required` / `verify_response_mismatch`) | Pydantic validation-error LIST `detail` (`[{"loc", "msg", "type"}]`) with a per-case `type` discriminator, via the shared `http_for` emitter in `api/v1/_errors.py` (registered in `runbook_runs.py`) | compliant (schema-conformant) — landed in #1364. Conforms to the `HTTPValidationError` schema FastAPI auto-declares for the route's 422, so the Go CLI's oapi-codegen client (and any OpenAPI-generated SDK) deserializes it; replaces the prior `detail=str(exc)` string body that forced the CLI's `rawNextResponse` shim | — |
| `POST/PATCH /api/v1/runbooks/templates*` 422 (`invalid_kb_slug` on draft / edit / publish / deprecate) | Pydantic validation-error LIST `detail` with `type="invalid_kb_slug"`, `loc=["path","slug"]`, via the same `http_for` emitter (registered in `runbook_templates.py`) | compliant (schema-conformant) — landed in #1364 alongside the runs surface | — |
| `GET /api/v1/runbooks/templates/{slug}` 500 **and** `meho.runbook.show_template` -32603 (`template_body_validation_failed`) | structured `detail` dict (REST, declared in OpenAPI via `_SHOW_RESPONSES`) / `error.data` (MCP `McpInternalError`) with `error='template_body_validation_failed'`, `slug`, `version`, `errors[]` (each `{type, loc, msg}` — url/ctx/input stripped so no non-serialisable object rides the envelope), `message` naming the row + the migration-0054 / re-save remediation + `docs/codebase/runbook-template-hydration.md`. One shared builder `build_template_body_validation_detail` (`runbooks/hydration_errors.py`) so the REST `detail` and the MCP `error.data` can't drift — same cross-transport posture as the ingest envelopes (#777). | compliant (structured) — landed in #2239 (G0.30-T #2239). Before #2239 a legacy empty / whitespace-only step body (PR #2122's forward-only `min_length=1` tightening shipped with no data migration) re-validated on read through `_steps_from_storage` and leaked as a bare `text/plain` 500 (REST) / opaque `-32603 "internal error: ValidationError"` (MCP); the same shared pinned-template hydration sink broke `list_runs` **tenant-wide**. Migration `0054` backfills the offending rows (the durable fix); this envelope covers the residual / future malformed row. See `docs/codebase/runbook-template-hydration.md`. | — |

The table is the audit artefact's deliverable, not a separate
spreadsheet. Adding new error surfaces against the convention
extends this table at the same time the surface lands.

## OpenAPI-schema conformance for 422 bodies (#1364)

Distinct from the *code + message + data* convention above (which
governs the **content** of `HTTPException.detail`), this rule governs
the **shape** of 422 bodies specifically, so typed clients generated
from the OpenAPI spec can deserialize them.

FastAPI auto-declares every route's 422 response as the
`HTTPValidationError` model — a list under `detail`:

```json
{"detail": [{"loc": ["body", "<field>"], "msg": "...", "type": "..."}]}
```

The framework's own `RequestValidationError` handler emits that list
shape, but a hand-raised `HTTPException(status_code=422,
detail=str(exc))` emits `{"detail": "<string>"}` instead. The two
don't match the declared schema, so a strict codegen client (the Go
CLI's oapi-codegen client; an `openapi-python-client` /
`openapi-typescript` SDK) errors on the list-vs-string mismatch
rather than deserializing. Before #1364 the only mitigation was the
CLI's `rawNextResponse` shim that bypassed the typed parser and read
the raw bytes.

**The rule:** a route that raises a 422 from a typed exception emits
the body through the shared `http_for` emitter
(`backend/src/meho_backplane/api/v1/_errors.py`), which wraps the
detail into the validation-error list shape with a per-case `type`
discriminator a client keys on. Register each exception once at module
import via `register_error(exc_cls, status=..., type_tag=..., loc=...)`.
Non-422 statuses (400 / 403 / 404 / 409) keep the plain string-detail
body — their OpenAPI schemas don't declare a structured shape, so the
string form is conformant there.

The runbook routes are the first adopters (#1364). Other `api/v1/*`
surfaces that raise a 422 with `detail=str(exc)` have the same latent
mismatch; sweeping them is a follow-up, not part of #1364's scope.

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
  message; `/probe` 500 structured `fingerprint_failed` envelope via
  the route's `try/except Exception` wrap around
  `connector.fingerprint(...)`, T1 #1210; `POST /api/v1/targets`
  `unknown_product` 422 via `_build_unknown_product_detail`, T3 #1144).
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
  `missing_exp` / `signature_verification_failed` / `token_expired` /
  `token_not_yet_valid` / `malformed_jws` / residual `invalid_token`.
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
- **Signal 16** — `POST /api/v1/agent-principals` 503 used to
  surface the bare domain code `keycloak_admin_not_configured`
  without the env vars or doc reference. T7 #1148 landed the
  symmetric `/ui/auth/login` shape: the response detail now names
  `KEYCLOAK_ADMIN_URL` / `KEYCLOAK_ADMIN_CLIENT_ID` /
  `KEYCLOAK_ADMIN_CLIENT_SECRET` and points at
  `docs/cross-repo/keycloak-agent-client.md`. Audit row updated.
- **Signal 17** — `/ready` did not enumerate the gated features
  (`agent_runtime`, `ui_surface`, `audit_replay`, `approval_queue`)
  with `configured: bool` and `missing_env`, so the operator could
  not discover *what* needed to be configured to satisfy a
  `*_not_configured` error. T7 #1148 landed the `features` block
  on `/ready`; `meho_backplane.features.build_features_block` is
  the canonical builder. `docs/RELEASING.md` §6a walks operators
  through each gate post-deploy.
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
