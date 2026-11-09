<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `targets.yaml` — cross-repo handshake for the MEHO target

> Cross-repo handshake between `evoila/meho` (this repo, producer)
> and the consumer's infrastructure repo (private; it runs MEHO
> in its own cluster and manages it as a target).
>
> This page is the upstream-side **tracker** for the consumer's
> `targets.yaml` registration of MEHO as a managed target. The
> actual `targets.yaml` file lives on the consumer side — what
> lives here is the **schema** the consumer file conforms to, the
> shape of the MEHO entry, and the verification commands
> either side can run to prove the contract holds.

## Tracking issue

This handshake spec is the producer-side half of the v0.1
"MEHO is a managed target" acceptance item. The consumer-side issue that lands the
`targets.yaml` entry and wires it into the connector chassis is
drafted at
[`./issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md).

## Why this handshake exists

The v0.1 acceptance item lands when MEHO is a managed target the consumer's
connector chassis can probe — like any other system the consumer
operates. The contract crosses two repo boundaries:

1. **Producer (`evoila/meho`) ships an image, a chart, and a
   federation chain** that pass the install + smoke + rollback
   acceptance bars (DoD bullets 1–3, contracts at
   [`docs/acceptance/`](../acceptance/README.md)).
2. **Consumer (the infrastructure repo) registers MEHO as a
   target** in its `targets.yaml` so the chassis can health-probe
   it, hand it to deploy automation, and surface it on the
   consumer-side operator dashboard.

This document is the spec for half 2. It does not redefine the
chassis — it describes the contract MEHO is held to and the
MEHO entry that satisfies it.

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
`manifests/meho/{install.sh, smoke.sh, values.yaml}`. If the consumer's chassis stores targets
under a different layout (`config/targets/meho.yaml`,
`environments/dogfood/targets.yaml`, …), the producer-side
contract is the same; only the relative path differs.

## Schema

The MEHO entry's required + recommended fields. Field names
are lower_snake_case; lists are YAML sequences; strings are
single-line.

```yaml
targets:
  - name: meho                           # REQUIRED — chassis lookup key
    description: ...                     # RECOMMENDED — one-line operator-visible blurb
    repo:                                # REQUIRED — producer-side identifying coordinates
      url: https://github.com/evoila/meho
      default_branch: main
    image:                               # REQUIRED — what runs in the cluster
      registry: ghcr.io
      repository: evoila/meho            # backplane image, NOT the chart
      tag_policy: sha-<git-sha>          # immutable tags only — :latest is rejected
    chart:                               # REQUIRED — what helm installs
      registry: oci://ghcr.io/evoila
      chart: meho-chart
      version_pin: <semver>              # consumer pins; producer publishes release and per-commit charts
    deploy:                              # REQUIRED — how the chassis rolls a new image forward
      strategy: helm-upgrade-install
      values_file: manifests/meho/values.yaml
      install_script: manifests/meho/install.sh
    smoke_gate:                          # REQUIRED — promotes a candidate image to "deployable"
      workflow: pr-smoke.yml             # producer-side workflow producing the counter
      source_repo: evoila/meho
      counter:
        contract: docs/acceptance/green-counter.md
        min_streak: 5                    # 5 consecutive green PRs
    probes:                              # REQUIRED — chassis health-probe targets
      health:
        url: https://meho.example.com/api/v1/health   # authenticated, needs the operator role
        auth: service-account-jwt
        timeout_seconds: 5
      liveness:
        url: https://meho.example.com/healthz         # anonymous
        auth: none
        timeout_seconds: 2
    acceptance:                          # RECOMMENDED — producer-side contracts the chassis can link to
      install: docs/acceptance/install.md
      smoke: docs/acceptance/smoke.md
      rollback: docs/acceptance/rollback.md
      green_counter: docs/acceptance/green-counter.md
    owner: platform-team                 # REQUIRED — operator-owning team on consumer side
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
| `image.tag_policy` | required | string | Document only — chassis uses the latest `sha-<git-sha>` tag the smoke gate has promoted. The literal string `sha-<git-sha>` is the **template**; the chassis fills in the full 40-character commit SHA of the image it picked |
| `chart.registry` | required | string | OCI registry URL for the helm chart |
| `chart.chart` | required | string | `meho-chart` |
| `chart.version_pin` | required | string | Consumer pins the chart version. Producer publishes a `<x.y.z>` chart for each release tag and a `0.1.<YYYYMMDD>-<short-sha>` chart for every commit on `main`; consumer decides cadence |
| `deploy.strategy` | required | enum | `helm-upgrade-install` for v0.1. Other values are chassis-defined |
| `deploy.values_file` | required | path | Consumer-side relative path to the values overlay |
| `deploy.install_script` | required | path | Consumer-side relative path to `install.sh` — the wrapper invoking the producer-side `install-verify.sh` |
| `smoke_gate.workflow` | required | string | `pr-smoke.yml` — the producer-side per-PR ephemeral-cluster smoke workflow |
| `smoke_gate.source_repo` | required | string | `evoila/meho` |
| `smoke_gate.counter.contract` | required | path | Relative path on the producer side to the green-counter contract |
| `smoke_gate.counter.min_streak` | required | integer | `5` for v0.1. Raising this in v0.2 is intentional |
| `probes.health.url` | required | URL | Authenticated `/api/v1/health` — the federation-summary endpoint. A monitoring account with only the `read_only` role uses `/api/v1/health/live` instead: it checks the login and the database and never calls Vault |
| `probes.health.auth` | required | enum | `service-account-jwt`: a JWT from the Keycloak realm MEHO trusts, for a service account with at least the `operator` role. A lower role gets 403 `insufficient_role` |
| `probes.health.timeout_seconds` | required | integer | 5 seconds — comfortably above the federation chain's warm-cluster latency floor |
| `probes.liveness.url` | required | URL | Anonymous `/healthz` |
| `probes.liveness.auth` | required | enum | `none` — `/healthz` is unauthenticated by design (per `backend/src/meho_backplane/health.py`) |
| `probes.liveness.timeout_seconds` | required | integer | 2 seconds — `/healthz` is a single in-process check |
| `acceptance.*` | recommended | paths | Producer-side acceptance contracts. Optional but the chassis links to these from its dashboard when present |
| `owner` | required | string | Operator-owning team on consumer side |
| `sensitivity` | required | enum | Chassis routing hint: `dogfood`, `staging`, `prod`. v0.1 is `dogfood` only |
| `notes` | optional | string | Free-form operator notes |

MEHO sends no `repository_dispatch` event when it pushes an image.
The chassis finds a new image by watching the tags of
`ghcr.io/evoila/meho` or the GitHub Releases feed of `evoila/meho`.

### Anti-patterns

- **`image.repository: evoila/meho-chart`.** The chart and the
  image are distinct artefacts; the chart references the image.
  `targets.yaml` names the image — what runs in the cluster — not
  the chart.
- **`image.tag_policy: latest`.** Forbidden by the deploy rules.
  Use `sha-<git-sha>` (the canonical CI-built tag).
- **`min_streak: 1`.** Defeats the purpose of the green counter.
  The v0.1 acceptance bar chose 5 to enforce "the smoke is so reliable that 5 in
  a row is not lucky"; the streak length is part of the
  acceptance bar.
- **Duplicate `name: meho` entries.** The chassis is allowed
  one canonical lookup; multiple entries with the same `name` is
  a chassis-time error.

## Worked example

The complete MEHO entry as it should appear in the
consumer's `targets.yaml`:

```yaml
targets:
  - name: meho
    description: MEHO governance backplane — dogfood environment.
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
      version_pin: 0.45.0
    deploy:
      strategy: helm-upgrade-install
      values_file: manifests/meho/values.yaml
      install_script: manifests/meho/install.sh
    smoke_gate:
      workflow: pr-smoke.yml
      source_repo: evoila/meho
      counter:
        contract: docs/acceptance/green-counter.md
        min_streak: 5
    probes:
      health:
        url: https://meho.example.com/api/v1/health
        auth: service-account-jwt
        timeout_seconds: 5
      liveness:
        url: https://meho.example.com/healthz
        auth: none
        timeout_seconds: 2
    acceptance:
      install: docs/acceptance/install.md
      smoke: docs/acceptance/smoke.md
      rollback: docs/acceptance/rollback.md
      green_counter: docs/acceptance/green-counter.md
    owner: platform-team
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

The chassis probes the MEHO target on a chassis-defined
interval (typically every 30–60 seconds; chassis-side
configurable). Each probe:

1. Gets a short-lived service-account JWT for the probe identity
   from the Keycloak realm MEHO trusts. The account needs at least
   the `operator` role. MEHO forwards this JWT to Vault, the same
   federation path that operator probes use (see
   [`docs/acceptance/smoke.md`](../acceptance/smoke.md) leg #4 — Vault).
2. Issues `GET https://meho.example.com/api/v1/health` with
   `Authorization: Bearer <jwt>` and a timeout matching
   `probes.health.timeout_seconds`.
3. Asserts HTTP 200 AND `.operator.sub` non-empty AND
   `.vault.reachable == true` AND `.vault.read_ok == true` AND
   `.db.migrated == true`. Same field set the smoke verifier
   asserts (legs #2, #4, #5 of `docs/acceptance/smoke.md`); the
   chassis is in effect running a continuous one-leg smoke.
   MEHO reports Vault problems as HTTP 200 with these flags set
   to `false`, not as a 5xx.
4. On 5xx or timeout, falls back to the anonymous `/healthz`
   probe (one extra round-trip, 2 second timeout). If `/healthz`
   also fails, the chassis records the target as `unhealthy` and
   pages on the chassis-side rotation.

The probe surface is **read-only**. The federation chain's
side-effects (one audit row per authenticated call: the audit row is written
before the response) are deliberate — every probe
contributes one audit row, which the consumer-side close-out
cross-checks against (5 consecutive merged PRs each
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

### Consumer-side (deferred — consumer-side close-out)

```bash
# 1. targets.yaml entry exists and is parseable.
cd <your-infrastructure-repo>
yq '.targets[] | select(.name == "meho")' targets.yaml
# Should return the MEHO entry (non-empty, parseable).

# 2. Required fields are populated per the schema in this doc.
yq '
  .targets[] | select(.name == "meho") |
  [.name, .repo.url, .image.registry, .image.repository,
   .chart.registry, .chart.chart,
   .smoke_gate.workflow, .smoke_gate.counter.min_streak,
   .probes.health.url, .probes.liveness.url,
   .owner, .sensitivity] | @json
' targets.yaml
# Should print the populated fields; no nulls.

# 3. Chassis probe lands.
./scripts/probe.sh meho
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
meho login https://meho.example.com
```

Stores a bearer token in the OS keyring (or
`$XDG_CONFIG_HOME/meho/credentials.json` on headless hosts).
`meho targets import` writes into your current tenant. With one
tenant, that is the tenant of your login. With several, pick one
first with `meho tenants use <name>`. There is no `--tenant` flag.

### Step 1 — Dry-run the import

```bash
meho targets import infra/targets.yaml --dry-run
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
meho targets import infra/targets.yaml
```

Default mode aborts on the first duplicate `name` in the tenant —
the plan is built before any write fires, so a partial-conflict
YAML never leaves the tenant half-imported. The 24-target consumer
file applies in ~5 seconds (sequential POSTs; concurrency is
v0.2.next polish).

### Step 3 — Iterate with `--update`

```bash
meho targets import infra/targets.yaml --update
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
| `version` | top-level column | the product version you assert before the first probe (#3649). A versioned connector needs it to match the target. Quote it: `version: "8.0"` — an unquoted number is rejected |
| `flight_recorder_capture` | top-level column | per-target flight-recorder capture setting: leave it out to inherit, `true` for on, `false` for off (#3272) |
| `preferred_impl_id` | top-level column | G0.3-T1.5 (#477) amendment — G0.6 resolver tie-break override |
| `verify_tls`, `tls_ca_pin`, `tls_server_name` | top-level column | per-target TLS trust (#1780 / #1784) and SNI / cert-verification hostname (#2002, mapped by #2643) — set `tls_server_name` to the cert's FQDN when the appliance is reached by IP |
| `secret_field_map` | top-level column | `{connector_field: source_field}` (#3711) — read the secret's `source_field` when the connector asks for `connector_field`; field names only. The flag `--secret-field <connector_field>=<source_field>` (repeatable) adds the same entry to every target in the file and wins over the YAML |
| `extras` (explicit block) | `extras` JSONB | merges with the spilled-extras map from unknown keys; no warning |
| `fingerprint` | dropped with warning | server-managed; only the probe verb writes it |
| any other key | spilled into `extras` JSONB, with a warning | the consumer's `sso_realm`, `kubeconfig_field`, `account`, `project_id` land here. The import prints one warning per key (#3649), so a typo or a missing column is visible. Move the key under `extras:` to silence it |

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
meho targets describe vcenter-prod --json | jq '.notes'

# Re-run --update --dry-run; an empty plan means the backplane is
# in sync with the YAML.
meho targets import infra/targets.yaml --update --dry-run
```

The Python integration test
[`backend/tests/test_api_v1_targets_import.py`](../../backend/tests/test_api_v1_targets_import.py)
pins this round-trip against a SHA-pinned snapshot of the
consumer's real `targets.yaml`; the snapshot lives in
`backend/tests/fixtures/`.

## Status

| Item | Side | State |
| --- | --- | --- |
| Schema definition | producer | landed in this PR ([`./targets-yaml.md`](./targets-yaml.md)) |
| Worked example | producer | landed in this PR ([Worked example](#worked-example) above) |
| Green-counter contract | producer | landed in this PR ([`docs/acceptance/green-counter.md`](../acceptance/green-counter.md)) |
| README badge placeholder | producer | not in the README; the README rewrite in #1456 dropped it |
| Consumer-side `targets.yaml` entry | consumer | pending — tracked at [`./issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md) |
| Connector-chassis probe of the MEHO entry | consumer | pending — tracked at [`./issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md) |
| 5-PR green-counter observed | consumer | pending — consumer-side close-out |

## References

- Sibling handshake: [`./rke2-infra-coordination.md`](./rke2-infra-coordination.md) — per-PR ephemeral smoke + `repository_dispatch`
- Green-counter contract: [`docs/acceptance/green-counter.md`](../acceptance/green-counter.md)
- Smoke acceptance contract: [`docs/acceptance/smoke.md`](../acceptance/smoke.md) — federation chain legs the probe relies on
- Consumer-side draft issue body: [`./issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md)
- YAML 1.2 spec: <https://yaml.org/spec/1.2.2/>
- `yq` (consumer-side query tool): <https://mikefarah.gitbook.io/yq/>
