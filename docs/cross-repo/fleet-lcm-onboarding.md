<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# VCF Fleet LCM (modern) op surface onboarding — operator recipe

> Operator-facing recipe for the **modern** `fleet-lcm-9.0` op surface — the
> VCF 9 Fleet LCM Service (`/v1/*`), its typed read core, the login through
> VCF Operations,
> and the optional wider-breadth spec ingest. The connector implementation
> lives in
> [`backend/src/meho_backplane/connectors/fleet_lcm/`](../../backend/src/meho_backplane/connectors/fleet_lcm/);
> the engineering-facing companion is
> [`docs/codebase/connectors-fleet-lcm.md`](../codebase/connectors-fleet-lcm.md).
> For the **legacy** `fleet-rest-9.0` surface (the vRSLCM-derived `/lcm/*`
> API an 8.x target resolves to) see
> [`docs/cross-repo/vcf-fleet-onboarding.md`](./vcf-fleet-onboarding.md).
> This doc is the cookbook an operator reads when onboarding a VCF Fleet
> **9.x** target.

## What this surface is

`fleet-lcm-9.0` is the **modern** generic implementation of `product=fleet`,
registered under the `(product="fleet", version="9.0", impl_id="fleet-lcm")`
triple. It dispatches the VCF 9 Fleet LCM Service — the successor to vRealize
Suite Lifecycle Manager (vRSLCM) 8.x — at
`https://vcf.broadcom.com/fleet-lcm` → `/v1/*`.

It is the **first real two-implementation case** in the codebase: it coexists
with the legacy typed `fleet-rest` impl, and the two are resolved **per
target by fingerprint** (not by the operator). A VCF Fleet **9.1** or newer
target resolves here (`fleet-lcm` covers `>=9.1,<10.0`); an 8.x or 9.0 target
resolves to `fleet-rest` (`>=8.0,<9.1`), because a 9.0 appliance serves only
the legacy `/lcm/*` API. See the "Dual implementation" section of
[`connectors-vcf-fleet.md`](../codebase/connectors-vcf-fleet.md) for the
resolution matrix.

### Typed read core (live at boot — no ingest needed)

The connector ships a curated **13-op typed read core** (`source_kind='typed'`,
#3047) that registers at backplane startup (`run_typed_op_registrars`) and
dispatches on a **fresh boot with zero catalog ingest**. This is what restores
9.0 fleet read dispatch under the "modern default now" resolver decision
(initiative [#3033](https://github.com/evoila/meho/issues/3033)). The read
core spans five operator-reviewed groups — its `group_key` + `when_to_use`
pairing **is** the curation (there is no separate ingest/curation step to run
for the reads):

| Group (`group_key`) | `op_id` | Reads |
| --- | --- | --- |
| `fleet-lcm-system` | `fleet-lcm.health` | Service liveness (`GET /v1/health`) |
| `fleet-lcm-system` | `fleet-lcm.system.info` | Fleet-wide summary: in-flight upgrade + bundles |
| `fleet-lcm-system` | `fleet-lcm.config.info` | Service configuration (flavor, bound cluster, status) |
| `fleet-lcm-sddc` | `fleet-lcm.sddc-lcm.list` | SDDC lifecycle managers the fleet governs |
| `fleet-lcm-sddc` | `fleet-lcm.sddc-lcm.info` | One SDDC LCM by id |
| `fleet-lcm-components` | `fleet-lcm.component.list` | Deployed components inventory |
| `fleet-lcm-components` | `fleet-lcm.component.info` | One component by id |
| `fleet-lcm-components` | `fleet-lcm.component.status` | One component's runtime status |
| `fleet-lcm-tasks` | `fleet-lcm.task.list` | Async operation tasks |
| `fleet-lcm-tasks` | `fleet-lcm.task.info` | One task by id |
| `fleet-lcm-lifecycle` | `fleet-lcm.upgrade-plan.list` | Planned upgrades |
| `fleet-lcm-lifecycle` | `fleet-lcm.upgrade-plan.info` | One upgrade plan by id |
| `fleet-lcm-lifecycle` | `fleet-lcm.release-version.list` | Targetable release versions |

There is **no dedicated `meho fleet-lcm …` CLI verb tree** — the modern
surface is dispatched through the generic `meho operation …` /
`meho connector …` verbs and the agent meta-tools (below).

## Auth — a token from VCF Operations

The fleet lifecycle API takes a Bearer token. MEHO gets it from VCF
Operations at the first call and keeps it:

1. MEHO logs in to VCF Operations with the `username` and `password` from the
   target's Vault secret (`POST /suite-api/api/auth/token/acquire`).
2. MEHO trades that token for a fleet lifecycle token
   (`POST /suite-api/api/auth/token/exchange`, service key `fleet-lcm`).
3. Every fleet call sends `Authorization: Bearer <token>`.

When the token expires, the fleet API answers `401`. MEHO then logs in once
more and runs the call again. You do not need to do anything.

By default MEHO logs in at the target's own host and port. If the target's
`host` carries the `/fleet-lcm` service path, MEHO leaves that path out of the
login. If VCF Operations has a different address, add it to the secret as
`vcf_ops_url` (see below).

**Fallback: a stored token.** If the secret has a `token` field, MEHO sends
that token as is and does not log in. A stored token expires, so use this
only when the login does not work for you.

## Prerequisites

- **A reachable VCF Fleet 9.x appliance.** The connector derives the base URL
  from `target.host` + `target.port`; the `/fleet-lcm` service base is an
  operator target-configuration concern carried on `host`.
- **A VCF Operations account in Vault.** The connector reads
  `{"username", "password"}` (required) and, when set, `vcf_ops_url` and
  `token` from Vault at `target.secret_ref`, under the **operator's**
  identity (operator-context Vault read).
- **A registered VCF Fleet 9.x target** with `product="fleet"`,
  `auth_model="shared_service_account"`, and a fingerprint that resolves to
  version 9.x (so the resolver picks `fleet-lcm`, not `fleet-rest`).
- **An operator session.** `meho login <backplane-url>` writes the session
  token the CLI reuses.

## Target registration

### `targets.yaml` entry

```yaml
targets:
  - name: fleet-9x
    product: fleet
    host: fleet.example.internal
    port: 443
    secret_ref: fleet-lcm/fleet-9x
    auth_model: shared_service_account
    notes: "VCF Fleet 9.x — resolves to fleet-lcm by fingerprint"
```

```bash
meho targets import fleet-9x.yaml   # add --update to PATCH an existing target
```

Verify the fingerprint resolved to 9.x (so `fleet-lcm` is selected):

```bash
meho targets probe fleet-9x --json | jq '{product, version, reachable}'
# expected: {"product": "fleet", "version": "9.x.y.z" | null, "reachable": true}
```

### Credentials in Vault

The VCF Operations account MEHO logs in with:

```bash
vault kv put secret/fleet-lcm/fleet-9x \
  username="admin" \
  password="<vcf-operations-password>"
```

Only when VCF Operations has a different address than the target — add its
address (`https://host` or `https://host:port`, https only, no path):

```bash
vault kv patch secret/fleet-lcm/fleet-9x \
  vcf_ops_url="https://vcf-ops.example.internal"
```

Fallback only — a stored fleet lifecycle token (MEHO then does not log in):

```bash
vault kv patch secret/fleet-lcm/fleet-9x \
  token="<stored-bearer-token>"
```

`secret_ref` is the **logical** KV-v2 path relative to the mount (e.g.
`fleet-lcm/fleet-9x`), not the API-path-shaped `secret/data/...` form.

## Quick-start — the typed read core

Once the target is registered and its credentials are in Vault, every read
core op works end to end with zero ingest:

```bash
# Is the fleet up?
meho operation call fleet-lcm-9.0 fleet-lcm.health --target fleet-9x

# What SDDCs does this fleet manage?
meho operation call fleet-lcm-9.0 fleet-lcm.sddc-lcm.list --target fleet-9x

# What is deployed, and is it running?
meho operation call fleet-lcm-9.0 fleet-lcm.component.list --target fleet-9x

# What can we upgrade to?
meho operation call fleet-lcm-9.0 fleet-lcm.release-version.list --target fleet-9x

# Discover ops by intent (hybrid BM25 + cosine search)
meho operation search fleet-lcm-9.0 "what upgrades are planned"
meho operation groups fleet-lcm-9.0
```

## Optional — ingest the wider `/v1/*` breadth

The typed read core is the curated, always-on read surface. The **full 51-op**
`/v1/*` surface — the wider read breadth plus the component / upgrade-plan /
task **writes** — is **not** code-shipped; it is enabled operationally by
ingesting the pinned `fleet-lcm-openapi.yaml` through the generic G0.7
pipeline and reviewing the operator-facing groups. Do this only when the read
core is insufficient (e.g. you need a write op or a niche read).

Follow the connector-agnostic runbook in
[`docs/cross-repo/connector-ingestion.md`](./connector-ingestion.md); the
fleet-lcm specifics are:

```bash
# 1. Ingest the pinned spec under the SAME connector id (fleet-lcm-9.0). The
#    ingested rows are source_kind="ingested" and ride the already-registered
#    FleetLcmConnector.auth_headers (the Bearer token), so no auth wiring is needed.
meho connector ingest \
  --product fleet --version 9.0 --impl fleet-lcm \
  --spec docs:fleet-lcm-9.0/fleet-lcm-openapi.yaml \
  --json

# 2. Review the LLM-summarised, operator-reviewable groups the ingest derived.
meho connector review fleet-lcm-9.0

# 3. Enable the ingested read breadth (writes stay default-deny until an
#    operator enables the specific group). enable_reads flips every GET/HEAD
#    ingested op to is_enabled=True.
meho connector enable-reads fleet-lcm-9.0
```

The read core's typed ops and the ingested breadth coexist under one
connector id: the typed ops keep their curated `group_key`s
(`fleet-lcm-system`, …), and the ingested ops land in the groups
`run_llm_grouping` derives during the ingest, which the operator reviews and
enables per group. Reconciling ingested rows against the spec they were
ingested from is tautological, so only the hand-coded typed paths are
guarded by the spec-reconcile lane.

## Agent meta-tool path

Agents use `search_operations` / `call_operation` with
`connector_id="fleet-lcm-9.0"`; the connector is never a per-op MCP tool
(CLAUDE.md postulate 5). A typical sequence:

1. `list_operation_groups(connector_id="fleet-lcm-9.0")` → the five read-core
   groups (plus any enabled ingested groups) with their `when_to_use` hints.
2. `search_operations(connector_id="fleet-lcm-9.0", query="what is deployed", group="fleet-lcm-components")`
   → `fleet-lcm.component.list` / `.info` hits.
3. `call_operation(connector_id="fleet-lcm-9.0", op_id="fleet-lcm.component.list", target="fleet-9x")`
   → the component inventory (reduced to a JSONFlux handle when large).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| A 9.x target dispatches `/lcm/*` legacy ops | Fingerprint resolved to 8.x → `fleet-rest` | `meho targets probe fleet-9x` — confirm version 9.x so `fleet-lcm` is selected |
| `VaultCredentialsReadError` naming the target on any read | The Vault secret is missing `username`/`password`, or `vcf_ops_url` is not a plain https address | Fix the secret (`vault kv put …`); a token-only secret is rejected |
| `connector_auth_failed` naming `token/acquire` | VCF Operations refused the username or password | Fix the account in the secret; the next call reads Vault again |
| `connector_error` naming `token/acquire` with HTTP 404 | The login went to an address that is not VCF Operations | Add `vcf_ops_url` with the VCF Operations address |
| `connector_auth_failed` naming `token/exchange` | VCF Operations refused to hand out a fleet lifecycle token for this account | Give the account access to fleet management in VCF Operations |
| `op_id unknown_op` for a write op | Writes are the ingested-breadth follow-up, not shipped in the read core | Ingest + enable the breadth (above), or use a read core op |
| `connector_auth_failed` after a re-login on fleet calls | The fleet API refused even a fresh token | Check the account's fleet roles in VCF Operations; as a last resort store a `token` |

## Related resources

- [`docs/codebase/connectors-fleet-lcm.md`](../codebase/connectors-fleet-lcm.md) — engineering reference
- [`backend/src/meho_backplane/connectors/fleet_lcm/typed_ops.py`](../../backend/src/meho_backplane/connectors/fleet_lcm/typed_ops.py) — typed read-op metadata + registrar
- [`backend/src/meho_backplane/connectors/fleet_lcm/session.py`](../../backend/src/meho_backplane/connectors/fleet_lcm/session.py) — the credential loader and the VCF Operations login
- [`docs/cross-repo/connector-ingestion.md`](./connector-ingestion.md) — the connector-agnostic ingest runbook
- [`docs/cross-repo/vcf-fleet-onboarding.md`](./vcf-fleet-onboarding.md) — the legacy `fleet-rest` (8.x) surface
- Task: [#3047](https://github.com/evoila/meho/issues/3047); parent initiative [#3033](https://github.com/evoila/meho/issues/3033)
