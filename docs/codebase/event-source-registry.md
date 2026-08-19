# Event source registry

The `event_source` registry is the tenant-scoped list of external event
producers (Alertmanager, Grafana, VCF Operations, Harbor, generic JSON
senders) authorised to publish into a tenant's event → agent-trigger
substrate. It is Task #2880 under Initiative #2877 (inbound event
ingestion); the inbound webhook endpoint that *consumes* the registry is
#2881 and is out of scope for this page.

## Why it exists

A webhook sender carries no JWT. The registry row is therefore the whole
trust context of an inbound request:

- **Tenant attribution.** `events.publish()` needs a real tenant. A
  JWT-less sender cannot supply one, so it comes from the resolved row —
  which is why `event_source.tenant_id` is a real `REFERENCES tenant(id)`
  FK, not the soft-FK the older `targets` table kept.
- **Authentication.** The row names an `auth_strategy` and points at a
  Vault-custodied secret (`secret_ref`); the ingest endpoint verifies the
  sender's signature / token against that secret.

## Overview

The registry is moulded on the `targets` registry (`db/models.py`,
`targets/`, `api/v1/targets.py`): a per-tenant table with a partial unique
index on `name`, a nullable `secret_ref` Vault path, an `extras` JSONB
escape hatch, and a `deleted_at` soft-delete. Three things differ from
`targets`, each deliberate:

| Aspect | `targets` | `event_source` | Why |
|---|---|---|---|
| `tenant_id` | soft-FK (no constraint) | real `FK → tenant.id` | tenant attribution for a JWT-less sender must be integrity-checked |
| `slug` uniqueness | n/a (addressed by `name`) | **global** partial unique | the slug is the sole routing key in the JWT-less ingest URL, so `slug → tenant` must resolve unambiguously across all tenants |
| secret handling | references an externally provisioned Vault path | the admin surface **writes** the secret to Vault | criterion 2: rotation = write, no downtime |

`name` is unique per tenant; `slug` is unique globally; both uniqueness
rules apply only among live rows (`WHERE deleted_at IS NULL`), so a
soft-deleted tombstone frees the `name`/`slug` for re-use (the `targets`
#2874 pattern).

## Key types

- **`EventSource`** (`db/models.py`) — the ORM row. Columns: `id`,
  `tenant_id` (FK), `name`, `slug`, `kind`, `auth_strategy`, `secret_ref`,
  `status`, `extras` (JSONB), `created_by_sub`, `created_at`,
  `updated_at`, `deleted_at`. `kind` / `auth_strategy` / `status` are
  closed sets enforced by `ck_event_source_*` CHECK constraints. Two
  partial unique indexes: `event_source_tenant_name_idx` on
  `(tenant_id, name)` and `event_source_slug_idx` on `(slug)`.
- **Schemas** (`event_source/schemas.py`) — `EventSourceCreate` /
  `EventSourceUpdate` (write, `extra='forbid'`), `EventSource` /
  `EventSourceSummary` (frozen reads), plus the `EventSourceKind`,
  `EventSourceAuthStrategy`, `EventSourceStatus` StrEnums whose values
  mirror the CHECK constraints. The write schemas carry a **write-only**
  `secret: SecretStr`; no read model has a `secret` field.
- **Resolvers** (`event_source/resolver.py`) —
  `resolve_event_source(session, tenant_id, slug)` is the tenant-scoped
  admin lookup (uniform 404 on miss or cross-tenant, no near-miss
  oracle); `resolve_event_source_by_slug(session, slug)` is the global
  ingest primitive (#2881 consumes it) returning the live row of any
  status, or `None`.
- **Secret custody** (`event_source/secrets.py`) —
  `event_source_secret_ref(tenant_id, slug)` derives
  `tenants/<tenant_id>/event-sources/<slug>`;
  `store_event_source_secret(operator, secret_ref, secret)` writes the
  value through the secret-broker vault-kv sink.
- **REST surface** (`api/v1/event_source.py`) — the CRUD router at
  `/api/v1/event-sources`, wired in `main.py`.

## Control flow

- **Create** (`POST /api/v1/event-sources`, tenant_admin) — build the
  row (`tenant_id` and `created_by_sub` from the JWT, never the body);
  `flush()` so a duplicate `name`/`slug` 409s *before* any Vault write;
  if a `secret` was supplied, derive `secret_ref` and write the value to
  Vault *before* the transaction commits, so a Vault failure rolls the row
  back (fail-closed 502). Omitting `secret` registers the source with no
  credential (`secret_ref` NULL); the ingest path fails closed until a
  later PATCH sets one.
- **Read** (`GET` list + `GET /{slug}`, operator) — tenant-scoped,
  keyset-paginated by `name`, optional `?status=` filter. A slug that
  does not resolve in the tenant (absent or cross-tenant) returns the
  identical uniform 404.
- **Update** (`PATCH /{slug}`, tenant_admin) — applies only sent fields;
  `name`/`slug` are immutable (the slug is a live routing key). A
  `status='paused'` PATCH takes effect on the ingest resolver's next
  lookup because nothing caches the row. A `secret` rotates the Vault
  value at the derived path (a new KV-v2 version — no downtime) and homes
  `secret_ref` if the source had none.
- **Delete** (`DELETE /{slug}`, tenant_admin) — soft-delete (stamps
  `deleted_at`); the row stays for audit history, the partial indexes free
  the `name`/`slug`, and a second DELETE collapses to the uniform 404.

## Secret-custody discipline

The auth secret lives in Vault, never in the DB. The registry row stores
only the derived logical path. `store_event_source_secret` holds the value
as `SecretStr` up to the write and hands it to the secret-broker's
`SecretMaterial` (redacted repr) at the KV-v2 write boundary, reusing
`VaultKvSecretEndpoint` so the "status + SHA-256 + length only, never the
value" discipline (`docs/codebase/connectors-secret-broker.md`) is shared,
not re-implemented. The value never enters params, the row, a log line,
the audit row, or a broadcast. The write runs under the operator's own
Vault identity (`vault_client_for_operator`), so it stays inside their
authz envelope. The always-on secret-leak sweep in `tests/conftest.py`
guards the log surface across the whole suite; the store path logs only
`secret_ref` and never the traceback locals that would render the
`secret` parameter name.

## Extras (forward config for the ingest path)

Per-source tuning the #2881 ingest reads — body-size cap, rate limit,
replay window, dedupe config, and (for `basic` auth) the non-secret
username — lives in the `extras` JSONB blob rather than first-class
columns, keeping the schema to #2880's explicit column set.

## Dependencies

- `meho_backplane.connectors.secret.vault_endpoint.VaultKvSecretEndpoint`
  + `SecretMaterial` — the vault-kv sink the secret write reuses.
- `meho_backplane.connectors.vault.tenant_paths.TENANT_SECRET_PREFIX` —
  the per-tenant Vault subtree the derived ref sits inside (the #1643
  scope guard enforces it).
- `meho_backplane.auth.rbac.require_role` / `auth.operator.Operator` —
  RBAC (operator reads, tenant_admin writes) and JWT-bound tenant scope.
- Alembic migration `0074_create_event_source.py`.

## Known issues / non-goals

- The inbound ingest endpoint, per-source auth enforcement (HMAC / bearer
  / basic verification), rate limiting, replay windows, and payload
  normalisers are #2881 / #2882 — not built here. This task ships the
  registry, its migration, Vault secret custody, and the REST/CLI/UI admin
  CRUD.
- Soft-delete leaves the Vault secret in place (RDC owns the Vault
  lifecycle). A re-created source re-using the slug derives the same ref
  and overwrites it on its next secret write.
- There is no MCP admin tool for `event_source` (unlike `targets`'
  `meho_targets_register`). #2880's surface set is REST + CLI + UI; an MCP
  admin tool would be a separate follow-up if agent-driven registration is
  wanted.

## References

- Initiative #2877 (inbound event ingestion), Task #2880.
- `docs/codebase/connectors-secret-broker.md` — the custody discipline.
- `docs/codebase/migrations.md` — the additive-only migration contract.
- `db/models.py` `Target` / `EventSource`; `api/v1/targets.py` — the mould.
