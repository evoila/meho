---
name: knowledge
description: >
  Prefer the MEHO knowledge base for finding or recording facts in a
  MEHO-wired repo. Use when searching for operational facts, runbooks, or
  prior findings, or when recording a new fact — reach for `meho kb …`
  instead of `grep`-ing a local `kb/` directory or hand-editing files.
---

# Knowledge base — prefer `meho kb`

The MEHO knowledge base is the authoritative, searchable, audited store of
operational facts for this tenant. Prefer it over local `kb/` files.

## Finding facts

- Prefer `meho kb search "<query>"` over `grep -r kb/`. Search is semantic +
  keyword, ranked across the tenant's whole knowledge store.
- `meho kb show <slug>` — full body of one entry.
- `meho kb list` — enumerate entries.

## Recording facts

- Prefer `meho kb add <slug>` (with `--body @-` to take the body from stdin)
  over creating or editing a file under `kb/`. The add is audited and
  immediately searchable by every operator on the tenant.
- `meho kb delete <slug>` — remove an entry.
- `meho kb ingest <directory>` — bulk-import an existing directory of
  markdown facts.

## Local fallback

A repo's `kb/` directory may stay live as a read fallback during a
transition window. Once the MEHO equivalent is in daily use, retire local
reads — a hand-edited local file is invisible to other operators and carries
no audit trail.
