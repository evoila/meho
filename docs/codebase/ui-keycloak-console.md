# `ui/routes/keycloak` — the read-only Keycloak realm browser

Initiative [#1943](https://github.com/evoila/meho/issues/1943) (G10.x
Keycloak console), Task [#1959](https://github.com/evoila/meho/issues/1959)
(T1). The read-only scaffold the two write tasks (T2 user management, T3
scope/mapper management) build on. It gives an operator a domain-shaped
view of the managed Keycloak realm — realm configuration, clients (with a
per-client detail drill-in), and client scopes — directly in the operator
console, where before the only way to read realm config from MEHO was the
`meho keycloak ...` CLI.

## Overview

Two routes, both `require_ui_session`-gated (GET reads need no CSRF):

| Method · path | Role | Purpose |
|---|---|---|
| `GET /ui/keycloak` | operator | Full-page realm browser: a tenant-scoped target picker + realm-config card + client list + client-scope list. |
| `GET /ui/keycloak/clients/{client_uuid}` | operator | Client-detail fragment (HTMX-swapped into a drawer container), keyed on the client's **internal UUID**. |

## Control flow

This surface is **pure UI/BFF assembly** — it adds no backend route and no
meta-tool. There is no `/api/v1/keycloak` REST surface at all: every CLI
keycloak verb is a thin Cobra layer over `POST /api/v1/operations/call`
against a pre-baked connector id, so Keycloak admin flows ride the
operations dispatcher. This console dispatches the curated `keycloak.*`
**read** ops in-process through
`operations.meta_tools.call_operation`, the same in-process pattern the
`/ui/operations` console uses, then renders a domain-shaped UX instead of
the generic op drawer. A browser carrying only the BFF session cookie
cannot authenticate the Bearer-gated REST route, so the in-process call is
the only path.

`GET /ui/keycloak`:

1. Lift the full `Operator` from the BFF session (re-verify the access
   token through the JWT chain — picks up a same-session role demotion).
2. List the tenant's `product == "keycloak"` targets (the picker options).
3. Resolve the active target: an explicit in-list `?target=` selection
   wins; otherwise default to the sole target when exactly one exists.
4. When a target is active, dispatch `keycloak.realm.get` +
   `keycloak.client.list` + `keycloak.client_scope.list` and read the
   named projections off each envelope's `result`.

`GET /ui/keycloak/clients/{client_uuid}` dispatches `keycloak.client.get`
with `params={"id": client_uuid}` and renders projected fields.

## Pinned connector id (load-bearing)

Every dispatch pins `connector_id` to the module constant
`KEYCLOAK_CONNECTOR_ID = "keycloak-admin-26.x"` — never typed by the
operator, never re-derived from the bare product slug `keycloak`. The bare
slug parses to `(product="keycloak", version="", impl_id="")`
(`operations/_lookup.py::parse_connector_id`), which names no registered
connector and dead-ends the dispatch with an unknown-connector fault. The
slug the operator *does* select is the **target** slug (which Keycloak
deployment to read), carried on the query string; the connector is fixed.
A re-versioning (`keycloak-admin-27.x`) is a one-line edit on the
constant, mirroring the CLI's `ConnectorID`.

## Secret redaction is upstream, not here (load-bearing)

Every keycloak read op scrubs nested secrets at the connector boundary
(`connectors/keycloak/redaction.py::redact_secret_fields`, the
`***REDACTED***` sentinel) before the result leaves the connector. The UI
**trusts** this and never re-exposes the raw envelope: the templates
render projected, named fields (clientId, enabled, redirect URIs, protocol
mappers, …) and never dump the verbatim `OperationResult` blob into the
page — so a future op that forgot to scrub a field cannot leak it through
a raw-blob render here.

## RBAC: reads are operator-tier

The dispatch is gated only by `require_ui_session`; the underlying
`POST /api/v1/operations/call` floor is `TenantRole.OPERATOR`, so a plain
operator can read every surface here. The page threads an
`is_tenant_admin` flag (via the soft-failing `resolve_role_probe`) so T2/T3
can hide their write affordances from operators — but a non-admin render
must succeed.

## Tenant isolation

The target list and every dispatch derive `tenant_id` from the validated
session only. There is **no** tenant-override query/form param: a
cross-tenant target slug is simply absent from the operator's target list
and drives no dispatch — the no-tenant-override posture the kb router
documents. A cross-tenant target/realm selector is deferred console-wide
([#865](https://github.com/evoila/meho/issues/865)).

## Cross-link, not duplicate

The client-detail view links to `/ui/agents/principals` (the agent
register/revoke kill switch,
[#1831](https://github.com/evoila/meho/issues/1831)/[#1866](https://github.com/evoila/meho/issues/1866)),
which stays the canonical agent-principal surface via
`auth/keycloak_admin.py`. This console only reads.

## Route ordering

`build_keycloak_router` registers the literal
`/ui/keycloak/clients/{client_uuid}` route before the bare `/ui/keycloak`
index; the only `{param}` route sits under the distinct
`/ui/keycloak/clients/` prefix, so a future literal `/ui/keycloak/users`
(T2) registered ahead of any `{param}` route binds first (first-match-wins).
The router is included before the stubs aggregate in
`ui/routes/__init__.py::build_router`.

## Dependencies

- `operations.meta_tools.call_operation` — the in-process dispatch entry.
- `connectors/keycloak/ops_read.py` — the curated `keycloak.*` read ops
  (`realm.get`, `client.list`, `client.get`, `client_scope.list` used here).
- `ui/routes/connectors/operator.py::resolve_role_probe` — the soft-failing
  role probe driving `is_tenant_admin`.
- `db.models.Target` — the tenant-scoped target picker source.

## Known limitations

- Read-only by design (T1). User management is T2; client-scope/mapper
  create is T3.
- Single managed realm per target (the connector resolves the realm from
  the target's `managed_realm`); no realm selector.

## References

- Task [#1959](https://github.com/evoila/meho/issues/1959), Initiative
  [#1943](https://github.com/evoila/meho/issues/1943).
- Operations console precedent:
  [#1835](https://github.com/evoila/meho/issues/1835)
  (`docs/codebase/ui.md`, `ui/routes/operations/routes.py`).
- Keycloak connector + read ops:
  `docs/codebase/connectors-keycloak.md`.
