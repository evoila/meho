# The policy file (`policy: v1`)

The policy file holds the categories (the taxonomy) and the
classifications of connectors, operation groups and operations
([policy-classification.md](policy-classification.md)). It is a file of
its own, with its own command (`meho policy`) and its own REST routes. It
is **not** a section of the settings file
([settings.md](settings.md)): the settings import adds and updates but
never deletes, and a policy import must be able to remove a right.

Everything here is behind the switch `POLICY_CLASSIFICATION` (off by
default). With the switch off, the import is refused with one sentence.
Validate, diff, export and history still work: they only read.

## The file

```yaml
policy: v1
description: "<free text>"
taxonomy:
  default: "<category>"          # the install's file only
  categories: ["<category-a>", "<category-b>"]
classifications:
  - match: {connector: "<connector-id-pattern>"}
    category: "<category-a>"
  - match: {connector: "<connector-id>", group: "<group-key>"}
    labels: {team: "<team-name>"}
    always_wait: true
  - match: {op: "<op-id-pattern>"}
    risk: dangerous
```

- `policy` is required and is always `v1`.
- `taxonomy.categories` in a tenant's file adds categories for that
  tenant. In the install's file it is the category list of every tenant.
  `taxonomy.default` is allowed only in the install's file. Each of the
  two replaces the built-in value when set, and the default must be one
  of the categories (the built-in default is `other`).
- Each `classifications` entry has a `match` (at least one of `connector`,
  `group`, `op`; patterns as in
  [policy-classification.md](policy-classification.md)) and sets at least
  one of `category`, `labels`, `risk`, `always_wait`. Two entries with the
  same `match` are a mistake.
- `labels` pass the same check as the single edit (`_check_labels` in
  `policy/store.py`): at most 32 labels, a key of 1 to 63 characters and a
  value of 1 to 255 characters, and the character rule of
  [policy-classification.md](policy-classification.md) (a key uses only
  letters, digits and `. _ / -`; a value has no line breaks and no control
  characters). A mistake names the entry.
- Later changes add `bindings` and `routing`. Until then they are unknown
  keys.

## Checks

The parser (`policy/document.py`) uses the strict YAML loader of the
settings file (`instance_settings/schema.py`): a repeated key, a YAML
anchor or alias, an unknown key, and an entry that is not a map are each a
mistake. The file may be at most 1 MiB.

The checks against what MEHO knows (`policy/validate.py`) run on every
operation the file can match: for a tenant's file the built-in operations
and the tenant's own; for the install's file every operation. Each problem
is one sentence that names the entry:

- An unknown category, an unknown risk level.
- **Floor: a secret.** An entry that sets `risk: safe` or
  `always_wait: false` on an operation that shows a secret
  (`operations/credential_read.py`) is refused: such an operation stays at
  least `caution` and always waits.
- **Floor: an address the caller gives.** The same for an operation with a
  parameter that holds an address MEHO connects to
  (`operations/caller_address.py`).
- **Floor: the code's level.** A `risk` below the operation's level in the
  code is refused. A file can raise a risk level, not lower it.
- Two entries that give one operation a different category (or a different
  value for one label key) at the same level.

At most 50 mistakes are listed, then one line says how many more there
are.

An install file with `always_wait: true` gets a **warning**: a tenant's own
entry that is as specific or more specific can set it back to the
operation's own value. An install admin cannot force "always wait" for
every tenant yet; that needs a decision first.

## Plan, import, approval

`policy/service.py`:

- `build_plan` parses and checks the file and works out the diff. It
  writes nothing. The diff has the sections `description`, `taxonomy` and
  `classifications`, each with `added`, `changed` (`before` and `after`)
  and `removed`. Each classification entry also has `matches` (how many
  operations it matches) and `changes` (how many of those get other
  values). Entries are told apart by their whole `match` (each part on its
  own), so two different matches never share one line. `operations_changed` counts all operations whose values change.
  `based_on` is the SHA-256 of the meaning of the stored policy (key order,
  comments and empty keys do not count). It covers only the stored policy
  of the import's own scope. A tenant import does not notice a change to
  the install's policy, or to the category and labels of a single
  operation (`operation_policy_override`), made after the plan. The run
  plans again, so the floors and conflict checks still use the policy of
  that moment; only the diff the approver saw can be out of date.
- The same file twice, or a file with the same meaning, is "No change.":
  nothing waits, and no version is written.
- An import is a change of rights. `POST /api/v1/policy/import` calls the
  typed operation `policy.apply` (`connectors/policy/apply.py`, connector
  `policy-registry-1.x`) through the dispatcher. It is `dangerous`, waits
  for a person to approve each call, needs the `tenant_admin` role, and
  carries the rights-change mark: the importer can never approve it;
  another admin must ([approvals.md](approvals.md)). The params are the
  scope, the file, its SHA-256 and `based_on`. The approval card shows the
  plan (a preview builder) and who asked (a permission preflight).
- Only a person asks for an import, and only a platform admin asks for
  `scope: install`. The import route checks this at the door. For every
  other path to `policy.apply` (`call_operation`, the REST dispatch route,
  `meho call`), the operation registers a requester check
  (`register_requester_check` in `operations/role_floors.py`). The policy
  gate runs it first, for every kind of login, so an agent, a service
  login, a hosted agent run, or a tenant admin asking for `scope: install`
  is refused before the request waits and before any plan is built. The
  preview builder refuses the same callers too.
- When it runs, `policy_apply` checks the requester again from the
  request: the approval card's record must say a person asked, with no
  agent acting for them, and for `scope: install` a platform admin. A
  request without that record does not run.
- When it runs after the approval, `apply_policy` checks the SHA-256 of
  the file and plans again. If the stored policy changed after the plan:
  "The policy changed after this import was planned. Run the preview
  again." Nothing is written then.
- Only the admin who approved runs the import. A run by the login that
  asked for it (for example its re-run of a failed request,
  `POST /api/v1/approvals/{id}/rerun`) writes nothing and says: "A policy
  import runs only from a second admin's approval. Run the import again."
- The import **replaces the whole policy** of its scope in `tenant_policy`:
  an entry that is not in the file is removed. This also removes entries
  that `policy.classify` wrote. The category and labels on single
  operations (`operation_policy_override`) are not part of the file and
  stay.
- The install's policy (`scope: install`) can only be imported by a
  platform admin, and runs only when a platform admin approves it.

## Versions: `policy_revision`

Migration `0163`. One row per imported file: the tenant (`NULL` for the
install), when, who imported it (`imported_by_sub`, the person who asked),
who approved it (`approved_by_sub`), the file's SHA-256, the file as sent,
the stored document after the import, and the diff. The service only adds
rows; after each import it deletes the rows beyond the last 20 of that
scope.

## A bad stored document

If a stored document holds an entry MEHO cannot read (for example a risk
level that does not exist), every policy read of that tenant fails while
the switch is on. The error names the document and the entry, for example
"The stored policy of tenant `<tenant-id>` is not valid, so MEHO cannot
classify operations: tenant:classifications[3]: ...". An import of a good
file replaces it; the preview says so in a warning. This holds also when
an entry or its `match` is not a map, or `classifications` is not a list:
the diff then lists such an entry as removed.

## Endpoints and CLI

Every route takes `scope` (`tenant`, the default, or `install`). Callers:
`tenant_admin` or platform admin; `install` needs a platform admin. The
import is human-only.

| Route | CLI | What it does |
|---|---|---|
| `POST /api/v1/policy/validate` | `meho policy validate FILE` | mistakes and warnings |
| `POST /api/v1/policy/import?preview=true` | `meho policy diff FILE` | mistakes and the diff; writes nothing |
| `POST /api/v1/policy/import` | `meho policy import FILE` | 202 with the approval request; 200 "No change."; 422 with mistakes; 409 with the switch off |
| `GET /api/v1/policy/export` | `meho policy export [-o FILE]` | the stored policy as a file |
| `GET /api/v1/policy/history` | `meho policy history` | the last 20 imported files, newest first |

Code: `backend/src/meho_backplane/api/v1/policy.py`,
`cli/internal/cmd/policy/`.

## Tests

`backend/tests/test_policy_document.py` (parser, floors, plan, apply,
versions), `backend/tests/test_api_v1_policy.py` (routes, waiting import,
approval by a second admin),
`backend/tests/migrations/test_migration_0163_add_policy_revision.py`,
`cli/internal/cmd/policy/file_test.go`.
