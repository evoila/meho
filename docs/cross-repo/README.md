<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `docs/cross-repo/` — cross-repository coordination specs

Specifications of the contracts `evoila/meho` exchanges with sibling
repositories. Every doc in this directory describes a **handshake** that
crosses a repo boundary: what `evoila/meho` produces, what the consumer
side must provision, and how each side verifies the contract holds.

These docs are upstream-side **trackers**, not the consumer's
implementation. The consumer-side code, secrets, and infrastructure live
in the partner repo. What lives here is the spec the consumer reads to
know what to build, and the verification commands either side can run to
prove the handshake works end-to-end.

## Current handshakes

| Doc | Consumer repo | Surface |
| --- | --- | --- |
| [`rke2-infra-coordination.md`](./rke2-infra-coordination.md) | [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc) | Per-PR ephemeral-cluster smoke + `repository_dispatch` deploy trigger; cluster auth (OIDC > kubeconfig); namespace-scoped RBAC for `meho-ci-*` |
| [`targets-yaml.md`](./targets-yaml.md) | [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc) | `targets.yaml` `rdc-meho` entry — schema, worked example, and health-probe contract for the consumer's connector chassis to manage MEHO as a target (Goal #11 DoD bullet 5) |
| [`vault-provisioning.md`](./vault-provisioning.md) | [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc) | Vault auth method + role + policy + KV mount + **federation-proof test KV path** (`secret/meho/test/federation`) the backplane reads on every authenticated `/api/v1/health` call. The fifth surface is the one most easily missed during provisioning — its absence breaks smoke leg #4 with a misleading "chain broken" diagnostic |
| [`keycloak-tenant-claims.md`](./keycloak-tenant-claims.md) | operator's Keycloak realm (every MEHO deployment has its own) | Realm-side recipe to expose `tenant_id` (group attribute) + `tenant_role` (realm role) as JWT claims the v0.2 backplane requires. v0.2 upgrade prerequisite: without it every authenticated request returns `401 missing_tenant_claim` (Initiative [#222](https://github.com/evoila/meho/issues/222)) |

The consumer-side ticket body the maintainer files when shipping
the consumer-side half of Task #58 is drafted at
[`issue-58-consumer-ticket-body.md`](./issue-58-consumer-ticket-body.md).
That file is **not** a handshake spec — it is a ready-to-file
issue body the maintainer copies into `gh issue create` on the
consumer repo. The handshake spec it tracks lives in
[`targets-yaml.md`](./targets-yaml.md), and the green-counter
contract it depends on lives at
[`docs/acceptance/green-counter.md`](../acceptance/green-counter.md).

## When to add a doc here

A handshake belongs in `docs/cross-repo/` when **all** of the following
are true:

1. The contract is between two distinct GitHub repositories (not two
   directories of one repo).
2. One side produces a stable interface (an event, a workflow trigger, a
   kubeconfig consumer, an OCI artefact) and the other side consumes it.
3. The contract has identifiable acceptance criteria on *both* sides —
   "we send `X`" + "they receive `X` and do `Y`".

If the cross-repo edge is a single comment in code or a single field in
a values file, put the note next to the code instead. This directory is
for the contracts substantial enough to need their own page.

## Operator-facing runbooks (not handshakes — recipes)

`docs/cross-repo/` also hosts the operator runbooks that span the
consumer-MEHO boundary even when there's no protocol contract to spec
— same audience (the operator deploying MEHO), same one-stop landing
shape (prerequisites + step-by-step + rollback), no consumer-side
implementation to track separately:

| Doc | Purpose |
| --- | --- |
| [`audit-query.md`](./audit-query.md) | How to investigate "who did X to Y and when?" via the G8.1 audit query surface — the five `meho audit ...` CLI verbs, common forensic questions, filter semantics, cross-tenant boundary, and aggregate-only audit-on-audit broadcast posture. Companion architecture: [`docs/architecture/audit.md`](../architecture/audit.md). |
| [`broadcast-onboarding.md`](./broadcast-onboarding.md) | How to subscribe to the per-tenant Valkey broadcast stream from `meho status --watch`, an MCP client, or a custom downstream subscriber. |
| [`broadcast-overrides.md`](./broadcast-overrides.md) | How to flip the broadcast detail per-call (`X-Broadcast-Detail: full` header — any operator, per request) and how a tenant admin configures durable `BroadcastOverride` rules via REST / CLI / MCP. Companion to `broadcast-onboarding.md`; covers the G6.3 PII opt-in/opt-out surface plus the `mcp-inspector --cli` verification one-liner. |
| [`connector-ingestion.md`](./connector-ingestion.md) | How to add a new vendor surface to MEHO via the G0.7 spec-ingestion pipeline — `meho connector ingest/review/edit/enable/disable`. Companion architecture: [`docs/architecture/spec-ingestion.md`](../architecture/spec-ingestion.md). |
| [`g07-vsphere-canary.md`](./g07-vsphere-canary.md) | The worked-example canary procedure: ingest the vCenter REST spec, drive the operator workflow, run the 10-query govc-parity benchmark. |
| [`kb-migration.md`](./kb-migration.md) | How to migrate the consumer's `kb/` knowledge corpus into MEHO via the G4.1 surface — `meho kb ingest/search/list/show/add/delete`, the ≥1-month overlap, the G4.3 eval, and the operator-driven retire decision. Companion architecture: [`docs/architecture/kb.md`](../architecture/kb.md). |
| [`mcp-client-setup.md`](./mcp-client-setup.md) | How to wire an MCP client (Claude.ai Custom Connector, MCP Inspector, Cline, Continue) to a running MEHO backplane, plus the Keycloak realm-side audience configuration. |
| [`memory-migration.md`](./memory-migration.md) | How to migrate the operator's laptop-local `~/.claude/projects/<...>/memory/` files into MEHO's server-side memory across the 5 scopes (user / user-tenant / user-target / tenant / target) via the G5.1 surface — `meho remember/recall/forget/list`, the manual migration recipe until G5.3 #375 ships the interactive picker, default-TTL behavior under G5.2 #374, rollback via `meho forget`. Companion architecture: [`docs/architecture/memory.md`](../architecture/memory.md). |
| [`retrieval-retirement.md`](./retrieval-retirement.md) | How to retire the consumer's pre-MEHO retrieval workflows (kb / memory / operations surfaces) using `meho retrieval retire-checklist` — 5-criterion decision matrix, per-surface retire + rollback procedures, `retrieval-migration-blocker` label workflow. Companion automation: [`scripts/setup-retrieval-migration-blocker-label.sh`](../../scripts/setup-retrieval-migration-blocker-label.sh). |
| [`vault-onboarding.md`](./vault-onboarding.md) | How to use the G3.3 `vault-1.x` op surface — the `meho vault kv/sys/auth …` verb tree, target/auth model, the agent meta-tool path, JSONFlux behaviour for `vault.kv.list`, the `credential_read` PII guarantee, and the `_secret-read.sh` / `vault.sh` → `meho vault …` migration table. Companion engineering doc: [`docs/codebase/connectors-vault.md`](../codebase/connectors-vault.md); federation-chain prerequisite: [`vault-provisioning.md`](./vault-provisioning.md). |
| [`reverse-proxy-contract.md`](./reverse-proxy-contract.md) | How to wire the cluster's TLS-terminating Ingress to the backplane so HTTPS→HTTP redirect downgrades don't leak. Covers the `X-Forwarded-Proto` contract on the Ingress side, the `config.forwardedAllowIps` knob on the chart side, recommended values per cluster shape, and the diagnostic walk when redirects come back as `http://`. Fixes Issue [#730](https://github.com/evoila/meho/issues/730) / Signal #3 from the 2026-05-20 RDC dogfood. |

## Related

- `docs/codebase/` — durable internal architecture docs (per area:
  backend, cli, devops). These describe what's inside `evoila/meho`;
  `cross-repo/` describes what crosses out of it.
- `docs/architecture/` — canonical architecture references for shipped
  substrates. The cross-repo runbooks above link to their architecture
  companion when one exists.
- Each handshake doc carries a status table that this README does not
  duplicate — drift between the two would be a bug.
