<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Adding a new vendor surface to MEHO — operator runbook

> Operator-facing runbook for the G0.7 spec-ingestion pipeline. The architecture sits in [`docs/architecture/spec-ingestion.md`](../architecture/spec-ingestion.md); this doc is the cookbook every operator reads when standing up a new vendor connector from an OpenAPI spec.

## When to use this

You want operators on a tenant to dispatch operations against a vendor API that isn't yet a MEHO connector, and the vendor publishes an OpenAPI 3.0/3.1 spec. The ingestion pipeline parses the spec, computes embeddings, asks an LLM to propose 8–15 operation groups with `when_to_use` hints, and stages the result for your review before any operation becomes dispatchable.

**Use the typed-connector runbook instead** when:

- The vendor publishes no usable OpenAPI spec (SSH-only, raw SOAP, proto/gRPC).
- The spec is published but materially incomplete (legacy `pyvmomi`-only ops).
- A higher-level composite is the right agent UX (e.g. "create a VM end-to-end with networking attached").

Typed connectors and ingested connectors are both first-class ([CLAUDE.md](../../CLAUDE.md) postulate 1) — same `endpoint_descriptor` table, same dispatcher, same agent meta-tools. The choice between them is driven by spec availability, not by preference.

## Prerequisites

- **Role.** Write verbs (`ingest`, `edit-group`, `edit-op`, `enable`, `disable`) require `tenant_admin`. Read verbs (`list`, `review`) require `operator`. The backplane returns HTTP 403 with the CLI exit code 5 if the wrong role tries a write verb.
- **A running backplane.** `meho login <backplane-url>` writes a session token the CLI reuses across every verb. Override per-call with `--backplane <url>` when needed.
- **The OpenAPI spec.** A path to a local file (`file:///abs/path/spec.yaml`) or an HTTPS URL the backplane can fetch (`https://vendor.example.com/openapi.yaml`). The CLI also accepts the `docs:<product>-<version>/<spec>.yaml` shorthand that resolves against `$CLAUDE_RDC_DOCS` when set; otherwise the shorthand is passed through for the backplane to resolve against its own checked-in docs corpus.
- **`ANTHROPIC_API_KEY` set for the grouping pass.** As of #1386 the chassis wires a production `LlmClient` at FastAPI lifespan startup: [`build_anthropic_ingest_llm_client`](../../backend/src/meho_backplane/operations/ingest/anthropic_client.py) is installed via [`set_llm_client_factory`](../../backend/src/meho_backplane/api/v1/connectors_ingest.py) and reuses `settings.anthropic_api_key` (the same key the agent runtime reads), talking to the Anthropic Messages API directly. So a deploy with the key set groups non-dry-run ingests for real across all three surfaces (REST route, CLI, and the `meho.connector.ingest` MCP tool — they all read the lifespan-wired factory). A deploy that configured **no key** keeps the fail-closed posture: the ingest REST route returns HTTP 503 `LlmClientUnavailable` and the CLI prints the structured error. (Tests inject a deterministic stub via the constructor's `llm_client_factory=` argument.) See [`ingest-llm-key.md`](ingest-llm-key.md) for the deployed-backplane on-ramp — where the key goes in the Helm chart (it renders only under `agent.enabled: true`), how to verify on a live deploy, and the no-key 503 symptom — and [`docs/codebase/spec-ingestion.md` §"LLM-client wiring"](../codebase/spec-ingestion.md#llm-client-wiring) for the symbol-level framing, including the air-gapped/resolver-routing follow-up.
- **Postgres with pgvector + FTS extensions.** v0.2 ships the `pgvector/pgvector:pg16`-derived chart image; local development uses the testcontainers fixture.

## Step-by-step

The workflow walks the [five-step pipeline](../architecture/spec-ingestion.md#the-five-step-pipeline) in operator-visible order: ingest → review → polish → enable → verify.

### Step 1 — find the spec

> For the shipped connectors, the curated
> [connector-spec catalog](connector-catalog.md) already records the
> recommended spec source(s) + required connector class per
> `(product, version)`. Start there before hunting for a URL.

Vendor specs live in one of three places, in priority order:

1. **The consumer's checked-in docs corpus** (e.g. `claude-rdc-hetzner-dc/docs/<product>-<version>/`). The maintainer's local clone is the conventional source. Use the `docs:<product>-<version>/<spec>.yaml` shorthand when this applies.
2. **The vendor's published URL** (e.g. `https://developer.vmware.com/.../vcenter-rest-9.0.yaml`). Use a full `https://` URL.
3. **A local download.** Use a full `file:///` path.

All three sources assume the vendor *publishes* an OpenAPI document somewhere. If it doesn't, jump to the next section before concluding the connector is un-ingestable.

#### Product publishes no OpenAPI spec

Some products ship **no** OpenAPI document at all — only HTML/Markdown reference docs (Hetzner Robot), or a proprietary management API with no published spec (VCF Fleet / vRSLCM `/lcm/`). For these, `meho connector ingest` is **not** a dead end, and the catalog-miss `next_step` hint on a `state=registered`, 0-operation connector points you here.

The ingest pipeline only ever sees the *bytes* of an OpenAPI 3.x document — it does not care whether a vendor published those bytes or you typed them by hand. So the supported on-ramp is:

1. **Author a minimal OpenAPI 3.x** covering just the operations you need today. You do **not** have to model the vendor's entire API — a handful of ops is enough to unblock an agent workflow, and you can extend the spec and re-ingest later (re-ingest is idempotent on unchanged rows).
2. **Save it locally** and ingest it with a `file://` URI, exactly as you would a downloaded spec:

   ```bash
   meho connector ingest \
     --product hetzner --version 1.0 --impl hetzner-rest \
     --spec file:///abs/path/hetzner-robot.yaml \
     --dry-run
   ```

A minimal worked example for a spec-less product (two ops — list servers, reset one):

```yaml
# hetzner-robot.yaml
openapi: 3.0.3
info:
  title: Hetzner Robot (hand-authored)
  version: "1.0"
paths:
  /server:
    get:
      summary: List dedicated servers
      operationId: listServers
      tags: [server]
      responses:
        "200":
          description: A list of servers.
          content:
            application/json:
              schema:
                type: array
                items:
                  type: object
  /reset/{server_ip}:
    post:
      summary: Trigger a hardware reset
      operationId: resetServer
      tags: [server]
      parameters:
        - name: server_ip
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Reset accepted.
```

`--dry-run` against this file prints `operations: 2 total (2 inserted …)`; drop `--dry-run` to ingest for real, then continue from [Step 2](#step-2--ingest-the-spec). The parser applies the **same** safety heuristic a downloaded spec gets (`GET` → `safe`, `POST` → `caution`, `DELETE` → `dangerous`), so review the staged groups (Step 4) and mark per-op overrides (Step 6) the same way.

Authoring tips:

- Use the verbs and paths the vendor's HTML/Markdown docs already describe, so the ingested `op_id` (e.g. `POST:/reset/{server_ip}`) reads naturally at the agent surface.
- Give each operation a `summary` and `description` — these feed the LLM grouping pass and the agent's `when_to_use` retrieval, so a one-line `summary` is worth more than a bare path.
- A bad path-param shape or a malformed `$ref` surfaces under `--dry-run` before any DB write; iterate on the file until the dry-run op count matches what you intended.

> **Why not auto-derive the spec?** Parsing vendor HTML/Markdown into OpenAPI, or deriving ops from a typed client, is a deliberately **deferred** capability ([initiative #1529 out-of-scope](https://github.com/evoila/meho/issues/1529)). A small hand-authored spec is the cheaper answer than an HTML→OpenAPI inference engine — author the ops you need, not the vendor's whole surface. Conform to [OpenAPI 3.0.3](https://spec.openapis.org/oas/v3.0.3.html) or [3.1.1](https://spec.openapis.org/oas/v3.1.1.html); the parser rejects Swagger 2.0 with a conversion-path remedy (see [`connector-catalog.md`](connector-catalog.md)).

Validate the spec before committing to a tenant-wide ingestion:

```bash
meho connector ingest \
  --product <product> --version <version> --impl <impl> \
  --spec <spec-uri> \
  --dry-run
```

`--dry-run` parses each spec, surfaces parser-rejected `$ref` shapes, and returns the bulk-upsert plan **without** writing to the DB or making any LLM calls. The CLI prints inserted/updated/skipped counts so you can sanity-check that the parser found roughly the operation count the vendor advertises.

### Step 2 — ingest the spec

```bash
meho connector ingest \
  --product vmware --version 9.0 --impl vmware-rest \
  --spec docs:vcenter-9.0/vcenter.yaml \
  --json
```

The connector lands `review_status='staged'` with every operation `is_enabled=false`. The expected human-readable output:

```
ingest vmware/9.0/vmware-rest — connector_id=vmware-rest-9.0
  operations: 1275 total (1275 inserted / 0 updated / 0 skipped)
  connector_registered: true (first ingest of this triple flips it to true)
  operations_grouped: true
  grouping: 8 groups, 1100 ops assigned, 175 unassigned (27 LLM call(s), 45000ms)

Connector is in review_status=staged. Next:
  meho connector review vmware-rest-9.0
  meho connector enable vmware-rest-9.0 --confirm
```

The LLM-call cost follows the formula `1 + ceil(op_count / batch_size)` (1 Pass-1 + N Pass-2 batches, default `batch_size=50`).

### Step 3 — multi-spec ingest

When one product publishes multiple specs that need to land under the same connector triple (vSphere's `vcenter.yaml` + `vi-json.yaml` is the canonical case), repeat `--spec`:

```bash
meho connector ingest \
  --product vmware --version 9.0 --impl vmware-rest \
  --spec docs:vcenter-9.0/vcenter.yaml \
  --spec docs:vcenter-9.0/vi-json.yaml \
  --json
```

Every row from the first spec carries a `spec:vcenter.yaml` tag; every row from the second carries `spec:vi-json.yaml`. The review payload (Step 4) renders the source tag so you can audit per spec.

> **Caveat — vi-json blocked on a T1 parser extension.** The T1 parser currently rejects `$ref: "#/components/parameters/moId"`, which appears on every `vi-json.yaml` operation. The vSphere canary ([#408](https://github.com/evoila/meho/issues/408)) ships with `vcenter.yaml` only. Multi-spec ingest works end-to-end the moment the parser gains a `#/components/parameters/*` resolver (see [Known gaps](#known-gaps) below). Until then, run the second spec only against a parser branch that resolves non-schema component refs.

### Step 4 — review the LLM-summarised groups

```bash
meho connector review vmware-rest-9.0
```

The review payload renders:

- Every `operation_group` row (`group_key`, `name`, `when_to_use`, `review_status`).
- Per-group operation count.
- Per-op flags (`safety_level`, `requires_approval`, `is_enabled`, the parser-set vs operator-set distinction for each).
- The `spec:<source>` tag distribution when multi-spec.

Inspect each group's `when_to_use` carefully — this is the verbatim text the agent reads before deciding which group to search within. A `when_to_use` like "Use these operations for VM lifecycle workflows: list, inspect, power on/off, clone, snapshot, migrate" is actionable; "Operations related to virtual machines" is not.

Use `--json` for machine-readable output suited to scripted post-processing (e.g. diffing the LLM's groups against a hand-curated reference).

### Step 5 — polish weak group hints

```bash
meho connector edit-group vmware-rest-9.0 vm \
  --when-to-use "Use these operations for any virtual-machine workflow: list, inspect, power on/off, clone, snapshot, migrate, or otherwise manage a VM."
```

Inline text up to ~2 KB works directly on the command line. For longer prose, use the `@<path>` form:

```bash
meho connector edit-group vmware-rest-9.0 storage --when-to-use @/tmp/storage_when_to_use.md
```

`edit-group` also accepts `--name` to override the LLM's group display name. Each edit writes a `meho.connector.edit_group` audit row in the same transaction as the column update so [G8 audit replay](https://github.com/evoila/meho/issues/218) can reconstruct exactly which operator polished which group at which time.

Most operators polish 2–4 groups per ingest; the LLM's output is usable for the rest.

### Step 6 — mark per-op safety overrides

The parser defaults `DELETE` verbs to `safety_level='dangerous'` but leaves `requires_approval=false`. Flip `requires_approval` on any op whose execution should block on the approval queue:

```bash
meho connector edit-op vmware-rest-9.0 'DELETE:/vcenter/vm/{vm}' \
  --safety dangerous --requires-approval
```

Other per-op overrides:

```bash
# Replace the agent-facing description (operator wins over vendor prose).
meho connector edit-op vmware-rest-9.0 'POST:/vcenter/vm/{vm}/power?action=start' \
  --custom-description @/tmp/vm_power_on_description.md

# Re-enable a single op the connector was disabled with.
meho connector edit-op vmware-rest-9.0 'GET:/vcenter/cluster' --enable

# Disable a single op without disabling the whole connector.
meho connector edit-op vmware-rest-9.0 'POST:/vcenter/legacy/deprecated-thing' --disable
```

`edit-op` requires at least one of `--custom-description`, `--safety`, `--requires-approval`, `--no-requires-approval`, `--enable`, `--disable`. An empty PATCH yields HTTP 400.

Each edit writes a single `meho.connector.edit_op` audit row.

### Step 7 — enable the connector

```bash
meho connector enable vmware-rest-9.0 --confirm
```

`enable` cascades every group to `review_status='enabled'` and every op to `is_enabled=true`. After this step:

- The connector appears in `search_connectors` for every operator in the tenant.
- Operations become dispatchable through `call_operation`.
- The agent's `search_operations(connector_id="vmware-rest-9.0", query=...)` returns ranked results from the enabled rows.

Without `--confirm`, the CLI prompts on stdin for `y/yes` — `--confirm` skips the prompt for scripted use (CI pipelines, etc.).

### Step 8 — verify the agent path

```bash
meho operation groups vmware-rest-9.0
meho operation search vmware-rest-9.0 "list virtual machines" --limit 10
```

The first command returns the 8–15 enabled groups with their `when_to_use` hints. The second returns ranked hits — the load-bearing acceptance bar from the vSphere canary is "the canonical operation appears in the top-3". See the canary's [10 (query, canonical-op) pairs](g07-vsphere-canary.md#test-variant) for the working examples.

To verify dispatch end-to-end against a real target:

```bash
meho operation call vmware-rest-9.0 'GET:/vcenter/cluster' \
  --target rdc-vcenter --json
```

The `--target` argument resolves against the tenant's [Target rows](targets-yaml.md); the call needs a reachable vCenter endpoint (a `vcsim` simulator suffices for read operations).

## Rollback / disable

```bash
meho connector disable vmware-rest-9.0 --confirm
```

`disable` flips every group to `review_status='disabled'` and every op to `is_enabled=false`. The agent meta-tools stop surfacing the connector immediately; no in-flight dispatches will route to it.

**Per-op operator overrides are preserved** (`custom_description`, `safety_level`, `requires_approval`) so a future `enable` resurfaces them verbatim. There is no `delete` verb — the rollback is reversible without losing operator work.

The audit trail (`meho.connector.disable` row + the prior `meho.connector.enable` row) is sufficient to reconstruct what happened.

After fix → re-run `meho connector ingest` against the corrected spec. T2's body-hash idempotence skips re-embedding rows whose parser output didn't change; only the changed rows take an embedding hit. After `review` + `enable`, the agent path re-warms.

## Common operator gotchas

### `op_id` carries `:` and `/`

Operation natural keys for ingested rows look like `"GET:/api/vcenter/cluster"` — a method, a colon, and the OpenAPI path. The colon and slashes are part of the key.

**In CLI commands**, quote the `op_id` to keep the shell from interpreting characters:

```bash
meho connector edit-op vmware-rest-9.0 'DELETE:/vcenter/vm/{vm}' --safety dangerous
```

**In REST calls**, the route uses FastAPI's `:path` converter so the full segment round-trips intact:

```bash
curl -X PATCH \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"safety_level": "dangerous"}' \
  "https://meho.example.com/api/v1/connectors/vmware-rest-9.0/operations/DELETE:/vcenter/vm/{vm}"
```

URL-encoding the colon as `%3A` works but isn't required.

### Multi-spec collisions

If two specs publish the same `(method, path)` under the same connector triple, T2's per-row cross-call check raises `OpIdCollision` with both spec sources named:

```
OpIdCollision: 'GET:/vcenter/vm' already registered with spec_source='vcenter.yaml'; this call uses spec_source='vi-json.yaml'
```

The operator decides which spec wins. Re-running `ingest` with only the chosen spec for that op is one path; bumping `impl_id` to keep both as separate connectors is another.

### Dry-run before committing

Always pass `--dry-run` on the first ingest of a new spec. It surfaces parser-rejected `$ref` shapes, OpenAPI-version rejections, and per-spec op counts before any DB write or LLM call. Production ingests where the operator skipped `--dry-run` and the parser rejected the spec halfway through have walked back through `meho connector disable` more often than they should.

### Tenant scope is JWT-derived

There is no `--tenant` flag. The tenant the connector lands under derives from the JWT's `tenant_id` claim; cross-tenant probes surface as HTTP 404 `ConnectorNotFoundError`, not 403. Use a different login (different tenant_admin operator) to drive ingestion in a different tenant.

### `--confirm` is required for `enable` and `disable`

Both state-transition verbs prompt on stdin by default. CI pipelines and scripted operators pass `--confirm` to skip the prompt. The prompt is intentional — these verbs change every operator's view of the connector, and the consequence of an accidental enable on a partially-reviewed connector is dispatching against operations whose `safety_level` and `requires_approval` haven't been audited.

### JSON output for everything

Every read verb (`list`, `review`) accepts `--json`. Every write verb supporting `--json` emits the canonical response shape so scripted post-processing has a stable contract. Without `--json`, the CLI renders a human-readable summary.

### `--backplane` overrides `meho login`

By default the CLI reads the backplane URL from the most recent `meho login` session file. Pass `--backplane https://other.example.com` to point one call at a different deploy without re-logging-in.

## RBAC notes

The pipeline ships two roles. Both are realm roles on the Keycloak side; the chassis maps the realm role onto the `TenantRole` Python enum via [`auth.rbac`](../../backend/src/meho_backplane/auth/rbac.py).

| Role | Sees | Can do |
|---|---|---|
| `operator` | `list_connectors`, `review_payload` for connectors in their tenant | Read-only. Cannot ingest, edit, enable, or disable. |
| `tenant_admin` | Everything `operator` sees, plus every administrative MCP tool under `meho.connector.*` | Full ingest → review → enable → disable workflow. |

Read paths require `operator`; write paths require `tenant_admin`. The REST router enforces this via FastAPI's `Depends(require_role(...))`; the admin MCP tools enforce it via the registry's `required_role` filter on `tools/list` plus a second-pass check in `handle_tools_call` so a client that guesses a hidden tool name is still rejected.

**No cross-tenant write access.** The JWT carries exactly one `tenant_id`; the tenant the operator's session is bound to is the tenant the write lands under. A platform admin needing to drive ingestion across multiple tenants logs in once per tenant.

## Known gaps

Documented inline pending formal Task filing. The vSphere canary (see [g07-vsphere-canary.md](g07-vsphere-canary.md)) is the test bed that surfaced each.

### Gap 1 — T1 `$ref` extension for `vi-json.yaml`

**Status:** documented; no Task filed yet.

The T1 parser rejects `$ref: "#/components/parameters/<name>"`. vSphere's `vi-json.yaml` references `#/components/parameters/moId` on every operation (~2,195 operations). The fix is small (~40 LoC in [`refs.py::_resolve_shallow_ref`](../../backend/src/meho_backplane/operations/ingest/refs.py) plus tests for the three additional ref shapes: `parameters`, `requestBodies`, `headers`) but the work belongs in T1's scope, not in T9's docs work.

**File a Task** when an early operator needs vi-json coverage — e.g. when a govc-style workflow that fundamentally needs Performance Manager (`govc events`), snapshot revert (`govc snapshot.revert`), or host network atomic mutation (`govc host.evac`) becomes a blocker.

### Gap 2 — T3 per-op `llm_instructions` enhancement

**Status:** documented; no Task filed yet.

The vSphere canary's 10-query benchmark currently `xfail`s on 3 queries:

- `list virtual machines`
- `power on virtual machine`
- `power off virtual machine`

The driver is description quality, not pipeline correctness. The vCenter spec's cardinal-op descriptions ("Vcenter.VM.FilterSpec", "Powers on a powered-off or suspended virtual machine") under-rank against sub-path operations that score higher on the BM25 + cosine fusion. T3's grouping pass produces per-group `when_to_use` hints but does NOT yet generate per-op `llm_instructions` or rewrite `summary`.

Resolution path: extend T3 with a per-op `llm_instructions` generation pass that produces operator-friendly prose from the spec body, then re-bench. The `endpoint_descriptor.llm_instructions` column already exists ([G0.6-T1 #392](https://github.com/evoila/meho/issues/392)) so no schema work is needed.

**File a Task** when the v0.2 retrieval-quality bar gates a downstream consumer.

### Gap 3 — live-LLM canary validation

**Status:** the production `LlmClient` is wired at FastAPI lifespan startup (#1386 — `build_anthropic_ingest_llm_client`, reusing `settings.anthropic_api_key`), so the live-LLM canary can run on any deploy / CI runner with `ANTHROPIC_API_KEY` set. The previously-cited [`Task #467`](https://github.com/evoila/meho/issues/467) was [G8.1-T3 audit CLI verbs (CLOSED)](https://github.com/evoila/meho/issues/467), never the chassis adapter. See [`docs/codebase/spec-ingestion.md` §"LLM-client wiring"](../codebase/spec-ingestion.md#llm-client-wiring) for the operator-facing framing.

The canary's acceptance test ships a deterministic stub-LLM that classifies ops by URL-path prefix. The stub keeps the test reproducible and fast but doesn't exercise a live Anthropic adapter's prompt-engineering or retry-policy edges.

A live-LLM variant exists at `MEHO_G07_CANARY_LIVE_LLM=1`. It skips only when `ANTHROPIC_API_KEY` is absent; with the key set the lifespan-wired `build_anthropic_ingest_llm_client` (#1386) drives the real grouping pass. To exercise it:

1. Re-run the canary with the env var set (and `ANTHROPIC_API_KEY` present).
2. Pick harder queries the path-prefix stub can't trivially classify — snapshot revert, performance-metrics query, host-network atomic mutation.
3. Compare top-3 hit rate against the stub baseline to verify the live LLM doesn't regress retrieval quality.

Routing the grouping pass through the G11.5 per-tenant model resolver (so air-gapped/no-Anthropic-key deploys can group too) is the remaining follow-up — see [`docs/codebase/spec-ingestion.md` §"LLM-client wiring"](../codebase/spec-ingestion.md#llm-client-wiring).

## References

- **Architecture doc:** [`docs/architecture/spec-ingestion.md`](../architecture/spec-ingestion.md) — the canonical reference for the pipeline's design.
- **Sister architecture doc:** [`docs/architecture/operations-substrate.md`](../architecture/operations-substrate.md) — G0.6's substrate this pipeline writes into.
- **vSphere canary runbook:** [`g07-vsphere-canary.md`](g07-vsphere-canary.md) — the worked example operators reproduce locally.
- **Internal codebase map:** [`docs/codebase/spec-ingestion.md`](../codebase/spec-ingestion.md) — symbol-level map of `backend/src/meho_backplane/operations/ingest/`.
- **CLAUDE.md postulates:** postulate 1 (two connector kinds, both first-class); postulate 4 (operation grouping + LLM hints, operator-reviewable); postulate 5 (agent surface is meta-tools).
- **Parent Initiative:** [#389 G0.7 Spec ingestion pipeline](https://github.com/evoila/meho/issues/389).
- **Parent Goal:** [#221 G0 foundational substrate](https://github.com/evoila/meho/issues/221).
- **CLI source:** [`cli/internal/cmd/connector/`](../../cli/internal/cmd/connector/).
- **Backend source:** [`backend/src/meho_backplane/operations/ingest/`](../../backend/src/meho_backplane/operations/ingest/).
- **REST router:** [`backend/src/meho_backplane/api/v1/connectors_ingest.py`](../../backend/src/meho_backplane/api/v1/connectors_ingest.py).
- **Admin MCP tools:** [`backend/src/meho_backplane/mcp/tools/connector_admin.py`](../../backend/src/meho_backplane/mcp/tools/connector_admin.py).
- **OpenAPI specs:** [3.0.3](https://spec.openapis.org/oas/v3.0.3.html); [3.1.1](https://spec.openapis.org/oas/v3.1.1.html).
