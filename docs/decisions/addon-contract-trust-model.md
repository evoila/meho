<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Add-on pairing contract — trust-model security review (decision)

**Status:** reviewed — no code-change defects; two hardening follow-ups already
tracked (see Findings)
**Date:** 2026-08-30
**Initiative:** [#2900](https://github.com/evoila/meho/issues/2900) (add-on
pairing contract)
**Task:** [#3030](https://github.com/evoila/meho/issues/3030) (proof plane —
reference double + conformance + contract docs + this review)

## What this records

Initiative #2900 has a definition-of-done item: *"Security review of the pairing
trust model (principal scoping, capability caps, event-subscription scoping to
own lineage)."* This is that review. It examines the shipped planes (#3025
pairing, #3026 capability advertisement, #3027 step events, #3028 audit
parent-linkage) against the three scoping axes the DoD names, states the threat
each axis defends, identifies the structural mechanism that defends it, and
records the verdict. Findings that are real, exploitable defects are filed as
issues; a clean axis is recorded here.

The scope is the **backplane-side** contract only. The paired add-ons themselves
(meho-automation, meho-ssp) and their own posture are out of scope, as is any
relaxation of the backplane-internal-only network stance — paired add-ons live
on the same private network and the pairing contract does not change that.

## Trust baseline

Pairing mints the add-on a **confidential Keycloak client-credentials client**
tagged `principal_kind=service`, `tenant_role=read_only`
(`operations/addon_pairing.py::_provision_keycloak_client`). Two identity
projections are captured at pair time, at the one point the backplane controls
the identity:

- `keycloak_client_id` = `addon:<name>` — recovered onto `Operator.client_id`
  from the `service-account-<clientId>` username marker (#3178);
- `service_account_sub` = the token `sub`, read back from
  `KeycloakAdminClient.get_service_account_user_id`.

Capturing both at provisioning time is the linchpin: every downstream
authorization decision joins on a value the backplane wrote, never on a claim
the add-on could assert.

## Axis 1 — Principal scoping (the sub cannot exceed its grants)

**Threat.** A paired add-on obtains standing write authority over the tenant's
vendor systems by virtue of being paired, or widens its own authority.

**Mechanism.** The add-on's client is minted `tenant_role=read_only`, and the
non-agent policy gate (`operations/_validate._non_agent_verdict`) **parks every
mutating op** for a `service` principal by default. A paired add-on therefore
starts with *zero* standing write authority. Widening is per-op and
operator-driven only, via `ServicePrincipalGrant` (`service-principal-grants.md`)
— never a side effect of pairing, and never self-service by the add-on. The
`destructive` safety tier (#3196) is non-grantable to a service principal at
all: no `ServicePrincipalGrant` can satisfy it, so deletes are never reachable by
a paired add-on regardless of operator grants. Pairing itself requires
`tenant_admin` (a human operator); the add-on cannot pair or unpair itself.

**Verdict — clean.** Authority is default-deny and operator-gated. The pairing
grants identity, not capability. No defect.

## Axis 2 — Capability caps (advertisement is data, not surface)

**Threat.** An add-on advertising a capability lights up agent-facing tools or
otherwise grows the narrow-waist surface, violating postulate 5 and letting a
paired add-on inject tool names an agent would route on.

**Mechanism.** Capability advertisement writes `addon_capability` rows; it never
touches the MCP tool registry (`mcp.registry._TOOLS`). `all_tools_for` filters
that registry by role / capability / surface only and never reads pairing or
capability-advertisement state, so an advertised meta-tool family cannot become a
`tools/list` entry. The vocabulary is a **closed set** (`CapabilityKind`, guarded
by the `addon_capability.kind` CHECK and the `ADDON_CAPABILITY_KINDS` drift
test): an add-on cannot invent a surface kind. Activation is **derived from
pairing health**, never stored, so a capability cannot outlive its pairing's
contract compatibility; `ON DELETE CASCADE` on `addon_capability.pairing_id`
removes all rows on unpair, leaving no dead surface.

**Guarding tests.** `test_addon_reference_double.py::test_advertising_meta_tool_family_grows_no_agent_surface`
(the advertised family name appears in no registry tool key; `tools/list` is
byte-identical while paired-and-advertising) and
`test_full_pairing_lifecycle_leaves_toolslist_byte_identical` (unpaired ==
never-paired). `test_addon_pairing_conformance.py` pins the same waist invariant
at registry level.

**Verdict — clean.** Advertisement is data; the agent waist is structurally
untouched. No defect.

## Axis 3 — Event-subscription scoping (own lineage only)

**Threat.** A paired add-on reads another add-on's step events — approval
outcomes, dispatch completions — especially where a `work_ref` string collides
across two add-ons.

**Mechanism.** Attribution happens at **write** time by identity: a step event is
recorded only when the producing principal's `sub` matches a pairing's
`service_account_sub`, and the row is stamped with that `pairing_id`
(`AddonStepEventService.record_if_owned`). A pairing's log therefore only ever
contains that pairing's events — cross-pairing containment is a property of which
rows *exist* in which log, not a read-time filter that could be bypassed. The
read side binds by the caller's own token `sub` (`resolve_pairing_for_sub`) and a
`sub` that binds to no pairing (or to a different one) gets a uniform 404 — never
another add-on's log, never a name-existence oracle. A `NULL`
`service_account_sub` (a pre-#3027 pairing) never matches a real `sub`, so it
fails closed. The same clientId-keyed boundary applies to audit parent-linkage
(#3028): an orchestration run is keyed by the caller's own `keycloak_client_id`,
so a shared `work_ref` opens *separate* runs per add-on and one add-on can never
attach to — or observe — another's replay subtree.

**Guarding tests.**
`test_addon_reference_double.py::test_step_events_are_scoped_to_each_doubles_own_lineage`
and `test_orchestration_isolated_per_double_on_shared_work_ref` (both driven
through the reference double on the real identity join, with a deliberately
shared `work_ref`); `test_addon_step_events.py::test_scoping_never_delivers_another_pairings_events`
and `test_addon_orchestration_linkage.py::test_distinct_principals_same_work_ref_are_isolated`
at plane level.

**Verdict — clean.** Isolation is structural (write-time attribution by
identity), not a filter. No defect.

## Findings

No axis surfaced an exploitable defect requiring a code change, so no new issues
are filed. Two **hardening follow-ups** are already documented as Known issues in
the plane pages and are recorded here for completeness — neither is a live
vulnerability:

1. **Coarse heartbeat principal binding.** `AddonPairingService.heartbeat`
   authorizes any `service` principal in the pairing's tenant matched by add-on
   *name*, rather than verifying `operator.sub == pairing.service_account_sub`.
   #3027 added `service_account_sub` precisely to enable the tighter check; it is
   not yet wired. Impact is low (a same-tenant service principal is already a
   trusted, operator-provisioned identity, and a heartbeat only stamps
   `last_seen_at`), so this is a hardening item, not a defect. Tracked in
   [`addon-pairing.md`](../codebase/addon-pairing.md) §Known issues and
   [`addon-step-events.md`](../codebase/addon-step-events.md) §Known issues.
2. **clientId recovery is username-derived.** `Operator.client_id` is stripped
   from the `service-account-<clientId>` marker, not a dedicated `azp` claim. A
   realm that stopped emitting the marker would drop linkage — but fail-open into
   the pre-#3028 independent-audit-row behaviour, never a wrong subtree. Tracked
   in [`addon-parent-linkage.md`](../codebase/addon-parent-linkage.md) §Known
   issues.

If either follow-up is promoted to scheduled work, file it as a Task under
Initiative #2900 at that time; this review does not pre-file speculative tickets.

## References

- Initiative #2900; Tasks #3025 / #3026 / #3027 / #3028 / #3030.
- [`docs/codebase/addon-contract.md`](../codebase/addon-contract.md) — the
  versioned contract + proof plane this review accompanies.
- `service-principal-grants.md` — the per-op grant mechanism Axis 1 relies on.
- v0.1-spec §6 (synchronous append-only audit), §7 (approval is a human
  decision); CLAUDE.md postulate 5 (narrow-waist discipline).
