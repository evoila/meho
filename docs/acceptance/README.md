<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `docs/acceptance/` — Goal #11 dogfood-proof contracts

> Producer-side acceptance contracts for
> [Goal #11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11).
>
> Each file here codifies one Goal #11 Definition-of-Done bullet so that
> the RDC operator running the test and the maintainer reviewing the
> result share a single definition of "passing". The actual end-to-end
> runs execute on the consumer-side
> [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc);
> the contracts and producer-side verifiers live here.

## Contents

| File | Goal #11 DoD bullet | Initiative G2.8 task |
| --- | --- | --- |
| [`install.md`](./install.md) | bullet 1 — `install.sh` cold-deploy → working MEHO at meho.evba.lab in <5 min | [#55](https://github.com/evoila-bosnia/meho-internal/issues/55) |

Siblings to be added by their respective G2.8 tasks as they land:

- [Task #56](https://github.com/evoila-bosnia/meho-internal/issues/56) —
  `smoke.sh` federation-chain proof (DoD bullet 2)
- [Task #57](https://github.com/evoila-bosnia/meho-internal/issues/57) —
  `helm rollback` end-to-end (DoD bullet 3)
- [Task #58](https://github.com/evoila-bosnia/meho-internal/issues/58) —
  5-consecutive-merged-PR green-smoke counter + `targets.yaml` entry
  (DoD bullets 4 + 5)

## Why these live in `evoila/meho`

The chart, image, and CLI all originate in this repo. When a chart
change invalidates the acceptance bar (a new probe lands, the
minimum cluster version moves, a new pre-install hook appears), the
contract changes here — in lock-step with the code — without the
consumer's `install.sh` / `smoke.sh` needing to know. The split is:

| Side | Owns |
| --- | --- |
| Producer (`evoila/meho`) | The acceptance contract + producer-side verifiers + the chart/image/CLI under test |
| Consumer (`claude-rdc-hetzner-dc`) | The environment overlay (`values-rdc.yaml`), the wrapper scripts (`install.sh`, `smoke.sh`), the operator runbook, and the test-run artefacts (timestamps, transcripts, `helm history`) |

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

- Parent Goal:
  [#11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11)
- Parent Initiative:
  [#54 — G2.8 Acceptance / dogfood proof](https://github.com/evoila-bosnia/meho-internal/issues/54)
- Cross-repo handshake:
  [`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md)
- Deploy surface deep-dive:
  [`docs/codebase/devops.md`](../codebase/devops.md)
