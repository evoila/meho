# `ui/routes/keycloak` — the Keycloak realm browser + user management

Initiative [#1943](https://github.com/evoila/meho/issues/1943) (G10.x
Keycloak console), Tasks
[#1959](https://github.com/evoila/meho/issues/1959) (T1, read-only
scaffold) and [#1960](https://github.com/evoila/meho/issues/1960) (T2, user
management). It gives an operator a domain-shaped view of the managed
Keycloak realm — realm configuration, clients (with a per-client detail
drill-in), and client scopes — plus the day-to-day human-user admin
(list / create / reset-password / role-assign), directly in the operator
console, where before the only way to do this from MEHO was the
`meho keycloak ...` CLI.

## Overview

Reads are `require_ui_session`-gated only (GET reads need no CSRF); every
write is CSRF-gated by the `ui/csrf.py` double-submit middleware AND
hard-gated to `tenant_admin`:

| Method · path | Role | Purpose |
|---|---|---|
| `GET /ui/keycloak` | operator | Full-page realm browser: a tenant-scoped target picker + realm-config card + client list + client-scope list. |
| `GET /ui/keycloak/clients/{client_uuid}` | operator | Client-detail fragment (HTMX-swapped into a drawer container), keyed on the client's **internal UUID**. |
| `GET /ui/keycloak/users` | operator | Full-page user list (`keycloak.user.list`, `safe`) + optional `?username=` filter. Write affordances soft-hidden from non-admins. |
| `GET /ui/keycloak/users/create` | tenant_admin | Create-user confirm modal (CSRF token minted + cookie re-set). |
| `POST /ui/keycloak/users/create` | tenant_admin | Dispatch `keycloak.user.create` (`caution`, requires approval). |
| `GET /ui/keycloak/users/{user_uuid}/reset-password` | tenant_admin | Reset-password confirm modal. |
| `POST /ui/keycloak/users/{user_uuid}/reset-password` | tenant_admin | Dispatch `keycloak.user.reset_password` (`caution`, requires approval). |
| `GET /ui/keycloak/users/{user_uuid}/roles/assign` | tenant_admin | Role-assign confirm modal (a **privilege grant**; reads current roles via `keycloak.role_mapping.get`). |
| `POST /ui/keycloak/users/{user_uuid}/roles/assign` | tenant_admin | Dispatch `keycloak.role_mapping.assign` (`dangerous`, requires approval). |

## User management writes — confirm, approval handoff, Vault-ref passwords

The three writes (create / reset-password / role-assign) share one
contract. Each renders an **unmissable confirm** modal before the POST; the
confirm button carries the OWASP signed double-submit CSRF token on its own
form's `hx-headers` (HTMX does **not** inherit `hx-headers` to children),
and `hx-disabled-elt` blocks a double-fire. Every write op is registered
`requires_approval=True`, so the dispatcher's policy gate routes a confirmed
write to `status="awaiting_approval"` with `extras["approval_request_id"]`
rather than executing immediately. The shared `_write_result.html` fragment
surfaces that id with a **deep-link into `/ui/approvals`** — without it the
operator would think the write silently no-op'd.

Passwords are **Vault-ref, never inline**. The create and reset-password
forms collect a Vault KV path (`password_secret_ref` + optional
`password_secret_mount` / `password_secret_key` / `temporary`) and have **no
plaintext password field**. The backend reads the password from Vault at
dispatch time; the value never lands in form params, request logs, or the
audit row — mirroring the CLI's `passwordSecretFlags` bundle.

Role-assign is the **highest-blast** write: granting a realm role widens the
user's authority, so it is `safety_level="dangerous"` and the confirm banner
names it a privilege grant explicitly. `roles` is a string list of realm
role names; the dispatch params are `{"roles": [...], "id": <uuid>}` (the
grant is idempotent server-side).

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

## RBAC: reads operator-tier, writes tenant_admin

Reads are gated only by `require_ui_session`; the underlying
`POST /api/v1/operations/call` floor is `TenantRole.OPERATOR`, so a plain
operator can read every browse surface (realm/client/scope list and the
user list). The user-list page threads an `is_tenant_admin` flag (via the
soft-failing `resolve_role_probe`) so the create / reset / assign buttons
are **soft-hidden** from a plain operator — but a non-admin render must
still succeed.

The soft-hide is only a UX hint. The three write POSTs are the
server-side authority: they depend on `_resolve_admin_or_403` (a
keycloak-local mirror of `connectors/operator.py::resolve_operator_or_403`
with a keycloak-appropriate 403 `detail`), so a forged POST from a plain
operator gets a hard `403` and never dispatches. The dispatch floor itself
is operator-tier; this BFF-layer `tenant_admin` gate is layered on top for
blast radius (create/reset are caution writes, role-assign is a privilege
grant), the same pattern existing UI write routes use.

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

`build_keycloak_router` registers, in order: the user-management routes
(via `_register_user_routes` → `_register_user_password_role_routes`), then
the literal `/ui/keycloak/clients/{client_uuid}`, then the bare
`/ui/keycloak` index. The literal `users` and `users/create` segments are
registered ahead of any `{user_uuid}` route, so `create` is never captured
as a UUID (first-match-wins). The registration is split across helper
functions purely to keep each function under the code-quality size budget;
the call order in the factory preserves the ordering invariant. The router
is included before the stubs aggregate in
`ui/routes/__init__.py::build_router`.

## Dependencies

- `operations.meta_tools.call_operation` — the in-process dispatch entry
  (reads and writes).
- `connectors/keycloak/ops_read.py` — the curated `keycloak.*` read ops
  (`realm.get`, `client.list`, `client.get`, `client_scope.list`,
  `user.list`, `role_mapping.get`).
- `connectors/keycloak/ops_write.py` — the write ops (`user.create`,
  `user.reset_password`, `role_mapping.assign`), all `requires_approval=True`.
- `ui/csrf.py` (`mint_csrf_token`) + `ui/routes/approvals/render.py`
  (`set_csrf_cookie`) — the double-submit token minted on each write modal.
- `ui/routes/connectors/operator.py::resolve_role_probe` — the soft-failing
  role probe driving `is_tenant_admin`.
- `db.models.Target` — the tenant-scoped target picker source.

## Known limitations

- Client-scope / protocol-mapper create is T3
  ([#1943](https://github.com/evoila/meho/issues/1943)); not in this surface.
- Single managed realm per target (the connector resolves the realm from
  the target's `managed_realm`); no realm selector.
- The role-assign picker collects free-text realm role names rather than a
  catalog dropdown (the realm role catalog op is not in this task's verb
  set); current realm roles are shown for context via `role_mapping.get`.
- `routes.py` exceeds the 600-line code-quality file-size budget (warning,
  pre-existing from the T1 scaffold); the route handlers are FastAPI Form
  closures whose param lists trip PLR0913. Both are recorded as warnings,
  not blockers.

## References

- Tasks [#1959](https://github.com/evoila/meho/issues/1959) (T1) +
  [#1960](https://github.com/evoila/meho/issues/1960) (T2), Initiative
  [#1943](https://github.com/evoila/meho/issues/1943).
- Approval handoff surface: `/ui/approvals`
  ([#1778](https://github.com/evoila/meho/issues/1778)).
- Operations console precedent:
  [#1835](https://github.com/evoila/meho/issues/1835)
  (`docs/codebase/ui.md`, `ui/routes/operations/routes.py`).
- Keycloak connector + read ops:
  `docs/codebase/connectors-keycloak.md`.
