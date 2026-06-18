<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `targets.yaml` — cross-repo handshake for the `rdc-meho` target

> Cross-repo handshake between `evoila/meho` (this repo, producer)
> and
> [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
> (private; consumer of MEHO and operator of the rke2-infra
> dogfooding cluster).
>
> This page is the upstream-side **tracker** for the consumer's
> `targets.yaml` registration of MEHO as a managed target. The
> actual `targets.yaml` file lives on the consumer side — what
> lives here is the **schema** the consumer file conforms to, the
> shape of the `rdc-meho` entry, and the verification commands
> either side can run to prove the contract holds.

## Tracking issue

This handshake spec is the producer-side half of
[`evoila-bosnia/meho-internal#58`](https://github.com/evoila-bosnia/meho-internal/issues/58)
(parent Initiative
[#54](https://github.com/evoila-bosnia/meho-internal/issues/54),
parent Goal
[#11](https://github.com/evoila-bosnia/meho-internal/issues/11) —
DoD bullet 5). The consumer-side issue that lands the
`targets.yaml` entry and wires it into the connector chassis is
drafted at
[`./issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md).

## Why this handshake exists

[Goal #11](https://github.com/evoila-bosnia/meho-internal/issues/11)
DoD bullet 5 lands when MEHO is a managed target the consumer's
connector chassis can probe — like any other system the consumer
operates. The contract crosses two repo boundaries:

1. **Producer (`evoila/meho`) ships an image, a chart, and a
   federation chain** that pass the install + smoke + rollback
   acceptance bars (DoD bullets 1–3, contracts at
   [`docs/acceptance/`](../acceptance/README.md)).
2. **Consumer (`claude-rdc-hetzner-dc`) registers MEHO as a
   target** in its `targets.yaml` so the chassis can health-probe
   it, hand it to deploy automation, and surface it on the
   consumer-side operator dashboard.

This document is the spec for half 2. It does not redefine the
chassis — it describes the contract MEHO is held to and the
`rdc-meho` entry that satisfies it.

## What `targets.yaml` is

`targets.yaml` is the consumer-side registry of every system the
chassis manages. Each entry names one system, declares the
endpoints it exposes, names the deploy automation that owns it,
and names the smoke gate that promotes a candidate image to
"deployable". The file lives on the consumer side; the contract
this document codifies is the **shape** of an entry, not the
location of the file.

Verbatim location convention on the consumer side:
`targets.yaml` at the consumer repo root, peer to
`manifests/meho/{install.sh, smoke.sh, values-rdc.yaml}` (per Goal
#11 cross-repo deps). If the consumer's chassis stores targets
under a different layout (`config/targets/rdc-meho.yaml`,
`environments/dogfood/targets.yaml`, …), the producer-side
contract is the same; only the relative path differs.

## Schema

The `rdc-meho` entry's required + recommended fields. Field names
are lower_snake_case; lists are YAML sequences; strings are
single-line.

```yaml
targets:
  - name: rdc-meho                       # REQUIRED — chassis lookup key
    description: ...                     # RECOMMENDED — one-line operator-visible blurb
    repo:                                # REQUIRED — producer-side identifying coordinates
      url: https://github.com/evoila/meho
      default_branch: main
    image:                               # REQUIRED — what runs in the cluster
      registry: ghcr.io
      repository: evoila/meho            # backplane image, NOT the chart
      tag_policy: sha-<git-sha>          # immutable tags only — Goal #11 deploy discipline rejects :latest
    chart:                               # REQUIRED — what helm installs
      registry: oci://ghcr.io/evoila
      chart: meho-chart
      version_pin: <semver>              # consumer pins; producer publishes versioned + sha-tagged
    deploy:                              # REQUIRED — how the chassis rolls a new image forward
      strategy: helm-upgrade-install
      values_file: manifests/meho/values-rdc.yaml
      install_script: manifests/meho/install.sh
      dispatch_event:                    # REQUIRED — links to `repository_dispatch` from producer
        event_type: meho-image-pushed    # G2.7-T3 producer-side; see rke2-infra-coordination.md
        source_repo: evoila/meho
    smoke_gate:                          # REQUIRED — promotes a candidate image to "deployable"
      workflow: pr-smoke.yml             # producer-side workflow producing the counter
      source_repo: evoila/meho
      counter:
        contract: docs/acceptance/green-counter.md
        min_streak: 5                    # Goal #11 DoD bullet 4 — 5 consecutive green PRs
    probes:                              # REQUIRED — chassis health-probe targets
      health:
        url: https://meho.evba.lab/api/v1/health   # authenticated
        auth: service-account-jwt
        timeout_seconds: 5
      liveness:
        url: https://meho.evba.lab/healthz         # anonymous (per #25 federation spec)
        auth: none
        timeout_seconds: 2
    acceptance:                          # RECOMMENDED — producer-side contracts the chassis can link to
      install: docs/acceptance/install.md
      smoke: docs/acceptance/smoke.md
      rollback: docs/acceptance/rollback.md
      green_counter: docs/acceptance/green-counter.md
    owner: rdc                           # REQUIRED — operator-owning team on consumer side
    sensitivity: dogfood                 # REQUIRED — chassis routing hint (dogfood ≠ prod)
    notes: |                             # OPTIONAL — free-form operator notes
      v0.1 first managed target; one environment only (dogfood).
      Promotes to v0.2 when SLO discipline lands.
```

### Field reference

| Field | Required? | Type | Notes |
| --- | --- | --- | --- |
| `name` | required | string | Chassis lookup key. Stable across `targets.yaml` revisions — renaming this is a breaking change for the operator dashboard |
| `description` | recommended | string | One-line operator-visible blurb. Avoid release-specific facts that go stale (versions, environment counts) |
| `repo.url` | required | URL | The producer repo. Public — `evoila/meho` is public-from-day-1 |
| `repo.default_branch` | required | string | Used by the chassis when no explicit ref is passed |
| `image.registry` | required | string | Always `ghcr.io` for v0.1 |
| `image.repository` | required | string | `evoila/meho` (the backplane image); **not** `evoila/meho-chart` (chart) |
| `image.tag_policy` | required | string | Document only — chassis uses the latest `sha-<git-sha>` tag the smoke gate has promoted. The literal string `sha-<git-sha>` is the **template**; the chassis substitutes from the latest green dispatch event |
| `chart.registry` | required | string | OCI registry URL for the helm chart |
| `chart.chart` | required | string | `meho-chart` |
| `chart.version_pin` | required | string | Consumer pins the chart semver. Producer publishes both `<semver>` and `sha-<git-sha>`-tagged charts on every merge; consumer decides cadence |
| `deploy.strategy` | required | enum | `helm-upgrade-install` for v0.1. Other values are chassis-defined |
| `deploy.values_file` | required | path | Consumer-side relative path to the values overlay |
| `deploy.install_script` | required | path | Consumer-side relative path to `install.sh` — the wrapper invoking the producer-side `install-verify.sh` |
| `deploy.dispatch_event.event_type` | required | string | Must match the producer-side `repository_dispatch` event type from G2.7-T3 (currently `meho-image-pushed`). Drift between producer and consumer here is a silent failure mode — the chassis subscribes to one event type and the producer emits another |
| `deploy.dispatch_event.source_repo` | required | string | `evoila/meho` |
| `smoke_gate.workflow` | required | string | `pr-smoke.yml` — the producer-side per-PR ephemeral-cluster smoke workflow |
| `smoke_gate.source_repo` | required | string | `evoila/meho` |
| `smoke_gate.counter.contract` | required | path | Relative path on the producer side to the green-counter contract |
| `smoke_gate.counter.min_streak` | required | integer | `5` for v0.1 (Goal #11 DoD bullet 4). Raising this in v0.2 is intentional |
| `probes.health.url` | required | URL | Authenticated `/api/v1/health` — the federation-summary endpoint |
| `probes.health.auth` | required | enum | `service-account-jwt` for v0.1; the chassis mints a Vault-issued JWT scoped to the probe service-account |
| `probes.health.timeout_seconds` | required | integer | 5 seconds — comfortably above the federation chain's warm-cluster latency floor |
| `probes.liveness.url` | required | URL | Anonymous `/healthz` |
| `probes.liveness.auth` | required | enum | `none` — `/healthz` is unauthenticated by design (per `backend/src/meho_backplane/api/v1/health.py`) |
| `probes.liveness.timeout_seconds` | required | integer | 2 seconds — `/healthz` is a single in-process check |
| `acceptance.*` | recommended | paths | Producer-side acceptance contracts. Optional but the chassis links to these from its dashboard when present |
| `owner` | required | string | Operator-owning team on consumer side |
| `sensitivity` | required | enum | Chassis routing hint: `dogfood`, `staging`, `prod`. v0.1 is `dogfood` only |
| `notes` | optional | string | Free-form operator notes |

### Anti-patterns

- **`image.repository: evoila/meho-chart`.** The chart and the
  image are distinct artefacts; the chart references the image.
  `targets.yaml` names the image — what runs in the cluster — not
  the chart.
- **`image.tag_policy: latest`.** Forbidden by Goal #11 deploy
  discipline. Use `sha-<git-sha>` (the canonical CI-built tag).
- **Missing `dispatch_event.event_type`.** Without this, the
  chassis never gets the "new image available" signal and
  promotion stalls silently. The `repository_dispatch` event is
  the only producer-to-consumer channel; missing the subscription
  is a contract violation.
- **`min_streak: 1`.** Defeats the purpose of the green counter.
  Goal #11 chose 5 to enforce "the smoke is so reliable that 5 in
  a row is not lucky"; the streak length is part of the
  acceptance bar.
- **Duplicate `name: rdc-meho` entries.** The chassis is allowed
  one canonical lookup; multiple entries with the same `name` is
  a chassis-time error.

## Worked example

The complete `rdc-meho` entry as it should appear in the
consumer's `targets.yaml`:

```yaml
targets:
  - name: rdc-meho
    description: MEHO governance backplane — RDC dogfood lab, evba.lab.
    repo:
      url: https://github.com/evoila/meho
      default_branch: main
    image:
      registry: ghcr.io
      repository: evoila/meho
      tag_policy: sha-<git-sha>
    chart:
      registry: oci://ghcr.io/evoila
      chart: meho-chart
      version_pin: 0.1.0-beta
    deploy:
      strategy: helm-upgrade-install
      values_file: manifests/meho/values-rdc.yaml
      install_script: manifests/meho/install.sh
      dispatch_event:
        event_type: meho-image-pushed
        source_repo: evoila/meho
    smoke_gate:
      workflow: pr-smoke.yml
      source_repo: evoila/meho
      counter:
        contract: docs/acceptance/green-counter.md
        min_streak: 5
    probes:
      health:
        url: https://meho.evba.lab/api/v1/health
        auth: service-account-jwt
        timeout_seconds: 5
      liveness:
        url: https://meho.evba.lab/healthz
        auth: none
        timeout_seconds: 2
    acceptance:
      install: docs/acceptance/install.md
      smoke: docs/acceptance/smoke.md
      rollback: docs/acceptance/rollback.md
      green_counter: docs/acceptance/green-counter.md
    owner: rdc
    sensitivity: dogfood
    notes: |
      v0.1 first managed target; one environment only (dogfood).
      Promotes to v0.2 when SLO discipline lands.
```

The producer side asserts this YAML parses as valid YAML 1.2 and
that the fields match the [schema](#schema) above. The producer
side does **not** assert that the file is committed on the
consumer side — that is the consumer-side issue's job (drafted at
[`./issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md)).

## Health probe contract

The chassis probes the `rdc-meho` target on a chassis-defined
interval (typically every 30–60 seconds; chassis-side
configurable). Each probe:

1. Mints a short-lived service-account JWT scoped to the probe
   identity, via Vault's OIDC role binding to the consumer's
   Keycloak realm (the same federation path that operator probes
   use; see [`docs/acceptance/smoke.md`](../acceptance/smoke.md)
   leg #4 — Vault).
2. Issues `GET https://meho.evba.lab/api/v1/health` with
   `Authorization: Bearer <jwt>` and a timeout matching
   `probes.health.timeout_seconds`.
3. Asserts HTTP 200 AND `.operator.sub` non-empty AND
   `.vault.reachable == true` AND `.vault.read_ok == true` AND
   `.db.migrated == true`. Same field set the smoke verifier
   asserts (legs #2, #4, #5 of `docs/acceptance/smoke.md`); the
   chassis is in effect running a continuous one-leg smoke.
4. On 5xx or timeout, falls back to the anonymous `/healthz`
   probe (one extra round-trip, 2 second timeout). If `/healthz`
   also fails, the chassis records the target as `unhealthy` and
   pages on the chassis-side rotation.

The probe surface is **read-only**. The federation chain's
side-effects (one audit row per authenticated call, per Task #28's
sync-row-before-response contract) are deliberate — every probe
contributes one audit row, which is the closing-comment artefact
on issue #58 cross-checks against (5 consecutive merged PRs each
contribute a smoke run's worth of audit rows; the probe
contributes its own steady-state stream).

## Verification commands

### Producer-side (this repo, this PR)

This PR's verifier asserts the spec is well-formed:

```bash
# The targets-yaml.md contract parses cleanly under YAML 1.2.
python3 -c "
import yaml, re, pathlib
text = pathlib.Path('docs/cross-repo/targets-yaml.md').read_text()
# Extract the YAML code blocks (worked example and schema).
blocks = re.findall(r'\`\`\`yaml\n(.+?)\n\`\`\`', text, re.DOTALL)
assert blocks, 'no yaml blocks found'
for i, block in enumerate(blocks):
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, dict), f'block {i} not a dict'
    assert 'targets' in parsed, f'block {i} missing targets key'
print(f'{len(blocks)} yaml blocks parsed clean')
"
```

The verifier runs locally during this PR's Phase 7 verification.

### Consumer-side (deferred — issue #58 consumer-side close-out)

```bash
# 1. targets.yaml entry exists and is parseable.
cd ~/repos/evoila-bosnia/claude-rdc-hetzner-dc
yq '.targets[] | select(.name == "rdc-meho")' targets.yaml
# Should return the rdc-meho entry (non-empty, parseable).

# 2. Required fields are populated per the schema in this doc.
yq '
  .targets[] | select(.name == "rdc-meho") |
  [.name, .repo.url, .image.registry, .image.repository,
   .chart.registry, .chart.chart, .deploy.dispatch_event.event_type,
   .smoke_gate.workflow, .smoke_gate.counter.min_streak,
   .probes.health.url, .probes.liveness.url,
   .owner, .sensitivity] | @json
' targets.yaml
# Should print the populated fields; no nulls.

# 3. Chassis probe lands.
~/repos/evoila-bosnia/claude-rdc-hetzner-dc/scripts/probe.sh rdc-meho
# Should return health-payload + status success.
```

## Migration recipe — bulk-importing a `targets.yaml` into MEHO

> Added 2026-05-15 as part of G0.3-T6 (#257). Companion to
> [docs/codebase/cli.md § Targets registry](../codebase/cli.md#targets-registry-meho-targets-g03-224).

This section is the operator-side recipe for moving an existing
consumer-shape `targets.yaml` (the file documented above) into the
MEHO governance backplane via `meho targets import`. It's the
"dual-read overlap" path Initiative #224 §7 calls out: the
backplane becomes the authoritative target registry while the
consumer's existing `targets.yaml` keeps working unchanged.

### Step 0 — Authenticate

```bash
meho login https://meho.evba.lab
```

Stores a bearer token in the OS keyring (or
`$XDG_CONFIG_HOME/meho/credentials.json` on headless hosts). The
tenant is bound to the JWT's `tenant_id` claim — `meho targets
import` writes into that tenant; there is no `--tenant` flag in
v0.2.

### Step 1 — Dry-run the import

```bash
meho targets import rdc-hetzner-dc/targets.yaml --dry-run
```

Prints the per-entry plan. Dry-run is **existence-accurate and
read-only**: it issues a single listing `GET /api/v1/targets` to
learn which names already exist, then classifies each entry exactly
as the real apply would — existing names render `UPDATE` (with the
sparse-PATCH body), new ones render `CREATE` — and returns before
any `POST`/`PATCH`. So `--update --dry-run` against a tenant that
already has a target previews `UPDATE`, matching the apply's
"updated" count (issue #1785). Dry-run still requires a valid
`meho login` session because of that GET; it is no longer air-gapped.
Add `--json` to format the plan as a structured object
(`{create: [...], update: [...], skip: [...]}`) for piping into `jq`
or capturing for diff against a follow-up dry-run.

### Step 2 — First import

```bash
meho targets import rdc-hetzner-dc/targets.yaml
```

Default mode aborts on the first duplicate `name` in the tenant —
the plan is built before any write fires, so a partial-conflict
YAML never leaves the tenant half-imported. The 25-target consumer
file applies in ~5 seconds (sequential POSTs; concurrency is
v0.2.next polish).

### Step 3 — Iterate with `--update`

```bash
meho targets import rdc-hetzner-dc/targets.yaml --update
```

`--update` PATCHes existing targets and POSTs new ones,
mixed-mode-safe. The PATCH body is **sparse** — only YAML-present
fields are sent on the wire, so re-running `--update` against a
YAML that omits some fields does not wipe those columns. This is
the load-bearing contract; see the codebase note linked at the top
of this section for the bug history (PR #362's review on issue
#257, 2026-05-14).

### Mapping rules at a glance

| YAML key | Lands at | Notes |
| --- | --- | --- |
| `name`, `aliases`, `product`, `host`, `port`, `fqdn`, `secret_ref`, `auth_model`, `vpn_required`, `notes` | top-level column | required: `name`, `product`, `host` |
| `preferred_impl_id` | top-level column | G0.3-T1.5 (#477) amendment — G0.6 resolver tie-break override |
| `extras` (explicit block) | `extras` JSONB | merges with the spilled-extras map from unknown keys |
| `fingerprint` | dropped with warning | server-managed; only the probe verb writes it |
| any other key | spilled into `extras` JSONB | the consumer's `sso_realm`, `kubeconfig_field`, `account`, `project_id` land here |

Required fields (`name`, `product`, `host`) are checked locally
before any HTTP request — a malformed YAML fails fast.
Cloud-provider targets that legitimately omit `host` (e.g. GCP
projects accessed by `project_id` + `account`) are rejected by the
CLI parser today; operators with that shape need to either add a
synthetic `host: cloud-provider` line or split cloud-provider
targets into a separate file until the schema grows a
host-optional variant.

### Dual-read overlap

While the consumer migrates, the same `targets.yaml` file can
serve **both** chassis sides:

- The chassis wrappers under `scripts/` keep reading the local
  `targets.yaml` directly for credential resolution.
- The MEHO backplane reads the imported state on every
  `meho targets list` / `meho targets describe` invocation.

Drift detection: re-running `meho targets import --update --dry-run`
prints a plan showing which entries the backplane would PATCH —
empty plan = backplane is in sync with the YAML. The consumer-side
chassis can wire this dry-run into a periodic check (cron / CI job
on the chassis repo) once the backplane URL is reachable from the
chassis runner.

### Verification

```bash
# After import, list every target the backplane sees:
meho targets list

# Spot-check a single entry against the YAML:
meho targets describe rdc-vcenter --json | jq '.notes'

# Re-run --update --dry-run; an empty plan means the backplane is
# in sync with the YAML.
meho targets import rdc-hetzner-dc/targets.yaml --update --dry-run
```

The Python integration test
[`backend/tests/test_api_v1_targets_import.py`](../../backend/tests/test_api_v1_targets_import.py)
pins this round-trip against a SHA-pinned snapshot of the
consumer's real `targets.yaml`; the snapshot lives at
`backend/tests/fixtures/rdc-hetzner-dc-targets.yaml`.

## Status

| Item | Side | State |
| --- | --- | --- |
| Schema definition | producer | landed in this PR ([`./targets-yaml.md`](./targets-yaml.md)) |
| Worked example | producer | landed in this PR ([Worked example](#worked-example) above) |
| Green-counter contract | producer | landed in this PR ([`docs/acceptance/green-counter.md`](../acceptance/green-counter.md)) |
| README badge placeholder | producer | landed in this PR (top of README, replaced when consumer-side endpoint live) |
| Consumer-side `targets.yaml` entry | consumer | pending — tracked at [`./issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md) |
| Connector-chassis probe of `rdc-meho` | consumer | pending — tracked at [`./issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md) |
| 5-PR green-counter observed | consumer | pending — closing-comment artefact on issue #58 |

## References

- Parent Goal: [#11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11) — DoD bullet 5
- Parent Initiative: [#54 — G2.8 Acceptance / dogfood proof](https://github.com/evoila-bosnia/meho-internal/issues/54)
- Predecessor: [#50 — Per-PR ephemeral cluster smoke (G2.7-T2)](https://github.com/evoila-bosnia/meho-internal/issues/50)
- Predecessor: [#53 — Cross-repo coordination tracker (G2.7-T5)](https://github.com/evoila-bosnia/meho-internal/issues/53)
- Sibling handshake: [`./rke2-infra-coordination.md`](./rke2-infra-coordination.md) — per-PR ephemeral smoke + `repository_dispatch`
- Green-counter contract: [`docs/acceptance/green-counter.md`](../acceptance/green-counter.md)
- Smoke acceptance contract: [`docs/acceptance/smoke.md`](../acceptance/smoke.md) — federation chain legs the probe relies on
- Consumer-side draft issue body: [`./issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md)
- YAML 1.2 spec: <https://yaml.org/spec/1.2.2/>
- `yq` (consumer-side query tool): <https://mikefarah.gitbook.io/yq/>
