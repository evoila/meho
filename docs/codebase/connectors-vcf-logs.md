# VCF Operations for Logs (vRLI) connector

## Overview

`backend/src/meho_backplane/connectors/vcf_logs/` ships
`VcfLogsConnector`, an `HttpConnector` subclass that authenticates
against vRLI 9.x with a **session-token Bearer** flow. The package
registers itself under the v2 connector registry triple
`(product="vrli", version="9.0", impl_id="vrli-rest")` at import
time; the chassis lifespan's `_eager_import_connectors` discovers it
during startup. The registered `product` is the dispatch-canonical
token `parse_connector_id("vrli-rest-9.0")` derives from the
`vrli-rest` impl_id, so a target carrying the natural `product="vrli"`
spelling resolves this hand-rolled connector rather than an auto-shim
(#1798).

This is one of the four VCF management-plane connectors landing under
Initiative #369 (G3.6). The wave is generic-ingested via G0.7 — this
Task ships only the **skeleton** (auth + fingerprint + probe + the
G0.6 dispatch shim). Most operations arrive in #834 via spec ingestion of
`vcf-logs-9.0/openapi.yaml`; the headline events query is the exception —
since #2295 it is the `vrli.event.query` **typed** op registered in code
(see Key types), dispatched on the connector session with zero catalog
ingest as the first conversion in Initiative #2266.

The connector imports its auth scaffolding from the shared
`connectors/_shared/vcf_auth.py` module (#841 G3.6-T13) — vRLI is the
session-token consumer of that shared module's `vcf_session_login`
helper. The session-expiry retry-once loop (440 or 401) wraps downstream
calls and lives in this connector module (not in the shared helper)
because the downstream paths differ per connector.

### Profile-derived auth + fingerprint (#1974, capstone of #1965)

The connector's declarative auth + fingerprint surfaces are now sourced
from the reviewed `VRLI_EXECUTION_PROFILE`
(`connectors/vcf_logs/profile.py`) rather than hand-coded literals — the
pilot that proves an `ExecutionProfile` can serve a shipped bespoke
connector's auth + fingerprint. The single declaration drives both the
typed connector and a profiled connector:

- the **session-create path** (`/api/v2/sessions`) comes from the
  profile's `session_login` named scheme spec (#1970);
- the **version endpoint** (`/api/v2/version`) and the `(public, build)`
  split come from the profile's fingerprint recipe — the `vrli_five_part`
  named splitter (#1972);
- the **session-expiry status set** `{401, 440}` is the profile's
  `expiry_statuses` (#1973), the one frozenset the retry seam narrows.

Two surfaces stay typed-only because the declarative profile cannot model
them: the per-target `provider` (`ActiveDirectory` / `vIDM`; the
`session_login` scheme hardcodes `"Local"`) and the fingerprint `extras`
(`release_name` / `version_full` / `patch`). The `ResultHandle`
large-result path is the connector-agnostic JSONFlux dispatch mechanism
(`connectors/schemas.ResultHandle` + `operations/jsonflux_reducer`), not
connector code, and is untouched. Per-method dispatch parity between the
typed and profiled paths is proven in
`tests/integration/test_connectors_vrli_profile_parity.py`.

## Key types

- **`VcfLogsConnector(HttpConnector)`** —
  `connectors/vcf_logs/connector.py`. Hand-rolled session-token
  connector with per-target token cache + Vault-sourced
  service-account credentials + session-expiry-driven re-login + retry-once
  (440 or 401).
- **`VcfLogsTargetLike`** — `connectors/vcf_logs/session.py`.
  Runtime-checkable Protocol extending the cross-connector
  `VcfTargetLike` with an optional `provider` field
  (`"Local"` / `"ActiveDirectory"` / `"vIDM"`). The concrete `Target`
  model satisfies it structurally once the column lands.
- **Ingested browse breadth** — the reads outside the typed events query
  (`vrli.about`, `vrli.aggregated.query`, `vrli.field.list`, `vrli.host.list`,
  `vrli.content.pack.list`, `vrli.alert.list`) land as ordinary
  `source_kind="ingested"` `endpoint_descriptor` rows via G0.7 spec ingestion
  and are enabled through the generic review flow
  (`ReviewService.enable_reads`). The hand-curated ingested-enable apparatus
  (`core_ops.py`, with its `VRLI_CORE_OPS` / `VRLI_CORE_GROUPS` /
  `apply_vrli_core_curation` / `classify_vrli_op` symbols) was retired in #2358
  (T7 of #2266); read enablement is now generic, with no per-product curation
  code.
- **`VRLI_EVENT_QUERY_OP` / `event_query_impl` / `register_vrli_typed_operations`** —
  `connectors/vcf_logs/typed_ops.py` (#2295). vRLI's first
  `source_kind="typed"` op, `vrli.event.query`: a bound method on the
  connector (`VcfLogsConnector.event_query`) that issues
  `GET /api/v2/events/<constraints>` (optional `limit`) directly on the
  connector session via `_get_json_with_session_retry`. Registered at
  lifespan through the typed-op registrar (queued in the package
  `__init__`), so it dispatches with **zero catalog ingest** — no
  `endpoint_descriptor` ingested row. `build_event_query_path` renders the
  reserved-expansion constraint sub-path with literal slashes (#2003/#2066),
  and rejects a `..` traversal dot-segment (shared
  `operations/_rfc6570.has_dot_segment`) before encoding — the literal slashes
  that carry a genuine chain are also what a `..` would use to climb out of
  `/api/v2/events/`; `?`/`#` are outside the safe set so a constraint can never
  open a query string or fragment (#S04).
  Mirrors `vmware_rest/typed_ops.py` (`vmware.host.usage`, #2257) and
  `argocd/ops.py`. Its contract meets the VMware operation-contract check
  (`backend/tests/test_operation_contracts.py`): one example call under
  `examples`, and a response schema whose event fields (`text`, `timestamp`,
  `timestampString`, `fields[]`) come from `events.get.response` in the pinned
  `vcf-operations-for-logs-openapi.json`. Host name and source are rows of
  `fields[]`, not top-level event keys.
- **`vrli_api_file_get_impl`**, **`check_path`**, **`DOCS_PAGES`**,
  **`DOCS_FOLDERS`** (`connectors/vcf_logs/api_file.py`) and
  **`VRLI_API_FILE_GET_OP`** (`connectors/vcf_logs/typed_ops_api_file.py`)
  — the body, the path rule and the metadata of `vrli.api_file.get`, the
  second typed read (see "The read: `vrli.api_file.get`" below). The
  fetch, the parts and the secret check are the shared steps in
  `connectors/_shared/api_file_read.py` and
  `connectors/_shared/api_file_secrets.py`. The handler is
  `VcfLogsConnector.api_file_get`; the 8.x subclass inherits it.
- **Re-exports of the shared module**: `VcfCredentialsLoader`,
  `load_credentials_from_vault` (default stub),
  `SessionLoginError` — callers should import these from
  `meho_backplane.connectors.vcf_logs` rather than reaching into
  `_shared`.

## Auth contract

**Verified against the consumer wrapper `scripts/vcf-logs.sh`
(2026-05-21 snapshot)** in `evoila-bosnia/claude-rdc-hetzner-dc` and
the vRLI 9.x REST API documentation. The wrapper is the authoritative
contract for the field shapes the appliance actually accepts.

### Login round-trip

```http
POST /api/v2/sessions
Content-Type: application/json
Accept: application/json

{"username": "<svc-account>", "password": "<…>", "provider": "Local"}
```

- `provider` field defaults to `"Local"` when the target leaves it
  unset (`vcf-logs.sh` line 95). `"ActiveDirectory"` and `"vIDM"` are
  the documented alternatives; v0.2 supports `Local` + `AD` only.
- Response body shape: `{"sessionId": "<token>", "ttl": <seconds>}`
  (`vcf-logs.sh` line 142 extracts `.sessionId`).

### Downstream calls

```http
GET /api/v2/<path>
Authorization: Bearer <sessionId>
Content-Type: application/json
Accept: application/json
```

The connector caches the `sessionId` per `target.name` in
`_session_tokens`; subsequent `auth_headers()` calls reuse the cached
value without a fresh login.

### Session-expiry retry-once (440 or 401)

`_get_json_with_session_retry()` wraps `HttpConnector._get_json` and
handles the session-expiry case. vRLI signals an expired session two
ways and the connector recovers from both
(`_SESSION_EXPIRED_STATUSES = {401, 440}`):

- **`440`** — vRLI's own `trait.authenticated.440`: *"the session ID has
  expired; obtain a new session ID from `/api/v2/sessions`"* (carried by
  117 endpoints in the spec). This is the case that bites in practice:
  vRLI idle-expires the in-memory session, so the call after an idle gap
  returns 440. **This is the recoverable case** — re-login fixes it.
- **`401`** — `trait.authenticated.401`: missing/invalid `Authorization`
  header or session ID.

The loop:

1. Issue the GET with the cached Bearer header.
2. On HTTP 440 **or** 401 → invalidate the cached `sessionId`
   (credentials cache untouched — a 440/401 means the session expired or
   was rejected, not that the creds are wrong) and re-login.
3. Retry the GET once with the fresh token.
4. If the retry also returns 440/401 → raise `RuntimeError` naming the
   target and the status.

This is the same posture the NSX precedent established (re-login once,
not a retry loop) — a misconfigured credential pair fails fast instead
of hammering vRLI's audit log.

Before #1909 the trigger keyed strictly on `401`, so a 440 fell straight
through unretried: the first call after a backplane start worked (fresh
cached session), vRLI idle-expired the session, and every subsequent call
returned 440 until a backplane restart cleared the in-memory token cache —
breaking any scheduled / long-running vRLI consumer. The
dispatcher-side classification of 440 → structured `connector_auth_failed`
(#1804, `operations/_errors.py`) only fixed the *diagnosability* of the
flat error.

**`_get_json_with_session_retry` is not on the dispatch path (#2067).**
This helper recovers a 440/401 only for a *direct* caller. vRLI's ingested
read ops dispatch as `source_kind='ingested'` through
`operations/_branches.py::dispatch_ingested` → `HttpConnector._request_json`
— they never call this wrapper, so #1909's 440-recovery never executed for
the dispatched ops the soak actually exercises, and the 440-until-restart
symptom persisted. #2067 closes that by wiring invalidate-and-retry-once
into the dispatcher's auth-class arm: the connector exposes a public
`invalidate_session(target)` (delegating to `_invalidate_session`), and on a
440/401 the dispatch path evicts the token and re-dispatches once — so a
dispatched vRLI GET recovers the same way a direct
`_get_json_with_session_retry` call does. The helper is retained for its
direct callers and shares the one `_invalidate_session` eviction.

Since #2295 the typed `vrli.event.query` op is one of those direct callers:
its handler routes through `_get_json_with_session_retry`, so it recovers a
440/401 via the helper's own re-login + retry-once rather than the #2067
dispatch-path seam (a typed handler owns its transport call, so the
dispatch-path `invalidate_session` hook does not wrap it). The remaining six
ingested read ops still dispatch as `source_kind='ingested'` and rely on the
#2067 dispatch-path recovery.

### Fingerprint + probe

`fingerprint()` issues an **unauthenticated** `GET /api/v2/version`.
The wrapper's probe mode auths first defensively, but the appliance
accepts the version call without a session — the connector takes the
cleaner unauthenticated path so a vRLI with valid TLS but broken
credentials still reports a reachable fingerprint.

Response shape (mirroring the wrapper's probe output):

```json
{
  "version": "9.0.0",
  "build": "21761695",
  "vendor": "vmware",
  "product": "vrli",
  "extras": {
    "release_name": "VMware Aria Operations for Logs 9.0",
    "version_full": "9.0.0.0.21761695",
    "patch": "0"
  }
}
```

The version-string parsing matches `vcf-logs.sh` lines 226-251: the
appliance reports `version` as a dot-separated tuple
(e.g. `"9.0.0.0.21761695"`); `parts[0:3]` is the public version,
`parts[3]` is the patch, `parts[4]` is the build.

`probe()` delegates to `fingerprint()` and returns
`ProbeResult(ok=True)` when reachable, otherwise
`ProbeResult(ok=False, reason=<error>)`.

## Auth model gating

`auth_headers()` rejects any `auth_model` other than
`shared_service_account` (or `None` for pre-G0.3
column-not-yet-populated targets) with `NotImplementedError` naming
both the target and the requested mode. Per-user and impersonation
modes are deferred to v0.2.next — same posture vSphere / NSX / VCF
Automation take.

## Control flow

```text
caller wants to issue an authenticated GET:
  ├─ _get_json_with_session_retry(target, path, operator)
  │    ├─ _get_json(target, path, operator=operator)
  │    │    └─ HttpConnector._request_json (retry on 5xx / conn err)
  │    │         ├─ auth_headers(target, operator)
  │    │         │    └─ _session_token(target)
  │    │         │         ├─ _credentials.get(target)
  │    │         │         │    └─ user-injected or Vault loader
  │    │         │         └─ vcf_session_login(...)
  │    │         │              └─ POST /api/v2/sessions
  │    │         └─ client.request("GET", path, ...)
  │    │              └─ on 200 → return resp.json()
  │    │              └─ on 440 / 401 → raise HTTPStatusError
  │    └─ on HTTPStatusError(440 or 401):    # _SESSION_EXPIRED_STATUSES
  │         ├─ _invalidate_session(target)       # drop cached sessionId
  │         └─ retry _get_json(...) once
  │              └─ on 440 / 401 again → RuntimeError "after refresh"
```

The credentials cache (`_credentials`, a shared `CredentialsCache`
keyed on `target.name`) is touched once per target lifetime; the
session-token cache (`_session_tokens`) is touched once per
target-session lifetime (initial login + any session-expiry-driven
re-login on 440 or 401). Both caches are flushed on `aclose()`.

## Typed reads

**Versions** says where the call exists: the product versions of the
connector implementations that register it. Both typed reads are
registered on `vrli-rest-9.0` (Logs 9.0) and, through the reused tuple
`VRLI_TYPED_OPS`, on `vrli-vrli8-8.0` (Logs 8.x).

| Operation | What it does | Vendor call | Risk | Versions |
|---|---|---|---|---|
| `vrli.event.query` | Query raw log events by a constraint chain, with an optional limit. | `GET /api/v2/events/{constraints}` | read (`safe`, no approval) | 8.x, 9.0 |
| `vrli.api_file.get` | Read one API reference page or API description file the system serves. Returns path, HTTP status, content type, size, SHA-256, the number of replaced secret values and the system version and build; the text of the checked copy is in parts behind a result handle. | `GET {path}`; version and build from `GET /api/v2/version` | read (`safe`, no approval) | 8.x, 9.0 |

### The read: `vrli.api_file.get`

Why it exists: some calls that VMware's own tools send to Logs 8.18 and
9.0 (agent groups, content packs, the alert update) appear in no vendor
reference and not in the pinned 9.0 API file. Only the reference that a
running system serves can show them. This read is how the lab pins those
pages. Its first live use is on a Logs 8.18 and a 9.0 system; if a page
loads its file from a path the rule refuses, file a bug for this read
with the exact path, and a small change adds it.

Address: the vendor writes the reference as `http://{host}/rest-api`, the
web address of the system, not the API port 9543. The read uses the
target's own address. If that address is the API port and the page is
not served there, the status in the answer shows it.

Input: one field, `path`. Before any vendor call the path is checked
against this rule:

1. allowed: the documentation page `/rest-api` (the API reference) and
   every path under the folder `/rest-api/` (for example
   `/rest-api/supported`, the Tech Preview API pages);
2. allowed: a Swagger or OpenAPI file at its usual place: a path with a
   part named `api-docs`, `swagger-resources` or `swagger-config`, or a
   path ending in `swagger.json`, `swagger.yaml`, `openapi.json` or
   `openapi.yaml`;
3. refused: everything else, and any path with `..`, `//`, `\`, an
   encoded dot or slash (`%2e`, `%2f`), a semicolon (`;`), a scheme, a
   host name, a fragment, whitespace or a control character. A query
   string may hold only `group` and `apiVersion`.

A refused path stops with an error and sends no HTTP request. So an
agent cannot use this read to fetch `/api/v2/hosts` or any other data
path.

Then the read sends one `GET` to the target's own address with the
target's own session token. It does not follow a redirect: a `3xx`
answer is reported with its status, and `redirect_to` holds the path to
read next (the same rule applies to that path). A relative `Location`,
such as `swagger.json` or `?group=x`, is resolved against the path that
was read, so `redirect_to` is always a full path. A `Location` on another
host is reported as `scheme://host/path`, so the caller sees it cannot be
read through this target. A `401`, or the system's own `440` ("the
session has expired"), is raised so the dispatcher drops the cached
session, logs in once more and asks once more. When the second try fails
too, the dispatcher copies the body of that answer into the error result.
So the read checks that body first, the same way as a file (see below):
the error result gets the checked copy, or no body when it is not UTF-8
text or does not parse. The status stays, so the retry still runs. Every
other status (for example `404`) is reported in `status` with the checked
copy of the body as text.

The fetch is the shared step in `connectors/_shared/api_file_read.py`.
The body is read in a stream. The read asks for `gzip` or `deflate` only
and decodes the wire chunks itself with `zlib`, in steps that never
produce more than the room left under the limit. So a small compressed
answer that decodes to a file over the limit is refused at the limit. The
read returns the whole file or fails: a body whose compressed stream ends
early is refused, and so are bytes after a `deflate` stream. A `gzip`
body made of several members is decoded whole, under the same limit. An
answer with any other content encoding is refused with the encoding
named.

Only UTF-8 text comes back. A content type that is not text (anything
other than `text/*`, JSON, YAML, XML or JavaScript) or a body that is not
valid UTF-8 is refused, and the error names the content type.

Secrets: the read gives the whole text to the shared secret check
(`check_api_file`, see `connectors-shared-api-file-secrets.md`) before it
cuts the text into parts. The check parses the file as JSON, YAML, XML or
HTML and replaces every value under a secret name with
`[REDACTED:param_name]`, at any depth and however it is written (quoted
or not, with quotes or spaces inside, any length, a YAML `|` or `>`
block). A secret name is a name like `password`, `token`, `licenseKey`
or `sessionId` (the login token the session API of this system returns).
A PEM private key is replaced under any name. A file that does not
parse, or whose format is unknown (JavaScript, CSS, plain text), is
refused with the reason and never the text. A redirect's query string
goes through the same name check. `secrets_replaced` counts every
replaced value.

A page or file can also hold free text that looks like a secret. The
vendor's Logs 9.0 API file has `Authorization: Bearer ...` examples in
its descriptions. So the check also runs MEHO's general secret-redaction
rules on every parsed text value before the file is written back
(`text_redactor`). Text they match comes back as a placeholder such as
`[REDACTED:authorization_header]`, and `secrets_replaced` counts it too.
The copy keeps its format, and `size`, `sha256` and the parts describe
it. The dispatcher runs the same rules on every answer before the caller
sees it. As a last guard, the read runs them once more on the whole
copy and on its answer, and stops with an error if they would still
change something. So `sha256` always names what the caller gets.

The flight recorder (the trace of each vendor call) gets the checked copy
of the body, never the body as it came. When the file is refused, the
recorder gets no body.

Version and build come from the connector's own version detection
(`fingerprint`, the unauthenticated `GET /api/v2/version`). If that read
fails (no connection, an error status, an answer with no `version`), the
file still comes back: `version` and `build` are `null` and
`version_error` names the reason.

The answer:

| Field | Meaning |
|---|---|
| `path` | The path that was read, with its query string. |
| `status` | The HTTP status. |
| `content_type` | The `Content-Type` header. |
| `size` | Size of the checked copy in bytes. |
| `sha256` | SHA-256 of the checked copy, hex. |
| `secrets_replaced` | How many values were replaced: values under a secret name (`[REDACTED:param_name]`) and text the general redaction rules match. |
| `version`, `build` | The system version and build, as the fingerprint reports them. |
| `version_error` | Why the version read failed; `null` when it worked. The file still comes back. |
| `redirect_to` | Path of a redirect answer; `null` otherwise. |
| `parts` | How many text parts there are. |
| `items` | The parts of the checked copy, `{index, text}`, at most 64 KiB each, in order. |

### Rebuilding a file from the parts

The checked copy is split on UTF-8 character boundaries into parts of at
most 64 KiB, so each part decodes on its own. Joining the `text` of every
part in `index` order gives the checked copy; the SHA-256 of the joined
bytes equals `sha256`. The copy is the parsed file written back: the same
content without comments, with secret values replaced. JSON comes back
compact.

A short page (under about 4 KB and 50 parts) comes back inline. A longer
file is set-shaped, so MEHO stores the parts behind a result handle and
keeps the scalar fields (`sha256`, `size`, `version`, ...) inline. Read
the parts with `result_query` in `index` order and join them.

Why parts and not lines: a served file can be one long line. One result
handle keeps at most 10,000 rows (`result_handle_max_spill_rows`) and
16 MiB (`result_handle_max_record_bytes`), and one `result_query` answer
is at most 256 KiB (`result_query_max_output_bytes`). The byte limit
applies to the whole stored record: the parts, the record's own fields
and the answer's scalars. The read measures that record with
`rows_record_bytes` from the result store before it answers. A file whose
record does not fit stops with an error that names the file's size and
the record's size. The read never returns a cut file.

Tests: `backend/tests/test_connectors_vcf_logs_api_file.py`.

## Dependencies

- `connectors/_shared/vcf_auth.py` (#841 G3.6-T13) — provides
  `CredentialsCache`, `vcf_session_login`, `is_acceptable_auth_model`,
  `SessionLoginError`, the default `load_credentials_from_vault` stub.
- `connectors/adapters/http.py` — `HttpConnector` base with the
  per-target httpx client pool, tenacity retry decorator on
  `_request_json`, and `aclose()` plumbing.
- `connectors/registry.py` — `register_connector_v2` for the
  module-import-time registration.
- `connectors/schemas.py` — `AuthModel`, `FingerprintResult`,
  `ProbeResult`, `OperationResult`.

External: `httpx>=0.27` (Bearer header + `AsyncClient`), `structlog`
(observability), `respx>=0.21` (test mocks only).

## Known issues

- The default credentials loader
  (`_shared.vcf_auth.load_credentials_from_vault`) is a stub that
  raises `NotImplementedError`; production deploys must inject a
  custom loader on connector construction until the operator-context
  Vault read path is wired for the VCF management-plane connectors
  (tracked under Goal #214).
- No proactive token refresh. vRLI's session has a documented TTL
  (default 30 days but operator-tunable) and also idle-expires; the
  connector relies on the session-expiry retry layer (440 or 401) to
  recover on demand rather than refreshing ahead of time. Acceptable in
  v0.2; revisit if operator-side cache TTLs drop below the round-trip
  timing.
- **The documentation paths of `vrli.api_file.get` come from the vendor's
  developer resources for 8.18.** Their first live use is the pinning of
  the Logs 8.18 and 9.0 reference pages. If a page loads its file from a
  path the rule refuses, file a bug for this read with the exact path; a
  small change adds it. The reconcile lane lists `/rest-api` and
  `/rest-api/` as `GET`, marked "documentation path, not in the API
  file", and keeps them out of the served check.
- `vrli.api_file.get` replaces values by their name. Free text in a page
  (prose, a `<pre>` block that is not JSON) has no names; only MEHO's
  general redaction rules check it, and they replace only what they
  match. A `<pre>` or `<code>` block that a page colours with child
  elements comes back only when its text is one JSON or YAML document
  with nothing to replace; any other such block is withheld whole. A
  licence key under a name that is not a secret name (the Logs licence
  API calls it `key`) is not replaced.
- No DELETE-revoke on `aclose()` — a per-target network call during
  lifespan shutdown is more risk than benefit (same posture NSX
  takes). Revoke-on-close is v0.2.next.

## References

- Task: <https://github.com/evoila/meho/issues/830>
  (skeleton + auth) and
  <https://github.com/evoila/meho/issues/834>
  (spec ingestion + ingested read enablement)
- Parent initiative: <https://github.com/evoila/meho/issues/369>
- Parent goal: <https://github.com/evoila/meho/issues/214>
- Canary runbook: [`docs/cross-repo/g36-vrli-canary.md`](../cross-repo/g36-vrli-canary.md)
- Shared auth module: `connectors/_shared/vcf_auth.py` (#841)
- vRLI API:
  <https://developer.broadcom.com/xapis/vrealize-log-insight-api/latest/>
- Consumer wrapper: `scripts/vcf-logs.sh` in the consumer's private repo.
- Sibling skeleton precedent: NSX (`connectors/nsx/`) for the
  session-token + 401 retry-once pattern; VCF Automation
  (`connectors/vcf_automation/`) for the most recent VCF management-
  plane skeleton.
- CLI verb tree (G3.6-T6 #838):
  [`cli/internal/cmd/vcf-logs/`](../../cli/internal/cmd/vcf-logs/) —
  thin Cobra wrappers over `POST /api/v1/operations/call` with
  `connector_id="vrli-rest-9.0"` pre-baked, mirroring the NSX /
  SDDC Manager alias-verb pattern.
- Operator-facing recipe (G3.6-T6 #838):
  [`docs/cross-repo/vcf-logs-onboarding.md`](../cross-repo/vcf-logs-onboarding.md).
- End-to-end recorded-fixture coverage (G3.6-T6 #838):
  [`backend/tests/test_connectors_vcf_logs_e2e.py`](../../backend/tests/test_connectors_vcf_logs_e2e.py)
  — exercises the 6 ingested ops through the full dispatcher, the
  session-establish + session-expiry retry-once (440 and 401) +
  second-expiry-fails paths via `_get_json_with_session_retry`, the
  audit-row contract, and the JSONFlux handle path on the typed
  `vrli.event.query`.
- Spec-reconcile guard (#2993):
  [`backend/tests/test_connectors_vcf_logs_spec_reconcile.py`](../../backend/tests/test_connectors_vcf_logs_spec_reconcile.py)
  asserts the hand-coded version / events / session-login paths against the
  already-pinned `vcf-operations-9.0/vcf-operations-for-logs-openapi.json`
  (public Apache-2.0, `vmware/vcf-api-specs`). Armed on every unit sweep; the
  lane extracts the served set directly because the vendor doc trips
  `parse_openapi`'s metaschema gate on a malformed `Bearer` scheme (see the
  vcf-logs extension in
  [`docs/decisions/vendor-spec-ci-provisioning.md`](../decisions/vendor-spec-ci-provisioning.md)).
- The API reference read (`vrli.api_file.get`):
  <https://github.com/evoila/meho/issues/4609>; the same read for NSX:
  <https://github.com/evoila/meho/issues/4395>. Tests:
  [`backend/tests/test_connectors_vcf_logs_api_file.py`](../../backend/tests/test_connectors_vcf_logs_api_file.py).
- Typed events-query coverage (#2295):
  [`backend/tests/test_connectors_vcf_logs_typed_event_query.py`](../../backend/tests/test_connectors_vcf_logs_typed_event_query.py)
  — dispatch-level `source_kind="typed"` dispatch on a fresh boot, the
  440 recovery on the typed path, the reserved-constraint literal-slash
  wire path, the `limit` query param, and the registration invariant
  (no ingested events row).
