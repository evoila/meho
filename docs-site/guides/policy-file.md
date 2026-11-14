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

## Start from the "small team" file

MEHO ships a default policy file, "small team":
[`deploy/policy-profiles/small-team.yaml`](https://github.com/evoila/meho/blob/main/deploy/policy-profiles/small-team.yaml).
It describes what MEHO does when no policy file is imported:

- it lists the six built-in categories (`network`, `storage`, `compute`,
  `identity`, `secrets`, `other`),
- every operation has the category `other`,
- it has no entries, so every operation keeps the risk level and the
  approval rule it has in MEHO's code.

Until you import a file for the whole install, MEHO uses this file. So
importing it changes nothing:

```bash
meho policy validate --scope install small-team.yaml # no mistakes
meho policy diff --scope install small-team.yaml     # answers "No change."
```

Check this file with `--scope install`. Without it, `validate` checks the
file as a tenant's file and reports `taxonomy.default` as a mistake.

On a fresh install, `meho policy export --scope install` writes the same
policy, without the comments. To start your own policy:

1. Export the install's policy, or copy `small-team.yaml`.
2. Add your entries under `classifications`, and your own categories under
   `taxonomy.categories`.
3. Run `meho policy validate --scope install` to check it, and
   `meho policy diff --scope install` to see what changes.
4. Import it. A second admin approves the import.

The "small team" file is for the whole install (`--scope install`). A
tenant's own file cannot set `taxonomy.default`.

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
