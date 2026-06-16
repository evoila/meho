<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# VCF Operations for Logs (vRLI) op surface onboarding — operator recipe

> Operator-facing recipe for the G3.6 `vrli-rest-9.0` op surface — the
> `meho vcf-logs ...` verb tree, the agent meta-tool path, and the
> migration off the consumer's `scripts/vcf-logs.sh` wrapper. The
> connector lives in
> [`backend/src/meho_backplane/connectors/vcf_logs/`](../../backend/src/meho_backplane/connectors/vcf_logs/);
> the engineering-facing companion is
> [`docs/codebase/connectors-vcf-logs.md`](../codebase/connectors-vcf-logs.md).
> This doc is the cookbook every RDC operator reads when retiring
> `scripts/vcf-logs.sh` in favour of `meho vcf-logs ...`.

## What this surface is

The `vrli-rest-9.0` connector is an **ingested** connector: the 7 curated
read-only ops are stored as `EndpointDescriptor` rows seeded from
[`VRLI_CORE_OPS`](../../backend/src/meho_backplane/connectors/vcf_logs/core_ops.py)
and dispatched through `HttpConnector._request_json` by the G0.6
`dispatch_ingested` branch. The connector registers under the
`(product="vcf-logs", version="9.0", impl_id="vrli-rest")` registry
triple — the connector id `vrli-rest-9.0`. Auth is **session-token
Bearer** (not HTTP Basic on every request): the connector POSTs
credentials once to `/api/v2/sessions`, caches the returned `sessionId`
per target, and threads it as `Authorization: Bearer <sessionId>` on
every downstream call. A 401 from any downstream GET invalidates the
cached token and triggers a single re-login + retry.

vRLI has no public CI simulator (#536 proved simulators cannot serve
vendor REST). Integration coverage uses recorded-fixture record/replay
against captured vRLI responses ([`backend/tests/test_connectors_vcf_logs_e2e.py`](../../backend/tests/test_connectors_vcf_logs_e2e.py)).

The v0.5 op surface (Initiative
[#369](https://github.com/evoila/meho/issues/369)) is the **read**
working set the consumer's `scripts/vcf-logs.sh` exercises daily —
write ops (alert create / update / delete, content-pack import, query
result export) stay in the wrapper until a later release ships policy
+ approval flow for vRLI:

| Group | CLI verb | `op_id` | Path |
| --- | --- | --- | --- |
| vrli-system | `meho vcf-logs about` | `GET:/api/v2/version` | Appliance version / release name / build |
| vrli-events | `meho vcf-logs query [constraints]` | `GET:/api/v2/events/{constraints}` | Raw event search (headline read surface) |
| vrli-events | `meho vcf-logs aggregated [constraints]` | `GET:/api/v2/aggregated-events/{constraints}` | Group-by aggregation over events |
| vrli-system | `meho vcf-logs field list` | `GET:/api/v2/fields` | Indexer-field catalog (static + extracted) |
| vrli-inventory | `meho vcf-logs host list` | `GET:/api/v2/hosts` | Hosts reporting log events |
| vrli-content | `meho vcf-logs content-pack list` | `GET:/api/v2/content/contentpack/list` | Installed content packs |
| vrli-alerts | `meho vcf-logs alert list` | `GET:/api/v2/alerts` | Configured alert definitions |

Every op dispatches through the same `POST /api/v1/operations/call`
route the agent surface uses — auth, policy, audit, broadcast, and
JSONFlux all run as documented in [CLAUDE.md](../../CLAUDE.md) §6. The
CLI verb tree is operator ergonomics over that one route; it is **not**
a separate data path and is **not** mirrored on the MCP surface
(CLAUDE.md postulate 5).

## Prerequisites

- **A reachable vRLI cluster** (single node or cluster VIP). The
  connector derives the base URL from `target.host` + `target.port`.
- **Service-account credentials in Vault.** The connector reads
  `{"username": ..., "password": ...}` from Vault at `target.secret_ref`.
  The pair is POSTed as JSON to `/api/v2/sessions` along with a
  `provider` field naming the identity-source. v0.5 supports `Local`
  (the default) and `ActiveDirectory`; `vIDM` is documented by the
  appliance but not supported in this release.
- **A registered vRLI target.** The CLI verbs take `--target <slug>`
  (e.g. `--target rdc-vrli`). The target carries `product="vcf-logs"`,
  `host` (the vRLI FQDN — no `https://`), `port` (default 443),
  `secret_ref` (the Vault path to the credentials), and
  `auth_model="shared_service_account"`.
- **The 7 curated ops registered + enabled.** Run the G3.6-T5 curation
  step (`apply_vrli_core_curation`) once per vRLI target after the
  G0.7 spec ingest. The
  [`docs/cross-repo/vcf-fixture-refresh.md`](./vcf-fixture-refresh.md)
  recipe covers the CI-side fixture refresh; the operator workflow
  for enabling the curated core mirrors the NSX precedent
  ([`g35-nsx-canary.md`](./g35-nsx-canary.md)).
- **An operator session.** `meho login <backplane-url>` writes the
  session token the CLI reuses. `meho vcf-logs ...` requires
  `operator` role minimum (same gate as every dispatch verb).

## Target + auth model

The shipped connector's auth model is **`shared_service_account`** —
the Vault-sourced credentials are used regardless of which operator
invokes the verb. Per-operator impersonation is out of scope for v0.5.

What this means for the credentials in Vault:

- Vault path: `target.secret_ref` (e.g. `kv/data/vrli/<slug>`).
- Required fields: `username` (string), `password` (string).
- The backplane reads them lazily on first op invocation per target;
  the session token returned by `POST /api/v2/sessions` is then cached
  in the connector's per-target token cache until a 401 triggers one
  re-login.
- **Session expiry**: vRLI's session TTL defaults to 30 days (the
  appliance reports it via the `ttl` field in the login response) and
  is operator-tunable via the appliance's settings UI. The connector
  does **not** proactively refresh — the 401-retry layer re-establishes
  on demand.
- **Identity-source field**: optional `target.provider` chooses among
  `Local` / `ActiveDirectory` / `vIDM`. Default `Local`. The connector
  threads the value into the JSON login body verbatim.
- **Credential rotation**: update the Vault secret, then restart the
  backplane (or wait for the next session expiry) so the connector
  reloads. There is no per-target credential refresh hook in v0.5.

To register a new target, write a descriptor and import it. `meho targets
import` takes a `targets.yaml` **file** (there is no `meho targets create`
verb in v0.2 — `import` is the CLI's only write path):

```yaml
# rdc-vrli.yaml
targets:
  - name: rdc-vrli
    product: vcf-logs
    host: vrli.rdc.evoila.io
    port: 443
    secret_ref: kv/data/vrli/rdc-vrli
    auth_model: shared_service_account
```

```bash
meho targets import rdc-vrli.yaml   # add --update to PATCH an existing target
```

**Self-signed / internal-CA appliance.** A nested-lab or freshly-deployed
vRLI commonly presents a self-signed cert, which otherwise fails dispatch
with `connector_tls_verify_failed`. Add a per-target TLS-trust field to the
same descriptor — prefer pinning the appliance CA (verification stays on);
use `verify_tls: false` only as an audited last resort (the two are mutually
exclusive — see the [per-target TLS-trust guide](../../deploy/values-examples/README.md)):

```yaml
    # secure — trust this CA; chain + hostname verification stay ON:
    tls_ca_pin: |
      -----BEGIN CERTIFICATE-----
      ...appliance CA PEM...
      -----END CERTIFICATE-----
    # last resort instead of tls_ca_pin (verification OFF for this target; MITM risk):
    # verify_tls: false
```

Verify the fingerprint resolved correctly:

```bash
meho targets probe --name rdc-vrli --json | jq '{product, version, reachable}'
# expected: {"product": "vcf-logs", "version": "9.0", "reachable": true}
```

## Quick-start

```bash
# Identity + version
meho vcf-logs about --target rdc-vrli

# Event-query — empty constraints + 1h time window (headline surface)
meho vcf-logs query --target rdc-vrli --time-range 1h

# Event-query — with constraints + 24h window + 100-row cap
meho vcf-logs query "text/CONTAINS+error" --target rdc-vrli --time-range 24h --limit 100

# Aggregated event-query — group-by counts over a 24h window
meho vcf-logs aggregated --target rdc-vrli --time-range 24h

# Indexer-field catalog
meho vcf-logs field list --target rdc-vrli

# Hosts reporting events
meho vcf-logs host list --target rdc-vrli

# Installed content packs
meho vcf-logs content-pack list --target rdc-vrli

# Configured alert definitions
meho vcf-logs alert list --target rdc-vrli

# Machine-readable output for any verb
meho vcf-logs host list --target rdc-vrli --json | jq '.result.hosts[] | .hostname'

# Escape hatch: run any vrli-rest-9.0 op by op_id
meho vcf-logs operation call GET:/api/v2/version --target rdc-vrli
meho vcf-logs operation search "alerts"
```

## Verb reference

### `meho vcf-logs about`

Dispatches `GET:/api/v2/version` against `connector_id="vrli-rest-9.0"`.
The version endpoint is **unauthenticated** — the connector still
threads the cached Bearer header (because `auth_headers()` lazily
establishes a session on first use), but the appliance accepts the
call without it. Renders `version` + `releaseName`.

```text
$ meho vcf-logs about --target rdc-vrli
vrli-rest-9.0 GET:/api/v2/version — status=ok (21ms)
  version:      9.0.0
  release_name: VMware Aria Operations for Logs 9.0
```

### `meho vcf-logs query [constraints]`

Dispatches `GET:/api/v2/events/{constraints}`. The headline read surface
of vRLI. The positional `constraints` argument carries vRLI's
URI-segment-encoded constraint expression (e.g.
`text/CONTAINS+error+hostname/CONTAINS+vcsa`); empty constraints (no
positional argument) is allowed and returns the unconstrained set
bounded by `--time-range` / `--limit`. The `--time-range` flag honours
Goal #214 G3.6 DoD L184 — it threads to `params.timestamp_window`
which the backend rewrites into the vRLI timestamp constraint. The
`--limit` flag caps the result-set size via the query string.

Large result sets are JSONFlux-handle-shaped: the dispatcher reduces
them into a `ResultHandle` and the human-rendered output shows the
row count with a pointer to `meho operation result-query` for
drilling in. Use `--json` to see the full envelope including the
handle metadata.

```text
$ meho vcf-logs query --target rdc-vrli --time-range 1h
vrli-rest-9.0 GET:/api/v2/events/{constraints} — status=ok (148ms)
  events:   42
  complete: true
timestamp                  hostname                 text
1747896000000              esx-01.lab               login failure on attempt 1
…
```

### `meho vcf-logs aggregated [constraints]`

Dispatches `GET:/api/v2/aggregated-events/{constraints}`. Group-by
aggregation over the same constraint shape `query` accepts, plus a
bin-by clause + an aggregation function (count, sum, avg, min, max)
encoded in the constraints string. Use when answering "how many error
events per host in the last 24h" or "top 10 sources by event volume
since midnight". Numeric sequence safe to inline directly into agent
context (not handle-shaped).

### `meho vcf-logs field list`

Dispatches `GET:/api/v2/fields`. The catalog of fields the indexer
knows about — static (`hostname`, `timestamp`, `text`, `source`) plus
extracted (parsed from log content via content-pack rules). Read
this before composing a non-trivial `vcf-logs query` constraint to
confirm a field name actually exists on the cluster.

### `meho vcf-logs host list`

Dispatches `GET:/api/v2/hosts`. Hosts currently reporting log events.
Combined with the field catalog, this is the minimum composer-context
for a useful event query.

### `meho vcf-logs content-pack list`

Dispatches `GET:/api/v2/content/contentpack/list`. Installed content
packs — each governs a set of extracted fields, dashboards, and alert
templates for a specific product integration (NSX, vSAN, vCenter, etc).
Read when answering "which integrations are configured on this vRLI"
or "why is field X missing from the catalog".

### `meho vcf-logs alert list`

Dispatches `GET:/api/v2/alerts`. Configured alert definitions — name,
enabled flag, search constraint, time window, hit threshold,
notification channels. Read-only in v0.5; alert create / update /
delete stay outside the curated core (write surface lands behind the
policy + approval gate in a later release).

### `meho vcf-logs operation search <query>`

Wraps `GET /api/v1/operations/search` with `connector_id="vrli-rest-9.0"`
pre-baked. Hybrid BM25 + cosine RRF search across the 7 vRLI ops.
Pre-scoped means the operator doesn't have to filter results by
`connector_id` after the fact.

```bash
meho vcf-logs operation search "event query"
meho vcf-logs operation search "alert definitions" --group vrli-alerts
```

### `meho vcf-logs operation call <op_id>`

Escape hatch for ops without a dedicated alias verb — wraps
`POST /api/v1/operations/call` with `connector_id="vrli-rest-9.0"`
pre-baked. Use when an enabled ingested op lacks a CLI alias or when
inspecting an op the curated core doesn't surface.

```bash
meho vcf-logs operation call GET:/api/v2/events/{constraints} \
  --target rdc-vrli \
  --params '{"constraints":"text/CONTAINS+error","timestamp_window":"1h","limit":"100"}'
```

## MCP surface

Per CLAUDE.md postulate 5, the agent reaches vRLI ops via the narrow-
waist meta-tool surface (`search_operations(connector_id="vrli-rest-9.0", ...)`
+ `call_operation(...)`) — **not** through a per-op MCP tool. The
curated `when_to_use` strings on the 5 vRLI operation groups and the
`llm_instructions` blobs on the 7 ops are what the agent reads when
choosing a group and composing the call. Both surfaces are reviewed
in this release (G3.6-T5).

The agent never sees the `meho vcf-logs ...` alias verbs — those are
operator-only ergonomics over the same backend route.

## Wrapper-retirement recipe

To retire `scripts/vcf-logs.sh` in favour of `meho vcf-logs ...`:

1. **Register the target** (one-time per appliance):

   ```bash
   meho targets import \
     --name rdc-vrli \
     --product vcf-logs \
     --host vrli.rdc.evoila.io \
     --port 443 \
     --secret-ref kv/data/vrli/rdc-vrli \
     --auth-model shared_service_account
   meho targets probe --name rdc-vrli
   ```

2. **Ingest + enable the curated 7-op core** (one-time per appliance):

   ```bash
   meho connector ingest --catalog vcf-logs/9.0
   # Then run the G3.6-T5 curation helper from a backend script or
   # admin notebook (apply_vrli_core_curation drives ReviewService).
   meho connector list | grep vrli-rest-9.0
   # expected: vrli-rest-9.0 ops_enabled=7 review_status=enabled
   ```

3. **Map wrapper calls 1:1**:

   | `scripts/vcf-logs.sh` invocation | `meho vcf-logs ...` equivalent |
   | --- | --- |
   | `./scripts/vcf-logs.sh --probe` | `meho targets probe --name rdc-vrli` |
   | `./scripts/vcf-logs.sh --version` | `meho vcf-logs about --target rdc-vrli` |
   | `./scripts/vcf-logs.sh --query <expr> --range 1h` | `meho vcf-logs query "<expr>" --target rdc-vrli --time-range 1h` |
   | `./scripts/vcf-logs.sh --aggregate <expr> --range 24h` | `meho vcf-logs aggregated "<expr>" --target rdc-vrli --time-range 24h` |
   | `./scripts/vcf-logs.sh --fields` | `meho vcf-logs field list --target rdc-vrli` |
   | `./scripts/vcf-logs.sh --hosts` | `meho vcf-logs host list --target rdc-vrli` |
   | `./scripts/vcf-logs.sh --content-packs` | `meho vcf-logs content-pack list --target rdc-vrli` |
   | `./scripts/vcf-logs.sh --alerts` | `meho vcf-logs alert list --target rdc-vrli` |

4. **Validate the audit trail**: every `meho vcf-logs ...` dispatch
   writes an `AuditLog` row carrying `op_id`, `target_id`, and a
   `params_hash`. Confirm via:

   ```bash
   meho audit recent --target rdc-vrli --limit 5
   ```

5. **Flip the wrapper symlink** (consumer-side):
   `scripts/vcf-logs.sh` becomes a stub that prints a deprecation
   notice + the equivalent `meho vcf-logs ...` invocation. Mirror the
   pattern from `scripts/nsx.sh` (post-#615 flip).

## Troubleshooting

### `meho: connector error: vrli session re-login failed for target …`

The connector's 401-retry-once contract: a 401 from a downstream call
triggers one re-login. A second 401 raises this error. Almost always
means the Vault-stored credentials are wrong (operator rotated the
account password but didn't update Vault) or the `provider` field
doesn't match the identity-source the account lives in.

### `meho: connector error: SessionLoginError … POST /api/v2/sessions returned HTTP 401`

The initial session-establish POST got a 401. Same root causes as
above; usually means the password in Vault doesn't match the
appliance.

### `meho: connector error: VcfLogsConnector only supports auth_model='shared_service_account'`

The target has `auth_model="per_user"` or `auth_model="impersonation"`.
v0.5 ships `shared_service_account` only. Update the target via
`meho targets import --auth-model shared_service_account` or
file an issue if per-user is required for your workflow.

### `meho: connector error: vrli credentials loader … returned a dict missing required key 'password'`

The Vault secret at `target.secret_ref` is missing the `password`
field. The connector expects exactly `{"username": str, "password": str}`
— extra fields are fine, missing required fields raise this clear
message.

### `meho: connector error: JSONDecodeError`

The dispatched op hit an unexpected response shape (empty body, HTML
error page, etc.). Most often means an appliance proxy / WAF
intercepted the call and returned non-JSON. Run with `--json` and
inspect `extras` for the raw transport error. Re-check
`meho targets probe --name <slug>` to confirm the appliance is
reachable on the expected path.

### Empty `vcf-logs query` result with `complete=false`

The constraint hit the appliance's row-cap before exhausting the
index. Increase `--limit`, tighten `--time-range`, or use
`vcf-logs aggregated` for group-by counts instead of a raw event
listing.

## References

- Initiative: [#369 G3.6 tier-3 VCF management plane](https://github.com/evoila/meho/issues/369).
- Parent goal: [#214 Connector parity](https://github.com/evoila/meho/issues/214).
- Engineering companion: [`docs/codebase/connectors-vcf-logs.md`](../codebase/connectors-vcf-logs.md).
- Shared auth helpers: [`docs/codebase/connectors-vcf-auth-shared.md`](../codebase/connectors-vcf-auth-shared.md).
- Recorded-fixture refresh recipe: [`docs/cross-repo/vcf-fixture-refresh.md`](./vcf-fixture-refresh.md).
- Sibling onboarding doc shape: [`docs/cross-repo/nsx-onboarding.md`](./nsx-onboarding.md).
- vRLI API: <https://developer.broadcom.com/xapis/vrealize-log-insight-api/latest/>.
- Consumer wrapper this replaces: [`scripts/vcf-logs.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/scripts/vcf-logs.sh).
