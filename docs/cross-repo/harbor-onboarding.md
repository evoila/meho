<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Harbor op surface onboarding — operator recipe

> Operator-facing recipe for the G3.5 `harbor-rest-2.x` op surface —
> the `meho harbor …` verb tree, the agent meta-tool path, robot account
> lifecycle, and the `targets.yaml` greenfield entry. The connector
> implementation lives in
> [`backend/src/meho_backplane/connectors/harbor/`](../../backend/src/meho_backplane/connectors/harbor/);
> the engineering-facing companion is
> [`docs/codebase/connectors-harbor.md`](../codebase/connectors-harbor.md).
> This doc is the cookbook every RDC operator reads when onboarding a
> new Harbor registry or retiring a manual `curl` workflow.

## What this surface is

The `harbor-rest-2.x` connector is a **hybrid** connector:

- The 9 curated read-only ops are **ingested** from the Harbor 2.x
  OpenAPI spec via the G0.7 ingest pipeline, stored as
  `EndpointDescriptor` rows with `source_kind='ingested'`, and enabled
  via [`apply_harbor_core_curation`](../../backend/src/meho_backplane/connectors/harbor/core_ops.py).
- Two **typed** ops (`harbor.robot.create`, `harbor.robot.delete`) are
  registered by the G3.5-T9 (#621) registrar and stored with
  `source_kind='typed'`.

All ops dispatch through the same `POST /api/v1/operations/call` route
the agent surface uses — auth, policy, audit, broadcast, and JSONFlux
all run as documented in [CLAUDE.md](../../CLAUDE.md) §6.

Auth is **HTTP Basic on every request** (username + password from
Vault, no session-cookie dance). The connector handles this
transparently via `HarborConnector.auth_headers`.

The v0.2 op surface (Initiative
[#368](https://github.com/evoila/meho/issues/368)) ships:

| Group | CLI verb | `op_id` | Notes |
| --- | --- | --- | --- |
| harbor-system | `meho harbor about` | `GET:/api/v2.0/systeminfo` | Version, auth mode, registry URL |
| harbor-system | `meho harbor health` | `GET:/api/v2.0/health` | Composite health: DB / redis / registry / jobservice |
| harbor-projects | `meho harbor project list` | `GET:/api/v2.0/projects` | Project inventory (public + private) |
| harbor-projects | `meho harbor project info <name>` | `GET:/api/v2.0/projects/{project_name}` | Full project detail + quota |
| harbor-repositories | `meho harbor repository list <project>` | `GET:/api/v2.0/projects/{project_name}/repositories` | Image repositories in a project |
| harbor-repositories | `meho harbor repository info <project> <repo>` | `GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}` | Repository detail |
| harbor-artifacts | `meho harbor artifact list <project> <repo>` | `GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts` | Artifacts (tags + digests + SBOM/sig status) |
| harbor-artifacts | `meho harbor artifact info <project> <repo> <ref>` | `GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}` | Full artifact metadata |
| harbor-robots | `meho harbor robot list` | `GET:/api/v2.0/robots` | System-level robots (no secret returned) |
| harbor-robots | `meho harbor robot create …` | `harbor.robot.create` | Mint a project-scoped robot credential |
| harbor-robots | `meho harbor robot delete …` | `harbor.robot.delete` | Delete a robot account |

The CLI verb tree is **operator ergonomics** over those dispatch routes;
it is **not** a separate data path and is **not** mirrored on the MCP
surface (CLAUDE.md postulate 5).

### Robot secret handling

`harbor.robot.create` is classified `credential_mint` by
[`broadcast/events.py`](../../backend/src/meho_backplane/broadcast/events.py).
The minted secret is returned to the **caller** in the `OperationResult`
but the broadcast event collapses to aggregate-only — the secret never
appears in the SSE stream or in `audit_log.payload`. Store it immediately
after `meho harbor robot create` returns; Harbor does not expose it again.

`GET:/api/v2.0/robots` (list) never returns `secret` in any entry —
this is a Harbor API guarantee, not a MEHO filter.

## Prerequisites

- **A reachable Harbor 2.x registry.** The connector derives the base
  URL from `target.host` + `target.port`. The connector ID
  `harbor-rest-2.x` requires Harbor ≥ 2.0 (semver range
  `">=2.0,<3.0"` in `HarborConnector.supported_version_range`).
- **Service-account credentials in Vault.** The connector reads
  `{"username": ..., "password": ...}` from Vault at `target.secret_ref`.
  Use a Harbor admin account or a system-level robot with the required
  permissions. HTTP Basic is re-sent on every request; no session state
  is cached.
- **A registered Harbor target.** The CLI verbs take `--target <slug>`
  (e.g. `--target prod-harbor`). The target carries `product="harbor"`,
  `host` (the Harbor FQDN — no `https://`), `port` (default 443),
  `secret_ref` (the Vault path to the credentials), and
  `auth_model="shared_service_account"`.
- **The 9 curated ingested ops registered + enabled.** Run
  `apply_harbor_core_curation` once per Harbor target after the G0.7
  spec ingest (see the curation step below).
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses. `meho harbor …` requires `operator` role
  minimum.

## Target registration

### `targets.yaml` entry (greenfield)

```yaml
# targets.yaml excerpt — Harbor product entry (G3.5-T10 #622)
- name: prod-harbor
  product: harbor
  host: harbor.rdc.evoila.io
  port: 443
  secret_ref: kv/data/harbor/prod-harbor
  auth_model: shared_service_account
  notes: "RDC production Harbor 2.11 registry"
```

### CLI import

```bash
meho targets import \
  --name prod-harbor \
  --product harbor \
  --host harbor.rdc.evoila.io \
  --port 443 \
  --secret-ref kv/data/harbor/prod-harbor \
  --auth-model shared_service_account
```

Verify the fingerprint resolved correctly:

```bash
meho targets probe --name prod-harbor --json | jq '{product, version, reachable}'
# expected: {"product": "harbor", "version": "v2.11.0", "reachable": true}
```

### Credentials in Vault

```bash
# Write the Harbor service-account credentials to Vault.
# Use a Harbor admin account or a system-level robot with registry read
# and robot-management permissions.
vault kv put kv/harbor/prod-harbor \
  username="harbor-svc-account" \
  password="<service-account-password>"
```

## Spec ingest (Swagger 2.0 → OpenAPI 3.x conversion)

Harbor publishes its API spec at
[`api/v2.0/swagger.yaml`](https://raw.githubusercontent.com/goharbor/harbor/v2.11.0/api/v2.0/swagger.yaml),
which is a **Swagger 2.0** document (`swagger: "2.0"`). The G0.7 ingest
parser is OpenAPI-3.x-only and **does not convert in-process**, so
handing it the raw `swagger.yaml` is rejected with an actionable
`UnsupportedSpecError`:

```text
Swagger 2.0 specs are not ingestible directly (document declares
swagger='2.0'); convert it to OpenAPI 3.x first (e.g. the
swagger2openapi CLI `npx swagger2openapi swagger.yaml -o openapi.yaml`,
or the hosted converter at https://converter.swagger.io/), then ingest
the converted 3.x document
```

Convert once, then ingest the 3.x output:

```bash
# Option A — swagger2openapi CLI (Node; the de-facto oas-kit converter)
npx swagger2openapi swagger.yaml -o harbor-openapi3.yaml

# Option B — the hosted converter (no local Node toolchain)
curl -sS https://converter.swagger.io/api/convert \
  -H 'Content-Type: application/yaml' \
  --data-binary @swagger.yaml -o harbor-openapi3.json

# Ingest the converted 3.x document via the explicit-quadruple shape
meho connector ingest \
  --product harbor --version 2.x --impl harbor-rest \
  --spec ./harbor-openapi3.yaml
```

The conversion preserves every operation; the 3.x output ingests
through the same path an OpenAPI-3.x vendor surface would. The catalog
row for `harbor/2.x` flags this same SHARP EDGE (#1532).

## Curation step (run once per Harbor target)

The 9 read-only ops must be enabled via the operator-review substrate
before they are dispatchable. Run once after the spec ingest above:

```bash
# From the backplane's Python environment:
python -c "
import asyncio
from meho_backplane.connectors.harbor.core_ops import apply_harbor_core_curation
from meho_backplane.operations.ingest.service import ReviewService
# ReviewService takes a pg_session_factory; wire appropriately.
asyncio.run(apply_harbor_core_curation(review_service, tenant_id=None))
"
```

After the curation completes, the ops appear in `meho operation search`
and every `meho harbor …` alias verb dispatches without error.

## Quick-start

```bash
# System info + version
meho harbor about --target prod-harbor

# Composite health check
meho harbor health --target prod-harbor

# Project inventory
meho harbor project list --target prod-harbor

# Full project detail (quota, metadata)
meho harbor project info library --target prod-harbor

# Repositories in a project
meho harbor repository list library --target prod-harbor

# Repository detail
meho harbor repository info library ubuntu --target prod-harbor

# Artifact list (tags + digests + SBOM/sig status)
meho harbor artifact list library ubuntu --target prod-harbor

# Full artifact metadata (vulnerability summary, SBOM, signature)
meho harbor artifact info library ubuntu latest --target prod-harbor

# Robot account inventory (no secrets returned)
meho harbor robot list --target prod-harbor

# Machine-readable output for any verb
meho harbor project list --target prod-harbor --json | jq '.result[].name'
meho harbor artifact list library ubuntu --target prod-harbor --json | \
  jq '.result[] | {digest: .digest, tags: [.tags[].name]}'

# Escape hatch: run any harbor-rest-2.x op by op_id
meho harbor operation call GET:/api/v2.0/systeminfo --target prod-harbor
meho harbor operation search "robot accounts"
```

## Robot account lifecycle

### Create a project-scoped robot

```bash
# Create a robot for CI/CD pushes to the "myproject" project.
# --duration 90 = 90-day validity; -1 = never expires.
meho harbor robot create \
  --name ci-push \
  --project myproject \
  --duration 90 \
  --target prod-harbor

# Output:
#   id:     42
#   name:   robot$myproject+ci-push
#   secret: <minted-secret>
#
# IMPORTANT: store the secret now — Harbor does not return it again.
```

The secret appears in the terminal and in the `OperationResult` payload
returned to the caller. It does **not** appear in:
- The SSE broadcast stream (`credential_mint` aggregate-only collapse).
- The `audit_log.payload` (the payload carries a `params_hash`, not the
  secret value).

Store it immediately — for example in Vault or as a CI/CD secret.

### Delete a robot

```bash
# Use the numeric ID from the create response (or from 'robot list').
meho harbor robot delete \
  --project myproject \
  --id 42 \
  --target prod-harbor
```

### List robots (audit)

```bash
meho harbor robot list --target prod-harbor
# Shows id, name, enabled status, and expiry timestamp.
# Never returns the secret.

# Check for expired or near-expiry robots:
meho harbor robot list --target prod-harbor --json | \
  jq '.result[] | select(.expires_at != -1) | {name, expires_at}'
```

## Verb reference

### `meho harbor about`

Dispatches `GET:/api/v2.0/systeminfo`. Human output: `harbor_version`,
`auth_mode`, `registry_url`, `external_url`.

```text
$ meho harbor about --target prod-harbor
harbor-rest-2.x GET:/api/v2.0/systeminfo — status=ok (38ms)
  version:      v2.11.0
  auth_mode:    db_auth
  registry_url: harbor.rdc.evoila.io
  external_url: https://harbor.rdc.evoila.io
```

### `meho harbor health`

Dispatches `GET:/api/v2.0/health`. Renders overall status plus
per-component rows: `core`, `database`, `jobservice`, `redis`,
`registry`, `registryctl`.

### `meho harbor project list`

Dispatches `GET:/api/v2.0/projects`. Renders `name`, `public` flag,
`repo_count`, and `owner`. Large registries may have many projects;
use `--json | jq` to filter.

### `meho harbor project info <project_name>`

Dispatches `GET:/api/v2.0/projects/{project_name}`. Renders full
project detail including quota usage/limit (in bytes), `repo_count`,
and metadata flags (`public`, `auto_scan`, etc.).

### `meho harbor repository list <project_name>`

Dispatches `GET:/api/v2.0/projects/{project_name}/repositories`.
Renders `name` (in `{project}/{repo}` form), `artifact_count`, and
`pull_count`.

### `meho harbor repository info <project_name> <repository_name>`

Dispatches `GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}`.
The `repository_name` is the bare image name without the project prefix
(e.g. `ubuntu`, not `library/ubuntu`).

### `meho harbor artifact list <project_name> <repository_name>`

Dispatches the `…/artifacts` endpoint. Each row shows tag name, digest
(truncated), and push time. Multi-tag artifacts render one row per tag.

### `meho harbor artifact info <project_name> <repository_name> <reference>`

Dispatches `…/artifacts/{reference}`. `<reference>` is a tag or a
`sha256:…` digest. Renders digest, size, push time, media type,
all tags, and accessory types (SBOM, signature).

### `meho harbor robot list`

Dispatches `GET:/api/v2.0/robots`. Renders `id`, `name`, `enabled`,
and `expires_at` (Unix timestamp; `-1` = never). **Never returns the
robot secret** — Harbor's list endpoint does not include it.

### `meho harbor robot create`

Dispatches `harbor.robot.create` (typed op). Required flags:
`--name`, `--project`. Optional: `--duration` (days, default `-1`).

The minted secret is printed once. If `--json` is used, it appears in
`.result.secret` of the `OperationResult` envelope.

### `meho harbor robot delete`

Dispatches `harbor.robot.delete` (typed op). Required flags:
`--project`, `--id` (numeric). Irreversible.

### `meho harbor operation search`

Hybrid BM25 + cosine RRF search across all `harbor-rest-2.x` ops.
Useful for discovering op IDs outside the curated 9-op core.

```bash
meho harbor operation search "artifact vulnerabilities"
meho harbor operation search "project quota" --group harbor-projects
```

### `meho harbor operation call`

Escape hatch to dispatch any `harbor-rest-2.x` op by `op_id` without
a dedicated alias verb.

```bash
meho harbor operation call GET:/api/v2.0/health --target prod-harbor
meho harbor operation call GET:/api/v2.0/projects \
  --target prod-harbor --json | jq '.result[].name'
```

## Agent meta-tool path

Agents continue to use `search_operations` / `call_operation` with
`connector_id="harbor-rest-2.x"`. The `meho harbor …` verbs are
operator-only ergonomics (CLAUDE.md postulate 5); agents do not use them.

Example agent reasoning sequence:

1. `search_operations(connector_id="harbor-rest-2.x", query="artifact SBOM")`
   → returns `GET:.../artifacts` and `GET:.../artifacts/{reference}` hits.
2. `call_operation(connector_id="harbor-rest-2.x", op_id="GET:.../artifacts", …)`
   → returns the artifact list with `accessories[].type` entries.
3. If `accessories[].type == "build.sbom"`, call
   `GET:.../artifacts/{reference}` for the full SBOM accessor link.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `auth_expired` on any verb | Vault path wrong or credentials rotated | `vault kv get kv/harbor/<slug>` + re-run curation |
| `HTTP 401: unauthorized` from connector | Harbor admin password changed | Update Vault secret + restart backplane |
| `HTTP 404: project not found` | Project name typo or project in different tenant | `meho harbor project list` to confirm name |
| `HTTP 403: forbidden` on robot create | Service account lacks robot-management permission | Use Harbor admin or a system-level robot |
| `op_id unknown_op` on dispatch | Curation not yet run or ops not ingested | Run `apply_harbor_core_curation` + ingest step |
| Artifact list returns a JSONFlux handle | > JSONFlux threshold items (default 100) | Use `result_query` meta-tool to paginate the handle |

## Related resources

- [`docs/codebase/connectors-harbor.md`](../codebase/connectors-harbor.md) — engineering reference
- [`backend/src/meho_backplane/connectors/harbor/core_ops.py`](../../backend/src/meho_backplane/connectors/harbor/core_ops.py) — curated op metadata
- [`backend/src/meho_backplane/connectors/harbor/ops.py`](../../backend/src/meho_backplane/connectors/harbor/ops.py) — typed op registrar (robot create/delete)
- [`cli/internal/cmd/harbor/`](../../cli/internal/cmd/harbor/) — CLI verb tree source
- [Harbor 2.11 API reference](https://goharbor.io/docs/2.11.0/build-customize-contribute/configure-swagger/) — upstream REST spec
