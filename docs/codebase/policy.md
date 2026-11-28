# The policy file (`policy: v1`)

The policy file holds the categories (the taxonomy), the
classifications of connectors, operation groups and operations
([policy-classification.md](policy-classification.md)), and the routing
of approvals: who may approve which waiting change
([approvals.md](approvals.md), "Approver routing"). It is a file of
its own, with its own command (`meho policy`) and its own REST routes. It
is **not** a section of the settings file
([settings.md](settings.md)): the settings import adds and updates but
never deletes, and a policy import must be able to remove a right.

Everything here is behind the switch `POLICY_CLASSIFICATION` or the
switch `CUSTOM_POLICY` (both off by default). With both off, the import is
refused with one sentence.
Validate, diff, export and history still work: they only read.

## The switches

Three parts of the policy each have their own switch, off by default:

- `POLICY_CLASSIFICATION`: categories, labels and "always wait" on
  operations ([policy-classification.md](policy-classification.md));
- `POLICY_ENGINE_SHADOW`: the policy evaluator runs next to today's
  checks and decides nothing ([policy-evaluator.md](policy-evaluator.md));
- `POLICY_APPROVER_ROUTING`: approvals go to the teams the file names
  ([policy-bindings.md](policy-bindings.md)).

`CUSTOM_POLICY` (Helm `config.policy.customPolicy`, off by default) is the
one switch of the custom policy model
([decision record](../decisions/2026-11-25-custom-policy-model.md),
decision 12). With it on, all three parts count as on. Each old switch
still works alone. With it off, each part follows its own switch.

One module decides whether a part is on:
`backend/src/meho_backplane/policy/switches.py`. Code asks it (or the
helpers that call it: `classification_enabled`, `shadow_enabled`,
`routing_enabled`), never the settings or the environment. Whether
`CUSTOM_POLICY` itself is on: `custom_policy_on(settings)`, or
`custom_policy_on_in_environment()` for code that runs before the
settings are loaded. `GET /ready`
shows `features.policy.custom_policy`, and for each part whether it is
on, by its own switch or by `CUSTOM_POLICY`. `GET /version` lists
`CUSTOM_POLICY` with the other switches that change what an agent sees.
`backend/tests/test_custom_policy_switch.py` checks all of this.

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
routing:
  - match: {category: "<category-a>"}
    approvers: [Operators, Admins]
  - match: {connector: "<connector-id-pattern>", risk: destructive}
    approvers: [Admins]
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
- Each `routing` entry has a `match` (at least one of `category`,
  `connector`, `risk`; `connector` is a pattern, the other two are exact)
  and `approvers`: one to five of the login group kinds `Readers`,
  `Operators`, `Admins`; the old kinds `SA-Operators` and `Approvers`
  stay as written and match only the members of that old login group
  (`Operator.old_group`, read from the token), never the whole new role.
  The order counts:
  in each file, the first entry that fits a waiting change wins. Two
  entries with the same `match` are a mistake. Routing only narrows who
  may approve; see [approvals.md](approvals.md), "Approver routing".
- **The install's routing is a floor** (decision of 15 November 2026). A
  tenant's routing can only make it narrower, never wider. The install's
  entry that fits counts first, then the tenant's; a person must be in
  both. See "The install's routing is a floor" below.
- `bindings` (a tenant's file only) says which login group is which team:
  a group, matched by its full path or its group attribute, gets one of the
  five group kinds and a set of categories. They count only while the
  switch `POLICY_APPROVER_ROUTING` is on. See
  [policy-bindings.md](policy-bindings.md).
- The one switch `POLICY_APPROVER_ROUTING` turns on both: the bindings at
  sign-in and the routing at the click.
- The file reserves seven top-level keys (`RESERVED_KEYS` in
  `policy/document.py`): `limits`, `agents`, and five refused keys,
  `approvers`, `role_bindings`, `risk_levels`, `custom_roles` and
  `baseline`. A file that holds one of the five is refused, whatever its
  value, in a tenant's file and in the install's file, with one sentence:
  "This version of MEHO does not read `approvers` in a policy file; remove
  it."
- `risk_levels` (`policy/risk_levels.py`) is the one exception, and only
  with the switch `CUSTOM_POLICY` on. The export writes the risk-level
  edits of its scope (`OperationPolicyOverride` rows that set
  `safety_level` or `requires_approval`): a tenant's own rows, or the
  install-wide rows. Each item has the rule id `override:<row id>`, the
  operation, the scope, the level or the approval flag, the reason, who
  set it and when. The import reads the part only to compare it: the same
  edits (in any order) change nothing, and any other part is refused with
  the line named. The stored document never holds the part.
- `limits` (the install's file only) is the outer limit of the tenants'
  policies: `limits.personal_agent_up_to` is the highest risk level
  (`safe`, `caution` or `dangerous`) that a tenant's rule for personal
  agents may let run at once. `destructive` is not a value: it always
  waits. Without the part the limit is `safe`; the shipped "small team"
  file has no such part. With the switch `CUSTOM_POLICY` on, a platform
  admin imports it with `meho policy import --scope install`; the import
  is a change of the policy like every `policy.apply`: in the web console
  it runs at once after a fresh sign-in, with a token it waits for a
  platform admin's click in the web console, and an agent login, a
  service login or a hosted agent run is refused before anything waits.
  The routes (`/api/v1/policy/validate`, `import`, `export`, the file of
  `explain`) and `policy.apply` (its approval card and its run) read the
  switch with `custom_policy_on(get_settings())` and pass it as
  `custom_policy` to `build_plan`, `apply_policy` and `export_policy`.
  The run reads the switch when it runs, not when it was asked.
- A tenant's file that holds the key `limits` is refused whatever its
  value, with the switch on or off: "`limits` belongs to the install's
  policy file. Only a platform admin sets the outer limit; a tenant's file
  stays inside it." The install's file with the switch off is refused the
  same way (also `limits: {}`, `limits: null` and `limits:`): "`limits`
  needs CUSTOM_POLICY: the outer limit counts only while the
  CUSTOM_POLICY setting is on. Remove the part, or ask the person who
  runs MEHO to turn the setting on." With the switch on, an empty part
  means the same as no part. A `limits` value of the wrong type gets its
  type sentence first, for example "`limits` must be a mapping of named
  keys."; an unknown key gets "`limits.<key>` is not a known key."; and
  `destructive` or another unknown level gets "`limits.personal_agent_up_to`
  is not a level a limit can have; use safe, caution or dangerous. A
  destructive operation always waits."
- With the switch on, the export of the install's file writes `limits`,
  and the exported file imports again with "No change.".
- With the switch off, a stored `limits` part is ignored: the export
  writes no `limits` part, so the exported file imports again; the diff
  has no `limits` section; a file that equals the stored policy apart from
  that part is "No change."; an approved import keeps the stored part as
  it is; `personal_agent_limit` answers `safe`. Classification reads the
  stored policy key by key, so a stored `limits` part does not change it.
- `install_personal_agent_limit(session, custom_policy=...)` in
  `policy/limits.py` reads the install's stored policy (`tenant_id IS
  NULL`) and answers with `personal_agent_limit`. A stored value that
  MEHO cannot read (not a mapping, or not a limit level) never counts as a
  looser limit: the answer is `safe`, the strictest limit, and one warning
  `policy_limit_not_readable` names the stored place (`install:limits` or
  `install:limits.personal_agent_up_to`). No call decision reads the limit
  in this version.

## Personal agent rules (`agents`)

`policy/agent_rules.py` holds the rule shape, its checks and the rule
match, as pure functions. `policy/agent_rules_plan.py` adds the rules to
the import plan. The user guide is the part "Rules for personal agents"
of `docs-site/guides/policy-file.md`.

- The parser (`parse_policy_file(content, custom_policy=...)` in
  `policy/document.py`): with the switch off, it refuses the key `agents`
  like the five refused keys (`AGENTS_NEED_CUSTOM_POLICY`), so that
  sentence comes back also when the file has other mistakes, and the
  checks after the parse do not run. With the switch on, `agents` is a
  list of at most 200 rules (`Field(max_length=200)`, like the other lists
  of the file), each kept as written; `document_of` stores the rules with
  only the keys that are set (`stored_agents`), and `agents: null` as
  `[]`, so the checks still see the key. The order of the rules does not
  count in `canonical_sha256`.
- `agent_rules_mistakes`, called by `check_document`: with the switch off,
  a document that holds the key `agents` is refused whatever its value
  (`AGENTS_NEED_CUSTOM_POLICY`); the install's file never holds it
  (`AGENTS_IN_INSTALL_FILE`); then the shape of each rule, with its place
  (`agents[N].caller`, ...); `caller: person` and `caller: service_agent`
  are "not in this version"; `rights_changes: allow` needs `connectors`;
  a repeated name; and a rule whose `up_to` is above
  `personal_agent_limit(install_document, ...)`.
- A stored tenant file that is now above a lowered limit is refused on its
  next import, also when it did not change: the checks run before "No
  change." is decided. So the tenant admin must lower the rule; the file
  does not import again as it is.
- How a rule is read (`rule_for`, `allowed_level`, `rule_lets_run`): a
  rule that names the connector wins over one without `connectors`;
  between rules that match equally, the lower `up_to` wins, then the rule
  without `rights_changes`. What counts is the lowest of the rule, the
  outer limit and the role's limit (`ROLE_LIMITS`: Reader `safe`,
  Operator `caution`, Admin `dangerous`). A `changes_rights` operation
  runs only under a rule that names its connector with
  `rights_changes: allow`. The rule id is `tenant_rule:<name>` (`rule_ref`).
- `agents_plan` (switch on only): a tenant's plan gets `agents` (rules
  added, changed, removed, by name) and `runs_at_once`, both only when one
  side has a rule. `agents_diff` keys a stored rule on its name; a stored
  rule that is not a mapping, or whose `name` is not text, keys on its
  whole text, so the plan shows it as removed and a valid file replaces it.
- `runs_at_once` holds, for each role, the operations that run at once
  under the file's rules and the file's classifications, and wait under
  the stored rules and the stored classifications
  (`runs_at_once_by_role(..., before=..., before_ops=...)`; `_rule_ops`
  runs once for each side of the plan). An edit that changes neither the
  rules nor what waits lists none. `_rule_ops` reads the operations with the functions
  dispatch uses (`lookup_descriptor`): `visible_scope_clause` (the
  tenant's own copy of a connector in place of the built-in rows, so each
  operation once), `enabled_for_tenant_clause` (the tenant's own on/off
  choice), `installed_owner_clause` and `row_code_missing`. The values
  are `policy_in_force` with the overrides of `load_overrides` (an admin's
  per-operation risk level and approval) and the classification of that
  side. An operation is never listed when it is `safe` (a token runs it
  at once today), or when a check that no rule lowers makes it wait:
  `destructive` (by code, edit or policy), "always wait" or a
  classification conflict, `never_covered_reason` (credential, per-call
  approval, caller address, delete sign), a MEHO change of rights or of
  the policy, a root shell, `hard_to_undo`, a multi-step operation, or
  approval in force that is not the plain mark. An operation is not
  listed for a role below its role floor (`role_floor_for`), because a
  person of that role cannot call it at all. An operation with the plain
  mark (`has_plain_mark`) in force counts as `dangerous`. An install plan
  gets `agents_above_limit` (each tenant with a stored rule above the
  file's limit, one warning each) only when there is such a tenant.
- The command line (`printAgents` in `cli/internal/cmd/policy/file.go`):
  `meho policy diff` and the import output print `+ agents: <name>`,
  `~ agents: <name>` and `- agents: <name>` (a rule without a readable
  name shows its whole text), and for each role
  `runs at once for <role>: <connector> <op> (<risk>, <rule>)`. A file
  that changes no rule prints no rule line.
- `rules_from_document` reads the stored rules; a part MEHO cannot read
  gives no rule and one warning `policy_agent_rules_not_readable`.
- With the switch off, a stored `agents` part is ignored like `limits`:
  the export leaves it out, the diff has no `agents` section, and the
  planned document keeps the stored part.
- No call decision reads the rules: every call gets the same answer with
  or without them.

## The shipped "small team" file

`deploy/policy-profiles/small-team.yaml` is MEHO's default policy. The
same file is packaged at
`backend/src/meho_backplane/policy/profiles/small-team.yaml` (listed in the
wheel `artifacts` in `backend/pyproject.toml`), so MEHO has it without a
network. It holds the built-in taxonomy (`DEFAULT_TAXONOMY` in
`policy/classification.py`) and no classification entries.

Until a policy for the whole install is stored, the install's policy is
this file (`builtin_install_document` in `policy/service.py`):

- `build_plan` compares an install file against it, so importing the file
  on a fresh install is "No change.", and `based_on` is its SHA-256;
- `export_policy` for `scope: install` writes it;
- a tenant import checks against its taxonomy.

The runtime (`policy/store.py`) does not read the file: with no stored
install policy it uses `DEFAULT_TAXONOMY` and no entries, which is the
same. `backend/tests/test_policy_small_team_parity.py` keeps the two
copies equal, keeps the file's taxonomy equal to `DEFAULT_TAXONOMY`, and
checks that storing the file changes no operation. For every registered
operation it compares, with and without the file: the risk level, the
wait, "always wait", the category, who may approve (`approvers_for`), and
the answer of `policy_gate` (run, wait or refuse, and the reason) for
seven kinds of caller, with a standing grant, an agent grant and a run
approval. It runs with `POLICY_CLASSIFICATION` and
`POLICY_APPROVER_ROUTING` on. The file has no `routing` section, so who
may approve stays today's rule. It cannot have `bindings`: they are for a
tenant's file only.

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
- **An install-wide "always wait" or risk level.** In a tenant's file, an
  entry with `always_wait: false` that matches an operation the install's
  policy makes always wait is refused. So is an entry that decides a lower
  `risk` than the install's level for a matching operation; a more
  specific tenant entry with the install's level or higher makes it fine.
  The sentence names the install entry, for example
  `install:classifications[0]`, and the operation where the part does not
  count. For a wide entry it suggests a more specific entry or a narrower
  match, not to remove the level, because the level still counts for the
  other operations.
- Two entries that give one operation a different category (or a different
  value for one label key) at the same level.
- **Routing** (`policy/routing.py`, `routing_mistakes`): an unknown
  category or risk level in `match`; a group kind that may approve none of
  the changes the entry routes ("`routing[N]` asks `ROLE`, but `ROLE` has
  no `approve` rule for these changes."); and a risk level that no group
  kind of the entry may approve ("A LEVEL change in category CAT can wait,
  but no role may approve it. It would wait until it expires."). An entry
  without `risk` is checked on all four risk levels. Readers never
  approve; Operators approve only `safe` and `caution`
  changes of other people.
- **Routing and the role floor** (`policy/validate.py`,
  `_routing_floor_mistakes`): only Admins approve an operation that needs
  the `tenant_admin` role (for example `targets.register`). For each known
  operation with that floor, the check works like the run: for each risk
  level the operation can wait at (its level and every level above), the
  first entry that fits wins (by its category, its connector, and a
  `risk` that is that level, or no `risk`). Only that entry is checked.
  When it does not ask Admins, that is a mistake ("`routing[N]` fits
  `OP` of connector `C`, but only MEHO-Admins may approve it and the entry
  does not ask them. It would wait until it expires."). So an earlier
  entry with `risk: caution` that asks Admins covers the `caution` level,
  and a later entry is not checked at that level.

- **A tenant's routing wider than the install's** (`policy/routing_floor.py`,
  `tenant_floor_mistakes`, only while `POLICY_APPROVER_ROUTING` is on): see
  the next section.
- **Limits** (`policy/limits.py`, `limits_mistakes`): the check reads
  whether the file has the key `limits`, not whether it has a value. A
  tenant's file that holds it is refused, and so is the install's file
  that holds it while the `CUSTOM_POLICY` switch is off ("`limits` needs
  CUSTOM_POLICY: ...").

At most 50 mistakes are listed, then one line says how many more there
are.

## The install's routing is a floor

The rule: when the install's file says "changes of category X are approved
by group A", a tenant can only narrow that. The people who may approve in
the tenant are always some of the people the install allows. For a
category that the install does not route, the tenant is free.

Example. The install's file:

```yaml
routing:
  - match: {category: network}
    approvers: [Admins]
```

A tenant's file may say `approvers: [Admins]` for `category: network`
(the same). The import refuses `approvers: [Operators, Admins]` (an added
group), `approvers: [Operators]` (another group), and
`match: {risk: caution}` with every group (it would take the network
changes away from the install's routing). The sentence names the category
and the install's groups: "`routing[0]` lets MEHO-Operators approve
changes with category `network`, but the install's routing
(`install:routing[0]`) lets only MEHO-Admins approve them. A tenant can
only narrow the install's routing: name only these groups, or fewer."

**A tenant that moves an operation to another category.** A tenant may
give an operation another category for its own rules: by its file, by the
single classification edit (`policy.classify`), by an override of the
operation, or by a category of its own. The import does not refuse this.
The install's routing for the operation still applies: the install's
entries match on the category that the install's policy alone gives the
operation (`Classification.install_category`, from the install's entries
and the install's overrides only, carried as
`EndpointDescriptor.policy_install_category`). The tenant's entries match
on the category the classification gives (the tenant's). So the people
who may approve are the ones the install allows for the install's
category AND the ones the tenant allows for the tenant's category.

**Narrower, exactly.** Routing names group kinds only (the five kinds). A
login group, matched by its full path or its group attribute, counts as
the kind its binding gives it ([policy-bindings.md](policy-bindings.md)).
A tenant entry is narrower than an install entry when every kind it names
is also named by the install entry. So the floor works per group kind,
not per login group. The tenant's `bindings` decide which of its login
groups belong to a kind: when a tenant binds one more login group to
`Admins`, the people in it count as Admins for the install's routing too.

**Which install entries a tenant entry is checked against.** Every
install entry that can route a change the tenant entry also routes. Two
entries can share a change unless one field of their `match` tells them
apart:

- `category` or `risk`: both entries set it, to different values.
- `connector`: both set a pattern, and the patterns cannot fit the same
  connector id. When one pattern has no wildcard (`*`, `?`, `[`), MEHO
  checks that text against the other pattern. When both have wildcards
  and differ, MEHO counts them as sharing a change.

A field that one entry leaves out fits everything. All of this is decided
from the two files, at import time.

**Refused or only a warning.** The import refuses a wider tenant entry,
with two exceptions. In these cases the import accepts the file and gives
a warning with the same sentence and one more (`tenant_floor_warnings`):

- An earlier entry of the same tenant fits every change that the wider
  entry shares with the install entry. The first tenant entry that fits
  counts, so the wider entry does not count for those changes. Example:
  `routing[0]` is `{category: network} -> [Admins]` and `routing[1]` is
  `{risk: caution} -> [every group]`. The warning ends "`routing[0]` comes
  first and fits each of these changes, so `routing[1]` does not count for
  them."
- Both entries set a `connector` pattern with a wildcard, and the patterns
  differ. MEHO cannot tell from the two patterns whether they fit the same
  connector.

Both are safe as warnings: at the run and at the click, the install's
groups are the floor, so a wider entry never lets more people approve.

**Export.** With `POLICY_APPROVER_ROUTING` on, `meho policy export` of a
tenant writes a `# Has no effect where wider: ...` comment above each
routing entry that names more groups than an install entry it can share a
change with (`tenant_wider_notes`), the same sentence as the import. It
is the same idea as the `# Has no effect:` comment of a classification
entry.

**At the run and at the click.** When a request starts to wait, the first
install entry that fits counts, then the first tenant entry that fits. The
stored group kinds are the kinds of both (`approver_rule` names both, the
install's first, for example `install:routing[0], tenant:routing[1]`). At
the approve click MEHO first routes the request again with the install's
current file and the stored install category
(`approval_request.approver_install_category`, migration `0173`; the
tenant's category is in `approver_category`), then checks the stored
kinds. The approval card, the "may approve" sign and the push recipients
read the install's routing as it is now too (`InstallFloor`), so they say
what the click decides. The approve audit row names the install entry
that counted at the click (`approver_floor`).

One rule names the connector of an operation in every policy answer:
`connector_id_of` and `coordinates_of` in `policy/store.py`. With
`CUSTOM_POLICY` off, a policy entry matches a connector with no version by
the id `-`. The overrides report names it by its implementation id, the
empty text, and the carry-over dry run names it by its product. A call
and its approval card name the connector by the id the caller used.
The dry run's list reads no switch; its first field, `custom_policy_on`,
says whether `CUSTOM_POLICY` is on at that moment. With `CUSTOM_POLICY` on, it is named by its product.
An entry that matches it only by the product can then add a wait or raise
a level. It never takes a wait away, lowers a level or changes the
category or the labels. The details are in
[policy-classification.md](policy-classification.md).

A step of a multi-step operation gets the same categories as a direct
call of the same operation: the step gate reads the classification
entries and the override rows of the operation (`classify_step`). The
group of a step comes from one shared rule, `StepGroups.group_of` in
`policy/store.py`: the group of the tenant's own row of the step first,
then the group of the built-in row, and no group without a row. The step
gate and the step preview (through `classify_step`) and the carry-over
dry run all use it. `load_step_groups` reads the groups of many steps in
one query; the dry run calls it once. So a
step and a direct call wait for the same people. So a tenant entry stored
before this rule, or an install file that changed later, never widens
anything: where a tenant entry is wider, its extra groups do not count.
Nothing fails open: an install routing that is not valid refuses the
click. A request that waited with stored groups before migration `0173`
has the empty text as both categories: MEHO does not know them. Such a request is
refused when an install entry that matches on a `category` could fit it;
reject it and start the call again. A request can have stored group
kinds but no stored install category (NULL) in two cases:

- It started to wait while `POLICY_CLASSIFICATION` was off. No change has
  a category then, so MEHO stored none.
- A build of main between 15 November 2026 and pull request #6542 wrote
  it. Those builds stored the group kinds but not the install category.

MEHO does the same in both cases. While `POLICY_CLASSIFICATION` is on,
the category counts as "not known", like the empty text: the click is
refused when an install entry that matches on a `category` could fit the
request. Reject it and start the call again. While the switch is off, a
`category` entry fits nothing for it, as when it started to wait, and the
other install entries decide. A request without stored group kinds
(it started to wait while the switch was off) keeps today's rule until it
is decided or expires: the install's routing is not checked for it.

**A change of the install's file.** The preview of an install import
(`meho policy diff --scope install`) lists the tenants whose stored
routing no longer fits: the diff key `routing_floor` (one item per tenant,
with `tenant_id` and `entries`) and one warning per entry. It lists also
the entries a tenant import only warns about (an earlier entry fits first,
or two connector patterns MEHO cannot compare). The key is
there only while `POLICY_APPROVER_ROUTING` is on. Those entries
stay stored, but where they are wider they do not count.

`meho policy explain` shows the routing entries that fit (`rule`) and the
install's entry (`floor`): "Install's floor: install:routing[0] (a
tenant's routing can only narrow it)". For a waiting request
(`--request`) the groups and the floor come from the install's routing
as it is now, as at the click (`install_floor`). A request that the click
refuses for its unknown category shows no group and the rule
`category_not_stored`. The install's category is also not known when the
install's classification entries give the operation two categories (an
operation registered after the install's import can match two such
entries). Then explain shows no group for the call too, and the click
refuses it, when an install entry that matches on a `category` could fit.

**Two install categories: the rule** (decided on 17 November 2026).
Nobody may approve such a change when any install entry that matches on a
`category` could fit it, also when no install entry names either of the
two categories. The reasons: MEHO stores only "category not known", not
the two categories, so it cannot tell which install entry would count;
and the cause is a mistake in the install's file that only the install's
admin can fix (the install import refuses two categories for an operation
it knows). So MEHO fails closed. Fix the install's file so the operation
has one category, reject the waiting request, and start the call again.

All of this runs only while `POLICY_APPROVER_ROUTING` is on.

**The stricter of install and tenant counts** (decision of 12 November
2026). For the risk level and for "always wait", the install and the
tenant each find their own value; then the higher risk level counts, and
the operation always waits when either says so. So what the install's
file sets is a floor for every tenant, and a tenant can only make things
stricter. A more specific install entry with `always_wait: false` does
not end a wait that a tenant entry adds. The rule is in `classify`
(`policy/classification.py`), so dispatch, preview, search, the policy
evaluator and its compare-only run (`POLICY_ENGINE_SHADOW`) all follow
it. The details and the case of a connector that a tenant registers
under its own id are in [policy-classification.md](policy-classification.md).

**A stored tenant entry against the install.** A stored tenant entry
that sets an install-wide wait back, or that sets a lower risk level
than the install, has no effect for that part on that operation (its
other fields, and its level on other operations, still count).
`export_policy` writes one `# Has no effect: ...` comment for each such
part above the entry, with the same text as the import refusal, and a
new import of the same entry is refused until the tenant admin fixes
it.

## Plan, import, approval

`policy/service.py`:

- `build_plan` parses and checks the file and works out the diff. It
  writes nothing. The diff has the sections `description`, `taxonomy`,
  `classifications`, `routing`, `bindings` and `limits`, each with
  `added`, `changed` (`before` and `after`) and `removed`. The `limits`
  section is there only with the switch on, and only when the stored
  policy or the file has limits; each item names its `key`. A routing entry is found by its
  `match`; it is `changed` when its approvers or its place in the list
  changed (`index`, and `was` for the old place). Each classification entry also has `matches` (how many
  operations it matches) and `changes` (how many of those get other
  values). Entries are told apart by their whole `match` (each part on its
  own), so two different matches never share one line. `operations_changed` counts all operations whose values change.
  An install import also has `routing_floor` while
  `POLICY_APPROVER_ROUTING` is on: the tenants whose stored routing is
  wider than the new file (see "The install's routing is a
  floor").
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
  carries the rights-change mark: an admin approves it with a click in the
  web console, and may be the importer ([approvals.md](approvals.md)). An
  admin's own call in the web console runs at once. The REST import route
  is a token call, so it waits. The params are the
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
- The approval card of an import planned with `CUSTOM_POLICY` on (of the
  install's file or of a tenant's file) holds
  `planned_with_custom_policy: true`, because the switch decides whether
  the `limits` part of the install's file and the `agents` part of a
  tenant's file are read. A card planned with the switch off has only the
  keys it had before the switch existed, also in `meho_approvals_get` and
  `meho_approvals_list`. A request made under another release does not
  run after an update of MEHO: the release check refuses it, with
  `CUSTOM_POLICY` off or on. A card without the field counts as planned
  with the switch off; so does a card that waited during an upgrade. After the
  approval, the run reads the switch again. When it is not the same as on
  the card, the run writes nothing and gives the same sentence.
- The stored card of a tenant import planned with `CUSTOM_POLICY` on
  also holds `planned_with_install_limits`: the hash of the install's
  stored `limits` and `classifications` (`install_limits_sha256`). At the
  run after the approval, MEHO compares this recorded value with the
  install's stored policy and refuses the run when they differ: "The
  install's policy changed since this request was planned. Ask again."
  Nothing is written, and the audit row of the run holds the sentence.
  Only the run reads the recorded value, from the stored card.
- How each reader sees a stored import card:
  `connectors/policy/apply_card.py` (`policy_card_as_read`, registered
  with `register_card_view` in `operations/platform_admin_card.py`). Every
  view shapes the card in one call of `card_for_reader`: the REST
  approval views, the console (`project_request_to_view`) and the MCP
  approval tools. It runs the card view first and then the filter of the
  parts only a platform admin reads (`card_without_tenant_parts`, which
  also empties `agents_above_limit` and drops its `Tenant <id>:`
  warnings). The stored row never changes. With `CUSTOM_POLICY` off,
  every view shows the card without `agents`, `runs_at_once`,
  `agents_above_limit` and its warnings, `planned_with_custom_policy` and
  `planned_with_install_limits`. With it on, no reader gets
  `planned_with_install_limits` in any view (the REST approvals answers,
  `meho_approvals_get`, `meho_approvals_list`, `preview_operation`, the
  console detail page and the bell panel); it stays in the stored card,
  where the run reads it. A person over REST and in
  the console reads each role's whole `runs_at_once` list; every MCP
  answer (the approval tools and `preview_operation`, also for a person)
  and an agent or service login over REST get, for each role, `count`,
  the first 50 `items`, and `more` ("N more") when there are more. The
  policy history (`GET /api/v1/policy/history`) shapes each entry's
  `diff` the same way (`diff_as_read`); the stored revision never
  changes. A runner login
  reads no approval card. This is how the approvals tools hold the size
  rule (no agent sees a big raw answer): an approval card is not an
  operation result, so it has no result link. An admin's
  own run in the web console has no card and plans with the switch as it
  is.
- A run without an approval, or a run by the login that asked for it (for
  example its re-run of a failed request, `POST /api/v1/approvals/{id}/rerun`),
  writes only for an admin in the web console. Any other such run writes
  nothing and says: "A policy import runs at once only for an admin in the
  web console. Any other import waits until an admin approves it in the web
  console. Run the import again."
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
the diff then lists such an entry as removed. With approver routing
on, a bad `routing` entry makes every request of that tenant fail to start
waiting: the call is refused ("approval queue unavailable") instead of
waiting with an unknown approver set.

## Endpoints and CLI

Every file route takes `scope` (`tenant`, the default, or `install`). Callers:
`tenant_admin` or platform admin; `install` needs a platform admin. The
import is human-only.

| Route | CLI | What it does |
|---|---|---|
| `POST /api/v1/policy/validate` | `meho policy validate FILE` | mistakes and warnings |
| `POST /api/v1/policy/import?preview=true` | `meho policy diff FILE` | mistakes and the diff; writes nothing |
| `POST /api/v1/policy/import` | `meho policy import FILE` | 202 with the approval request; 200 "No change."; 422 with mistakes; 409 with the switch off |
| `GET /api/v1/policy/export` | `meho policy export [-o FILE]` | the stored policy as a file |
| `GET /api/v1/policy/history` | `meho policy history` | the last 20 imported files, newest first |
| `POST /api/v1/policy/explain` | `meho policy explain` | why a call runs, waits or is denied, and who may approve; with `CUSTOM_POLICY` on, also whether the person who asked may approve their own request (`own_approval`); runs nothing ([policy-evaluator.md](policy-evaluator.md)) |
| `GET /api/v1/policy/carry-over/dry-run` | `meho policy carry-over --dry-run` | every saved "always wait" and what changes if MEHO reads it as `destructive`; writes only its audit row; an agent login gets 403 (`policy/carry_over.py`) |

Code: `backend/src/meho_backplane/api/v1/policy.py`,
`cli/internal/cmd/policy/`.

## Tests

`backend/tests/test_policy_document.py` (parser, floors, plan, apply,
versions), `backend/tests/test_policy_bindings.py` (bindings), `backend/tests/test_api_v1_policy.py` (routes, waiting import,
approval by an admin in the web console, an admin's import in the console),
`backend/tests/test_policy_small_team_parity.py` (the "small team" file:
round trip and no change over every operation),
`backend/tests/migrations/test_migration_0163_add_policy_revision.py`,
`backend/tests/test_policy_routing.py` (the `routing` section, its checks,
diff and export, the "separated duties" file, the stored approver kinds
and the click), `backend/tests/test_policy_routing_floor.py` (the
install's routing as a floor: import, run, click, install diff),
`backend/tests/test_policy_routing_step_floor.py` (a step and a direct
call get the same approvers, warnings and export marks, the card and the
approve audit row),
`backend/tests/test_policy_limits.py` (the `limits` part and the reserved
keys: parser, checks, import, export, the reader),
`backend/tests/test_api_v1_policy_limits.py` (the same through the routes
and `policy.apply`: a stored limit with the switch off, the approved run,
the refusal sentences),
`backend/tests/test_policy_carry_over_dry_run.py` (the dry run of the
"always wait" carry-over: the list, steps at every level, the operations
of a tenant), `backend/tests/test_policy_carry_over_dry_run_access.py`
(who may run it, every table the same, an override keeps waiting),
`backend/tests/test_policy_carry_over_dry_run_step_warning.py` (a step
warning exactly when the step check makes the step wait, in both scopes),
`backend/tests/test_policy_carry_over_step_grid.py` (a grid of 396
cases: the dry run names a multi-step operation exactly when the step
gate makes the step wait; the three follow the shared group rule; one
query for the groups of all steps),
`backend/tests/test_policy_connector_without_version.py` (with
`CUSTOM_POLICY` on, a connector with no version is named by its product in
every answer; no reader builds the id itself),
`backend/tests/test_policy_connector_product_form.py` (with `CUSTOM_POLICY`
on, the product form can only add a wait or raise a level, in both scopes;
the import check sees an install entry of either form, and its refusal
names the operation with its product and suggests an entry for that
product; explain names the entry that makes the call wait; the approval
card's `classification_rule` is an entry whose value the card shows),
`backend/tests/test_policy_connector_without_version_switch_off.py` (with
`CUSTOM_POLICY` off, the overrides report names a connector with no
version by its implementation id; a call, the preview and the approval
card match it as `-`; the dry run names it by its product with every switch
state),
`backend/tests/test_api_v1_policy_install_limits.py` (the outer limit with
`CUSTOM_POLICY` on: import, export and import again, who may ask and
approve, the audit rows, a wrong part, a stored value MEHO cannot read,
the approval table with no part, the switch turned off again),
`backend/tests/test_api_v1_policy_install_limits_card.py` (the `limits`
part on the approval card of an install import, and a run with the switch
not the same as on the card),
`cli/internal/cmd/policy/file_test.go`, `cli/internal/cmd/policy/carry_over_test.go`.
