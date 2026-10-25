# Connector: vcf-operations (VCF Operations / vROps 9.0)

## Overview

The `vcf-operations` connector is the hand-rolled `HttpConnector` subclass
that dispatches VMware Aria Operations (formerly vRealize Operations Manager;
"vROps") REST operations under the
`(product="vrops", version="9.0", impl_id="vrops-rest")` registry triple.
G3.6-T1 (#829) shipped the skeleton; #2395 rebuilt its auth on an
acquired-token session (`OpsToken`) after live VCF Operations 9.0.2 rejected
the original stateless HTTP Basic. The audited read set ships as typed ops
(#2303), and so does VCF 9 licence management (#4089); the remaining
`/suite-api` breadth is ingested/curated.

Source: `backend/src/meho_backplane/connectors/vcf_operations/`.

The connector is **single-plane** (one API surface at `/suite-api/api/*`)
and **session-stateful**: it acquires a token via
`POST /suite-api/api/auth/token/acquire` and presents it as
`Authorization: OpsToken <token>` on every request, re-acquiring on an
idle-expired session through the dispatcher's #2067 seam. This mirrors the
vRLI (#830) token-session shape; Fleet #831 adds a product-specific
fingerprint path, Automation #832 adds dual-plane auth + vhost routing.

## Key types

- **`VcfOperationsConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="vrops"`, `version="9.0"`,
  `impl_id="vrops-rest"`, `supported_version_range=">=9.0,<10.0"`,
  `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim defensively if both somehow register for the same triple.
  Holds a per-target session-token cache (`_session_tokens`, keyed on the
  tenant-unique `(tenant_id, id)` tuple) plus the shared `CredentialsCache`.
- **`VcfOperationsTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: the shared
  `name` / `host` / `port` / `secret_ref` / `auth_model` quintet plus one
  vROps-specific field, `auth_source` (optional identity-domain label).
  Replaced by the concrete `Target` model once `auth_source` lands as a
  column in `meho_backplane.targets` (G0.3 #224); the model satisfies the
  Protocol structurally without code edits here.
- **`VcfOperationsCredentialsLoader`** (`session.py`) — type alias for the
  shared `VcfCredentialsLoader` callable that resolves a target to
  `{"username": ..., "password": ...}`. Injectable on connector construction
  (`VcfOperationsConnector(credentials_loader=...)`) so unit tests,
  integration tests, and pre-G0.3 production deploys override the default
  Vault loader.
- **`load_credentials_from_vault`** (re-exported from
  `connectors/_shared/vcf_auth.py`) — default loader; the live
  operator-context per-target Vault KV-v2 read (G3.10-T2 #946). The same
  loader serves vROps / vRLI / Fleet (#841 lifted the helper into `_shared`).
- **`SessionLoginError`** (re-exported from `connectors/_shared/vcf_auth.py`)
  — raised when `token/acquire` returns a non-2xx or a token-less 2xx; its
  `ConnectorAuthError` subclass carries the 401/403 establish-auth cause the
  dispatcher maps to `connector_auth_failed`.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.vcf_operations` triggers the
   module-level
   `register_connector_v2(product="vrops", version="9.0", impl_id="vrops-rest", cls=VcfOperationsConnector)`
   call.
3. The registry's v2 table now resolves `("vrops", "9.0", "vrops-rest")` to
   `VcfOperationsConnector`. The G0.7 auto-shim's idempotency check (in
   `ensure_connector_class_registered`) no-ops on subsequent ingests against
   the same triple.

The v1 `register_connector` entry point is deliberately **not** called —
the v1 entry would land as `("vrops", "", "")` and confuse
`resolve_connector`'s tie-break ladder. Same pattern Harbor / SDDC Manager
/ NSX / VCF Automation established. (A separate wildcard-fallback
`("vrops", "", "")` v2 entry is registered so an unfingerprinted target
still resolves — the versioned entry wins the tie-break when both exist.)

### Authentication (acquired-token session, `OpsToken`)

Live VCF Operations 9.0.2 rejects stateless HTTP Basic on
`/suite-api/api/*` (the earlier skeleton's design assumption was false —
#2395). The connector establishes a session token and presents it on every
request. The flow:

1. `auth_headers(target, operator)` checks `target.auth_model` via the
   shared `is_acceptable_auth_model` predicate. Anything other than
   `shared_service_account` / the enum member / `None` (the pre-G0.3
   sentinel) raises `NotImplementedError` naming both the target and the
   requested mode.
2. `_session_token(target, operator)` returns the cached token on a hit.
   On first use (under a per-connector lock, keyed on the tenant-unique
   `(tenant_id, id)` tuple) it loads credentials via the injected
   `VcfOperationsCredentialsLoader` (cached in the shared `CredentialsCache`;
   missing `"username"`/`"password"` surfaces as `RuntimeError` naming the
   target) and POSTs them to `POST /suite-api/api/auth/token/acquire`
   through the shared `vcf_session_login` helper. The 200 body
   `{"token", "validity", "expiresAt", "roles"}` yields `token`, which is
   cached.
3. Returns `{"Authorization": "OpsToken <token>"}`. The 9.x-native
   `OpsToken` scheme is preferred over the legacy `vRealizeOpsToken` alias
   (the connector pins `>=9.0,<10.0`); neither `Basic` nor `Bearer` is
   accepted by the appliance.

The full `operator` is threaded through so the live default loader reads
the per-target Vault secret under the operator's identity. An empty
`operator.raw_jwt` fails closed (`VaultCredentialsReadError`) before the
cache lookup, so a token primed by an authenticated caller can't leak to a
system-initiated caller.

Both dispatch paths — the typed reads and the generic-ingested path — attach
auth through the same `auth_headers` seam (`adapters/http.py` at the
`_request_json` and `_post_json` request sites), so both carry `OpsToken`.

### Optional `authSource` federation

vROps can federate identity through multiple sources (the local realm,
`vIDM`, an Active Directory realm name, etc.). When `target.auth_source`
is set, it rides the **`token/acquire` body** as `"authSource"` — its
token-era home. When unset (`None` / `""`), the field is omitted and vROps
authenticates against its default local realm. The accepted values are
operator-configured per vROps deployment; the connector passes the string
through verbatim. (The pre-token skeleton rode this as a `?auth-source=`
query parameter on every request; that mechanism is deleted — the appliance
reads `authSource` only at acquire time.)

### Session-expiry recovery (the #2067 seam)

The connector advertises a duck-typed `invalidate_session(target)` hook. On
an auth-class status (401) from a dispatched op, the generic-ingested
dispatch path evicts the cached token via this hook and re-dispatches the op
exactly once (G0.29-T2 #2067) — so an idle-expired session re-acquires there
rather than failing until a process restart. A second auth failure (the
re-acquire also failed) falls through to `connector_auth_failed`. A 401/403
at `token/acquire` itself surfaces as `connector_auth_failed` with
`cause=session_establish_401` / `_403` via the shared `ConnectorAuthError`.

The typed connector carries no `ExecutionProfile`, so the dispatcher
classifies its 401 against the typed-connector global auth-failed set
(`{401, 440}`). The base `HttpConnector._request_json` tenacity decorator
(3 retries, exponential backoff, connection errors + 5xx only — never 4xx)
is inherited unchanged.

### Fingerprint

`GET /suite-api/api/versions/current` returns a JSON payload shaped
`{"releaseName": "...", "buildNumber": <int>, "humanlyReadableReleaseName"?: "..."}`.
The connector lifts:

- `releaseName` → `FingerprintResult.version`.
- `buildNumber` → `FingerprintResult.build` (stringified — the wire-level
  field is an integer; the result-model field is `str | None`).
- `humanlyReadableReleaseName` → `extras["humanly_readable_release_name"]`
  (some 9.0 builds emit it, some don't).

The version call rides the connector's `OpsToken` session like every other
read (through `_get_json`), so a credential / session-establish failure is
part of the fingerprint's failure surface.

On transport, session-establish, or status failure, the result carries
`reachable=False` and `extras["error"]` set to `f"{type(exc).__name__}: {exc}"`
— the same pattern Harbor / SDDC Manager / NSX use.

### Probe

`probe(target)` delegates to `fingerprint(target)`. vROps does not expose
a dedicated `/health` endpoint distinct from the version surface, so the
fingerprint call is the right reachability probe. `reachable=True` ⇒
`ok=True`; `reachable=False` ⇒ `ok=False` with `extras["error"]`
surfaced as the probe's `reason`. Harbor's purpose-built
`/api/v2.0/health` is the exception, not the rule.

### Dispatch shim

`execute(target, op_id, params)` synthesises a minimal `Operator`
(nil-UUID tenant_id + `sub="system:vcf-operations-connector-shim"`) and
delegates to `meho_backplane.operations.dispatch` with
`connector_id="vrops-rest-9.0"`. Pre-G0.6 chassis routes reach the
dispatcher through this shim; post-G0.6 callers (the
`/api/v1/operations/call` route, MCP `call_operation`, and the
`meho vcf-operations …` CLI verbs added in #837) construct a real
`Operator` and call `dispatch` themselves.

The connector exposes two operation surfaces: a **typed** read set
(below) that works with zero catalog ingest, plus the **ingested**
`/suite-api` breadth catalog seeded from `tests.acceptance._vrops_canary_fixtures`.

### Typed read operations (#2303, Initiative #2266 T3)

The adopter's *audited* vROps read set (audit #2294) ships as **typed**
ops (`source_kind="typed"`) in `typed_ops.py`, dispatched directly on the
connector's `OpsToken` session — no ingested descriptor row, so they work
on a fresh boot with zero catalog ingest (the #2262 no-shadow invariant).
Handlers are bound methods on
`VcfOperationsConnector`; a module-level `register_vcf_operations_typed_operations`
registrar is queued via `register_typed_op_registrar` in the package
`__init__` and run by the lifespan.

- **`vrops.liveness`** — `GET /suite-api/api/versions/current`. Appliance
  liveness + identity (release/build); the same surface `probe()` uses.
  The adopter named the probe `casa/health`, but the CaSA API is
  private/undocumented, so the documented version surface is the grounded
  liveness op. Supersedes the former curated `vrops.about`.
- **`vrops.alert.list`** — `GET /suite-api/api/alerts`. Alert triage,
  filtered by `activeOnly` / `alertCriticality` / `alertStatus` /
  `resourceId` with pagination. Supersedes the curated
  `GET:/suite-api/api/alerts` ingested row.
- **`vrops.resource.query`** — `POST /suite-api/api/resources/query`. A
  body-shaped POST carrying a typed `ResourceQuerySpec` subset (match on
  `resourceKind` / `name` / `regex` / `adapterKind` / state / status /
  health / parent / `statKey`), paginated via `page` / `pageSize` query
  params.
- **`vrops.resource.stats`** — `GET /suite-api/api/resources/stats/latest`
  (wave-2 sizing read, #2838). Returns the latest metric **values** vROps
  computed for named resources — current CPU demand, memory workload,
  capacity-remaining — the sizing / resource-pressure answer, closing the
  gap where the backplane could find resources (`vrops.resource.query`) and
  see alerts (`vrops.alert.list`) but never read the values vROps exists to
  compute. `resourceId` is a **required** list (`minItems=1`, typically fed
  from `vrops.resource.query`); the optional `statKey` list narrows to
  specific stat keys and the optional `currentOnly` boolean reports only
  current values. `resourceId` / `statKey` serialise to repeated query
  params. The `required`/`minItems` schema means the dispatcher's
  pre-execution param validation refuses an unbounded all-resource stats
  read before the handler runs. Unlike `vrops.resource.query`'s `statKey`
  (which filters resources by *whether* they report a stat), this op returns
  the stat's **value**; the handler passes the payload through unparsed so
  the vendor's `stats-of-resources` field names stay out of executable code.
- **`vrops.resource_kind.properties`** —
  `GET /suite-api/api/adapterkinds/{adapterKindKey}/resourcekinds/{resourceKindKey}/properties`.
  Lists which properties one resource kind of one adapter kind has (for
  example `VirtualMachine` of `VMWARE`): one row per property under
  `resourceTypeAttributes`, with `key`, `name`, `description`, `dataType2`
  and `unit`. An agent uses it to find the exact property key before it
  reads the value. Both keys are required and are percent-encoded into the
  path. The optional `keyContains` text keeps only the rows whose `key` or
  `name` contains it (not case-sensitive); MEHO filters the rows after the
  call, because the vendor's own `properties` filter takes exact names only.
  This op is **9.x only**: it sits in `VROPS_9_ONLY_TYPED_OPS`, not in
  `VROPS_TYPED_OPS`, because the 8.x connector reuses `VROPS_TYPED_OPS`. The
  metric keys of a resource kind (`.../statkeys`) are not covered.
- **`vrops.license.info`** — `GET /suite-api/api/product/licensing/edition`
  and `GET /suite-api/api/product/licensing/info`. The edition and the
  licence of this VCF Operations instance in one object:
  `productLicensingEdition`, `licensed`, `licenseName`, `expirationDate`
  (epoch milliseconds) and `licenseKey`. The handler masks `licenseKey`
  before it returns: only the last 5 characters show, dashes stay, and a
  key of 10 characters or fewer is hidden in full. An empty key, or one
  that is not text, gives null; the response schema allows both. The rule is
  `mask_license_key` in `connectors/_shared/license_key.py`, the same one
  `sddc.license.list` uses. The op also carries the tag
  `license-key`, so the flight recorder leaves the raw vendor bodies of
  both calls out of the trace. So no licence key reaches the agent, the
  audit row, a stored result or the trace in clear text. The vendor API
  shows no licence use on these two calls. This op is **9.x only**: it sits
  in `VROPS_9_ONLY_TYPED_OPS`, not in `VROPS_TYPED_OPS`, because the 8.x
  connector reuses `VROPS_TYPED_OPS`. The licence management calls
  (licence servers, assigning licences) are a separate piece of work.

- **`vrops.api_file.get`** (`typed_ops_api_file.py`, handler `api_file_get`)
  — reads one documentation page or API description file that the
  appliance serves, so the API file of a version with no vendor download
  (Aria Operations 8.18) can be pinned, and a 9.x file can be compared with
  what an appliance serves. `safe`, read-only, in its own group
  `vrops-api-files`. Registered through `VROPS_TYPED_OPS`, so it is on both
  vROps catalogs.

  | Versions | Catalog |
  |---|---|
  | Aria Operations 8.18 | `vrops-vrops8-8.0` |
  | VCF Operations 9.0, 9.1, 9.1.1 | `vrops-rest-9.0` |

  - **Which paths it reads.** Only: the API start page `/suite-api/`; a
    path under the documentation folder `/suite-api/doc/` (the Swagger
    page `/suite-api/doc/swagger-ui.html` and the files under it); or a
    Swagger or OpenAPI file at its usual place: a path with a part named
    `api-docs`, `swagger-resources` or `swagger-config`, or a path that
    ends with `swagger.json`, `swagger.yaml`, `openapi.json` or
    `openapi.yaml`. Allowed characters are letters, digits and `. _ ~ / -`;
    no `..` or `//`; the query may hold only `group` and `apiVersion`. So a
    backslash, a `%` (an encoded dot or slash), a scheme or a host is
    refused. The same rule is the `pattern` of the `path` parameter, so the
    dispatcher refuses a bad path as `invalid_params`, and the handler
    checks it again. A refused path sends no request. The vendor names the
    page but not always the file the page loads; the agent reads the page
    first, then the file the page asks for. If that file lies outside the
    rule, a small change to the rule adds it.
  - **How it reads.** One `GET` on the pooled client with the target's own
    login (`Authorization: OpsToken <token>` on 9.x, `vRealizeOpsToken` on
    8.x through the inherited `auth_headers`). The request goes through
    `httpx.AsyncClient.send(..., follow_redirects=False)`, so a redirect
    is not followed (the pooled client class would follow a same-origin
    redirect by itself). A `3xx` returns `redirect_path`, with the value
    of a secret-named query setting in it (`?licenseKey=...`, also with an
    encoded name such as `license%4Bey`) replaced by
    `[REDACTED:param_name]` (`scrub_query_string` of the shared check).
    A `401`, `403`, `404` or `5xx` returns its `status` with no text. A login failure
    (no credentials, a rejected login) stops the read before the request.
  - **What it returns.** `path`, `status`, `content_type`, `redirect_path`,
    `size` (bytes of the checked copy), `sha256` (of the checked copy),
    `secrets_replaced`, `parts`, `text_parts`, `version`, `build`,
    `version_error`. Only UTF-8 text comes back; other content is refused
    and the error names its content type.
  - **Version and build** come from `GET /suite-api/api/versions/current`
    (`releaseName`, `buildNumber`) on the same login, the call the
    connector's version detection makes. If that read fails, the file still
    comes back, `version` and `build` are `null`, and `version_error` says
    why.
  - **How to rebuild the checked copy.** `text_parts` is a list of `{index, text}`,
    each at most 64 KiB, cut only between characters. The size is measured
    as JSON text (a quote, a backslash or a control character counts as
    its escaped form), so a part always fits one `result_query` answer,
    and a part is never more than 64 KiB of UTF-8 either. Join the `text`
    of all parts in `index` order and encode as UTF-8; the SHA-256 of the
    result equals `sha256`. A part is a fixed size, not a line, because a
    served file can be one long line. `text_parts` is the only
    list in the answer, so JSONFlux stores a large answer as rows (one row
    per part) behind a result handle and keeps the scalar fields
    (`sha256`, `size`, `parts`, `build`, ...) inline. Page the rows with
    `result_query` (`offset` and a small `limit` such as 2; one answer is
    at most 256 KiB). A file that does not fit in one stored result
    (`result_handle_max_spill_rows`, 10,000 rows, or
    `result_handle_max_record_bytes`, 16 MiB, measured as stored JSON)
    stops with an error that names its size; the read never returns a cut
    file.
  - **No secret comes back.** The read runs the shared check,
    `check_api_file` in
    `backend/src/meho_backplane/connectors/_shared/api_file_secrets.py`
    ([`connectors-shared-api-file-secrets.md`](connectors-shared-api-file-secrets.md)),
    on the whole file before it cuts the file into parts, so a part cut
    cannot hide a secret. The check parses the file with a real parser for
    its format (JSON, YAML, XML or HTML; the content type decides, or the
    file ending) and replaces every value under a secret name with
    `[REDACTED:param_name]`, at any depth and however it is written. It
    then writes the file back from the parsed copy; `size`, `sha256` and
    the parts describe that copy, and `secrets_replaced` counts the
    replaced values (redirect query included). The copy loses comments,
    and JSON comes back compact. An inline script that is not JSON comes
    back as the placeholder, so the Swagger page keeps its markup but not
    its script. A file that does not parse, or a JavaScript, CSS or plain
    text file, is refused with the reason, never with the text.
  - **Redaction keeps the copy whole.** MEHO redacts secret-shaped text in
    every stored result. A rule that fires on the copy would rewrite a
    part, and the joined parts would no longer match `sha256`. So the read
    runs the same middleware, with the same connector, tenant and operation
    labels, on the text parts and on the whole file text before it returns,
    and stops with an error that names the rule and the place when the text
    would change. The whole-file pass catches a secret that the part cut
    splits in two. Redaction itself stays on.
  - **Tests.** `tests/test_connectors_vcf_operations_typed_api_file.py`
    (respx: accepted and refused paths with no request, the login header on
    both connectors, status and redirect cases, a redirect with a
    secret-named query value (plain and encoded name), large files, version
    failures, a login failure, a copy that a redaction rule would change, a
    secret value in JSON, YAML, XML or HTML that comes back replaced (also
    the examples the code reviews found), a file with no secret name that
    comes back with nothing replaced, a file that cannot be parsed, a
    secret split across the part cut in both forms, parts bounded by their
    JSON size, the result-handle round trip for a one-line file and for a
    file of control characters, and the operation-contract rules),
    `tests/test_connectors_shared_api_file_secrets.py` (the shared check)
    and the reconcile lane below.

All seven are `safety_level="safe"`, `requires_approval=False`,
read-only. Each rides the connector's `OpsToken` session; `authSource`
federation lives in the acquire body, not on the reads. `vrops.resource.query`
encodes its `page`/`pageSize` pagination onto the request path (the base
`_post_json` takes no `params` mapping); `vrops.resource.stats` threads its
`resourceId` / `statKey` / `currentOnly` through the base `_get_json`
`params` mapping (httpx serialises a list value to repeated pairs). Large
stat / alert / resource / property sets reduce to a JSONFlux handle at the dispatch
layer.

**Operation contracts.** The six ops meet the VMware operation-contract check
(`backend/tests/test_operation_contracts.py`). Their response schemas
live in `_response_schemas.py` and use the field names of the pinned
`vcf-operations-9.0` Suite API file (`version`, `alerts`, `resources`,
`stats-of-resources`, `resource-kind-attributes`, `product-licensing`,
`LicenseInfo`; note the stats rows sit under `stat-list.stat[]`). Each
parameter schema has one example call under `examples`, and each
`llm_instructions` blob has `when_to_call`, `output_shape` and `next_step`. The
`resourceState`, `resourceStatus` and `resourceHealth` filters of
`vrops.resource.query` take only the values of the vendor enums. Its `name`
filter takes one value, and a call that sends `name` together with `regex` is
rejected before the call (the vendor would use `name` and drop `regex`). The 9.0 file
lists only `id`, `resourceId`, `page` and `pageSize` as filters on
`GET /api/alerts`; `vrops.alert.list` still sends `activeOnly`,
`alertCriticality` and `alertStatus` (no handler change here), and its guidance
tells the AI to check each row.

The remaining 6 ingested-browse ops (resource list/get, alert definitions,
symptoms, recommendations, super metrics) stay browsable until Initiative
#2266 T7 retires the apparatus.

**Spec-reconcile lane (#2984).** Every hand-coded `METHOD:/path` above — the
seven typed-read paths (the licence read uses two of them) plus the session mint
(`POST /suite-api/api/auth/token/acquire`) — is asserted against the pinned
`vcf-operations-9.0` shelf spec by
[`backend/tests/test_connectors_vcf_operations_spec_reconcile.py`](../../backend/tests/test_connectors_vcf_operations_spec_reconcile.py)
(the #2980 harness; parse-only, runs in the required unit sweep, uniform skip
when the shelf is unconfigured). The lane parses
`vcf-operations-openapi.ingestable.json` — a deterministic derivative of the
vendor core spec pinned on the shelf, because the vendor document itself
fails `parse_openapi`'s OpenAPI 3.0 metaschema gate (three
`components.schemas` entries carry `"required": []`; the derivative drops
them, a semantic no-op — recipe + sha256 in the shelf's `MANIFEST.md`). An
operator ingesting the core spec needs the same remediation. The lane also
lists the one path `vrops.api_file.get` adds, `GET /suite-api/doc/`, as a
documentation path, not in the API file: an API file never lists the path
that serves it, so the served-path check skips it with that reason and pins
instead that the spec serves nothing under that folder. Standard:
[`docs/decisions/spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md).

### Licence management (VCF 9 licences)

VCF 9 licences are managed in VCF Operations 9.x. Six typed operations read
and change them. They live in `licensing_ops.py` (the op metadata) and
`licensing_handlers.py` (the handlers, on the `VropsLicensingHandlers` mixin
that `VcfOperationsConnector` inherits). They share one group,
`vrops-licensing`.

| op_id | Call | What it does |
|---|---|---|
| `vrops.licensing.registration.status` | `GET …/vcf-license-cloud-integration/registration/status` | Is VCF Operations registered (connected or disconnected mode), its ids, the next usage-report date |
| `vrops.licensing.server.list` | `GET …/vcf-license-cloud-integration/license-servers` | The licence servers, with registration and connection state |
| `vrops.licensing.entitlement.query` | `POST …/vcf-entitlement/entitlements/query` | The licences that can be assigned |
| `vrops.licensing.vcenter.query` | `POST …/vcf-entitlement/vcenter-systems/query` | The licence state of each vCenter (evaluation or licensed, end date); `page` / `pageSize` |
| `vrops.licensing.server.query` | `POST …/vcf-entitlement/license-servers/query` | The licence servers as the licence store sees them |
| `vrops.licensing.assign` | `POST …/vcf-entitlement/assign` | Assign licences to one vCenter (a change) |

All paths start with `/suite-api/internal/extension/`.

- **Not published by the vendor.** These are the calls the VCF Operations UI
  uses. They are not in the pinned Suite API file, so the spec-reconcile lane
  does not cover them. VCF Operations answers `403` to them unless the request
  carries `X-vRealizeOps-API-use-unsupported: true`; every handler sends it.
- **Request shapes.** The query calls send an empty JSON object. The assign
  call sends a JSON array with one entry:
  `{vcenter: {id, adapterName, host}, allocationIds: [...]}`. The handler sends
  the vCenter address as both `adapterName` and `host`, like the UI. Source:
  the community PowerShell module `lamw/Broadcom.Community.VCFLicensing` and
  live reads against VCF Operations 9.1.
- **Answers.** `entitlement.query` and `vcenter.query` return the vendor answer
  as it is; their response schemas name the known fields. The other four calls
  have no known answer shape, so the handler returns
  `{"payload": <vendor answer>}`. The `vcenter.query` answer has two shapes.
  Newer builds put `id`, `host` and `vcfAdapterName` at the top of each
  `vcenterSystems` entry. Older builds put them under `vcenterInfo`
  (`vcenterInfo.id`, `vcenterInfo.host`). The schema and the agent hints name
  both, so the agent can find the values for `assign`.
- **Paging.** `vcenter.query` sends `pageSize=1000` when the caller gives
  none, so one call returns all vCenters of a normal install. No total count
  is known for this answer. To read more, ask for the next `page` until a page has fewer
  than `pageSize` entries.
- **Safety.** The five reads are `safe` and read-only. `vrops.licensing.assign`
  is `dangerous` with `requires_approval=True`: it waits until a person
  approves it.
- **9.x only.** VCF 9 licensing does not exist on Aria Operations 8.x. The ops
  are kept out of `VROPS_TYPED_OPS` (which the 8.x connector reuses); only the
  9.x registrar adds them.
- **Checking a licence change.** The UI can say a licence was imported while
  the licence store is empty. Check with `entitlement.query` (`totalCount`) and
  `vcenter.query` (each vCenter `state: LICENSED`, `unlicensedUsage: 0`).

### Shutdown

`aclose()` clears the in-memory session-token cache and the shared
`CredentialsCache` under their locks (so a post-`aclose` reuse of the same
connector instance starts clean) and delegates to `HttpConnector.aclose()`
which closes every per-target httpx client. No server-side token revoke is
issued — the vROps token idle-expires on the appliance (same posture
NSX / vRLI take).

## Dependencies

- **httpx 0.28.x** — per-target `AsyncClient` pool inherited from
  `HttpConnector`; the per-target client carries the base URL
  (`https://{host}` or `https://{host}:{port}` when port ≠ 443),
  `Timeout(connect=5, read=30, write=30, pool=5)`, and
  `follow_redirects=True`. The `token/acquire` POST goes through the shared
  `vcf_session_login` helper on this client (bypassing the tenacity
  decorator by design — one attempt, surface the failure cleanly).
- **tenacity 9.x** — base `HttpConnector._request_json` carries the retry
  decorator (3 retries / exponential backoff / connection errors + 5xx
  only); the connector inherits it unchanged (no `_request_json` override).
- **pydantic 2.13.x** — `FingerprintResult` / `ProbeResult` /
  `OperationResult` are frozen models; the connector constructs them by
  keyword.
- **structlog 25.x** — credential-load events log through the shared
  `CredentialsCache`; the connector itself doesn't add structlog calls
  beyond what the base + cache emit.
- **respx 0.23.x (test-only)** — the unit-test module mocks every request
  shape (`token/acquire` happy path + 401/missing-token/empty-token,
  `authSource` in the acquire body set + unset, OpsToken scheme regression
  on both the typed and ingested paths, the #2067 401 → re-acquire → retry
  recovery, fingerprint/probe reachable + unreachable) without a network
  call.

## Known issues

- **Credential-cache eviction on rotation** — a rotated service-account
  password is caught at `token/acquire` (401 → `connector_auth_failed`),
  but the cached *credential* is not auto-evicted; the family-wide
  credential-cache eviction hook is a sibling task (#2396). Until then a
  process restart (which clears both caches on `aclose`) is the workaround.
- **Ingested-curation retirement pending** — the 6 remaining
  ingested-browse ops still depend on per-deploy catalog state (an ingest of
  the vROps `/suite-api` spec + operator review); the typed reads above do
  not. Retiring the whole ingested-curation apparatus is Initiative #2266 T7.

## References

- **Task (skeleton)**: <https://github.com/evoila/meho/issues/829>
- **Task (OpsToken auth rebuild)**: <https://github.com/evoila/meho/issues/2395>
- **Task (stat/metric read for sizing)**: <https://github.com/evoila/meho/issues/2838>
- **Task (VCF 9 licence management)**: <https://github.com/evoila/meho/issues/4089>
- **Task (API description read)**: <https://github.com/evoila/meho/issues/4605>
- **Parent initiative**: <https://github.com/evoila/meho/issues/369>
- **Parent goal**: <https://github.com/evoila/meho/issues/214>
- **Sibling skeletons (same Initiative wave)**:
  <https://github.com/evoila/meho/issues/830> (vRLI),
  <https://github.com/evoila/meho/issues/831> (Fleet),
  <https://github.com/evoila/meho/issues/832> (VCF Automation, merged).
- **Shared auth scaffolding**: `backend/src/meho_backplane/connectors/_shared/vcf_auth.py`
  ([#841](https://github.com/evoila/meho/issues/841)).
- **Co-area connector docs**: [`connectors-harbor.md`](connectors-harbor.md)
  (closest HTTP-Basic precedent), [`connectors-sddc-manager.md`](connectors-sddc-manager.md),
  [`connectors-vcf-automation.md`](connectors-vcf-automation.md),
  [`connectors-vcf-auth-shared.md`](connectors-vcf-auth-shared.md).
- **vROps Suite API**:
  <https://developer.broadcom.com/xapis/vcf-operations-api/latest/>
- **Acquire an authentication token (VCF Operations 9.0)**:
  <https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/administration-sdks-cli-and-tools/understanding-the-vr-ops-api/getting-started-with-the-api/acquire-an-authentication-token.html>
- **Release-readiness rubric**:
  [`connector-release-readiness.md`](connector-release-readiness.md).
