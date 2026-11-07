# Memory Migration: `meho migrate memory`

Migrate laptop-local Claude Code memory files to the MEHO backplane so they are
available across machines and to your tenant's AI assistant.

---

## What gets scanned

`meho migrate memory` scans a **memory directory** for Markdown files with
YAML front-matter (`name`, `description`, and optionally `type`). The directory
is resolved in this order:

1. `--source <path>` flag (explicit override)
2. `$CLAUDE_PROJECT_DIR/memory/` environment variable
3. `$HOME/.claude/projects/<sanitized-cwd>/memory/` (default Claude Code layout)

Only `*.md` files directly in that directory are read; sub-directories are
not. A file without valid YAML front-matter (delimited by `---`) is still
listed: the picker shows a warning for it, and its `type` counts as unset.

---

## The interactive picker

Running `meho migrate memory` (without `--non-interactive`) opens a terminal
picker for each found file:

| Step | What you see |
|------|-------------|
| **Action** | Choose: *Migrate to &lt;suggested scope&gt;*, *Migrate to a different scope…*, *Migrate (edit body first)*, *Skip (keep laptop-local)*. A machine-local file also gets *Skip — machine-local*. |
| **Scope** | Visible only when "Migrate to a different scope…" is selected. Today the list has only `user` (see *Scope suggestion* below). |
| **Slug** | Pre-filled from the filename; editable. Must match `^[a-z0-9][a-z0-9-]*$`. Hidden when you skip the file. |
| **Body edit** | Visible only when "Migrate (edit body first)" is selected. |
| **Confirm** | One final confirmation before any network call is made. |

The picker defaults to the suggested scope (see *Scope suggestion* below) and
pre-fills the slug from the filename: lowercased, each run of characters other
than `a-z` and `0-9` replaced by one hyphen, and `entry-` put in front when the
result is empty or starts with a digit.

Set `MEHO_ACCESSIBLE=1` for screen-reader-friendly plain-text output instead of
the interactive TUI.

---

## Machine-local heuristics

Files whose body contains path-like content that appears specific to the
current machine are flagged as **machine-local**. The heuristics check for:

| Category | Example match |
|----------|--------------|
| **Absolute paths with home dir** | `/Users/alice/projects/foo`, `/home/bob/src` |
| **Tilde-prefixed home** | `~/Documents`, `~/code` |
| **Windows home paths** | `C:\Users\alice\` |
| **Local host names** | `localhost`, `host.docker.internal`, `nas.local`, `router.lan` |
| **Operator username** | Three or more occurrences of your home directory name |

Machine-local files default to *Skip (machine-local content)* in the picker and
are **always skipped** in `--non-interactive` mode, even when
`--include-machine-local` is set. The rationale: machine-local content must be
reviewed interactively before leaving the laptop.

To mark a file as machine-local yourself, add this HTML comment anywhere in
the file:

```markdown
<!-- meho:machine-local -->
```

There is no comment that turns the check off. To migrate a flagged file, pick
a *Migrate* action for it in the picker, or start the picker with
`--include-machine-local`.

---

## Scope suggestion table

The picker pre-selects a scope based on the file's `type` front-matter field:

| `type` | Suggested scope | Rationale |
|--------|----------------|-----------|
| `user` | `user` | Personal workflow knowledge — scoped to you. |
| `feedback` | `user` | Coding preferences — personal by default. |
| `project` | `user` | Project context. MEHO suggests `user×tenant` only when a default tenant is set, and `meho migrate memory` does not read one today. |
| `reference` | `user` | Stable reference — personal by default. |
| *(unset / other)* | `user` | Conservative default. |

The command also does not read a default target or your role, so the scope
list in the picker has only `user` today. The full 5-scope
model (tenant, target, user×tenant, user×target, user) and its server-side
access control matrix are documented in the cross-repo guide:
[`docs/cross-repo/memory-migration.md`](../cross-repo/memory-migration.md)
(G5.1-T6, issue #427).

---

## Idempotency and re-runs

The CLI sends each entry to `POST /api/v1/memory` with its scope and slug. The
server keys a memory on its scope, your user and its slug (plus the target for
target scopes), so a re-run with the same scope and slug writes to the same
entry:

- **Same body, re-run** → no new entry; the server keeps the entry and moves
  `updated_at` forward.
- **Changed body, re-run** → the server updates the entry.
- **Different machine, same slug and scope** → the same entry; the last run
  wins.

The CLI does **not** deduplicate locally — it always POSTs and relies on the
server-side upsert contract.

---

## Flag reference

| Flag | Default | Description |
|------|---------|-------------|
| `--source <path>` | see *What gets scanned* | Override the memory directory to scan. |
| `--dry-run` | `false` | Print one JSON request body per entry that would be migrated; make no network call. |
| `--non-interactive` | `false` | Skip the picker; migrate `user` and `feedback` entries at their suggested scope. All other entries (`project`, `reference`, or no `type`) are refused: the CLI names each one and exits with an error after it sends the rest. Machine-local entries are always skipped. |
| `--include-machine-local` | `false` | Include machine-local entries in the dry-run preview and in the interactive picker (changes the default action from *Skip* to *Migrate*). Has no effect in `--non-interactive` mode. |
| `--mark-migrated` | `false` | Write the migration-complete marker when the run ends without an error and sent its entries (silences the post-login nudge). It is not written when you cancel, choose no entries, or an entry is refused. |
| `--backplane <url>` | from `meho login` config | Override the backplane URL. |

---

## Post-login nudge

After a successful `meho login`, if your default memory directory is non-empty
and the migration-complete marker is absent, MEHO prints:

```
Tip: you have N memory file(s) at <dir>. Run `meho migrate memory` to sync them to MEHO.
```

The nudge is non-fatal and never blocks or delays login.

The marker is the file `$XDG_CONFIG_HOME/meho/migrated-from/<dir>`
(`~/.config/meho/migrated-from/<dir>` when `XDG_CONFIG_HOME` is not set).
`<dir>` is the memory directory path with `/` and `:` replaced by `_`.
`--mark-migrated` writes it. Delete the file to see the nudge again.

---

## Error handling

Transient errors are HTTP 500/502/503/504 and transport errors such as
a refused connection or a timeout. In interactive mode, a transient error prompts **Retry / Skip /
Abort** at once. In `--non-interactive` mode, the CLI retries an entry up to
3 times; an entry that still fails is counted as `Errored` in the summary,
and the run goes on with the next entry.

Every other error (for example 401, 403, 404 or 422) is not retried. It stops
the run: the CLI prints the summary and exits with an error code (`2` for 401
or a missing login, `5` for 403, `4` for other answers). A 401 means your token
has expired — run `meho login` again.

The final summary line format:

```
Migrated: N, Skipped: M, Errored: K (retried R)
```

---

## Out of scope

- **Reverse migration** (`meho backup memory`) — planned as G5.4.
- **Conflict-resolution UI** for same-slug-different-machine — the last run
  wins; an explicit merge UI is future work.
- **Server-side storage, RBAC, TTL** — owned by G5.1 (#332) and G5.2 (#374).
- **The 5-scope decision tree** — see [`docs/cross-repo/memory-migration.md`](../cross-repo/memory-migration.md).
