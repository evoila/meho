# Connector: hetzner-robot (Hetzner Robot Webservice)

## Overview

The `hetzner-robot` connector is the hand-rolled `HttpConnector` subclass
for the [Hetzner Robot Webservice API](https://robot.hetzner.com/doc/webservice/en.html).
G3.7-T7 (#846) ships the skeleton — HTTP Basic auth, fingerprint, probe,
`_post_form` helper, and the G0.6 dispatch shim. G3.7-T8 (#849) ships the
read-only v0.2 core: the Robot Webservice OpenAPI spec is ingested via G0.7
into the `endpoint_descriptor` table, and the curated read core is staged
for operator review in `core_ops.py`. G3.7-T9 (#852) ships the read-only CLI
verbs (`server list`/`server info`, `ip list`,
`subnet list`, `vswitch list`/`vswitch info`, `failover list`, `rdns list`,
`ssh-key list`, and the generic `operation search`/`operation call`; the
former `about` verb was removed by #2985 — see the reconcile note below), the
dispatch smoke test suite (AC1–AC5 covering all curated read ops, JSONFlux
handle path, audit-row assertions, and sandbox empty-array tolerance), and the
operator onboarding doc. Task #2848 (Initiative #2833) curates the per-server
firewall read (`GET:/firewall/{server-ip}`) into the `robot-networking`
group — the **11th** core op — and adds the `hetzner-robot firewall get
<server-ip>` CLI verb, so the consumer onboarding runbook's firewall-verify
step no longer shells `scripts/hetzner-robot.sh GET /firewall/<server-number>`
outside MEHO's policy / audit / broadcast / JSONFlux path.

Task #3973 adds **vSwitch membership**: an operator can add a dedicated
server to a vSwitch, or remove it, through MEHO. What changed:

- Two new curated write ops, `POST:/vswitch/{vswitch-id}/server` (add) and
  `DELETE:/vswitch/{vswitch-id}/server` (remove). The curated core now has
  **12 ops**: 10 reads and these 2 writes.
- Every Robot write now goes out as **form fields**, the only body format
  Robot reads (see "Form-encoded writes" below).
- Every vSwitch write **waits for a human approval**, for every kind of
  login. A standing grant cannot skip the wait (see "Approval for vSwitch
  writes" below).
- The two vSwitch ops the spec described wrongly now say what they really
  do. `POST:/vswitch/{vswitch-id}` renames the vSwitch or changes its VLAN
  ID. `DELETE:/vswitch/{vswitch-id}` cancels the whole vSwitch. Both stay
  switched off.

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
- **`ROBOT_CORE_GROUPS`** (`core_ops.py`) — 3 curated `RobotCoreGroup`
  entries with operator-reviewed `when_to_use` hints spanning the read-only
  core: `robot-servers`, `robot-networking`, `robot-ssh-keys`. (The former
  `robot-about` group fell with the #2985 `GET:/query` removal.)
- **`ROBOT_CORE_OPS`** (`core_ops.py`) — 12 curated `RobotCoreOp` entries,
  each with `op_id` (`METHOD:/path` form), `group_key`, and `llm_instructions`
  blob (`when_to_call` / `output_shape` / `next_step`). Ten are reads. The
  10th read, `GET:/firewall/{server-ip}` (#2848), reads one dedicated server's
  packet-filter firewall (status, allowlist flag, ordered input/output rules)
  for the onboarding firewall-verify step and classifies into `robot-networking`.
  The two writes (#3973) add servers to a vSwitch and remove them, both in
  `robot-networking`. Their `llm_instructions` tell the agent that the change
  waits for a human approval (the requester cannot approve it), that an agent
  without an explicit permission is refused (`denied`) and should ask a human
  instead of retrying, that Robot
  applies it in the background (poll `GET:/vswitch/{vswitch-id}` until the
  server is `ready`, or gone after a remove), that `409 VSWITCH_IN_PROCESS`
  means an earlier change is still running, that Robot allows 100 calls per
  hour on these routes, and that vSwitch traffic needs MTU 1400.
- **`apply_robot_core_curation`** (`core_ops.py`) — async function that
  drives `ReviewService.edit_group` + `enable_group` + `edit_op` to flip the
  12 curated ops to `is_enabled=True` and land `llm_instructions`. Every
  other op in a curated group is switched off, so the vSwitch rename and
  cancel ops stay off.
- **`classify_robot_op`** (`core_ops.py`) — path-prefix classifier mapping
  a `GET:/path` op_id, or one of the two membership writes, to its curated
  `group_key` via `ROBOT_PATH_RULES`. Every other write returns `"none"`.
- **`robot_form_fields`** (`connector.py`) — turns a write body into the
  form fields Robot reads (#3973).
- **`hetzner_robot_safety_floor`** (`ingest_safety.py`) — the ingest safety
  floor for the four vSwitch writes (#3973). Registered when the package is
  imported.

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

### Form-encoded writes

Robot reads write bodies only as `application/x-www-form-urlencoded`. It
rejects `application/json`. The dispatcher passes every ingested write body
to the connector's `_post_json` as `json=`. `HetznerRobotConnector`
overrides `_post_json` (#3973) and sends the body with httpx's `data=`
instead:

- text and numbers go as they are; booleans go as `true` / `false`;
- a list goes as repeated `name[]` fields: `{"server": [321, "1.2.3.4"]}`
  becomes `server[]=321&server[]=1.2.3.4`;
- a nested object, a list inside a list, or `null` has no form shape, so the
  write is refused with a clear `ValueError` **before any HTTP call** (the
  dispatcher reports `connector_error` and the message is in
  `extras.exception_message`). This is why the firewall set
  (`POST:/firewall/{server-ip}`), with its nested rule list, cannot be sent;
  it stays off.

The rest is the shared `HttpConnector._post_json` seam: the real verb (POST
or DELETE; the DELETE body is sent the same way), **no retry** (a 500 or a
timeout is exactly one HTTP call), and an empty answer or a `204` maps to
`{}`. A 401 is one call, and the dispatcher reports it as
`connector_auth_failed` with the fix-the-credential hint — the same as for
reads, so the Robot IP block (three failed logins) is never risked by a
retry.

The older `_post_form(target, path, data)` helper stays for direct callers;
dispatch does not use it.

### Approval for vSwitch writes

Ingest gives a POST `caution` and a DELETE `dangerous`, both without an
approval, so a human login would run either at once. The connector's safety
floor (`ingest_safety.py`, #3973) raises the four vSwitch writes after every
ingest:

| Op | What it does | Safety level | Approval |
|---|---|---|---|
| `POST:/vswitch/{vswitch-id}/server` | add servers | `dangerous` | required |
| `DELETE:/vswitch/{vswitch-id}/server` | remove servers | `dangerous` | required |
| `POST:/vswitch/{vswitch-id}` | rename, or change the VLAN of every member | `dangerous` | required |
| `DELETE:/vswitch/{vswitch-id}` | cancel the whole vSwitch | `destructive` | required |

The floor only raises a level, and it survives a re-ingest: the ingest merge
keeps the stricter level and puts the approval flag back, even if someone
lowered it with `meho connector edit-op`. What each login gets:

- **Human:** waits for approval. The requester can never approve their own
  `dangerous` or `destructive` request, not even with
  `APPROVAL_ALLOW_SELF_APPROVAL=true`.
- **Agent:** refused by default. With an explicit agent permission, a
  membership change waits for approval. The cancel op is always refused for
  agents.
- **Service login:** waits for approval. A standing grant cannot skip the
  wait: the default grant patterns refuse `DELETE:*` and, since #3973,
  `POST:/vswitch/*`.
- An approved change runs at most once: a second resume of the same approval
  returns `already_resumed` and sends nothing.
- `preview_operation` shows `requires_approval: true`, the safety level, the
  vSwitch id (in `resolved_path`) and the server list (in `redacted_body`)
  before the real call.

The rename and cancel ops stay switched off. Even if someone switches the
cancel op on, it cannot wait for approval: a `destructive` op needs a
blast-radius statement, and this ingested op has none, so the dispatcher
refuses it (`blast_radius_required`) before Robot is called.

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
  covering list/get servers, vSwitch get + membership add/remove + rename +
  cancel, per-server firewall get/set, reverse DNS, and the `server_addon`
  order. The four vSwitch writes declare `application/x-www-form-urlencoded`
  bodies and match the vendor reference (#3973; before that, the rename and
  cancel routes were described as membership add and remove). Each of these spec GET
  op_ids (`GET:/server`, `GET:/server/{server-ip}`, `GET:/vswitch`,
  `GET:/vswitch/{vswitch-id}`, `GET:/firewall/{server-ip}`, `GET:/rdns`) is
  curated in `ROBOT_CORE_OPS`, so the ingested rows and the curated read core
  agree on the same strings. `GET:/firewall/{server-ip}` was ingested from this
  spec from the start but only joined the curated core in #2848; before that it
  was ingested-but-uncurated (`is_enabled=False`, no `llm_instructions`) and
  unreachable through `search_operations` / `call_operation`. (`GET:/rdns/{ip}`
  is the one spec GET the core deliberately leaves uncurated — the account-wide
  `GET:/rdns` list covers the read need.)
- **Spec-reconcile lane (#2985)** —
  `backend/tests/test_connectors_hetzner_robot_spec_reconcile.py` asserts
  every hand-coded `METHOD:/path` (the curated `ROBOT_CORE_OPS` strings plus
  the minimal spec's op_ids, parsed through the real ingest parser) against
  the pinned `hetzner-robot-2026-04/webservice-en.md` documented-route list
  on the consumer spec-shelf (Hetzner publishes no machine-readable spec).
  First run caught two findings, fixed in the same PR: `GET:/query`
  (`hetzner-robot.about` + the CLI `about` verb + the `robot-about` group)
  referenced an endpoint the Robot Webservice does not serve and was
  removed; the `/vswitch/{id}` template family was renamed to the vendor's
  `{vswitch-id}` (runtime request path unchanged — only the template/param
  name). The `{server-ip}` paths ride the vendor's *documented deprecated*
  IP-addressed alternatives to the `{server-number}` routes; the lane pins
  that reliance explicitly.
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
- Writes: only vSwitch membership add and remove are curated (#3973). Server
  reset, vSwitch rename / VLAN change, vSwitch cancel, the firewall set and
  rDNS edits stay switched off. Purchases (ordering or cancelling servers and
  add-ons) are manual by policy: a person does them in the Hetzner portal.
  The `server_addon` order in the spec stays off; its body uses `product`,
  but Robot wants `product_id`.
- The curated reads `GET:/ip`, `GET:/subnet`, `GET:/failover` and `GET:/key`
  are not in the shipped minimal spec, so an install that ingests only this
  spec has no rows for them, and `apply_robot_core_curation` stops at the
  first missing one.
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
