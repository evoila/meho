<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Draft consumer-side issue body — Goal #11 DoD bullets 4 + 5

> The maintainer files this as a single issue on
> [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
> by copy-pasting **everything below the marker line** (`---` after
> this paragraph) into `gh issue create`'s body. Same pattern as
> the existing consumer-side coordination ticket filed for Task
> #53 (per Task #53's "Acceptance criteria — coordination ticket
> filed on `evoila-bosnia/claude-rdc-hetzner-dc`" bullet).

The body is **stable text**, not a template — the maintainer does
not have to substitute anything before filing. Every cross-repo
reference (issue numbers, paths, URLs) is final.

After filing the issue, the maintainer records the
consumer-side issue number on a closing comment to producer-side
issue #58 so the round-trip is auditable.

---

## Title

`MEHO v0.1 dogfood: green-smoke counter + targets.yaml rdc-meho entry`

## Body

### Target

Two consumer-side acceptance items, combined into one issue because
they share a producer-side dependency (the green-smoke counter
contract in `evoila/meho/docs/acceptance/green-counter.md`) and a
single audit surface (the `rdc-meho` entry in `targets.yaml`):

1. **`targets.yaml` `rdc-meho` entry.** Register MEHO as a managed
   target the consumer's connector chassis can probe — see the
   producer-side schema spec at
   [`evoila/meho/docs/cross-repo/targets-yaml.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/targets-yaml.md).
2. **5-consecutive-merged-PR green-smoke counter implementation.**
   Implement the counter computation per the producer-side contract
   at
   [`evoila/meho/docs/acceptance/green-counter.md`](https://github.com/evoila/meho/blob/main/docs/acceptance/green-counter.md),
   surface it as a Shields-compatible static-JSON endpoint, and
   wire the producer-side README badge placeholder to the live URL.

This consumer-side issue closes when both items above land green
on this repo.

### Context

This issue is the consumer-side half of
[`evoila-bosnia/meho-internal#58`](https://github.com/evoila-bosnia/meho-internal/issues/58),
which is the producer-side acceptance tracker. The producer-side
issue carries the closing artefact (5 PR numbers, `targets.yaml`
diff, successful probe transcript); this issue carries the
implementation work.

Same pattern as the earlier consumer-side coordination ticket
filed for
[`#53 G2.7-T5`](https://github.com/evoila-bosnia/meho-internal/issues/53):
producer side ships the spec + a draft body; consumer files the
issue with that body verbatim; both sides cross-link.

### Acceptance criteria

#### Half 1 — `targets.yaml` `rdc-meho` entry

- [ ] `targets.yaml` exists on this repo at the root path (or the
  chassis-defined alternative location) — verifiable via
  `[ -f targets.yaml ]` from a clean clone.
- [ ] An entry with `name: rdc-meho` is present — verifiable via
  `yq '.targets[] | select(.name == "rdc-meho")' targets.yaml`
  returning a non-empty mapping.
- [ ] Every required field per the producer-side schema is
  populated and non-null — verifiable via the JSON probe in
  `targets-yaml.md` "Verification commands" section.
- [ ] No anti-patterns from `targets-yaml.md` are present:
  - [ ] `image.repository` is `evoila/meho` (the backplane), not
    `evoila/meho-chart`.
  - [ ] `image.tag_policy` is `sha-<git-sha>` template, never
    `latest`.
  - [ ] `deploy.dispatch_event.event_type` matches the producer's
    `meho-image-pushed` (G2.7-T3); drift here is a silent
    integration failure.
  - [ ] `smoke_gate.counter.min_streak` is `5`.
  - [ ] No duplicate `name: rdc-meho` rows.
- [ ] The connector chassis can probe `rdc-meho` and the probe
  returns success — verifiable via the consumer-side probe
  command (chassis-specific; the canonical form is
  `./scripts/probe.sh rdc-meho` or chassis-equivalent).
- [ ] A successful probe transcript is captured and posted on
  this issue's closing comment.

#### Half 2 — 5-consecutive-merged-PR green-smoke counter

- [ ] A consumer-side service (workflow, scheduled job, or
  connector-chassis subroutine) computes the counter per the
  reference algorithm in
  [`green-counter.md`](https://github.com/evoila/meho/blob/main/docs/acceptance/green-counter.md#reference-algorithm-pseudocode).
- [ ] The counter is exposed as a Shields-compatible static-JSON
  endpoint at a stable URL on this repo's hosted surface (e.g.
  a GitHub Pages site, an S3 bucket, or a chassis-internal
  endpoint reachable from Shields.io). Schema:

  ```json
  {
    "schemaVersion": 1,
    "label": "green smoke",
    "message": "<N>",
    "color": "brightgreen"
  }
  ```

  Color is `brightgreen` when N >= 5, `yellow` when 1 <= N < 5,
  `red` when N = 0.
- [ ] The endpoint refreshes on a chassis-defined cadence (no
  worse than once per hour) AND on every `repository_dispatch`
  event of type `meho-image-pushed` from `evoila/meho` (so the
  counter ticks immediately on a fresh green merge instead of
  lagging by the refresh window).
- [ ] The endpoint URL is published in a way the producer-side
  README can link to. The producer-side
  [`README.md`](https://github.com/evoila/meho/blob/main/README.md)
  ships a placeholder for this badge; once the URL is live, the
  producer-side maintainer replaces the placeholder via a follow-up
  PR.
- [ ] `gh run list --workflow pr-smoke.yml --repo evoila/meho
  --status success --limit 10` is observed to show at least 5
  sequential `success` entries at least once — recorded on this
  issue's closing comment with the 5 PR numbers and timestamps
  for traceability. (No flake tolerance — if a smoke flakes
  during the observation window, the streak resets and the
  observation re-starts.)

### Producer-side artefacts to consume

All landed on `evoila/meho` via the producer-side PR closing
[`#58`](https://github.com/evoila-bosnia/meho-internal/issues/58):

| Artefact | Path on `evoila/meho` | What it gives this issue |
| --- | --- | --- |
| Green-counter contract | [`docs/acceptance/green-counter.md`](https://github.com/evoila/meho/blob/main/docs/acceptance/green-counter.md) | Counter definition, scope, exclusions, data source, reference algorithm |
| `targets.yaml` contract | [`docs/cross-repo/targets-yaml.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/targets-yaml.md) | Schema, field reference, anti-patterns, worked example, health-probe contract |
| Worked example for `rdc-meho` | [`docs/cross-repo/targets-yaml.md` — Worked example](https://github.com/evoila/meho/blob/main/docs/cross-repo/targets-yaml.md#worked-example) | Copy-paste-ready YAML; the consumer's `targets.yaml` should match this verbatim except for chart `version_pin` (consumer chooses cadence) and `notes` (operator-private) |
| README badge placeholder | [`README.md`](https://github.com/evoila/meho/blob/main/README.md) (Status block) | Marker the maintainer replaces with the live badge URL via a follow-up PR once Half 2 lands |
| `repository_dispatch` event spec | [`docs/cross-repo/rke2-infra-coordination.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/rke2-infra-coordination.md) | The `meho-image-pushed` event type the counter listens for to refresh on every green merge |

### Out of scope (do NOT do here)

- **The green-counter contract itself.** Owned by producer side
  (`evoila/meho/docs/acceptance/green-counter.md`). If the
  contract feels off, file an issue against `evoila/meho` — do
  not redefine the counter on this side.
- **Continuous monitoring / SLO discipline.** v0.2 — out of scope
  for both this issue and the producer-side acceptance bar.
- **Multi-environment targets** (`rdc-meho-staging`,
  `rdc-meho-prod`). v0.1 has only the dogfood lab.
- **Counter for non-`main` branches.** v0.1 is trunk-only on
  producer side; consumer-side counter is trunk-only by extension.
- **Auto-promotion of green counter to releases.** v0.2 — when
  releases become frequent.
- **The connector chassis itself.** This issue uses the chassis
  (registers a target, runs a probe); does not change it.

### Verification

```bash
# Half 1 — targets.yaml entry
yq '.targets[] | select(.name == "rdc-meho")' targets.yaml
# Returns the rdc-meho entry as a YAML mapping.

# Half 1 — required fields populated
yq '
  .targets[] | select(.name == "rdc-meho") |
  [.name, .repo.url, .image.registry, .image.repository,
   .chart.registry, .chart.chart, .deploy.dispatch_event.event_type,
   .smoke_gate.workflow, .smoke_gate.counter.min_streak,
   .probes.health.url, .probes.liveness.url,
   .owner, .sensitivity] | @json
' targets.yaml
# All fields non-null; min_streak == 5.

# Half 1 — chassis probe
./scripts/probe.sh rdc-meho
# (or chassis-equivalent — adapt path)

# Half 2 — counter endpoint reachable and well-formed
curl -sSf https://<consumer-counter-endpoint>/green-counter.json | jq .
# Returns {"schemaVersion":1, "label":"green smoke", "message":"<N>", "color":"..."}

# Half 2 — producer-side workflow history shows the streak
gh run list \
  --workflow pr-smoke.yml \
  --repo evoila/meho \
  --branch main \
  --event pull_request_target \
  --limit 10 \
  --json conclusion,headBranch,headSha,databaseId,event
# At least 5 sequential success entries; transcript recorded on closing comment.
```

### References

- Producer-side issue: [`evoila-bosnia/meho-internal#58`](https://github.com/evoila-bosnia/meho-internal/issues/58)
- Producer-side parent Initiative: [`#54 — G2.8 Acceptance / dogfood proof`](https://github.com/evoila-bosnia/meho-internal/issues/54)
- Producer-side parent Goal: [`#11 — Deployable v0.1`](https://github.com/evoila-bosnia/meho-internal/issues/11) — DoD bullets 4 + 5
- Sibling consumer-side coordination ticket: filed under [`#53 G2.7-T5`](https://github.com/evoila-bosnia/meho-internal/issues/53)
- Cross-repo handshake spec: [`evoila/meho/docs/cross-repo/rke2-infra-coordination.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/rke2-infra-coordination.md)
- Green-counter contract: [`evoila/meho/docs/acceptance/green-counter.md`](https://github.com/evoila/meho/blob/main/docs/acceptance/green-counter.md)
- `targets.yaml` schema + worked example: [`evoila/meho/docs/cross-repo/targets-yaml.md`](https://github.com/evoila/meho/blob/main/docs/cross-repo/targets-yaml.md)
- Shields.io endpoint-badge format: <https://shields.io/badges/endpoint-badge>
- `yq` (the YAML query tool used in verification): <https://mikefarah.gitbook.io/yq/>

---

## Filing command

When the time comes, the producer-side maintainer files the
consumer-side issue as follows (run from a checkout of
`claude-rdc-hetzner-dc`):

```bash
# Extract the body section between "## Body" and "## Filing command"
# from this file and pass it to `gh issue create --body-file`.
gh issue create \
  --repo evoila-bosnia/claude-rdc-hetzner-dc \
  --title 'MEHO v0.1 dogfood: green-smoke counter + targets.yaml rdc-meho entry' \
  --body-file <(sed -n '/^## Body$/,/^## Filing command$/p' \
                /path/to/issue-58-consumer-ticket-body.md \
                | sed '/^## Body$/d; /^## Filing command$/d')
```

The single-quoting around the title preserves the colon literally
(shell-safe; no command substitution).
