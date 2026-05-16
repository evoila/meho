# Connector: vault (HashiCorp Vault 1.x)

## Overview

The `vault` connector is a **typed** connector: operations are
hand-coded Python handlers against the `hvac` SDK, registered into the
G0.6 `endpoint_descriptor` table via `register_typed_operation()` at
lifespan startup. It dispatches under the
`(product="vault", version="1.x", impl_id="vault")` registry triple.

Vault publishes an OpenAPI spec, but the shipped chassis already used
hand-coded HTTP against Vault's REST (G0.2-T5 #244), so Vault stays
typed per the minimum-disruption decision in Initiative #366. Switching
to generic-ingested is a v0.2.next consideration if Vault's surface
grows substantially.

Auth model: `shared_service_account`. Every operator's Keycloak JWT is
forwarded to Vault's JWT/OIDC auth method (bound to the `meho-mcp`
role); the resulting per-request Vault token is revoked on context
exit. The unauthenticated `/sys/health` endpoint is the one exception —
it needs no token.

Source: `backend/src/meho_backplane/connectors/vault/`.

## Key types

- **`VaultConnector`** (`connector.py`) — `Connector` subclass.
  `product="vault"`, `version="1.x"`, `impl_id="vault"`. Its
  `fingerprint` and `probe` methods read `GET /v1/sys/health` through
  the shared `auth.vault` seams. `execute` is a thin shim that
  delegates to the G0.6 dispatcher (parameter validation, policy gate,
  audit, broadcast, JSONFlux) so pre-G0.6 callers keep working.
- **`VaultTarget`** (`connector.py`) — pre-G0.3 stand-in for the
  `Target` model; carries only `raw_jwt`.
- **KV-v2 op group** (`ops.py`) — `register_vault_typed_operations()`
  registers the `vault.kv.*` surface (G0.6-T-Refactor shipped
  `vault.kv.read`; G3.3-T1 #545 adds list/put/versions/delete). Group
  key `kv`. `vault.kv.read` / `vault.kv.list` are classified
  `credential_read` (decision #3 — aggregate-only broadcast).
- **`sys` read op group** (`ops_sys.py`, G3.3-T2 #546) —
  `register_vault_sys_typed_operations()` registers four read-only
  diagnostics ops under group key `sys`, all `safety_level='safe'`,
  `requires_approval=False`:
  - `vault.sys.health` — `GET /v1/sys/health`. **Shares** the
    probe-path implementation: calls `auth.vault._build_client`
    (unauthenticated, settings-driven), `_to_thread_read_health`, and
    `_classify_health_response` — the same three seams
    `VaultConnector.probe`/`fingerprint` use. No JWT needed; the
    `target` arg is accepted to satisfy the dispatcher's typed-handler
    signature but is unused.
  - `vault.sys.seal_status` — `GET /v1/sys/seal-status`. Returns the
    raw seal-status object.
  - `vault.sys.mounts.list` — `GET /v1/sys/mounts`. Returns the
    envelope's `data` under a `mounts` key.
  - `vault.sys.auth.list` — `GET /v1/sys/auth`. Returns the envelope's
    `data` under an `auth_methods` key.

  The three authenticated ops forward the operator JWT via
  `vault_client_for_operator` and offload the blocking `hvac` call with
  `asyncio.to_thread`.
- **`auth` read op group** (`ops_auth.py`, G3.3-T3 #547) —
  `register_vault_auth_operations()` registers `vault.auth.userpass.list`
  / `vault.auth.userpass.read` / `vault.auth.approle.list` /
  `vault.auth.approle.read` under group key `auth`, all
  `safety_level='safe'`. Mount path is parameterised (defaults
  `userpass` / `approle`); a not-mounted backend raises
  `VaultAuthBackendNotMountedError`. AppRole secret-id generation is
  deliberately out of scope (v0.2.next, behind a policy gate). It is
  registered from its own module via the
  `register_vault_auth_operations()` call at the end of
  `register_vault_typed_operations()`.

## KV-v2 op group

| op_id | hvac call | safety_level | op_class (broadcast) |
|---|---|---|---|
| `vault.kv.read` | `read_secret_version` | safe | `credential_read` |
| `vault.kv.list` | `list_secrets` | safe | `credential_read` |
| `vault.kv.versions` | `read_secret_metadata` | safe | `read` |
| `vault.kv.put` | `create_or_update_secret` | caution | `write` |
| `vault.kv.delete` | `delete_secret_versions` | dangerous | `write` |

All five register into operation group `kv`.

**Mount handling.** Every handler accepts an optional `mount` param
(JSON Schema default `"secret"`, mirroring hvac's `mount_point`
default). The pre-existing `vault.kv.read` `path`-only call sites keep
working; the consumer wrappers pass `<mount> <path>` explicitly for
non-default mounts. The mount pattern rejects whitespace-only and
slash-bearing input at param validation (`^(?=.*\S)[^/]+$`), so a bad
mount is an `invalid_params` failure rather than a runtime
`connector_error` (G3.3-T1 review B1/M1).

**Two-phase failure model.** Login-side failures (Vault unreachable,
role denied) raise `VaultClientError` subclasses; read/write-side
failures (KV miss, malformed payload, CAS mismatch, permission
denied) raise the underlying exception. Callers that need the
distinction (the `/api/v1/health` route) string-match
`extras["exception_class"]` against the known `VaultClientError`
subclass names. Structural unwrap of the hvac payload raises
`KeyError` on a malformed envelope so the dispatcher reports a
structured error rather than an unhandled exception.

## Broadcast PII discipline (decision #3)

The G6 broadcast publisher emits an aggregate-only payload for
`credential_read` and `audit_query` ops. The sensitivity class is
derived **from the op-id**, not a per-descriptor field:
`broadcast.events.classify_op` consults the `_CREDENTIAL_READ_OPS`
allowlist (currently `{vault.kv.read, vault.kv.list}`) and the
`_WRITE_SUFFIXES` / `_READ_SUFFIXES` tuples. The shipped G0.6
`endpoint_descriptor` table has **no `op_class` column** — decision #3
locks the classifier on the op-id, so the register-time signal is the
op-id itself. `vault.kv.versions` reads only version metadata (never
secret values) and is deliberately a plain `read`, not
`credential_read`. The `.put` / `.versions` suffixes were added to the
write / read suffix tuples by #545 — without that, `vault.kv.put`
would have classified `other` and broadcast the written secret to
every operator (the credential-leak fix).

## Approval gating

`vault.kv.put` (`caution`) and `vault.kv.delete` (`dangerous`)
register with `requires_approval=False` — the dev default.
`requires_approval` is a static boolean on the descriptor; the shipped
G0.6 substrate has no per-path approval predicate. The
production-path approval gate is G7/G10 policy territory (see the
`EndpointDescriptor` model docstring: `caution`/`dangerous` ops "flow
through G7 / G10 policy logic once those Goals land"). `safety_level`
is the load-bearing signal that future gate keys on.

## Control flow

1. Importing `connectors.vault` (package `__init__.py`) registers
   `VaultConnector` against the v2 registry synchronously and queues
   two typed-op registrars (`register_vault_typed_operations`,
   `register_vault_sys_typed_operations`) onto the lifespan-driven
   registrar list. `register_vault_typed_operations` registers the
   KV-v2 group and then calls `register_vault_auth_operations` for the
   `auth` group.
2. At FastAPI lifespan startup, `run_typed_op_registrars()` invokes
   both registrars, which upsert the `endpoint_descriptor` rows. Upsert
   is idempotent: a restart against unchanged descriptions is a no-op
   for the embedding pipeline (body-hash skip in
   `register_typed_operation`).
3. A `call_operation` (CLI or MCP) dispatches via
   `meho_backplane.operations.dispatch`, which validates params against
   the registered `parameter_schema`, applies the policy gate, invokes
   the typed handler, writes the synchronous audit row, publishes the
   broadcast event, and reduces the result (JSONFlux).
4. `op_class` for audit/broadcast is **derived from the op-id suffix**
   by `broadcast.events.classify_op`, not passed at registration. The
   four `sys` ops classify as `read` — `.list` was already a read
   suffix; `.health` and `.seal_status` were added to the read-suffix
   tuple by #546 (no secret content, so they broadcast at the same
   sensitivity as `.list`/`.get` rather than falling through to the
   full-detail `other` class).

## Error handling

Typed handlers **raise** on failure rather than returning a structured
result. The dispatcher's `connector_error` branch turns the raised
exception into an `OperationResult(status="error")` with the exception
class name in `extras["exception_class"]`. Callers (e.g. the
`/api/v1/health` route) string-match that class name to distinguish
login-phase failure (any `VaultClientError` subclass:
`VaultUnreachableError`, `VaultRoleDeniedError`) from read-phase
failure (anything else). An unreachable or sealed Vault therefore never
surfaces a raw traceback to the agent.

## Dependencies

- `hvac` — synchronous Vault SDK (built on `requests`); every call is
  wrapped in `asyncio.to_thread` to keep the event loop responsive.
- `meho_backplane.auth.vault` — the OIDC forward-auth middle link;
  owns `_build_client`, `vault_client_for_operator`,
  `_to_thread_read_health`, `_classify_health_response`. The
  `_build_client` function is the single test seam (monkeypatched by
  `tests/_vault_fakes.py`).
- `meho_backplane.operations.typed_register` — `register_typed_operation`
  / `register_typed_op_registrar`.
- `meho_backplane.broadcast.events` — `classify_op` (op-id → op_class).

## Known issues

- `VaultTarget` is a pre-G0.3 placeholder; replace with the real
  `Target` model once G0.3 (#224) connection-parameter resolution
  lands. Connection params (address/namespace/timeout) currently come
  from `get_settings()`, not the target.
- `vault.sys.seal_status` is unauthenticated on Vault's side but is
  routed through `vault_client_for_operator` for uniform per-operator
  audit attribution — at the cost of one extra OIDC login + revoke per
  call. Acceptable under v0.2 dogfood load (per-request login is
  already the v0.1/v0.2 model); revisit if a per-operator token cache
  lands.
- `sys` writes (unseal, mount/unmount, policy write) are deliberately
  out of scope for v0.2.
- AppRole secret-id generation is out of scope for v0.2 (high-risk
  write with policy implications; v0.2.next behind a policy gate).

## References

- Initiative #366 (G3.3 vault-1.x typed op surface); Goal #214.
- Tasks: #545 (G3.3-T1 KV-v2), #546 (G3.3-T2 sys read group),
  #547 (G3.3-T3 auth read group).
- Substrate: #388 (G0.6 operation registry), #390 (Refactor-Vault).
- Vault API: https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2
  (KV-v2), https://developer.hashicorp.com/vault/api-docs/system (sys),
  https://developer.hashicorp.com/vault/api-docs/auth/userpass
  (userpass), https://developer.hashicorp.com/vault/api-docs/auth/approle
  (approle).
- Decision #3 PII redaction: `docs/planning/v0.2-decisions.md`.
- CLAUDE.md postulates 1 (typed connectors first-class) and 7
  (synchronous append-only audit).
