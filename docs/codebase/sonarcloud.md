<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# SonarCloud checks

[SonarCloud](https://sonarcloud.io/project/overview?id=evoila_meho) checks project
`evoila_meho` in organisation `evoila`. The project is public. Read APIs do not
need a token. A passing new-code gate does not mean the whole project has no
bugs or security findings.

## What runs

CI runs on main pushes, pull requests, and merge-queue commits. Main and PRs
run one unit-test sweep in eight coverage shards. Those shards also satisfy
the existing required Python test check; the four ordinary unit shards do not
run again. Queue commits keep the four fast unit shards and reuse the member
PR Sonar results. No extra coverage sweep runs in the queue.

Both sweeps select the same test files and use the same required prerequisites.
Integration and migration tests stay in their separate jobs. Trusted runs use
the existing private spec checkout and checks, Helm, PHP, embedding model, and
spaCy setup. Fork coverage runs on hosted runners with public container images.
It never reads the private spec shelf or schedules internal runners. Existing
fork exclusions for lint and private-spec tests remain in place. The required
Python check now requires the public fork coverage sweep to pass too. Proven
docs-only PRs keep their lint skip, but still run the measured sweep to supply
the report used by the Sonar gate.

The combine job publishes `python-coverage` only when all eight shards finish
and supply data. Go CI publishes `go-coverage`. Test failures propagate to the
existing Python aggregate; missing reports also fail the Sonar gate.

The `Quality Gate` workflow starts after CI completes. It runs on disposable
GitHub-hosted infrastructure. Fork source is never executed by this workflow.
It downloads the reports from that exact CI run into separate temporary
folders and reads the source under `analyzed-source`. Helper code runs outside
that directory. Trusted settings and scanner work files stay in the runner's
temporary directory. Source files cannot replace those files. Before scanning,
the workflow replaces the PR's `sonar-project.properties` with a copy of its
trusted generated settings. The scanner then uses normal config discovery;
`project.settings` is not combined with `sonar.projectBaseDir`. Before preparing
the scan, the workflow checks every tracked source symlink. Links must resolve
inside the checkout. External, broken, and looping links fail the gate. This
also prevents source links from making the scanner read other runner files. Its inline Python uses isolated mode, so PR-added modules cannot replace
standard-library imports. It does not run repository scripts, local actions, builds, or package installers.

The workflow binds a PR to its current head commit and main base through the
GitHub API. It passes the PR number, source branch, and target branch explicitly
to SonarCloud. PR analysis therefore stays separate from main analysis. Main
pushes update main. Queue commits are not Sonar-scanned: the Free plan does
not support feature-branch analysis. Instead, the queue gate reads the full
current GitHub queue and finds the entry for the exact queue SHA. It requires
a continuous chain from current main to that entry and a successful Sonar gate
on every member PR's exact current head. A passing status must link to this
repository's successful default-branch Quality Gate workflow_run execution,
with a nonexpired successful-scan artifact named for that exact head. A status
from an ordinary PR workflow is not trusted, even if GitHub Actions published
it. Scan proof artifacts last seven days; rerun a scan if its proof expires.
Missing, stale, or unclear membership,
or any missing, pending, or failed PR status, fails the queue gate. Existing
merge-group CI still tests the combined code. The status description says that
the queue commit itself was not Sonar-scanned. A failed or cancelled upstream CI run produces a failed gate
status without scanning.

The scanner waits up to ten minutes for the server's gate result. The workflow
publishes the `SonarCloud Quality Gate` commit status on the analyzed SHA. This
status fails when CI, coverage download, scan, or the server's gate fails.
Scanning success alone does not count as gate success.

Settings come from `sonar-project.properties` at the trusted default-branch
workflow commit. A PR cannot change its own scan scope or redirect the scan
token. A change to scan settings takes effect after it lands on main.

## New code and release versions

Main uses SonarCloud's `previous_version` new-code period. The scanner sets
`sonar.projectVersion` to the latest reachable `v*` release tag. Releases are
identified by git tags; see [RELEASING.md](../RELEASING.md). The version stays
fixed between releases. The analyzed commit is recorded separately with
`sonar.scm.revision`.

Do not use a commit SHA or build number as the project version. It would move
the previous-version comparison window on every scan. SonarSource warns against
this in its [analysis parameter documentation](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/analysis-parameters/parameters-not-settable-in-ui).
PR gates assess the PR's changes against its target branch. The queue check
rechecks all member PR heads. It does not claim to analyze the combined source.

## Safe rollout of a required check

The workflow must land on main before any requirement changes. First prove
main analysis, a same-repository PR, a failing PR gate, and an approved public
fork. Check terminal statuses, exact head SHAs, complete reports, trusted proof
artifacts, and runner use. Keep fork source off internal runners and away from
scan secrets. Use existing completed runs when they test the same head with
the landed workflow. Re-running an older run retains its original commit and
ref; see [GitHub's rerun rules](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).

Queue proof needs a short, controlled initial enforcement window. An advisory
queue gate may start too late: once existing required checks pass, GitHub can
merge the group and remove its entry before `workflow_run` reads it. A missing
advisory queue entry is not evidence that the required gate is broken. GitHub
[documents that merge timing](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue).

Use this bounded sequence:

1. Measure the consolidated PR/main sweep first. Each run must use only the
   eight measured shards; queue runs must use only the four fast shards.
   A pre-consolidation run used about 150 runner-minutes for coverage and combine.
   Measure the new workload instead of assuming that old cost still applies.
2. Let the existing queue drain naturally. Use a quiet window and at most two
   already approved, ready PRs as canaries. Each exact head must already have a
   successful trusted Sonar result and an unexpired proof. Reuse their normal
   CI evidence. If the queue cannot drain, leave the gate advisory for now.
3. Save the full current branch-protection and ruleset settings. Review the
   proposed change: append only `SonarCloud Quality Gate`, with its expected
   publisher, to the existing required checks. Preserve every existing check,
   review rule, queue rule, and bypass restriction. A maintainer makes this
   bounded initial enforcement change only after the earlier proofs pass.
4. Enqueue the prepared canaries. The missing required queue status now holds
   each group after its ordinary CI finishes. The workflow can read the live
   entry, check the full chain from main, and check every member's exact head.
   Capture the queue SHA, member heads, proof run IDs, and terminal status.
   Two normal ready PRs can exercise a multi-member prefix without creating
   extra coverage jobs or artificial PRs. No scan runs on the queue source.
5. If either gate fails or cannot publish, stop adding canaries. Remove only
   those canary entries if they remain queued. Restore only the new Sonar
   requirement to the saved baseline, preserving all prior protections. Keep
   the workflow advisory while the failure is fixed and reviewed. Never post
   a synthetic passing test status to move a canary through.
6. After the live queue proof passes, keep the requirement and record the
   evidence. Use the regression fixtures for missing, failed, expired, forged,
   and stale member proofs; do not disrupt unrelated PRs to manufacture them.

A required check must not be enabled for the whole active queue before its
member heads have been checked. Keep the status name stable. Do not add path
filters that leave required statuses absent. Platform interruption can still
prevent terminal status publication; inspect the upstream run and SHA before
rerunning the affected workflow.

The commit status still shares the GitHub Actions publisher identity with other
repository workflows. Contributors who can run same-repository workflows with
`statuses:write` remain trusted: they could publish the same status directly on
a queue SHA. The member-PR proof checks prevent status forgery from fooling this
workflow, but cannot stop another workflow from publishing the final check
itself. If that contributor threat is in scope, use a dedicated publisher App
or enforce a trusted required workflow at repository/organisation level before
claiming that the gate prevents such forgery.

## How to handle findings

The dashboard uses Multi-Quality Rule (MQR) mode: one rule can have several
quality impacts. Its Reliability and Security impact counts differ from the
older Bugs and Vulnerabilities counts returned by some API metrics. Do not mix
these counts or use the older counts as today's dashboard totals. Inspect each
finding's current impact and severity when choosing work.

The overall Reliability and Security grades summarize open findings across the
project. They are separate from the new-code gate. The Security Review grade
tracks whether security hotspots have been reviewed. A hotspot is a request for
human security review, not proof of a vulnerability.

Review each current production finding before calling it a false positive.
Historical counts and earlier safe decisions are not evidence about today's
code. Test fixtures can cause noise, but they do not explain every bad grade.
Go `*_test.go` files and Python test trees are classified as tests. Generated
code is excluded. Coverage exclusions do not suppress issue analysis.

The person who owns the affected code owns the fix or review evidence. A
security reviewer must check each hotspot's current code, data sources, and
controls. Fix unsafe code. Mark a hotspot Safe only with a reason that names the
control and the evidence checked. Review findings individually in SonarCloud;
do not add broad production exclusions to improve the grade. An accepted-risk
finding needs an owner, a reason, and a review date. A local `NOSONAR` comment or
a sentence in this document does not prove that a server-side review happened.

Prioritize open security findings and reliability bugs by their real effect.
Then reduce new findings through the PR gate. Use current SonarCloud issue
lists, rather than old counts copied into documentation.

## When coverage is missing or drops

A missing Python or Go artifact fails the scan workflow. An incomplete Python
shard set must not produce an XML report. Check the upstream CI run first:

- Confirm all eight shard tests completed and all completion markers were
  uploaded. Inspect their pytest logs and peak-memory steps.
- Confirm the combine job downloaded all shards and published `python-coverage`.
- Confirm the Go job published `go-coverage`.
- Read the scanner log. It must parse `backend/coverage.xml` and load
  `cli/coverage.out`. Python paths are rewritten to the `backend` source root.
- Treat parser warnings as gaps in analysis. Check invalid encoding, shell
  syntax, SQL dialect, and template/JavaScript parsing separately.

Never describe incomplete coverage as a test regression until report import and
shard completeness have been checked.

## Files and references

| Purpose | File |
|---|---|
| Source, test, and coverage scope | [`sonar-project.properties`](../../sonar-project.properties) |
| Scan routing, secrets, and status | [Quality Gate workflow](../../.github/workflows/quality-gate.yml) |
| Coverage shards and report production | [CI workflow](../../.github/workflows/ci.yml) |
| Python coverage paths | [`backend/.coveragerc.ci`](../../backend/.coveragerc.ci) |

Official setup references: [GitHub Actions and gate results](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/ci-based-analysis/github-actions-for-sonarcloud),
[PR parameters](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/analysis-parameters/parameters-not-settable-in-ui),
and [GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).
