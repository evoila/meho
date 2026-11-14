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
  refused. The message names the install entry and the part to remove;
  the other fields of the entry can stay. The install cannot end a wait
  that a tenant's own entry adds either.
- An install entry matches connectors by their id, not by product. A
  connector that a tenant registers itself, under its own id, is matched
  only by an install entry with a wildcard connector, for example
  `{connector: "*", op: "<op-id-pattern>"}`.

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

!!! note "Install-wide always wait and risk level"

    An install file can make operations always wait and raise their risk
    level. This holds for every tenant: a tenant cannot set it back. A
    tenant entry stored before this rule that sets it back has no effect
    for that part. `meho policy export` writes a `# Has no effect:`
    comment above such an entry, one for each part. Remove that part
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
