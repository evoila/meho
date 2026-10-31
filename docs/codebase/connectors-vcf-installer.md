# Connector: vcf-installer (VCF Installer 9.0 and 9.1)

## Overview

The `vcf-installer` connector is the hand-rolled `HttpConnector` subclass that
dispatches VMware Cloud Foundation **Installer** operations under the
`(product="installer", version="9.1", impl_id="installer-rest")` registry triple
(`connector_id="installer-rest-9.1"`). The connector serves Installer 9.0 and
9.1 (see [Installer 9.0](#installer-90)). The Installer is the VCF 9.x bring-up
appliance (the successor to Cloud Builder); this connector is the governed
dispatch surface the automation add-on
([evoila-bosnia/meho-internal#223](https://github.com/evoila/meho/issues/2907))
binds its bring-up step to — it generates the `SddcSpec`, meho governs the POST.

Filed under Initiative [#2907](https://github.com/evoila/meho/issues/2907)
(backplane-first coverage) as [#3065](https://github.com/evoila/meho/issues/3065),
converting the register's "VCF stack / Cloud Builder bring-up — to file" row to
the 9.x reality. Increment 1 (the **skeleton** — token auth, fingerprint / probe,
and the bring-up **status poll**) shipped in
[#3066](https://github.com/evoila/meho/issues/3066). Increment 2 added the
governed bring-up **write** `installer.composite.sddc.bringup` — a `dangerous` +
`requires_approval` composite (validate → deploy) — because the
highest-blast-radius write in the product must carry the preview + sub-op-policy
machinery a raw op cannot. [#3078](https://github.com/evoila/meho/issues/3078)
then decomposed the surface into **submit/poll typed primitives** — five since
the [#3123](https://github.com/evoila/meho/issues/3123) failed-task retry — (the
vendor bring-up API is natively asynchronous, so every governed call is short
and the standard park → approve → resume machinery works as-is): the durable
multi-hour orchestration loop belongs to the deploy-automation add-on, which
binds to these primitives, while the composite remains the one-shot governed
validate-then-deploy unit for operators.

Source: `backend/src/meho_backplane/connectors/vcf_installer/`.

## Key types

- **`InstallerConnector`** (`connector.py`) — `HttpConnector` subclass.
  `product="installer"`, `version="9.1"`, `impl_id="installer-rest"`,
  `supported_version_range=">=9.0,<10.0"`, `priority=1`. The id keeps `9.1`;
  only the range starts at 9.0.
- **`INSTALLER_EXECUTION_PROFILE`** (`profile.py`) — the reviewed declarative
  profile the connector derives its session auth from (the `session_login_token`
  scheme) and its fingerprint recipe (`GET /v1/system/appliance-info` → literal
  top-level `version`).
- **`InstallerTargetLike`** / **`InstallerCredentialsLoader`** (`session.py`) —
  the structural target Protocol and the injectable credential loader; the
  default `load_credentials_from_vault` delegates to the shared
  `_shared/vcf_auth.load_basic_credentials` live operator-context Vault read
  (rubric State 2, `shared_service_account`).

## Control flow

### Registration

Importing `meho_backplane.connectors.vcf_installer` calls
`register_connector_v2(product="installer", version="9.1",
impl_id="installer-rest", cls=InstallerConnector)` (plus a `("installer","","")`
wildcard fallback for a fresh, unfingerprinted target), and queues
`register_installer_typed_operations` onto the lifespan registrar list. The
`(product, impl_id)` round-trip constraint requires `product="installer"` (the
first hyphen-segment of `impl_id`), **not** `vcf-installer`.

### Auth — token session (`session_login_token`)

The Installer is token-only, the same scheme as SDDC Manager:
`POST /v1/tokens` with a `{username, password}` body (`TokenCreationSpec`)
returns `accessToken` (`TokenPair`), sent as `Authorization: Bearer <accessToken>`
on every subsequent request. The connector derives the login path, request
headers, and expiry status set from the profile's scheme spec, so the mechanics
are declared once and cannot drift. The per-target token is cached (single-flight
under a lock); a downstream `401` evicts it and re-logs in once (the
`_get_json_with_session_retry` helper for the fingerprint path, the public
`invalidate_session` hook for the dispatch path, #2067). Credentials load once
per target from Vault. v0.2 locks the connector to
`AuthModel.SHARED_SERVICE_ACCOUNT` (or `None` for pre-G0.3 targets); any other
`auth_model` raises `NotImplementedError`.

### Fingerprint + probe

`fingerprint(target)` issues `GET /v1/system/appliance-info` through the token
session and reads the flat `ApplianceInfo` object's top-level `version` and
`role`. On transport/status failure it returns `reachable=False` with a
structured `extras["error"]`. `probe(target)` delegates to `fingerprint` — one
authenticated request covers reachability and auth-challenge.

## Operations

### The five submit/poll primitives (typed, [#3078](https://github.com/evoila/meho/issues/3078), retry [#3123](https://github.com/evoila/meho/issues/3123))

All five are typed ops (`source_kind="typed"`), dispatchable on a fresh boot
with zero catalog ingest; read bodies live in `typed_reads.py`, submit bodies
in `typed_writes.py`, a bound-method shim on the connector exposes each. Every
call is short — the submits return their tracking object in seconds and the
polls are single GETs — so no long-blocking loop and no async dispatch
machinery exist here.

| op_id | Vendor call | Safety |
|---|---|---|
| `installer.sddc.spec.validate` | `POST /v1/sddcs/validations` → `Validation` | `caution` (writes to the installer, mutates no estate) |
| `installer.sddc.validation.status` | `GET /v1/sddcs/validations/{id}` → `Validation` | `safe` |
| `installer.sddc.bringup.start` | `POST /v1/sddcs` → `SddcTask` | `dangerous` + `requires_approval` |
| `installer.sddc.bringup.retry` | `PATCH /v1/sddcs/{id}` (`retrySddc`) → `SddcTask` | `dangerous` + `requires_approval` |
| `installer.sddc.bringup.status` | `GET /v1/sddcs/{id}` → `SddcTask` | `safe` |
| `installer.sddc.list` | `GET /v1/sddcs` (`getSddcTasks`) → `PageOfSddcTask`, cut down per task | `safe` |
| `installer.system.depot.get` | `GET /v1/system/settings/depot` → `DepotSettings` (passwords scrubbed) | `safe` |
| `installer.system.depot.set` | `PUT /v1/system/settings/depot` (sync connection check) | `caution` + `requires_approval` |
| `installer.system.depot.sync-status` | `GET /v1/system/settings/depot/depot-sync-info` → `DepotSyncInfo` (catalog ingest state) | `safe` |
| `installer.system.trusted-certificates.list` | `GET /v1/sddc-manager/trusted-certificates` | `safe` |
| `installer.system.trusted-certificates.add` | `POST /v1/sddc-manager/trusted-certificates` | `caution` + `requires_approval` |
| `installer.bundles.list` | `GET /v1/bundles` → `PageOfBundle` | `safe` |
| `installer.bundles.download-status` | `GET /v1/bundles/download-status` → `PageOfBundleDownloadStatusInfo` | `safe` |
| `installer.bundles.download` | `PATCH /v1/bundles/{id}` per id (one approved batch) | `caution` + `requires_approval` |

The bottom eight rows are the **air-gapped depot lifecycle** (#3121, group
`installer-depot`) — the steps that used to force a governed bring-up
out-of-band: configure the depot (`depot.set` maps the vendor rejection to a
structured `depot_error`, with inline remediation for the common
`VMWARE_DEPOT_CONNECT_FAILURE` TLS-trust case → `trusted-certificates.add`,
then retry), wait for the catalog ingest (`depot.sync-status`, below), then
explicitly download the release bundles
(`bundles.download`, one approval per batch; per-bundle failures collected as
`status="partial"`, not fatal) **before** validating a spec — a freshly
connected installer pins components to the newest catalog versions and
hard-fails Versions-and-Bundles when those binaries are absent. `depot.get`
scrubs every `password` key at every depth before the settings echo crosses
the governed surface; `depot.set`'s park-time preview echoes
hostname/port/mode + account username only, `trusted-certificates.add`'s a
SHA-256 fingerprint, `bundles.download`'s the id batch (truncated past 20).
`bundles.download-status` ([#3410](https://github.com/evoila/meho/issues/3410))
explains a `FAILED` row on `bundles.list`, which only carries `downloadStatus`.
Per bundle it returns the vendor failure `message`, `downloadedSize` (bytes so
far; compare with `sizeMB` from `bundles.list` for progress), `isDownloadable`
(can the bundle be downloaded right now), and `downloadId` (the download task
id). Optional `bundle_id` / `release_version` / `image_type` params map to the
vendor `bundleId` / `releaseVersion` / `imageType` query filters. A
release-wide read reduces to a result handle; its `result_digest` hint adds
per-`downloadStatus` counts and the `IN_PROGRESS` bundle ids inline. `FAILED`
rows are not copied inline, because a depot outage fails every row; read them
with `bundle_id` or `result_query`. The vendor schema has no credential
fields, so nothing is scrubbed.

**Waiting for the catalog ingest (#3406).** `depot.set` returns as soon as the
depot connection check passes (about a second). The appliance then ingests the
release catalog in the background, and `bundles.list` stays empty until that
finishes. `depot.sync-status` reads the vendor `DepotSyncInfo`
(`syncStatus` / `errorMessage` / `lastSyncCompletionTimestamp`) and returns it
as sent, with `password` keys scrubbed. The 9.1 spec does not list the
`syncStatus` values, so MEHO does not interpret them. The documented "catalog
ready" rule, carried in the op's `llm_instructions` and the group
`when_to_use`, is: `errorMessage` is empty and `lastSyncCompletionTimestamp`
is later than the `depot.set` call. A non-empty `errorMessage` means the sync
failed, so the caller fixes the depot instead of waiting. A non-empty
`bundles.list` confirms it. The order is `depot.set` → poll
`depot.sync-status` → `bundles.list` → `bundles.download`. The vendor also
has `PATCH .../depot-sync-info` to start a new sync; MEHO does not register
it yet.
The group's `when_to_use` and the `spec` parameter hint on the submit
primitives carry the 9.1 field rules proven live (three unique
`vspClusterSpec` FQDNs outside the pool, ≤20-char core passwords, the
derived `vcf-identity` FQDN, `esaConfig` for all-flash/nested).

`spec.validate` submits the caller's `SddcSpec` verbatim as a non-mutating
dry-run; `validation.status` polls the returned id to a terminal
`executionStatus` and surfaces the `resultStatus` verdict (`SUCCEEDED` /
`WARNING` / `FAILED`) plus `validationChecks[]`. `bringup.start` is the estate
mutation: the dispatcher parks it for approval before the handler runs, and its
park-time preview is the composite's `_blast_radius`-backed identity/network
echo (registered for both op_ids in `bringup.py`) — never a password.
`bringup.retry` (#3123) resumes a `COMPLETED_WITH_FAILURE` task from its failed
sub-task under the same posture: the `SddcSpec` body is optional (omitted, the
appliance retries the stored spec as-is — the common transient-failure case;
provided, it is the vendor's edit-and-retry flow), and its own park-time
preview leads with the failed task id, appending the blast-radius identity only
when a spec rides along. `bringup.status` polls the `SddcTask` (`IN_PROGRESS` /
`COMPLETED_WITH_SUCCESS` / `ROLLBACK_SUCCESS` / `COMPLETED_WITH_FAILURE`, plus
`sddcSubTasks[]` / `milestones[]`). `ROLLBACK_SUCCESS` is a finished task too:
the bring-up failed and the appliance undid what it had built, so the AI
guidance of `bringup.start`, `bringup.retry`, `bringup.status` and the
composite names it as a stop condition. `installer.sddc.list`
([#3418](https://github.com/evoila/meho/issues/3418)) lists every bring-up task
and needs no id. Use it when the id of a running bring-up is lost — for example
when the result of an approved `bringup.start` was not picked up in time — then
poll `bringup.status` with the id it returns. Each task is cut down to `id` /
`name` / `status` / `deploymentType` / `vcfInstanceName` / `creationTimestamp`.
That list of fields is a whitelist, so the ~260-row `sddcSubTasks[]`,
`milestones[]` and any field the vendor adds later never reach the result. A
normal appliance has a few tasks, so the list stays inline; a long list goes
through the normal JSONFlux reduction and returns a handle whose sample rows
still carry the ids. Session-expiry recovery for all of these rides the dispatcher's
#2067 arm (raw `401` → `invalidate_session` → re-dispatch once — safe for the
POSTs/PATCH too, since a `401` is rejected at auth before the server processes
the request).

**Poll scalars survive JSONFlux reduction
([#3084](https://github.com/evoila/meho/issues/3084)).** All five
primitives register the `result_scalars` reduction hint (see
`docs/architecture/jsonflux.md` § *Preserved scalar siblings*): when an
over-threshold `validationChecks[]` / `sddcSubTasks[]` rides the JSONFlux
reduction, the vendor object's top-level scalars stay on the reduced
result — `Validation`'s `id` / `description` / `executionStatus` /
`resultStatus`, `SddcTask`'s `id` / `name` / `status` / `deploymentType` /
`vcfInstanceName` / `creationTimestamp` — so the submit → poll loop is
drivable from the governed response alone (the pre-#3084 reduction
replaced the whole object with reduction bookkeeping and swallowed the
poll `id`, observed live against a VCF Installer 9.0.2 appliance). The
check/sub-task rows keep the full handle + drill-in contract; an
under-threshold response passes through verbatim as before. An `SddcTask`
carrying **both** `sddcSubTasks[]` and `milestones[]` is a dict-of-arrays
detail object (#2113) and passes through whole. Regression-pinned by
`backend/tests/test_connectors_vcf_installer_jsonflux_scalars.py`.

**When to use which surface.** The primitives are the deploy-automation
add-on's durable-workflow surface: validate first (no approval), surface the
verdict to a human, then request approval for a bare submit whose
approve-resume completes in seconds. The composite is the one-shot operator
surface: it couples validation to the approval-gated submit, so its
approve-resume re-runs the validation poll (bounded at 20 minutes) before the
deploy POST — correct for a single governed "just bring it up" call, wrong
for a workflow that wants short resumes. (Pattern precedent: vmware-rest
ships composites alongside raw ops.)

### Installer settings and the resource calculation (typed)

Six more typed operations cover the setup work done once per site. Their
metadata is in `typed_ops_system.py`; the bodies are in `system_settings.py`
and `resources_calc.py`. `typed_ops.INSTALLER_TYPED_OPS` includes them, so the
same registrar upserts them. The shared metadata type and the group keys live in
`_op_meta.py`.

| op_id | What it does | Vendor call | Risk | Versions |
|---|---|---|---|---|
| `installer.system.proxy.get` | The proxy: on or off, host, port, HTTP or HTTPS, user. Never the password. With `task_id`, also the state of a proxy job. | `GET /v1/system/proxy-configuration`, `GET /v1/tasks/{id}` | `safe` | 9.0, 9.1 |
| `installer.system.proxy.set` | Set the proxy, or turn it off. The password comes from a Vault path. | `PATCH /v1/system/proxy-configuration` (vendor job) | `dangerous` + approval | 9.0, 9.1 |
| `installer.system.settings.get` | CEIP state and the most domains allowed in subscription mode. With `task_id`, also the state of a CEIP job. | `GET /v1/system/ceip`, `GET /v1/system`, `GET /v1/tasks/{id}` | `safe` | 9.0, 9.1 |
| `installer.system.settings.set` | Turn CEIP on or off; change the domain limit (1 or more). | `PATCH /v1/system/ceip` (vendor job), `PATCH /v1/system` | `caution` + approval | 9.0, 9.1 |
| `installer.system.trusted-certificates.remove` | Remove a certificate from the trust store by alias. Warns when it belongs to the depot that is set. | `DELETE /v1/sddc-manager/trusted-certificates/{alias}` | `dangerous` + approval | 9.0, 9.1 |
| `installer.sddc.resources.calculate` | Calculate the CPU, memory and storage a spec needs and compare it with the hosts, check by check. Changes nothing. | `POST /v1/sddcs/resources-calculation`, then `GET /v1/sddcs/resources-calculation/{id}` | `safe` | 9.1+ |

Groups: the proxy and settings operations are in the new `installer-system`
group, the certificate removal is in `installer-depot` (next to `.list` and
`.add`), and the resource calculation is in `installer-bringup`.

**Version check.** `_version_gate.require_installer_version` runs first in
`installer.sddc.resources.calculate`. On a 9.0 target it raises
`NotImplementedError` before any vendor call. The message names the operation,
the target's version and "9.1 or newer". The dispatcher turns it into an
audited `connector_unsupported` error. A target with no known version goes on.

**CEIP body by version.** On 9.0 the CEIP `PATCH` body is a plain JSON string
(`"ENABLED"` / `"DISABLED"`). From 9.1 on it is `{"status": "ENABLE" |
"DISABLE"}`. `system_settings._ceip_body` picks the shape from the target's
version; an unknown version gets the 9.1 shape.

**Vendor jobs (proxy and CEIP changes).** The appliance answers `202` with a
vendor `Task` and does the work after that.

- The change answers `accepted` with the vendor `task_id` and a `status_read`
  block: the read to call (`installer.system.proxy.get` or
  `installer.system.settings.get`) and its `task_id`. It never says done.
- The status read with `task_id` reads `GET /v1/tasks/{id}` and returns the
  vendor `state`, a plain `outcome` (`queued`, `running`, `succeeded`,
  `failed`, `unknown`, `not_reachable`), the progress (sub-tasks done of all)
  when the vendor gives sub-tasks, and the vendor error texts.
- Before a start, the change lists `GET /v1/tasks` and looks for a job that
  has not ended whose type or name holds the word `PROXY` (not `CLOUD`) or
  `CEIP`. The CEIP change also reads the CEIP state: `ENABLING` or `DISABLING`
  means a change runs. If one runs, the answer is `already_running` with that
  job's id, and nothing starts. The vendor does not document its task types, so
  the match is on words. It is a help, not a lock: two callers can both check
  before either starts, and a job that just ended is no longer in the list.
- After a lost reply (`status: unknown`), do not start again blindly. Read
  the status read without `task_id` first: it shows the settings and, for
  CEIP, a changing state.
- Use MEHO's async mode (`async: true`, see
  [`async-governed-dispatch.md`](async-governed-dispatch.md)) for the change.
  MEHO then keeps the answer, with the job id, under a run handle, so a lost
  reply does not lose the job id.
- Every call uses the connector's session-retry helpers. When the login
  expired, the read or write logs in again once and goes on. A write is safe
  to send again after a `401`, because the appliance refused it before doing
  anything.
- When the appliance does not answer (for example during its own upgrade), the
  status read returns `outcome: not_reachable` with the job id and
  `checked_at`, never `failed`. MEHO keeps no job state between calls; the
  caller keeps the job id (or the async run handle of the change).
- `installer.system.settings.set` runs up to two steps: CEIP, then the system
  setting. Each step reports `accepted`, `done`, `failed`, `unknown` or
  `not_run`. A failed step stops the rest, and the steps that ran are not
  undone. The overall `status` is `partial` when a step failed after another
  ran.
- A vendor refusal of a change (for example `400` for a proxy that cannot be
  reached, or `409` while another CEIP change runs) comes back in the answer
  as `rejected` (or as a failed step) with the vendor error code and text, the
  same way `depot.set` answers `depot_error`.

**Secrets.** `installer.system.proxy.set` takes `password_secret_ref` (plus
optional `password_secret_mount` and `password_secret_key`) and reads the
password from Vault under the caller's identity (`_secret_refs.py`). Answers
never hold it: the proxy read and echo drop every `password` key, and a vendor
text that holds the password is replaced. The operation carries the
`credential` tag and is pinned in `broadcast/events.py` as a credential
write, so a trace keeps no body and the broadcast holds only a summary.

**The resource calculation is work MEHO does inside its own call.** The
handler starts the calculation, reads it every 5 seconds until
`executionStatus` is no longer `IN_PROGRESS`, and only then answers (`done`).
Other answers: `timed_out` after `timeout_seconds` (default 600) with the id
and the last state, `read_failed` when a read failed part-way (with the id,
the last state and the error), and `rejected` when the appliance refused the
spec (for example `400`). The checks come back as one flat row each, nested
checks included, with the vendor error text. The main numbers also come back
as flat keys (`required_cores`, `required_memory`, `required_storage`,
`required_esx_hosts` and the same four `available_*` keys). The operation is
pinned as a credential write (its request holds the spec passwords), so a long
answer keeps only these flat keys inline (`result_scalars`) and drops the
`required` / `available` objects.

- The spec must hold no password value. A secret-named field with a value is
  refused before any call, with the field's JSON pointer in the message (never
  the value). The caller names each password in `password_refs` (a JSON
  pointer plus a Vault path); the handler puts them into a copy of the spec.
  The operation carries the `credential` tag.
- Use `async: true`: MEHO answers `202` with a run handle at once, and the
  stored result of the run has the final answer.
- There is nothing to clean up. The calculation changes nothing, and MEHO
  opens no download session or temporary item; the login token is the
  connector's shared one. The connector's HTTP client never follows a
  redirect, so the login token never goes to another host.
- MEHO's async mode does not resume interrupted work. If MEHO restarts during
  the call, the run ends as failed. The vendor's calculation record stays on
  the appliance; it is harmless. Start the operation again; a new run makes a
  new calculation.

### `installer.composite.sddc.bringup` — governed bring-up write (composite, `dangerous`)

The highest-blast-radius write in the product, in `bringup.py`. A `dangerous` +
`requires_approval` composite (`source_kind="composite"`) orchestrating, as one
approved unit:

1. **validate** — `POST /v1/sddcs/validations` with the `SddcSpec` body, then poll
   `GET /v1/sddcs/validations/{id}` to a terminal `executionStatus`. Validation is
   a **non-mutating dry-run**, so it is *ungated*. The deploy is gated on the
   `resultStatus`: `SUCCEEDED`/`WARNING` proceed (warnings surfaced in the
   return), anything else returns `validation_failed` / `validation_timeout`
   **before any mutation**, with the failed checks summarised.
2. **deploy** — `POST /v1/sddcs` with the same `SddcSpec` through
   `enforce_subop_policy`. This is the single state mutation and the single sub-op
   gate (the top-level composite is `requires_approval=True`; the deploy sub-op
   passes `requires_approval=False` so an approved-and-resumed dispatch is not
   double-gated).
3. **hand off** — the bring-up runs for *hours*, so the composite returns
   `{status: "deploying", sddc_task: {id, status, …}, poll_with:
   "installer.sddc.bringup.status"}` the moment the deploy is accepted. The caller (an
   operator or the deploy-automation add-on's durable workflow) polls
   `installer.sddc.bringup.status` to a terminal `COMPLETED_WITH_SUCCESS` /
   `ROLLBACK_SUCCESS` / `COMPLETED_WITH_FAILURE`. Blocking one dispatch on a
   multi-hour terminal state would be wrong.

**Secret hygiene (#1503).** The park-time approval preview and the sub-op policy
params are both built by `_blast_radius(spec)`, which reads only a whitelist of
SDDC identity + network keys and *never* reads any `*Password` /
`credentials` field of the `SddcSpec` — redaction by construction. Covered by the
whole-suite secret-leak guard.

**Direct-session dispatch (Goal #2247).** Every sub-call goes through the injected
connector's own session helpers (`_post_json_with_session_retry` /
`_get_json_with_session_retry`) — never `dispatch_child` into an ingested
`METHOD:/path` primitive — so correctness never depends on per-deploy catalog
state. `_post_json_with_session_retry` is the write-path twin of the increment-1
GET helper: a `401` means auth was rejected *before* the server processed the
request, so re-issuing the non-idempotent POST once after a re-login cannot
double-apply.

Shipped as increment 2 of [#3065](https://github.com/evoila/meho/issues/3065).

### Operation contracts

The 20 typed ops and the bring-up composite meet the VMware operation-contract
check (`backend/tests/test_operation_contracts.py`), with one gap left
(see below):

- **Response schemas** live in `_response_schemas.py` (split out for the
  file-size budget). Vendor field names come from the pinned `vcf-installer-9.1`
  OpenAPI file (`Validation`, `SddcTask`, `DepotSettings`, `DepotSyncInfo`,
  `PageOfTrustedCertificate`, `PageOfBundle`,
  `PageOfBundleDownloadStatusInfo`). The envelope keys of the writes and of the
  composite (`status`, `settings`, `results`, `sddc_task`, ...) are the keys the
  handlers build. The schemas are informational; the reducer does not read them.
- **Examples.** Each parameter schema has one example call under `examples`:
  `{}` for the no-argument reads, a placeholder such as `<validation-id>` for
  the reads by id, and the short placeholder `SDDC_SPEC_EXAMPLE` for the spec
  submits.
- **AI guidance.** Each `llm_instructions` blob has `when_to_call`,
  `output_shape` and `next_step`; the changes also have `preconditions`. The
  JSONFlux hints (`result_scalars`, `result_digest`) are unchanged.
- **One group text.** The `installer-bringup` group text is defined only in
  `typed_ops.INSTALLER_TYPED_WHEN_TO_USE_BY_GROUP`. `bringup.py` reads it from
  there, so the group row gets the same text whichever registrar runs first.
- **Open gap.** `installer.sddc.spec.validate` is `caution` without
  `requires_approval`. It only checks a spec and deploys nothing; making it wait
  for approval would change what it does, so it stays on the gap list.

## Installer 9.0

A VCF 9.0.x bring-up uses Installer 9.0.2. Such a target resolves to
`installer-rest-9.1`, because the range is `>=9.0,<10.0`. There is no separate
9.0 connector, because MEHO calls no path that 9.0 lacks:

- The pinned 9.0 and 9.1 installer API files were compared with
  `backend/scripts/diff_vcf_api_contract_catalogs.py`. 9.1 adds 5 operations
  and removes none. MEHO calls one of the 5 new ones, the resources
  calculation, and only on 9.1 targets (see the version check above). It
  calls none of the others (SDDC Manager discovery, vCenter network discovery,
  depot machine details).
- The 9.0 case of the spec-reconcile lane (below) checks this in CI.

**Every installer operation but one works on 9.0.** That is 19 typed operations
(`installer.sddc.spec.validate`, `installer.sddc.validation.status`,
`installer.sddc.bringup.start`, `installer.sddc.bringup.retry`,
`installer.sddc.bringup.status`, `installer.sddc.list`,
`installer.bundles.list`, `installer.bundles.download-status`,
`installer.bundles.download`, `installer.system.depot.get`,
`installer.system.depot.set`, `installer.system.depot.sync-status`,
`installer.system.trusted-certificates.list`,
`installer.system.trusted-certificates.add`, `installer.system.proxy.get`,
`installer.system.proxy.set`, `installer.system.settings.get`,
`installer.system.settings.set`,
`installer.system.trusted-certificates.remove`) and the multi-step operation
`installer.composite.sddc.bringup`. The one exception is
`installer.sddc.resources.calculate`: the 9.0 file has no resource
calculation, so on a 9.0 target it stops early with "needs VCF Installer 9.1
or newer".

What is different on 9.0 (from the two API files):

- **SddcSpec.** MEHO sends the spec as it is, so the caller must build it for
  the Installer's version. A 9.0 SddcSpec has `vcfOperationsFleetManagementSpec`
  and no `vspClusterSpec`, `vidbSpec`, `fleetLcmSpec`, `sddcLcmSpec`,
  `saltSpec` or `licenseServerSpec`. The `spec` hint says that rules (1) and
  (3) are 9.1 only.
- **Depot settings.** On 9.0, `depotConfiguration` needs `hostname` and `port`
  and has no `url`. `vmwareAccount` has no `downloadActivationCode`.
- **Answers.** Some 9.1 fields are not in a 9.0 answer, for example
  `deploymentType` and `vcfInstanceName` on a bring-up task, and the DNS, NTP
  and IP fields of `appliance-info`. MEHO reads these fields only when they are
  there.
- The token login (`POST /v1/tokens`), the trusted-certificate body and the
  bundle download body are the same.

A live bring-up of VCF 9.0 with Installer 9.0.2 through MEHO is a separate
lab proof.

## Spec-reconcile lane

Every hand-coded `METHOD:/path` — `POST:/v1/tokens`,
`GET:/v1/system/appliance-info` (fingerprint), the typed reads'
`GET:/v1/sddcs/{id}`, `GET:/v1/sddcs`, `GET:/v1/sddcs/validations/{id}` and
the depot-family reads such as `GET:/v1/system/settings/depot/depot-sync-info`
(the `_*_PATH` constants of `typed_reads.py`, among others), the submit primitives'
`POST:/v1/sddcs/validations` and `POST:/v1/sddcs` (introspected from
`typed_writes.TYPED_WRITE_DECLARED_OP_IDS`), and the composite's
`POST:/v1/sddcs/validations`, `POST:/v1/sddcs`,
`GET:/v1/sddcs/validations/{id}` (introspected from
`bringup.BRINGUP_DECLARED_OP_IDS`), and the settings calls of
`system_settings.SYSTEM_DECLARED_OP_IDS` (proxy, CEIP, system, certificate
remove, `GET:/v1/tasks` and `GET:/v1/tasks/{id}`) — is asserted against **both** pinned
shelf specs, `vcf-installer-9.0` and `vcf-installer-9.1` (vendored from
`vmware/vcf-api-specs`; one test case per file), by
[`backend/tests/test_connectors_vcf_installer_spec_reconcile.py`](../../backend/tests/test_connectors_vcf_installer_spec_reconcile.py)
(the #2980 harness; parse-only, in the required unit sweep, uniform skip when the
shelf is unconfigured). CI checks out both files and fails when one is missing,
so neither case skips there. A new operation that calls a 9.1-only path fails
the 9.0 case; such an operation then needs a "not available on this version"
answer for 9.0 targets before the range can stay at 9.0. The resource
calculation is such an operation: `resources_calc.RESOURCES_DECLARED_OP_IDS`
is checked against the 9.1 file only, and a guard checks that the 9.0 file
still lacks it. Standard:
[`docs/decisions/spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md).

## Dependencies

- **httpx** — per-target `AsyncClient` pool (inherited from `HttpConnector`);
  Bearer token computed by the connector.
- **respx** (test-only) — the unit-test module mocks every request shape without
  a network call.
- **structlog** — `installer_session_established` / `installer_credentials_loaded`
  events, both carrying `target` and `host`.

## Known issues

- The composite **kicks off** the bring-up and returns the `SddcTask` id; it does
  not block on the multi-hour deployment. Terminal state (`COMPLETED_WITH_SUCCESS`
  / `ROLLBACK_SUCCESS` / `COMPLETED_WITH_FAILURE`) is reached by polling
  `installer.sddc.bringup.status`. A `COMPLETED_WITH_FAILURE` resumes via the
  `installer.sddc.bringup.retry` primitive (#3123); *durable* orchestration of
  the wait-and-retry loop is the deploy-automation add-on's DBOS workflow, not
  this connector.
- `WARNING`-result validations proceed to deploy (VCF treats them as non-fatal);
  the return always echoes `validation_result_status` (and the leaf WARNING
  checks when present), but the approver saw the preview *before* validation ran.
  Blocking on `WARNING` would make most lab bring-ups unrunnable; revisit if a
  stricter posture is needed.
- **Secret at rest in the approval table.** The `_blast_radius` preview + sub-op
  policy params never carry a password, but when the top-level op parks for
  approval the dispatcher persists the raw dispatch `params` (the full `SddcSpec`,
  including plaintext passwords) verbatim in `approval_request.params` for the
  approval TTL (#1503 store-verbatim). That column is never surfaced by any API /
  audit / broadcast read path — exposure is at-rest-in-the-governance-table,
  gated by DB/row access control, the same posture as every `requires_approval`
  op taking inline secrets (e.g. the GOSC composites). A Vault-ref-in-spec
  redesign that keeps plaintext out of the row entirely is future work.
- **Agent callers are not a supported path.** The intended caller is a non-agent
  operator or the automation add-on's service account. A run-bound agent hits the
  backplane's `dangerous` safety-ceiling on the deploy sub-op and would re-park a
  second, currently un-resumable approval (fleet-wide `dangerous`-composite
  behavior, not specific to this op).
- The default credentials loader is the live State-2 read; a target must be
  `shared_service_account` with a `secret_ref` resolving to a
  `{username, password}` KV-v2 secret.

## References

- Tasks: [#3065](https://github.com/evoila/meho/issues/3065) (skeleton +
  composite), [#3078](https://github.com/evoila/meho/issues/3078) (submit/poll
  primitives). Parent Initiative:
  [#2907](https://github.com/evoila/meho/issues/2907). Parent Goal:
  [#221](https://github.com/evoila/meho/issues/221).
- Template precedent: `connectors/sddc_manager/` (identical `session_login_token`
  auth), `connectors/vcf_fleet/` (skeleton wiring).
- VCF Installer API reference:
  <https://developer.broadcom.com/xapis/vcf-installer-api/latest/>.
