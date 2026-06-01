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
- **Operator-aware JWT (G0.8-T3 #629)** — the operator's bearer token
  is request-scoped state on the `Operator` the dispatcher threads to
  handlers whose signature names an `operator` parameter
  (`operations/_branches.py::dispatch_typed`). The Vault handlers read
  `operator.raw_jwt`; the token is **never** read off the persisted
  `Target` (a per-request bearer must not live on a shared, persisted
  target row). The pre-G0.3 `VaultTarget` stub that carried `raw_jwt`
  was deleted by #629; `probe`/`fingerprint`/`execute` are typed
  against the real `targets.schemas.Target | None`.
- **KV-v2 op group** (`ops.py`) — `register_vault_typed_operations()`
  registers the `vault.kv.*` surface (G0.6-T-Refactor shipped
  `vault.kv.read`; G3.3-T1 #545 adds list/put/versions/delete). Group
  key `kv`. `vault.kv.read` / `vault.kv.list` are classified
  `credential_read` (decision #3 — aggregate-only broadcast).
- **`sys` op group** (`ops_sys.py`, G3.3-T2 #546; policy ops
  G3.15-T2 #1410) — `register_vault_sys_typed_operations()` registers
  four read-only diagnostics ops plus the four ACL-policy ops under
  group key `sys`. The diagnostics ops are all `safety_level='safe'`,
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

  The ACL-policy ops (`ops_sys_policy.py` — handlers/schemas split out
  to keep `ops_sys.py` under the 600-line file budget; registered from
  the same `register_vault_sys_typed_operations()` so the whole `sys`
  group lands from one place):

  | op_id | hvac call | safety_level | requires_approval | op_class |
  |---|---|---|---|---|
  | `vault.sys.policy.read` | `sys.read_policy` | safe | False | `other` |
  | `vault.sys.policy.list` | `sys.list_policies` | safe | False | `read` |
  | `vault.sys.policy.write` | `sys.create_or_update_policy` | dangerous | True | `write` |
  | `vault.sys.policy.delete` | `sys.delete_policy` | dangerous | True | `write` |

  All four forward the operator JWT via `vault_client_for_operator` and
  offload the sync `hvac` call with `asyncio.to_thread`. `policy.read`
  unwraps the `data.rules` (or `data.policy`) envelope, falling back to
  the legacy top-level keys → `{name, rules}` (rules `None` when
  absent). `policy.list` returns `{policies: [...]}` from the
  envelope's `data.policies`. `policy.write` / `policy.delete` return
  Vault's HTTP 204 with no body, so the handlers synthesize
  `{name, written: true}` / `{name, deleted: true}` (reaching-here =
  success; hvac raises on non-2xx). The shared `name` param uses
  `pattern="^(?=.*\S)[^/]+$"` (the KV-v2 `mount` fragment's shape): a
  blank name fails validation rather than degrading to a runtime error,
  and a slash-bearing name is rejected. `policy.write`'s `policy` param
  is a non-empty HCL/JSON body that **replaces** the policy in full (not
  a merge); the verb layer / agent owns any body linting — this op is a
  thin pass-through and lets Vault reject malformed HCL. `policy.read`
  classifies `other` (its only param is the policy name; `.read` is not
  a read-suffix — see "Broadcast PII discipline"), `policy.list` `read`
  via the `.list` suffix, and `policy.write` / `policy.delete` `write`
  (the `.write` / `.delete` suffixes redact the HCL body).
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
| `vault.kv.put` | `create_or_update_secret` | caution | `credential_write` |
| `vault.kv.delete` | `delete_secret_versions` | dangerous | `write` |

All five register into operation group `kv`.

**Mount handling.** Every handler — including `vault.kv.read` —
accepts an optional `mount` param (JSON Schema default `"secret"`,
mirroring hvac's `mount_point` default) forwarded as hvac's
`mount_point`. The pre-existing `vault.kv.read` `path`-only call sites
keep working on the default; the consumer wrappers pass
`<mount> <path>` explicitly for non-default mounts (the Initiative
#366 goal — retiring `scripts/_secret-read.sh`, which derived the
mount from the path's first segment). The shared `mount` schema
fragment uses `pattern="^(?=.*\S)[^/]+$"`: the `(?=.*\S)` lookahead
makes an all-whitespace value a validation-time `invalid_params`
failure rather than a value that `.strip()`s to an empty mount and
degrades to a runtime `connector_error`; `[^/]+` rejects a
slash-bearing value (`"secret/data"`) where hvac expects the bare
mount handle.

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
allowlist (currently `{vault.kv.read, vault.kv.list}`), the
`_CREDENTIAL_WRITE_OPS` allowlist (G11.7-T1 #1401 — `{vault.kv.put,
vault.auth.userpass.write, vault.auth.userpass.update_password,
k8s.secret.create}`), and the `_WRITE_SUFFIXES` / `_READ_SUFFIXES`
tuples. The shipped G0.6 `endpoint_descriptor` table has **no
`op_class` column** — decision #3 locks the classifier on the op-id, so
the register-time signal is the op-id itself. `vault.kv.versions` reads
only version metadata (never secret values) and is deliberately a plain
`read`, not `credential_read`.

`vault.kv.put` classifies `credential_write` (G11.7-T1 #1401): the
secret `data` is in the *request params*, and the broadcast ships
params, so it must collapse to aggregate-only. It previously
classified plain `write` via the `.put` write-suffix (#545), which kept
it out of the `other` class but still broadcast the written secret in
full — the `credential_write` reclassification closes that. Response-side
secret-mint ops (`vault.token.create`,
`vault.auth.approle.generate_secret_id`) classify `credential_mint`
alongside `harbor.robot.create`. See `docs/codebase/broadcast.md` for
the full sensitivity taxonomy.

`vault.sys.policy.write` (G3.15-T2 #1410) classifies `write` via the
`.write` write-suffix (added to `_WRITE_SUFFIXES` for this op): the HCL
policy body is in the request params, so without the suffix it would
fall through to `other` and broadcast the policy text in full. The
`_CREDENTIAL_WRITE_OPS` allowlist is consulted first, so the
`.write`-shaped `vault.auth.userpass.write` keeps its `credential_write`
class. `vault.sys.policy.delete` is `write` via `.delete`;
`vault.sys.policy.list` is `read` via `.list`. `vault.sys.policy.read`
classifies `other` — `.read` is deliberately **not** a read-suffix (it
would over-match the `credential_read`-allowlisted `vault.kv.read`), and
the policy-read param is only the policy name, so the `other` full-param
broadcast is the consistent, safe direction (same rationale as the
`vault.auth.*.read` auth-config ops).

## Approval gating

`vault.kv.put` (`caution`) and `vault.kv.delete` (`dangerous`)
register with `requires_approval=False` — the dev default.
`requires_approval` is a static boolean on the descriptor consulted by
the G11.7-T1 (#1401) policy gate: an op with `requires_approval=True`
is routed to the approval queue (verdict `NEEDS_APPROVAL`) and the
dispatch returns `status="awaiting_approval"` with an
`approval_request_id` in `extras` — the handler does **not** run until
a human approves. `safety_level` is the load-bearing severity signal
the gate and the UI surface key on.

`vault.sys.policy.write` / `vault.sys.policy.delete` (G3.15-T2 #1410)
are the first Vault ops to register `requires_approval=True`
(`safety_level='dangerous'`): a bad HCL body can lock everyone out or
silently widen access, so both are approval-gated. A dispatch by a
human/service principal therefore parks as `awaiting_approval` before
reaching Vault (proven in `test_connectors_vault_sys.py`); the
handler's hvac-forwarding logic is exercised by calling it directly in
the unit suite. The policy reads (`policy.read` / `policy.list`) stay
`requires_approval=False`.

## JSONFlux result-handle path (`vault.kv.list`)

`vault.kv.list` is the only set-shaped op on the v0.2 Vault surface
(it returns `{"keys": [...]}`; every other op returns a bounded scalar
or single-secret dict). Per v0.1-spec §4 / CLAUDE.md postulate 6, a
set larger than the JSONFlux threshold (~50 rows / 4 KB) must come
back as a sample + `ResultHandle`, not the raw list.

The wrapping is the **dispatcher's** job, not the handler's: the
handler returns `{"keys": [...]}` verbatim and `dispatch` passes it
through the configured `Reducer` before audit/broadcast. The default
reducer is the threshold-aware
[`JsonFluxReducer`](../architecture/jsonflux.md) (G0.6.1, #750) —
`vault.kv.list` with ≤50 keys (≤4 KB) passes through inline with
`OperationResult.handle is None`; a larger list returns a sample +
`ResultHandle`. The `result_query` / `result_aggregate` /
`result_describe` / `result_export` meta-tools that read a handle back
ship in a follow-on Initiative.

`tests/test_vault_kv_list_jsonflux.py` (G3.3-T4) pins both halves of
the contract: ≤50 keys stays inline with no handle, and >50 keys
produces `{sample, ...}` on `result` plus a `ResultHandle` whose
`total_rows` / `sample_rows` carry exactly what a future
`result_describe` / `result_query` will read. The agent never sees the
raw >50-key list once a handle is produced.

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

## Integration harness (G3.3-T7, dev-mode CI)

The unit suites mock hvac through `tests/_vault_fakes.py` (the
`_build_client` seam). The real-Vault layer lives in
`backend/tests/integration/test_connectors_vault_dev_e2e.py`: it boots
a `hashicorp/vault:1.18` server in dev mode via testcontainers'
`DockerContainer`, seeds the surfaces every op touches, then
dispatches every registered `vault.kv.*` / `vault.sys.*` /
`vault.auth.*` op through the real `dispatch()` against the live Vault
and a real Postgres audit store (reusing the integration conftest's
`pg_engine` fixture).

- **Container.** Default entrypoint runs `vault server -dev`;
  `VAULT_DEV_ROOT_TOKEN_ID` pins the generated root token,
  `VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200` makes the host-mapped port
  reachable, `IPC_LOCK` is granted (Vault mlocks memory). Image
  overridable via `MEHO_TEST_VAULT_IMAGE` (same env-knob shape as
  `MEHO_TEST_PGVECTOR_IMAGE`); `ci.yml` sets it to the Harbor proxy
  mirror. Docker-socket-absent sandbox skips cleanly — the gate
  matches `tests/integration/conftest.py`.
- **Seed.** Dev mode mounts KV-v2 at `secret/`. The fixture writes a
  plain secret, a twice-written secret (so `vault.kv.versions` sees
  `current_version == 2`), and a `bulk/` folder with > 50 child keys
  (the set-shape fixture G3.3-T4's threshold test also consumes). It
  enables `userpass` + `approle` and seeds one user / one role.
- **Client seam.** Dev mode has no OIDC method, so the test
  monkeypatches `vault_client_for_operator` to yield a root-token
  client bound to the container and pins `VAULT_ADDR` for the
  `_build_client` path (`sys.health` / `sys.seal_status`). Only
  credential acquisition is swapped; the full handler → hvac →
  unwrap → dispatcher → audit → broadcast path runs unchanged.
- **Assertions.** Each op asserts the live response shape, a single
  synchronously-committed `audit_log` row (postulate 7), and the
  broadcast `op_class` — `credential_write` for `kv.put` (G11.7-T1
  #1401) and `write` for `kv.delete`,
  `credential_read` for `kv.read` / `kv.list`, `read` for the
  KV-v2 / sys metadata reads and the `.list` auth ops
  (`auth.userpass.list` / `auth.approle.list`), and `other` for the
  two `.read` auth-config ops (`auth.userpass.read` /
  `auth.approle.read`). The `.read` suffix is deliberately absent
  from `_READ_SUFFIXES` so the suffix check never over-matches the
  `credential_read`-allowlisted `vault.kv.read`; the auth-config
  `.read` ops therefore fall through to `other`, which is the safe
  over-broadcast direction for non-secret auth-method metadata
  (decision #3).
- **Secrets discipline.** The dev-root token is generated into the
  in-memory throwaway container and only ever lives in the fixture's
  return value — never logged, never asserted, never persisted.

## Known issues

- Connection params (address/namespace/timeout) come from
  `get_settings()`, not the target — Vault is a deployment-level
  singleton in v0.2, so `probe`/`fingerprint` accept `Target | None`
  and ignore the value. (The pre-G0.3 `VaultTarget` stub was removed
  by G0.8-T3 #629; the operator JWT now comes from request-scoped
  `Operator` context, not a persisted target field.)
- `vault.sys.seal_status` is unauthenticated on Vault's side but is
  routed through `vault_client_for_operator` for uniform per-operator
  audit attribution — at the cost of one extra OIDC login + revoke per
  call. Acceptable under v0.2 dogfood load (per-request login is
  already the v0.1/v0.2 model); revisit if a per-operator token cache
  lands.
- `sys` policy writes (`policy.write` / `policy.delete`) landed in
  G3.15-T2 (#1410), approval-gated. Other `sys` writes (unseal,
  mount/unmount bootstrap) remain out of scope here (G3.15-T5).
- AppRole secret-id generation is out of scope for v0.2 (high-risk
  write with policy implications; v0.2.next behind a policy gate).

## References

- Initiative #366 (G3.3 vault-1.x typed op surface); Goal #214.
- Tasks: #545 (G3.3-T1 KV-v2), #546 (G3.3-T2 sys read group),
  #547 (G3.3-T3 auth read group), #551 (G3.3-T7 dev-mode CI
  integration harness).
- Vault dev mode: https://developer.hashicorp.com/vault/docs/concepts/dev-server
- Substrate: #388 (G0.6 operation registry), #390 (Refactor-Vault).
- Vault API: https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2
  (KV-v2), https://developer.hashicorp.com/vault/api-docs/system (sys),
  https://developer.hashicorp.com/vault/api-docs/auth/userpass
  (userpass), https://developer.hashicorp.com/vault/api-docs/auth/approle
  (approle).
- Decision #3 PII redaction: `docs/planning/v0.2-decisions.md`.
- CLAUDE.md postulates 1 (typed connectors first-class) and 7
  (synchronous append-only audit).
