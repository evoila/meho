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

CI runs on main pushes, pull requests, and merge-queue commits. Eight Python
coverage shards measure unit tests on main and PRs. Queue runs reuse the
member PR results and do not repeat the coverage sweep. The combine job publishes `python-coverage`
only when every shard completed its tests and supplied worker data. Go CI
publishes `go-coverage`. Integration-test coverage is not included.

The `Quality Gate` workflow starts after CI completes. It runs on disposable
GitHub-hosted infrastructure. Fork source is never executed by this workflow.
It downloads the reports from that exact CI run and reads the source. It does
not run repository scripts, local actions, builds, or package installers.

The workflow binds a PR to its current head commit and main base through the
GitHub API. It passes the PR number, source branch, and target branch explicitly
to SonarCloud. PR analysis therefore stays separate from main analysis. Main
pushes update main. Queue commits are not Sonar-scanned: the Free plan does
not support feature-branch analysis. Instead, the queue gate reads the full
current GitHub queue and finds the entry for the exact queue SHA. It requires
a continuous chain from current main to that entry and a successful Sonar gate
on every member PR's exact current head. Missing, stale, or unclear membership,
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

Do not require the new commit status before this workflow is on main. Existing
branch protection does not require SonarCloud. A maintainer must first check:

1. A same-repository PR produces a terminal status on its head SHA and a separate
   PR analysis in SonarCloud. A failing gate must produce a failed status.
2. An approved public-fork PR runs coverage on GitHub-hosted runners and receives
   a terminal status. Follow GitHub's fork-workflow approval policy. Never send
   fork source to internal runners or run it with scan secrets.
3. A merge-queue run receives a terminal status on the queue SHA. Test a group
   with several PRs, including an earlier member with a missing or failed gate.
   The whole group must fail. Confirm that stale or removed queue entries fail.
4. All eight shards complete within the runner budget. PR coverage adds load;
   monitor queue time and memory before making this required. A successful
   October 25 run used about 150 runner-minutes across eight shards plus the
   combine job. Each PR update adds a sweep. Queue runs do not repeat it.

Then a repository maintainer can require `SonarCloud Quality Gate` for main,
including merge-queue commits. Keep the check name stable. Do not add path filters
that would leave required statuses absent. A workflow cancellation or platform
outage can still interrupt status publication; rerun the failed workflow after
checking its upstream run and SHA.

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
