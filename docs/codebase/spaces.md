# Spaces

## Overview

A space is an area inside one tenant that belongs to one person or a small
team. It lists its members and the systems assigned to it. This page covers
the three tables and the history rule. Operations, routes and the overview
come in later Tasks of the same Initiative.

The words:

- **Tenant:** the company boundary in MEHO. A space never crosses tenants.
- **Space:** a named area in a tenant. Its kind is `person` or `team`. Its
  owner is the Keycloak issuer and subject, never an email address.
- **Member:** a person in a space, with a role (`owner` or `member`) and a
  state (`active` or `disabled`).
- **Assignment:** one record "this system belongs to this space". It says
  who made it, why, and from when to when.

## Key types

Code is in `backend/src/meho_backplane/spaces/`. Migration `0155` makes the
tables.

| Table | Holds |
|---|---|
| `space` | id, tenant id, name (unique in the tenant), kind, owner issuer and subject, state, created at, created by |
| `space_member` | space id, member issuer and subject, role, state, `valid_from`, `valid_to`, changed by, reason |
| `resource_assignment` | space id, kind (`target`, `object`, `service`, `fact`), MEHO target id, vendor object id, name at the time, access (`own` or `shared_summary`), `valid_from`, `valid_to`, changed by, reason |

`target_id` has no foreign key: a deleted target keeps its assignment
history. For machines, the vendor's object id (`object_id`) is the stable
key, not the name.

## Control flow: the history rule

- A change never edits a member or assignment row, and never deletes one.
- It sets `valid_to` on the current row and adds a new row. Both use the same
  time. The current row is the one with `valid_to` empty.
- Ending a row is one `UPDATE ... WHERE valid_to IS NULL`. When two callers
  end or move the same row at once, only one of them ends it. The other one
  gets "not found" (an assignment) or `False` (a member), and adds no row.
- `space_member` has a unique index on the current row of one person in one
  space, so there cannot be two.
- Every row carries `tenant_id` (no foreign key, like other per-tenant tables, so test cleanup can truncate `tenant`). Every function in `spaces/store.py` takes
  the tenant id and filters on it. A space or assignment of another tenant
  is "not found".
- The callers commit.

## Dependencies

- The setting `SPACES_ENABLED` (off by default) turns spaces on. The tables
  exist either way and are empty. With the setting off, nothing changes for
  an install.

## Known issues

- Only the tables and the store exist. No operation, route, or CLI verb uses
  them yet.

## References

- Tests: `backend/tests/test_spaces_store.py`,
  `backend/tests/migrations/test_migration_0155_create_spaces_tables.py`.
