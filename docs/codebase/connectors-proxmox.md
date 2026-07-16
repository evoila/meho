# Proxmox VE connector

`ProxmoxConnector` is the hand-rolled `HttpConnector` subclass for Proxmox VE
8.x, over the REST API at `/api2/json` on port 8006 (self-signed TLS by
default). It is the **one read/write connector** of Initiative #2228 (the
other five are read-only); it has **no code-level GET gate** — write
authorisation leans entirely on MEHO's policy gate + approval queue.

Package: `backend/src/meho_backplane/connectors/proxmox/`. Task #2238.

## Overview

Proxmox VE exposes a broad, uniform REST surface, so the connector does not
hand-curate per-endpoint ops. Instead it ships a generic `<METHOD> <path>`
passthrough plus a small identity/task surface:

| op_id | safety | approval | endpoint |
|---|---|---|---|
| `proxmox.about` | safe | no | `GET /version` + `GET /nodes` |
| `proxmox.api.get` | safe | no | `GET`/`HEAD <path>` (allowlisted) |
| `proxmox.task.status` | safe | no | `GET /nodes/{node}/tasks/{upid}/status` |
| `proxmox.api.write` | dangerous | **yes** | `POST`/`PUT`/`DELETE <path>` (allowlisted) |

The agent composes the relative path (`cluster/resources`,
`nodes/pve/qemu/100/status/start`, …); the connector prepends the constant
`/api2/json` base and dispatches. This is the "dumb substrate, smart agent"
shape — no per-endpoint DSL.

## Key types

- `ProxmoxConnector` (`connector.py`) — registry triple
  `("proxmox", "8.x", "proxmox-api")`, `priority=1`. Owns auth, fingerprint,
  probe, the read/write handlers, and `register_operations`.
- `ProxmoxCredentials` (`session.py`) — resolved credential material,
  discriminated by `mode` (`"token"` or `"ticket"`).
- `ProxmoxOp` (`ops.py`) — the op-metadata dataclass the registrar splats into
  `register_typed_operation`.
- `ProxmoxPathError` / `ProxmoxMethodError` (`ops.py`) — handler-layer
  allowlist rejections (both `ValueError` subclasses → `connector_error`).

## Control flow

### Auth (token preferred over ticket)

`session.load_credentials_from_vault` reads the target's `secret_ref` via the
shared operator-context `load_vault_secret_data`, then discriminates by stored
fields:

- **API token (preferred)** — `token_id` + `token_secret` present →
  `Authorization: PVEAPIToken=<token_id>=<token_secret>`. API tokens are
  **CSRF-exempt**, so writes need no extra header — the load-bearing reason
  token auth is preferred for a read/write connector.
- **Ticket (fallback)** — `username` + `password` (optional `realm`, default
  `pam`) → one `POST /api2/json/access/ticket` mints a `ticket` +
  `CSRFPreventionToken`. The ticket rides as the `PVEAuthCookie` cookie and,
  because ticket auth is **not** CSRF-exempt, `_write_json` attaches the
  `CSRFPreventionToken` header on every write.

Token material wins when both are stored. Credentials (and, for ticket auth,
the minted session) are cached under the tenant-unique `(tenant_id, id)` key;
the system-operator fast-path carve-out re-runs the loader for
background/operator-less callers (fail-closed).

### Passthrough allowlist (two layers)

A `<METHOD> <path>` passthrough is not an open proxy — modelled on holodeck's
read-only `kubectl` guard:

1. **Schema layer** — `path` carries an anchored `\A … \Z` `pattern`
   (`API_PATH_PATTERN`) admitting only a relative path from a safe character
   class, rejecting a leading `/` and any `..` segment; `method` is a per-op
   enum. Runs in the dispatcher's `validate_params` before the handler.
2. **Handler layer** — `validate_api_path` / `validate_method` re-check and
   fail closed. This is the **authoritative** gate; the method allowlist
   (`READ_METHODS` vs `WRITE_METHODS`) enforces the read/write split at the
   transport boundary so GET/HEAD can never reach the write handler.

`API_BASE` (`/api2/json`) is a constant prepended by the handler, never a
parameter.

### Approval-gated writes

`proxmox.api.write` registers `safety_level="dangerous"`,
`requires_approval=True`. The dispatcher's policy gate routes a USER-principal
dispatch to the human approve-queue (`awaiting_approval`) and floors an agent
dispatch to needs-approval; the handler runs only on the `_approved=True`
resume path. No bypass, no hard-deny — the descriptor flag is the only knob
the connector controls.

### Async writes → UPID → task poll

Most Proxmox writes run as background tasks and return a `UPID` string.
`proxmox.api.write` returns `{data, upid, node}` (parsing the UPID's second
colon field as the node). The agent follows it with `proxmox.task.status`
(`wait=true`), which polls `GET /nodes/{node}/tasks/{upid}/status` until
`status == "stopped"` (terminal) or the bounded timeout, returning the
`exitstatus` (`OK` on success).

### Fingerprint / probe

`fingerprint` issues `GET /version` (→ `version` / `release` / `repoid`) and
`GET /nodes` (→ per-node `[{node, status}]`). Both require authentication (PVE
has no unauthenticated identity endpoint), so a background/`operator=None`
call runs under the synthesised system operator and fails closed → `reachable=
False`. `probe` delegates to `fingerprint`.

### Self-signed TLS

Inherited from `HttpConnector`: the operator pins a CA (`tls_ca_pin` —
verification stays on) or opts out per-target (`verify_tls=false`). No
connector-level TLS handling.

## Dependencies

- `connectors/adapters/http.py` — pooled `httpx` client, retry, per-target TLS
  trust, SSRF guard.
- `connectors/_shared/vault_creds.py` — `load_vault_secret_data`,
  `strip_credential_value`, the two-phase Vault error contract.
- `connectors/_shared/system_operator.py` — `synthesise_system_operator` /
  `is_system_operator`.
- `operations/typed_register.py` — `register_typed_operation`; the registrar is
  queued in `__init__.py`.
- Registry v2 dual registration (versioned + wildcard) in `__init__.py`.

## Known issues / limits

- Registering `proxmox` auto-adds it to the `TargetCreate.product` enum
  (derived from `_registered_products()`); the OpenAPI snapshot + generated Go
  client are regenerated to match.
- Tickets last ~2h; the connector caches the minted ticket and does not yet
  auto-re-mint mid-session on expiry (a 401 surfaces as a `connector_error`).
  Token auth (preferred) has no such expiry.
- Write bodies are sent form-encoded (`application/x-www-form-urlencoded`) —
  the canonical Proxmox write shape; flat scalar params.

## References

- Task #2238; Initiative #2228; write-surface mold
  `connectors/argocd/ops_write_schemas.py`.
- Proxmox VE API: https://pve.proxmox.com/wiki/Proxmox_VE_API ; API tokens:
  https://pve.proxmox.com/wiki/User_Management#pveum_tokens ; API viewer:
  https://pve.proxmox.com/pve-docs/api-viewer/
