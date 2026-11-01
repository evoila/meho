# Connector: fleet-lcm (VCF 9 Fleet LCM Service, modern generic impl)

## Overview

The `fleet-lcm` connector is the hand-rolled `HttpConnector` subclass that
makes the **VCF 9 Fleet LCM Service** REST API dispatchable under the
`(product="fleet", version="9.0", impl_id="fleet-lcm")` registry triple. It is
the **modern** implementation of `product=fleet` and coexists with the legacy
typed `fleet-rest` impl ([`connectors-vcf-fleet.md`](connectors-vcf-fleet.md)) —
the codebase's first real two-implementation case (initiative
[#3033](https://github.com/evoila/meho/issues/3033), tasks #3036 + #3037,
decision of record `versioned-connector-dual-impl`). The two are resolved per
target by fingerprint; see the "Dual implementation" section of the vcf-fleet
doc for the resolution matrix and the
[resolution test](../../backend/tests/test_connectors_fleet_dual_impl_resolution.py).

Source: `backend/src/meho_backplane/connectors/fleet_lcm/`.

The Fleet LCM Service (`https://vcf.broadcom.com/fleet-lcm` → `/v1/*`) is the
VCF 9 successor to vRealize Suite Lifecycle Manager (vRSLCM) 8.x. It is a
**distinct API surface** from the legacy `/lcm/lcops/api/v2/*` the `fleet-rest`
impl dispatches. The `/fleet-lcm/v1` API first ships with VCF 9.1 (its spec
file is in the `9.1.0.0` tag of `vmware/vcf-api-specs`). A VCF 9.0 fleet
appliance serves only the legacy `/lcm/*` API. So `fleet-lcm` covers
`>=9.1,<10.0` and `fleet-rest` covers `>=8.0,<9.1`; the ranges do not overlap
(#4442), and the resolver — not the connector — picks the surface per target.

## Scope: typed read core (#3047)

The connector ships a curated **13-op typed read core** — the modern successor
to the legacy `fleet-rest` read surface — registered as `source_kind="typed"`
and **enabled at register time**, so a VCF Fleet 9.1 or newer target (which
resolves here by version range) dispatches a modern `/v1/*` read op on a
**fresh boot with zero catalog ingest**. The #3036 skeleton left `fleet-lcm`
registered but with no dispatchable ops; the read core closes that gap.

The read core covers the operator's "is it up, what's deployed, what's
happening, what can we upgrade to" drill path across five groups:

| Group | Ops |
|---|---|
| `fleet-lcm-system` | `fleet-lcm.health`, `.system.info`, `.config.info` |
| `fleet-lcm-sddc` | `fleet-lcm.sddc-lcm.list`, `.sddc-lcm.info` |
| `fleet-lcm-components` | `fleet-lcm.component.list`, `.component.info`, `.component.status` |
| `fleet-lcm-tasks` | `fleet-lcm.task.list`, `.task.info` |
| `fleet-lcm-lifecycle` | `fleet-lcm.upgrade-plan.list`, `.upgrade-plan.info`, `.release-version.list` |

**Why typed, not ingested.** The connector *is* the modern generic impl and its
51-op spec is pinnable, but the read core is hand-coded typed (the harbor / nsx
#2358 pattern) precisely so it is live on a fresh boot with **no** operational
`meho connector ingest` + `enable_reads` step — merging the read core makes it
work, with no runbook the operator must still run. Six lifecycle changes
ship the same way (see "Lifecycle changes" below). The rest of the spec (as
`source_kind="ingested"` breadth, including the component and system writes)
can be enabled operationally through the generic review flow
(`ReviewService.enable_reads` over an ingest of the same pinned
`fleet-lcm-openapi.yaml`). Registering this real `HttpConnector` subclass
is also the load-bearing prerequisite for those future ingested rows to become
dispatchable (a bare `GenericRestConnector` auto-shim is non-dispatchable — the
`VmwareRestConnector` pattern).

**Not yet proved on a live system.** The read core and the login (see "Auth"
below) are unit-tested end to end against a respx-mocked service and checked
against the vendor files. A separate lab proof runs the login on VCF 9.1.0 and
9.1.1.

## Lifecycle changes (VCF 9.1 and newer)

Six typed changes, written by hand, live on a fresh install with no spec
ingest. Each one waits until a second person approves it
(`requires_approval: true`). An agent may run a `caution` change after
approval; a `dangerous` change also needs a grant. Every op here is on the
`fleet-lcm-9.0` connector, which serves VCF 9.1.0 and 9.1.1 (range
`>=9.1,<10.0`); a 9.0 fleet resolves to the older `fleet-rest` connector.

| Operation | What it does | Vendor call | Returns | Risk | Versions |
|---|---|---|---|---|---|
| `fleet-lcm.task.retry` | Retry a failed task from the stage that failed. | `POST /v1/tasks/{taskId}?action=retry` | `accepted` + task id + status read | dangerous | 9.1+ |
| `fleet-lcm.task.cancel` | Cancel a running task. The running stage ends first; nothing is undone. | `POST /v1/tasks/{taskId}?action=cancel` | `accepted` + task id + status read | dangerous | 9.1+ |
| `fleet-lcm.upgrade-plan.create` | Check the plan spec, then create the plan. | `POST /v1/upgrade-plans/validations`, then `POST /v1/upgrade-plans` | the steps, the new plan | caution | 9.1+ |
| `fleet-lcm.upgrade-plan.precheck` | Start the pre-check of a plan. | `POST /v1/upgrade-plans/{planId}?action=precheck` | `accepted` (or `already_running`) + task id | caution | 9.1+ |
| `fleet-lcm.upgrade-plan.apply` | Start the upgrades of a plan. | `POST /v1/upgrade-plans/{planId}?action=apply` | `accepted` (or `already_running`) + task id | dangerous | 9.1+ |
| `fleet-lcm.depot.sync` | Sync the depot metadata (the release catalog) now. | `POST /v1/depot-metadata?action=sync` | `accepted` + task id | caution | 9.1+ |
| `fleet-lcm.task.info` (read) | One task: the vendor task plus `state`, `progress`, `errors`. The status read of every job. | `GET /v1/tasks/{taskId}` | task + state | safe | 9.1+ |

The issue that asked for these named the status read `fleet-lcm.task.get`
and used `upgrade_plan` in the ids. The connector already had
`fleet-lcm.task.info` for the same call, and its ids use `upgrade-plan`, so
the existing read was extended and the ids keep that pattern.

**Vendor jobs.** Retry, cancel, pre-check, apply and depot sync start a job
at the vendor and return at once:

- The answer says `status: accepted`, gives the vendor's `task_id`, and names
  the status read (`status_read: {op_id: fleet-lcm.task.info, params:
  {taskId}}`). It never says the work is done.
- `fleet-lcm.task.info` gives `state` (queued, running, succeeded, failed,
  canceled, unknown), `progress` (stages done of stages total, and the stage
  that runs now), and `errors` (the `ERROR` texts of the task, its stages and
  its sub-tasks). The caller reads it until the job ends.
- When the service does not answer (no connection, a timeout, or HTTP 502 /
  503 / 504, for example while it upgrades itself), the read says `state:
  not_reachable` with the task id, the reason and `checked_at`. It never says
  `failed`. MEHO keeps no job state between calls.
- When the login expired, the read gets a `401`; the dispatcher logs in again
  and reads once more. When that new login meets HTTP 502 / 503 / 504 at the
  login service, the read also says `not_reachable` (reason `login HTTP 503`,
  for example) with the task id. A refused login (401 / 403) stays an error
  (`connector_auth_failed`).
- Before a pre-check or an apply, MEHO reads the plan. When its `executions`
  list a run of the same kind (`PRECHECK` or `UPGRADE`) that has not ended,
  MEHO returns that run's task id as `already_running` and starts nothing.
  This is a help, not a lock: two callers can both read before either starts.
- The depot sync has no MEHO check: the vendor runs one sync at a time and
  returns the running sync's task when one runs.
- After a lost reply, do not start again blindly. Read first: the plan
  (`fleet-lcm.upgrade-plan.info`, its `executions`) for pre-check and apply,
  the task (`fleet-lcm.task.info`) for retry and cancel, `fleet-lcm.task.list`
  for the depot sync, and `fleet-lcm.upgrade-plan.list` for create.
- Use MEHO's async mode for a start (`async: true`, see
  [`async-governed-dispatch.md`](async-governed-dispatch.md)): MEHO stores the
  start's answer, with the task id, under a run handle, so a lost reply does
  not lose the task id.

**Create, in two steps.** `fleet-lcm.upgrade-plan.create` posts the spec to
the vendor's plan check (itself a job), reads that job until it ends (at most
one minute), and creates the plan only when the check passed (no `ERROR` in
`failedValidations`, no failed job). The answer lists `steps`, `steps_ran`,
`step_failed`, `steps_not_run` and a `note` that says what to do next. A
failed check, or a check still running after the wait, creates nothing. A
create that fails after the check passed lists the check as run and the
create as failed with the vendor's text; nothing is undone. The vendor's
create updates a matching plan, so a second call does not make a second plan.

The answer keeps what is known when the service stops answering:

- While MEHO follows the check (no connection, a timeout, HTTP 502 / 503 /
  504, or a login service that answers so): outcome `validation_not_reachable`.
  The check step says `not_reachable` and keeps the check's task id, so the
  caller can read it later with `fleet-lcm.task.info`. Nothing was created.
- At the create itself: outcome `create_unknown`. The check is listed as run,
  the create step says `unknown` with the reason. The plan may or may not
  exist; read `fleet-lcm.upgrade-plan.list` before calling again.

**No secrets.** No input takes a password. The vendor's `userInputSpec` can
carry a VSP cluster password for a VCF Automation upgrade; MEHO offers only
the non-secret fields (`additionalProperties: false`), so such an upgrade
that needs a new cluster password is not possible through MEHO yet. The
answers carry vendor task data only.

Code: `lifecycle_ops.py` (metadata, schemas, `llm_instructions`),
`lifecycle_handlers.py` (bodies and the status read), `op_types.py` (the
shared `FleetLcmTypedOp` record). Tests:
[`test_connectors_fleet_lcm_lifecycle.py`](../../backend/tests/test_connectors_fleet_lcm_lifecycle.py).

## Key types

- **`FleetLcmConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="fleet"`, `version="9.0"`, `impl_id="fleet-lcm"`,
  `supported_version_range=">=9.1,<10.0"`, `priority=1`. The range starts at
  9.1, where the `/fleet-lcm/v1` API first ships, so a 9.0 target resolves to
  the legacy `fleet-rest` instead (#4442); `priority=1` equals the legacy impl
  by design (the split is by version range, not priority).
- **`FleetLcmTargetLike`** / **`FleetLcmCredentialsLoader`** (`session.py`) —
  aliases of the shared `VcfTargetLike` Protocol and `VcfCredentialsLoader`
  type (the modern impl reads the same target fields as the legacy). The
  loader is injectable on construction
  (`FleetLcmConnector(credentials_loader=...)`) for unit / integration tests.
- **`load_credentials_from_vault`** (`session.py`) — the fleet-lcm default
  loader. Reads the target's Vault secret under the operator's identity (via
  the shared `load_vault_secret_data`). Returns the VCF Operations `username`
  and `password` (required), plus `vcf_ops_url` and `token` when they are set.
  A bad `vcf_ops_url` fails closed with an error that names the target.
- **`mint_fleet_lcm_token`** (`session.py`) — the two-step VCF Operations
  login (acquire, then exchange) on a given client. Returns the Bearer token.
- Canonical constants (`__init__.py`): `FLEET_LCM_PRODUCT`,
  `FLEET_LCM_VERSION`, `FLEET_LCM_IMPL_ID`, `FLEET_LCM_CONNECTOR_ID`.

The connector reuses `is_acceptable_auth_model`, `CredentialsCache`,
`vcf_session_login` and `session_establish_auth_error` from
`meho_backplane.connectors._shared.vcf_auth`.

## Control flow

### Registration

Importing `meho_backplane.connectors.fleet_lcm` calls
`register_connector_v2(product="fleet", version="9.0", impl_id="fleet-lcm",
cls=FleetLcmConnector)`. **No wildcard sibling** is registered: the legacy
`vcf_fleet` package already owns the `("fleet","","")` fallback, and a second
class on that key would raise `RuntimeError` at import. One wildcard per
product suffices (it only guarantees an unfingerprinted target resolves
*something*); the versioned entries carry the real per-band matching.

### Auth (Bearer token from VCF Operations, #4476)

The VCF 9.1 fleet lifecycle API takes a Bearer token. VCF Operations hands it
out in two steps. `auth_headers` returns `Authorization: Bearer <token>`:

1. It refuses any `target.auth_model` other than `shared_service_account` or
   `None` (the shared `is_acceptable_auth_model`).
2. It reads the credentials from Vault (kept per target by the shared
   `CredentialsCache`).
3. **Stored token (fallback).** If the secret has a `token`, MEHO sends it as
   is and does not log in. This is the old way. A stored token expires, so
   it is only a fallback.
4. **Login (the normal way).** Otherwise MEHO logs in once per credential
   and keeps the token (`_session_tokens`, keyed by `credential_cache_key`:
   tenant, secret and the resolved login address, see below). Two targets
   with one secret and one `vcf_ops_url` share one token, one login and one
   renewal, whatever their own hosts are (#5033):
   - `POST /suite-api/api/auth/token/acquire` with
     `{"username", "password"}` → `{"token": ...}` (a VCF Operations token).
   - `POST /suite-api/api/auth/token/exchange` with
     `Authorization: OpsToken <token>` and `{"serviceKeys": ["fleet-lcm"]}`
     → `{"jwtToken": ...}`, the Bearer token.

**Where the login goes.** The login paths start at the root of the host
(`/suite-api/...`). By default MEHO logs in at the target's own host and port.
A service path carried on the target's `host` (for example `/fleet-lcm`) is
used for the fleet calls only, not for the login. When VCF Operations has a
different address, put it in the secret as `vcf_ops_url` (`https://host` or
`https://host:port`; https only, no path). An IPv6 address keeps its brackets,
for example `https://[fd00::10]:8443`. The login always uses its own pooled
client (`_LoginEndpoint`). It gets the same address check (SSRF guard) and the
same TLS rules (`verify_tls`, `tls_ca_pin`) as the target. A target's
`tls_server_name` is used only when the login goes to the target's own host.
The token key is built from this resolved login address. The login does not
send the target's `fqdn`, and the key does not hold it. The connector keeps,
per target, the key its token sits under (`_login_keys`), so the `401` path
(`invalidate_session`) finds the token without reading the secret again.

**Renewal.** When a fleet call answers `401`, the dispatcher calls
`invalidate_session(target)` and runs the call once more. `invalidate_session`
drops the kept token and the kept credentials, so the retry reads Vault again
and logs in again. A second `401` ends as `connector_auth_failed`. A `401` or
`403` at acquire or exchange raises `ConnectorAuthError` (also
`connector_auth_failed`); the dispatcher then calls `invalidate_credentials`,
so a fixed secret is picked up on the next call.

**No secret in output.** The login does not go through the flight recorder
(it uses `client.post` directly). Error texts name the target, the path and
the HTTP status only. The log line `fleet_lcm_session_established` names the
target and the login host only. Tests check that no password or token appears
in a result, an error text or a log line.

**The depot token (`VCF_FDS`).** The depot service takes its own token, with
the service key `VCF_FDS`. This connector calls no depot API (all 13 reads
are fleet lifecycle `/v1/*` calls), and neither vendor file names service keys.
So there is no `VCF_FDS` token here. A future depot operation would add it.

### Fingerprint / probe

Unlike the legacy impl (whose diagnostic endpoints return HTTP 500 on 9.0),
the modern service exposes a first-class health endpoint. The connector probes
`GET /v1/health` (`operationId` `getHealth`; the spec marks it `security: []`).
`fingerprint` returns a reachable `FingerprintResult`
(`vendor="vmware"`, `product="fleet"`, `probe_method="GET /v1/health ..."`),
with `version` taken from an explicit `version` string in the health payload
when present, else `None`. On any transport/status failure it returns a
non-reachable result carrying `extras["error"]` (the Harbor / NSX / vcf-fleet
pattern). `probe()` delegates to `fingerprint()`.

### Dispatch

The typed read-core ops dispatch through `meho_backplane.operations.dispatch`
(via `POST /api/v1/operations/call` or MCP `call_operation`): the dispatcher
resolves the persisted `module.ClassName.method` handler_ref to the connector's
bound-method shim, threads `operator` / `target` / `params`, and the handler
issues an authenticated `GET` (or, for the six changes, `POST`) on this
connector's client — so every call rides `auth_headers` (the Bearer token). `execute()` is the G0.6
ABC-compatibility shim (builds a synthetic `Operator` and forwards to
`dispatch`); it remains the path for any future `source_kind="ingested"` op.

## Typed read core internals

The read core follows the harbor #2856 layout:

- **`typed_ops.py`** — the `FleetLcmTypedOp` dataclass table
  (`FLEET_LCM_TYPED_OPS`), the per-group `when_to_use` map
  (`FLEET_LCM_TYPED_WHEN_TO_USE_BY_GROUP`), and the module-level
  `register_fleet_lcm_typed_operations(*, embedding_service=None)` registrar.
- **`typed_reads.py`** — the op bodies (`async def fleet_lcm_*_impl(connector,
  operator, target, params)`), each issuing an authenticated `GET` via
  `HttpConnector._get_json` and returning the parsed payload.
- **`_llm_instructions.py`** — the per-op `llm_instructions` blobs
  (`when_to_call` / `output_shape` / `next_step`, plus `parameter_hints` on the
  by-id reads), keyed by op id (split out for the file-size budget).
- **`_response_schemas.py`** — the per-op response schemas
  (`FLEET_LCM_RESPONSE_SCHEMAS`), keyed by op id. The field names come from
  the vendor's `fleet-lcm-openapi.yaml` (9.1.0.0, checked against 9.1.1). They
  are informational; the reducer does not read them.
- **`_paths.py`** — the `/v1/*` request-path templates + `fill_path` /
  `encode_segment` (percent-encoded, fail-loud path substitution for the by-id
  ops; the reconcile lane introspects these constants).
- **`connector.py`** — 13 thin bound-method shims (`health`, `system_info`, …)
  that lazily import and delegate to their `_impl` body; each typed op's
  `handler_attr` names its shim.

`register_fleet_lcm_typed_operations` is queued onto the lifespan registrar
list via `register_typed_op_registrar` in `__init__.py`;
`register_typed_operation` creates each `OperationGroup` (`review_status=
"enabled"`) and inserts each descriptor `source_kind="typed"`, `is_enabled=
True`, `method=None` / `path=None` (the wire path lives only in the handler).
So the read core needs no operator review — it is live the moment the connector
loads.

**Operation contracts.** The 13 typed ops meet the VMware operation-contract
check (`backend/tests/test_operation_contracts.py`): each response schema
names its main fields, each parameter schema carries one example call under
`examples` (`{}` for the no-argument reads, a placeholder such as
`<component-id>` for the by-id reads), and each `llm_instructions` blob has
`when_to_call`, `output_shape` and `next_step`.

## Spec reconcile lane

[`backend/tests/test_connectors_fleet_lcm_spec_reconcile.py`](../../backend/tests/test_connectors_fleet_lcm_spec_reconcile.py)
asserts every hand-coded `METHOD:/path` the typed ops dispatch — the 13 `GET`
templates and the 7 `POST` templates (named `_*_POST_PATH`) in `_paths.py`,
plus the connector's `_FLEET_LCM_HEALTH_PATH` probe (introspected from the
live constants — the #2944 pattern) — is served by the pinned
`fleet-lcm-9.0/fleet-lcm-openapi.yaml` (9.1.0) and by the 9.1.1 file next to
it (`fleet-lcm-openapi-9.1.1.yaml`). The `?action=` part of a `POST` template
is part of the vendor's own path key, so it compares byte for byte. CI
requires both files.
The typed health op and the probe share `/v1/health`; the pin asserts that link
so the two never drift. **No server-base fold** is applied: the spec's server
url `https://vcf.broadcom.com/fleet-lcm` is *absolute*, so `parse_openapi` does
not fold the `/fleet-lcm` base onto path keys (only *relative* server bases are
folded, #1796) — the served op_ids are the raw `GET:/v1/*`, matching the
templates (the by-id placeholders `{sddcLcmId}` / `{componentId}` / `{taskId}` /
`{planId}` are byte-for-byte the spec's own path-parameter names). The lane arms
when the shelf provides the spec (`MEHO_CONSUMER_DOCS_ROOT`) and skips uniformly
otherwise (`tests/_spec_shelf.py` contract). (The wider ingested breadth is not
reconciled here — reconciling ingested rows against the spec they were ingested
from is tautological.)

The same file also checks the two VCF Operations login paths (#4476):
`POST:/suite-api/api/auth/token/acquire` against the pinned
`vcf-operations-9.0/vcf-operations-openapi.ingestable.json`, and both login
paths against `vcf-operations-9.1/vcf-operations-openapi.ingestable.json`
(the exchange path is new in 9.1). The vendor's 9.1 file fails the same
metaschema check as the 9.0 file, so the lane reads an `.ingestable.json`
copy. The 9.1 check skips until that file is on the shelf.

## Dependencies

- `meho_backplane.connectors.adapters.http.HttpConnector` — pooled
  `httpx.AsyncClient` per target, retry-on-idempotent transport, base-URL
  composition.
- `meho_backplane.connectors._shared.vcf_auth` — `is_acceptable_auth_model`,
  `CredentialsCache`, `vcf_session_login`, `session_establish_auth_error`,
  `ConnectorAuthError`.
- `meho_backplane.connectors.schemas` — `AuthModel`, `FingerprintResult`,
  `ProbeResult`, `OperationResult`.
- `httpx`, `respx` (test-only). No new runtime deps.

## Known issues / follow-ups

- **The login is not yet proved live.** It follows the vendor's VCF Operations
  9.1 file and is unit-tested with mocked answers. A separate lab proof runs it
  on VCF 9.1.0 and 9.1.1. That proof also shows whether VCF Operations and the
  fleet lifecycle API share one address (no `vcf_ops_url` needed) or not.
- **The VCF Operations token is not released.** After the exchange MEHO does
  not call `token/release`. VCF Operations ends an unused token after six
  hours.
- **Typed ops: the read core and six changes.** The rest of the 51-op `/v1/*`
  surface (as `source_kind="ingested"` breadth, including the component and
  system writes) is enabled operationally through the generic review flow
  (`meho connector ingest` of `fleet-lcm-openapi.yaml` → `ReviewService.enable_reads`),
  not shipped here — see the operator runbook
  [`docs/cross-repo/fleet-lcm-onboarding.md`](../cross-repo/fleet-lcm-onboarding.md).
- **The lifecycle changes are not yet proved live.** They follow the vendor
  file and are tested with mocked answers. A separate lab proof runs them on
  VCF 9.1.0 and 9.1.1. The vendor file does not list the task `type` values
  or say which `resourceId` a plan's tasks carry, so MEHO does not rely on
  them; the running-job check reads the plan's `executions` instead.
- **A VCF Automation upgrade that needs a new VSP cluster password** cannot
  be planned through MEHO yet (no password input is offered).

## References

- Typed read-core Task: <https://github.com/evoila/meho/issues/3047>
- Lifecycle changes Task: <https://github.com/evoila/meho/issues/4477>
- Modern-impl skeleton Task: <https://github.com/evoila/meho/issues/3036>
- Legacy re-band + resolution test Task: <https://github.com/evoila/meho/issues/3037>
- Parent initiative: <https://github.com/evoila/meho/issues/3033>
- Read-core tests:
  [`test_connectors_fleet_lcm_typed_reads.py`](../../backend/tests/test_connectors_fleet_lcm_typed_reads.py)
  (dispatch + registration) and
  [`test_connectors_fleet_lcm_spec_reconcile.py`](../../backend/tests/test_connectors_fleet_lcm_spec_reconcile.py)
  (13-path reconcile lane).
- Auth tests:
  [`test_connectors_fleet_lcm_auth.py`](../../backend/tests/test_connectors_fleet_lcm_auth.py)
  (the login steps, the stored-token fallback, `vcf_ops_url`, renewal, refused
  logins, no secret in logs or errors) and
  [`test_connectors_fleet_lcm_credread.py`](../../backend/tests/test_connectors_fleet_lcm_credread.py)
  (the real default loader through a Vault fake). The `401` → new login →
  success path through `dispatch()` is in
  [`test_connectors_fleet_lcm_typed_reads.py`](../../backend/tests/test_connectors_fleet_lcm_typed_reads.py).
- Login Task: <https://github.com/evoila/meho/issues/4476>
- Operator runbook: [`fleet-lcm-onboarding.md`](../cross-repo/fleet-lcm-onboarding.md).
- Legacy impl: [`connectors-vcf-fleet.md`](connectors-vcf-fleet.md)
- Spec provenance: `vmware/vcf-api-specs@c3f3b52c` (Apache-2.0); shelf
  `docs/fleet-lcm-9.0/MANIFEST.md`.
