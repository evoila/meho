# Connector: vcf-fleet (VCF Fleet 9.0, vRSLCM-derived)

## Overview

The `vcf-fleet` connector is the hand-rolled `HttpConnector` subclass that
dispatches VCF Fleet REST operations under the
`(product="vcf-fleet", version="9.0", impl_id="fleet-rest")` registry triple.
G3.6-T7 (#831) shipped the skeleton — HTTP Basic auth against the LCM-local
user store, the wrapper-verified fingerprint/probe call, and the G0.6
dispatch shim. G3.6-T8 (#835) added an ingested read-enable surface. T4
(#2304, Initiative #2266) then converted the audited read set (about/health
probe + component inventory) to **typed** ops in `typed_ops.py`, removing
the Fleet-LCM-spec ingest dependency for the operational surface. T7 (#2358)
retired the hand-curated ingested-enable apparatus, leaving the remaining
reads as generic ingested breadth. G3.6-T9 (#839) shipped the CLI verb tree +
recorded-fixture E2E.

Source: `backend/src/meho_backplane/connectors/vcf_fleet/`.

The connector ships ten typed operations: eight reads and two changes. **Versions** says where the call
exists: the product versions of the connector implementation that
registers it (`fleet-rest`, `>=8.0,<9.1`). An 8.x or 9.0 target resolves
here; a 9.1 or newer target resolves to `fleet-lcm` (see "Dual
implementation").

| Operation | What it does | Vendor call | Risk | Versions |
|---|---|---|---|---|
| `fleet.about` | Read the appliance's API version, product version and build. | `GET /lcm/lcops/api/v2/about` | read (`safe`, no approval) | 8.x, 9.0 |
| `fleet.environment.list` | List the Fleet environments with the products deployed in each. | `GET /lcm/lcops/api/v2/environments` | read (`safe`, no approval) | 8.x, 9.0 |
| `fleet.api_file.get` | Read one documentation page or API description file the appliance serves (the Lifecycle APIs or the Content Management APIs). Returns path, HTTP status, content type, size, SHA-256 and the version and build; the text is in parts behind a result handle. | `GET {path}`; version and build from `GET /lcm/lcops/api/v2/about` | read (`safe`, no approval) | 8.x, 9.0 |
| `fleet.health` | Read the appliance health: name, version, build, disk use, and the state of each service (`all_up` plus one row per service). A part that fails is named in its error field. | `GET /lcm/lcops/api/v2/settings/system-details`, `GET /lcm/health/api/v2/status` | read (`safe`, no approval) | 8.18, 9.0 |
| `fleet.environment.get` | Read one environment with its products, versions and nodes. Secret values are replaced. | `GET /lcm/lcops/api/v2/environments/{environmentId}` | read (`safe`, no approval) | 8.18, 9.0 |
| `fleet.datacenter.list` | List the datacenters and their vCenters, one row per vCenter. No password is returned. | `GET /lcm/lcops/api/v2/datacenters`, `GET /lcm/lcops/api/v2/datacenters/{dataCenterVmid}/vcenters` | read (`safe`, no approval) | 8.18, 9.0 |
| `fleet.product.inventory_sync` | Start an inventory sync of one product in an environment, unless one already runs. Returns `accepted` (or `already_running`) with the request id. | `GET /lcm/request/api/v2/requests`, then `POST /lcm/lcops/api/v2/environments/{environmentId}/products/{productId}/inventory-sync` | change (`caution`, waits for approval) | 8.18, 9.0 |
| `fleet.request.list` | List lifecycle requests with state, plain outcome and error text, newest first; filter by state or outcome. | `GET /lcm/request/api/v2/requests` | read (`safe`, no approval) | 8.18, 9.0 |
| `fleet.request.get` | Read one request: state, outcome and, when it failed, its error causes (the first is the failed step). Says `not_reachable` when the appliance does not answer. | `GET /lcm/request/api/v2/requests/{requestId}`, `GET /lcm/request/api/v2/requests/{requestId}/error-causes` | read (`safe`, no approval) | 8.18, 9.0 |
| `fleet.request.retry` | Retry a failed request, optionally with corrected inputs. Returns `accepted`, `not_started` or `failed`, with the steps. | `GET /lcm/request/api/v2/requests/{requestId}`, `GET .../error-causes`, then `PATCH /lcm/request/api/v2/requests/{requestId}/retry` | change (`dangerous`, waits for approval) | 8.18, 9.0 |

VCF Fleet is the rebrand of vRealize Suite Lifecycle Manager (vRSLCM) under
the VCF 9 umbrella; the appliance still identifies as vRSLCM in its internal
config and every response carries an `Lcm-API-Version` header.

## Dual implementation (fleet-rest + fleet-lcm)

`product=fleet` is the codebase's **first real two-implementation case**
(initiative [#3033](https://github.com/evoila/meho/issues/3033), decision of
record `versioned-connector-dual-impl`). Two hand-rolled `HttpConnector`
subclasses coexist, resolved **per target by fingerprint** — the agent never
sees the difference:

| impl | connector | surface | `supported_version_range` |
|---|---|---|---|
| `fleet-rest` (legacy, typed) | `VcfFleetConnector` (this doc) | vRSLCM `/lcm/lcops/api/v2/*` | `>=8.0,<9.1` |
| `fleet-lcm` (modern, generic/ingested) | [`FleetLcmConnector`](connectors-fleet-lcm.md) | VCF 9 Fleet LCM Service `/v1/*` | `>=9.1,<10.0` |

Both register under `product="fleet"` with distinct `impl_id`s, so the v2
registry keys `("fleet","9.0","fleet-rest")` and `("fleet","9.0","fleet-lcm")`
never collide. The `("fleet","","")` **wildcard** fallback is registered once,
by this legacy package only (a second class on that key would raise at import);
`fleet-lcm` registers the versioned triple alone.

The [resolver](../../backend/src/meho_backplane/connectors/resolver.py) picks
the impl per target:

- **8.x or 9.0 target → `fleet-rest`** (`>=8.0,<9.1`). A VCF 9.0 fleet
  appliance serves only the legacy `/lcm/*` API.
- **9.1 or newer target → `fleet-lcm`** (`>=9.1,<10.0`). The `/fleet-lcm/v1`
  API first ships with VCF 9.1: its spec file is in the `9.1.0.0` tag of
  `vmware/vcf-api-specs`.

The two ranges meet at 9.1 and do not overlap (#4442), so every version has
exactly one candidate. Before #4442, `fleet-lcm` claimed `>=9.0,<10.0` and
won a 9.0 target because the narrower range wins. Its reads then called
paths a 9.0 appliance does not have. The split is by version range, not
`priority` — both impls keep `priority=1`. Operator preference
(`preferred_impl_id`) only breaks a tie between in-range candidates, so it
cannot pick an out-of-range impl.

The split is proven by
[`backend/tests/test_connectors_fleet_dual_impl_resolution.py`](../../backend/tests/test_connectors_fleet_dual_impl_resolution.py)
(8.5, 8.18, 9.0 and 9.0.2 → `fleet-rest`; 9.1, 9.1.0 and 9.1.1 → `fleet-lcm`).

## Key types

- **`VcfFleetConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="fleet"`, `version="9.0"`,
  `impl_id="fleet-rest"`, `supported_version_range=">=8.0,<9.1"`
  (8.x and 9.0 — see "Dual implementation" above), `priority=1`. The priority outranks a future `GenericRestConnector`
  auto-shim defensively if both somehow register for the same triple.
- **`VcfFleetTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape the connector reads: `name`, `host`,
  `port`, `secret_ref`, `auth_model`. No `sso_realm` field — Fleet does
  NOT federate with SSO, so the Basic auth header carries
  `username:password` directly. Replaced by the concrete `Target` model in
  `meho_backplane.targets` (#224) structurally; no code edits here when
  the model lands.
- **`VcfFleetCredentialsLoader`** (`session.py`) — async callable type
  resolving a target to `{"username": ..., "password": ...}`. Injectable
  on connector construction (`VcfFleetConnector(credentials_loader=...)`)
  so unit tests, integration tests, and pre-G0.3 production deploys
  override the default Vault loader.
- **`FleetApplianceVersion`**, **`parse_appliance_version`** (`version.py`)
  and **`VcfFleetConnector.appliance_version`** (`connector.py`) — the
  version and build from `GET /lcm/lcops/api/v2/about` as one value:
  `version`, `build` and `error`. The method never raises; on VCF 9.0
  builds the call answers HTTP 500 and `error` names it.
- **`fleet_api_file_get_impl`**, **`check_path`**, **`DOCS_PAGES`**,
  **`DOCS_FOLDERS`** (`api_file.py`) — the body of `fleet.api_file.get`
  and the path rule. The fetch, the file check, the part splitter and
  the flight recorder call are the shared steps in
  `connectors/_shared/api_file_read.py`.
- **`load_credentials_from_vault`** (`session.py`) — default loader,
  stubbed `NotImplementedError` until the live operator-context per-target
  Vault read lands. Mirrors `load_credentials_from_vault` in
  `connectors/harbor/` and `connectors/vcf_automation/` — all stubs flip
  to live implementations together under Goal #214.

The connector reuses three helpers from
`meho_backplane.connectors._shared.vcf_auth` (the #841 cross-cutting
module landed in G3.6-T13):

- `basic_auth_header(username, password)` — produces the
  `Authorization: Basic <b64>` value.
- `is_acceptable_auth_model(value)` — gates the SHARED_SERVICE_ACCOUNT
  enum / string / `None` triple.
- `CredentialsCache(loader, product_label=...)` — load-once-per-target
  `{"username": str, "password": str}` cache with the missing-key →
  `RuntimeError` contract and a serialised `clear()` for `aclose`.

Sister G3.6 connectors `vcf_operations` (#829) and `vcf_logs` (#830) will
import the same helpers; `vcf_automation` (#832 / merged) deliberately
does not — its dual-plane auth shape is bespoke.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()` in
   `meho_backplane/connectors/registry.py`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.vcf_fleet` triggers the
   module-level
   `register_connector_v2(product="vcf-fleet", version="9.0", impl_id="fleet-rest", cls=VcfFleetConnector)`
   call.
3. The registry's v2 table now resolves `("vcf-fleet", "9.0",
   "fleet-rest")` to `VcfFleetConnector`. The G0.7 auto-shim's
   idempotency check no-ops on subsequent ingests against the same
   triple.

### Auth

1. The chassis dispatcher calls `auth_headers(target, operator)`
   before issuing the request.
2. `auth_headers` rejects any `target.auth_model` other than
   `shared_service_account` / `None` via the shared
   `is_acceptable_auth_model` predicate, raising `NotImplementedError`
   naming the target and requested mode.
3. The shared `CredentialsCache.get(target)` invokes the injected loader
   on first call per target. The loader returns
   `{"username": "admin@local", "password": "..."}`; missing keys
   raise `RuntimeError` naming both the target and the missing key.
4. The cached dict is fed to `basic_auth_header(username, password)` and
   returned as `{"Authorization": "Basic <b64>"}`.

The Fleet account is typically `admin@local` — the `@local` suffix is
part of the literal username, not a realm decoration. Fleet does **not**
federate with vCenter SSO out of the box (consumer wrapper header,
verified 2026-05-21: a `vsphere.local` service account was rejected with
HTTP 401 "Bad credentials" during the discovery journey).

### Fingerprint / probe (wrapper-verified)

Fleet's first-party diagnostic endpoints all return HTTP 500 in VCF 9.0
builds — known appliance issue:

- `/lcm/lcops/api/v2/about`
- `/lcm/lcops/api/v2/health`
- `/lcm/lcops/api/v2/version`
- `/lcm/lcops/api/v2/system-details`
- `/lcm/common/api/about`
- `/lcm/locker/api/v2/about`

The consumer wrapper `scripts/vcf-fleet.sh` documents this explicitly and
works around it by calling `GET /lcm/lcops/api/v2/datacenters` with HTTP
Basic auth and reading the `Lcm-API-Version` response header for the LCM
API version. The connector follows the wrapper's contract verbatim:

- `probe_method` = `"GET /lcm/lcops/api/v2/datacenters with HTTP Basic; read Lcm-API-Version response header"`.
- `version` ← the `Lcm-API-Version` header value (e.g. `"8.0"`) when
  present, `None` otherwise.
- `build` ← `None` (no working endpoint exposes a build string in 9.0).
- `extras` ← `{lcm_api_version, datacenter_count, product_lineage,
  diagnostic_endpoints_broken}`.

The product version itself is **not** surfaced by any working endpoint in
9.0. Operators cross-source it from SDDC Manager's `/v1/vcf-services`
(LCM service entry) — that's an operator-context concern above the
per-product connector and out of scope for this skeleton.

`probe()` delegates to `fingerprint()`: Fleet has no working dedicated
health endpoint, and the datacenters call already proves both transport
and HTTP Basic auth, so reusing one round-trip is the right shape (same
delegation pattern as `vcf_automation`, `sddc_manager`, `nsx`).

### Dispatch

The audited read set dispatches as **typed** ops (`source_kind="typed"`)
off the connector's hand-rolled HTTP Basic session; the declined breadth
stays **generic-ingested**. Typed ops need no catalog ingest — they are
registered in code at lifespan startup and dispatch on a fresh boot with
zero catalog state. Ingested ops still take the G0.7 path: the operator
ingests the Fleet OpenAPI spec into `endpoint_descriptor`, enables the reads
generically via `ReviewService.enable_reads`, then dispatches. Both paths route through
`meho_backplane.operations.dispatch` via `POST /api/v1/operations/call`
(or MCP `call_operation`). The connector's `execute()` is the G0.6
ABC-compatibility shim that builds a synthetic `Operator` and forwards to
the dispatcher; post-G0.6 callers construct a real `Operator` and invoke
`dispatch` directly.

### Typed read set (T4 · #2304, Initiative #2266)

`typed_ops.py` + `VcfFleetConnector.register_operations()` carry the
**audited** read set — the two ops the adopter actually uses (#2294 T0
audit, row 21) — converted from ingested-row curation to typed ops so the
operational surface no longer depends on ingesting the crash-prone Fleet
LCM spec (the #2272 datetime-crash artifact):

- **`fleet.about`** (`GET /lcm/lcops/api/v2/about`, group `fleet-about`) —
  the about/health probe. Returns HTTP 500 in VCF 9.0 (see known-issue
  below); the `llm_instructions` carry the 500 warning + reachability
  fallback that moved with the op.
- **`fleet.environment.list`** (`GET /lcm/lcops/api/v2/environments`,
  group `fleet-inventory`) — the component inventory ("what's deployed").
  Wraps Fleet's bare environments array under an `environments` key
  (the vSphere typed-reads envelope shape).

Both ops meet the VMware operation-contract check
(`backend/tests/test_operation_contracts.py`): a short summary and
description, one example call (`{}`) each, and a response schema with a
description per field. The `environments[]` row fields (`environmentId`,
`environmentName`, `environmentStatus`, `products`) are the names the op
already used; no vendor API file for this surface is on the spec shelf yet,
so they are not checked against one.

`register_operations` walks `FLEET_TYPED_OPS`, resolves each op's
`handler_attr` to the bound method on the connector, and routes it through
`register_typed_operation` — the argocd / bind9 / vmware_rest mold. The
module-level `register_fleet_typed_operations` wrapper is queued via
`register_typed_op_registrar` in `__init__.py` so the rows land before the
first dispatch.

### The read: `fleet.api_file.get`

Why it exists: Aria Suite Lifecycle 8.18 publishes no vendor file of its
`/lcm` API, and the vendor's portal stops at 8.16. Only a running
appliance serves the API descriptions. This read is how the lab pins
those files; the reconcile lane below arms itself once they are pinned.

Input: one field, `path`. Before any vendor call the path is checked
against this rule:

1. allowed: the documentation pages `/api/swagger-ui.html` (Lifecycle
   APIs) and `/lcm/cms/api/swagger-ui.html` (Content Management APIs),
   as the 8.12 to 8.18 programming guides name them;
2. allowed: every path under the folders `/api/swagger-ui/` and
   `/lcm/cms/api/swagger-ui/` (from 8.18 Patch 3 on the pages live at
   `index.html` under each folder);
3. allowed: a Swagger or OpenAPI file at its usual place -- a path with a
   part named `api-docs`, `swagger-resources` or `swagger-config`, or a
   path ending in `swagger.json`, `swagger.yaml`, `openapi.json` or
   `openapi.yaml`;
4. refused: everything else, and any path with `..`, `//`, `\`, an
   encoded dot or slash (`%2e`, `%2f`), a semicolon (`;`), a scheme, a
   host name, a fragment, whitespace or a control character. A query
   string may hold only `group` and `apiVersion`.

A refused path stops with an error and sends no HTTP request. So an
agent cannot use this read to fetch `/lcm/locker/...` or any other data
path.

Then the read sends one `GET` to the target's own address with the
target's own HTTP Basic login. It does not follow a redirect: a `3xx`
answer is reported with its status, and `redirect_to` holds the path to
read next (the same rule applies to that path). A relative `Location`,
such as `swagger.json` or `?group=x`, is resolved against the path that
was read, so `redirect_to` is always a full path. A `Location` on another
host is reported as `scheme://host/path`, so the caller sees it cannot be
read through this target. Only a `401` is raised, so the dispatcher drops
the cached login; every other status (for example `404`) is reported in
`status` with the body as text.

The fetch and the file check are one shared step,
`fetch_checked_api_file` in `connectors/_shared/api_file_read.py`. The
size check of the stored record, the last redaction check and the flight
recorder call are a second shared step, `finish_api_file_read`. The
flight recorder (the stored record of each vendor call) gets the checked
copy of the file, never the body as it came, and only after every check
has passed: a file the read refuses, for any reason, is recorded with no
body. A `401` body goes through the same check before the error is
raised, or the error is raised with no body, also when the body is too
big or cannot be decoded. So neither the stored call record nor the
error result can carry a password the appliance put in a file.

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

Before the text is cut into parts, the read checks the whole file with
the shared API file secret check
(`connectors/_shared/api_file_secrets.py`, see
`connectors-shared-api-file-secrets.md`). The file is parsed (JSON, YAML,
XML or HTML; the content type decides, or the file ending when the type
is `text/plain` or missing), every value under a secret name is replaced
with `[REDACTED:param_name]`, and the file is written back from the
parsed copy. A file that does not parse, or whose format is unknown
(JavaScript, CSS, plain text), is refused with the reason. The answer
carries the count in `secrets_replaced`; `size` and `sha256` describe the
checked copy. A redirect path gets the same check on its query string.

The dispatcher runs the shared secret-redaction rules on every answer
before the caller sees it. A documentation page can still hold free text
that looks like a secret (for example an `Authorization` header line in
a text block). Such a page would come back changed while `sha256` names
the copy as checked. The read runs the same rules first, on the whole
text and then on its answer, and stops with an error that names the
rules and the number of places. The whole text is checked because a
secret whose label ends one part and whose value starts the next is not
seen in either part alone. The rules skip the placeholder, so a replaced
value does not set them off. Redaction stays on; the read just never
claims a file it cannot return.

Version and build come from `GET /lcm/lcops/api/v2/about`, the call of
`fleet.about`, in one request with no retry. On VCF 9.0 builds that call
answers HTTP 500 (see "Known issues"): then `version` and `build` are
`null`, `version_error` names the 500, and `lcm_api_version` still
carries the `Lcm-API-Version` header the appliance sent with the file.
A request that fails before a usable answer exists (a connection fault,
a body that does not decode, a redirect loop) is named the same way.
The file comes back either way.

The answer:

| Field | Meaning |
|---|---|
| `path` | The path that was read, with its query string. |
| `status` | The HTTP status. |
| `content_type` | The `Content-Type` header. |
| `size` | Size of the checked copy in bytes. |
| `sha256` | SHA-256 of the checked copy, hex. |
| `secrets_replaced` | How many values under a secret name were replaced in the copy. |
| `version`, `build` | `productVersion` and `buildNumber` from the about call. |
| `version_error` | Why the version read failed; `null` when it worked. The file still comes back. |
| `lcm_api_version` | The `Lcm-API-Version` header the appliance sent with the file; `null` when absent. |
| `redirect_to` | Path of a redirect answer; `null` otherwise. |
| `parts` | How many text parts there are. |
| `items` | The parts, `{index, text}`, at most 64 KiB each, in order. |

### Rebuilding a file from the parts

The checked copy is split on UTF-8 character boundaries into parts of at
most 64 KiB, so each part decodes on its own. Joining the `text` of every
part in `index` order gives the copy; the SHA-256 of the joined bytes
equals `sha256`.

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

Tests: `backend/tests/test_connectors_vcf_fleet_api_file.py`.

### Requests, environments, inventory sync, health and datacenters

Seven typed operations in `lifecycle.py` (handlers, on the
`FleetLifecycleOps` base class of `VcfFleetConnector`) and
`lifecycle_ops.py` (metadata). `typed_ops.py` adds them to
`FLEET_TYPED_OPS` and holds the group texts. Groups: `fleet-about`
(`fleet.about`, `fleet.health`), `fleet-inventory` (the environment reads,
`fleet.datacenter.list` and the inventory sync) and `fleet-requests` (the
three request operations). The paths come from the vendor's API reference
for vRealize Suite Lifecycle Manager and the Aria Suite Lifecycle 8.18
programming guide ("Retry an install or import request").

- **Request state.** `state` is the vendor's state; `outcome` puts it in
  plain words: `queued`, `running`, `succeeded`, `failed`, `cancelled`,
  or `unknown` for a state not in `OUTCOME_BY_STATE`. The vendor gives no
  progress number and no list of steps, so `fleet.request.get` returns the
  error causes of a failed request; the first one is the failed step.
- **Status read.** `fleet.request.get` follows every start of this
  connector. When the appliance does not answer (a connection or time-out
  error, or a 502, 503 or 504, for example during its own upgrade), it
  returns `outcome: not_reachable` with the request id and `checked_at`.
  It never says `failed` then. Both of its reads are covered: when the
  request read worked and the error-causes read did not, `state` keeps
  what the request read said and `outcome` is still `not_reachable`. MEHO
  keeps no job state between calls; the caller keeps the request id (or
  the async run handle of the start).
- **Login again.** Fleet sends the stored user and password with every
  call. On a `401` the request read and the two changes drop the cached
  login, read it again from Vault and repeat the call once. A `401` means
  the appliance refused the call before it ran.
- **Inventory sync (`fleet.product.inventory_sync`).** Before the start it
  lists the requests and looks for a queued or running request whose type
  or name says inventory sync and whose `environmentId` and `productId`
  inputs equal the given ids, each as a whole value. Other input text does
  not count, so a sync of another product that only mentions these ids is
  not a match. If one runs, it returns `already_running` with that request id
  and starts nothing. This check is a help, not a lock: two callers can
  both check before either starts. Otherwise it sends the start and returns
  `accepted` with the request id the appliance gives. It never says the
  sync is done. Use MEHO's async mode (`async: true`, see
  [`async-governed-dispatch.md`](async-governed-dispatch.md)) for the
  start, so a lost reply keeps the request id under a run handle. After a
  lost reply, read `fleet.request.list` (outcome `running`) before you
  start again.
- **Retry (`fleet.request.retry`).** Three steps: read the request, read
  its error causes, then send the causes back with `retry: true`
  (`PATCH .../retry`). Corrected inputs go in `properties`, by the names in
  the error causes; an unknown name is refused before the change. A secret
  input is a locker reference (`locker:password:<id>:<alias>`), never the
  value. A request that did not fail, or has no error causes, gives
  `not_started` and nothing is sent. When any step fails with a vendor or
  transport error, the answer is `status: failed` with the vendor's error
  and `steps` lists what ran, what failed and what did not run; nothing is
  undone. After a lost reply,
  read the request first: if it is running again, the retry went through.
- **Health (`fleet.health`).** Two reads: the system details (version,
  build, disk use) and the service state (`/lcm/health/api/v2/status`:
  `vrlcm-server`, `postgres`, `blackstone`, `bootstrap`). A part that fails
  with a vendor error other than `401` is named in `system_details_error`
  or `health_error`, and the other part still answers. The Task named
  `/lcm/bootstrap/api/status` for the service state; the vendor API
  reference documents `/lcm/health/api/v2/status` instead, and its answer
  carries the bootstrap state, so the operation reads that.
- **Secrets.** MEHO's shared secret-field scrub (`scrub_secret_fields` in
  `redaction/secret_fields.py`, see [`redaction.md`](redaction.md)) runs on
  every vendor object and every vendor text an answer carries. A field with
  a secret name (`is_secret_param_name`) is replaced by
  `[REDACTED:param_name]`, at any depth: environment product and node
  properties, error-cause inputs, and a quoted pair such as
  `"adminPassword": "..."` inside an error text, a request reason or a
  vendor error body. The datacenter list builds its rows from named fields
  only, so the vCenter password the vendor sends (`vcPassword`) never
  reaches the answer.
- **Versions.** The calls exist on every version this connector serves
  (Aria Suite Lifecycle 8.x and VCF 9.0 fleet management), so no operation
  stops early with a version message. A live proof on 8.18 and 9.0 is a
  separate lab test.

Tests: `backend/tests/test_connectors_vcf_fleet_lifecycle.py`.

### Upgrades, binaries and new environments

Seven typed ops in `changes_ops.py`. Their handlers are on the
`FleetChangeHandlers` mixin in `changes.py`; `VcfFleetConnector`
inherits it. They exist on Aria Suite Lifecycle 8.18 and VCF 9.0 fleet
management (the whole `fleet-rest-9.0` range, `>=8.0,<9.1`), so no op has
its own version check.

| Operation | What it does | Vendor call | Risk | Returns | Versions |
|---|---|---|---|---|---|
| `fleet.binary.list` | Lists the install and upgrade files the appliance has. | `GET /lcm/lcops/api/v2/settings/product-binaries` | safe (read) | `{binaries: [{filePath, name, type}]}` | 8.18, 9.0 |
| `fleet.binary.map` | Finds the files at a local or NFS path, then maps them. | `POST .../settings/product-binaries`, then `POST .../settings/product-binaries/download` | caution | `{status, request_id, status_read, binaries, steps}` | 8.18, 9.0 |
| `fleet.product.upgrade.precheck` | Starts the pre-check of a product upgrade. | `POST .../environments/{environmentId}/products/{productId}/upgrade/pre-validate` | caution | `{status, request_id, status_read, environment_id, product_id}` | 8.18, 9.0 |
| `fleet.product.upgrade` | Starts the upgrade of a product. Sends `preValidate: true` unless `skip_precheck` is set. | `POST .../environments/{environmentId}/products/{productId}/upgrade` | dangerous | same as the pre-check | 8.18, 9.0 |
| `fleet.environment.precheck` | Starts the check of a new environment spec. | `POST /lcm/lcops/api/v2/environments/pre-validate` | caution | `{status, request_id, status_read, environment_name}` | 8.18, 9.0 |
| `fleet.environment.create` | Starts creating an environment: deploys or imports its products. | `POST /lcm/lcops/api/v2/environments` | dangerous | same as the pre-check | 8.18, 9.0 |
| `fleet.environment.delete` | Starts deleting an environment; with `delete_vms` also its VMs in vCenter. | `DELETE /lcm/lcops/api/v2/environments/{environmentId}` with a `DeleteEnvironmentRequestDTO` body | destructive | `{status, request_id, status_read, environment_id, delete_vms}` | 8.18, 9.0 |

Every change has `requires_approval: true`. A person's call waits until a
second person approves it. An agent needs a grant for a `dangerous` change
and is always refused for the delete.

**Vendor jobs.** Each change answers with the vendor's `requestId` at once.
The op returns `status: "accepted"`, the `request_id`, and `status_read`
(`fleet.request.get` with that id). `accepted` means started, not done.
The caller reads `fleet.request.get` until the request ends. MEHO keeps no
job state between calls. Use MEHO's async mode (`async: true`, see
[`async-governed-dispatch.md`](async-governed-dispatch.md)) for a start, so
the request id is kept under a run handle even when the reply is lost.

**A running request stops a second start.** Before an upgrade, an upgrade
pre-check or an environment create, the op reads
`GET /lcm/request/api/v2/requests`. When a request that has not ended
(`CREATED`, `SUBMITTED`, `INPROGRESS`) names the same object, the op starts
nothing and returns that request's id and state:

- `status: "already_running"` when it is of the same kind.
- `status: "blocked"` (with a `reason`) when an upgrade pre-check runs and an
  upgrade is asked, or the other way round. A pre-check does not stand in
  for the upgrade, so the upgrade is never reported as running when only its
  pre-check runs.

The kind comes from the request type, name or reason: `upgrade` together
with a check word (`validat`, `precheck`, `pre-check`) is an upgrade
pre-check, `upgrade` alone is an upgrade, `create environment` is a create.
"Same object" means the environment and product ids (or the environment
name) are whole values in the request's `inputMap`. Part of a value does
not count: a running create of `Production-DR` does not stop a create of
`Production`. An empty name never matches. This is a help, not a lock: two
callers can both check before either starts, and the check sees only
requests that still run. When the request list cannot be read, the start
does not run.

**After a lost reply.** Do not start again blindly. With the request id,
read `fleet.request.get`. Without one, read the saved async result (run the
start with `async: true`, so the request id is kept under a run handle), or
`fleet.request.list`: a request of this kind on this object, running or
ended, is yours. For a create or delete, read `fleet.environment.list` too;
for a binary map, `fleet.binary.list`. Start again only when none of them
shows the request or its result. A second call of the start finds a running
request, but not one that ended: it would start the job again.

**Two steps in `fleet.binary.map`.** The `find` step answers at once with
the files it found; the `map` step is a vendor job. When a step fails with a
vendor error, the answer has `status: "failed"` and `steps`: the steps that
ran, the step that failed with the vendor's error text, and the steps that
did not run. A transport error (a time-out, a lost connection) keeps the
same report. In the `find` step it is a failure like any other: nothing was
started. In the `map` step the answer has `status: "unknown"`: the step is
reported as `unknown` with the error, `binaries` lists what was sent, and
`reason` says to read `fleet.request.list` and `fleet.binary.list` before a
new call, because the appliance may be mapping the files. Nothing is undone.

**No secret in clear text.** An environment spec must name every password,
certificate and licence by its locker reference (`locker:password:...`,
`locker:license:...`): the appliance resolves it from its own secret store.
A field named like a password or secret, or a licence field, with any other
value is refused, whatever its type (a text, a number, a list or an object),
and so is a private key anywhere in the spec. The check
(`spec_secret_problem`) is registered as the params check of both
environment ops (`register_params_check` in `operations/_validate.py`), so
the dispatcher and `preview_operation` refuse the spec as `invalid_params`
right after the schema check, before the policy gate. A clear secret
therefore never waits in the approval queue, and the refusal names the
field, never the value. The handlers check the spec again before the vendor
call. The upgrade `license_ref` must be a locker reference too (its schema
says so). Store a password in the locker first, from a Vault reference. The
environment ops carry the tag `credential`, so the flight recorder keeps no
request or answer body.

**The delete preview.** `environment_delete_preview` reads
`GET /lcm/lcops/api/v2/environments/{environmentId}` and fills the
`blast_radius` block: the environment (`object`), its products with version
and node count (`children`), and what cannot be undone (`irreversibility`;
with `delete_vms` the VMs are gone for good). A missing environment (HTTP
400 or 404, or an empty answer) makes the builder raise, so the delete is
refused before it waits for approval.

Tests: `backend/tests/test_connectors_vcf_fleet_changes.py`.

### Remaining ingested breadth (enabled generically)

The reads that have no typed operation (for example the product list and
the vCenter detail) stay as
ordinary `source_kind="ingested"` `endpoint_descriptor` rows that land via
G0.7 spec ingestion and stay browsable as profiled-dispatch breadth. They are
enabled through the **generic review flow** —
`ReviewService.enable_reads(connector_id, tenant_id=...)`.

The hand-curated ingested-enable apparatus (the `core_ops.py` module with its
`FLEET_CORE_OPS` / `FLEET_CORE_GROUPS` / `FLEET_PATH_RULES` constants and the
`classify_fleet_op` / `apply_fleet_core_curation` helpers) was retired in #2358
(T7 of #2266); read enablement is now generic, with no per-product curation code.

The runbook for end-to-end ingest + enable + verify is
[`docs/cross-repo/g36-fleet-canary.md`](../cross-repo/g36-fleet-canary.md).

## Dependencies

- `meho_backplane.connectors.adapters.http.HttpConnector` — base class
  carrying the pooled `httpx.AsyncClient` per target, retry-on-idempotent
  transport, and base-URL composition.
- `meho_backplane.connectors._shared.vcf_auth` — `basic_auth_header`,
  `is_acceptable_auth_model`, `CredentialsCache`. Shipped in #841 / merged
  into main 2026-05-22.
- `meho_backplane.connectors.schemas` — `AuthModel`, `FingerprintResult`,
  `ProbeResult`, `OperationResult`.
- `httpx>=0.27` (0.28.1 resolved), `pydantic>=2.13.4`, `respx>=0.21`
  (test-only). No new runtime deps introduced by this Task.

## Known issues

- **Product version unreachable in 9.0.** None of Fleet's `/about` /
  `/version` / `/health` / `/system-details` endpoints work in 9.0. The
  connector surfaces only the `Lcm-API-Version` header value (the
  underlying vRSLCM API version, typically `"8.0"`) — not the marketing
  product version. Operators needing the product version cross-source it
  from SDDC Manager's `/v1/vcf-services` LCM service entry.
- **`auth_model="per_user"` / `"impersonation"` rejected.** v0.2 locks
  the connector to `shared_service_account` / `None`. Once the per-user
  identity model lands, the gate moves to the shared module and every
  consumer (including this connector) flips together.
- **Default Vault loader is a stub.** Until Goal #214 (Connector parity)
  wires the operator-context per-target Vault read, production deploys
  must inject a custom `credentials_loader` on construction.
- **The `/lcm` API files are not pinned yet.** The reconcile lane
  (`tests/test_connectors_vcf_fleet_spec_reconcile.py`) skips its served
  check until the shelf holds `vcf-fleet-9.0/`. The manifest-pin test runs
  always; it lists the documentation pages and folders of
  `fleet.api_file.get` as `GET`, marked "documentation path, not in the API
  file", and keeps them out of the served check.
- **The upgrade pre-check path is not in the published API reference.**
  `POST .../products/{productId}/upgrade/pre-validate` follows the vendor's
  pattern for the other pre-checks (patches, certificates, scale-up), but the
  published reference (up to 8.16) does not list it. The live test on 8.18
  and 9.0 decides it. The upgrade itself is safe either way: it sends
  `preValidate: true`, so the appliance checks before it changes anything.
- **The documentation paths come from the vendor's guides.** Their first
  live use is the pinning of the Aria Suite Lifecycle 8.18 files. If a page
  loads its file from a path the rule refuses, file a bug for this read
  with the exact path; a small change adds it.

## References

- Task: <https://github.com/evoila/meho/issues/831>
- Parent initiative: <https://github.com/evoila/meho/issues/369>
- Parent goal: <https://github.com/evoila/meho/issues/214>
- Shared scaffolding (G3.6-T13): <https://github.com/evoila/meho/issues/841>
- Sibling skeletons: vROps #829, vRLI #830, VCFA #832 (merged).
- Consumer wrapper (authoritative contract): `scripts/vcf-fleet.sh`
  in the consumer's private repo.
- VCF Fleet / vRSLCM API:
  <https://developer.broadcom.com/xapis/vrealize-suite-lifecycle-manager-api/latest/>
- The API description read: <https://github.com/evoila/meho/issues/4607>
  (the same read for NSX: #4395; VCF 9.0 targets on this connector: #4442).
- Spec-reconcile guard (#2993):
  [`backend/tests/test_connectors_vcf_fleet_spec_reconcile.py`](../../backend/tests/test_connectors_vcf_fleet_spec_reconcile.py)
  asserts every hand-coded `/lcm/*` literal against a pinned vRSLCM 1.3.0 LCM
  REST OpenAPI. Ships **dormant** (skips until the spec is pinned): the spec
  exists — appliance-served (`/api/swagger-ui.html`), Broadcom-portal-documented,
  and ingested by the G3.6 canary — but is **not** the public
  `vmware/vcf-api-specs` `fleet-lcm-openapi.yaml` (that is the new
  `/fleet-lcm/v1/*` service, a different surface serving none of these paths).
  Determination + pin handoff: the lane docstring and the vcf-fleet note in
  [`docs/decisions/vendor-spec-ci-provisioning.md`](../decisions/vendor-spec-ci-provisioning.md).
