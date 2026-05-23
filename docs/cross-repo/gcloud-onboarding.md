<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# GCP (gcloud) op surface onboarding — operator recipe

> Operator-facing recipe for the G3.7 `gcloud-rest-1.0` op surface —
> the `meho gcloud …` verb tree, the agent meta-tool path, and the
> migration off the consumer's `scripts/gcloud.sh` wrapper. The op
> handlers live in
> [`backend/src/meho_backplane/connectors/gcloud/`](../../backend/src/meho_backplane/connectors/gcloud/);
> the engineering-facing companion is
> [`docs/codebase/connectors-gcloud.md`](../codebase/connectors-gcloud.md).
> This doc is the cookbook every RDC operator reads when retiring the
> bash wrapper in favour of `meho gcloud …`.

## What this surface is

The `gcloud-rest-1.0` connector is a **typed** connector: hand-coded
handlers over `httpx` + Google ADC + service-account impersonation,
registered into the G0.6 `endpoint_descriptor` table at backplane
startup. It dispatches under the
`(product="gcloud", version="1.0", impl_id="gcloud-rest")` registry
triple — the connector id `gcloud-rest-1.0`.

**Why typed, not generic?** Google publishes a Discovery Document
spec for most GCP APIs, but the connector needs ADC + impersonation
auth that requires the `google-auth` library — not a plain OpenAPI
`securitySchemes` fetch. Hand-coded typed ops give cleaner integration
with the google-auth credential cache than wrapping a generated client.

The v0.2 op surface (Initiative
[#370](https://github.com/evoila/meho/issues/370)) covers the read
operations an RDC operator uses for project auditing and IAM review:

| Group | Op ID | Notes |
| --- | --- | --- |
| `identity` | `gcloud.about` | Project fingerprint — quick identity check |
| `project` | `gcloud.project.describe` | Full CRM v1 resource dict |
| `services` | `gcloud.services.list` | Enabled / all services; follows pagination |
| `iam` | `gcloud.iam.service_accounts.list` | SA inventory |
| `iam` | `gcloud.iam.policy.read` | Project IAM policy (role→members bindings) |
| `compute` | `gcloud.compute.instances.list` | All zones (aggregatedList) or one `--zone` |
| `compute` | `gcloud.compute.networks.list` | VPC network inventory |
| `compute` | `gcloud.compute.subnetworks.list` | All regions (aggregatedList) or one `--region` |

Eight ops total. All are `safety_level="safe"` and
`requires_approval=False`. Every op dispatches through
`POST /api/v1/operations/call` — auth, policy, audit, broadcast, and
JSONFlux all run as documented in
[CLAUDE.md](../../CLAUDE.md) §6.

## Auth model: ADC + impersonation (no SA JSON keys)

This connector's auth model is **ADC + service-account impersonation**.
There are **no SA JSON keys** — the org-policy constraint
`constraints/iam.disableServiceAccountKeyCreation` forbids SA JSON key
creation on the consumer's GCP organization. The connector enforces
this at the call boundary: any `secret_ref` payload containing SA JSON
key material (fields `private_key`, `private_key_id`,
`client_secret`, etc.) is rejected with a `ValueError` before any
token is built. This is a hard fail — not a warning.

### How the credential chain works

```
Operator workstation / CI runner
  └─ Application Default Credentials (ADC)
       └─ gcloud auth application-default login
          OR GOOGLE_APPLICATION_CREDENTIALS env var
          OR Workload Identity (GKE / Cloud Run)

  ADC source token
  └─ google.auth.impersonated_credentials.Credentials
       └─ target.gcp_impersonate_sa  (e.g. meho-reader@my-project.iam.gserviceaccount.com)
            └─ Bearer token for GCP REST calls
```

The backplane process's ADC identity (service account or user) calls
the GCP [Service Account Token Creator](https://cloud.google.com/iam/docs/create-short-lived-credentials-direct)
API to exchange its own credentials for a short-lived impersonated
token for `target.gcp_impersonate_sa`. The impersonated SA must have
the project-level roles needed to execute the ops (see below).

### Setting up ADC on the backplane host

**Development / on-prem operator laptop:**

```console
$ gcloud auth application-default login
```

This writes `~/.config/gcloud/application_default_credentials.json`.
The `google.auth.default()` call in the connector picks this up
automatically without any additional configuration.

**CI / server-side (non-interactive):**

Set `GOOGLE_APPLICATION_CREDENTIALS` to a Workload Identity or a
non-SA-JSON-key service account key alternative (e.g. Workload
Identity Federation credential configuration file):

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp-wif-config.json
```

**GKE / Cloud Run:**

Workload Identity is picked up automatically by `google.auth.default()`
from the GCE metadata server — no additional configuration is needed.

### Granting the impersonation permission

The backplane's ADC principal (service account or user) needs:

```
roles/iam.serviceAccountTokenCreator
```

granted **on the target service account** (not at project level).
Granting it at project level gives token-creator rights over every SA
in the project — grant it narrowly:

```console
$ gcloud iam service-accounts add-iam-policy-binding \
    meho-reader@my-project.iam.gserviceaccount.com \
    --member="serviceAccount:backplane-sa@infra-project.iam.gserviceaccount.com" \
    --role="roles/iam.serviceAccountTokenCreator"
```

### Granting the target SA the read roles

The target SA (`target.gcp_impersonate_sa`) must have at minimum:

| Op group | Required role |
| --- | --- |
| `gcloud.about`, `gcloud.project.describe` | `roles/resourcemanager.projectViewer` |
| `gcloud.services.list` | `roles/serviceusage.serviceUsageViewer` |
| `gcloud.iam.service_accounts.list` | `roles/iam.serviceAccountViewer` |
| `gcloud.iam.policy.read` | `roles/resourcemanager.projectIamAdmin` (or `roles/viewer` for read-only; `getIamPolicy` requires `resourcemanager.projects.getIamPolicy` which is in `roles/viewer`) |
| `gcloud.compute.instances.list` | `roles/compute.viewer` |
| `gcloud.compute.networks.list` | `roles/compute.viewer` |
| `gcloud.compute.subnetworks.list` | `roles/compute.viewer` |

A single `roles/viewer` project-level binding covers most of these;
assign narrower custom roles for production least-privilege setups.

## Prerequisites

- **Google ADC configured** on the backplane host (see above).
- **A target service account** (`meho-reader@<project>.iam.gserviceaccount.com`
  or similar) with the read roles in the table above.
- **Token Creator binding** on the target SA (see above) granted to the
  backplane's ADC principal.
- **Cloud Resource Manager API enabled** (`cloudresourcemanager.googleapis.com`)
  — needed for `gcloud.about`, `gcloud.project.describe`, and
  `gcloud.iam.policy.read`. Usually enabled by default in active projects.
- **A registered gcloud target** in the MEHO `targets` table (see below).
- **An operator session.** `meho login <backplane-url>` writes the session
  token the CLI reuses across every verb. `meho gcloud …` needs `operator`
  role minimum; `read_only` callers get HTTP 403.

## Target configuration

A gcloud target row carries:

| Field | Example | Notes |
| --- | --- | --- |
| `name` | `rdc-gcp-dev` | Slug used with `--target` flag |
| `product` | `gcloud` | Must be exactly `gcloud` |
| `version` | `1.0` | Must be exactly `1.0` |
| `impl_id` | `gcloud-rest` | Must be exactly `gcloud-rest` |
| `gcp_project` | `rdc-dev-123456` | GCP project ID |
| `gcp_impersonate_sa` | `meho-reader@rdc-dev-123456.iam.gserviceaccount.com` | SA the connector impersonates |
| `secret_ref` | `{}` or Vault path dict | **No SA JSON key fields** (see auth section) |
| `auth_model` | `impersonation` | The only supported auth model |

### targets.yaml entry

```yaml
targets:
  - name: rdc-gcp-dev
    product: gcloud
    version: "1.0"
    impl_id: gcloud-rest
    gcp_project: rdc-dev-123456
    gcp_impersonate_sa: meho-reader@rdc-dev-123456.iam.gserviceaccount.com
    secret_ref: {}
    auth_model: impersonation
```

Register with:

```console
$ meho targets import targets.yaml
```

Verify with:

```console
$ meho targets probe --name rdc-gcp-dev
```

A green probe confirms: ADC credentials valid, impersonation chain
works, Cloud Resource Manager API reachable, `projectId` matches.

## Quick-start

```console
# Verify the target is reachable
$ meho gcloud about --target rdc-gcp-dev

# List all enabled GCP services
$ meho gcloud services list --target rdc-gcp-dev

# Describe the project in full
$ meho gcloud project describe --target rdc-gcp-dev

# List IAM service accounts
$ meho gcloud iam sa list --target rdc-gcp-dev

# Read the project IAM policy (all role→members bindings)
$ meho gcloud iam policy read --target rdc-gcp-dev

# List all Compute instances (all zones)
$ meho gcloud compute instances list --target rdc-gcp-dev

# List instances in a specific zone
$ meho gcloud compute instances list --target rdc-gcp-dev --zone europe-west3-a

# List VPC networks
$ meho gcloud compute networks list --target rdc-gcp-dev

# List subnets (all regions)
$ meho gcloud compute subnets list --target rdc-gcp-dev

# List subnets in one region
$ meho gcloud compute subnets list --target rdc-gcp-dev --region europe-west3

# JSON output for piping to jq
$ meho gcloud iam policy read --target rdc-gcp-dev --json | jq '.result.bindings'
$ meho gcloud compute instances list --target rdc-gcp-dev --json | jq '.result.total'
```

## Verb reference

### `meho gcloud about`

Maps to `gcloud.about`. Returns project identity summary: `project_id`,
`project_number`, `lifecycle_state`, `organization`.

```console
$ meho gcloud about --target rdc-gcp-dev
gcloud-rest-1.0 gcloud.about — status=ok (142ms)
  project_id:      rdc-dev-123456
  project_number:  987654321012
  lifecycle_state: ACTIVE
  organization:    organizations/1234567890
```

**When to use:** Call first when connecting to a new target, or when
you need to confirm the project is active before dispatching further
ops. Also the first diagnostic step when `permission-denied` errors
appear — it exercises the full impersonation chain.

### `meho gcloud project describe`

Maps to `gcloud.project.describe`. Returns the full CRM v1 project
resource: `projectId`, `projectNumber`, `name`, `lifecycleState`,
`createTime`, `labels`, `parent`.

```console
$ meho gcloud project describe --target rdc-gcp-dev
gcloud-rest-1.0 gcloud.project.describe — status=ok (98ms)
  projectId:      rdc-dev-123456
  name:           RDC Dev Project
  lifecycleState: ACTIVE
  createTime:     2024-03-15T09:22:44.123Z
  parent:         organization / 1234567890
```

### `meho gcloud services list`

Maps to `gcloud.services.list`. Lists enabled GCP services by default.
Pass `--all` to include disabled services as well.

```console
$ meho gcloud services list --target rdc-gcp-dev
gcloud-rest-1.0 gcloud.services.list — status=ok (310ms)
  service                                           state    title
  compute.googleapis.com                            ENABLED  Compute Engine API
  iam.googleapis.com                                ENABLED  Identity and Access Management (IAM) API
  …
```

Flags:
- `--all` — include disabled services (`enabled_only=false`).

### `meho gcloud iam sa list`

Maps to `gcloud.iam.service_accounts.list`. Lists all service accounts
in the project.

```console
$ meho gcloud iam sa list --target rdc-gcp-dev
gcloud-rest-1.0 gcloud.iam.service_accounts.list — status=ok (188ms)
  email                                                   disabled   display_name
  meho-reader@rdc-dev-123456.iam.gserviceaccount.com     false      MEHO Reader
  …
```

### `meho gcloud iam policy read`

Maps to `gcloud.iam.policy.read`. Returns the project-level IAM policy:
`version`, `etag`, and all role→members bindings. Use to audit who has
which roles before investigating a permission-denied failure or before
assigning new roles.

```console
$ meho gcloud iam policy read --target rdc-gcp-dev
gcloud-rest-1.0 gcloud.iam.policy.read — status=ok (204ms)
  version: 1
  etag:    BwX...

  role                                               members
  roles/compute.viewer                               serviceAccount:meho-reader@…
  roles/iam.serviceAccountTokenCreator               serviceAccount:backplane-sa@…
  …
```

### `meho gcloud compute instances list`

Maps to `gcloud.compute.instances.list`. Calls Compute v1
`aggregatedList` for a project-wide inventory when `--zone` is omitted;
calls the per-zone `list` API when `--zone` is set.

```console
$ meho gcloud compute instances list --target rdc-gcp-dev
gcloud-rest-1.0 gcloud.compute.instances.list — status=ok (521ms)
  zone                           name                      machine_type              status     internal_ips
  europe-west3-a/instances       rdc-bastion-01            e2-standard-2             RUNNING    10.0.0.5
  …
```

Flags:
- `--zone <zone>` — e.g. `europe-west3-a`; omit for all zones.

Returns a `{rows, total}` JSONFlux-compatible envelope — `total` reports
the full count even when the JSONFlux reducer eventually spills rows to
MinIO.

### `meho gcloud compute networks list`

Maps to `gcloud.compute.networks.list`. Lists all VPC networks in the
project.

```console
$ meho gcloud compute networks list --target rdc-gcp-dev
gcloud-rest-1.0 gcloud.compute.networks.list — status=ok (112ms)
  name                           auto     routing_mode         mtu
  rdc-vpc-prod                   false    REGIONAL             1460
  default                        true     REGIONAL             1460
```

### `meho gcloud compute subnets list`

Maps to `gcloud.compute.subnetworks.list`. Calls `aggregatedList` for
all regions when `--region` is omitted; calls the per-region API when
`--region` is set.

```console
$ meho gcloud compute subnets list --target rdc-gcp-dev
gcloud-rest-1.0 gcloud.compute.subnetworks.list — status=ok (198ms)
  region               name                           cidr_range           purpose
  europe-west3         rdc-subnet-prod                10.0.0.0/24          PRIVATE
  …
```

Flags:
- `--region <region>` — e.g. `europe-west3`; omit for all regions.

## The agent meta-tool path

Per [CLAUDE.md](../../CLAUDE.md) postulate 5, the agent surface is the
~17 meta-tools registered by G0.5 (#226). The CLI verbs are operator
ergonomics over `POST /api/v1/operations/call`; the agent reaches every
gcloud op via:

```
search_operations("gcloud-rest-1.0", "compute instances", group="compute")
call_operation("gcloud-rest-1.0", "gcloud.compute.instances.list",
               target="rdc-gcp-dev", params={"zone": "europe-west3-a"})
```

The `llm_instructions.when_to_use` blurb on each op guides the agent.
The `gcloud.about` blurb includes the auth model summary and the
permission-denied diagnostic hint so the agent can self-recover when
the impersonation chain is broken.

## Org-policy constraint: why SA JSON keys are refused

The consumer's GCP organization has the org-policy constraint
`constraints/iam.disableServiceAccountKeyCreation` active. This
policy:

1. **Prevents creating SA JSON key files** (`gcloud iam service-accounts keys create …`).
2. **Means no SA JSON key exists** that could be stored in Vault for
   the connector to use.
3. **Makes long-lived SA JSON keys a security risk** — they bypass the
   centralized token revocation path.

The connector encodes this constraint at the code level: if a Vault
`secret_ref` payload contains any of the well-known SA JSON key fields
(`private_key`, `private_key_id`, `client_secret`, `client_id`,
`client_email` in the SA-key role, etc.), the connector raises
`ValueError` immediately and **no** token is built. This is not
operator-configurable — it is a hard invariant that reflects the
organization-wide policy.

**What to use instead:** ADC + impersonation (described above). The
backplane's ADC identity exchanges its own short-lived credential for
an impersonated token with a 1-hour lifetime. No long-lived key, no
Vault secret rotation needed for the SA credential itself.

## Migrating off `scripts/gcloud.sh`

The consumer team maintains a `scripts/gcloud.sh` wrapper that calls
the `gcloud` CLI directly. The wrapper retirement follows the same
pattern as the bind9 / vcf-automation migrations.

### Wrapper-flip recipe (per ticket)

For each ticket that currently runs a `gcloud.sh` command:

1. **Identify the `gcloud` CLI command** used in the ticket workflow.
   Example: `gcloud compute instances list --project rdc-dev-123456`.

2. **Map to the MEHO verb:**

   | gcloud CLI | meho gcloud verb |
   | --- | --- |
   | `gcloud projects describe` | `meho gcloud project describe --target <slug>` |
   | `gcloud services list` | `meho gcloud services list --target <slug>` |
   | `gcloud iam service-accounts list` | `meho gcloud iam sa list --target <slug>` |
   | `gcloud projects get-iam-policy` | `meho gcloud iam policy read --target <slug>` |
   | `gcloud compute instances list` | `meho gcloud compute instances list --target <slug>` |
   | `gcloud compute networks list` | `meho gcloud compute networks list --target <slug>` |
   | `gcloud compute networks subnets list` | `meho gcloud compute subnets list --target <slug>` |

3. **Run the MEHO verb** against the matching target:

   ```console
   # Before (wrapper)
   ./scripts/gcloud.sh compute instances list --project rdc-dev-123456

   # After (meho)
   meho gcloud compute instances list --target rdc-gcp-dev
   ```

4. **Update the ticket template** to reference `meho gcloud …` instead
   of `./scripts/gcloud.sh …`. The `--json` flag pipes cleanly into jq
   for existing automation that parses gcloud output.

5. **Retire the wrapper section** in `scripts/gcloud.sh` once the
   team has run the MEHO verb for two consecutive sprints without
   reverting to the wrapper.

### JSON output compatibility

The `--json` flag emits the full `OperationResult` envelope. Scripts
that parsed raw `gcloud` CLI JSON output need a small adaptation:

```bash
# Before (gcloud CLI JSON)
gcloud compute instances list --format=json | jq '.[].name'

# After (meho JSON envelope)
meho gcloud compute instances list --target rdc-gcp-dev --json \
  | jq '.result.rows[].name'
```

The `result.rows` array matches the per-row shapes documented in each
op's `response_schema`.

## Audit and broadcast

Every `meho gcloud …` dispatch writes an audit row to the
`operation_audit_log` table:

- `connector_id = "gcloud-rest-1.0"`
- `op_id` = the dispatched op (e.g. `gcloud.compute.instances.list`)
- `target_id` = the resolved target row ID
- `params_hash` = SHA-256 of the input params (for replay detection)
- `status` = `ok` / `error` / `denied`
- `duration_ms` = connector wall-clock time

Broadcast events follow the standard envelope (CLAUDE.md §6 §7). All
8 ops are `safety_level="safe"` and broadcast with
`risk_level=LOW` unless the agent's policy engine overrides.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `status=error connector_error: ValueError: target … secret_ref contains SA JSON key fields` | Vault secret has SA key material | Replace `secret_ref` with an empty dict `{}`; use ADC+impersonation instead |
| `status=error connector_error: google.auth.exceptions.TransportError` | No ADC configured on backplane host | Run `gcloud auth application-default login` on the backplane; or set `GOOGLE_APPLICATION_CREDENTIALS` |
| `status=error connector_error: google.auth.exceptions.RefreshError … 403 PERMISSION_DENIED` on impersonation | Backplane ADC principal lacks `roles/iam.serviceAccountTokenCreator` on the target SA | Grant the Token Creator binding (see Prerequisites) |
| `status=error connector_error: google.auth.exceptions.RefreshError … 403 PERMISSION_DENIED` on the GCP API | Target SA lacks the required role | Grant the appropriate `roles/viewer` or narrower role on the project |
| `status=error connector_error: httpx.HTTPStatusError … 403` | Target SA cannot access the GCP API | Verify SA roles; check if the relevant GCP API is enabled (`gcloud services list`) |
| `status=error connector_error: httpx.HTTPStatusError … 404 on cloudresourcemanager` | `gcp_project` ID is wrong or project does not exist | Verify `target.gcp_project` matches a real GCP project ID |
| `status=denied` | `read_only` operator token | Use a token with `operator` role |
| Probe fails: `projectId mismatch` | Target `gcp_project` does not match the project the SA belongs to | Correct `gcp_project` in `targets.yaml`; re-import |
| `meho gcloud compute instances list` returns `(0 instances)` for a project with instances | Wrong zone filter, or SA lacks `roles/compute.viewer` | Omit `--zone`; verify SA roles |

## Goal #214 G3.7 gcloud checklist

| Checklist item | Status |
| --- | --- |
| G3.7-T4 #845 — `GcloudConnector` skeleton + ADC+impersonation auth + SA-JSON-key refusal | ✅ merged |
| G3.7-T5 #848 — 8 read-only typed ops (CRM, IAM, Compute, Service Usage) | ✅ merged |
| G3.7-T6 #851 — `meho gcloud …` CLI verbs (all 8 ops) | ✅ this PR |
| MCP `llm_instructions.when_to_use` reviewed; `gcloud.about` carries ADC+impersonation + org-policy auth context | ✅ done (#851) |
| `constraints/iam.disableServiceAccountKeyCreation` rationale documented | ✅ this document |
| httpx_mock (respx) E2E tests for all 8 ops | ✅ `test_connectors_gcloud_e2e.py` (#851) |
| `CI_GCLOUD_CREDENTIALS_PRESENT`-gated live integration tests | ✅ `test_connectors_gcloud_e2e.py` (#851) |
| JSONFlux `rows`+`total` envelope tested (`compute.instances.list`) | ✅ `test_connectors_gcloud_e2e.py` (#851) |
| `docs/cross-repo/gcloud-onboarding.md` with wrapper-flip recipe + ADC setup + org-policy rationale | ✅ this document |

## References

- Initiative: [#370 G3.7 tier-3 standalone](https://github.com/evoila/meho/issues/370);
  Goal [#214](https://github.com/evoila/meho/issues/214) (connector parity).
- Tasks that shipped this surface: [#845](https://github.com/evoila/meho/issues/845) (T4 skeleton + auth),
  [#848](https://github.com/evoila/meho/issues/848) (T5 8 ops),
  [#851](https://github.com/evoila/meho/issues/851) (T6 CLI + E2E + this doc).
- Connector source: [`backend/src/meho_backplane/connectors/gcloud/`](../../backend/src/meho_backplane/connectors/gcloud/).
- CLI verbs: [`cli/internal/cmd/gcloud/`](../../cli/internal/cmd/gcloud/).
- E2E tests: [`backend/tests/test_connectors_gcloud_e2e.py`](../../backend/tests/test_connectors_gcloud_e2e.py).
- Engineering codebase doc: [`docs/codebase/connectors-gcloud.md`](../codebase/connectors-gcloud.md).
- Org-policy constraint: [GCP docs — best practices for managing SA keys](https://cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys).
- google-auth impersonated_credentials: <https://google-auth.readthedocs.io/en/master/reference/google.auth.impersonated_credentials.html>
- GCP Cloud Resource Manager REST: <https://cloud.google.com/resource-manager/reference/rest>
- Related onboarding docs: [`audit-query.md`](./audit-query.md), [`targets-yaml.md`](./targets-yaml.md), [`vault-onboarding.md`](./vault-onboarding.md).
