# Per-tenant mail-recipient policy — operator mutation surface

## Overview

The `mail.*` connector's recipient floor (`MAIL_RECIPIENT_ALLOWLIST`,
`connectors/mail/allowlist.py`) is deployment-level: one MTA, one allowlist for
the whole instance. On a **shared** instance that single floor cannot be
narrowed per tenant, so an approved `mail.send` in one tenant could deliver to
the recipients another tenant configured (the Envision-stand threat,
meho-internal#320). #3499 adds a **per-tenant narrowing** override —
`tenant.mail_recipient_allowlist` — and this surface is its operator-plane write
path.

It is an **operator-plane** surface: REST + CLI only, gated at the
`tenant_admin` tier. It is deliberately **not** on the 25-tool agent working
surface and has **no MCP tool** — narrowing a tenant's mail reach is a
governance decision, an operator action, not an agent one. (Simplest correct
answer per postulate 5: REST + CLI, no MCP tool at all — the same posture as the
flight-recorder policy, [`flight-recorder-policy.md`](flight-recorder-policy.md).)

## Route

- `PATCH /api/v1/tenants/mail-recipient-policy`
  (`meho_backplane/api/v1/tenants.py`) — the single
  `mail_recipient_allowlist` field. **Tenant-scoped to the caller's own tenant**
  (`operator.tenant_id` from the JWT); it accepts no tenant id in the path or
  body, so a cross-tenant write is not expressible.
  `require_role(TenantRole.TENANT_ADMIN)` (`operator` / `read_only` → 403).
  Body `TenantMailRecipientPolicyUpdate` (optional-partial, `extra='forbid'`);
  returns the resolved `TenantMailRecipientPolicy`.

## Tri-state on the value (not a Boolean)

Unlike the flight-recorder tri-states (Boolean columns), the mail allowlist
carries its tri-state on a nullable `Text` value:

- **absent** (key omitted) = leave the column unchanged.
- `null` = clear back to **inherit** — no per-tenant screen; the deployment
  `MAIL_RECIPIENT_ALLOWLIST` instance floor alone governs the tenant's
  `mail.send`.
- `""` (empty string) = **deny** — the tenant's parsed allowlist is empty, so
  every dispatched `mail.send` for the tenant is refused (the inverted
  "empty ⇒ inert" default the instance floor already uses). This is the
  shared-instance containment lever: pin an untrusted-visitor tenant to no mail
  while production keeps its alert mail.
- a comma-separated address/domain string = the tenant's own recipient space,
  validated at write time with the same grammar as the instance floor
  (`parse_recipient_allowlist`), so a malformed entry (`foo@`, bare `@`,
  whitespace, …) is a **422 now** rather than a silently-inert allowlist at
  dispatch time.

The handler keys off `model_fields_set` (`model_dump(exclude_unset=True)`), so
the JSON-`null`-vs-absent distinction is preserved (same discipline as the
flight-recorder route).

## Resolution + narrowing (where it takes effect)

`connectors/mail/tenant_policy.py::resolve_tenant_recipient_allowlist` reads the
column per dispatch (60s-TTL cache) and the `mail.send` handler
(`connectors/mail/ops.py`) screens every recipient against the tenant's parsed
set **before** the transport applies the instance floor. The tenant screen can
only **narrow**: a recipient the tenant lists but the instance floor does not is
still refused by the transport. The checks notifier's direct-import
`send_email` path is not tenant-dispatched and keeps only the instance floor.

**Fail-closed:** a DB read/parse error in the resolver resolves to deny (empty
allowlist), never to inherit — a delivery-authorization decision where doubt
reduces exposure (the opposite direction from the flight-recorder resolver's
fail-open). The deny is not cached.

## Audit

An applied change is folded into the request's `audit_log` row via `audit_*`
contextvars naming field / old / new (governance-relevant, never silent). A
`NULL` value is bound as the `inherit` sentinel because the audit-payload
builder drops `None` contextvars (mirroring the flight-recorder route and the
target CA-pin `""` marker); an empty-string deny is bound verbatim. A no-op /
absent field binds nothing.

## Cache invalidation

The resolver caches per-tenant policy for 60s. The route calls
`invalidate_tenant_mail_policy_cache` on a change so the new value governs the
**next dispatched `mail.send`** rather than waiting out the TTL or a restart
(proven by the without-reset test in `test_api_v1_tenants_mail_recipient.py`).

## CLI

- `meho tenants mail-recipient-policy set --allowlist <value>` — set the
  tenant's recipient space (comma-separated addresses/domains); `--allowlist=""`
  denies all mail for the tenant.
- `meho tenants mail-recipient-policy set --clear` — clear the override back to
  inheriting the instance floor.
  (`cli/internal/cmd/tenants/mail_recipient.go`.) The verb builds a **sparse**
  PATCH body (only the field the operator set) so a nil never marshals to an
  unintended `null`, preserving the null-vs-absent distinction — same rationale
  as the flight-recorder verb.
