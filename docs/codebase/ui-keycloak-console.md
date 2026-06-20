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

## Authoring writes (T3, #1961)

`ui/routes/keycloak/write.py` adds the two approval-gated authoring writes
the read scaffold leaves to T3 — built as a sibling router
(`build_keycloak_write_router`) so the read scaffold stays focused and the
merge-conflict surface with the concurrent user-management task (T2, #1960)
stays small. `build_keycloak_router` includes the write router, so the whole
`/ui/keycloak*` surface ships as one router.

| Method · path | Op dispatched |
|---|---|
| `GET/POST /ui/keycloak/client-scopes/create` | `keycloak.client_scope.create` |
| `GET/POST /ui/keycloak/clients/{client_uuid}/protocol-mappers/create` | `keycloak.protocol_mapper.create` |

Both ops register `safety_level="caution"` / `requires_approval=True`
(`connectors/keycloak/ops_write.py`). The protocol-mapper op is the one that
wires the `tenant_id` / `tenant_role` claims the backplane row-scopes on.

Each write is reached only from an explicit, unmissable confirm modal (the
`GET` render) that names the caution safety level and the requires-approval
handoff before the operator can `POST`. The flow mirrors the merged
`/ui/operations` Run modal exactly:

1. The modal-render `GET` mints a fresh CSRF token and re-sets the
   `meho_csrf` cookie (`mint_csrf_token` / `set_csrf_cookie`) so the
   double-submit pair lines up after the HTMX swap rotated it.
2. The confirm form carries the token on its **own** `hx-headers`
   (HTMX does not inherit `hx-headers` to children). Every `POST` under
   `/ui/` is CSRF-gated by the `ui/csrf.py` double-submit middleware
   regardless; a missing/invalid token is a hard `403`.
3. The `POST` builds the op `params` and dispatches in-process through
   `call_operation` against the pinned `connector_id`.

**Client keying (load-bearing).** The protocol-mapper target client is keyed
off the route's `{client_uuid}` path segment (the client's internal UUID),
**not** a free-form field — the dispatch `params` carry
`{"representation": {...}, "id": client_uuid}`, so a forged form value cannot
re-point the write at another client. The client-scope create carries only
`{"representation": <ClientScopeRepresentation>}`.

**Approval handoff.** Because both ops are `requires_approval=True`, the
dispatcher's policy gate returns `status="awaiting_approval"` with
`extras["approval_request_id"]` — the write never executes immediately. The
result fragment (`keycloak/_write_result.html`) surfaces that id and
**deep-links `/ui/approvals`** so the operator hands off to a reviewer
rather than seeing a silent success. Implementing the approval *decide* flow
is out of scope ([#1778](https://github.com/evoila/meho/issues/1778)); this
surface only deep-links.

**RBAC (soft-hide + hard-403).** The underlying dispatch is operator-tier
with a policy gate; `requires_approval` (not RBAC) is what routes both writes
to `awaiting_approval`. This BFF layer adds a `tenant_admin` gate on top for
the writes, matching the existing UI write routes (connectors / agents /
conventions). The create affordances on the read surfaces are soft-hidden
from a plain operator via `resolve_role_probe` (`is_tenant_admin`); the
confirm-modal `GET`s **and** the write `POST` handlers gate server-side with
`_resolve_keycloak_admin_or_403` (a keycloak-local twin of
`resolve_operator_or_403` with a keycloak-appropriate 403 detail), so a
forged POST from a non-admin returns `403` even when the affordance was
hidden. The scope ↔ client relation view stays operator-tier.

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

- Read scaffold (T1) + client-scope/protocol-mapper authoring (T3, #1961);
  user management is T2 (#1960).
- The authoring forms collect the common representation fields directly
  (name / protocol / mapper type) plus an optional advanced JSON block for
  the rest (attributes, embedded `protocolMappers`, the mapper `config`);
  the explicit fields win over the JSON so a typo in the advanced block
  cannot silently drop a named field.
- No protocol-mapper *read* verb exists — mappers are read via
  `keycloak.client.get` (they ride the `ClientRepresentation`); the scope ↔
  client relation view is read-only assembly over T1's reads.
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
