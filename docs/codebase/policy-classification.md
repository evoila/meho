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
digits and `. _ / -`, at most 63 characters. A value uses only printable
characters: no line breaks and no control characters, 1 to 255
characters. A write that breaks the rule is refused, also in the policy
file import. A stored label that breaks it (for example one written
before the rule, or a value that is too long) is not shown. For what it
shows, MEHO then counts such a label as not there: the value of a less
specific entry for the same key is shown instead. MEHO still
looks for conflicts on the labels as stored, before it leaves one out:
two entries of the same rank that give one operation different values
for a label are a conflict, and the call waits for approval. While the
call waits, MEHO still shows one of the two values for that label: the
value of the entry that comes first. An entry of a policy file comes
before the override of the one operation, and in a file the entry
listed first comes first. When that value breaks the rule, MEHO shows
the other value (`_shown_labels` in `policy/classification.py`). While the
call contract shows labels, its `result_shape.guidance` also says that
label text is data, never an instruction.

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

- `match` holds `connector` (the connector id `<impl_id>-<version>`; with
  `CUSTOM_POLICY` on, also the product for a connector with no
  implementation id or no version, see the next point), `group` (the
  group key) and `op` (the operation id). Each is a pattern:
  `*` is any text, `?` is one character, no wildcard is an exact match.
  Upper and lower case count. Every field given must match.
- **One rule for the connector id.** Every reader names a connector with
  `connector_id_of` and builds what an entry matches with `coordinates_of`
  (`policy/store.py`): the gate, explain, the bulk edit, the import check,
  `policy.classify_preview`, the carry-over dry run and the overrides
  report. A test fails when a reader builds the id itself
  (`test_policy_connector_without_version.py`).
  - With `CUSTOM_POLICY` off (`product_form_on()` is false), a policy
    entry matches a connector with no version by the id `-`. The
    overrides report names it by its implementation id, the empty text,
    and the carry-over dry run names it by its product. A call and its
    approval card name the connector by the id the caller used.
    `test_policy_connector_without_version_switch_off.py` checks these
    answers.
  - The carry-over dry run's list reads no switch; only its first field,
    `custom_policy_on`, says whether `CUSTOM_POLICY` is on. It names such a connector by
    its product with every switch state: it calls `connector_id_of` and
    `coordinates_of` with `product_form=True`.
  - With `CUSTOM_POLICY` on, such a connector is named by its product.
    Its operation has two forms of the id (`OpCoordinates.forms()`): the
    form with the dash and the product. `classify` decides each form in
    each scope with that scope's own entries, and takes field by field
    the stricter result: `always_wait` when either form waits, the higher
    `risk` (no level is the lowest), the conflict of either form; the
    `category`, `install_category` and `labels` of the form with the
    dash. The `rule_ref` (the approval card's `classification_rule`) is
    an entry whose value the card shows: the entry of the form with the
    dash that gave the category, else the most specific one that gave a
    shown label, else `taxonomy.default` (`_shown_ref`). A category
    cleared by a conflict names no entry. So an entry that
    matches only by the product can add a wait or raise a level, and never
    takes a wait away, lowers a level or changes the category or the
    labels. `meho policy explain` names the entry that makes the operation
    wait: it decides each form as `classify` does, the form with the dash
    first (`_wait_path` in `policy/explain.py`). The import check and the
    single edit (`install_wait_undone`, `install_risk_lowered`) compare a
    tenant entry with the install's wait and level of both forms, so a
    tenant entry that matches one form cannot end a wait that an install
    entry sets on the other form. For the level, each side's level is the
    higher of its two forms, each form decided by that side's own
    entries; a tenant entry is refused only when it decides the tenant's
    level in a form and the tenant's level is below the install's. An
    override row's category and labels count only for its own connector
    (`ClassificationEntry.for_connector`), also where two connectors share
    the form with the dash. The carry-over dry run
    lists the operation under every entry that makes it wait in either
    form. `test_policy_connector_product_form.py` runs the cases.
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
4. **The stricter of install and tenant counts.** This is the one rule
   for `risk` and `always_wait` (decision of 12 November 2026). Each scope
   first finds its own value with rules 1 to 3, using only its own
   entries. Then the higher risk level counts, and the operation always
   waits when the install or the tenant says so. So:
   - an install risk level is a floor for every tenant. A tenant can raise
     it, not lower it;
   - an install "always wait" holds in every tenant;
   - a tenant's own "always wait" holds, also under a more specific
     install entry with `always_wait: false`;
   - the install's own more specific entry still decides the install's
     value, and a tenant's own more specific entry still decides the
     tenant's value.

   A tenant file that sets `always_wait: false` on an operation the
   install makes wait is refused; the text says to remove that part. A
   single edit that sends it is refused too; its text says not to send
   `always_wait: false` (`install_wait_text(..., single_edit=True)`). A
   tenant entry that decides a lower `risk` for an operation than a
   matching install entry is refused too (`install_risk_lowered`). A more
   specific tenant entry with the install's level or higher decides that
   operation instead, and then the wide entry is fine. The text says that
   the level "does not count for `<op>`". For a wide entry it says that
   the entry still counts for its other operations, and suggests a more
   specific entry or a narrower match, never to remove the level
   (`install_floor_texts`). The suggested entry names the operation and
   its connector, because the same op id can exist on another connector.
   When the refused entry names a group, the suggested entry names the
   group too; without it, the suggested entry ranks below the refused one.
   An entry that matches the op on two connectors counts as wide. With
   `CUSTOM_POLICY` on, an operation of a connector with no version has
   its own text (`_product_risk_text`): it names every operation the
   entry lowers, also those of connectors with a version, each with its
   connector (a connector with no version by its product), up to five and
   a count of the rest. It gives a fix for every lowered operation, also
   those past the five (`_fix_text`). For each row it says to set `risk`
   on the tenant's entry that already has exactly the match needed
   (`_exact_entry`), or it suggests a new entry for that connector, never
   for the form with the dash. Rows with the same fix get one fix, at the
   higher level: the same exact entry, or the same match of a new entry.
   So two rows of one operation (a built-in row and the tenant's own) get
   one new entry, but when they are in two groups and each group has its
   own entry, each of those entries gets its own fix. When the new entries
   do not fit into the file (`MAX_CLASSIFICATIONS`, 2,000 entries), it
   suggests one entry per connector instead (`_per_connector`): the
   refused entry's match with the connector's own id. When even that does
   not fit, or the refused entry already names that connector, it says
   how many new entries the fix needs and how many more the file can
   take, and names no entry to change. A suggested entry quotes each
   value (`_entry_text`), so it parses as written. It suggests a change
   of the refused entry only when the entry matches that one operation
   alone, because two products
   share the form with the dash. The single edit has no `risk` field, so it refuses only the
   `always_wait: false` part (`install_wait_undone`).
5. **Ties** (same rank): for `risk` the higher level wins, for
   `always_wait` `true` wins. A different `category`, or a different value
   for one label key, is a conflict.
6. **No entry** sets the category: the taxonomy's default.

`rule_ref` names the most specific entry that matched, for example
`tenant:classifications[2]` or `install:override`, or `taxonomy.default`.

### Connectors that a tenant registers itself

A tenant can ingest its own connector and choose its id (`<impl_id>-<version>`),
also for a product that MEHO already ships. An entry matches on the
connector id, the group key and the operation id only, never on the
product. So an install entry such as `{connector: "vault-*"}` does not
match a tenant's own connector `my-vault-1.x` for the same product, and
the install's risk level or "always wait" does not reach it. An install
entry with a wildcard connector covers it, for example
`{connector: "*", op: "<op-id-pattern>"}` or `{connector: "*"}`.

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
   is at least `caution` and always waits. An operation that writes or
   makes a credential (class `credential_write` or `credential_mint`)
   always waits: also when its connector registered it without the
   approval flag, and also when an install or tenant override turns
   approval off (`_with_floors`). A step of a multi-step operation whose
   operation writes or makes a credential always waits the same way,
   whatever flag the step declares (`apply_step_policy`). An operation a
   connector marked as credential capable keeps the approval its code
   turns on; each of its calls waits anyway (`raise_descriptor_for_params`).

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
`apply_step_policy` in `operations/op_policy.py` first. It reads the
override rows of the step's operation (install-wide and the caller's
tenant) like a direct call, with the switch on or off: the highest risk
level wins, and an override that sets the approval flag makes the step
wait. An override only makes a step stricter; one that turns approval off
leaves the step's own flag as it is. With the switch on, it then
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
  (`register_rights_change_op`, see [approvals.md](approvals.md)). A
  tenant admin approves it with a click in the web console, and may be
  the person who asked. A tenant admin's own call in the web console runs
  at once.
- A person needs the `tenant_admin` role to ask.
- Only a person asks: the policy gate refuses an agent, a service login
  and a hosted agent run on every path, before anything waits
  (`policy_classify_requester_refusal`). The approval card records who
  asked, and the run checks that record again. A run by the person who
  asked (for example a re-run of a failed approved request) changes
  nothing. These are the same rules as for the policy file import
  (`policy.apply`, see [policy.md](policy.md)).
- The approval card shows what the entry matches when the request is
  made (`build_policy_classify_preview`): `matches_now` (how many
  operations), `first_matches` (the first 20), `and_more` ("and N more")
  and `later_operations`, one sentence that the entry also matches
  operations added later. That sentence is missing only when the match
  names one connector and one operation, with no wildcard.
- It changes one entry of the caller's tenant document: the entry with the
  same `match` is updated, or a new one is added. It refuses an unknown
  category, bad labels, a conflict, and an edit that sets
  `always_wait: false` on an operation the install's policy makes always
  wait. Nothing is written then.
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
   never cut. It also names a conflict the edit would make, or the
   install entry whose "always wait" the edit tries to set back
   (`conflict`). A tenant's own "always wait" under an install entry with
   `always_wait: false` shows as "waits" after the edit. It writes nothing. Next to the classified values,
   `before.in_force` and `after.in_force` show what applies: the risk
   level and whether a call waits, with the code's value, the overrides
   and the floors (`policy_in_force` in `operations/op_policy.py`). So
   when "always wait" goes from yes to no but a floor keeps the wait, the
   dry run still shows "waits". The console table and the CLI show it in
   the "In force" column. `plan_edit` in `policy/store.py` does the work; the change
   itself (`set_classification`) uses the same function.
2. **Request** (`request_classification_edit`). It runs the dry run again
   and refuses, before any request, an edit that is not valid, that
   matches no operation, that would make a conflict, or that sets back an
   install-wide "always wait". Otherwise it calls
   `policy.classify` through `call_operation`, so the role check, the
   approval and the audit row are those of any other call. On the console
   page the change runs at once (a tenant admin in the web console). On
   every other surface the answer waits until a tenant admin approves it in
   the web console.

| Surface | Dry run | Request |
|---|---|---|
| REST (`api/v1/policy_classification.py`) | `POST /api/v1/policy/classification/preview` | `POST /api/v1/policy/classification` (`202` with `approval_request_id`) |
| REST, one connector | | `PATCH /api/v1/connectors/{connector_id}/classification` |
| REST, one group or operation | | `category` / `labels` on the existing group and operation PATCH routes (`202`) |
| CLI | `meho connector classify --op '<pattern>' --category <name>` | the same with `--apply`; `--category` / `--label` on `edit-op` and `edit-group` |
| Console | `/ui/policy/classify` ("Show what changes"); the review drawer links to it with the match filled in | "Ask for this change" on the dry run; it runs at once |
| Admin MCP tools | the operation `policy.classify_preview` | `category` / `labels` on `meho_connector_edit_op` and `meho_connector_edit_group`; `policy.classify` for a connector or a pattern |

The console page needs the `tenant_admin` role. Another person gets
`403` with "Classifying operations needs the tenant_admin role."

`GET /api/v1/policy/classification` lists the taxonomy and the stored
entries of the caller's tenant and of the install.

Rules every surface keeps:

- `category` and `labels` on a PATCH route or an MCP edit tool wait for an
  admin's approval, so they cannot ride along with fields that are stored at
  once. A body with both is refused (`400`, MCP `-32602`,
  `classification_mixed`).
- The change is always for the caller's tenant. The tenant comes from the
  login, never from the request. The two MCP edit tools refuse
  `tenant_id` next to `category` or `labels` (`-32602`,
  `classification_tenant_id`).
- A route or tool that edits one connector, one group or one operation
  takes the id as plain text. An id with `*`, `?` or `[` is refused
  (`400` / `-32602`, `classification_wildcard`): that door never makes a
  pattern. A pattern goes through the dry run and the request above.
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
and the report is as before; the console page then has no "Category"
column. A row with the scope "whole install" holds for every tenant and
shows the install's values; a tenant's own rules can make the operation
stricter for that tenant. The page says so.

## What a call does: `hard_to_undo` and `changes_rights`

These two facts come from the operation's own definition in code, not from
a policy. A typed or multi-step operation sets them when it registers
(`register_typed_operation` and `register_composite_operation` in
`operations/typed_register.py`). A connector package sets them through the
connector kit (kit 1.7 or higher).

| Field | Values | Means |
|---|---|---|
| `hard_to_undo` | `deletes`, `stops`, `overwrites`, or none (the default) | A call can delete, stop or overwrite something, and no later call of the same connector brings it back. |
| `changes_rights` | yes or no (the default) | A call that changes who or what may reach or use something in the target system: a login, a group or role, an access list, a firewall rule. |

The mark means the same thing in every built-in connector. Three rules say
when a write gets `hard_to_undo: overwrites`:

1. A write that replaces a stored access rule, role or identity entry as a
   whole gets `hard_to_undo: overwrites`. No later call of the same
   connector brings the replaced entry back. The mark also goes on a write
   of data that neither the target nor the connector gives back, such as a
   secret value or a set of keys: the old state cannot be read, so it
   cannot be set back.
2. A write of one single setting gets no mark when the target system lets
   the old value be set back. The mark says something about the target,
   not about MEHO's own input checks: a limit of a handler's input is not a
   reason for the mark. Where a handler cannot write back a state that the
   target allows, that is a gap of the handler, not a reason for the mark.
3. A backup that writes over an older backup at the same path stays without
   the mark. The operation's own text says that it overwrites the backup at
   the same path. No mark depends on the parameters of a call.

A delete of an object gets `hard_to_undo: deletes` when no later call of the
same connector brings the object back as it was, also in one of MEHO's own
stores, whose connector is the only way in (for example
`topology.delete_node`: a node made again gets a new id, and the edges of
the old node do not come back).

`changes_rights` follows one sentence in every connector: a call changes
rights when it changes who or what may reach or use something in the
target system: a login, a group or role, an access list, or a firewall
rule. This holds at every risk level above `safe`, also `destructive`. A
create of a group or role counts, also when the new group is still empty.
A create, enable or disable of a login counts too.

- MEHO leaves an operation whose mark does not fit its risk level out at
  registration, and the log line names it. Every other operation
  registers, and the start goes on. The first such mark is
  `hard_to_undo` on a `safe` operation.
- MEHO leaves an operation that sets `changes_rights` at the risk level
  `safe` out at registration, and the log line names it. Every other
  operation registers. This holds for a typed and for a multi-step
  operation, with the switch `CUSTOM_POLICY` off and on.
- The checks are in `_MARK_FIT_CHECKS` in `operations/typed_register.py`;
  a new check is one more entry there.
- For a left-out operation, no row is written or deleted. A row that an
  earlier start, an import or a tenant stored under that key stays as it
  is, with the operator's settings on it. The start keeps the key in
  memory. For a left-out key, every stored built-in typed or multi-step
  row under that key is hidden, unless a registrar wrote that key in this
  same start. A tenant's row and an imported row are not touched
  (`operations/_left_out.py`). The lookups hide the row the same way they
  hide the rows of a connector package that is not installed
  (`installed_owner_clause` in `operations/_lookup.py` and its raw-SQL
  twin in `operations/_search.py`). So search, the group list and the
  connector list do not offer it, and a call answers
  `operation_left_out`, also when no row is stored. The start makes its
  left-out keys current before the background loops start, so nothing can
  call the hidden row. Every path that finds a row to run it hides the
  row the same way, also the forward path of the connectors (for example
  `k8s.ls`). A later start without the fault uses the stored row again,
  with the operator's settings.
- The log line is `operation_left_out`, with the operation, its connector
  (product, version, implementation and the connector package, if any)
  and the reason. When the operation is the test read a connector
  declares, the connector starts without a test read, and a second line,
  `connector_test_read_left_out`, names the connector and the operation
  (`connector-target-form.md`).
- Every other check of a registration still runs for a left-out operation
  (for example an unknown switch name in a parameter), and a second fault
  stops the start with its own message.
- Registration refuses any `hard_to_undo` word other than the three. The
  error names the operation and says why, and the start stops.
- Both are stored on the operation's row: `endpoint_descriptor.hard_to_undo`
  (a CHECK allows only the three words) and
  `endpoint_descriptor.changes_rights` (migration `0190`). NULL means no.
  Each start writes the values from the code again. An import of an API
  spec writes both as NULL on each row it writes: on a fresh import, on a
  re-import (a value stored on the row before goes away) and on an import
  into one tenant. An import never changes a built-in typed row.
  `backend/tests/test_imported_operation_marks.py` checks this through the
  real import.
- `backend/tests/test_operation_marks_stored.py` checks that each start
  stores the values of the code for every built-in operation, also on an
  upgraded database. It reads the values from the definitions in code, so
  it fails when a connector's register call does not pass the two fields on.
- With the switch `CUSTOM_POLICY` on, an operation with `hard_to_undo` has
  a delete sign, the same as a `DELETE` call. So a standing grant of a
  service login and a run approval do not cover a direct call of it: the
  call waits for a person. In sandbox mode a delete grant covers it, as for
  an operation with another delete sign, except for an operation that
  waits on every call because it can read, write or make a credential. The one
  read site is `_delete_shaped_reason_by_descriptor` in
  `operations/service_grants.py`. With the switch off, `hard_to_undo`
  changes no answer.
- No rule of MEHO reads `changes_rights`. No call waits, runs or is
  refused because of it.
- A delivery call of the Argo CD or Kubernetes connector (it deploys or
  runs a workload) carries no `hard_to_undo` on the whole operation. Its
  `changes_rights` stays as it is. `tests/test_delivery_calls_marks.py`
  names these calls and checks their stored rows.
- With the switch `CUSTOM_POLICY` on, such a call can have a delete sign
  of its own, read from its params. A connector registers a delete check
  with `declare_delete_check` (`op_classes.py`). The check gets the params
  of one call and returns one plain sentence that says what the call
  deletes, or `None`. A check that fails counts as a call that deletes.
  `raise_descriptor_for_params` (`operations/wait_when_set.py`) asks the
  check only with the switch on. It sets `requires_approval` and
  `delete_call_reason` on the row of that one call; the stored row does
  not change. The dispatcher and the preview both run it before the
  policy gate, so the preview shows the same wait as the call.
  `_delete_shaped_reason_by_descriptor` reads `delete_call_reason` as a
  delete sign: a standing grant and a run approval do not cover the call,
  and the fixed rule `floor:delete_waits` names it. The approval card
  shows the sentence (`proposed_effect.delete_wait`).
  - `argocd.app.sync` and `argocd.app.rollback`: a call with `prune`
    `true` waits for a person. A call with `prune` `false` or without
    `prune` keeps the rules of its operation.
  - `argocd.app.refresh` has no check: none of its params deletes.
  - `argocd.app.set`, `k8s.apply`, `k8s.job.create` and `k8s.exec` have no
    check: each of their calls waits for a person already, with the switch
    on or off.
  - `tests/test_delivery_call_delete_sign.py` sends each call through
    `call_operation` and `preview_operation`, with the switch on and off.

## Known limits

- The policy file import (`policy.apply`, see [policy.md](policy.md))
  replaces the whole document of a tenant or the install, also the entries
  that `policy.classify` wrote.
- On the pattern routes (`/api/v1/policy/classification`, the console
  page, `policy.classify`) a connector id, group key or operation id that
  holds `*`, `?` or `[` is read as a pattern. The dry run shows what it
  matches.
- The flight-recorder check (`test_flight_recorder_typed_spans.py`) runs
  with the switch off, so it does not list `policy-registry`.
