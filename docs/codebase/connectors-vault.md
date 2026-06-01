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
  G3.15-T2 #1410; bootstrap ops G3.15-T5 #1413) —
  `register_vault_sys_typed_operations()` registers four read-only
  diagnostics ops, the four ACL-policy ops, and the four bootstrap
  enable/tune ops under group key `sys`. The diagnostics ops are all
  `safety_level='safe'`,
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

  The sys bootstrap ops (`ops_sys_bootstrap.py` — same module-split
  rationale as the policy ops; registered from the same
  `register_vault_sys_typed_operations()`) move the auth-method /
  secret-mount enable + tune operations off the break-glass shell
  wrapper. All four are `requires_approval=True`:

  | op_id | hvac call | safety_level | op_class |
  |---|---|---|---|
  | `vault.sys.auth.enable` | `sys.enable_auth_method` | dangerous | `other` |
  | `vault.sys.auth.tune` | `sys.tune_auth_method` | caution | `other` |
  | `vault.sys.mounts.enable` | `sys.enable_secrets_engine` | dangerous | `other` |
  | `vault.sys.mounts.tune` | `sys.tune_mount_configuration` | caution | `other` |

  Enables are `dangerous` (a new auth method / secret engine widens the
  cluster's credential surface); tunes are `caution` (they reconfigure
  an already-enabled mount — lease TTLs, description, listing
  visibility — without standing up a new path). The `path` param uses
  the policy-name pattern shape (`^(?=.*\S)[^/]+$`): a blank or
  slash-bearing path fails validation. Enables take a required type
  (`method_type` / `backend_type`) plus an optional `description`;
  tunes take only the supplied config knobs (an omitted knob leaves
  Vault's current value untouched). All four forward the operator JWT
  via `vault_client_for_operator` and offload the sync `hvac` call with
  `asyncio.to_thread`.

  **Enable idempotency.** A duplicate `enable` (same type at an
  already-mounted path) is Vault's HTTP 400 "path is already in use".
  The enable handlers unwrap *that one* `hvac.exceptions.InvalidRequest`
  into a `{created: false}` success (matching the connector's
  error-unwrapping posture); every other 400 (unknown type, malformed
  config) re-raises for the dispatcher's `connector_error` branch. Tunes
  are naturally idempotent on Vault's side (a no-op 204) and synthesize
  `{path, tuned: true}`.

  **Broadcast classification.** All four classify `other` — `.enable` /
  `.tune` are deliberately **not** added to the classifier's
  write-suffix tuple. Adding `.enable` there would reclassify the
  unrelated `meho.connector.enable` MCP admin tool (whose broadcast
  op_class is derived from `classify_op` on the tool name) from `other`
  to `write`, an out-of-scope behaviour change. None of these ops carry
  secret material in their params (type, path, lease TTLs,
  descriptions — config only), so the full-detail `other` broadcast
  leaks nothing; `other` is both the cleaner and the more scoped choice.
- **`auth` read op group** (`ops_auth.py`, G3.3-T3 #547) —
  `register_vault_auth_operations()` registers `vault.auth.userpass.list`
  / `vault.auth.userpass.read` / `vault.auth.approle.list` /
  `vault.auth.approle.read` under group key `auth`, all
  `safety_level='safe'`. Mount path is parameterised (defaults
  `userpass` / `approle`); a not-mounted backend raises
  `VaultAuthBackendNotMountedError`. It is registered from its own
  module via the `register_vault_auth_operations()` call at the end of
  `register_vault_typed_operations()`.
- **`auth` write op group** (`ops_auth_write.py` /
  `ops_auth_write_schemas.py`, G3.15-T3 #1411) —
  `register_vault_auth_write_operations()` registers the userpass +
  approle credential lifecycle under the same `auth` group key, all
  `requires_approval=True`:

  | op_id | hvac call | safety_level | op_class (broadcast) |
  |---|---|---|---|
  | `vault.auth.userpass.write` | `create_or_update_user` | dangerous | `credential_write` |
  | `vault.auth.userpass.update_password` | `update_password_on_user` | caution | `credential_write` |
  | `vault.auth.userpass.delete` | `delete_user` | dangerous | `write` |
  | `vault.auth.approle.write` | `create_or_update_approle` | dangerous | `write` |
  | `vault.auth.approle.delete` | `delete_role` | dangerous | `write` |
  | `vault.auth.approle.generate_secret_id` | `generate_secret_id` | dangerous | `credential_mint` |

  Passwords (userpass write / update_password) are **request-side**
  secret material → `credential_write` (aggregate-only broadcast); the
  handlers also return a value-free confirmation (username/mount/policies,
  never the password). `generate_secret_id` mints a SecretID in its
  **response** → `credential_mint`; it is **non-idempotent** (a fresh
  SecretID per call), flagged as such in its `llm_instructions`, and the
  minted value reaches only the caller's `OperationResult` (never the
  audit row or broadcast). `delete` / `approle.write` carry no secret and
  classify plain `write` off their suffix. Registered from its own
  module via the `register_vault_auth_write_operations()` call at the end
  of `register_vault_typed_operations()`. `_reclassify_not_found` /
  `VaultAuthBackendNotMountedError` are reused from `ops_auth.py` so a
  404 from an unmounted backend surfaces the same error class as the
  read ops.
- **`identity` op group** (`ops_identity.py` / `ops_identity_schemas.py`,
  G3.15-T4 #1412) — entity / entity-alias / group lifecycle on the core
  `identity/` engine, group key `identity`:

  | op_id | hvac call | safety_level | approval | op_class |
  |---|---|---|---|---|
  | `vault.identity.entity.write` | `create_or_update_entity` | dangerous | True | `write` |
  | `vault.identity.entity_alias.write` | `create_or_update_entity_alias` | dangerous | True | `write` |
  | `vault.identity.group.write` | `create_or_update_group` | dangerous | True | `write` |
  | `vault.identity.group.delete` | `delete_group_by_name` | dangerous | True | `write` |
  | `vault.identity.entity.read` | `read_entity` | safe | False | `other` |
  | `vault.identity.group.read` | `read_group_by_name` | safe | False | `other` |
  | `vault.identity.list` | `list_groups` / `list_entities` | safe | False | `read` |

  Entity/group policy bindings are privilege assignments and group
  membership is privilege plumbing, so the four writes are approval-gated.
  The read primitives are registered **safe** (not `caution`) even though
  the lookups are HTTP POST/LIST, so a create-if-absent flow does not
  stall on approval (the issue's explicit ask). `entity.read` /
  `group.read` classify `other` (`.read` is deliberately not a
  read-suffix, matching the `vault.auth.*.read` convention);
  `list` classifies `read` via the `.list` suffix and normalises an
  empty-store 404 to `{"keys": []}`. None of the identity objects carry
  secret material.
- **`token` op group** (`ops_token.py` / `ops_token_schemas.py`,
  G3.15-T4 #1412) — token lifecycle on the core `token` backend, group
  key `token`:

  | op_id | hvac call | safety_level | approval | op_class |
  |---|---|---|---|---|
  | `vault.token.create` | `auth.token.create` | dangerous | True | `credential_mint` |
  | `vault.token.revoke_accessor` | `auth.token.revoke_accessor` | dangerous | True | `other` |
  | `vault.token.list_accessors` | `auth.token.list_accessors` | safe | False | `other` |

  `token.create` mints a client token in its **response** →
  `credential_mint` (aggregate-only broadcast; the token reaches only the
  caller's `OperationResult`, never the audit row or feed). It is
  **non-idempotent** (a fresh token per call). `revoke_accessor` is
  **surgical** — it revokes exactly one token by accessor; there is
  intentionally **no bulk-revoke op** (the vault skill's loudest
  Don't-rule). Token accessors are non-secret reference handles, so they
  are not redacted (`revoke_accessor` param / `list_accessors` response
  classify `other`).

  Both groups register from `ops_identity.py` / `ops_token.py` spec
  tables, composed by the thin
  `register_vault_identity_token_operations()` registrar
  (`ops_identity_token.py`), which is queued from the package `__init__`
  as its own lifespan registrar entry (independent of the KV / sys / auth
  registrars). `identity/` and `token` are core backends (always
  mounted), so there is no backend-not-mounted reclassification — a 404
  means a missing entity/group/accessor and surfaces as the underlying
  `hvac.exceptions.InvalidPath`.

## KV-v2 op group

| op_id | hvac call | safety_level | op_class (broadcast) |
|---|---|---|---|
| `vault.kv.read` | `read_secret_version` | safe | `credential_read` |
| `vault.kv.list` | `list_secrets` | safe | `credential_read` |
| `vault.kv.versions` | `read_secret_metadata` | safe | `read` |
| `vault.kv.put` | `create_or_update_secret` | caution | `credential_write` |
| `vault.kv.patch` | `patch` | caution | `credential_write` |
| `vault.kv.delete` | `delete_secret_versions` | dangerous | `write` |

All six register into operation group `kv`.

**`kv.put` vs. `kv.patch`.** `kv.put` replaces the latest version
wholesale (KV v2 does not merge — omitted keys are dropped). `kv.patch`
is the partial-write counterpart (G3.15-T1 #1409): hvac's
`secrets.kv.v2.patch` reads the current version, JSON-merges the
supplied `data` over it, and writes the result as a new version, so
keys absent from the request are preserved. The secret must already
exist (patching a missing path fails). `patch` exposes no `cas` guard
(it issues its own internal read+write), so the `kv.patch` schema —
unlike `kv.put` — has no `cas` property and `additionalProperties=False`
rejects a stray one.

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
`_CREDENTIAL_WRITE_OPS` allowlist (G11.7-T1 #1401 / G3.15-T1 #1409 —
`{vault.kv.put, vault.kv.patch, vault.auth.userpass.write,
vault.auth.userpass.update_password, k8s.secret.create, k8s.job.create}`),
and the `_WRITE_SUFFIXES` / `_READ_SUFFIXES`
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
full — the `credential_write` reclassification closes that.
`vault.kv.patch` (G3.15-T1 #1409) gets the same explicit pin for the
same reason: its merged fields ride in `params`, and without the
allowlist entry the `.patch` write-suffix would broadcast the partial
secret in full. The auth-write ops (G3.15-T3 #1411)
`vault.auth.userpass.write` / `vault.auth.userpass.update_password` are
also `credential_write` — the password is in their request params.
Response-side secret-mint ops (`vault.token.create`,
`vault.auth.approle.generate_secret_id`) classify `credential_mint`
alongside `harbor.robot.create`: the minted value is in the response, so
the broadcast collapses to aggregate-only and the audit row keeps only a
`params_hash`, while the caller's `OperationResult` still carries the
secret. See `docs/codebase/broadcast.md` for the full sensitivity
taxonomy.

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

The mutating KV ops — `vault.kv.put` (`caution`), `vault.kv.patch`
(`caution`), `vault.kv.delete` (`dangerous`) — register
`requires_approval=True` (G3.15-T1 #1409). The flag became meaningful
once G11.7-T1 (#1401) routed human principals hitting a
`requires_approval` op to the **approval queue** (park for review)
instead of hard-denying — so the gate no longer blocks the operator,
it parks the write. The read ops (`vault.kv.read` / `vault.kv.list` /
`vault.kv.versions`) omit the key and default `False`.
`requires_approval` is carried per-op in `_KV_OP_SPECS`; the
registration loop forwards `spec.get("requires_approval", False)`.
`safety_level` is the orthogonal posture signal (it does not by itself
gate execution).

The auth-write ops (G3.15-T3 #1411) likewise register
`requires_approval=True`: each is a privilege assignment (binding
`token_policies`), an irreversible identity removal, or a secret mint.
By the same G11.7-T1 (#1401) policy gate a human/service principal
hitting a `requires_approval=True` op is routed to the approval queue
(parked durably with synchronous `approval.request` / `approval.decision`
audit rows) rather than hard-denied; the call executes on the
approvals-API resume path once approved. `requires_approval` is a static
boolean on the descriptor and `safety_level` is the load-bearing signal
the gate keys on.

`vault.sys.policy.write` / `vault.sys.policy.delete` (G3.15-T2 #1410)
also register `requires_approval=True` (`safety_level='dangerous'`): a
bad HCL body can lock everyone out or silently widen access, so both
are approval-gated. A dispatch by a human/service principal therefore
parks as `awaiting_approval` before reaching Vault (proven in
`test_connectors_vault_sys.py`); the handler's hvac-forwarding logic is
exercised by calling it directly in the unit suite. The policy reads
(`policy.read` / `policy.list`) stay `requires_approval=False`.

The identity write ops (`identity.entity.write` / `entity_alias.write` /
`group.write` / `group.delete`) and the token write ops
(`token.create` / `token.revoke_accessor`) (G3.15-T4 #1412) likewise
register `requires_approval=True` — each is a privilege assignment,
privilege-plumbing membership change, irreversible removal, or a secret
mint. `requires_approval` / `safety_level` are carried per-op in the
`IDENTITY_OP_SPECS` / `TOKEN_OP_SPECS` rows. The identity reads
(`entity.read` / `group.read` / `list`) and `token.list_accessors` stay
`requires_approval=False`, registered `safe` so create-if-absent flows
do not stall. The integration tests drive the gated writes via the
approvals-API resume path (`dispatch(..., _approved=True)`), the same
path that runs once a human approves.

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
   three typed-op registrars (`register_vault_typed_operations`,
   `register_vault_sys_typed_operations`,
   `register_vault_identity_token_operations`) onto the lifespan-driven
   registrar list. `register_vault_typed_operations` registers the
   KV-v2 group and then calls `register_vault_auth_operations` (auth
   read) + `register_vault_auth_write_operations` (auth write).
   `register_vault_identity_token_operations` composes the `identity` +
   `token` groups from their per-module spec tables.
2. At FastAPI lifespan startup, `run_typed_op_registrars()` invokes
   all registrars, which upsert the `endpoint_descriptor` rows. Upsert
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
  G3.15-T2 (#1410), approval-gated; the sys bootstrap writes
  (`auth.enable` / `auth.tune` / `mounts.enable` / `mounts.tune`)
  landed in G3.15-T5 (#1413), also approval-gated. Unseal / rekey
  (destructive cluster-lifecycle ops) remain out of scope — not in the
  govc-parity verb set, not filed.
- AppRole secret-id generation is out of scope for v0.2 (high-risk
  write with policy implications; v0.2.next behind a policy gate).

## References

- Initiative #366 (G3.3 vault-1.x typed op surface); Goal #214.
- Tasks: #545 (G3.3-T1 KV-v2), #546 (G3.3-T2 sys read group),
  #547 (G3.3-T3 auth read group), #551 (G3.3-T7 dev-mode CI
  integration harness), #1409 (G3.15-T1 KV writes), #1410 (G3.15-T2
  policy ops), #1411 (G3.15-T3 auth credential lifecycle), #1412
  (G3.15-T4 identity + token ops).
- Vault dev mode: https://developer.hashicorp.com/vault/docs/concepts/dev-server
- Substrate: #388 (G0.6 operation registry), #390 (Refactor-Vault).
- Vault API: https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2
  (KV-v2), https://developer.hashicorp.com/vault/api-docs/system (sys),
  https://developer.hashicorp.com/vault/api-docs/auth/userpass
  (userpass), https://developer.hashicorp.com/vault/api-docs/auth/approle
  (approle), https://developer.hashicorp.com/vault/api-docs/secret/identity
  (identity), https://developer.hashicorp.com/vault/api-docs/auth/token
  (token).
- Decision #3 PII redaction: `docs/planning/v0.2-decisions.md`.
- CLAUDE.md postulates 1 (typed connectors first-class) and 7
  (synchronous append-only audit).
