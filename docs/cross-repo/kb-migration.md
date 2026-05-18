<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Migrating the consumer's `kb/` to MEHO — operator runbook

> Operator-facing runbook for the G4.1 knowledge-base surface. Architecture sits in [`docs/architecture/kb.md`](../architecture/kb.md); this doc is the cookbook the operator follows to point MEHO at the consumer's `kb/`, verify ingestion, run the ≥1-month overlap, and retire the in-repo copy. Implements [decision #2](../planning/v0.2-decisions.md): "one-shot import + 1-month overlap; retire when daily use shifts."

## Why this matters

- The consumer's [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc) repo carries a `kb/` directory — the team's distilled vendor knowledge (vCenter / NSX / Vault / Keycloak / k8s / Argo / Harbor / general; ~44 entries at the time of writing, and growing).
- Today every operator's Claude session relies on the repo being cloned and `grep kb/`. New knowledge reaches the team only via PR review + clone. **Two operators landing the same kb entry independently is a real failure mode.**
- Per [decision #2](../planning/v0.2-decisions.md): MEHO ingests the corpus once; the repo `kb/` stays live as the fallback for ≥1 month; the in-repo copy retires only when `meho kb search` is in daily use and the team agrees. There is no auto-retirement — the retire decision is an explicit operator call backed by the G4.3 eval.

The entry count is a moving target — the canary acceptance test asserts the body-hash idempotency property, not a fixed cardinality. Treat "44" below as "however many `*.md` files the consumer's `kb/` currently holds".

## Prerequisites

- **Roles.** `meho kb ingest` / `add` / `delete` require `tenant_admin`. `meho kb search` / `list` / `show` are `operator`-level. `read_only` callers get HTTP 403 (CLI exit code 5). Tenant scoping is enforced server-side from the JWT — no surface accepts a tenant id.
- **A running backplane.** `meho login <backplane-url>` writes a session token the CLI reuses across every verb. Override per-call with `--backplane <url>`.
- **A local checkout of the consumer repo.** `meho kb ingest <dir>` is a server-side bulk import: the path is interpreted on the **backplane host's** filesystem. Run it where the backplane can see the directory (the operator's deploy host with the consumer repo checked out, or a CI runner). The architecture doc's [REST section](../architecture/kb.md#rest-t2-416--five-routes-under-apiv1kb) explains the `tenant_admin` gate.
- **No manual tenant seed.** You do **not** run a `psql "INSERT INTO tenant …"` before the first ingest, and there is no tenant-provisioning API call to make first. The backplane seeds the `tenant` row just-in-time from the verified `tenant_id` JWT claim in the `verify_jwt_and_bind` middleware every authenticated route flows through (G0.8-T1, #628), so the **first authenticated request** of any kind — including the `--dry-run` in step 2 below — triggers the seed; concurrent first requests are safe (`ON CONFLICT DO NOTHING`).

## Step-by-step ingestion

### 1. Clone the consumer repo

```bash
git clone git@github.com:evoila-bosnia/claude-rdc-hetzner-dc.git
cd claude-rdc-hetzner-dc
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

Expected: a `KbIngestionResult` with `inserted_count == <file count>` on the first real run. Any per-file failure (binary file masquerading as `.md`, malformed front-matter, invalid slug) is counted in `error_count` and described in `errors[]` — the run continues; one bad file does not abort the corpus.

The `tenant` row was already seeded by the **first authenticated request** of this session — including the `--dry-run` in step 2. The seed runs in the `verify_jwt_and_bind` middleware that every authenticated route flows through (G0.8-T1, #628), so any authenticated call (a read, a `--dry-run`, or this real ingest) provisions the row idempotently before the route runs. Before #628 this step failed with an asyncpg `documents_tenant_id_fkey` violation on a fresh deploy because the `tenant` table was empty; that failure mode is gone.

Slug derivation: the slug is the filename stem (`vcenter-9.0-snapshot-revert.md` → slug `vcenter-9.0-snapshot-revert`) unless the file's YAML front-matter carries a `slug:` override. The consumer's kb has no front-matter today; the override is future-compat. Slugs must match `^[a-z](?:[a-z0-9.\-]*[a-z0-9])?$` — lowercase, start with a letter, end with a letter or digit, with hyphens and **dots** allowed (dots carry the version numbers, e.g. `vcenter-9.0-...`). A filename that does not produce a valid slug surfaces as a per-file error rather than aborting the run.

To skip drafts or scratch files, drop a `.kb-ignore` file at the kb root — one glob pattern per line, `#` for comments. A bare directory name (`drafts`) skips everything beneath it.

### 4. Verify ingestion

```bash
meho kb list                       # all entries, slug-sorted, 200-char preview each
meho kb search "vsphere snapshot"  # ranked hits via hybrid BM25 + cosine
meho kb show vcenter-9.0-snapshot-revert   # full Markdown body of one entry
```

`meho kb list` should return the ingested count. `meho kb search` returns ranked hits (slug + ~200-char snippet + fused score); `--json` exposes the per-signal BM25 / cosine scores and ranks for retrieval tuning. `meho kb show <slug>` returns the full body. Note the recipe: **search → identify slug → `show` (or, for agents, the `meho://kb/{slug}` resource) for the full body** — the snippet is deliberately truncated so a search response stays small.

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
- Measure whether the migration is succeeding with the G4.3 eval (already shipped — see "Measuring the overlap" below). The retire decision is data-driven, not a calendar event.

## Measuring the overlap

The eval tooling is already on `main` (G4.3-T1 [#440](https://github.com/evoila/meho/issues/440) corpus + G4.3-T2 [#441](https://github.com/evoila/meho/issues/441) runner). The remaining operationalisation is tracked by G4.3 Initiative [#373](https://github.com/evoila/meho/issues/373).

```bash
meho retrieval eval --json    # precision@5 / MRR / coverage@5 vs the grep baseline
```

This runs the seeded 10-query kb eval corpus through MEHO's hybrid retrieval and against a `grep` baseline, and reports precision@5, MRR, and coverage@5. The Initiative #373 green defaults are MRR ≥ 0.50 and coverage@5 ≥ 0.90 (the canary test [`backend/tests/acceptance/test_g41_kb_canary.py`](../../backend/tests/acceptance/test_g41_kb_canary.py) gates on these). Decision #2's acceptance — 10 sampled queries return answers equivalent to `grep kb/` — is exactly what this eval measures; treat a sustained green eval over the overlap window as the quantitative half of the retire decision.

## Retiring the in-repo copy

The retire decision is **operator-driven** and surface-scoped. Run the checklist verb (note: it is `meho retrieval retire-checklist`, not a `kb`-specific verb — one checklist scores all three retrieval surfaces; kb is one of them):

```bash
meho retrieval retire-checklist --json
```

It combines the eval results with the open-blocker count (GitHub issues labelled `retrieval-migration-blocker`) into a per-surface GREEN / YELLOW / RED verdict over five criteria. When the **kb** surface is GREEN on all five and the team agrees daily use has shifted:

> **Which surface feeds the overlap clock — read this before you start dogfooding.** The daily-use criteria (criterion 1 "days since first daily use" and criterion 2 "operator breadth") are fed **only by the audited MCP search meta-tools**: `search_knowledge` (kb), `search_memory` (memory), `search_operations` (operations). Those land in `audit_log` under `/mcp/tools/call/<tool>` and are the surfaces named in the `counted_surfaces` field of both `meho retrieval usage --json` and `meho retrieval retire-checklist --json` (e.g. `["mcp:search_knowledge", "mcp:search_memory", "mcp:search_operations"]`).
>
> **REST `POST /api/v1/retrieve` is deliberately excluded** (`rest_excluded: true` in the same responses). It runs the identical retrieval substrate but audits under `/api/v1/retrieve`, which is not a counted path — so a `search_knowledge` call ticks the clock while a REST `/retrieve` call does not. Counting REST too is intentionally out of scope for v0.2 (it would change audit volume and risk double-counting against the MCP path).
>
> **Practical consequence:** if you dogfood the ≥1-month overlap exclusively through REST `/retrieve` (or before `/mcp` is configured at all), `total_searches` stays `0` and the retire checklist stays RED on criteria 1 + 2 for the entire window — not because the migration failed, but because the counted surface saw no traffic. Dogfood through the MCP `search_knowledge` tool (the agent-facing surface) so the overlap clock actually ticks. The zero is no longer silent: `counted_surfaces` + `rest_excluded` on every `usage` / `retire-checklist` response tell you exactly why a zero is a zero.

1. Open a PR in `evoila-bosnia/claude-rdc-hetzner-dc` removing `kb/`.
2. Update the consumer's `CLAUDE.md` to drop "grep `kb/`" patterns in favour of "use `meho kb search`".
3. Keep the deleted directory recoverable from git history (a tag or the merge commit SHA is enough — see Rollback).

Do not retire on a calendar alone. A GREEN checklist over a sustained window is the contract; the 1-month overlap is the *minimum*, not the trigger.

## Rollback

If a retrieval-quality regression surfaces after retiring the in-repo copy:

1. From the consumer repo checkout (the same `claude-rdc-hetzner-dc` working directory established in step 1), restore the deleted directory from git history:
   ```bash
   git checkout <pre-retire-sha> -- kb/
   ```
   (`<pre-retire-sha>` is the commit immediately before the retire PR merged — the parent of the deletion commit.)
2. Open an issue in the consumer repo labelled `retrieval-migration-blocker` so the next `meho retrieval retire-checklist` run flips the kb surface off GREEN until the regression is resolved.
3. Re-investigate via `meho retrieval eval --json`: did precision@5 / MRR / coverage@5 drop? Is a specific query class failing? Re-ingest with `meho kb ingest ./kb --json` after the consumer-side `kb/` is restored, then re-measure.

Retiring is reversible by design — the corpus is in git on the consumer side and re-ingest is idempotent and cheap.

## Writing new kb entries through MEHO

Once daily use is established, new knowledge is written through MEHO directly (no PR review cycle — the entry reaches every operator in the tenant on write):

- **Agent path.** `add_to_knowledge {"slug": "...", "body": "...", "metadata": {...}}`. The agent must `search_knowledge` for the topic **first** — re-adding under a different slug fragments the corpus and dilutes retrieval. A same-slug re-add merges in place via the body-hash short-circuit. `add_to_knowledge` is `operator`-role on the agent surface (deliberately narrower than the REST `tenant_admin` gate — the audit row + broadcast event provide traceability).
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
- [decision #2](../planning/v0.2-decisions.md) — one-shot import + 1-month overlap.
- [Initiative #373 G4.3](https://github.com/evoila/meho/issues/373) — retrieval migration tooling (eval + retire-checklist) that operationalises the retire decision.
- [`docs/cross-repo/README.md`](./README.md) — the index of cross-repo coordination specs and operator runbooks this doc is listed in.
