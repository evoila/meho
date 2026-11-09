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
> This doc is the cookbook every operator reads when onboarding a
> new Harbor registry or retiring a manual `curl` workflow.

## What this surface is

The `harbor-rest-2.x` connector is a **fully typed** connector:

- 12 read-only ops are **typed** ops (`source_kind='typed'`): the 9-op
  core (`harbor.about` … `harbor.robot.list`) from the
  `register_harbor_typed_operations` registrar, plus the CVE list of one
  artifact, `harbor.project.summary` and `harbor.quota.list` from
  `ops.py`. They dispatch on a fresh boot with
  **zero catalog ingest** — no spec ingest or curation step is required.
- Two **typed** write ops (`harbor.robot.create`, `harbor.robot.delete`)
  are registered in `ops.py`, also `source_kind='typed'`. Both wait until
  a second person approves them.

All ops dispatch through the same `POST /api/v1/operations/call` route
the agent surface uses — auth, policy, audit, broadcast, and JSONFlux
all run on every call.

Auth is **HTTP Basic on every request** (username + password from
Vault, no session-cookie dance). The connector handles this
transparently via `HarborConnector.auth_headers`.

The v0.2 op surface (Initiative
[#368](https://github.com/evoila/meho/issues/368)) ships:

| Group | CLI verb | `op_id` | Notes |
| --- | --- | --- | --- |
| harbor-system | `meho harbor about` | `harbor.about` | Version, auth mode, registry URL |
| harbor-system | `meho harbor health` | `harbor.health` | Composite health: DB / redis / registry / jobservice |
| harbor-projects | `meho harbor project list` | `harbor.project.list` | Project inventory (public + private) |
| harbor-projects | `meho harbor project info <name>` | `harbor.project.info` | Project detail: owner, repo count, metadata (no storage quota) |
| harbor-projects | `meho harbor operation call harbor.project.summary` | `harbor.project.summary` | Storage quota (hard + used, bytes), repo count, member counts of one project |
| harbor-projects | `meho harbor operation call harbor.quota.list` | `harbor.quota.list` | Storage quotas of all projects |
| harbor-repositories | `meho harbor repository list <project>` | `harbor.repository.list` | Image repositories in a project |
| harbor-repositories | `meho harbor repository info <project> <repo>` | `harbor.repository.info` | Repository detail |
| harbor-artifacts | `meho harbor artifact list <project> <repo>` | `harbor.artifact.list` | Artifacts (tags + digests + SBOM/sig status) |
| harbor-artifacts | `meho harbor artifact info <project> <repo> <ref>` | `harbor.artifact.info` | Full artifact metadata |
| harbor-robots | `meho harbor robot list` | `harbor.robot.list` | System-level robots (no secret returned) |
| robot | `meho harbor robot create …` | `harbor.robot.create` | Mint a project-scoped robot credential (`caution`, waits for approval) |
| robot | `meho harbor robot delete …` | `harbor.robot.delete` | Delete a robot account (`destructive`, waits for approval) |

The CLI verb tree is **operator ergonomics** over those dispatch routes;
it is **not** a separate data path and is **not** mirrored on the MCP
surface (CLAUDE.md postulate 5).

### Robot secret handling

`harbor.robot.create` is classified `credential_mint` by
[`broadcast/events.py`](../../backend/src/meho_backplane/broadcast/events.py).
The minted secret is returned to the **caller** in the `OperationResult`
but the broadcast event collapses to aggregate-only — the secret never
appears in the SSE stream or in `audit_log.payload`. The create waits for
approval first; store the secret as soon as `meho approvals result <id>`
shows it. Harbor does not expose it again.

`harbor.robot.list` (list) never returns `secret` in any entry —
this is a Harbor API guarantee, not a MEHO filter.

## Prerequisites

- **A reachable Harbor 2.x registry.** The connector derives the base
  URL from `target.host` + `target.port`. The connector ID
  `harbor-rest-2.x` requires Harbor ≥ 2.0 (semver range
  `">=2.0,<3.0"` in `HarborConnector.supported_version_range`).
- **Service-account credentials in Vault.** The connector reads
  `{"username": ..., "password": ...}` from Vault at `target.secret_ref`.
  Use a Harbor admin account or a system-level robot with the required
  permissions. HTTP Basic is sent on every request. MEHO keeps no
  session; it keeps only the credentials it read from Vault, per target.
- **A registered Harbor target.** The CLI verbs take `--target <slug>`
  (e.g. `--target prod-harbor`). The target carries `product="harbor"`,
  `host` (the Harbor FQDN — no `https://`), `port` (default 443),
  `secret_ref` (the Vault path to the credentials), and
  `auth_model="shared_service_account"`.
- **No ingest or curation step.** The read core + robot write ops are
  code-shipped typed ops (#2856); they register at backplane startup and
  dispatch on a fresh boot with zero catalog state.
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses. `meho harbor …` requires `operator` role
  minimum.

## Target registration

### `targets.yaml` entry (greenfield)

```yaml
# prod-harbor.yaml — Harbor product entry (G3.5-T10 #622)
targets:
  - name: prod-harbor
    product: harbor
    host: harbor.example.com
    port: 443
    secret_ref: harbor/prod-harbor
    auth_model: shared_service_account
    notes: "Production Harbor 2.11 registry"
```

### CLI import

`meho targets import` takes a `targets.yaml` **file**. To register one
target from flags instead, use `meho targets add <name>` (alias `create`):

```bash
meho targets import prod-harbor.yaml   # add --update to PATCH an existing target
```

Verify the fingerprint resolved correctly:

```bash
meho targets probe prod-harbor --json | jq '{product, version, reachable}'
# expected: {"product": "harbor", "version": "v2.11.0", "reachable": true}
```

### Credentials in Vault

```bash
# Write the Harbor service-account credentials to Vault.
# Use a Harbor admin account or a system-level robot with registry read
# and robot-management permissions.
# MEHO reads the `secret` KV-v2 mount. The target's secret_ref is the
# path inside that mount (harbor/prod-harbor), without `secret/` or `/data/`.
vault kv put -mount=secret harbor/prod-harbor \
  username="harbor-svc-account" \
  password="<service-account-password>"
```

## No spec ingest or curation needed

The read core and the robot write ops are code-shipped **typed** ops
(`source_kind='typed'`, #2856) — mirroring the NSX (#2302) and SDDC
Manager (#2306) conversions under Task #2358. They register at backplane
startup (`run_typed_op_registrars`) and dispatch on a fresh boot with
**zero catalog state**. There is no Harbor OpenAPI spec ingest, no
Swagger-2.0 → OpenAPI-3.x conversion, and no operator-review curation
step to run: once the target is registered (above) and its credentials
are in Vault, every `meho harbor …` verb works end to end. The ops are
immediately visible in `meho operation search` (or
`meho harbor operation search`).

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
meho harbor operation call harbor.about --target prod-harbor

# Reads with no alias verb
meho harbor operation call harbor.project.summary --target prod-harbor \
  --params '{"project_name": "library"}'
meho harbor operation call harbor.quota.list --target prod-harbor
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

# The request waits until a second person approves it.
# The command prints the approval id.

# After the approval, MEHO runs the create. Read the result
# (only the person who sent the request can read it):
meho approvals result <approval-id>
#   id:     42
#   name:   robot$myproject+ci-push
#   secret: <minted-secret>
#
# IMPORTANT: store the secret now — Harbor does not return it again.
```

The secret appears in the result returned to the person who sent the
request. It does **not** appear in:
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

Dispatches `harbor.about`. Human output: `harbor_version`,
`auth_mode`, `registry_url`, `external_url`.

```text
$ meho harbor about --target prod-harbor
harbor-rest-2.x harbor.about — status=ok (38ms)
  version:      v2.11.0
  auth_mode:    db_auth
  registry_url: harbor.example.com
  external_url: https://harbor.example.com
```

### `meho harbor health`

Dispatches `harbor.health`. Renders overall status plus
per-component rows: `core`, `database`, `jobservice`, `redis`,
`registry`, `registryctl`.

### `meho harbor project list`

Dispatches `harbor.project.list`. Renders `name`, `public` flag,
`repo_count`, and `owner`. Large registries may have many projects;
use `--json | jq` to filter.

### `meho harbor project info <project_name>`

Dispatches `harbor.project.info`. Renders `name`, `owner`,
`repo_count` and the `public` flag; `--json` shows all metadata flags.
The project object has no storage quota. For the quota, call
`harbor.project.summary` or `harbor.quota.list` with
`meho harbor operation call`.

### `meho harbor repository list <project_name>`

Dispatches `harbor.repository.list`.
Renders `name` (in `{project}/{repo}` form), `artifact_count`, and
`pull_count`.

### `meho harbor repository info <project_name> <repository_name>`

Dispatches `harbor.repository.info`.
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

Dispatches `harbor.robot.list`. Renders `id`, `name`, `enabled`,
and `expires_at` (Unix timestamp; `-1` = never). **Never returns the
robot secret** — Harbor's list endpoint does not include it.

### `meho harbor robot create`

Dispatches `harbor.robot.create` (typed op). Required flags:
`--name`, `--project`. Optional: `--duration` (days, default `-1`).

The op is on the `caution` risk level and needs approval. The result is
`status=awaiting_approval`; the create runs when a second person approves
it. Then the person who sent the request reads the minted secret once
with `meho approvals result <id>` (with `--json`, it is in the result's
`secret` field).

### `meho harbor robot delete`

Dispatches `harbor.robot.delete` (typed op). Required flags:
`--project`, `--id` (numeric). Irreversible.

The op is on the destructive risk level, so a second person must
approve it. The command first previews the delete, then sends it with
the `preview_hash`. The result is `status=awaiting_approval`; the delete
runs when a second person approves it. If the preview fails, the command
stops, shows the reason, and sends nothing.

### `meho harbor operation search`

Hybrid BM25 + cosine RRF search across all `harbor-rest-2.x` ops.
Useful for finding ops that have no alias verb, such as
`harbor.project.summary`, `harbor.quota.list` and the CVE list of one
artifact.

```bash
meho harbor operation search "artifact vulnerabilities"
meho harbor operation search "project quota" --group harbor-projects
```

### `meho harbor operation call`

Escape hatch to dispatch any `harbor-rest-2.x` op by `op_id` without
a dedicated alias verb.

```bash
meho harbor operation call harbor.health --target prod-harbor
meho harbor operation call harbor.project.list \
  --target prod-harbor --json | jq '.result[].name'
```

A destructive op needs `--preview-hash`. Run
`meho operation preview harbor-rest-2.x <op_id>` with the same
`--target` and `--params` first, then pass its `preview_hash`. The call
then waits until a second person approves it.

## Agent meta-tool path

Agents continue to use `search_operations` / `call_operation` with
`connector_id="harbor-rest-2.x"`. The `meho harbor …` verbs are
operator-only ergonomics (CLAUDE.md postulate 5); agents do not use them.

Example agent reasoning sequence:

1. `search_operations(connector_id="harbor-rest-2.x", query="artifact SBOM")`
   → returns `harbor.artifact.list` and `harbor.artifact.info` hits.
2. `call_operation(connector_id="harbor-rest-2.x", op_id="harbor.artifact.list", …)`
   → returns the artifact list with `accessories[].type` entries.
3. If `accessories[].type == "build.sbom"`, call
   `harbor.artifact.info` for the full SBOM accessor link.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `auth_expired` on any verb | Vault path wrong or credentials rotated | `vault kv get -mount=secret harbor/<slug>` + re-stage the secret |
| `HTTP 401: unauthorized` from connector | Harbor admin password changed | Update Vault secret + restart backplane |
| `HTTP 404: project not found` | Project name typo or project in different tenant | `meho harbor project list` to confirm name |
| `HTTP 403: forbidden` on robot create | Service account lacks robot-management permission | Use Harbor admin or a system-level robot |
| `op_id unknown_op` on dispatch | Op id typo, or a backplane build predating #2856 | Check the op id table above; the typed read core registers at startup, so confirm the backplane ships it (no ingest/curation to run) |
| Artifact list returns a JSONFlux handle | The list has more than 50 rows or 4 KB | Use the `result_query` meta-tool or `meho operation result-query <handle_id>` to page through the handle |

## Related resources

- [`docs/codebase/connectors-harbor.md`](../codebase/connectors-harbor.md) — engineering reference
- [`backend/src/meho_backplane/connectors/harbor/typed_ops.py`](../../backend/src/meho_backplane/connectors/harbor/typed_ops.py) — typed read-op metadata + registrar
- [`backend/src/meho_backplane/connectors/harbor/typed_reads.py`](../../backend/src/meho_backplane/connectors/harbor/typed_reads.py) — read-op bodies + llm_instructions
- [`backend/src/meho_backplane/connectors/harbor/ops.py`](../../backend/src/meho_backplane/connectors/harbor/ops.py) — typed op registrar (robot create/delete)
- [`cli/internal/cmd/harbor/`](../../cli/internal/cmd/harbor/) — CLI verb tree source
- [Harbor 2.11 API reference](https://goharbor.io/docs/2.11.0/build-customize-contribute/configure-swagger/) — upstream REST spec
