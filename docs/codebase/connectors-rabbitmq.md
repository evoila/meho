# RabbitMQ connector

## Overview

`RabbitMqConnector` is a **read-only** connector over the RabbitMQ
Management HTTP API (`/api`, port 15672 for HTTP / 15671 for HTTPS). It
gives an agent a policy-gated, audited window into a broker's cluster
health, messaging topology, live client connectivity, and — the reason it
was built (#2233) — the cross-site **shovel / federation** topology, so
that surface finally lands inside the MEHO policy + audit seam instead of
staying outside it.

It lives at `backend/src/meho_backplane/connectors/rabbitmq/`:

- `connector.py` — `RabbitMqConnector(HttpConnector)`: auth, method gate,
  fingerprint/probe, the op handlers, registration, and the dispatch shim.
- `ops.py` — the `RABBITMQ_OPS` table (op metadata) + the
  `RABBITMQ_WHEN_TO_USE_BY_GROUP` group blurbs + `RABBITMQ_REDACTED_OP_IDS`.
- `redact.py` — `redact_rabbitmq_payload`, the credential-redaction walk.
- `session.py` — `RabbitMqTargetLike` (the target Protocol) + the
  injectable `RabbitMqCredentialsLoader` and the default Vault-backed
  `load_credentials_from_vault`.
- `__init__.py` — the two-phase registration (sync v2 registry entry at
  import + async typed-op registrar queued for lifespan).

## Key types

- **`RabbitMqConnector`** — subclass of `HttpConnector`
  (`connectors/adapters/http.py`). Registry v2 triple
  `("rabbitmq", "3.x", "rabbitmq-management")`; `supported_version_range`
  `">=3.8,<5.0"`; `priority = 1`.
- **`RabbitMqTargetLike`** — structural Protocol the concrete `Target`
  satisfies (`id`, `tenant_id`, `name`, `host`, `port`, `secret_ref`,
  `auth_model`).
- **`RabbitMqMethodNotAllowedError(ValueError)`** — raised by the method
  gate; the dispatcher records it as `connector_error`.
- **`RabbitMqOp`** — frozen dataclass mirroring the
  `register_typed_operation` kwargs (same shape as `ArgoCdOp`).

## Control flow

### Registration (two-phase)

Importing the package runs `register_connector_v2` twice — the versioned
triple and the `("rabbitmq", "", "")` wildcard fallback (G0.15-T6 dual
registration) — and queues `register_rabbitmq_typed_operations` onto the
lifespan registrar list. The `_eager_import_connectors` walk discovers the
subpackage by directory name, so no import-list edit is needed elsewhere.
At lifespan startup the registrar calls
`RabbitMqConnector.register_operations`, which walks `RABBITMQ_OPS` and
upserts one `endpoint_descriptor` per op (idempotent across restarts).

### Auth (HTTP Basic)

The Management plugin authenticates every request with HTTP Basic against
a broker user. `auth_headers` loads a `{username, password}` pair from the
target's `secret_ref` via the injectable loader (default: an
operator-context Vault KV-v2 read through the shared
`load_basic_credentials` helper), caches it per tenant-unique
`(tenant_id, id)` key, and sends `Authorization: Basic <base64>`. The
connector locks to `auth_model = shared_service_account` (or `None`).

### Read-only method gate

`_assert_read_method` refuses any verb other than `GET` / `HEAD` **before**
the request is issued. All curated ops are GETs; the gate exists to keep
the `rabbitmq.request` passthrough (and any future caller) from ever
mutating the broker — the read-only guarantee is enforced in code, not
just by op registration.

### Ops

16 ops across five functional groups, each carrying the RabbitMQ **user
tag** its surface requires — the second load-bearing nuance beside
redaction (the tag documents the broker permission the credential needs):

| Group | Ops | User tag |
|---|---|---|
| `rabbitmq-cluster` | `overview`, `nodes` | `monitoring` |
| `rabbitmq-topology` | `exchanges`, `queues`, `bindings`, `vhosts` | `monitoring` |
| `rabbitmq-connectivity` | `connections`, `channels`, `consumers` | `monitoring` |
| `rabbitmq-federation` | `shovel_status`, `federation_links` | `monitoring` |
| `rabbitmq-federation` | `shovels`, `parameters`, `policies` | `policymaker` |
| `rabbitmq-definitions` | `definitions` | `administrator` |
| `rabbitmq-raw` | `request` (GET/HEAD passthrough) | `monitoring` |

Vhost-scoped ops (`exchanges`, `queues`, `bindings`, `policies`,
`shovels`, `shovel_status`) accept an optional `vhost` param appended as a
percent-encoded path segment (`/api/queues/%2F`). `shovel_status` is
`/api/shovels` (the `rabbitmq_shovel_management` plugin); `shovels` is
`/api/parameters/shovel` (the dynamic-shovel runtime parameters);
`federation_links` is `/api/federation-links`
(`rabbitmq_federation_management`).

### Credential redaction (the load-bearing nuance)

The shovel / federation / parameter / definitions surfaces echo back the
credentials an operator stored: `amqp://user:pass@host` URIs in
`src-uri` / `dest-uri` / upstream `uri` fields, and — for
`/api/definitions` — user `password_hash` values. The handlers listed in
`RABBITMQ_REDACTED_OP_IDS` run their result through
`redact_rabbitmq_payload`, which:

1. blanks any `amqp(s)://user:pass@` userinfo to `amqp(s)://***@host`
   (host/port/vhost/query preserved), and
2. blanks the value of any mapping key containing `password` or `secret`
   (case-insensitive — catches `password`, `password_hash`,
   `client_secret`) to `***`.

The passthrough `rabbitmq.request` is redacted too (defence in depth: its
path may reach any of the above). The walk returns a new structure and
never mutates the input. This is *in addition to* the dispatcher's
generic connector-boundary redaction middleware — the connector owns the
AMQP-URI-specific rule.

### Fingerprint / probe

`fingerprint` reads `GET /api/overview` + `GET /api/nodes` and returns
`rabbitmq_version` as the canonical `version`, with `cluster_name`,
`erlang_version`, `management_version`, `product_name`/`product_version`,
and a per-node `{name, running, type, erlang_version}` summary under
`extras`. Both endpoints require Basic auth, so an operator-less
(background) call falls back to the synthesised system operator and fails
closed at the live Vault read — `reachable=False` with `extras["error"]`,
never an exception. `probe` delegates to `fingerprint`.

## Dependencies

- `HttpConnector` (`connectors/adapters/http.py`) — pooled httpx client,
  retry policy, TLS-trust handling, SSRF guard.
- `load_basic_credentials` (`connectors/_shared/vault_creds.py`) — the
  shared operator-context Vault KV-v2 read.
- `register_typed_operation` (`operations/typed_register.py`) — the
  descriptor upsert + embedding.
- The dispatcher (`operations/dispatcher.py`) — resolves the connector,
  validates params against the op schema, threads `operator`, redacts,
  reduces, audits.

## Known issues / limitations

- **Probe needs operator context.** RabbitMQ's Management API has no
  unauthenticated endpoint, so `probe()` (operator-less) always fails
  closed at the Vault read. Reachability is meaningful only on the
  dispatch path where a real operator is present.
- **Shovel/federation status needs the plugins.** `shovel_status` and
  `federation_links` require `rabbitmq_shovel_management` /
  `rabbitmq_federation_management` to be enabled on the target; a broker
  without them returns 404 for those paths.
- **Read-only by design.** No write/admin ops (create/delete shovel,
  set policy, …) — those would be a separate approval-gated G3.x write
  surface, not part of this connector.
- **Plain-HTTP brokers need `extras.scheme`.** `HttpConnector` dials
  `https` by default, so a broker whose Management API is HTTP-only on
  `15672` (the common default — HTTPS on `15671` only when the mgmt TLS
  listener is enabled) is unreachable until the operator opts in by
  setting `extras: {"scheme": "http"}` on the target (#2587). The scheme
  is validated to `http`/`https` at dispatch and is orthogonal to
  `verify_tls` (certificate trust vs. transport selection).

## References

- Task #2233 (this connector); Initiative #2228.
- Contract precedents: `connectors-argocd.md`, `connectors-harbor.md`,
  `connectors-pfsense.md`, `connectors-bind9.md`.
- RabbitMQ Management HTTP API: <https://www.rabbitmq.com/docs/management>,
  <https://www.rabbitmq.com/docs/http-api-reference>.
- Shovel / federation plugins: <https://www.rabbitmq.com/docs/shovel>,
  `rabbitmq_shovel_management` / `rabbitmq_federation_management`.
