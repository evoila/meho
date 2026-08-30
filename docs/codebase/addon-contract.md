# Add-on pairing contract (versioned) + proof plane

The **add-on pairing contract** is the versioned integration surface a sibling
product (first consumers: meho-automation, meho-ssp) speaks to become a
first-class, governed peer of the backplane. Initiative #2900 builds it across
four planes, each documented on its own page; this page is the **contract-level
synthesis** — the version it declares, how it behaves under version skew, and
the reference-double **proof plane** (#3030) that shows the four planes compose
end to end while an unpaired backplane stays byte-identical to today.

A paired add-on is deliberately **not** a connector. A connector is a managed
vendor system *below* the narrow waist; a paired add-on is a peer control plane
*beside* it, integrated through the governance planes themselves. The agent
surface (the meta-tool waist) is untouched: an add-on identifier — its name, its
advertised meta-tool family — is *data* (a DB row), never a tool name
(postulate 5).

## The four planes

| Plane | Task | Page | What the add-on gets |
|---|---|---|---|
| Pairing + identity | #3025 | [`addon-pairing.md`](addon-pairing.md) | a Keycloak `kind=service` principal, a negotiated contract version, reversible/audited pair-unpair, health in `/status` |
| Capability advertisement | #3026 | [`addon-pairing.md`](addon-pairing.md) | declares its surfaces (meta-tool family, CLI verb family, console panel, event kind) as data; activation derived from pairing health |
| Step-event push | #3027 | [`addon-step-events.md`](addon-step-events.md) | a durable, resumable outbound log of its own approval outcomes + dispatch completions, scoped to its lineage |
| Audit parent-linkage | #3028 | [`addon-parent-linkage.md`](addon-parent-linkage.md) | an out-of-process orchestration's per-step dispatches collapse into one audit-replay subtree, keyed to `work_ref` |

All four are authorized against the **same identity** the pairing establishes:
the add-on's Keycloak service-account. Two projections of that identity join the
planes together:

- `AddonPairing.keycloak_client_id` (`addon:<name>`) — the OAuth `clientId`,
  recovered onto `Operator.client_id`; the audit-linkage key (#3028).
- `AddonPairing.service_account_sub` — the token `sub`, captured at pair time
  from `KeycloakAdminClient.get_service_account_user_id`; the step-event
  attribution + subscription-bind key (#3027).

Capturing both at pair time — the one point the backplane controls the identity
— is what lets every later plane attribute work to a pairing without ever
inferring identity from an unverified token.

## Contract version

Contract versions are **monotonic integers** (one publisher, a small set of
first-party consumers — a counter carries the pinning semantics without a
dotted-string parser). Two constants in
`operations/addon_pairing_contract.py` declare the backplane's position:

- `BACKPLANE_CONTRACT_VERSION` — the contract this build speaks.
- `MIN_SUPPORTED_ADDON_CONTRACT_VERSION` — the oldest add-on contract it still
  accepts.

The pair request carries the add-on's position: `addon_contract_version` (what
the add-on speaks) and `addon_min_backplane_version` (the add-on's backplane
floor). `negotiate()` pins **both** directions and, on success, persists the
negotiated `min(addon_contract_version, BACKPLANE_CONTRACT_VERSION)`.

Growing the vocabulary a version understands (e.g. a new `CapabilityKind`) is a
coordinated bump: the enum, the `addon_capability.kind` CHECK constraint, and
`models.ADDON_CAPABILITY_KINDS` move together (drift-guarded by
`test_addon_capability_service`), and the new kind is only meaningful at or above
the contract version that introduced it.

## Version-skew behavior

Skew is evaluated **twice**: once at pair time (`negotiate()`, which refuses an
un-pairable pair) and continuously afterwards (`is_contract_compatible()`,
re-applied live against the *current* backplane constants — the health signal).
A pairing that negotiated cleanly against an earlier build can drift
incompatible after an upgrade or downgrade, with no re-handshake.

| Direction | Condition | At pair time (`negotiate`) | While paired (`is_contract_compatible`) |
|---|---|---|---|
| add-on too old | `addon_contract_version < MIN_SUPPORTED_ADDON_CONTRACT_VERSION` | refuse — `addon_contract_below_backplane_minimum` (409) | reads `contract_compatible=false`; capabilities drop out of the activation view |
| backplane too old | `addon_min_backplane_version > BACKPLANE_CONTRACT_VERSION` | refuse — `backplane_contract_below_addon_minimum` (409) | reads `contract_compatible=false`; capabilities drop out |
| compatible | both floors satisfied | negotiate + persist | reads `contract_compatible=true`; capabilities active |

The two drift directions are the real operational cases:

- **Newer backplane, older add-on.** A backplane upgrade that raises
  `MIN_SUPPORTED_ADDON_CONTRACT_VERSION` past a pairing's advertised
  `addon_contract_version` flips that pairing incompatible. Its declaration
  persists (health can recover once the add-on upgrades), but it contributes
  nothing to `active_capabilities()` and reads `contract_compatible=false` in
  `/status` and the console — a fail-safe, not a silent half-activation.
- **Older backplane, newer add-on.** A backplane downgrade (or an add-on that
  raised its `addon_min_backplane_version` floor) below the add-on's required
  floor flips the same signal the other way. Identical fail-safe: the pairing
  reads incompatible and its surfaces deactivate until the versions realign.

Activation is **derived, never stored**: a capability is active only while its
pairing satisfies `is_contract_compatible()`, so the compatible/incompatible
transition needs no row rewrite — the `active` flag and activation-view
membership recompute from live pairing health.

## The proof plane (#3030)

The four planes each ship with plane-level unit tests. The proof plane adds the
**composition** proof: a single reference add-on that participates in every
plane, and the conformance guarantee that doing so grows no agent surface.

### Reference double (test harness)

`tests/addon_reference_double.py::ReferenceAddon` is a `tests/` harness — **not**
a shipped connector or add-on. It drives the *real* contract services
(`AddonPairingService`, `AddonCapabilityService`, `AddonStepEventService`, the
`addon_orchestration` linkage seam); the only stub is Keycloak, monkey-patched
at the pairing boundary so CI needs no live realm. Its methods map one-to-one to
the planes: `pair()` / `advertise_meta_tool_family()` / `produce_step_event()` +
`consume_step_events()` / `run_orchestration()` / `unpair()`. The double holds
exactly the identity a real add-on's client-credentials token carries — its
`keycloak_client_id` and the `service_account_sub` captured at pair time — and
`operator()` mints the matching `PrincipalKind.SERVICE` operator the linkage seam
authorizes against.

> **Harness gotcha.** Pairing reads the add-on's `service_account_sub` back from
> `KeycloakAdminClient.get_service_account_user_id`. A test double whose Keycloak
> stub omits that method pairs with a `NULL` sub, and every later plane
> (step-event attribution, subscription bind) fails closed against it. The
> reference double's stub always provides it; reuse the double rather than
> re-stubbing Keycloak per test.

### Unpaired byte-identical conformance

The invariant an unpaired backplane must hold: it is behaviorally identical to a
never-paired one, and pairing grows only operator/add-on-plane surface — never
the agent's meta-tool waist. `tests/test_addon_reference_double.py` pins it two
ways:

- Advertising a meta-tool family registers **no** MCP tool — the advertised
  family name appears in no `mcp.registry._TOOLS` key, and the `tools/list` wire
  output while paired-and-advertising is byte-identical to the pre-pair baseline.
- A full pair → advertise → produce/consume → orchestrate → unpair lifecycle
  returns `tools/list` byte-identical to the baseline.

Both compare against a baseline **captured live at test start**, not a pinned
literal tool list. This is deliberate: the working surface grows over time (the
sibling #3029 paired-surface-activation task lights up the first-party
`automation` family gated on pairing state), and the conformance property is
"pairing an arbitrary add-on and advertising a family is data, not surface
growth" — which stays true regardless of how the baseline itself evolves.
`all_tools_for` filters the registry by role / capability / surface only and
never reads pairing state, so the invariant is structural; the test guards
against a regression that would make it otherwise.

### Trust model

The pairing trust model — principal scoping, capability caps, and
event-subscription scoping to a pairing's own lineage — is reviewed in
[`docs/decisions/addon-contract-trust-model.md`](../decisions/addon-contract-trust-model.md).
The review found no defects requiring a code change; each scoping property is
tied to a specific structural mechanism and its guarding test.

## Key types

- `operations/addon_pairing_contract` — `BACKPLANE_CONTRACT_VERSION`,
  `MIN_SUPPORTED_ADDON_CONTRACT_VERSION`, `negotiate()`,
  `is_contract_compatible()`, `NegotiatedContract`, `ContractSkewError`.
- `operations/addon_pairing.AddonPairingService` — `pair` / `unpair` / `list_` /
  `get` / `get_by_client_id` / `heartbeat`.
- `operations/addon_capability.AddonCapabilityService` — `declare` /
  `list_declared` / `active_capabilities`.
- `operations/addon_step_events.AddonStepEventService` — `record_if_owned` /
  `record_if_owned_committed` / `resolve_pairing_for_sub` / `list_for_pairing`.
- `operations/addon_orchestration` — `resolve_or_open_orchestration_run` /
  `bound_parent_linkage`.
- `tests/addon_reference_double.ReferenceAddon` — the proof-plane harness.

## References

- Initiative #2900 (add-on pairing contract). Tasks #3025 (pairing), #3026
  (capability advertisement), #3027 (step events), #3028 (parent-linkage),
  #3030 (this proof plane).
- [`addon-pairing.md`](addon-pairing.md), [`addon-step-events.md`](addon-step-events.md),
  [`addon-parent-linkage.md`](addon-parent-linkage.md) — the per-plane pages.
- `docs/codebase/mcp.md` — the dual-surface tool inventory the conformance test
  guards against pairing-driven growth.
- v0.1-spec §6 (synchronous append-only audit); CLAUDE.md postulate 5
  (narrow-waist discipline), postulate 7 (audit lineage).
