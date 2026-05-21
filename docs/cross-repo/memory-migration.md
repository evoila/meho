<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Migrating laptop-local memory to MEHO — operator runbook

> Operator-facing runbook for the G5.1 server-side memory surface. Architecture sits in [`docs/architecture/memory.md`](../architecture/memory.md); this doc is the cookbook the operator follows to move from laptop-local `~/.claude/projects/<...>/memory/` files to MEHO's server-side memory across the five scopes (user / user-tenant / user-target / tenant / target).

## Why this matters

- Today every operator's Claude sessions read and write `~/.claude/projects/<...>/memory/<file>.md` on a single laptop. What one operator's Claude learns is invisible to every other operator's Claude. The corpus dies with the laptop.
- After the migration: server-side memory across five scopes; **the team becomes the unit of memory** per [consumer-needs.md §G5 L131](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/docs/meho-coordination/consumer-needs.md). A senior teaching their Claude about an infrastructure target can promote that memory to `tenant` scope and every junior's Claude session in the same tenant sees it the next time it searches.
- The migration is **one-way** — there is no "restore from laptop" path. The mitigation is the G5.3 #375 per-file picker (when shipped), which lets the operator choose per-file whether each laptop entry migrates, stays laptop-local (machine-specific paths, scratch notes), or gets edited before sending. Until G5.3 ships, the manual `meho remember` path documented here is the migration recipe.

## The 5-scope decision matrix

Every memory entry lives in exactly one scope. Pick the narrowest scope that captures the entry's audience — promotion is one-way and the surface deliberately makes broader-scope writes more deliberate (tenant scope requires `tenant_admin`).

| Scope | Visible to | Use for |
|---|---|---|
| `user` | Just you, across **every** tenant you belong to | Personal behavioral preferences that travel with you ("I prefer kubectl over k9s", "always show me JSON output first") |
| `user-tenant` | Just you, within this tenant | Personal context scoped to one lab ("for rdc-internal I'm investigating the snapshot regression") |
| `user-target` | Just you, scoped to one target | Personal notes about one vCenter / k8s cluster ("my local kubectl alias `pf` port-forwards to rdc-tools-1") |
| `tenant` | Everyone in your tenant (read); `tenant_admin` only (write) | Team-wide conventions ("we use GitOps for everything"; "PR titles follow conventional-commits") |
| `target` | Anyone with access to this target (in v0.2: every operator in the target's tenant) | Target-specific gotchas ("rdc-vcenter requires VPN"; "vmware-prod has stricter snapshot quotas") |

Decision tree:

1. **Will another operator ever need this entry?** No → user-flavoured scope. Yes → step 2.
2. **Is it about one specific infrastructure target?** Yes → `target` (everyone with access to that target sees it). No → step 3.
3. **Is it about this tenant's conventions / policies?** Yes → `tenant` (you need `tenant_admin`). No → re-read the entry; if it isn't user-private *and* isn't tenant-shared *and* isn't target-specific, it probably belongs in the **kb** ([`docs/architecture/kb.md`](../architecture/kb.md)) as durable vendor knowledge rather than memory.
4. **Does the entry tie a person to a tenant or a person to a target?** Yes → `user-tenant` or `user-target` respectively. No → `user`.

Two corollaries:

- **Tenant boundary**: cross-tenant memory reads are impossible by construction; you don't pick the tenant when you write — the JWT does. `user`-scoped memories are the only ones that span tenants (within one operator).
- **Read visibility on user-flavoured rows**: the operator's `sub` is in the row's natural key encoding, so no other operator (not even a `tenant_admin`) can read your `user-tenant` memories. The RBAC matrix is enforced at every read.

## Prerequisites

- **Roles.** `meho remember` / `recall` / `forget` / `list` are `operator`-level. Writes to `tenant` scope require `tenant_admin` (the server returns 403 from the `MemoryRbacResolver`; the CLI surfaces the exit code 5 `insufficient_role`). `read_only` callers can read `tenant` / `target` memories but cannot write any scope.
- **A running backplane.** `meho login <backplane-url>` writes a session token the CLI reuses across every verb. Override per-call with `--backplane <url>`.
- **Tenant context.** The JWT carries `tenant_id` — `meho remember` is scoped to the tenant your token was issued for. To migrate memories that belong in a different tenant, `meho login` against that tenant first.
- **Your laptop-local memory directory.** Claude Code keeps memory files at `~/.claude/projects/<sanitized-cwd-path>/memory/<slug>.md`. The exact path depends on your working directory at the time the memory was written. The G5.3 #375 verb will walk this directory automatically when it lands.

## Migration path — G5.3 (when shipped)

The first-class migration UX is the G5.3 #375 verb:

```bash
meho migrate memory
```

It walks `~/.claude/projects/<...>/memory/`, parses the YAML frontmatter, runs a machine-local heuristic (flagging files with `/Users/<name>/`, `*.local`, `host.docker.internal`, ...), suggests a scope per file based on the `type:` frontmatter (`user` / `feedback` → `user`; `project` → `user-tenant`; `reference` → `user`), and presents an interactive picker (huh-based form). For each file the operator chooses:

- **Migrate to the suggested scope** (default), or
- **Migrate to a different scope** (the picker filters tenant / target out unless your role permits), or
- **Skip — keep laptop-local** (machine-specific paths, scratch notes), or
- **Edit body, then migrate** (strip machine-local snippets first).

Idempotency: each entry is POSTed with a stable `source_id` derived from the body's SHA-256, so re-running the migration after editing one laptop file updates that row in place and is a no-op for everything else.

A `--dry-run` flag prints the per-entry envelopes that would be sent; `--non-interactive` migrates the safest defaults (`type: user` and `type: feedback` only) at their suggested scope, useful for automation.

## Migration path — manual (until G5.3 ships)

Until [#375 G5.3](https://github.com/evoila/meho/issues/375) lands, walk the directory manually and pipe each file's body through `meho remember`. The shape is the same — pick the scope per file, hand the body to the CLI:

```bash
cd ~/.claude/projects/<sanitized-cwd>/memory
for f in *.md; do
  slug="${f%.md}"
  echo "=== $f ==="
  head -20 "$f"           # eyeball the body
  read -r -p "scope [user|user-tenant|user-target|tenant|target|skip]: " scope
  case "$scope" in
    skip) continue ;;
    user-target|target)
      read -r -p "target name: " target
      cat "$f" | meho remember - --scope "$scope" --slug "$slug" --target "$target"
      ;;
    *)
      cat "$f" | meho remember - --scope "$scope" --slug "$slug"
      ;;
  esac
done
```

Notes on the shape:

- `meho remember -` reads the body from stdin (bare hyphen). Trailing newlines from the pipe are stripped; embedded newlines inside the body are preserved.
- `--slug` is optional — without it the service auto-generates a 12-char UUID hex prefix. Using the filename stem (with `.md` removed) keeps the migration auditable: the laptop file `kubectl-preference.md` becomes the server-side slug `kubectl-preference`.
- The slug pattern is `^[A-Za-z0-9_\-\.]+$` — letters, digits, hyphen, underscore, dot. Colon is forbidden (the `source_id` encoding uses it as a segment separator). A laptop filename containing `/` (very unlikely) or `:` needs renaming first.
- `--scope user-target` / `--scope target` require `--target NAME` at the CLI; the CLI fails fast client-side rather than relying on a 422 round-trip.
- `--tag <T>` (repeatable) attaches tag metadata; useful for grouping migration cohorts (`--tag laptop-migration --tag 2026-05-19`).

## Verifying the migration

After every batch, list and search to confirm the corpus shape:

```bash
meho list --scope user --json | jq '.entries | length'           # count user-scoped entries
meho list --scope user-tenant --json | jq '.entries[] | .slug'   # slugs in the current tenant
meho recall --query "kubectl" --scope user                       # hybrid retrieval against your user-scoped corpus
meho recall user/kubectl-preference                              # exact natural-key fetch (writes body to stdout)
```

`meho recall --query` rides the G0.4 retrieval substrate (`POST /api/v1/retrieve` with `source="memory"` pinned). The substrate post-filters user-scoped hits against your `sub`, so a query never surfaces another operator's user-scoped row even when retrieval ranked it highly. `--scope user` narrows to `kind=memory-user` server-side; omitting it considers every scope visible to you.

A `meho recall user/missing-slug` returns 404 (CLI exit code 4) by design — the route deliberately conflates "not found" and "no access" so an operator can't probe for slugs they shouldn't see.

## Default TTL behavior

[#624 G5.2-T2](https://github.com/evoila/meho/issues/624) ships the default 7-day TTL injection on `memory-user` writes that omit `expires_at`. Concretely:

- Every `meho remember --scope user "..."` without `--ttl` lands with `expires_at = now + Settings.memory_user_default_ttl_days` (default 7, range 1-365 via the `MEMORY_USER_DEFAULT_TTL_DAYS` env var / Helm `memory.userDefaultTtlDays`). The entry is invisible to reads after the cutoff (read-side filter on `expires_at`) and physically deleted by the G5.2-T1 [#623](https://github.com/evoila/meho/issues/623) daily reap.
- Pass `--ttl 30d` (or `--ttl 12h`, `--ttl 36m`) to set a specific lifetime. The shorthand accepts `s` / `m` / `h` / `d` suffixes.
- Pass `--persist` to opt out of the default TTL (the entry never expires until you `meho forget` it). Use this for memories you intentionally want to retain across the 7-day window — typically the durable preferences (`kubectl-preference`, `editor-preference`). `--persist` is mutually exclusive with `--ttl`: passing both surfaces a CLI-side error before the HTTP round-trip.
- `user-tenant`, `user-target`, `tenant`, `target` scopes do **not** receive the default TTL — only `user` does. Tenant / target memories are intentionally long-lived; user-scoped is the "what I'm investigating today" cadence.
- Direct REST callers (not `meho remember`) can opt out by sending `"expires_at": null` in the JSON body. The handler distinguishes "field omitted" from "field present with value null" via Pydantic v2's `BaseModel.model_fields_set` — the former triggers the default, the latter is the explicit-persist shape `--persist` emits.

Plan migration accordingly: under G5.2 the user-scoped subset of a bulk migration starts expiring on a rolling 7-day window unless you pass `--persist` per row. Pre-existing rows written under G5.1 (before #624 landed) keep their stored value — only **new** writes go through the default-injection path.

## Rollback

Rollback is forward-only — the migration writes new server-side rows; the laptop files remain in `~/.claude/projects/<...>/memory/` until you delete them. The two CLI verbs that constitute the rollback path:

1. **List the corpus including expired rows**:

   ```bash
   meho list --scope user --include-expired --json
   ```

   `--include-expired` surfaces entries whose `metadata.expires_at` is in the past (relevant once G5.2 ships) so you see the full state, not just the read-side-visible subset.

2. **Delete individual entries**:

   ```bash
   meho forget user/kubectl-preference                # quiet on a present row
   meho forget user/already-gone                      # idempotent — no error on missing rows
   meho forget user-target/cluster-note --target rdc-tools-1   # target-scoped delete
   meho forget user/old-thing --confirm               # skip the y/N prompt for scripted use
   ```

   `meho forget` is **idempotent**: deleting an already-absent slug returns success (204 at the API). The CLI's `--confirm` flag skips the interactive `[y/N]` prompt so scripted rollback (e.g. `xargs meho forget`) doesn't deadlock on stdin.

There is **no "restore from laptop" path** built into MEHO. The mitigation is:

- The original laptop files are still in `~/.claude/projects/<...>/memory/` until the operator deletes them. Keep the laptop directory until you've validated the server-side corpus.
- G5.3's per-file picker is the safety net for "I didn't want to migrate that one" — the picker shows the body before each POST, so the wrong entries don't reach the server in the first place.
- Server-side `meho forget` cleans up rows the operator wrote by mistake. Re-running `meho remember` from the laptop file restores them.

For a tenant-scoped rollback ("we agreed to roll back the team-wide `gitops-convention` memory"), the same `meho forget tenant/<slug>` works — the verb requires `tenant_admin` for tenant scope writes (delete is a write, in the RBAC matrix).

## Writing new memories through MEHO

Once the laptop directory is migrated (or you've decided which files stay laptop-local), new memories are written through MEHO directly:

- **Agent path.** Every agent session that learns something durable calls `add_to_memory(body, scope, ttl?, slug?, ...)` through the MCP meta-tool (the field was renamed from `content` to `body` in v0.3.2 to match `add_to_knowledge` + the REST surface; see [G0.9.1-T7 #779](https://github.com/evoila/meho/issues/779)). The agent must `search_memory` first to avoid duplicates; same-slug re-add merges in place via the body-hash short-circuit.
- **Operator CLI.**

  ```bash
  meho remember "I prefer kubectl over k9s" --scope user --slug kubectl-preference
  meho remember "rdc-vcenter requires VPN" --scope target --target rdc-vcenter --slug vpn-requirement
  meho remember "tenant convention: PR titles follow conventional-commits" --scope tenant --slug pr-title-convention
  ```

  `--tag <T>` attaches arbitrary tag metadata for downstream filtering (`meho list --tag onboarding-2026`).

Ephemeral session notes do not need to live in memory — the agent's working memory is fine. Memory is for durable learnings (preferences, conventions, gotchas) that should outlast a single session.

## Promoting between scopes (when G5.2 ships)

[#374 G5.2](https://github.com/evoila/meho/issues/374) ships the `meho promote` verb:

```bash
meho promote user/<slug> --to user-tenant         # operator-level, no extra role
meho promote user-tenant/<slug> --to tenant       # requires tenant_admin
meho promote user/<slug> --to user-target --move  # move (delete source) rather than copy
```

Promotion is **one-way and operator-initiated** — there is no `meho demote` and no AI-suggested promotion (explicitly disallowed per consumer-needs.md §G5 "Out of scope"). Until G5.2 ships, the manual equivalent is "`meho recall` the source, `meho remember` at the target scope, `meho forget` the source".

## Related

- [`docs/architecture/memory.md`](../architecture/memory.md) — the architecture companion: module shape, `MemoryService` method map, the four surfaces, RBAC matrix.
- [Initiative #332 G5.1](https://github.com/evoila/meho/issues/332) — scope + definition of done; the surface this runbook documents.
- [Initiative #374 G5.2](https://github.com/evoila/meho/issues/374) — auto-expiry + tenant-promotion verb + per-scope RBAC tightening (default 7-day TTL on user scope, `meho promote`).
- [Initiative #375 G5.3](https://github.com/evoila/meho/issues/375) — laptop-local migration UX (`meho migrate memory` interactive per-file picker + machine-local heuristic + post-login nudge).
- [Consumer-needs.md §G5](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/docs/meho-coordination/consumer-needs.md) L130-160 — the canonical product spec for the memory layer (team-as-unit-of-memory unlock, 5-scope shape, auto-expiry policy, operator-initiated promotion only).
- [`docs/cross-repo/README.md`](./README.md) — the index of cross-repo coordination specs and operator runbooks this doc is listed in.
- [`docs/cross-repo/kb-migration.md`](./kb-migration.md) — the sister runbook for migrating the consumer's `kb/` corpus into MEHO. Memory and kb ride the same G0.4 substrate but answer different questions ("what does this operator / team prefer" vs "what do we know about vendor X").
