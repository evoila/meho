<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `docs/acceptance/` — deploy acceptance contracts

> Producer-side acceptance contracts for the first deployable release
> (v0.1).
>
> Each file here codifies one point of the v0.1 "done" list so that
> the lab operator running the test and the maintainer reviewing the
> result share a single definition of "passing". The actual end-to-end
> runs execute in the consumer's own deployment repo (a private lab);
> the contracts and producer-side verifiers live here.

## Contents

| File | v0.1 "done" point |
| --- | --- |
| [`install.md`](./install.md) | 1 — `install.sh` cold-deploy → working MEHO in <5 min |
| [`smoke.md`](./smoke.md) | 2 — `smoke.sh` passes (login + status + audit-row + Vault + DB-migration state) |
| [`rollback.md`](./rollback.md) | 3 — `helm rollback meho` verified end-to-end with a non-trivial schema diff |
| [`green-counter.md`](./green-counter.md) | 4 + 5 — 5-consecutive-merged-PR green-smoke counter + a `targets.yaml` entry for the lab's MEHO |

The `green-counter.md` contract is the producer-side spec; the
counter implementation and the `targets.yaml` entry land on the
consumer side per the cross-repo split (see
[`docs/cross-repo/targets-yaml.md`](../cross-repo/targets-yaml.md)
for the `targets.yaml` schema).

These contracts were written for v0.1. The verifiers still run
against a current chart (v0.45.0 or newer); each page notes where the
chart works differently today, for example the built-in PostgreSQL,
which runs the migration in the pod instead of in a hook Job.

## Why these live in `evoila/meho`

The chart, image, and CLI all originate in this repo. When a chart
change invalidates the acceptance bar (a new probe lands, the
minimum cluster version moves, a new pre-install hook appears), the
contract changes here — in lock-step with the code — without the
consumer's `install.sh` / `smoke.sh` needing to know. The split is:

| Side | Owns |
| --- | --- |
| Producer (`evoila/meho`) | The acceptance contract + producer-side verifiers + the chart/image/CLI under test |
| Consumer (the lab's deployment repo) | The environment overlay (its values file), the wrapper scripts (`install.sh`, `smoke.sh`), the operator runbook, and the test-run artefacts (timestamps, transcripts, `helm history`) |

The producer-side verifiers (e.g.
[`scripts/acceptance/install-verify.sh`](../../scripts/acceptance/install-verify.sh))
are invoked as the last step of the consumer's wrapper. The
verifier's exit code is the wrapper's exit code: a passing acceptance
run is one where both sides cooperate on a single boolean outcome.

## How to use these contracts

1. **Operator running a cold-deploy** — read the relevant
   `docs/acceptance/<bullet>.md`, understand what passing looks like,
   then invoke the consumer-side wrapper (`install.sh`,
   `smoke.sh`, …). The wrapper invokes the producer-side verifier as
   its last step.
2. **Maintainer reviewing the test-run artefact** — cross-check the
   captured output against the acceptance criteria table in the
   relevant contract file. The verifier's `[ok]` / `[FAIL]` /
   `[WARN]` lines are stable and grep-able.
3. **Contributor changing the chart** — if your change invalidates a
   producer-side check (e.g. you added a new container that must be
   Ready before MEHO is "working"), update the relevant
   `docs/acceptance/<bullet>.md` + the verifier in the same PR.

## References

- Deploy surface deep-dive:
  [`docs/codebase/devops.md`](../codebase/devops.md)
