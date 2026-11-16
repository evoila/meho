# The policy file

The policy file says which category each connector, operation group and
operation is in, which labels it has, and which operations always wait
for approval. It can also route approvals: for example, only
MEHO-SA-Operators and MEHO-Admins approve network changes.

You keep the policy as one file, `policy: v1`. You can:

- **validate** a file and see every mistake,
- see a **diff**: what an import would change (a dry run),
- **import** a file. A second admin must approve the import. Only a
  person can ask for an import, never an agent. Only a platform admin can
  ask to import the install's policy,
- **export** the stored policy as a file,
- read the **history**: the last 20 imported files.

!!! note "Off by default"

    The policy file needs the setting `POLICY_CLASSIFICATION` (Helm
    `config.policy.classification`). While it is off, MEHO refuses an
    import with one sentence.

The policy file is not part of the [settings file](settings-file.md). It
has its own command, `meho policy`.

## A small example

```yaml
policy: v1
description: "Rules for the network team"
taxonomy:
  categories: ["firewall"]        # adds a category for your tenant
classifications:
  - match: {connector: "<connector-id>"}
    category: network
  - match: {connector: "<connector-id>", group: "<group-key>"}
    labels: {team: "<team-name>"}
  - match: {op: "<op-id-pattern>"}
    always_wait: true
```

- `match` says what the entry is for: a connector, an operation group, an
  operation, or a mix. `*` is any text, `?` is one character.
- An entry sets one or more of: `category`, `labels`, `risk`
  (`safe`, `caution`, `dangerous`, `destructive`) and `always_wait`.
- `labels` follow the same rules as a single label change: at most 32
  labels, a key of 1 to 63 characters, a value of 1 to 255 characters.

## Who approves: routing

The `routing` section says which login groups may approve a change that
waits. It needs the setting `POLICY_APPROVER_ROUTING` (Helm
`config.policy.approverRouting`), off by default. This one setting turns on
both parts: the bindings at sign-in (next section) and the routing at the
click.

```yaml
routing:
  - match: {category: network}
    approvers: [SA-Operators, Admins]
  - match: {connector: "<connector-id-pattern>", risk: destructive}
    approvers: [Admins]
```

- `match` names a `category`, a `connector` (`*` is any text) and a
  `risk` level, or some of them. All that it names must fit.
- `approvers` lists login group kinds: `Readers`, `Operators`,
  `SA-Operators`, `Admins`, `Approvers`.
- In each file, the first entry that fits wins. When no entry fits, the
  same people as today may approve.
- **The install's routing is a floor.** A tenant can make it narrower,
  never wider (see below).
- Routing only makes the list shorter. A group never approves more than it
  may today: for example, Operators and Approvers never approve another
  person's `dangerous` or `destructive` change.
- MEHO refuses an entry that asks a group that may approve none of its
  changes, and an entry that leaves a risk level that nobody may approve.
- Only Admins approve an operation that needs the admin role, for example
  registering a target. MEHO refuses an entry that fits such an operation
  and does not ask Admins.
- A change of rights (for example a policy import) is not routed. Another
  admin approves it, as today.

When a change starts to wait, MEHO stores the groups that may approve it.
The approvals page shows "Who can approve", and the **For you** tab lists
the changes you may approve. A person outside the stored groups gets one
sentence that names who may approve. A change that started to wait before
the setting was on keeps today's rule.

### The install's routing is a floor

When the install's file says "changes of category X are approved by group
A", a tenant can only narrow that. The people who may approve in the
tenant are always some of the people the install allows. For a category
that the install does not route, the tenant is free.

Example. The install's file says:

```yaml
routing:
  - match: {category: network}
    approvers: [SA-Operators, Admins]
```

A tenant's file may then say `approvers: [Admins]` for network changes.
MEHO refuses the tenant's file when an entry:

- adds a group, for example `[Operators, SA-Operators, Admins]`;
- names another group, for example `[Approvers]`;
- takes the network changes away from the install's entry, for example
  `match: {risk: caution}` with every group.

The refusal names the category and the install's groups.

How MEHO decides: an entry is narrower when every group it names is also
in the install's entry. A tenant entry is checked against every install
entry that can fit the same change. Two entries cannot fit the same change
when both name a `category` (or a `risk`) and the values differ, or when
their `connector` patterns cannot fit the same connector. When both
connector patterns have `*`, `?` or `[` and differ, MEHO counts them as
fitting the same change. A login group, matched by its full path or its
group attribute, counts as the group kind its binding gives it.

When a change waits, the install's entry counts first, then the tenant's.
A person must be in both. At the approve click MEHO checks the install's
file as it is then. So a tenant entry stored before this rule, or an
install file that changed later, cannot widen anything. When the install's
file changes, `meho policy diff --scope install` lists the tenants whose
routing no longer fits. Their entries stay, but where they are wider they
do not count.

MEHO ships a starting file, "separated duties"
(`deploy/policy-profiles/separated-duties.yaml`): identity and secret
changes only Admins approve, network and storage changes only
SA-Operators and Admins.

## Which login group is which team

A tenant's file can say which login group is which team, in `bindings`.
A binding gives a group one of the five group kinds (`Readers`,
`Operators`, `SA-Operators`, `Admins`, `Approvers`) and a set of
categories.

```yaml
bindings:
  - match: {group: /MEHO-Operators/Network}   # the full path of the group
    kind: SA-Operators
    categories: [network]
  - match: {attribute: "<tenant>-storage"}    # the group attribute meho_group
    kind: Operators
    categories: [storage]
```

- MEHO matches the group by its **full path** or by its **group
  attribute** `meho_group`, never by its short name. A short name can
  belong to more than one group. The login setup must send full paths or
  the attribute: see [Keycloak realm setup](../install/keycloak-realm.md).
- `categories: ["*"]` means every category.
- A binding never changes a person's base level (read-only, operator or
  admin). It can only pick between the kinds of that level: `Operators`
  or `SA-Operators`, `Readers` or `Approvers`.
- A person whose groups match two kinds in one tenant cannot log in. Fix
  the bindings or the groups.
- A person whose groups match no binding keeps the kind they have today.
- `principal: agent` or `principal: service` binds a machine login. It can
  only get `Readers`: only a person approves. A runner gets no binding.
- A token whose `groups` claim is not a list of texts matches no binding.
- When your file has no bindings, MEHO uses the shipped bindings: the
  groups of the settings `JWT_ROLE_GROUPS` and `JWT_SELF_APPROVER_GROUP`,
  by their full path. They count only for a person whose token has no
  tenant list (`meho_tenants`, the older login setup). With a tenant list,
  the person's entry for the tenant decides, as without bindings.
  `meho policy export` shows them.
- A shipped binding takes its kind from the **end** of the group name:
  `Corp-Operators` gives `Operators`. If your site renamed the groups,
  check that each name ends with its kind. Run `meho policy export` and
  read `bindings`. A group whose name ends with no kind (for example
  `Corp-Read-Only`) is not there: write its binding yourself.
- `meho policy diff` and the import compare your file with the **stored**
  bindings, not the shipped ones. So the first import of an exported file
  lists the shipped bindings as added. A file without `bindings` removes
  the stored ones, and the shipped bindings count again.
- A binding can name any group of the login setup, also a group of
  another tenant. Who is in that group is then decided by whoever manages
  it. Bind only groups that your tenant manages. Two admins of the tenant
  decide the bindings: one imports the file, a second one approves it.

!!! note "Off by default"

    Bindings count only while the setting `POLICY_APPROVER_ROUTING` (Helm
    `config.policy.approverRouting`) is on. The policy commands, and so
    the import of bindings, also need the setting `POLICY_CLASSIFICATION`
    (Helm `config.policy.classification`).

## Rules that no file can loosen

MEHO checks each file against these rules. A file that breaks one is
refused, and the message names the entry and the operation.

- An operation that shows a secret stays at least `caution` and always
  waits for approval.
- An operation that connects to an address the caller gives stays at least
  `caution` and always waits.
- A file can raise a risk level, but never lower it below the level in
  MEHO's code.
- **The stricter of install and tenant counts.** What the install's file
  sets is a floor for every tenant. A tenant's file can raise a risk level
  and make more operations wait. It cannot set a lower risk level than
  the install, and cannot end an install-wide wait: such an entry is
  refused. The message names the install entry and the operation. The
  install cannot end a wait that a tenant's own entry adds either.
- A wide tenant entry, for example `{match: {connector: X}, risk: caution}`,
  is refused when the install sets one operation of `X` higher. The level
  does not count for that operation, but it still counts for the other
  operations of `X`. So do not remove the level. Add a more specific
  tenant entry for that operation with the install's level or higher, or
  leave the operation out of the match. With such an entry, the file is
  accepted. The message names the operation and its connector. Put both
  in the new entry, for example
  `{match: {connector: X, op: <op-id>}, risk: <level>}`. The same
  operation id can exist on another connector, for example on another
  version of the product. An entry without the connector sets that
  operation's level too. When the refused entry names a group, the
  message names the group as well. Put it in the new entry too, for
  example `{match: {connector: X, group: <group>, op: <op-id>}, risk: <level>}`.
  Without the group, the new entry is less specific than the refused
  one, and the file is refused again.
- Which install values make **every** call wait: `always_wait: true` and
  `risk: destructive`. With `risk: dangerous`, a call waits too, but
  two things can still let it run without a new approval: a standing
  grant of a service account, and a run approval of a service account.
  An agent grant cannot: a `dangerous` call of an agent always waits.
- An install entry matches connectors by their id, not by product. A
  connector that a tenant registers itself, under its own id, is matched
  only by an install entry with a wildcard connector, for example
  `{connector: "*"}` for every operation of every connector, or
  `{connector: "*", op: "<op-id-pattern>"}` for some operations.

## Commands

```bash
meho policy validate policy.yaml     # list every mistake
meho policy diff policy.yaml         # what an import would change
meho policy import policy.yaml       # ask to import; a second admin approves
meho policy export -o policy.yaml    # the stored policy as a file
meho policy history                  # the last 20 imported files
```

Add `--scope install` to work on the policy of the whole install. Only a
platform admin may do that.

## Why does a call run, wait or fail? `meho policy explain`

`meho policy explain` says what MEHO does with one call: it **runs**,
**waits** for approval, or is **denied**. It also says why, and who may
approve it. Nothing runs and nothing is written.

```bash
meho policy explain <connector-id> <op-id> --me
meho policy explain <connector-id> <op-id> --groups Operators --target <target-name>
meho policy explain <connector-id> <op-id> --groups Operators --principal agent
meho policy explain <connector-id> <op-id> --groups Operators --file policy.yaml
meho policy explain --request <request-id>
```

- `--me` asks for yourself. You need the operator role, as for a call.
- `--groups` asks for a login in these groups, with no grants of its own.
  `--principal` says if it is a `person` (the default), an `agent` or a
  `service` login. `--via web` asks for a person in the web interface.
  For a person, `--via` is `token` by default with `--groups`, and as you
  call with `--me`. Only a tenant admin or a platform admin may ask for
  other groups.
- `--file` asks with a policy file that is not imported. The file takes
  the place of your stored policy. It needs `POLICY_CLASSIFICATION`.
- `--request` shows the rule stored on an approval request, and the
  groups that may approve it. The groups stored on the request count only
  while `POLICY_APPROVER_ROUTING` is on. With it on, the install's routing
  as it is now narrows them, as at the approve click. With it off, explain
  shows today's groups, the same groups the approve check uses.
- With routing on, "Who may approve" names the routing entries that fit,
  the install's first. "Install's floor" names the install's entry: a
  tenant's routing can only narrow it.

Good to know:

- explain asks about a call **with no parameters**. For an operation
  whose answer depends on its parameters, the answer is the one of a call
  without parameters.
- explain works with `POLICY_ENGINE_SHADOW` off. It then shows today's
  answer only.
- Only a tenant admin or a platform admin sees the entries of the policy
  and their places in it. Everyone else gets the answer, the reason, the
  risk level and who may approve.

An example answer, for a person with a token who asks for a `caution`
operation that a file makes always wait:

```text
Answer: wait (today's checks decide)
Who may approve: MEHO-Operators, MEHO-SA-Operators, MEHO-Admins, MEHO-Approvers (rule: default)
Today: needs-approval: <today's reason>
Policy evaluator (shadow): wait, same as today
Decided by: floor token_runs_only_safe: wait (<rule text>)
Floor: floor token_runs_only_safe: wait (<rule text>)
Rules in order:
  1. default person.token.caution: wait (risk level caution waits)
  2. default always_wait: wait (the operation always waits) at file:classifications[1]
  3. floor token_runs_only_safe: wait (<rule text>)
Risk level: caution, always waits: yes, category: compute
  matched file:classifications[0]
  matched file:classifications[1]
```

!!! note "The policy evaluator is off by default"

    The lines about the policy evaluator need the setting
    `POLICY_ENGINE_SHADOW` (Helm `config.policy.engineShadow`). With it
    on, the evaluator works out its own answer next to today's checks, and
    explain lists every rule it matched, in order. Only the `always_wait`
    step carries a place in the policy file (for example
    `file:classifications[1]`). The other classification entries that
    match are listed under the classification (the `matched` lines).
    Today's checks still decide. With the setting off, explain shows
    today's answer only.

## What an import does

1. MEHO checks the file and works out the diff.
2. If the file changes nothing, MEHO answers "No change." Nothing waits.
3. Otherwise the import waits for approval. **Nothing changes yet.** You
   can never approve your own import. Another tenant admin approves it in
   the web console or on the command line.
4. When it is approved, MEHO checks again. If the stored policy changed in
   the meantime, the import does not run: run the diff again and import
   again. An approved import that failed cannot run again. Import the file
   again, and a second admin approves the new request.
5. The import **replaces the whole policy**. An entry that is not in the
   file is removed.

!!! note "Install-wide always wait and risk level"

    An install file can make operations always wait and raise their risk
    level. This holds for every tenant: a tenant cannot set it back. A
    stored tenant entry that sets it back has no effect for that part.
    `meho policy export` writes a `# Has no effect:` comment above such an
    entry, one for each part. The comment names the operation where the
    part does not count, and says how to fix the entry. Fix it before you
    import the file again.

## REST

| Route | What it does |
|---|---|
| `POST /api/v1/policy/validate` | mistakes and warnings |
| `POST /api/v1/policy/import?preview=true` | mistakes and the diff |
| `POST /api/v1/policy/import` | asks to import; waits for approval |
| `GET /api/v1/policy/export` | the stored policy as a file |
| `GET /api/v1/policy/history` | the imported files |
| `POST /api/v1/policy/explain` | why a call runs, waits or is denied; runs nothing |

Send the file as the request body with `Content-Type: application/yaml`.
Every file route takes `?scope=tenant` (the default) or `?scope=install`.
`explain` takes a JSON body with the same questions as the command.
