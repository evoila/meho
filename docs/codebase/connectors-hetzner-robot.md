# Connector: hetzner-robot (Hetzner Robot Webservice)

## Overview

The `hetzner-robot` connector is the hand-rolled `HttpConnector` subclass
for the [Hetzner Robot Webservice API](https://robot.hetzner.com/doc/webservice/en.html).
G3.7-T6 (#846) ships the skeleton — HTTP Basic auth, fingerprint, probe,
`_post_form` helper, and the G0.6 dispatch shim. G3.7-T8 (#849) ships the
read-only v0.2 core: the Robot Webservice OpenAPI spec is ingested via G0.7
into the `endpoint_descriptor` table, and the curated 10-op core is staged
for operator review in `core_ops.py`. G3.7-T9 (#852) ships the CLI verbs
(`hetzner-robot target add/list/delete/set`), the dispatch smoke test suite
(AC1–AC5 covering all 10 read ops, JSONFlux handle path, audit-row
assertions, and sandbox empty-array tolerance), and the operator onboarding
doc.

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
  resolving a target to `{"username": ..., "password": ...}`. Injectable on
  connector construction for tests and pre-G0.3 production deploys.
- **`load_credentials_from_vault`** (`session.py`) — default loader, stubbed
  `NotImplementedError` until Goal #214 lands the operator-context Vault
  read path.
- **`ROBOT_CORE_GROUPS`** (`core_ops.py`) — 4 curated `RobotCoreGroup`
  entries with operator-reviewed `when_to_use` hints spanning the read-only
  core: `robot-about`, `robot-servers`, `robot-networking`, `robot-ssh-keys`.
- **`ROBOT_CORE_OPS`** (`core_ops.py`) — 10 curated `RobotCoreOp` entries
  (the read-only v0.2 core), each with `op_id` (`GET:/path` form), `group_key`,
  and `llm_instructions` blob (`when_to_call` / `output_shape` / `next_step`).
- **`apply_robot_core_curation`** (`core_ops.py`) — async function that
  drives `ReviewService.edit_group` + `enable_group` + `edit_op` to flip the
  10 curated ops to `is_enabled=True` and land `llm_instructions`.
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

1. `auth_headers(target, raw_jwt)` checks `target.auth_model` — must be
   `shared_service_account` or `None`.
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

## Known issues / out of scope

- Vault credential read stub: `load_credentials_from_vault` raises
  `NotImplementedError` until Goal #214 lands. Inject a loader at
  construction time for production deploys until then.
- Env-gated automated canary: the full spec ingest against
  `IngestionPipelineService` with a real LLM stub is a follow-up to T8
  requiring the Robot spec reachable from CI.
- Writes: server reset, vSwitch mutation, cancellation, rDNS edits are out of
  scope for G3.7 v0.2. The `_post_form` helper is the write-path foundation.
- Hetzner Cloud (the second Hetzner product): out of scope.

## References

- Hetzner Robot Webservice docs: https://robot.hetzner.com/doc/webservice/en.html
- G3.7-T6 skeleton issue: https://github.com/evoila/meho/issues/846
- G3.7-T8 core-ops issue: https://github.com/evoila/meho/issues/849
- Canary runbook: [`docs/cross-repo/g37-hetzner-canary.md`](../cross-repo/g37-hetzner-canary.md)
- Precedent: `connectors/harbor/core_ops.py` (apply_harbor_core_curation pattern)
- Precedent: `connectors/adapters/http.py` (`HttpConnector` + retry policy)
