# Connector: meho-automation add-on (mehoauto-rest)

## Overview

The meho-automation **add-on** is a paired, backplane-adjacent workflow
runner. This connector registers it as a **versioned, profile-backed generic
connector target** so the backplane can drive the add-on's operator-facing
launch / validate / gate-decision API through `call_operation` — the same
auth → policy → audit → JSONFlux → broadcast path any vendor API rides —
instead of an out-of-band header-trust call. The add-on remains a peer
control plane reached only over the wire; there is **no reverse code
coupling** (the backplane imports nothing from the add-on; an unpaired
backplane is byte-identical).

It is a **profile-backed generic connector**, not a typed connector and not a
per-op MCP tool family:

- **No hand-coded connector class.** The dispatchable
  `ProfiledRestConnector` is synthesised from the shipped
  `ExecutionProfile` at boot by `stamp_catalog_profiled_connectors`.
- **No vendored spec.** The three ops are ingested at registration time from
  the add-on's published `/openapi.json` (OpenAPI 3.1) via the operator
  `--spec` on-ramp. The add-on's OpenAPI is **not committed to this repo**.
- **No new agent tools.** The three ops ride `op_id` under `call_operation`;
  the MCP working surface is unchanged.

Dispatch triple: `(product="mehoauto", version="0.1.0",
impl_id="mehoauto-rest")`. The product slug is **hyphen-free** on purpose:
the connector-id parse recovers the product from the first hyphen segment of
`impl_id`, so a hyphenated product (`meho-automation`) would not round-trip
(the same constraint that gave `sddc-manager → sddc`,
`vcf-automation → vcfa`). The add-on's Kubernetes Service name
(`meho-automation`) and the Keycloak audience (`meho-automation`) are
decoupled from this slug and unaffected.

## Shipped artifacts (in this repo)

| Artifact | Path |
|---|---|
| Catalog row | `operations/ingest/catalog.yaml` (`product: mehoauto`) |
| ExecutionProfile | `connectors/profiles/meho_automation_minimal.yaml` |
| Ingest safety floor | `connectors/meho_automation/ingest_safety.py` |

The `connectors/meho_automation/` package holds **only** the safety-floor
registration — a profile-backed connector has no hand-coded class. It is
eager-imported at boot like every other connector subpackage.

## Ops in scope

| Op (`op_id` after ingest) | Purpose | Safety tier |
|---|---|---|
| `POST:/api/v1/runs` | Launch a run | `caution` (no approval park) |
| `POST:/api/v1/blueprints/{blueprint_id}/validate` | Validate a blueprint (read-side dry-run) | `safe` |
| `POST:/api/v1/runs/{run_id}/gates/{node_id}/decision` | Decide an in-run gate | `caution` (no approval park) |

The launch body is compiled server-side from typed `inputs`; passing
`inputs` (plus `name`, and optionally `work_ref`, `simulate`, `tenant`) is
enough for a governed launch. `params` / `target_ref` are advanced per-node
overrides.

### Safety tiers and the pin

All three routes are POSTs, so the generic ingest heuristic classifies each
`caution` by default (#3563). The connector-owned floor
(`meho_automation_safety_floor`) **pins** the decided tiers so a spec
re-ingest — or a future change to the verb heuristic — cannot silently drift
them:

- launch + gate → `caution`, **`requires_approval=False`** (no backplane
  approval park);
- validate → `safe` (downgraded from the POST-default `caution` because it
  dispatches nothing).

**Security note — launch executes without a backplane park.** Launch is a
potentially destructive lifecycle operation, but this connector does **not**
park it for a MEHO-side approval. That is a deliberate operator decision
(governed-launch design T-dec-4): the add-on's own **in-run gate nodes** are
the human control point, so a second backplane-side approval on the launch
call would be redundant. Enabling a launch-capable connector, and the first
live launch, remain operator-gated actions. The gate-decision op is exposed
as an ordinary `caution` op here (the add-on's gate model is the human
control); MEHO's own approval-decision verbs
(`meho_approvals_approve/reject`) are unrelated and remain human-only with no
MCP path.

## Auth — oauth2_mint with an external, per-target issuer

The add-on validates an inbound realm bearer whose issuer (the identity
provider) is a **different host** from the add-on API, so the profile uses
`oauth2_mint` with an external token endpoint (public #3571 gave `oauth2_mint`
its `token_url` / `scope` / `audience` fields). The backplane mints a
client-credentials token against the realm and presents it as `Bearer`.

The realm token endpoint is a **per-deployment value** (each deployment mints
against its own realm), so it is **not** hard-coded in the shipped, public
profile. Instead it is sourced **per-target from the Vault-backed target
credential** as a `token_url` field, alongside `client_id` / `client_secret`:

```
auth:
  scheme: oauth2_mint
  secret_fields: [client_id, client_secret, token_url]
  audience: meho-automation      # non-secret; the audience the add-on accepts
```

`token_url` is declared in `secret_fields`, so it is **mandatory in Vault**
(a fail-closed read error if the operator omits it). At dispatch the
`oauth2_mint` scheme resolves the endpoint via
`_oauth2_login_path_from_secret` (`connectors/_shared/profile_auth.py`) with
this precedence: credential `token_url` (validated fail-closed with the same
`_validate_token_url` SSRF rule the profile field clears — `https` for a
public host, plaintext `http` only for a cluster-internal / private issuer) →
profile `auth.token_url` → target-relative `/realms/master/...`. A credential
bundle without a `token_url` (e.g. keycloak) falls straight through to the
existing behaviour, so this override is invisible to every other
`oauth2_mint` user.

The Vault credential the target's `secret_ref` points at therefore holds:

```json
{
  "client_id": "...",
  "client_secret": "...",
  "token_url": "https://<realm-token-endpoint>/protocol/openid-connect/token"
}
```

## Registration recipe (operator, on a deployed backplane)

1. **Create the target** at the cluster-internal base URL (no ingress, no
   TLS — plain `http` is accepted for a dotless / in-cluster host):

   ```
   meho target create \
     --name automation-addon \
     --product mehoauto --version 0.1.0 \
     --base-url http://meho-automation:8000 \
     --secret-ref <vault-kv-path-holding-client_id/client_secret/token_url>
   ```

   (The add-on OpenAPI declares no `servers` block, so the base URL is set on
   the target here.)

2. **Ingest the ops from the add-on's live spec** via the `--spec` on-ramp —
   there is no vendored spec and no catalog upstream, so `--catalog` is not
   the path (the listing's `next_step` hint says so):

   ```
   # fetch the add-on's published OpenAPI (unauthenticated), then ingest its bytes
   curl -s http://meho-automation:8000/openapi.json > /tmp/mehoauto.openapi.json
   meho connector ingest \
     --product mehoauto --version 0.1.0 --impl-id mehoauto-rest \
     --spec file:///tmp/mehoauto.openapi.json
   ```

   The three ops land **staged / disabled** (`is_enabled=false`,
   `source_kind=ingested`) with the pinned tiers above.

3. **Review the LLM-proposed op groups + per-group hints**, then **enable**
   the connector (staged → enabled) once the surface looks right:

   ```
   meho connector enable mehoauto-rest-0.1.0
   ```

4. **Dispatch** with the EXACT `connector_id` and the op's `op_id`:

   ```
   meho operation call --connector-id mehoauto-rest-0.1.0 \
     --op-id 'POST:/api/v1/blueprints/{blueprint_id}/validate' \
     --target automation-addon --params '{"blueprint_id": "...", "inputs": {...}}'
   ```

## Boot guards

At boot the shipped row + profile clear, in order (`main.py` lifespan):

- `load_catalog()` — the row parses (`spec_resource: null`,
  `profile_resource` set, `catalog_ingest: spec-only`);
- `validate_catalog_registry_coverage()` — profile-backed rows are exempt
  from the class-presence + triple-registration checks;
- `validate_shipped_artifacts()` — dry-run-parses the profile
  (`ExecutionProfile.model_validate` + `validate_execution_profile`); there is
  no `spec_resource`, so nothing is parsed there and nothing is leaked;
- `stamp_catalog_profiled_connectors()` — synthesises + registers
  `ProfiledRestConnector_mehoauto_0_1_0` under the `(mehoauto, 0.1.0,
  mehoauto-rest)` v2 key, resolvable for a target of that fingerprint.

## References

- Catalog / profile mechanism: `docs/codebase/connector-auth-coverage.md`
  (auth catalog), the shipped `_fixture/1.0`, `vmware/9.0`, `sddc/9.0` rows.
- `oauth2_mint` external issuer: public #3571.
- Add-on pairing contract (identity / capability / event / audit planes):
  `docs/codebase/addon-contract.md`, `docs/codebase/addon-pairing.md`.
