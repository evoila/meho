<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Migrating the consumer's `kb/` to MEHO — operator runbook

> Operator-facing runbook for the G4.1 knowledge-base surface. Architecture sits in [`docs/architecture/kb.md`](../architecture/kb.md); this doc is the cookbook the operator follows to point MEHO at the consumer's `kb/`, verify ingestion, run the ≥1-month overlap, and retire the in-repo copy. Implements [decision #2](../decisions/locked-decisions.md): "one-shot import + 1-month overlap; retire when daily use shifts."

## Why this matters

- The consumer's infrastructure repo carries a `kb/` directory — the team's distilled vendor knowledge (vCenter / NSX / Vault / Keycloak / k8s / Argo / Harbor / general; the count keeps growing).
- Today every operator's Claude session relies on the repo being cloned and `grep kb/`. New knowledge reaches the team only via PR review + clone. **Two operators landing the same kb entry independently is a real failure mode.**
- Per [decision #2](../decisions/locked-decisions.md): MEHO ingests the corpus once; the repo `kb/` stays live as the fallback for ≥1 month; the in-repo copy retires only when `meho kb search` is in daily use and the team agrees. There is no auto-retirement — the retire decision is an explicit operator call.

The entry count is a moving target — the canary acceptance test asserts the body-hash idempotency property, not a fixed cardinality. Treat `<file count>` below as "however many `*.md` files the consumer's `kb/` currently holds".

## Prerequisites

- **Roles.** `meho kb ingest` / `add` / `delete` require `tenant_admin`. `meho kb search` / `list` / `show` are `operator`-level. `read_only` callers get HTTP 403 (CLI exit code 5). Tenant scoping is enforced server-side from the JWT — no surface accepts a tenant id.
- **A running backplane.** `meho login <backplane-url>` writes a session token the CLI reuses across every verb. Override per-call with `--backplane <url>`.
- **A local checkout of the consumer repo, under `KB_INGEST_ROOT`.** `meho kb ingest <dir>` is a server-side bulk import: the path is interpreted on the **backplane host's** filesystem. The ingest directory is confined to the `KB_INGEST_ROOT` setting (default `/opt/meho/kb-ingest`); a path that resolves outside the root — via `..` traversal or an escaping symlink — is rejected with HTTP 400 `kb_ingest_path_outside_root` before any file is read (the #101 path-traversal / LFI guard). So the consumer checkout must live **under** the configured root: mount it at `/opt/meho/kb-ingest/<repo>`, or set `KB_INGEST_ROOT` (env var on the backplane pod) to wherever the deploy host / CI runner checks the repo out. The architecture doc's [REST section](../architecture/kb.md#rest-t2-416--five-routes-under-apiv1kb) explains the `tenant_admin` gate.
- **No manual tenant seed.** You do **not** run a `psql "INSERT INTO tenant …"` before the first ingest, and there is no tenant-provisioning API call to make first. The backplane seeds the `tenant` row just-in-time from the verified `tenant_id` JWT claim in the `verify_jwt_and_bind` middleware every authenticated route flows through (G0.8-T1, #628), so the **first authenticated request** of any kind — including the `--dry-run` in step 2 below — triggers the seed; concurrent first requests are safe (`ON CONFLICT DO NOTHING`).

## Step-by-step ingestion

### 1. Clone the consumer repo

```bash
git clone git@github.com:<your-org>/<your-infra-repo>.git
cd <your-infra-repo>
```

### 2. Dry-run first

`--dry-run` walks the directory and classifies every file (insert / update / skip) **without writing**. Use it to confirm the path resolves and the corpus shape looks right before a real run:

```bash
meho kb ingest ./kb --dry-run --json
```

Expected on a never-ingested corpus: `inserted_count == <file count>`, `updated_count == 0`, `skipped_count == 0`, `error_count == 0`. The four counters always partition every discovered `*.md` file: `inserted + updated + skipped + error == total`.

### 3. Ingest for real

```bash
meho kb ingest ./kb --json
```

Expected: a `KbIngestionResult` with `inserted_count == <file count>` on the first real run. Any per-file failure (binary file masquerading as `.md`, malformed front-matter, invalid slug) is counted in `error_count` and described in `errors[]` — the run continues; one bad file does not abort the corpus. If two or more files share a slug, none of them is written, the existing entry stays unchanged, and they are listed in `conflicts[]` (counted in `conflict_count`). Rename a file or fix its `slug:` front-matter, then run the ingest again.

The `tenant` row was already seeded by the **first authenticated request** of this session — including the `--dry-run` in step 2. The seed runs in the `verify_jwt_and_bind` middleware that every authenticated route flows through (G0.8-T1, #628), so any authenticated call (a read, a `--dry-run`, or this real ingest) provisions the row idempotently before the route runs. Before #628 this step failed with an asyncpg `documents_tenant_id_fkey` violation on a fresh deploy because the `tenant` table was empty; that failure mode is gone.

Slug derivation: the slug is the filename stem (`vcenter-9.0-snapshot-revert.md` → slug `vcenter-9.0-snapshot-revert`) unless the file's YAML front-matter carries a `slug:` override. The consumer's kb has no front-matter today; the override is future-compat. Slugs must match `^[a-z](?:[a-z0-9.\-]*[a-z0-9])?$` — lowercase, start with a letter, end with a letter or digit, with hyphens and **dots** allowed (dots carry the version numbers, e.g. `vcenter-9.0-...`). A filename that does not produce a valid slug surfaces as a per-file error rather than aborting the run.

To skip drafts or scratch files, drop a `.kb-ignore` file at the kb root — one glob pattern per line, `#` for comments. A bare directory name (`drafts`) skips everything beneath it.

The ingest reads only regular files inside the folder. It skips links (to files or folders) and special files such as pipes. For each skipped file it writes one `kb_file_skipped` line to the server log. If your kb content arrives as links, copy it into a normal folder first. For example, a Kubernetes ConfigMap volume shows its files as links.

### 4. Verify ingestion

```bash
meho kb list                       # all entries, slug-sorted, 200-char preview each
meho kb list --order newest        # most recently updated first
meho kb search "vsphere snapshot"  # ranked hits via hybrid BM25 + cosine
meho kb show vcenter-9.0-snapshot-revert   # full Markdown body of one entry
```

`meho kb list` should return the ingested count. `meho kb search` returns ranked hits (slug + ~200-char snippet + fused score); `--json` exposes the per-signal BM25 / cosine scores and ranks for retrieval tuning. `meho kb show <slug>` returns the full body. Note the recipe: **search → identify slug → `show` (or, for agents, `search_knowledge` with `slug`, or the `meho://kb/{slug}` resource) for the full body** — the snippet is deliberately truncated so a search response stays small.

### 5. Re-ingest after consumer-side updates

```bash
git pull
meho kb ingest ./kb --json
```

Idempotent. The body-hash short-circuit from the G0.4 substrate means an unchanged corpus produces `skipped_count == <total>` and zero embedding compute; a single edited entry produces `updated_count == 1, skipped_count == <total - 1>`. Re-run as often as the consumer's `kb/` changes — it is cheap.

## During the ≥1-month overlap

- The consumer repo's `kb/` **stays live as the fallback.** Do not delete it yet.
- Operators use `meho kb search` for daily lookups; `grep kb/` is the override when search misses.
- Re-ingest on a cadence that tracks the consumer's `kb/` churn (a cron or a post-`git pull` hook on the deploy host). The cost is bounded by the number of *changed* entries, not corpus size.
- Check whether daily use is moving to MEHO with `meho retrieval usage` (see "Measuring the overlap" below). The retire decision is based on real use, not a calendar date.

## Measuring the overlap

```bash
meho retrieval usage --json    # searches per day and per surface, for your tenant
```

This reports, per day and per surface, how many searches ran, how many operators ran them, and how often a search led to a next step. Use it to see whether the team really searches MEHO instead of `grep kb/`.

> **Which searches are counted — read this before you start dogfooding.** Only the MCP search tools count: `search_knowledge` (kb), `search_memory` (memory), `search_operations` (operations). MEHO counts them from request usage events (route `tool:<tool name>`). It records these events only when `REQUEST_USAGE_RETENTION_DAYS` is set; without it, the report shows no searches. The `counted_surfaces` field of `meho retrieval usage --json` names the counted tools (e.g. `["mcp:search_knowledge", "mcp:search_memory", "mcp:search_operations"]`).
>
> **REST `POST /api/v1/retrieve` is not counted** (`rest_excluded: true` in the same response). It runs the same search, but a REST `/retrieve` call does not count, while a `search_knowledge` call does.
>
> **What this means:** if you dogfood the overlap only through REST `/retrieve` (or before `/mcp` is set up), `total_searches` stays `0` for the whole window. The migration did not fail; the counted tools just saw no traffic. Dogfood through the MCP `search_knowledge` tool (the agent-facing surface) so the searches count. `counted_surfaces` and `rest_excluded` on every `usage` response tell you why a zero is a zero.

## Retiring the in-repo copy

The retire decision is **operator-driven**. MEHO does not decide it for you. When the usage report shows that daily lookups have moved to MEHO over the overlap window, and the team agrees:

1. Open a PR in the consumer's repo removing `kb/`.
2. Update the consumer's `CLAUDE.md` to drop "grep `kb/`" patterns in favour of "use `meho kb search`".
3. Keep the deleted directory recoverable from git history (a tag or the merge commit SHA is enough — see Rollback).

Do not retire on a calendar alone. The 1-month overlap is the *minimum*, not the trigger.

## Rollback

If search quality gets worse after retiring the in-repo copy:

1. From the consumer repo checkout (the same working directory established in step 1), restore the deleted directory from git history:
   ```bash
   git checkout <pre-retire-sha> -- kb/
   ```
   (`<pre-retire-sha>` is the commit immediately before the retire PR merged — the parent of the deletion commit.)
2. Re-ingest with `meho kb ingest ./kb --json` after the consumer-side `kb/` is restored, and check which queries return poor hits with `meho kb search --json`.

Retiring is reversible by design — the corpus is in git on the consumer side and re-ingest is idempotent and cheap.

## Writing new kb entries through MEHO

Once daily use is established, new knowledge is written through MEHO directly (no PR review cycle — the entry reaches every operator in the tenant on write):

- **Agent path.** `add_to_knowledge {"slug": "...", "body": "...", "metadata": {...}}`. The agent must `search_knowledge` for the topic **first** — re-adding under a different slug fragments the corpus and dilutes retrieval. A same-slug re-add merges in place via the body-hash short-circuit. `add_to_knowledge` is `operator`-role on the agent surface (deliberately narrower than the REST `tenant_admin` gate — the audit row + broadcast event provide traceability). An `operator` changes only the entries it created and cannot set the `source_kind` metadata key; a `tenant_admin` can change any entry.
- **Operator CLI.** `tenant_admin` only:
  ```bash
  meho kb add my-product-9.0-topic --body @./entry.md --metadata source=runbook,owner=ops
  ```
  `--body` accepts inline text, `@<path>` for a file, or `@-` for stdin. Slugs are validated against the same pattern as ingestion.
- **Delete.** `meho kb delete <slug>` (`tenant_admin`). Idempotent — deleting an already-absent slug still returns success (204), so scripts are safe to re-run. `--confirm` skips the interactive prompt for scripted use.

Ephemeral session notes do **not** belong in the kb — those go in `add_to_memory` (G5). The kb is durable, team-wide knowledge.

## Related

- [`docs/architecture/kb.md`](../architecture/kb.md) — the architecture companion: module shape, `KbService` method map, the four surfaces, RBAC matrix.
- [Initiative #331 G4.1](https://github.com/evoila/meho/issues/331) — scope + definition of done.
- [decision #2](../decisions/locked-decisions.md) — one-shot import + 1-month overlap.
- [`docs/cross-repo/README.md`](./README.md) — the index of cross-repo coordination specs and operator runbooks this doc is listed in.
