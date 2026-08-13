# Connector: hetzner-robot (Hetzner Robot Webservice)

## Overview

The `hetzner-robot` connector is the hand-rolled `HttpConnector` subclass
for the [Hetzner Robot Webservice API](https://robot.hetzner.com/doc/webservice/en.html).
G3.7-T7 (#846) ships the skeleton — HTTP Basic auth, fingerprint, probe,
`_post_form` helper, and the G0.6 dispatch shim. G3.7-T8 (#849) ships the
read-only v0.2 core: the Robot Webservice OpenAPI spec is ingested via G0.7
into the `endpoint_descriptor` table, and the curated read core is staged
for operator review in `core_ops.py`. G3.7-T9 (#852) ships the read-only CLI
verbs (`hetzner-robot about`, `server list`/`server info`, `ip list`,
`subnet list`, `vswitch list`/`vswitch info`, `failover list`, `rdns list`,
`ssh-key list`, and the generic `operation search`/`operation call`), the
dispatch smoke test suite (AC1–AC5 covering all curated read ops, JSONFlux
handle path, audit-row assertions, and sandbox empty-array tolerance), and the
operator onboarding doc. Task #2848 (Initiative #2833) curates the per-server
firewall read (`GET:/firewall/{server-ip}`) into the `robot-networking`
group — the **11th** core op — and adds the `hetzner-robot firewall get
<server-ip>` CLI verb, so the consumer onboarding runbook's firewall-verify
step no longer shells `scripts/hetzner-robot.sh GET /firewall/<server-number>`
outside MEHO's policy / audit / broadcast / JSONFlux path.

Source: `backend/src/meho_backplane/connectors/hetzner_robot/`.

## Key types

- **`HetznerRobotConnector`** (`connector.py`) — `HttpConnector` subclass.
  Class attributes: `product="hetzner-robot"`, `version="2026.04"`,
  `impl_id="hetzner-rest"`, `priority=1`. The priority outranks a future
  `GenericRestConnector` auto-shim (priority=0) defensively.
- **`HetznerRobotTargetLike`** (`session.py`) — runtime-checkable Protocol
  capturing the minimum target shape: `name`, `host`, `port`, `secret_ref`,
  `auth_model`. No `sso_realm` field — Hetzner Robot Basic auth sends
  `username:password` directly with no realm suffix.
- **`HetznerRobotCredentialsLoader`** (`session.py`) — async callable type
  resolving a `(target, operator)` pair to `{"username": ..., "password": ...}`.
  Injectable on connector construction for tests and integration deploys.
- **`load_credentials_from_vault`** (`session.py`) — default loader.
  Performs the **live** operator-context KV-v2 read by delegating to the
  shared `_shared/vault_creds.load_basic_credentials` helper (#2079) — the
  same read harbor / vmware / sddc use. The Webservice-user credential is
  read under the operator's Vault identity; a system-initiated call
  (`raw_jwt=""`) fails closed with `VaultCredentialsReadError` rather than
  falling back.
- **`ROBOT_CORE_GROUPS`** (`core_ops.py`) — 4 curated `RobotCoreGroup`
  entries with operator-reviewed `when_to_use` hints spanning the read-only
  core: `robot-about`, `robot-servers`, `robot-networking`, `robot-ssh-keys`.
- **`ROBOT_CORE_OPS`** (`core_ops.py`) — 11 curated `RobotCoreOp` entries
  (the read-only v0.2 core), each with `op_id` (`GET:/path` form), `group_key`,
  and `llm_instructions` blob (`when_to_call` / `output_shape` / `next_step`).
  The 11th, `GET:/firewall/{server-ip}` (#2848), reads one dedicated server's
  packet-filter firewall (status, allowlist flag, ordered input/output rules)
  for the onboarding firewall-verify step and classifies into `robot-networking`.
- **`apply_robot_core_curation`** (`core_ops.py`) — async function that
  drives `ReviewService.edit_group` + `enable_group` + `edit_op` to flip the
  11 curated ops to `is_enabled=True` and land `llm_instructions`.
- **`classify_robot_op`** (`core_ops.py`) — path-prefix classifier mapping
  a `GET:/path` op_id to its curated `group_key` via `ROBOT_PATH_RULES`.

## Key design decisions

### IP-block protection (no-retry-on-401)

Hetzner Robot blocks the source IP for **10 minutes** after 3 consecutive
401 responses from that IP. Because MEHO operates on a shared egress IP,
a single misconfigured target could lock every operator off the Robot API
for 10 minutes.

The connector raises `RuntimeError` with an `auth_failed` label and a
remediation message on the **first** 401 response — it never retries,
never consumes the 2 remaining attempts. The base `HttpConnector._retryable`
predicate already excludes 4xx from the tenacity retry logic; `_get_robot_json`
adds the explicit intercept so operators see a useful message instead of a
generic `httpx.HTTPStatusError`.

### Form-encoded bodies

The Robot Webservice API requires `application/x-www-form-urlencoded` bodies
for all write verbs — it rejects `application/json`. The `_post_form(target,
path, data)` helper wraps httpx's `data=` parameter (which encodes a dict as
RFC 3986 form-encoded). v0.2 read operations never POST, but the helper ships
for v0.2.next write readiness.

### Webservice user

The Robot API authenticates with a **Webservice user** — a separate account
distinct from the Robot portal login user. Operators must create the Webservice
user in the Robot portal and store its credentials at the target's `secret_ref`
Vault path as `{"username": ..., "password": ...}`.

## Control flow

### Registration

1. Lifespan calls `_eager_import_connectors()`, which walks every
   `connectors/<product>/` subpackage in name-sorted order.
2. Importing `meho_backplane.connectors.hetzner_robot` triggers the
   module-level `register_connector_v2(product="hetzner-robot",
   version="2026.04", impl_id="hetzner-rest", cls=HetznerRobotConnector)`.
3. The registry's v2 table resolves `("hetzner-robot", "2026.04",
   "hetzner-rest")` to `HetznerRobotConnector`.

### Auth flow

1. `auth_headers(target, operator)` checks `target.auth_model` — must be
   `shared_service_account` or `None`. The `operator` is accepted for the
   shared HTTP auth surface (G3.9-T1) but unused — `shared_service_account`
   mode authenticates with a Vault-sourced Webservice-user credential, not
   the operator's OIDC token.
2. `_load_credentials(target)` checks `_creds_cache`; on miss, calls the
   injectable loader.
3. Loader returns `{"username": ..., "password": ...}`; connector computes
   `Authorization: Basic <base64>` and caches the raw dict.
4. Every subsequent call against the same target uses the cached value.

### Fingerprint flow

1. `fingerprint(target)` calls `_get_robot_json(target, "/server")`.
2. On 401: `_get_robot_json` raises `RuntimeError("auth_failed: ...")` (1
   request, no retry). `fingerprint()` catches it and returns
   `FingerprintResult(reachable=False, extras={"error": ...})`.
3. On success: parses the server list (both `{"servers": [...]}` wrapper and
   bare `[...]` forms), extracts `server_count` and `account_id` from the
   first server's `server_number`.

### Probe flow

1. `probe(target)` calls `_get_robot_json(target, "/server")`.
2. On any error (including 401-not-retried): returns `ProbeResult(ok=False,
   reason=...)`.
3. On success: returns `ProbeResult(ok=True)`.

## Dependencies

- `httpx>=0.27` (0.28.1 resolved) — `data=` for form-encoded POSTs
- `tenacity>=9.0` — base class retry logic (401 excluded from retry predicate)
- `structlog` — structured logging

## Spec ingest (#2079)

The connector shell registers empty (`operation_count=0`); ingesting a spec
fills the `endpoint_descriptor` table. Because the Robot Webservice publishes
no OpenAPI document, MEHO ships a hand-authored minimal spec as package data:

- **`operations/ingest/specs/hetzner_robot_minimal.yaml`** — OpenAPI 3.0
  covering list/get servers, vSwitch get + membership, per-server firewall
  get/set, reverse DNS, and the `server_addon` order. Each of these spec GET
  op_ids (`GET:/server`, `GET:/server/{server-ip}`, `GET:/vswitch`,
  `GET:/vswitch/{id}`, `GET:/firewall/{server-ip}`, `GET:/rdns`) is curated in
  `ROBOT_CORE_OPS`, so the ingested rows and the curated read core agree on the
  same strings. `GET:/firewall/{server-ip}` was ingested from this spec from the
  start but only joined the curated core in #2848; before that it was
  ingested-but-uncurated (`is_enabled=False`, no `llm_instructions`) and
  unreachable through `search_operations` / `call_operation`. (`GET:/rdns/{ip}`
  is the one spec GET the core deliberately leaves uncurated — the account-wide
  `GET:/rdns` list covers the read need.)
- Ingest via `meho connector ingest --product hetzner --version 2026.04
  --impl hetzner-rest --spec <this-file>`. The ingest guard defers to the
  registered `HetznerRobotConnector` for the triple rather than scaffolding a
  `GenericRestConnector` shim, so the ingested ops resolve to the hand-coded
  connector (not `no_connector`). Coverage:
  `tests/test_connectors_hetzner_robot_ingest.py`.

## Known issues / out of scope

- Env-gated automated canary: the full spec ingest against
  `IngestionPipelineService` with a real LLM stub is a follow-up to T8
  requiring the Robot spec reachable from CI.
- Writes: server reset, vSwitch mutation, cancellation, rDNS edits are out of
  scope for the read-only core. The write ops are declared in the shipped
  minimal spec so the ingested corpus covers the full wrapper surface, but
  only the curated read ops are enabled; the `_post_form` helper is the
  write-path foundation for the G3.x write-surface curation.
- Hetzner Cloud (the second Hetzner product): out of scope.

## References

- Hetzner Robot Webservice docs: https://robot.hetzner.com/doc/webservice/en.html
- G3.7-T7 skeleton issue: https://github.com/evoila/meho/issues/846
- G3.7-T8 core-ops issue: https://github.com/evoila/meho/issues/849
- Spec-ingest + Vault-auth wiring: https://github.com/evoila/meho/issues/2079
- Canary runbook: [`docs/cross-repo/g37-hetzner-canary.md`](../cross-repo/g37-hetzner-canary.md)
- Precedent: `connectors/harbor/core_ops.py` (apply_harbor_core_curation pattern)
- Precedent: `connectors/harbor/connector.py` (HTTP Basic + loader + fingerprint/probe)
- Precedent: `connectors/adapters/http.py` (`HttpConnector` + retry policy)
