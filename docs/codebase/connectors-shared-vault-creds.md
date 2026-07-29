# Shared operator-context Vault basic-credentials helper

## Overview

`backend/src/meho_backplane/connectors/_shared/vault_creds.py` is the
single reusable helper every REST connector loader uses to resolve a
target's `secret_ref` to vendor credentials, reading a KV-v2 secret
**under the operator's identity**. G3.9-T2 (#941) landed it so the vmware
loader (G3.9-T3 #942) and the REST fan-out (#G3.10) share one
implementation with one tested error contract — rather than each
connector re-deriving the hvac call.

It implements the locked architecture decision in
`docs/architecture/connector-auth.md` (Option A, operator-context): the
read forwards the operator's validated Keycloak JWT to Vault's JWT/OIDC
auth method, giving per-operator RBAC (templated ACL policy) and
per-operator audit (Vault attributes the read to the operator's Identity
entity) through the single `meho-mcp` role.

Since #2229 (Initiative #2227) the loaders no longer read Vault directly.
They resolve the target's `secret_ref` through a **backend-agnostic
dispatch seam** (`connectors/_shared/credential_backend.py`): a
`secret_ref` scheme selects a `CredentialBackend` from a kind-keyed
registry. A schemeless ref (`targets/<id>`) resolves through the
deployment default (`config.credentialBackend` / `CREDENTIAL_BACKEND`,
default `vault`) and an explicit `vault:targets/<id>` ref resolves
identically — both dispatch to `VaultCredentialBackend`, today's KV-v2
read, byte-for-byte. A `gsm:<project>/<secret>[#field]` ref resolves
through `GcpSecretManagerBackend` (#2230, see `## GCP Secret Manager
backend (gsm)` below); any other unknown scheme raises
`UnknownCredentialBackendError` instead of a silent Vault attempt. The
seam is the target-credential analogue of the
`SECRET_ENDPOINT_REGISTRY` the `secret.move` broker already uses; the two
are separate registries because they serve different contracts (the
broker moves one opaque value; a credential backend returns a named-field
payload). See `## Credential-backend dispatch seam` below.

The helper reuses the lower-level primitive
`meho_backplane.auth.vault.vault_client_for_operator` + hvac's
`read_secret_version` — **not** the `vault.kv.read` op handler. The op
handler is coupled to the dispatch surface (returns `{"data", "version"}`
shaped for the reducer, registered as a typed op with a JSON schema); a
connector loader needs something narrower: a plain `dict[str, str]` of
named fields and a read-phase error contract distinct from the
dispatcher's `connector_error` branch.

## Key types

- **`load_basic_credentials(target, operator, *, fields=("username",
  "password"), mount="secret") -> dict[str, str]`** — the public entry
  point. Opens `vault_client_for_operator(operator)` (JWT/OIDC login),
  reads `target.secret_ref` as a KV-v2 secret off the event loop
  (`asyncio.to_thread` — hvac is synchronous), structurally unwraps the
  nested `data["data"]`, and returns the requested fields as a flat
  `{field: value}` dict. Values run through `strip_credential_value` —
  coerced to `str` so a numeric secret field round-trips as a string, and
  surrounding-whitespace-stripped so a trailing newline never reaches a
  vendor Basic-auth header verbatim.
- **`strip_credential_value(value) -> str`** — `str(value).strip()`. The
  one place credential whitespace is normalised. A trailing newline is the
  single most common secret-storage artifact (`echo` without `-n`, `jq -r`,
  an editor's final newline, `vault kv put k=-`); sent verbatim in an auth
  header or token-request body it surfaces as an upstream
  401/`unauthorized_client` that reads like a permissions/realm problem
  (#1474). `_extract_fields` applies it to every field, so every
  `load_basic_credentials` consumer is covered at once; the payload-shape
  discriminators that use `load_vault_secret_data` (keycloak / gh-rest, and
  the SSH adapter's `_resolve_secret` — key-vs-password selection plus the
  bind9 sudo password, #2155) call
  it directly on the fields they pluck. Only surrounding whitespace is
  trimmed — internal whitespace is preserved.
- **`CredentialsReadError`** (in `credential_backend.py`, re-exported from
  `vault_creds`) — the backend-neutral read-phase base, added by #2642.
  Catch this when the intent is "the credential could not be read, whatever
  the store is": the dispatcher renders a handler exception as
  `connector_error: <class name>`, so a Vault-named class on a
  `credentialBackend=gsm` deploy points the operator at a component that
  isn't installed. Connector probe / fingerprint handlers catch the base.
- **`VaultCredentialsReadError`** — the Vault subclass: read-phase failure
  (empty JWT, unset `secret_ref`, malformed payload, missing field).
  Deliberately distinct from `auth.vault.VaultClientError` (login-phase:
  Vault unreachable, role denied), so a caller can render an
  operator-actionable detail string per phase. A missing field never
  surfaces as a bare `KeyError`. `GcpSecretManagerReadError` is the GSM
  sibling subclass.
- **`BasicCredentialsTargetLike`** — runtime-checkable Protocol with
  fields `name`, `host`, `secret_ref`. The concrete `Target` model in
  `meho_backplane.targets` (G0.3 #224) satisfies it structurally
  unchanged.
- **`DEFAULT_KV_MOUNT = "secret"`** — the consumer convention mount (dev
  mode mounts `secret/` as KV-v2 by default; `targets.yaml` `secret_ref`
  paths live under it). Pass `mount=` only for a non-default mount.
- **`DEFAULT_BASIC_CREDENTIAL_FIELDS = ("username", "password")`** — the
  basic-credentials field names a vendor session-establish call needs;
  shared so loaders and tests have one source of truth.
- **`VaultCredentialBackend`** — the `CredentialBackend` for kind
  `vault`, registered at import under `"vault"` (and the schemeless
  default). Its `load_secret_data(secret_ref, operator, *, target_name,
  mount)` holds the KV-v2 API-path guard, the operator-context Vault read,
  and the structural unwrap. The loaders dispatch to it; direct callers
  should keep using `load_basic_credentials` / `load_vault_secret_data`.

## Control flow

1. **Reject unset `secret_ref`.** A target with `secret_ref=None` is
   unconfigured → `VaultCredentialsReadError` (`_require_secret_ref`);
   the value is stripped for the scheme split and read.
2. **Split the scheme and dispatch.** `split_credential_ref` resolves the
   ref to `(kind, store_ref)`: a schemeless ref uses the deployment
   default (`config.credentialBackend`, default `vault`), an explicit
   `<kind>:` prefix selects that kind, and `resolve_credential_backend`
   maps the kind to a `CredentialBackend` (`UnknownCredentialBackendError`
   on an unregistered kind). Everything below runs inside the resolved
   backend; for the Vault backend:
3. **Fail closed on empty JWT (Vault backend only, #2642).** If
   `operator.raw_jwt` is empty — a system-initiated call: topology
   scheduler, readiness probe, the runbook verify dispatch's synthetic
   operator (`runbooks/run_service.py::_build_operator_for_dispatch`), or
   the sensor check-runner with no principal configured — raise
   `VaultCredentialsReadError` before any Vault network round-trip. Vault's
   only auth model here is the operator's JWT, so such a call must error
   rather than silently fall back to a backplane identity (the decision's
   system-call carve-out). Synthetic operators pointed at Vault must carry
   `raw_jwt=""` — a non-empty *invalid* placeholder would sail past this
   guard and forward garbage to Vault's JWT/OIDC login (a live round-trip
   before rejection); a genuine service-principal token (#2642) is a
   different thing and is exactly what the guard is meant to admit.

   **This guard used to run in the shared loader**, before the scheme was
   even split, which made it fire for *every* backend. That was right for
   Vault and wrong for a store MEHO can read under a deployment identity:
   on a `credentialBackend=gsm` install it rejected the SA-direct read that
   would have worked, so no Sensor could evaluate and the error was
   Vault-named on a deploy running no Vault. Each backend now owns its own
   precondition and raises its own error class.
4. **Reject an API-path-shaped `secret_ref` (Vault backend only).**
   `secret_ref` must be the *logical* KV-v2 path relative to the mount —
   hvac builds the wire URL as `/{mount_point}/data/{path}` and inserts
   the `/data/` segment itself. A value embedding the mount or that
   segment (`secret/data/…`, `kv/data/…`, leading `data/…`)
   double-resolves to a 404, so the guard (`_is_api_path_shaped`) rejects
   it with a `VaultCredentialsReadError` naming the target and the
   logical-path fix — no auto-stripping. The predicate is *specific*: it
   trips only when the first path segment is `data` or the second is
   `data`, so a logical segment legitimately named `data` deeper in the
   path (`targets/data-center-01/host`) stays valid. This is a KV-v2
   wire-format concern, so it stays on the Vault path only.
5. **Read under operator identity.** `async with
   vault_client_for_operator(operator) as client:` performs the JWT/OIDC
   login, then `await asyncio.to_thread(client.secrets.kv.v2.\
   read_secret_version, path=..., mount_point=mount,
   raise_on_deleted_version=False)`. Login-phase failures
   (`VaultUnreachableError` / `VaultRoleDeniedError`) propagate verbatim.
   The per-request Vault token is revoked on context exit.
6. **Structural unwrap.** KV-v2's GET returns `{"data": {"data":
   {<secret kv>}, "metadata": {...}}}`; the secret content is the nested
   `data["data"]` (the same double-unwrap `vault/ops.py:308` performs). A
   malformed payload raises `VaultCredentialsReadError`, not a bare
   `KeyError`. This is the backend's return value — the shared loader
   takes it from here.
7. **Extract fields.** For each name in `fields`, a missing key raises
   `VaultCredentialsReadError` naming the target + the missing field +
   the `store_ref` (the scheme-stripped ref). Present values pass through
   `strip_credential_value` (coerced to `str`, surrounding whitespace
   stripped) so a trailing newline never rides into a vendor auth header /
   token body (#1474).
8. **Log non-secret attribution only.** A single
   `vault_basic_credentials_loaded` structlog event carries `target` /
   `host` / the requested field *names* — never a value. The returned
   dict is ephemeral in-memory state and must not enter any log event,
   `OperationResult`, or durable artifact. The logger is resolved
   per-call (`structlog.get_logger(__name__).info(...)`) rather than from
   a module-level proxy so `structlog.testing.capture_logs` can reach the
   event under the production `cache_logger_on_first_use=True` config —
   same precedent as `meho_backplane.auth.rbac.require_role`.

## Credential-backend dispatch seam

`backend/src/meho_backplane/connectors/_shared/credential_backend.py` is
the pure routing layer the loaders funnel through (#2229, Initiative
#2227). It holds no secret and performs no read — it only maps a
`secret_ref` scheme to the backend that reads.

- **`CredentialBackend`** — a `runtime_checkable typing.Protocol`. A
  backend implements `async load_secret_data(secret_ref, operator, *,
  target_name, mount) -> dict[str, object]`, returning the store secret's
  raw field dict. Field extraction, whitespace normalisation, and the
  no-secret structlog event stay in the shared loader, so a new backend
  only implements the store read. `mount` is a Vault-KV concept threaded
  through the loader's existing `mount=` parameter; a backend with no
  mount concept (GSM, #2230) ignores it.
- **`CREDENTIAL_BACKEND_REGISTRY` + `register_credential_backend(kind,
  backend)`** — kind-string → backend registry, populated at import time
  (`vault_creds` registers `"vault"`). A duplicate kind raises
  `ValueError` (a wiring bug should fail the eager-import pass loudly),
  mirroring `register_secret_endpoint`.
- **`resolve_credential_backend(kind)`** — returns the backend or raises
  **`UnknownCredentialBackendError`** naming the unknown kind and the
  registered kinds. Distinct from `VaultCredentialsReadError` (a config
  error, not a read-phase failure) but with the same actionable posture.
- **`split_credential_ref(secret_ref, *, default_backend)`** — splits a
  ref into `(kind, store_ref)`. A colon is a scheme separator only when
  the segment before it is a bare scheme token (leading letter, no slash)
  and the remainder is non-empty; everything else is schemeless and
  resolves through `default_backend`. A logical KV-v2 path never carries
  such a prefix, so a path with a colon deeper in a segment is never
  mis-split.
- **`DEFAULT_CREDENTIAL_BACKEND = "vault"`** — the schemeless default when
  `config.credentialBackend` / `CREDENTIAL_BACKEND` is unset (mirrored by
  `Settings.credential_backend`, default `vault`). Zero migration: every
  existing install stores bare paths and runs Vault, so nothing changes.

This is the target-credential analogue of
`connectors/secret/endpoints.py`'s `SECRET_ENDPOINT_REGISTRY` (the
`secret.move` broker seam). The two are intentionally separate registries:
the broker moves one opaque value and reports only its SHA-256
(`SecretEndpoint.read_secret` → `SecretMaterial`), while a credential
backend returns a named-field payload a connector session builder
consumes. Same shape, different contract.

Chart wiring landed with the GSM Helm surface (#2231): the configmap
renders `config.credentialBackend` → `CREDENTIAL_BACKEND`, `config.gsmProject`
→ `GSM_PROJECT`, and `config.gsmImpersonateSa` → `GSM_IMPERSONATE_SA`, and
`values.schema.json` requires `vault.address` only when
`credentialBackend: vault` (a `gsm` install passes with a blank Vault
address). The same `credential_backend` setting also drives the
`GET /api/v1/health` federation proof: `vault` keeps the unchanged
`vault.kv.read` dispatch, while `gsm` reads
`gsm:<gsmProject>/meho-test-federation` through this seam
(`api/v1/health.py:_probe_backend_federation`).

## GCP Secret Manager backend (gsm)

`backend/src/meho_backplane/connectors/_shared/gsm_creds.py` registers
`GcpSecretManagerBackend` under kind `gsm` (#2230, Initiative #2227) — the
second backend on the seam, so a GCP-native adopter runs Vault-free. The
`_shared` package `__init__` imports the module so the `register_credential_backend("gsm", ...)`
call runs eagerly, exactly as `vault_creds` does for `vault`.

**Auth model — SA-direct under GKE Workload Identity (Phase 1).** The read
runs under MEHO's *own* identity: the pod's Application Default Credentials
(`google.auth.default()`), which on GKE resolve to the deployment's
Workload Identity service account. A configured deployment-level SA
(`GSM_IMPERSONATE_SA` / `Settings.gsm_impersonate_sa`, empty by default)
wraps that ADC source in `google.auth.impersonated_credentials.Credentials`
targeting the SA — the same ADC + impersonation chain `GcloudConnector`
drives (`gcloud/connector.py:_fetch_token_sync`). No service-account JSON
key ever enters the flow, honouring `constraints/iam.disableServiceAccountKeyCreation`
by never using key material. Per-operator GCP federation (STS token
exchange) is #2232, out of scope here; MEHO's audit still attributes to
the Keycloak `sub` (the policy/audit seam is untouched), so Phase 1 loses
only *GCP-layer* per-operator attribution.

**Ref grammar.** The scheme-stripped store ref is
`<project-id>/<secret-name>[/versions/<version>][#<field>]`:

- bare `proj/secret` — reads the **latest** version, returns the whole
  JSON object as the field dict.
- pinned `proj/secret/versions/5` — reads that exact version.
- `#field` — `proj/secret#password` — returns just `{field: value}`.

Both forms require the decoded payload to be a JSON **object** (the seam
contract returns a named-field dict the loader's `_extract_fields`
consumes, exactly as the Vault backend returns a KV-v2 data dict) — a
non-JSON / non-object payload, or a missing field, raises
`GcpSecretManagerReadError`. That error type is the GSM analogue of
`VaultCredentialsReadError`: distinct and actionable, raised on a
malformed ref, an empty/unresolvable ADC source, access-denied
(`PermissionDenied`), not-found (`NotFound`), or a transport failure —
never a bare `google.api_core` exception, never echoing a value. The
`SecretManagerServiceClient.access_secret_version` call is synchronous, so
it runs off the event loop via `asyncio.to_thread` (the hvac precedent).
The structlog `gsm_secret_accessed` event carries only `target` /
`project` / `secret_name` / resolved `version` / `field` name.

**Per-operator path — Workload Identity Federation (Phase 2, #2232).**
When `GSM_WIF_AUDIENCE` is set, a `gsm:` read runs under the **operator's**
identity instead of MEHO's SA. The backend builds a fresh
`google.auth.identity_pool.Credentials` (external-account) per read whose
subject-token supplier returns `operator.raw_jwt`; google-auth exchanges
that JWT at `https://sts.googleapis.com/v1/token` for a short-lived
federated token against the configured Workload Identity Pool + OIDC
provider, optionally impersonates `GSM_WIF_SERVICE_ACCOUNT` (via the IAM
`generateAccessToken` URL) for the final read, and the token is discarded
when the credential goes out of scope. This restores *GCP-layer*
per-operator attribution — GCP's audit log names the operator — mirroring
the Vault `vault_client_for_operator` JIT contract (`auth/vault.py:198`): a
fresh credential per operation, never cached across requests. Selection is
per-read: WIF configured **and** an operator JWT present ⇒ operator path;
unconfigured ⇒ the Phase-1 SA-direct path, unchanged (no behaviour change
for Phase-1 installs); configured but no JWT ⇒ the #2642 SA-direct fallback
(see **Background dispatch** below).

The settings (`GSM_WIF_*`, rendered into the ConfigMap from the
`gsm.workloadIdentityFederation.*` chart keys since #2642 — they were
declared-but-unrendered stubs before, so a WIF install had to reach for
`extraEnv`):

- `GSM_WIF_AUDIENCE` — the full WIF provider resource name google-auth
  consumes, `//iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>`.
  Non-empty ⇒ WIF enabled (the selection predicate).
- `GSM_WIF_POOL_ID` / `GSM_WIF_PROVIDER_ID` — the operator-facing
  `gsm.workloadIdentityFederation.{poolId,providerId}` chart keys. Checked
  for consistency against the audience (a copy-paste mismatch fails the
  read closed) and logged for non-secret attribution.
- `GSM_WIF_SERVICE_ACCOUNT` — optional SA to impersonate; empty ⇒ no
  impersonation.
- `GSM_WIF_SUBJECT_TOKEN_TYPE` — STS subject-token type; defaults to
  `urn:ietf:params:oauth:token-type:jwt` (Keycloak OIDC JWT).

**GCP-side prerequisite.** The Workload Identity Pool + OIDC provider must
be created out-of-band and configured to **trust the MEHO Keycloak
issuer** (the provider's issuer URI = the Keycloak realm issuer, audience =
the MEHO client). The impersonated SA (when set) must grant the pool
principal `roles/iam.workloadIdentityUser`, and the SA (or the pool
principal directly) must hold `roles/secretmanager.secretAccessor` on the
secret. MEHO never creates key material — the federation is keyless.

**Background dispatch (#2642).** A system-initiated call with `raw_jwt=""`
has no JWT to exchange. It used to fail closed in the shared loader before
dispatch; now `_select_auth_path` decides per read:

| WIF configured | Operator JWT | Path | `auth_path` label |
|---|---|---|---|
| no | either | Phase-1 SA-direct ADC | `sa_direct` |
| yes | non-empty | per-operator WIF exchange | `wif` |
| yes | empty | SA-direct fallback under MEHO's own ADC | `sa_direct_fallback` |

**Which callers this relaxation covers.** Moving the precondition off the
shared loader was not scoped to the check-runner. The old guard fired for
*any* `operator.raw_jwt == ""`, so on a `credentialBackend=gsm` install —
including a plain Phase-1 SA-direct one, where `_select_auth_path` returns
`sa_direct` and the read simply succeeds — every one of these now resolves
per-target vendor credentials where it previously failed closed:

- the sensor check-runner (`checks/runner.py`), the motivating case;
- the topology-refresh scheduler's `_system_operator`
  (`topology/scheduler.py`);
- `runbooks/run_service.py::_build_operator_for_dispatch`'s operator, which
  is reconstructed from `operator_sub` and **not validated from a bearer
  token**;
- the legacy `execute()` shims in ~15 connectors (`postgres`, `keycloak`,
  `rabbitmq`, `loki`, `nsx`, …), whose own docstrings still assert they
  "fail closed in the credential loader" — untrue on GSM since #2642.

The `/api/v1/health` and `/ready` probes are **not** on that list. They
exercise backend reachability (Vault / Keycloak / migration state /
broadcast / docs) and the `secret/meho/test/federation` federation proof,
which the calling operator's own JWT authorises — no path through them
resolves a per-target `secret_ref`, so the guard that moved never fired for
them in the first place.

The credential is read under MEHO's own ADC — the same identity a Phase-1
install already uses for *every* read — so no GCP privilege boundary moves,
and the fallback is labelled distinctly so an audit can tell a read GCP
attributed to the operator from one attributed to MEHO. The point is that
**the "system-initiated calls cannot read per-target vendor credentials"
carve-out is now Vault-only.** On GSM it no longer holds for any of the
callers above.

**Connector `probe()` / `fingerprint()` are not in that set.** They run
under `synthesise_system_operator()`, whose `raw_jwt` is a deliberately
non-empty placeholder (G3.10). `_select_auth_path` tests truthiness only, so
on a per-operator-WIF install those paths take the `wif` branch and federate
the placeholder at `sts.googleapis.com`, which rejects it — the read fails
with `GcpSecretManagerReadError` and the connector degrades to
`reachable=False` / `auth_failed`. The placeholder is a fixed non-secret
sentinel, so the failed exchange leaks nothing; but row 3 of the table above
does not rescue credentialed-target probes, on GKE or on-prem. Making it do
so means teaching `_select_auth_path` to treat the placeholder as absent
(`system_operator.is_system_operator` already exists for it) — a behaviour
change with its own blast radius, tracked separately.

The fallback needs an ambient GCP identity (GKE Workload Identity, a mounted
SA), which an on-prem cluster does not have. That deployment class instead
configures the **check-runner service principal**
(`CHECK_RUNNER_CLIENT_ID` / `CHECK_RUNNER_CLIENT_SECRET`, chart
`checkRunner.*`, `backend/src/meho_backplane/auth/runner_identity.py`): the
runner mints a Keycloak `client_credentials` token and the ordinary WIF
exchange runs with it as the subject token, so GCP attributes scheduled
reads to that principal. With neither, the read fails closed with an error
naming both remedies. MEHO's own audit attribution is unchanged on every
path (the audit row carries the sensor's / operator's `sub`, never the
runner principal).

**The check-runner principal is not GSM-only, and on Vault it widens
privilege.** `checks/runner.py` mints the token whenever
`CHECK_RUNNER_CLIENT_ID` / `CHECK_RUNNER_CLIENT_SECRET` are set, whatever
`CREDENTIAL_BACKEND` says, so on a Vault install the synthetic operator
stops carrying `raw_jwt=""` and `VaultCredentialBackend.load_secret_data`'s
precondition no longer fires. Whether the read then succeeds is Vault-side
configuration — and with the role this project documents
(`docs/cross-repo/vault-provisioning.md`: `role_type=jwt user_claim=sub
bound_audiences=<keycloak-audience>`, no `bound_subject`, no `bound_claims`,
policy `read` on all of `secret/data/meho/*`) it succeeds. `check_runner_jwt()`
requests `audience=settings.keycloak_audience`, and the realm recipe in
`docs/deploying.md` has the operator add the matching audience mapper, so
Vault accepts the runner principal against the **existing** `meho-mcp` role
with no further provisioning and hands it the full policy. Enabling
`checkRunner.*` on a Vault deploy therefore removes the "system-initiated
calls cannot perform an operator-context Vault read" carve-out for *all*
background dispatch, not just for Sensors. Operators are told to bound the
role first — a distinct audience plus a dedicated narrower role, or an
**exact-match** `bound_claims` on `meho-mcp` keyed on a claim value only
operator tokens carry (a `bound_claims_type=glob` `"*"` is not one: it
matches any present value, including the runner service account's
`preferred_username`) — in
`docs/cross-repo/vault-provisioning.md` § "Bounding the check-runner
principal", in the `checkRunner` block of `deploy/charts/meho/values.yaml`,
and in a `helm install` NOTES warning the chart prints whenever
`checkRunner.enabled` meets `config.credentialBackend: vault`.

**Error hierarchy (#2642).** `credential_backend.CredentialsReadError` is
the backend-neutral base; `VaultCredentialsReadError` and
`GcpSecretManagerReadError` both subclass it. The dispatcher renders a
handler exception as `connector_error: <class name>`, so a Vault-named class
on a GSM deploy is an actively misleading diagnostic. Connector probe /
fingerprint paths that mean "the credential could not be read" catch the
**base** — catching only `VaultCredentialsReadError` let a GSM read error
escape a handler that was supposed to degrade to `auth_failed`. Widened in
#2642: bind9, github, holodeck, keycloak, mongodb, pfsense, postgres,
prometheus, proxmox, rabbitmq, rke2. Sites that *raise* a credential error
of their own (`_require_secret_ref`, the connector cache fast-path guards)
still raise the Vault-named class on every backend — an open inconsistency,
not something #2642 changed.

**Test seams.** The backend takes injectable `adc_loader` (replaces
`google.auth.default`), `client_factory` (replaces
`SecretManagerServiceClient`), and `wif_credentials_factory` (replaces the
real `identity_pool.Credentials` builder) so the unit suite drives parse /
decode / impersonation / WIF-selection / STS-exchange / error /
no-secret-in-logs behaviour with a canned payload and a mocked STS endpoint
— no live GCP (`tests/test_connectors_gsm_creds.py`).

## Dependencies

- **`hvac`** (2.4.0 resolved) — `client.secrets.kv.v2.read_secret_version`
  (signature `(path, version=None, mount_point="secret",
  raise_on_deleted_version=None)`). Synchronous; wrapped in
  `asyncio.to_thread`.
- **`meho_backplane.auth.vault.vault_client_for_operator`** — the
  JWT/OIDC login context manager; the proven operator-context Vault read
  primitive (`auth/vault.py:198`).
- **`meho_backplane.auth.operator.Operator`** — the frozen request-scoped
  operator whose `raw_jwt` is forwarded to Vault.
- **`structlog`** — the `vault_basic_credentials_loaded` event.
- **`google-cloud-secret-manager`** (2.29.0 resolved) — the `gsm` backend's
  `SecretManagerServiceClient.access_secret_version(name=...)` →
  `response.payload.data` (bytes). Synchronous; wrapped in
  `asyncio.to_thread`.
- **`google-auth`** (2.55.1 resolved) — `google.auth.default()` for the ADC
  source and `google.auth.impersonated_credentials.Credentials` for the
  optional SA-direct impersonation the `gsm` backend reuses from
  `GcloudConnector`. The WIF path (#2232) uses
  `google.auth.identity_pool.Credentials` (external-account) with a
  duck-typed `subject_token_supplier` returning `operator.raw_jwt`,
  `service_account_impersonation_url` for the optional target-SA
  impersonation, and `token_url=https://sts.googleapis.com/v1/token`.

## Known issues

- The `secret_ref` is read under a single `mount` (default `"secret"`).
  A non-default mount is passed via `mount=`, not embedded in the ref
  string. An API-path-shaped ref (mount or `/data/` segment embedded, e.g.
  `kv/data/...`) is **rejected** by the shape guard (step 3) rather than
  silently double-resolving — `secret_ref` is path-only and logical, per
  the consumer convention. Registration-time validation (a Pydantic
  validator on `Target.secret_ref` that fails even earlier) is out of
  scope for the read-path guard and can be filed separately (#989 scope).
- A kubeconfig variant / generic `read_secret_fields` is **out of scope**
  — k8s (#G3.10-T4) returns a kubeconfig dict and has its own parse;
  this helper stays basic-credentials-shaped.
- Dynamic secrets, rotation, and response-wrapping are out of scope. A
  dynamic-secret backend would be a *different loader*, not a different
  call site (research doc §5).
- Since #2642 the loader reads `Settings` (to resolve the deployment's
  default backend kind) **before** any backend's empty-`raw_jwt` guard
  fires. That is unobservable in a running backplane — the chassis env is
  required at startup — but a unit test driving the fail-closed path with
  no chassis env now needs to pin `KEYCLOAK_ISSUER_URL` /
  `KEYCLOAK_AUDIENCE` (the six `tests/test_connectors_*_auth.py` suites
  carry a `_chassis_settings_env` fixture for exactly this).
- `_require_secret_ref` still raises `VaultCredentialsReadError` for an
  unset `secret_ref` on **every** backend: it is a shared-loader error
  raised before the scheme is split, so there is no backend to name it
  after. Worth normalising onto `CredentialsReadError` if the seam grows a
  third backend; it is not the failure #2642 addressed (that one is a
  credential *read*, this one is an unconfigured target).

## Testing

- **Unit** (`backend/tests/test_connectors_vault_creds.py`) — secret-free,
  runs in the always-on unit lane. Uses the in-process Vault fake
  (`tests/_vault_fakes.install_fake_client`, which patches
  `auth.vault._build_client`) so `vault_client_for_operator` runs its
  real login path against the fake. Covers fail-closed, missing
  `secret_ref`, the API-path-shape guard (parametrized reject **and**
  accept cases — `secret/data/foo` / `kv/data/foo` / `data/foo` rejected,
  `targets/data-center-01/host` / `vsphere/vcenter-a` accepted), missing
  field (asserts not a bare `KeyError`), malformed payload, login-error
  propagation, and no-secret-in-logs via `capture_logs`.
- **Live** (`backend/tests/integration/test_connectors_vault_creds_dev_e2e.py`)
  — the rubric State-2 bar. Boots a real `hashicorp/vault:1.18` dev-mode
  container via testcontainers, seeds a KV-v2 secret, monkeypatches
  `vault_client_for_operator` to yield a root-token client at the
  container (dev mode has no OIDC method), and exercises the full helper
  code path against the live store. Lives under `tests/integration/`, so
  the unit CI lane deselects it (`pytest --ignore=tests/integration`) and
  the integration lane runs it (`pytest -x tests/integration/`); a
  Docker-absent sandbox skips cleanly. Image overridable via
  `MEHO_TEST_VAULT_IMAGE`.
- **Background dispatch** (`backend/tests/test_connectors_gsm_creds.py` +
  `backend/tests/test_sensor_runner.py`, #2642) — the SA-direct fallback,
  the backend-neutral error class, and a real check-runner tick driven
  through resolve → dispatch → credential load with a stubbed STS exchange
  (the `wif_credentials_factory` seam) and a stubbed Keycloak token
  endpoint (respx), asserting the presented subject token is the runner
  principal's JWT. The principal itself is covered by
  `backend/tests/test_auth_runner_identity.py` (opt-in, caching,
  fail-soft, no secret in logs).

## References

- Dispatch seam Task: https://github.com/evoila/meho/issues/2229
- Pluggable-backend Initiative: https://github.com/evoila/meho/issues/2227
- Prior-art seam: `backend/src/meho_backplane/connectors/secret/endpoints.py`
  (`SECRET_ENDPOINT_REGISTRY`, the `secret.move` broker).
- Task: https://github.com/evoila/meho/issues/941
- Credential whitespace strip: https://github.com/evoila/meho/issues/1474
- Shape guard: https://github.com/evoila/meho/issues/989
- Parent Initiative: https://github.com/evoila/meho/issues/939
- Parent Goal: https://github.com/evoila/meho/issues/214
- Decision: `docs/architecture/connector-auth.md` (Option A,
  operator-context).
- Research: `docs/research/214-connector-credential-broker.md` §2 (KV-v2),
  §6 (no-secret-in-logs), §7 (testing without real secrets).
- Lift source: `backend/src/meho_backplane/connectors/vault/ops.py`
  L284-312 (the `vault_client_for_operator` + KV-v2 read + structural
  unwrap).
- Primitive: `backend/src/meho_backplane/auth/vault.py` L198
  (`vault_client_for_operator`), error classes L79-107.
