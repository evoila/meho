# Connector: vcf-installer (VCF Installer 9.1)

## Overview

The `vcf-installer` connector is the hand-rolled `HttpConnector` subclass that
dispatches VMware Cloud Foundation **Installer** operations under the
`(product="installer", version="9.1", impl_id="installer-rest")` registry triple
(`connector_id="installer-rest-9.1"`). The Installer is the VCF 9.x bring-up
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
then decomposed the surface into four **submit/poll typed primitives** (the
vendor bring-up API is natively asynchronous, so every governed call is short
and the standard park → approve → resume machinery works as-is): the durable
multi-hour orchestration loop belongs to the deploy-automation add-on, which
binds to these primitives, while the composite remains the one-shot governed
validate-then-deploy unit for operators.

Source: `backend/src/meho_backplane/connectors/vcf_installer/`.

## Key types

- **`InstallerConnector`** (`connector.py`) — `HttpConnector` subclass.
  `product="installer"`, `version="9.1"`, `impl_id="installer-rest"`,
  `supported_version_range=">=9.1,<10.0"`, `priority=1`.
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

### The four submit/poll primitives (typed, [#3078](https://github.com/evoila/meho/issues/3078))

All four are typed ops (`source_kind="typed"`), dispatchable on a fresh boot
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
| `installer.sddc.bringup.status` | `GET /v1/sddcs/{id}` → `SddcTask` | `safe` |

`spec.validate` submits the caller's `SddcSpec` verbatim as a non-mutating
dry-run; `validation.status` polls the returned id to a terminal
`executionStatus` and surfaces the `resultStatus` verdict (`SUCCEEDED` /
`WARNING` / `FAILED`) plus `validationChecks[]`. `bringup.start` is the estate
mutation: the dispatcher parks it for approval before the handler runs, and its
park-time preview is the composite's `_blast_radius`-backed identity/network
echo (registered for both op_ids in `bringup.py`) — never a password.
`bringup.status` polls the `SddcTask` (`IN_PROGRESS` /
`COMPLETED_WITH_SUCCESS` / `COMPLETED_WITH_FAILURE`, plus `sddcSubTasks[]` /
`milestones[]`). Session-expiry recovery for all four rides the dispatcher's
#2067 arm (raw `401` → `invalidate_session` → re-dispatch once — safe for the
POSTs too, since a `401` is rejected at auth before the server processes the
request).

**Poll scalars survive JSONFlux reduction
([#3084](https://github.com/evoila/meho/issues/3084)).** All four
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

## Spec-reconcile lane

Every hand-coded `METHOD:/path` — `POST:/v1/tokens`,
`GET:/v1/system/appliance-info` (fingerprint), the typed reads'
`GET:/v1/sddcs/{id}` and `GET:/v1/sddcs/validations/{id}` (the `_*_PATH`
constants of `typed_reads.py`), the submit primitives'
`POST:/v1/sddcs/validations` and `POST:/v1/sddcs` (introspected from
`typed_writes.TYPED_WRITE_DECLARED_OP_IDS`), and the composite's
`POST:/v1/sddcs/validations`, `POST:/v1/sddcs`,
`GET:/v1/sddcs/validations/{id}` (introspected from
`bringup.BRINGUP_DECLARED_OP_IDS`) — is asserted against the pinned
`vcf-installer-9.1` shelf spec (vendored from `vmware/vcf-api-specs`) by
[`backend/tests/test_connectors_vcf_installer_spec_reconcile.py`](../../backend/tests/test_connectors_vcf_installer_spec_reconcile.py)
(the #2980 harness; parse-only, in the required unit sweep, uniform skip when the
shelf is unconfigured). Standard:
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
  `installer.sddc.bringup.status`. Durable orchestration of that wait (retry, resume) is
  the deploy-automation add-on's DBOS workflow, not this connector.
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
