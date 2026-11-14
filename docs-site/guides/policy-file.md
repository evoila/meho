# The policy file

The policy file says which category each connector, operation group and
operation is in, which labels it has, and which operations always wait
for approval. Later, approvals will be routed by category: for example,
only the network team approves network changes.

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
- When the install's policy makes an operation always wait, it waits in
  every tenant. A tenant's file can make more operations wait. An entry
  with `always_wait: false` that would end an install-wide wait is
  refused, and the message names the install entry.

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

!!! note "Install-wide always wait"

    An install file can make operations always wait. This holds for every
    tenant: a tenant cannot set it back. A tenant entry stored before this
    rule that sets it back has no effect on the wait. `meho policy export`
    writes a `# Has no effect:` comment above such an entry. Remove it
    before you import the file again.

## REST

| Route | What it does |
|---|---|
| `POST /api/v1/policy/validate` | mistakes and warnings |
| `POST /api/v1/policy/import?preview=true` | mistakes and the diff |
| `POST /api/v1/policy/import` | asks to import; waits for approval |
| `GET /api/v1/policy/export` | the stored policy as a file |
| `GET /api/v1/policy/history` | the imported files |

Send the file as the request body with `Content-Type: application/yaml`.
Every route takes `?scope=tenant` (the default) or `?scope=install`.
