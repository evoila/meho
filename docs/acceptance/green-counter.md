<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# 5-consecutive-merged-PR green-smoke counter — acceptance contract

> The producer-side specification of Goal #11 DoD bullet 4:
>
> > 5 consecutive merged PRs in `evoila/meho` have green per-PR
> > ephemeral-cluster smoke (the discipline MEHO.X never had).
>
> This document codifies **what the counter is, how it is computed,
> and what makes it "passing"** so the RDC operator running the
> consumer-side stability badge and the maintainer reviewing the
> result are working from one shared definition.
>
> The counter itself is computed and rendered on the consumer side
> ([`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc));
> the producer (this repo) owns the contract + the data source
> (`pr-smoke.yml`'s workflow-run history) the consumer queries.

## Tracking issue

This contract is the producer-side half of
[`evoila-bosnia/meho-internal#58`](https://github.com/evoila-bosnia/meho-internal/issues/58)
(parent Initiative
[#54](https://github.com/evoila-bosnia/meho-internal/issues/54),
parent Goal
[#11](https://github.com/evoila-bosnia/meho-internal/issues/11)).
The consumer-side issue that maintains the counter implementation
and the `targets.yaml` `rdc-meho` entry is drafted at
[`docs/cross-repo/issue-58-consumer-ticket-body.md`](../cross-repo/issue-58-consumer-ticket-body.md);
the maintainer files it on
`evoila-bosnia/claude-rdc-hetzner-dc` using that body verbatim.

## Why this lives in `evoila/meho`

The data source is `pr-smoke.yml`'s workflow-run history on this
repo. When the smoke workflow changes shape — a job is renamed, the
trigger moves from `pull_request` to `pull_request_target`, the
chart-render leg is split out, the `auth available` gate is
collapsed — the counter's definition of "what counts as a smoke
run" changes with it. Pinning the contract here keeps the counter
in lock-step with the workflow it is derived from, while the
consumer's badge and the closing-comment artefact on issue #58 stay
on the consumer side where the environment-specific details live.

## What the counter is

The **green-smoke counter** is a non-negative integer:

> The length of the current contiguous suffix of merged-PR smoke
> runs on `evoila/meho/main` where every run's `conclusion` is
> `success`.

In plain English: walk the merged PRs newest-to-oldest, count how
many in a row passed the per-PR smoke (Task #50), stop at the first
non-success. The counter is **5 or greater** when Goal #11 DoD
bullet 4 is satisfied. A single `failure` or `cancelled` run
resets the counter to 0; there is no flake tolerance by design.

The "current" counter is a function of `main`'s merge history at
the moment the operator queries it. A run that flakes resets the
counter; a subsequent green run starts the next streak at 1, not at
"prior-streak minus the flake".

### Scope (what counts as a smoke run)

A workflow run counts toward the counter **iff** all five
conditions hold:

| # | Condition | Why |
| --- | --- | --- |
| 1 | Workflow file is `.github/workflows/pr-smoke.yml` | This is the per-PR ephemeral-cluster discipline Goal #11 DoD bullet 4 references; the other CI workflows (`ci.yml`, `chart.yml`, `image.yml`, …) verify pieces of the deploy but do not exercise the rke2-infra smoke chain |
| 2 | Trigger event was `pull_request_target` AND the PR merged | A `pull_request_target` run whose PR was *closed without merging* is not part of the merged-PR stream Goal #11 counts |
| 3 | Run's `head_sha` is reachable from `origin/main` after merge | Filters runs against transient PR heads that never made it into the merge graph (force-pushes, rebases that diverged before merge). The canonical check is `git merge-base --is-ancestor <head_sha> origin/main` |
| 4 | Run's `conclusion` is one of: `success`, `skipped` (when the workflow's `auth available` gate fired — see [Exclusions](#exclusions-what-does-not-count)) | A `skipped` smoke run is **not** a `failure`; the workflow's documented behaviour is to skip cleanly when consumer-side auth has not been provisioned. The counter treats `skipped` per its category (counts vs excluded) per the exclusions table |
| 5 | The PR's merge commit is on `evoila/meho`'s `main` branch (not a side branch or release/hotfix branch) | The Goal #11 deploy contract is anchored to `main`; release/hotfix-branch streams are tracked separately if and when they exist |

### Exclusions (what does **not** count)

Some merged-PR smoke runs are excluded from the counter. Excluded
runs do **not reset** the counter and do **not advance** it — they
are silent. The exclusion list is deliberately narrow:

| # | Exclusion | Rationale |
| --- | --- | --- |
| 1 | Docs-only PRs (every changed file in the merged PR matches `docs/**` OR `*.md`) | `pr-smoke.yml`'s `paths-ignore` already skips these at trigger time; the workflow run does not exist, so no counter input is produced. Listed here for completeness — operators reading workflow-run history without context can be confused by "no smoke run for PR #X" entries |
| 2 | Smoke runs that completed with conclusion `skipped` because the workflow's `auth available` gate fired (i.e. neither `RKE2_CA_CERT` nor `RDC_KUBECONFIG` was provisioned at the time the PR ran) | Documented `pr-smoke.yml` behaviour: the workflow is shipped before consumer-side auth lands and skips cleanly while auth is being provisioned. A `skipped` run in this state is "the gate didn't actually run" — neither pass nor fail. Once auth lands, the workflow runs every PR and the exclusion stops triggering for new runs |
| 3 | Runs cancelled by the user (manual `gh run cancel` or a force-push that triggered `cancel-in-progress`) | A cancelled run is not evidence of a regression; it is evidence of operator intent. Counter-resetting on user cancellation would punish housekeeping (rebases, force-pushes for PR cleanup) |
| 4 | Runs that failed solely due to infrastructure unavailability outside MEHO's contract (rke2-infra cluster down, GHCR outage during image pull) | Recorded as an **operator note** in the consumer's badge metadata, not as a counter reset. The discipline here is strict: an exclusion under this row requires a same-day operator note on the consumer-side runbook ("rke2-infra control-plane CPU saturation 14:00–14:30 UTC, smoke run X excluded"). Without the note, the run resets the counter |

Exclusion #4 is the only judgement call. The default is **no
exclusion** (the run counts as a `failure` and resets the counter).
The operator note exists so that the rare legitimate exclusion is
auditable; "the smoke flaked" without a documented infrastructure
cause is **not** an exclusion — it is a counter reset.

## Data source

The canonical data source is GitHub Actions' workflow-run API for
`pr-smoke.yml` on `evoila/meho`:

```bash
gh api repos/evoila/meho/actions/workflows/pr-smoke.yml/runs \
  --jq '.workflow_runs
    | map(select(.event == "pull_request_target"))
    | sort_by(.run_started_at)
    | reverse'
```

This returns runs newest-first. The counter algorithm walks the
list, applies the [scope](#scope-what-counts-as-a-smoke-run) and
[exclusions](#exclusions-what-does-not-count) filters, and stops at
the first non-success.

### Reference algorithm (pseudocode)

```python
def green_counter(runs: list[WorkflowRun], merge_base: Callable[[str], bool]) -> int:
    """Count contiguous green merged-PR smoke runs.

    runs: GitHub workflow_runs API output, newest-first.
    merge_base(sha): True iff sha is reachable from origin/main.

    Returns: the counter (0 or positive).
    """
    streak = 0
    for run in runs:
        # Scope filters: drop runs that aren't part of the merged-PR stream.
        if run.event != "pull_request_target":
            continue
        if not run.pull_requests:
            continue  # No PR association
        pr = run.pull_requests[0]
        if not pr.merged:
            continue
        if not merge_base(run.head_sha):
            continue

        # Exclusion filters: don't reset, don't advance.
        if run.conclusion == "skipped" and gate_was_auth_available(run):
            continue
        if run.conclusion == "cancelled":
            continue
        if run.conclusion == "failure" and operator_note_excludes(run.id):
            continue

        # Counter logic.
        if run.conclusion == "success":
            streak += 1
            continue
        # Any other terminal conclusion resets the counter.
        break

    return streak
```

The producer side does not ship this code (the consumer's
connector chassis owns the implementation per the [split](#why-this-lives-in-evoilameho)).
The pseudocode is the **contract** the consumer-side code is
audited against.

## How to read the counter

Three surfaces, in increasing operator-effort order:

### 1. Consumer-side README badge (preferred)

The maintainer drops a [Shields.io](https://shields.io/) badge into
this repo's README:

```markdown
[![green-smoke counter](https://<consumer-side-endpoint>/green-counter.svg)](https://github.com/evoila/meho/actions/workflows/pr-smoke.yml)
```

The consumer-side endpoint is a static-JSON Shields badge
(`schemaVersion: 1`, `label: green smoke`, `message: <N>`,
`color: brightgreen` when N >= 5 else `yellow` when 1 <= N < 5
else `red`). The README badge placeholder is shipped in this PR
as a Markdown comment; the maintainer replaces the comment with
the live `![…]` line once the consumer-side endpoint is live.

The badge's link target is `pr-smoke.yml`'s workflow page on
`evoila/meho` — clicking it lands the reader on the canonical run
history, which the counter is derived from.

### 2. One-shot CLI check (producer-side, no consumer dependency)

For when the operator needs a quick read without the badge being
live yet:

```bash
gh run list \
  --workflow pr-smoke.yml \
  --repo evoila/meho \
  --branch main \
  --event pull_request_target \
  --limit 20 \
  --json conclusion,headBranch,headSha,databaseId,event \
  --jq '[.[] | select(.event == "pull_request_target")]'
```

Then walk the list newest-first and apply the [reference
algorithm](#reference-algorithm-pseudocode). The CLI form is
deliberately **read-only** — it queries GitHub's API and never
mutates state. Operators with `gh` and read access on `evoila/meho`
can compute the counter without any consumer-side dependency.

### 3. Consumer's connector-chassis probe (verifiable)

The consumer's connector chassis (per `claude-rdc-hetzner-dc`'s
CLAUDE.md) exposes a `rdc-meho` target whose health probe calls a
function-equivalent of the reference algorithm and returns the
counter as part of the health payload. The probe is what closes
Goal #11 DoD bullet 5; the counter is the data the probe surfaces
for bullet 4.

## Acceptance-criteria status

This is a **split-side** task. Three columns: PR-time (producer
side, asserted by this PR's verifier), deployed-time (consumer
side, asserted once consumer-side counter is live + the streak is
observed), and operator-side (recorded in the issue #58 closing
comment).

| # | Acceptance criterion | PR-time (producer) | Deployed-time (consumer) | Operator-side |
| --- | --- | --- | --- | --- |
| 1 | The counter has a single agreed definition shared by producer + consumer | [`docs/acceptance/green-counter.md`](./green-counter.md) — this document — exists, scoped, with the [reference algorithm](#reference-algorithm-pseudocode) | n/a | n/a |
| 2 | The data source is documented and queryable | [Data source](#data-source) section names the exact `gh api` query | n/a | n/a |
| 3 | The README carries a placeholder for the consumer-side badge | Markdown comment shipped in this PR; maintainer drops in the live badge URL once consumer side is up | Maintainer replaces the placeholder; subsequent badge state reflects live counter | n/a |
| 4 | `gh run list --workflow pr-smoke.yml --repo evoila/meho --status success --limit 10` shows at least 5 sequential `success` entries | n/a | Counter ≥ 5 sustained for at least one query window | Recorded as a transcript in the closing comment on issue #58 |
| 5 | PR numbers from those 5 successful smoke runs are recorded in the closing comment for traceability | n/a | n/a | The maintainer (or `/auto-implement-goal` orchestrator's close-out comment) lists the 5 PRs |
| 6 | `targets.yaml` in `claude-rdc-hetzner-dc` includes a `rdc-meho` entry | [`docs/cross-repo/targets-yaml.md`](../cross-repo/targets-yaml.md) — schema + worked example — exists in this PR | Consumer-side `targets.yaml` entry filed and merged | Closing comment links to the consumer-side commit that added the entry |
| 7 | Consumer's connector chassis can probe `rdc-meho` and returns health | [`docs/cross-repo/targets-yaml.md` — Health probe contract](../cross-repo/targets-yaml.md#health-probe-contract) names the probe surface | Consumer-side probe implementation hits `/api/v1/health` and returns status | Closing comment includes a successful probe transcript |

PR-time AC1–AC3 are what this PR's reviewer asserts. AC4–AC7 are
deferred to the consumer-side issue (drafted at
[`docs/cross-repo/issue-58-consumer-ticket-body.md`](../cross-repo/issue-58-consumer-ticket-body.md)).

## Out of scope

- **Continuous monitoring beyond the 5-PR check.** v0.2 introduces
  SLO discipline; v0.1 is a counter that ticks once.
- **Multi-environment counters** (rdc-meho-staging, rdc-meho-prod).
  v0.1 has only the dogfooding lab.
- **Counter for non-`main` branches.** Release/hotfix-branch
  streams are tracked separately if and when they exist; v0.1 is
  trunk-only.
- **Flake-quarantine retry logic.** No flake tolerance by design —
  if a smoke flakes, the counter resets. The discipline this Goal
  is enforcing is "the smoke is so reliable that 5 in a row is
  not lucky"; building in retries papers over the signal that this
  acceptance bar is measuring.
- **Auto-promotion of the green counter to releases.** v0.2, when
  releases become frequent enough that the counter informs
  release-cutting cadence.

## References

- Parent Goal: [#11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11) — DoD bullets 4 + 5
- Parent Initiative: [#54 — G2.8 Acceptance / dogfood proof](https://github.com/evoila-bosnia/meho-internal/issues/54)
- Predecessor: [#50 — Per-PR ephemeral cluster smoke (G2.7-T2)](https://github.com/evoila-bosnia/meho-internal/issues/50)
- Predecessor: [#53 — Cross-repo coordination tracker (G2.7-T5)](https://github.com/evoila-bosnia/meho-internal/issues/53)
- Workflow file: [`.github/workflows/pr-smoke.yml`](../../.github/workflows/pr-smoke.yml)
- Cross-repo handshake: [`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md)
- `targets.yaml` contract: [`docs/cross-repo/targets-yaml.md`](../cross-repo/targets-yaml.md)
- Consumer-side draft issue body: [`docs/cross-repo/issue-58-consumer-ticket-body.md`](../cross-repo/issue-58-consumer-ticket-body.md)
- Shields.io endpoint-badge format: <https://shields.io/badges/endpoint-badge>
- GitHub Actions workflow-runs API: <https://docs.github.com/en/rest/actions/workflow-runs>
- DevOps deep-dive — Acceptance contracts table: [`docs/codebase/devops.md`](../codebase/devops.md#acceptance-contracts-goal-11-dod)
