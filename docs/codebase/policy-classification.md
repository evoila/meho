# Category, labels and "always wait" on operations

Each connector, operation group and operation can carry:

- a **category**, for example `network` or `storage`;
- **labels**: free `key=value` text;
- a **risk level** that is higher than the one in the code;
- an **"always wait"** mark: every call waits for approval.

An operation takes the values of its group, and a group the values of its
connector, unless they are set lower down.

All of this is behind the switch `POLICY_CLASSIFICATION` (Helm
`config.policy.classification`). It is **off by default**. When it is off,
MEHO reads none of it: every operation keeps the risk level and the
approval it has today, and `category`, `labels` and `rule_ref` stay empty.
`/ready` shows the switch under `features.policy.classification`.
The switch changes what the agent sees (the category and labels in search
results and in the call contract), so `GET /version` lists it too.

Label text can reach the agent, so it has a character rule
(`label_text_ok` in `policy/classification.py`). A key uses only letters,
digits and `. _ / -`. A value uses only printable characters: no line
breaks and no control characters. A write that breaks the rule is
refused. A stored label that breaks it (for example one written before the
rule) is not shown.

## Where the values come from

| Place | Holds |
|---|---|
| `tenant_policy` (model `TenantPolicy`, migration `0159`) | One document per tenant, and at most one for the whole install (`tenant_id` NULL): the `taxonomy` and the `classifications` entries. |
| `operation_policy_override.category`, `.labels` | The category and labels of one operation: an exact operation entry. |

A document has the shape of the policy file:

```yaml
taxonomy:
  default: "<default-category>"
  categories: ["<category-a>", "<category-b>", "<default-category>"]
classifications:
  - match: {connector: "<connector-id-pattern>"}
    category: "<category-a>"
  - match: {connector: "<connector-id>", group: "<group-key>"}
    labels: {team: "<team-name>"}
    always_wait: true
  - match: {op: "<op-id-pattern>"}
    risk: dangerous
```

- `match` holds `connector` (the connector id `<impl_id>-<version>`),
  `group` (the group key) and `op` (the operation id). Each is a pattern:
  `*` is any text, `?` is one character, no wildcard is an exact match.
  Upper and lower case count. Every field given must match.
- **Taxonomy.** One category list per install. Without an install
  document MEHO uses its built-in list (`DEFAULT_TAXONOMY` in
  `policy/classification.py`). A tenant's document can add categories
  for that tenant only. The default category is always the install's.
  The install's `categories` and `default` each replace the built-in
  value when set.

## How one operation is classified

The rules are in `policy/classification.py` (`classify`, no database):

1. **Level.** An entry with `op` beats one with `group`, which beats one
   with only `connector`.
2. **Inside one level**, the longer literal text before the first wildcard
   wins (the same rule as agent grants): first `op`, then `group`, then
   `connector`. Then a tenant's entry beats an install entry.
3. **Each field on its own.** The most specific entry that sets a field
   decides it. Labels add up; a more specific entry replaces the value of
   a key.
4. **Ties** (same rank): for `risk` the higher level wins, for
   `always_wait` `true` wins. A different `category`, or a different value
   for one label key, is a conflict.
5. **No entry** sets the category: the taxonomy's default.

`rule_ref` names the most specific entry that matched, for example
`tenant:classifications[2]` or `install:override`, or `taxonomy.default`.

**A conflict** is refused when an entry is written (one sentence that
names both entries). If a reader still meets one (for example a new
operation that two old entries match), the operation has no category and
always waits.

## The floors come last

`effective_policy_map` in `operations/op_policy.py` works in this order:

1. the value in the code, then the operator overrides (`effective_policy`);
2. the classification (`_with_classification`): the risk level becomes the
   higher of the two, and `always_wait` can only add a wait. A raise to
   `destructive` always waits. So a level never goes below the code's
   value, and `always_wait: false` only goes back to the code's value;
3. the floors: an operation that connects to an address the caller gives
   is at least `caution` and always waits.

Dispatch (`lookup_descriptor`, which joins the group key), preview, search,
the call contract and the console all read `effective_policy_map`, so they
show the same values. The returned `EffectivePolicy` carries `category`,
`labels`, `rule_ref` and `always_wait`.

## Where a person and an agent see it

While the switch is on:

- a `search_operations` hit and the call contract (exact-op mode) carry
  `category` and `labels` ([mcp.md](mcp.md));
- an approval request carries `category`, `labels` and
  `classification_rule` (the `rule_ref`), and the approvals console and
  `meho approvals show` show them ([approvals.md](approvals.md)).

With the switch off, none of these keys is in an answer.

## "Always wait" means a person approves each call

When a classification entry makes an operation always wait, or two entries
conflict, `EffectivePolicy.always_wait` is `true`. `apply_effective_policy`
copies it to the looked-up row as `policy_always_wait` (not a column). Then
`always_wait_grant_reason` in `operations/service_grants.py` refuses:

- a standing grant at call time, also a grant made before the entry;
- a new standing grant for the operation;
- a run approval list that names the operation, and a run approval at call
  time.

So a service login can never run such an operation without a person
approving that call.

**Steps of a multi-step operation.** A multi-step operation checks each
write step against a row built in memory from the step's declared risk
level (`enforce_subop_policy` in `operations/composite.py`), and the
preview checks the same steps (`operations/_preview_steps.py`). Both call
`apply_step_classification` in `operations/op_policy.py` first. It
classifies the step (`classify_step` in `policy/store.py`), raises its
level and sets `policy_always_wait`. So a marked step waits for a person
also when a standing grant or a run approval covers it, and also when a
person approved the whole multi-step operation. Entries by connector and
by operation always match a step. The step's group key comes from its
stored row when there is one; a step without a stored row still runs, but
then entries by group do not match it.

## Changing a classification is a change of rights

A category decides who may approve. So a change of a category, labels or
the "always wait" mark is a change of rights.

The change is the operation `policy.classify` on the connector
`policy-registry-1.x` (`connectors/policy/`):

- It is `dangerous` and always waits for approval.
- A person approves each call (`declare_per_call_approval`): no standing
  grant and no run approval covers it.
- Every request carries the rights-change mark
  (`register_rights_change_op`, see [approvals.md](approvals.md)). The
  person who asked can never approve it; another tenant admin must.
- A person needs the `tenant_admin` role to ask.
- It changes one entry of the caller's tenant document: the entry with the
  same `match` is updated, or a new one is added. It refuses an unknown
  category, bad labels and a conflict. Nothing is written then.
- It exists only while the switch is on. With the switch off, the
  registrar deletes the operation and its group if an earlier start wrote
  them (`remove_built_in_connector_rows`). So after a restart with the
  switch off, nobody finds or calls it, and no new request waits for it.
  The handler also refuses a call when the switch is off.

## Edit one at a time or by pattern

A tenant admin edits one operation, one group, one connector, or many
operations by pattern (for example `{"op": "k8s.deployment.*"}`). A
pattern can match more than the admin expects, so every surface works in
two steps. Both are in `policy/bulk.py`:

1. **Dry run** (`preview_classification_edit`). It lists every operation
   the entry matches, with its category, labels, risk level and "always
   wait" before and after the edit, and `changes` for each. The list is
   never cut. It also names a conflict the edit would make. It writes
   nothing. `plan_edit` in `policy/store.py` does the work; the change
   itself (`set_classification`) uses the same function.
2. **Request** (`request_classification_edit`). It runs the dry run again
   and refuses, before any request, an edit that is not valid, that
   matches no operation, or that would make a conflict. Otherwise it calls
   `policy.classify` through `call_operation`, so the role check, the
   approval and the audit row are those of any other call. The answer
   waits until another tenant admin approves it.

| Surface | Dry run | Request |
|---|---|---|
| REST (`api/v1/policy_classification.py`) | `POST /api/v1/policy/classification/preview` | `POST /api/v1/policy/classification` (`202` with `approval_request_id`) |
| REST, one connector | | `PATCH /api/v1/connectors/{connector_id}/classification` |
| REST, one group or operation | | `category` / `labels` on the existing group and operation PATCH routes (`202`) |
| CLI | `meho connector classify --op '<pattern>' --category <name>` | the same with `--apply`; `--category` / `--label` on `edit-op` and `edit-group` |
| Console | `/ui/policy/classify` ("Show what changes"); the review drawer links to it with the match filled in | "Ask for this change" on the dry run |
| Admin MCP tools | the operation `policy.classify_preview` | `category` / `labels` on `meho_connector_edit_op` and `meho_connector_edit_group`; `policy.classify` for a connector or a pattern |

`GET /api/v1/policy/classification` lists the taxonomy and the stored
entries of the caller's tenant and of the install.

Rules every surface keeps:

- `category` and `labels` on a PATCH route or an MCP edit tool wait for a
  second admin, so they cannot ride along with fields that are stored at
  once. A body with both is refused (`400`, MCP `-32602`,
  `classification_mixed`).
- The change is always for the caller's tenant. The tenant comes from the
  login, never from the request.
- While the switch is off, REST answers `409` (`classification_off`), the
  list says `enabled: false`, the console page says so, and the two MCP
  edit tools do not show the fields (`classification_for_settings`), so
  `tools/list` is as before. The answer of `meho_connector_review` has no
  `category` / `labels` on its ops (`drop_classification_fields`), so it
  is as before too. The dry-run operation is not registered.

`policy.classify_preview` is `safe` and runs at once (no approval). Like
`policy.classify` it needs the `tenant_admin` role and is registered only
while the switch is on.

## The override report shows the value in force

The override report (`list_overrides` in `operations/op_policy.py`, REST
`GET /api/v1/connectors/operation-overrides`, `meho connector overrides`,
`/ui/connectors/overrides`) works out each row with
`effective_policy_map`, like dispatch. So with the switch on it never shows
"approval off" for an operation that a classification entry still makes
wait. Each row also has `category`, `labels`, `always_wait` and `rule_ref`
(the entry that decided the values). With the switch off these stay empty
and the report is as before.

## Known limits

- The policy file import (`policy.apply`, see [policy.md](policy.md))
  replaces the whole document of a tenant or the install, also the entries
  that `policy.classify` wrote.
- A connector id, group key or operation id that holds `*`, `?` or `[` is
  read as a pattern, also on the PATCH routes. The dry run shows what it
  matches.
- The flight-recorder check (`test_flight_recorder_typed_spans.py`) runs
  with the switch off, so it does not list `policy-registry`.
