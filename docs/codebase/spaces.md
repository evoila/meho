# Spaces

## Overview

A space is an area inside one tenant that belongs to one person or a small
team. It lists its members and the systems assigned to it. This page covers
the three tables, the history rule and the six operations. Routes come in
later Tasks of the same Initiative.

The words:

- **Tenant:** the company boundary in MEHO. A space never crosses tenants.
- **Space:** a named area in a tenant. Its kind is `person` or `team`. Its
  owner is the Keycloak issuer and subject, never an email address.
- **Member:** a person in a space, with a role (`owner` or `member`) and a
  state (`active` or `disabled`).
- **Assignment:** one record "this system belongs to this space". It says
  who made it, why, and from when to when.

## Key types

Code is in `backend/src/meho_backplane/spaces/`. Migration `0156` makes the
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

## Operations

The built-in connector `spaces-records-1.x` (code in
`backend/src/meho_backplane/connectors/spaces/`) has six operations. They
are typed operations, like the ones of `audit-integrity-1.x`.

| Operation | What it does | Risk level |
|---|---|---|
| `spaces.assign` | Assigns a target, a machine, a service or a fact to a space. Needs a reason. The same thing twice in one space changes nothing. | `caution` |
| `spaces.unassign` | Ends an assignment. The row stays, with an end time. | `caution` |
| `spaces.reassign` | Ends an assignment and starts a new one in another space, in one step. | `caution` |
| `spaces.member.disable` | "Leave a lab": ends one person's membership in one space (a new row with state `disabled` starts). Nothing is deleted. It never calls the Keycloak connector; the company account is a separate step. | `caution` |
| `spaces.import` | Loads a site's ownership records from a manifest (see below). | `caution` |
| `spaces.overview` | Per space: owner, members, assigned systems. Per target: product, version, when it was detected, and health. Reads stored data only. | `safe` |

- The five writes need a reason, and they only work in the caller's tenant.
  A person needs the `tenant_admin` role (the operation's role floor), and
  only a `tenant_admin` can approve a waiting call. An agent's call runs
  under the agent's own login after that approval.
- The audit row of every write holds who made it and the reason
  (`reason` in its payload). Assign, reassign and member.disable also save
  `changed_by` and `reason` on the new row.
- "The same thing" means the same kind on the same target: two targets can
  have objects with the same vendor id, or services with the same name.
- The five writes have `requires_approval` on, so every call waits for an
  approval click, also in the web interface. This keeps the operation
  contract rules (`test_operation_contracts.py`).
- "Owner left": when a person with role `owner` is disabled and the space
  has no active owner, `spaces.member.disable` lists the current assignments
  of the space, and `spaces.overview` shows `owner_left: true` on the space
  and on each of its assignments. They wait for `spaces.reassign`. No column
  stores this; it is read from the member rows.
- Health of a target: the state of the newest run of the checks (Sensors)
  that watch the target by name. When that run is older than twice the
  check's interval, or has no time, the health is `unknown`. For a cron
  check the interval is the gap between two runs.
- A long overview comes back behind a result handle (JSONFlux); `count`
  stays in the answer.

## Import from a manifest

`spaces.import` takes a manifest that the site builds offline. The same
shape also feeds the fleet import of the automation add-on, so a site builds
one manifest. MEHO's import is generic; the builder stays at the site.
Targets are named as MEHO knows them. Owners and users are Keycloak issuer
and subject. Example with made-up names:

```json
{
  "environments": [
    {
      "name": "lab-one",
      "targets": ["demo-target-1", "demo-target-2"],
      "owner": {"issuer": "<issuer-url>", "subject": "<subject-ann>"},
      "users": [{"issuer": "<issuer-url>", "subject": "<subject-bob>"}]
    }
  ],
  "people": [
    {"issuer": "<issuer-url>", "subject": "<subject-ann>", "display_name": "Ann"},
    {"issuer": "<issuer-url>", "subject": "<subject-bob>", "display_name": "Bob"}
  ]
}
```

- `owner` is one person. A list of two or more people is accepted and
  listed as the conflict `two_owners`.
- `people` lists the accounts that exist. `display_name` is read but not
  stored; MEHO keeps no person record.
- `only` (a list of lab names) imports part of the manifest. Other labs are
  not read, planned or counted. A name that is not in the manifest is an
  error.
- Every input row lands in exactly one bucket: `created`, `changed`,
  `unchanged` or `conflict`. The rows are the labs (kind `space`, with the
  owner), the people, the target names (`assignment`) and the users
  (`member`). A person row is always `unchanged`. The answer holds the counts
  per kind, the total (equal to the input) and every conflict with a code.
- `changed`: the space exists with the same owner, but its owner has no
  current owner record yet, or only a plain member record.
- Lab-level conflicts skip the whole lab and list all its rows: `no_owner`,
  `two_owners`, `owner_has_no_account`, `duplicate_lab` (the name twice),
  `space_disabled`, `owner_differs_in_meho`, `owner_disabled_in_meho`,
  `owner_ended_in_meho`.
- Row-level conflicts leave the rest of the lab to import: `unknown_target`,
  `target_in_two_labs`, `assigned_elsewhere_in_meho`, `ended_in_meho`,
  `no_account`, `disabled_in_meho`. `assigned_elsewhere_in_meho`,
  `ended_in_meho` and `disabled_in_meho` mean MEHO holds a newer record. The
  import never brings back a disabled person, an old owner, or an assignment
  or membership that an administrator ended. A `write_failed`
  conflict lists a lab whose transaction failed.
- The preview (the approval card, `preview_operation`) plans with the same
  code and writes nothing. Apply writes one transaction per lab and uses the
  same store and audit path as the other writes. The second run changes
  nothing.
- The answer keeps `status`, `total_rows`, `conflict_count` and
  `labs_written` inline; a long conflict list comes back behind a result
  handle.

## Dependencies

- The setting `SPACES_ENABLED` (off by default) turns spaces on. The tables
  exist either way and are empty. With the setting off, all six operations
  answer "Spaces are off" and nothing else changes for an install.

## Known issues

- No route or CLI verb uses the store yet.

## References

- Tests: `backend/tests/test_spaces_ops.py`, `backend/tests/test_spaces_import.py`, `backend/tests/test_spaces_store.py`,
  `backend/tests/migrations/test_migration_0156_create_spaces_tables.py`.
