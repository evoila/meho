<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Retiring the consumer's pre-MEHO retrieval workflows

The operator-facing runbook for the G4.3 migration tooling
([Initiative #373](https://github.com/evoila/meho/issues/373)). Companion
to the `meho retrieval retire-checklist` verb shipped by
[T6 #445](https://github.com/evoila/meho/issues/445); ships the
`retrieval-migration-blocker` GitHub label automation and the
per-surface retire + rollback procedure
([T7 #446](https://github.com/evoila/meho/issues/446)).

Audience: the operator deciding whether to delete a pre-MEHO retrieval
artefact (the consumer repo's `kb/` directory, an operator's laptop-local
`memory/` files, the consumer's `grep paths.txt + yq` operations
workflow) and the operator executing that delete + the rollback if it
goes wrong.

Locked decision the runbook executes:
[`docs/decisions/locked-decisions.md` decision #2 — *side-by-side with
1-month overlap; retire when daily use shifts*](../decisions/locked-decisions.md).

> The runbook is upstream of the actual retire-PR. MEHO ships the
> tooling that makes the call (`retire-checklist` returns READY / REVIEW
> / NOT YET); operators ship the retire-PR by hand in the consumer
> repo + on their own laptops once the call has been made. There is no
> automation that deletes the consumer's `kb/` directory.

## The three retrieval surfaces in retirement

The post-G4.3 architectural correction collapsed the migration model from
"kb + docs" to three coequal surfaces. Every step below applies
per-surface; the retire-checklist verb verdicts each surface independently.

| Surface | Pre-MEHO artefact | MEHO replacement | Retire boundary |
| --- | --- | --- | --- |
| **kb** | [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc) `kb/` directory | `meho kb search` (`search_knowledge` meta-tool over the `documents` table, `source="kb"`) | Delete `kb/` from the consumer repo + update consumer `CLAUDE.md` to drop the "grep `kb/`" guidance. |
| **memory** | Laptop-local `~/.claude/{projects}/memory/` files (per-operator) | `meho memory search` (`search_memory` meta-tool over the `documents` table, `source="memory"`) | Archive each operator's laptop-local `memory/` directory locally; agents fetch from MEHO. |
| **operations** | The consumer's `grep docs/<product>-<version>/paths.txt + yq` workflow against locally-cloned vendor specs | `meho operation search` + `meho operation call` (`search_operations` / `call_operation` against the `endpoint_descriptor` table) | Stop using the `grep paths.txt` workflow; agents redirect to `meho operation search`. The `docs/<product>-<version>/` directories **stay** in the consumer repo as grounding for future spec re-ingestion (not retired). |

Per-surface filtering uses the issue's existing area labels
(`knowledge` on kb-side issues, `memory` on memory-side issues,
`connector` on operation-substrate issues) cross-referenced with the
single repo-wide `retrieval-migration-blocker` label — no per-surface
blocker labels are introduced in v0.2.

## The 5-criterion retire-checklist

Run the checklist before every retire conversation:

```bash
meho retrieval retire-checklist --surface kb          # per-surface
meho retrieval retire-checklist --surface all         # default; per-surface in one report
meho retrieval retire-checklist --surface kb --json   # machine-readable
```

The verb composes audit-log usage telemetry (T5
[#444](https://github.com/evoila/meho/issues/444)) with eval results
(T2 [#441](https://github.com/evoila/meho/issues/441)) and a local
`gh issue list` lookup against the
`retrieval-migration-blocker` label (this Task) to verdict each surface
on five criteria:

| # | Criterion | Green | Yellow | Red |
| --- | --- | --- | --- | --- |
| 1 | Days since first daily-use date for the surface | ≥ 30 days | [21, 30) days | < 21 days |
| 2 | Distinct operators with ≥ 1 search per week for ≥ 4 consecutive ISO weeks | ≥ 3 operators | 2 operators | ≤ 1 operator |
| 3 | Eval precision@5 against the surface's checked-in corpus | ≥ 0.80 | [0.56, 0.80) | < 0.56 |
| 4 | MEHO ranking ≥ baseline on every metric (precision@5 + MRR + coverage) | every metric ≥ baseline | baseline did not run for this surface | any metric below baseline |
| 5 | Open issues labeled `retrieval-migration-blocker` (surface-bucketed) | 0 open | count unknown / `gh` lookup not run | ≥ 1 open |

The yellow floors derive from `YELLOW_FLOOR_RATIO = 0.70` centralised in
`backend/src/meho_backplane/retrieval/retire.py` (matches
`retrieval.eval.metrics.YELLOW_FLOOR_RATIO`, so a future re-tune
touches one constant).

### Per-surface verdict

| Verdict | Condition | Operator action |
| --- | --- | --- |
| **READY TO RETIRE** | Every criterion green. | File the retire-PR for this surface (steps below). |
| **REVIEW MANUALLY** | At least one yellow, no red. | Operator + ≥ 2 teammates concur via a v0.2 ship-bar review, **or** wait for the yellow band to clear. |
| **NOT YET** | Any red. | Resolve the blocker (more daily use, more operators, eval regression fix, or close the labeled blocker issues) before re-running the checklist. |

### Overall verdict

The report's bottom-line verdict is the **worst per-surface verdict**
(operator intuition: "we retire the slowest surface last"). Mirrors
`retrieval.eval.runner._worst_verdict`. So a kb surface at READY +
memory at NOT YET prints overall verdict `NOT YET`, and only the kb
surface is unblocked.

## Per-surface retire procedures

Run each procedure only after the retire-checklist returns
`READY TO RETIRE` for that surface (or after a documented
`REVIEW MANUALLY` concurrence + the `--json` report is captured in the
retire-PR description). Never retire on `NOT YET`.

### kb

1. **Capture the retire-checklist output** (`meho retrieval retire-checklist --surface kb --json > retire-kb.json`) as evidence in the retire-PR description; commit `retire-kb.json` to the PR as a checked-in audit artefact.
2. **Open the retire-PR** in [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc) deleting the `kb/` directory. Title: `chore(kb): retire pre-MEHO kb/ directory (post-#373 G4.3)`. Body cites this runbook + the retire-checklist JSON.
3. **Update the consumer `CLAUDE.md`** in the same PR: drop the "grep `kb/`" guidance and point agents at `meho kb search` instead. The consumer-side runbook for migrating away from grep will ship with [G4.1-T6 #420](https://github.com/evoila/meho/issues/420) (`docs/cross-repo/kb-migration.md`); link it from the consumer `CLAUDE.md` once that runbook lands.
4. **Archive the pre-retire SHA** by tagging it `kb-pre-retire-<YYYYMMDD>` and pushing the tag to the consumer repo. The tag is the rollback anchor (see below).
5. **Merge the retire-PR.** The consumer-side ingestion already covers the deleted files; the delete has zero data loss because the documents already live in MEHO's `documents` table.

### memory

1. **Capture the retire-checklist output** (`meho retrieval retire-checklist --surface memory --json > retire-memory.json`) as evidence; commit to a per-operator retire log (the laptop-local archive — there is no shared PR for memory).
2. **Run `meho migrate memory`** ([G5.3 #375](https://github.com/evoila/meho/issues/375)) per-operator to bulk-transfer the laptop-local `memory/` files into MEHO. The migration verb is idempotent; rerun if it errors mid-transfer.
3. **Archive each operator's laptop-local `~/.claude/{projects}/memory/` directory** (e.g. `tar czf ~/memory-pre-retire-<YYYYMMDD>.tgz ~/.claude/.../memory/ && rm -rf ~/.claude/.../memory/`). Keep the archive ≥ 1 month after retire — recovering from a laptop archive is the rollback path; without the archive the rollback degrades to "re-create from server-side memories" (lossy).
4. **Update the operator's local `CLAUDE.md`** to point at `meho memory search` rather than the `memory/` directory.
5. **Repeat for every operator** before the surface is fully retired. Until every operator has retired, the audit-log telemetry will still show non-zero `search_memory` from the laggard operators' MCP clients, but the laptop-local files are the actual source of truth for them; the retire-checklist verdict reflects the team aggregate, not per-operator.

### operations

1. **Capture the retire-checklist output** (`meho retrieval retire-checklist --surface operations --json > retire-operations.json`).
2. **Stop using the `grep docs/<product>-<version>/paths.txt + yq` workflow.** Update the consumer `CLAUDE.md` (and per-operator agent prompts) to redirect at `meho operation search` for op discovery + `meho operation call` for execution.
3. **Keep `docs/<product>-<version>/` in the consumer repo.** This surface's "retire" only deletes the *workflow* (the grep pattern), not the spec directories — those stay as grounding for future re-ingestion (G0.7 spec ingestion) and as the source of truth for spec drift PRs.
4. **No tag** required: the spec directories remain in `main`; rollback is "tell agents to grep again" + (if eval is the reason) re-running [G0.7 spec ingestion](https://github.com/evoila/meho/issues/389) to fix the eval regression.
5. **The retire-checklist criterion 4 baseline** for this surface is "the consumer's `grep paths.txt + yq` flow", not a server-side grep — there is no v0.2 server-side baseline corpus snapshot for the operations surface. Pass the locally-computed baseline metrics into the verb via the `BaselineMetricsOverride` body field (see the CLI's `--baseline-file` flag) so criterion 4 leaves yellow.

## Rollback procedures

Always rollback before re-running the retire-checklist. If the rollback
itself fails, file a `retrieval-migration-blocker` issue immediately
(see "Blocker workflow" below) — the surface is now in a hybrid state
and the next retire conversation must be evidence-backed against the
rollback issue, not the original retire-checklist output.

### kb rollback

1. **Restore the `kb/` directory** from the pre-retire tag:

   ```bash
   git checkout kb-pre-retire-<YYYYMMDD> -- kb/
   git commit -m "Revert: restore kb/ pending blocker resolution"
   ```

2. **Update the consumer `CLAUDE.md`** to re-enable the "grep `kb/`"
   guidance until the blocker resolves.
3. **File a `retrieval-migration-blocker`-labeled issue** on
   `evoila/meho` (Blocker workflow below). Add the `knowledge` area
   label so the retire-checklist's criterion 5 buckets it under kb.
4. **Re-run `meho retrieval retire-checklist --surface kb`** — it
   should now return `NOT YET` on criterion 5 until the blocker
   resolves.

### memory rollback

The memory rollback is the lossiest of the three because laptop-local
files were one-way archived per-operator.

1. **Restore from the per-operator archive** (if kept):

   ```bash
   tar xzf ~/memory-pre-retire-<YYYYMMDD>.tgz -C /
   ```

2. **If no archive exists**: re-create the laptop-local files from the
   server-side memories. Run `meho memory export --scope <scope>`
   per scope and re-shape into the laptop-local `~/.claude/.../memory/`
   directory layout. This is lossy — memory entries created
   server-side that never existed in the laptop-local files remain
   server-side-only (which is fine; the rollback re-establishes the
   pre-retire local cache, not the server state).
3. **Update the operator's local `CLAUDE.md`** to re-enable the
   laptop-local lookup until the blocker resolves.
4. **File a `retrieval-migration-blocker`-labeled issue** with the
   `memory` area label.

### operations rollback

1. **No file restore needed** — `docs/<product>-<version>/` already
   live in the consumer repo.
2. **Update agent prompts + the consumer `CLAUDE.md`** to re-enable
   the `grep paths.txt + yq` workflow until the blocker resolves.
3. **If the regression was eval-driven** (criterion 3 or 4 went red
   after retire), re-run [G0.7 spec ingestion](https://github.com/evoila/meho/issues/389)
   for the affected vendor surface to refresh the `endpoint_descriptor`
   table. The `connector` area label on the resulting blocker issue
   tracks the regression.
4. **File a `retrieval-migration-blocker`-labeled issue** with the
   `connector` area label.

## Blocker workflow

The `retrieval-migration-blocker` label is the operator-facing
emergency brake on retire. Any operator can label any issue
`retrieval-migration-blocker` to halt retire across the affected
surface; the retire-checklist's criterion 5 reports a non-zero open
count and pins the verdict at `NOT YET`.

### One-time label setup

The label is created via the idempotent script in this Task:

```bash
./scripts/setup-retrieval-migration-blocker-label.sh           # apply against evoila/meho
./scripts/setup-retrieval-migration-blocker-label.sh --dry-run # print, do not mutate
REPO=evoila-bosnia/claude-rdc-hetzner-dc \
  ./scripts/setup-retrieval-migration-blocker-label.sh         # also apply on the consumer repo
```

Requires `gh` authenticated as a maintainer with `repo:admin` on the
target repo. Re-running is a no-op (the script uses `gh label create
--force` to update the description/color if drifted).

The script is the deliverable; running it against the live repos is an
operator action documented here, not a CI step.

### Filing a blocker issue

When a retrieval regression surfaces:

1. **File an issue** on `evoila/meho` with `--label retrieval-migration-blocker` + the surface area label (`knowledge` for kb, `memory` for memory, `connector` for operations).
2. **Body must include**:
   - The retire-checklist `--json` output (pre-blocker) for context.
   - The reproducer (the exact query that regressed, the expected vs observed top-N hits, the eval-corpus row if applicable).
   - The proposed fix (corpus row update, embedder re-tune, re-ingestion of the spec, etc.).
3. **The retire-checklist criterion 5 picks it up** on the next run via `gh issue list --label retrieval-migration-blocker --state open`. An issue labeled `retrieval-migration-blocker` *without* a surface label is treated as a **generic blocker** and counted against **every surface** (the conservative interpretation: a generic blocker holds every retire candidate until resolved).

### Resolving a blocker

1. **Fix the underlying issue** (commit landed, eval back to green, regression closed).
2. **Close the issue** — either:
   - As `Closes #<n>` in the fix PR (closes-by-merge: the standard path), or
   - `gh issue close <n> --reason completed` (manual: when the fix wasn't a PR — corpus row update, label re-bucketing, etc.).
3. **Re-run the retire-checklist** — criterion 5 should return to green.
4. **Do not close as `wontfix`** without a written rationale in the issue body. The retire-checklist's view of "criterion 5 is green" must be evidence-backed; a silently-closed blocker is the worst kind of regression (the verdict goes green but the underlying surface is still degraded).

## Operator quick reference

```bash
# Run the checklist
meho retrieval retire-checklist --surface all

# Surface-scoped run with JSON for archive
meho retrieval retire-checklist --surface kb --json > retire-kb-$(date +%F).json

# List open blockers (the gh query criterion 5 runs)
gh issue list \
  --repo evoila/meho \
  --label retrieval-migration-blocker \
  --state open \
  --json number,title,labels

# Surface-bucket the blockers (matches the verb's internal logic)
gh issue list --repo evoila/meho \
  --label retrieval-migration-blocker --label knowledge --state open  # kb
gh issue list --repo evoila/meho \
  --label retrieval-migration-blocker --label memory --state open     # memory
gh issue list --repo evoila/meho \
  --label retrieval-migration-blocker --label connector --state open  # operations

# Create the label (one-time, per repo)
scripts/setup-retrieval-migration-blocker-label.sh
```

## Status

| Surface | Substrate | Retire-checklist | Retire status (as of 2026-05-16) |
| --- | --- | --- | --- |
| kb | [#331 G4.1](https://github.com/evoila/meho/issues/331) | wired ✓ | tooling ready; retire on operator + team-of-4 call |
| memory | [G5.1 (#380)](https://github.com/evoila/meho/issues/380) | wired ✓ | corpus ([T4 #443](https://github.com/evoila/meho/issues/443)) + migration verb ([G5.3 #375](https://github.com/evoila/meho/issues/375)) pending |
| operations | [#388 G0.6](https://github.com/evoila/meho/issues/388) + [#389 G0.7](https://github.com/evoila/meho/issues/389) | wired ✓ | corpus ([T3 #442](https://github.com/evoila/meho/issues/442)) pending |

## References

- Parent Initiative: [#373 G4.3](https://github.com/evoila/meho/issues/373) — three-surface retrieval migration tooling.
- Parent Task: [#446 G4.3-T7](https://github.com/evoila/meho/issues/446) — this runbook + label automation.
- Sibling runbooks (filed; not yet shipped — link when the docs land):
  - [G4.1-T6 #420](https://github.com/evoila/meho/issues/420) `docs/cross-repo/kb-migration.md`.
  - [G5.1-T6 #427](https://github.com/evoila/meho/issues/427) `docs/cross-repo/memory-migration.md`.
- Decision #2: [`docs/decisions/locked-decisions.md`](../decisions/locked-decisions.md) — side-by-side migration + 1-month overlap + retire-when-daily-use-shifts.
- Retire-checklist service: [`backend/src/meho_backplane/retrieval/retire.py`](../../backend/src/meho_backplane/retrieval/retire.py) (T6 #445).
- Retire-checklist CLI: [`cli/internal/cmd/retrieval/retire_checklist.go`](../../cli/internal/cmd/retrieval/retire_checklist.go).
- Codebase pointer (the retire-checklist control flow + threshold contract): [`docs/codebase/backend.md`](../codebase/backend.md) — search for "G4.3-T6 (Task #445) retire-decision verb".
