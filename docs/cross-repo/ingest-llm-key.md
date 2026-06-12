<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `ANTHROPIC_API_KEY` on a deployed backplane — ingest grouping on-ramp

> Operator-facing deploy-config runbook for the spec-ingestion grouping
> pass. The cookbook for *running* an ingest is
> [`connector-ingestion.md`](connector-ingestion.md); the symbol-level
> map is [`docs/codebase/spec-ingestion.md`](../codebase/spec-ingestion.md).
> **This doc is the one-page answer to "why does `meho connector ingest`
> 503 on my live deploy, and what env do I set to fix it?"**

## Why this exists

`meho connector ingest --catalog <product>/<version>` (and the
explicit `--spec <uri>` form) runs an LLM **grouping pass** that
proposes 8–15 operation groups before any operation becomes
dispatchable. As of [#1386](https://github.com/evoila/meho/issues/1386)
the chassis wires a production LLM client for that pass at FastAPI
lifespan startup — [`build_anthropic_ingest_llm_client`](../../backend/src/meho_backplane/operations/ingest/anthropic_client.py),
installed via [`set_llm_client_factory`](../../backend/src/meho_backplane/api/v1/connectors_ingest.py)
from [`_wire_ingest_llm_client()`](../../backend/src/meho_backplane/main.py)
inside [`_run_lifespan_startup()`](../../backend/src/meho_backplane/main.py).
It reuses `settings.anthropic_api_key` — **the same key the agent
runtime reads** ([`settings.py`](../../backend/src/meho_backplane/settings.py),
`ANTHROPIC_API_KEY` env).

The operational trap this doc closes: the key is wired into the
backplane Deployment **only when `agent.enabled: true`** (default
`false`). A deploy that left the agent runtime off — because it only
wanted the read/governance plane — now *also* has no key for the
ingest grouping pass. The symptom is identical to the recurring
v0.8.x → v0.9.0 finding: a non-dry-run ingest 503s on the grouping
step even though the operator never intended to "turn on agents". The
factory is **fail-closed**: it installs unconditionally at startup (no
crash on a keyless boot) and only raises when an ingest actually runs
the grouping pass with no key configured.

## What "no key" looks like vs. "key set"

| | `ANTHROPIC_API_KEY` **unset / empty** | `ANTHROPIC_API_KEY` **set** |
|---|---|---|
| Startup | Backplane boots normally — the factory is *installed* but not *called* | Same |
| `--dry-run` ingest | Works (parses + plans, **no** LLM call) | Works |
| Non-dry-run ingest, un-grouped connector | **HTTP 503 `LlmClientUnavailable`** on the grouping step; CLI prints the structured error; MCP tool returns its error envelope | Grouping completes; connector lands `review_status='staged'` with groups |
| Net effect | Typed/generic connectors stay "registered, zero dispatchable ops" | Connector is reviewable → enablable → dispatchable |

The 503 envelope text names the requirement explicitly — it tells the
operator to set `ANTHROPIC_API_KEY` (or accept build-time-only
grouping on an on-prem-routed agent runtime). The mapping lives at the
ingest route's `except LlmClientUnavailable` →
`HTTP_503_SERVICE_UNAVAILABLE` handler in
[`api/v1/connectors_ingest.py`](../../backend/src/meho_backplane/api/v1/connectors_ingest.py).

> **No silent degrade.** A keyless deploy never *quietly* groups
> nothing — it fails closed with an operator-visible 503 + named
> remediation. That is the intended posture (external-API key as
> explicit deploy config); see [Out of scope](#out-of-scope-build-time-only-fallback)
> for the air-gapped follow-up.

## Wire the key into the deploy

The chart wires `ANTHROPIC_API_KEY` as a `secretKeyRef` env var (never
a plaintext chart value), and the env block renders **only under
`agent.enabled: true`** — see the gated block in
[`deploy/charts/meho/templates/deployment.yaml`](../../deploy/charts/meho/templates/deployment.yaml)
and the `agent.*` knobs in
[`deploy/charts/meho/values.yaml`](../../deploy/charts/meho/values.yaml).
There are two supported provisioning paths; pick one.

### Path A — operator-managed Kubernetes Secret

Bring your own Secret, reference it by name + key:

```yaml
# values.yaml overlay
agent:
  enabled: true            # renders the ANTHROPIC_API_KEY env wiring
  secretName: meho-anthropic
  secretKey: api_key       # default; key within secretName holding the value
```

```bash
kubectl create secret generic meho-anthropic \
  --namespace <meho-namespace> \
  --from-literal=api_key="sk-ant-..."
```

### Path B — ExternalSecrets Operator (ESO) from Vault

Let ESO materialise the Secret from Vault (mirrors the
Keycloak-client-secret precedent):

```yaml
# values.yaml overlay
agent:
  enabled: true
eso:
  enabled: true
  agent:
    enabled: true
    # remoteKey default: "<vault.paths.kv>/agent"; remoteProperty default "api_key"
```

The chart renders an `ExternalSecret` named `<fullname>-agent` that
pulls the key from Vault into the Secret the Deployment's
`ANTHROPIC_API_KEY` env references — see the agent block in
[`deploy/charts/meho/templates/externalsecrets.yaml`](../../deploy/charts/meho/templates/externalsecrets.yaml).
The `SecretStore` / `ClusterSecretStore` (with the Vault auth
credentials) is consumer-owned and not rendered by the chart.

> The key the agent runtime already uses **is** the key the ingest
> grouping pass uses — one secret covers both. If your deploy already
> set `agent.enabled: true` to run agents, ingest grouping already has
> its key and no extra wiring is needed.

## Verify on a live deploy

After deploying with the key set, confirm the grouping pass works
end-to-end against a real connector spec. The VCF-family catalog
upstreams are HTML/templated, so the explicit `--spec` URI is the
realistic route for those; `--catalog <product>/<version>` works for
any curated catalog entry (`meho connector catalog list`).

```bash
# 1. Log in to the deployed backplane (writes the session token).
meho login https://<your-meho-host>

# 2. Validate first — --dry-run parses + plans with NO LLM call,
#    so it succeeds even on a keyless deploy. Use it to confirm the
#    spec parses before spending an LLM round-trip.
meho connector ingest \
  --product vmware --version 9.0 --impl vmware-rest \
  --spec <spec-uri> \
  --dry-run --json

# 3. Real ingest — this is the step that calls the grouping LLM.
#    On a keyless deploy this 503s; with ANTHROPIC_API_KEY set it
#    lands the connector staged with groups.
meho connector ingest \
  --product vmware --version 9.0 --impl vmware-rest \
  --spec <spec-uri> \
  --json

# 4. List the staged connector + its groups.
meho connector list --json
meho operation groups vmware-rest-9.0
```

Equivalent REST check for the enabled groups (post-enable):

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://<your-meho-host>/api/v1/operations/groups" | jq
```

`GET /api/v1/operations/groups` is served by
[`api/v1/operations.py`](../../backend/src/meho_backplane/api/v1/operations.py).
A successful grouping pass is the line between "registered, zero
dispatchable ops" and a reviewable connector; from there follow
[`connector-ingestion.md`](connector-ingestion.md) Steps 4–8 (review →
polish → enable → verify).

> **Recording the verification.** A live-deploy run against a real
> Anthropic key produces concrete `group_keys` (the staged
> `operation_group.group_key` values). Append the command you ran plus
> the resulting `group_keys` to the Task thread on
> [#1408](https://github.com/evoila/meho/issues/1408) so the
> deployed-backplane verification is recorded against a real ingest,
> not a fixture stub.

## Out of scope (build-time-only fallback)

The grouping pass talks to the Anthropic Messages API **directly**, not
through the G11.5 per-tenant model resolver (Bedrock / vLLM / VCF
PAIF, egress-aware). A fully air-gapped deploy that routes the agent
runtime to an on-prem backend and configures **no** Anthropic key
therefore still gets the 503 on `--catalog` / `--spec` grouping —
grouping stays a build-time operator action with no per-tenant tier or
egress context today. Routing ingest grouping through the resolver is a
separate, larger change; the framing and rationale live in
[`docs/codebase/spec-ingestion.md` §"LLM-client wiring"](../codebase/spec-ingestion.md#llm-client-wiring).
Until then, the supported keyless path is `--dry-run` (parse + plan,
no grouping) plus the CI fixture's deterministic stub for hermetic
tests.

## References

- **Parent Initiative:** [#1407](https://github.com/evoila/meho/issues/1407)
  — production ingest LLM client wiring. T1 ([#1386](https://github.com/evoila/meho/issues/1386))
  shipped the adapter + lifespan wiring; this doc is T2's operator note.
- **Ingest cookbook:** [`connector-ingestion.md`](connector-ingestion.md)
  — the ingest → review → enable → verify workflow.
- **Internal codebase map:** [`docs/codebase/spec-ingestion.md`](../codebase/spec-ingestion.md)
  — symbol-level map, incl. the [LLM-client wiring](../codebase/spec-ingestion.md#llm-client-wiring) section.
- **Architecture:** [`docs/architecture/spec-ingestion.md`](../architecture/spec-ingestion.md).
- **Adapter source:** [`operations/ingest/anthropic_client.py`](../../backend/src/meho_backplane/operations/ingest/anthropic_client.py).
- **Lifespan wiring:** [`main.py`](../../backend/src/meho_backplane/main.py) (`_wire_ingest_llm_client`).
- **Factory seam + 503 mapping:** [`api/v1/connectors_ingest.py`](../../backend/src/meho_backplane/api/v1/connectors_ingest.py).
- **Chart env wiring:** [`deploy/charts/meho/values.yaml`](../../deploy/charts/meho/values.yaml) (`agent.*`, `eso.agent.*`);
  [`deploy/charts/meho/templates/deployment.yaml`](../../deploy/charts/meho/templates/deployment.yaml).
- **Post-deploy enablement checklist:** [`docs/RELEASING.md` §6a](../RELEASING.md).
