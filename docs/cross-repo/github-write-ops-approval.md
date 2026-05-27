<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# GitHub write-op approval-queue annotation — operator runbook

> Operator-facing runbook for opting the 4 high-blast-radius
> [`gh-rest-v3`](github-app-credential.md) write operations into the
> [G11.2 approval queue](https://github.com/evoila/meho/issues/803)
> after a fresh ingest.
>
> Part of the
> [G3.11 typed-connector Initiative (#1220)](https://github.com/evoila/meho/issues/1220),
> Task [#1225](https://github.com/evoila/meho/issues/1225).

## Why this exists

The G0.7 review state machine ingests `gh-rest-v3` operations
`is_enabled=False, requires_approval=False` by default. Four of the
~700 GitHub write operations are high-blast-radius enough that an
agent attempting them without operator-in-the-loop approval is
unacceptable for any production target:

| Op (nickname)              | HTTP shape                                                                   | Blast radius |
| -------------------------- | ---------------------------------------------------------------------------- | ------------ |
| `gh.issue.create`          | `POST /repos/{owner}/{repo}/issues`                                          | low/medium   |
| `gh.pr.merge`              | `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge`                        | HIGH         |
| `gh.workflow_run.dispatch` | `POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches`      | variable     |
| `gh.release.create`        | `POST /repos/{owner}/{repo}/releases`                                        | HIGH         |

The script
[`backend/scripts/annotate_github_write_ops.py`](../../backend/scripts/annotate_github_write_ops.py)
flips `requires_approval=True` and tightens `safety_level` to
`dangerous` on these 4 rows. It is **idempotent** — re-running on
already-annotated rows is a no-op — so operators can safely re-run
after every re-ingest.

## When to run

Run **once** after each fresh `gh/v3` ingest. Specifically:

* After `meho connector ingest --catalog gh/v3` has populated the
  `endpoint_descriptor` table with the ~700 GitHub L2 rows.
* After a re-ingest (a quarterly spec refresh, an operator-applied
  `info.version` bump). The flip is preserved by row identity, but
  the script is idempotent so re-running is harmless and removes
  any ambiguity about "did the re-ingest reset my flags?".

## Prerequisites

* The `gh-rest-v3` connector has been ingested (see
  [github-app-credential.md](github-app-credential.md) for the
  per-target credential bootstrap; the ingest itself is operator-
  driven and orthogonal to the credential plumbing).
* The backplane process can reach the same `MEHO_DATABASE_URL`
  the script uses. The script reads the standard env var via
  `meho_backplane.settings.get_settings()`.

> ⚠️ **Parser-fix dependency** — as of MEHO v0.7.0 the G0.7 OpenAPI
> parser doesn't inline `#/components/responses/*` `$ref` shapes,
> which the GitHub REST spec uses extensively. Live ingest of the
> GitHub spec raises `UnsupportedSpecError`; the integration test
> at `backend/tests/integration/test_operations_ingest_github.py`
> is `pytest.xfail`-marked accordingly. **Until that follow-up
> lands** (a sibling Task on Initiative #1220 or a Goal #214 parser
> follow-up), the 4 target rows are absent from `endpoint_descriptor`
> and the script reports `MISSING` for each. The script itself is
> deliverable today; the operationally-meaningful run waits on the
> parser fix.

## Pre-flight (dry-run)

```bash
cd backend
uv run python -m scripts.annotate_github_write_ops --dry-run
```

The dry-run prints one line per op (`WOULD-FLIP` / `OK` / `MISSING`)
and exits `0` if every targeted op is present (irrespective of
current flag state). Exit code `2` means one or more rows are
absent from `endpoint_descriptor` — usually the parser-fix
dependency above; nothing is mutated.

## Apply

```bash
cd backend
uv run python -m scripts.annotate_github_write_ops
```

On success, `gh.pr.merge` (and the other three) now carry
`requires_approval=True, safety_level="dangerous"`. The dispatcher
parks every subsequent agent-initiated `gh.pr.merge` on the G11.2
approval queue (`_handle_needs_approval` at
`backend/src/meho_backplane/operations/dispatcher.py`); operators
approve / reject via the REST / MCP / UI surfaces.

## Verify

The flip is queryable through the standard operations meta-tool
once it's enabled:

```bash
# MCP / REST surface (production-shaped verification)
meho operation view gh.pr.merge | grep requires_approval
# expected: requires_approval: true
```

Or directly against the database (admin-only):

```sql
SELECT op_id, safety_level, requires_approval
  FROM endpoint_descriptor
 WHERE product = 'gh' AND version = 'v3' AND impl_id = 'gh-rest'
   AND op_id IN (
     'POST:/repos/{owner}/{repo}/issues',
     'PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge',
     'POST:/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches',
     'POST:/repos/{owner}/{repo}/releases'
   );
-- expected: all 4 rows; safety_level='dangerous'; requires_approval=true
```

## Schema-vocabulary deviation from the issue body

The Task [#1225](https://github.com/evoila/meho/issues/1225) body
specifies `safety_level="write"` per-op; the
[`endpoint_descriptor` schema](../../backend/src/meho_backplane/db/models.py)
allows only `safe` / `caution` / `dangerous`. The script maps all
4 ops to `safety_level="dangerous"` (the existing high-blast-radius
tier; matches the issue's intent). Widening the enum to admit a
new `write` literal is **out of scope** for T5 — track separately
if the policy team decides the 4-value vocabulary is insufficient.

## Out of scope (per the Task body)

* Annotating the other ~40 GitHub write ops — operators enable +
  annotate further ops via the standard G0.7 review state machine
  (`meho.connector.review.edit_op`) as they need them.
* Per-target approval thresholds (e.g. "approve `gh.pr.merge` on
  internal repos automatically, require approval on external
  repos") — per-tenant policy work, not in scope for this Task.
* Composite-level approval annotations — composites wrap write-class
  L2 sub-ops and approval cascades correctly via the dispatcher's
  recursion (no per-composite annotation needed).
* Automated annotation policy that detects new write ops on
  re-ingest — operator-driven for now.

## See also

* [`docs/cross-repo/github-app-credential.md`](github-app-credential.md)
  — per-target credential bootstrap (App + PAT).
* [`backend/src/meho_backplane/operations/dispatcher.py`](../../backend/src/meho_backplane/operations/dispatcher.py)
  `_handle_needs_approval` — the dispatcher's approval-park mechanism.
* [`backend/tests/integration/test_operations_ingest_github.py`](../../backend/tests/integration/test_operations_ingest_github.py)
  — the xfailed live-ingest test that gates the operationally-
  meaningful run.
