# Add-on pairing

The pairing handshake + identity plane — the foundation of the add-on
pairing contract (Initiative #2900, Task #3025). It lets a sibling add-on
product (first consumers: meho-automation, meho-ssp) pair once with the
backplane and become a first-class, governed peer, while an **unpaired**
backplane stays byte-identical to a never-paired one.

Pairing is deliberately **not** connector registration. A connector is a
managed vendor system below the narrow waist; a paired add-on is a peer
control plane beside it, integrated through the governance planes
themselves. The agent surface (the meta-tool waist) is untouched by this
task — an add-on identifier is data (`op_id`, a DB row), never a tool name.

## Overview

Pairing does three things, all audited and reversible:

1. **Identity** — provisions a confidential Keycloak client-credentials
   client tagged `kind=service` (`principal_kind=service`,
   `tenant_role=read_only`) and hands the add-on its one-time
   `client_secret`. The add-on authenticates as this **service** principal.
   It is scoped by construction: the non-agent policy gate parks every
   mutating op for a service principal by default, so a paired add-on starts
   with **no blanket admin** — operators widen it per-op via
   `ServicePrincipalGrant` (`service-principal-grants.md`), never here.
2. **Versioned contract negotiation** — the add-on advertises the contract
   version it speaks and the oldest backplane it will accept; the backplane
   pins both directions (below) and persists the negotiated version.
3. **Health / liveness** — the pairing's contract compatibility (re-evaluated
   live against the current backplane) and last liveness heartbeat surface in
   `/api/v1/health` (and therefore the `meho_status` MCP tool + `meho status`
   CLI) and the `/ui/pairing` console panel.

**Unpair** is reversible: it deletes the Keycloak client (the authoritative
kill switch) then hard-deletes the pairing row. The row is not soft-kept — an
unpaired backplane is byte-identical to a never-paired one; the append-only
`audit_log` retains the pair/unpair history. The name frees up for a clean
re-pair.

## Contract negotiation and version skew

Contract versions are monotonic integers (one publisher, a small set of
first-party consumers — a counter carries all the pinning semantics without a
dotted-string parser). The backplane declares:

- `BACKPLANE_CONTRACT_VERSION` — the contract this build speaks.
- `MIN_SUPPORTED_ADDON_CONTRACT_VERSION` — the oldest add-on contract it
  still accepts.

The pair request carries `addon_contract_version` (what the add-on speaks)
and `addon_min_backplane_version` (the add-on's backplane floor).
`negotiate()` pins **both** directions:

| Direction | Condition | Result |
|---|---|---|
| add-on too old | `addon_contract_version < MIN_SUPPORTED_ADDON_CONTRACT_VERSION` | refuse — code `addon_contract_below_backplane_minimum` (409) |
| backplane too old | `addon_min_backplane_version > BACKPLANE_CONTRACT_VERSION` | refuse — code `backplane_contract_below_addon_minimum` (409) |
| compatible | both floors satisfied | negotiate `min(addon_contract_version, BACKPLANE_CONTRACT_VERSION)`, persist |

`is_contract_compatible()` re-applies the same two checks to a **persisted**
pairing against the *current* backplane constants. This is the health signal:
a pairing that negotiated cleanly against an earlier build can drift
incompatible after an upgrade that raised the minimum past the add-on's
advertised version, or a downgrade that dropped the backplane version below
the add-on's floor. Either way the pairing reads `contract_compatible=false`
in `/status` and the console without a re-handshake.

## Key types

- `meho_backplane.operations.addon_pairing_contract` — pure negotiation:
  the two version constants, `negotiate()`, `is_contract_compatible()`,
  `NegotiatedContract`, and `ContractSkewError` (carries a stable machine
  `code` + the diagnostic versions).
- `meho_backplane.operations.addon_pairing.AddonPairingService` — the single
  lifecycle path (`pair` / `unpair` / `list_` / `get` / `heartbeat`).
  Stateless, method-scoped sessions; Keycloak-first on both mutating paths
  (mirrors `AgentPrincipalService` / `runner_principals`). Errors:
  `AddonAlreadyPairedError`, `AddonNotPairedError`.
- `meho_backplane.operations.addon_pairing_schemas` — `PairAddonRequest`
  (intake, `extra="forbid"`), `PairedAddonRead`, `PairAddonResult`
  (one-time, repr-hidden `client_secret`), `PairedAddonListResponse`
  (the `{items, next_cursor}` envelope).
- `meho_backplane.db.models.AddonPairing` — table `addon_pairing`; unique
  `(tenant_id, name)` and unique `keycloak_client_id`; migration `0079`.
- `meho_backplane.api.v1.addon_pairing` — the REST surface.
- `meho_backplane.api.v1.health.PairingHealth` — the `/status` facet.
- `meho_backplane.ui.routes.pairing` — the read-only `/ui/pairing` console
  panel.

## Control flow

**Pair** (`POST /api/v1/addons/pairings`, `tenant_admin`):

1. Validate the add-on name; `negotiate()` (cheap, side-effect-free — a skew
   rejection never provisions Keycloak).
2. `KeycloakAdminClient.create_client(principal_kind="service",
   kind_attribute="service", tenant_role="read_only")`, then read back the
   generated secret. Any failure after create rolls the client back (no
   orphaned, un-revocable identity).
3. Insert the `addon_pairing` row (a DB failure rolls the Keycloak client
   back).
4. Return the one-time `PairAddonResult` (client id + secret + negotiated
   contract). The audit middleware records the row synchronously
   (`op_id=addon.pair`, `op_class=write`).

**Unpair** (`DELETE /api/v1/addons/pairings/{name}`, `tenant_admin`): look up
the row, `delete_client` in Keycloak first (a non-404 failure aborts before
the row is touched, so the backplane never reports unpaired while the client
can still mint tokens), then hard-delete the row. Audited
(`op_id=addon.unpair`).

**Heartbeat** (`POST /api/v1/addons/pairings/{name}/heartbeat`): the paired
add-on, authenticating as its own **service** principal (a human principal is
403), stamps `last_seen_at`.

**Health**: `build_health_response` calls `_pairing_health(tenant_id)`, which
lists active pairings and maps each to a `PairingHealth`
(`contract_compatible` recomputed live). Empty list when nothing is paired.

## Dependencies

- `KeycloakAdminClient` (`auth/keycloak_admin.py`) for client-credentials
  client lifecycle. Keycloak admin unconfigured → 503 at the pair/unpair
  boundary; other admin failures → 502.
- The `service`-principal policy gate (`operations/_validate._non_agent_verdict`)
  + `ServicePrincipalGrant` are what keep a paired add-on scoped.
- Feature maturity: `addon_pairing` (experimental) in `FEATURE_MATURITY`,
  mapped by tag (`addon-pairing`) and console surface (`pairing`).

## Known issues / follow-ups

- **Go CLI verbs** (`meho addon pair/unpair/list`) are not shipped here.
  Pairing management is REST + console; pairing health already flows to
  `meho status` via `/api/v1/health` and the `meho_status` MCP tool. Adding
  the Go verbs is a separate change (OpenAPI snapshot + oapi-codegen regen).
- **Heartbeat principal binding** is coarse: it requires a `service`
  principal in the pairing's tenant, matched by add-on name. A finer binding
  (verifying the caller's client id against the pairing's
  `keycloak_client_id`) is a hardening follow-up for the initiative's
  security-review DoD item.
- **Console is read-only**: pair / unpair are REST-only. Console write
  actions (a pair/unpair button behind CSRF) are a follow-up.
- Keycloak client provisioning is now duplicated three ways (agent, runner,
  add-on); a shared helper is an extraction candidate once the pattern
  stabilises (rule of three), but is out of scope for this task.

## References

- Initiative #2900 (add-on pairing contract), Task #3025 (this foundation).
- `service-principal-grants.md` — the scoping mechanism the paired principal
  relies on.
- `connectors-keycloak.md` — the Keycloak admin client lifecycle.
