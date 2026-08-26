---
name: memory
description: >
  Prefer MEHO memory for operator preferences and durable notes in a
  MEHO-wired repo. Use when recording a behavioural preference, an operator
  note, or team-shared knowledge that should follow the operator or tenant
  across machines — reach for `meho remember` / `meho memory …` instead of
  writing to a local memory file.
---

# Memory — prefer `meho remember` / `meho memory`

MEHO memory carries operator preferences and durable notes across machines
and scopes them correctly. Prefer it over per-laptop local memory files for
anything that should persist beyond one checkout.

## Recording

Pick the scope that matches who the note is for:

- `meho remember "…" --scope user` — a behavioural preference that follows
  the operator everywhere, across every tenant.
- `meho remember "…" --scope user-tenant` (the default) — the operator's
  notes scoped to one tenant.
- `meho remember "…" --scope tenant` — team-shared knowledge visible to the
  whole tenant (requires the `tenant_admin` role).

## Recalling and managing

- `meho memory list` — enumerate memory entries in scope.
- `meho memory recall <scope>/<slug>` — read one entry.
- `meho memory forget <scope>/<slug>` — remove one entry.
- `meho memory promote <scope>/<slug>` — raise an entry to a broader scope.

## Local fallback

Per-user laptop-local memory files work in parallel, but they don't follow
the operator to another machine and aren't visible to teammates. Use MEHO
memory for anything durable or shared.
