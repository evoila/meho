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
read, byte-for-byte. An unknown scheme (`gsm:` before #2230 registers the
GSM backend) raises `UnknownCredentialBackendError` instead of a silent
Vault attempt. The seam is the target-credential analogue of the
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
- **`VaultCredentialsReadError`** — read-phase failure (empty JWT, unset
  `secret_ref`, malformed payload, missing field). Deliberately distinct
  from `auth.vault.VaultClientError` (login-phase: Vault unreachable,
  role denied), so a caller can render an operator-actionable detail
  string per phase. A missing field never surfaces as a bare `KeyError`.
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

1. **Fail closed on empty JWT.** If `operator.raw_jwt` is empty (a
   system-initiated call — topology scheduler, readiness probe, the
   runbook verify dispatch's synthetic operator built by
   `runbooks/run_service.py::_build_operator_for_dispatch`), raise
   `VaultCredentialsReadError` *before* touching Vault — and before the
   scheme is split or `get_settings()` is read (`_resolve_and_load`). The
   decision's system-call carve-out: such calls cannot perform an
   operator-context read and must error, never silently fall back to a
   backplane identity. This operator-context precondition lives in the
   shared dispatch, not in a backend, so it fires before any settings /
   store access exactly as before the seam; it reflects the
   operator-context model of today's only backend (Vault), and a future
   deployment-identity backend (GSM SA-direct, #2230) would relax it.
   Synthetic operators must carry `raw_jwt=""` — a non-empty placeholder
   would sail past this guard and forward an invalid string to Vault's
   JWT/OIDC login (a live network round-trip before rejection).
2. **Reject unset `secret_ref`.** A target with `secret_ref=None` is
   unconfigured → `VaultCredentialsReadError` (`_require_secret_ref`);
   the value is stripped for the scheme split and read.
3. **Split the scheme and dispatch.** `split_credential_ref` resolves the
   ref to `(kind, store_ref)`: a schemeless ref uses the deployment
   default (`config.credentialBackend`, default `vault`), an explicit
   `<kind>:` prefix selects that kind, and `resolve_credential_backend`
   maps the kind to a `CredentialBackend` (`UnknownCredentialBackendError`
   on an unregistered kind). Everything below runs inside the resolved
   backend; for the Vault backend:
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

Chart wiring (`config.credentialBackend` → `CREDENTIAL_BACKEND` in the
Helm configmap) lands with the GSM Helm surface in #2231; the setting is
already read here so a value flows the moment the chart exposes it.

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
