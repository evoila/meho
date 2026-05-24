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
  `{field: value}` dict. Values are coerced to `str` so a numeric secret
  field round-trips as the string a vendor Basic-auth header expects.
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

## Control flow

1. **Fail closed on empty JWT.** If `operator.raw_jwt` is empty (a
   system-initiated call — topology scheduler, readiness probe), raise
   `VaultCredentialsReadError` *before* touching Vault. The decision's
   system-call carve-out: such calls cannot perform an operator-context
   read and must error, never silently fall back to a backplane identity.
2. **Reject unset `secret_ref`.** A target with `secret_ref=None` is
   unconfigured → `VaultCredentialsReadError`.
3. **Reject an API-path-shaped `secret_ref`.** `secret_ref` must be the
   *logical* KV-v2 path relative to the mount — hvac builds the wire URL
   as `/{mount_point}/data/{path}` and inserts the `/data/` segment
   itself. A value embedding the mount or that segment (`secret/data/…`,
   `kv/data/…`, leading `data/…`) double-resolves to a 404, so the guard
   (`_is_api_path_shaped`) rejects it with a `VaultCredentialsReadError`
   naming the target and the logical-path fix — no auto-stripping. The
   predicate is *specific*: it trips only when the first path segment is
   `data` or the second is `data`, so a logical segment legitimately
   named `data` deeper in the path (`targets/data-center-01/host`) stays
   valid.
4. **Read under operator identity.** `async with
   vault_client_for_operator(operator) as client:` performs the JWT/OIDC
   login, then `await asyncio.to_thread(client.secrets.kv.v2.\
   read_secret_version, path=..., mount_point=mount,
   raise_on_deleted_version=False)`. Login-phase failures
   (`VaultUnreachableError` / `VaultRoleDeniedError`) propagate verbatim.
   The per-request Vault token is revoked on context exit.
5. **Structural unwrap.** KV-v2's GET returns `{"data": {"data":
   {<secret kv>}, "metadata": {...}}}`; the secret content is the nested
   `data["data"]` (the same double-unwrap `vault/ops.py:308` performs). A
   malformed payload raises `VaultCredentialsReadError`, not a bare
   `KeyError`.
6. **Extract fields.** For each name in `fields`, a missing key raises
   `VaultCredentialsReadError` naming the target + the missing field +
   the `secret_ref`. Present values are coerced to `str`.
7. **Log non-secret attribution only.** A single
   `vault_basic_credentials_loaded` structlog event carries `target` /
   `host` / the requested field *names* — never a value. The returned
   dict is ephemeral in-memory state and must not enter any log event,
   `OperationResult`, or durable artifact. The logger is resolved
   per-call (`structlog.get_logger(__name__).info(...)`) rather than from
   a module-level proxy so `structlog.testing.capture_logs` can reach the
   event under the production `cache_logger_on_first_use=True` config —
   same precedent as `meho_backplane.auth.rbac.require_role`.

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

- Task: https://github.com/evoila/meho/issues/941
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
