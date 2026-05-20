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

Each file must contain valid YAML front-matter delimited by `---`. Files without
front-matter are silently skipped.

---

## The interactive picker

Running `meho migrate memory` (without `--non-interactive`) opens a terminal
picker for each found file:

| Step | What you see |
|------|-------------|
| **Action** | Choose: *Migrate (suggested scope)*, *Migrate (pick a different scope)*, *Migrate (edit body first)*, *Skip (machine-local content)*, *Skip (manual)*. |
| **Scope** | Visible only when "Migrate (pick a different scope)" is selected. |
| **Slug** | Pre-filled from the filename; editable. Must match `^[a-z0-9][a-z0-9-]*$`. |
| **Body edit** | Visible only when "Migrate (edit body first)" is selected. |
| **Confirm** | One final confirmation before any network call is made. |

The picker defaults to the suggested scope (see *Scope suggestion* below) and
pre-fills the slug from the filename (lowercased, spaces/underscores replaced
with hyphens, leading digits prefixed with `entry-`).

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
| **Windows drive paths** | `C:\Users\alice`, `D:\work` |
| **Operator username** | Three or more occurrences of the current OS username |

Machine-local files default to *Skip (machine-local content)* in the picker and
are **always skipped** in `--non-interactive` mode, even when
`--include-machine-local` is set. The rationale: machine-local content must be
reviewed interactively before leaving the laptop.

To permanently opt a file out of machine-local detection (e.g. you use absolute
paths intentionally and want them migrated), add the following HTML comment
anywhere in the file body:

```markdown
<!-- meho:machine-local:opt-out -->
```

---

## Scope suggestion table

The picker pre-selects a scope based on the file's `type` front-matter field:

| `type` | Suggested scope | Rationale |
|--------|----------------|-----------|
| `user` | `user` | Personal workflow knowledge — scoped to you. |
| `feedback` | `user` | Coding preferences — personal by default. |
| `project` | `user×tenant` | Project context — tied to the current tenant. |
| `reference` | `user` | Stable reference — personal by default. |
| *(unset / other)* | `user` | Conservative default. |

You can override the suggested scope in the interactive picker. The full 5-scope
model (tenant, target, user×tenant, user×target, user) and its server-side
access control matrix are documented in the cross-repo guide:
[`docs/cross-repo/memory-migration.md`](../cross-repo/memory-migration.md)
(G5.1-T6, issue #427).

---

## Idempotency and re-runs

Every entry is submitted with a `source_id` of the form
`laptop-migration/<first-12-hex-chars-of-SHA-256-body-hash>`. The server
implements an upsert-by-`source_id` contract (G5.1):

- **Same body, re-run** → server-side no-op; `updated_at` is unchanged.
- **Changed body, re-run** → server updates the entry; `updated_at` is bumped.
- **Different machine, same file** → same `source_id` if the body hash matches;
  an edit on one machine propagates on the next run from the other.

The CLI does **not** deduplicate locally — it always POSTs and relies on the
server-side upsert contract.

---

## Flag reference

| Flag | Default | Description |
|------|---------|-------------|
| `--source <path>` | XDG-resolved | Override the memory directory to scan. |
| `--dry-run` | `false` | Print one JSON envelope per entry that would be migrated; make no network call. |
| `--non-interactive` | `false` | Skip the picker; migrate `user` and `feedback` entries at their suggested scope. `project` and `reference` entries are refused (they require interactive scope review). Machine-local entries are always skipped. |
| `--include-machine-local` | `false` | Include machine-local entries in the dry-run preview and in the interactive picker (changes the default action from *Skip* to *Migrate*). Has no effect in `--non-interactive` mode. |
| `--mark-migrated` | `false` | Write the migration-complete marker after a successful run (silences the post-login nudge). |
| `--backplane <url>` | from `meho login` config | Override the backplane URL. |

---

## Post-login nudge

After a successful `meho login`, if your default memory directory is non-empty
and the migration-complete marker is absent, MEHO prints:

```
Tip: you have N memory file(s) at <dir>. Run `meho migrate memory` to sync them to MEHO.
```

The nudge is non-fatal and never blocks or delays login.

To silence the nudge after migrating by other means:

```bash
meho migrate memory --mark-migrated
```

Deleting the marker file (`$XDG_CONFIG_HOME/meho/migrated-from/<sanitized-dir>`)
re-enables the nudge.

---

## Error handling

During submission, transient backplane errors (HTTP 500/502/503/504, transport
timeouts) are retried automatically. In interactive mode, a persistent transient
error pauses and prompts **Retry / Skip / Abort**. In `--non-interactive` mode,
auto-retries are bounded (up to 3 attempts per entry); entries that exhaust
retries are counted as `Errored` in the summary.

Permanent errors (401 Unauthorized, 403 Forbidden, 404 Not Found, 422 Unprocessable)
are not retried. A 401 means your token has expired — run `meho login` again.

The final summary line format:

```
Migrated: N, Skipped: M, Errored: K (retried R)
```

---

## Out of scope

- **Reverse migration** (`meho backup memory`) — planned as G5.4.
- **Conflict-resolution UI** for same-slug-different-machine — v0.2 is
  update-on-change (newer `updated_at` wins); an explicit merge UI is future
  work.
- **Server-side storage, RBAC, TTL** — owned by G5.1 (#332) and G5.2 (#374).
- **The 5-scope decision tree** — see [`docs/cross-repo/memory-migration.md`](../cross-repo/memory-migration.md).
