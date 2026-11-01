# Targets — listing and the result-handle threshold

## Overview

A **target** is a per-tenant registered endpoint an agent or operator
can act on: a vCenter, a Kubernetes API server, a Vault, an SSH host.
Rows live in the `targets` table (`backend/src/meho_backplane/db/models.py`,
class `Target`); `name` is unique among a tenant's live rows and is the
identifier every `target` argument resolves by.

Targets are reached through three fronts that share the table, not
each other's code:

| Front | Where | Purpose |
|---|---|---|
| REST `/api/v1/targets` | `backend/src/meho_backplane/api/v1/targets.py` | list, discover, describe, probe, create, patch, soft-delete (the last four by name, alias or id) |
| CLI `meho targets …` | `cli/` (Go) over the REST routes | operator verbs; `meho targets list` calls `GET /api/v1/targets`, `meho targets delete` calls `DELETE /api/v1/targets/{name}` |
| MCP `list_targets` | `backend/src/meho_backplane/mcp/tools/topology.py` | working-surface enumeration for agents |

`meho_targets_register` (`mcp/tools/targets_register.py`) is the
operator-plane MCP write. It runs the `targets.register` operation, whose
params schema is `TARGETS_REGISTER_PARAMETER_SCHEMA`
(`connectors/targets/schemas.py`): required `name`, `product`, `host`;
optional `aliases`, `version`, `port`, `fqdn`, `secret_ref`,
`auth_model`, `verify_tls`, `tls_ca_pin`, `tls_server_name`,
`vpn_required`, `extras`, `notes`, `preferred_impl_id`. Any other field
is refused. `vpn_required` (boolean, default `false`) records that a
VPN must be up to reach the target. MEHO does not act on it; it is
shown to the people and agents that read the target (#5075). Every
`target` argument elsewhere resolves
through `resolve_target` (`targets/resolver.py`). Destination checks
are covered in [`target-ssrf-guard.md`](target-ssrf-guard.md).

This document covers the two **list** paths, how `list_targets`
bounds its response (#3858), how the single-target REST routes find a
target, the name and host rules for new targets (#5244), the per-target
**secret field map** (#3711), how a target is removed, and what a target
change does to the logins connectors keep (#3879).

## Finding one target: describe, update, probe, delete

These four routes take a name, an alias or an id in the path (#5245):

- `GET /api/v1/targets/{value}`
- `PATCH /api/v1/targets/{value}`
- `POST /api/v1/targets/{value}/probe`
- `DELETE /api/v1/targets/{value}`

All four call `_resolve_route_target` in `api/v1/targets.py`. It tries,
in this order:

1. Exact name, then exact alias, through `resolve_target`. Everything
   that worked before works the same way.
2. The id, through `resolve_target_by_id`. This step runs only when
   step 1 found nothing and the value is a valid UUID. So a target
   named like a UUID is still found by its name first.

The id step finds only a live (not deleted) target of the caller's
tenant. The id of another tenant's target, or of a deleted target,
gives the same 404 as an unknown name. When a UUID matches nothing, the
404 body (`error: no_target`) also has
`message: "No target has this name, alias or id."`.

`resolve_target` binds `target_id` and `target_name` for the audit row
when it finds a target; `resolve_target_by_id` does not. So the helper
binds them after an id match. A delete by id writes the same audit row
as a delete by name: `target_id` set, op id `targets.delete`.

Only these four routes take an id. The `target` argument of
`call_operation` and every other caller of `resolve_target` still take
a name or an alias only. Tests: `tests/test_api_v1_targets_by_id.py`.

## Key types

| Symbol | Where | Role |
|---|---|---|
| `Target` | `db/models.py` | ORM row, including the cached probe `fingerprint`, `extras`, and the per-target `tls_ca_pin` |
| `TargetSummary` / `TargetListResponse` | `targets/schemas.py` | REST list row and `{items, next_cursor}` envelope |
| `project_target_to_summary` | `targets/schemas.py` | the one ORM → `TargetSummary` projection (REST list + resolver diagnostics) |
| `_list_targets_handler` | `mcp/tools/topology.py` | MCP handler: tenant scope, query, page |
| `_reduce_target_page` | `mcp/tools/topology.py` | runs the page through JSONFlux |
| `JsonFluxReducer` | `operations/jsonflux_reducer.py` | the reducer `call_operation` results go through; thresholds and handle shape |
| `ResultHandleStore` | `connectors/result_handle_store.py` | Valkey-backed spill that `result_query` reads back |

## Control flow — changing a target

`PATCH /api/v1/targets/{name}` (`update_target`) changes only the sent
fields. Connectors keep logins and credentials per target, keyed on
`(tenant_id, id)`, and a change does not alter that key. So when the
PATCH sends a field that changes how MEHO reaches or logs in to the
target (`_CONNECTION_FIELDS`: `product`, `version`, `preferred_impl_id`, `host`,
`port`, `fqdn`, `secret_ref`, `auth_model`, `verify_tls`, `tls_ca_pin`,
`tls_server_name`, `extras`), the route calls `forget_target_in_connectors(target)`
(`operations/_handler_resolve.py`, through
`_drop_kept_logins_on_connection_change`).

That helper asks every cached connector instance to run its
`invalidate_session` and `invalidate_credentials` hooks for the
target, when it has them. The next dispatch logs in with the new
values (#3879). A hook that fails is logged and skipped.

The check is "field sent", not "value changed". After a secret changes
only in Vault, re-send the same `secret_ref`
(`meho targets update <name> --secret-ref <same path>`) to make MEHO
read it again.

Limits:

- The kept logins live in each backplane process. The drop above runs
  only in the process that served the PATCH. Every other process (a
  second backplane server, a satellite runner) notices the change on
  its next call: the dispatcher and the runner executor call
  `forget_target_if_changed` with the target they just read. It keeps,
  per process, the `updated_at` it saw last for each target. The update
  route always refreshes `updated_at`, also when the same `secret_ref`
  is sent again. A new value drops the kept logins once, then the next
  login uses the new values. The runner gets `updated_at` in its target
  descriptor from the gateway. The check and the drop run under a lock
  per target, and the new `updated_at` is recorded only when every hook
  is done. So a call that arrives while a hook still runs waits for the
  drop, instead of treating the version as handled and reusing the old
  login. Paths that call a connector without the dispatcher (topology
  refresh, scope repair) do not run this check.
- The handler does not commit on its own: the request's session
  commits after the response is sent, and the UI import calls it for
  many targets in one transaction. So the drop runs twice: once in the
  handler, and once more after the commit, from a one-time SQLAlchemy
  `after_commit` listener on `session.sync_session`
  (`_drop_again_after_commit`). The second drop catches a login kept by
  a dispatch that read the old row before the commit. A login that
  started before the change and finishes after the second drop can
  still be kept, except for SSH: `SshConnector` counts drops per
  target. A call reads the count when it starts, checks it again once
  it holds the connect lock, and once more after the handshake. When
  the count moved, the call raises `SshTargetChangedError`: a call that
  was waiting for the lock does not log in with its old target values,
  and a login that finished after the drop is closed instead of landing
  in the pool.
- The Proxmox hook drops both the credential and the login ticket
  minted from it; dropping only the credential would still send the
  old ticket.
- A connector with no hook keeps its login until it expires. The
  pooled HTTP client is not affected: its key already includes the
  host, port, scheme and TLS settings.

## Control flow — listing targets

### REST and CLI: `GET /api/v1/targets`

1. Tenant comes from the JWT; soft-deleted rows (`deleted_at IS NOT
   NULL`) are excluded.
2. Keyset pagination on `name`: `limit` default 100, max 500; the
   route over-fetches `limit + 1` so `next_cursor` is non-null only when
   another page exists (#1312).
3. Each row is projected to the full `TargetSummary` (including
   `fingerprint`, `secret_ref`, TLS fields) and returned as
   `{items, next_cursor}`.

This path is **not** reduced. It is the operator surface: the CLI
renders the table and pages with `--cursor`, and the rows carry fields
the MCP projection omits. The JSONFlux bound protects an agent's
context window, which the REST front does not feed.

### MCP: `list_targets`

1. **Tenant scope.** No `tenant_id` → the caller's tenant. A slug or
   UUID naming another tenant requires `platform_admin` (#1641).
2. **One query.** A single SELECT of the five projected columns —
   `id`, `name`, `aliases`, `product`, `host` — keyset on `name`,
   `limit` default 100 / max 500, optionally narrowed by the product
   component of `connector_id`. No joins, no per-row follow-up query,
   and the `fingerprint` / `extras` JSON and CA pin are never loaded.
3. **JSONFlux.** The page `{targets, next_cursor}` goes through
   `JsonFluxReducer` with the same default thresholds as every
   `call_operation` result (CLAUDE.md postulate 6):
   - **At or under** (≤ 50 rows and ≤ 4 KB serialized): returned
     unchanged.
   - **Over**: `targets` is absent. The response is the reducer summary
     — `row_count`, `total`, `sample_rows_returned`, `sample_bytes`,
     `source_key: "targets"`, and `next_cursor` (kept as a preserved
     scalar) — plus `handle`, the same `ResultHandle` shape
     `call_operation` returns: `sample_rows` (at most 5 rows within the
     4 KB `jsonflux_sample_byte_budget`), `schema_`, `summary_md`, and
     `fetch_more`. `fetch_more.drill_in` names `result_query`;
     `fetch_more.native_pagination` names `connector_id` / `limit` /
     `cursor` for re-calling `list_targets`.
   - `row_count` and `total` count **this page**, not the tenant.
     `next_cursor` still continues the listing.
4. **Spill key.** The handle is spilled under the **caller's** tenant
   and subject, even when a `platform_admin` listed another tenant:
   `result_query` reads back under the JWT's tenant and subject, so that
   is the only key the caller can drill into.
5. The MCP dispatcher (`mcp/handlers.py`) packs the result as a text
   block **and** `structuredContent` (#2774), then writes the audit row
   and publishes the broadcast event.

An agent reads a reduced page with `result_query(handle_id=…)`: page
with `offset` / `limit`, or pass a `query` such as
`{"select": ["name", "product"]}` or a `product` filter. Narrowing with
`connector_id`, or a smaller `limit`, returns inline rows instead.

Response sizes measured locally for #3858 (in-process test app, SQLite,
rows about 165 bytes each):

| Page | Before (HTTP body / text block) | After |
|---|---|---|
| 89 rows, default `limit` | 30.5 KB / 14.7 KB | 5.1 KB / 2.4 KB |
| 10 rows (`limit=10`) | 3.6 KB / 1.7 KB | unchanged (inline) |
| 500 rows (`limit=500`) | 173 KB / 84 KB | 5.1 KB / 2.4 KB |

End-to-end call time (warm median of 7 calls each, same setup) was
about the same in both versions: about 6–8 ms for an 89-row page and
about 12 ms for a 500-row page.

## Name and host rules for new and changed targets

`TargetCreate` and `TargetUpdate` (`targets/schemas.py`) check the name,
the aliases, the host and the fqdn. The checks live in
`targets/field_rules.py` (#5244). All write paths build one of these two
models, so the same rules apply to `POST` and `PATCH /api/v1/targets`,
`meho targets add`, `meho targets import` and `targets.register`. A broken
value gives a 422 that names the field.

**Name and each alias:**

- must not be blank;
- must not contain a space, a tab, a line break or any other control
  character;
- must not contain `/`. The routes take the name as one path part, so a
  target with `/` in its name could not be read, changed or deleted.

**Host and fqdn:**

- The check looks at the host part MEHO connects to, the same part the
  SSRF guard screens (`_dialed_host` in `targets/ssrf_guard.py`). So a port
  after the host (`db-01:27017`) and a path after the host (`owner/repo`,
  `api.github.com/repos/owner/repo`) still work.
- The host part must be a valid IPv4 address, a valid IPv6 address, or a
  valid DNS host name: parts of letters, digits and `-`, each 1 to 63
  characters long, not starting or ending with `-`. The whole name, in its
  encoded (ASCII) form, must be at most 253 characters. A trailing dot is
  fine and does not count.
- When every part is a number, the value is an IPv4 address and must be
  complete and valid. `10.0.0.` and `10.0.0` are refused.
- The format check runs first. The SSRF guard then runs unchanged. The
  format check does no DNS lookup, so its message never shows an address.

**Targets saved before these rules:** nothing changes. They still resolve
by name or alias and can still be changed or deleted by name. Tests:
`tests/test_targets_name_host_rules.py`.

## Secret field map (#3711)

### What it is for

Many connectors read fixed field names from the target's secret. The SSH
family reads `ssh_private_key`, `username` and `password`. The vSphere
guest operations read `guest_username` and `guest_password`. A secret that
already holds the right values under other names could not back the
target before this change. The only way out was a copy of the secret with
new key names.

A target can now carry `secret_field_map`: a map from the field name the
connector reads to the field name the secret really uses.

```json
{"ssh_private_key": "private_key", "guest_username": "username", "guest_password": "password"}
```

When a connector reads field `X`, MEHO reads the secret's field
`map.get(X, X)`. Fields that are not in the map are read by their own name,
as before. The map holds field names only, never a value. Connector code
does not change and does not know the map exists.

### Where it lives

| Piece | Where |
|---|---|
| Column `targets.secret_field_map` (JSON, nullable, `NULL` = no map) | `db/models.py`, migration `0128` |
| `secret_field_map` on `Target`, `TargetSummary`, `TargetCreate`, `TargetUpdate` | `targets/schemas.py` |
| Applying the map when a secret is read | `connectors/_shared/vault_creds.py` (`secret_field_map_of`, `_apply_secret_field_map`) |
| PATCH merge rules and the probe check | `targets/secret_field_map.py` |
| Map carried to a satellite runner | `runner/wire.py` (`ResolvedTargetDescriptor`), `gateway/assignment_service.py` |

The map is applied in `load_basic_credentials` and
`load_vault_secret_data`, so it works for every credential backend
(`vault:`, `gsm:`). It is applied exactly once. A `wrapped:` credential for
a satellite runner is brokered at the centre from the mapped view of the
secret, so the runner that unwraps it does not apply the map again. The map
only applies to the target's **own** secret. Readers that build a small stand-in target around another secret
ref (for example `guest_secret_ref` or a `password_secret_ref` op
parameter) do not copy the map, so it never applies there.

If a mapped source field is missing from the secret, the connector field is
missing too. `load_basic_credentials` then fails with an error that names
both field names (never a value).

### Writing the map

- **Create** (`POST /api/v1/targets`): `secret_field_map` is an object of
  field names. Names are 1 to 200 characters; at most 50 entries.
- **Update** (`PATCH /api/v1/targets/{name}`): entries are **merged** into
  the stored map. A string value adds or replaces that entry. A `null`
  value removes that entry. `"secret_field_map": null` removes the whole
  map. Leaving the field out keeps the map as it is.
- **CLI**:
  - `meho targets add <name> ... --secret-field <connector_field>=<source_field>`
    (repeatable).
  - `meho targets update <name> --secret-field <connector_field>=<source_field>`
    adds or replaces one entry. `--secret-field <connector_field>=` removes
    one entry. `--clear secret-field` removes the whole map.
  - `meho targets import <file>` reads a `secret_field_map:` key on each
    entry. `--secret-field <connector_field>=<source_field>` (repeatable)
    adds the entry to every target in the file and wins over the YAML.
  - `meho targets describe` shows the map as
    `secret_field_map: connector_field=source_field, ...`.

Example for an SSH host whose secret, written by an automation pack, holds
`private_key` and `login`:

```bash
meho targets add web-01 --product linux --host web-01.example.com \
  --secret-ref tenants/<tenant-id>/web-01 \
  --secret-field ssh_private_key=private_key --secret-field username=login
meho targets probe web-01
```

### Check at probe time

`POST /api/v1/targets/{name}/probe` (`meho targets probe`) reads the
target's secret when the target has a map, and checks that every mapped
**source** field exists. If one is missing, the probe fails with a 422 and
does not save a fingerprint:

```json
{"detail": {"kind": "secret_field_map_source_missing",
            "missing_source_fields": ["login"],
            "message": "target 'web-01': the secret has no field named 'login', ..."}}
```

The error holds field names only. A target with no map skips this read. A
failed secret read during the check is reported like any other probe
failure (500, `fingerprint_failed`).

### SSH `private_key` alias

As a shortcut for the most common case, the SSH connector
(`connectors/adapters/ssh.py`, `_auth_config`) also accepts a field named
`private_key` when `ssh_private_key` is absent. A secret written by an
automation pack therefore works for SSH key login with no map at all. When
both fields are present, `ssh_private_key` wins.
## Control flow — deleting a target

`DELETE /api/v1/targets/{name}` (`tenant_admin`) and the CLI verb
`meho targets delete <name> [--force] [--confirm]` (#3943) on top of it:

1. The CLI asks y/N first unless `--confirm` is given. Closed stdin
   counts as no, so a script never deletes by accident.
2. The route resolves the name or alias in the caller's tenant (404
   with near-misses, 409 on an ambiguous alias).
3. It counts the `graph_node` rows that point at the target. Audit rows
   are not counted: every request against a target writes one.
4. When that count is above zero and `force` is not set, it answers 409
   with `detail.kind = "target_has_references"` and
   `detail.graph_node_refs`. The CLI prints the count and says to run
   again with `--force`.
5. Otherwise it sets `deleted_at` (a soft delete) and answers 204. The
   row stays, so audit rows still point at it; every read path filters
   it out. With `force`, the topology nodes stay and keep the deleted
   target's id in `target_id`. The FK's `ON DELETE SET NULL` only acts
   on a hard delete, which this route never does.

## Removing a target (`targets.remove`)

An agent or a person removes a target with
`call_operation(connector_id="targets-registry-1.x", op_id="targets.remove",
params={"target_id": "<target-uuid>"})`. It is the opposite of
`targets.register` (#4623).

- **One delete service.** The op and `DELETE /api/v1/targets/{name}` both
  call `soft_delete_target` in `api/v1/targets.py`. It counts the
  `graph_node` rows that point at the target and refuses when the count is
  not zero, unless `force` is true. Then it stamps `deleted_at`. The REST
  route looks the target up by name, alias or id (see above); the op
  looks it up by id (`resolve_target_by_id`). Both look only in the
  caller's tenant.
- **Result.** `{status, target_id, name}`. `status` is `removed`, or
  `unchanged` when no live target with that id exists in the caller's
  tenant (also for a target of another tenant). A second run gives
  `unchanged`.
- **What stays.** Only MEHO's record goes. The system, its Vault secret and
  its checks stay.
- **Role.** Only a login with the `tenant_admin` role can remove a target,
  the same rule as the REST route. Any other role gets an error and the
  target stays. A call that waits for approval runs, once approved, under
  the login of the person who approved it, so that person's role counts.
- **Approval.** `dangerous` with `requires_approval=True`; the name ends in
  `.remove`, so the delete rules apply. A second person must approve every
  call. See [`mcp.md`](mcp.md#admin-registry-writes-route-through-the-dispatcher).
- **Preview.** `preview_operation` shows the target (id, name, host), its
  product and the `graph_node` count, and says whether the call would
  remove the target, fail, or return `unchanged`.
- **Audit.** Every call writes an audit row with op id `targets.remove`.
  The REST route keeps its own op id, `targets.delete`.
- Code: `connectors/targets/ops.py` (handler, preview, registration) and
  `connectors/targets/schemas.py`. Tests: `tests/test_targets_remove_op.py`.

## Dependencies

- SQLAlchemy 2.0 async sessions. A column select returns `Row` objects
  with attribute access by column name, not ORM instances.
- `JsonFluxReducer` and `ResultHandleStore`, which shares the broadcast
  Valkey client (`BROADCAST_REDIS_URL`). If the store cannot persist the
  spill, the handle still ships with `drill_in.available=false` and
  `reason="result_store_unavailable"`; the agent falls back to
  `native_pagination` (narrow with `connector_id`, or a smaller
  `limit`). See [`result-spill.md`](result-spill.md).
- Settings: `jsonflux_sample_byte_budget` (preview size),
  `result_handle_max_spill_rows` (spill cap, default 10000, above the
  500-row page maximum).

## Known issues

### #3858 — default `list_targets` timed out and returned inline rows above the threshold

**Symptom (field test #3143 F10).** On a tenant with 89 targets, the
default-params `list_targets` call from Claude Desktop timed out twice,
while 10-row pages returned instantly. The call also returned all 89
rows inline, above the size at which postulate 6 says a set-shaped
result becomes a handle.

**Root cause.**

- *Server-side latency was not the cause.* The suspected per-row
  fingerprint or probe joins do not exist. The MCP handler (and the
  REST route) ran one keyset SELECT; there was no N+1. Reproduced
  locally, the old 89-row default call had a warm median of about 6 ms
  (7 calls, in-process test app, SQLite). The handler did hydrate whole ORM rows, decoding each target's
  `fingerprint` / `extras` JSON and CA pin only to project five fields;
  that per-row decode is small but was the only per-row work on the
  path.
- *What scaled with the row count was the response size.* The handler
  bypassed JSONFlux. Only `call_operation` went through the reducer, so
  `list_targets` inlined every row, and the dispatcher emits the payload
  twice (text block and `structuredContent`). The 89-row default call
  put about 30 KB on the wire, against about 3.5 KB for a 10-row page;
  response size was the one input that differed between the calls that
  timed out and the calls that returned instantly. The size-dependent
  cost is on the client path (Desktop, the `mcp-remote` stdio bridge,
  the model's context). The field session's audit rows, which would
  confirm the server-side duration of the two timed-out calls, were not
  readable from the implementing session.

**Fix.** `list_targets` now reduces an over-threshold page through the
same `JsonFluxReducer` `call_operation` uses and returns a summary plus
`handle`, as described under [MCP: `list_targets`](#mcp-list_targets).
The query now selects the five projected columns only. The
`outputSchema` declares the reduced fields and no longer requires
`targets`. Tool name, arguments, the default `limit` of 100, and the
inline shape for small pages are unchanged. REST and the CLI are
untouched.

**Regression tests.** `backend/tests/test_mcp_list_targets_result_handle.py`
covers: an 89-row default page returns a handle and no inline rows,
within 4 KB; all rows read back through `result_query` (paging and
query); small pages stay inline; a `platform_admin`'s cross-tenant
handle is readable by the caller; and the listing is one SELECT with no
`fingerprint` or CA-pin column.
`test_list_targets_reduced_page_conforms` in
`backend/tests/test_mcp_output_schema_conformance.py` validates the
reduced payload against the declared `outputSchema`, which Claude
Desktop enforces client-side.

**Pending human verification.** The live check (default `list_targets`
under 5 s from Claude Desktop on a tenant with 89 or more targets) needs
a Desktop session against a deployed backplane running this release.

### MCP `next_cursor` on an exactly full last page

`list_targets` sets `next_cursor` whenever the page fills `limit`, so a
tenant whose target count is an exact multiple of `limit` gets one extra
empty follow-up page. The REST route avoids this by over-fetching
`limit + 1` (#1312). Not changed by #3858.

## References

- #3858, field test #3143 (F10).
- CLAUDE.md postulate 6 (JSONFlux and result handles); v0.1-spec §4.
- [`result-spill.md`](result-spill.md) (spill and `result_query` read-back);
  [`../architecture/jsonflux.md`](../architecture/jsonflux.md) (reducer contract).
- [`mcp.md`](mcp.md) (tool inventory, `structuredContent` emission, #2774).
- [`api-shape-conventions.md`](api-shape-conventions.md) §14 (MCP list-tool
  `limit` / `cursor` parity).
- #3711 (secret field map). Related work: #3364 (`guest_secret_ref` picks
  *which* secret a guest operation reads; the map picks *which field*).
- #134 (preview serialized once, on `handle.sample_rows`); #1312 (REST
  `limit + 1` over-fetch); #1641 (cross-tenant listing gated to
  `platform_admin`).
