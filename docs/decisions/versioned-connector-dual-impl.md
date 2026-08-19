# Versioned connectors keep both a legacy and a modern implementation (decision)

**Status:** decided — operator determination of record; standing policy for all connectors
**Date:** 2026-08-19
**Goal:** [#221](https://github.com/evoila/meho/issues/221) — G0 foundational substrate (connector base + fingerprint resolution)
**Initiative:** [#3033](https://github.com/evoila/meho/issues/3033) — dual-version connectors, vcf-fleet the first real two-impl case
**Surfaced by:** the [#2993](https://github.com/evoila/meho/issues/2993) vcf-fleet spec-availability hunt — VCF 9 replaced the vRSLCM 8.x `/lcm/*` lifecycle surface with a brand-new `/fleet-lcm/v1/*` service, forcing the question of which one the `fleet` connector should target
**Task:** [#3035](https://github.com/evoila/meho/issues/3035) (this decision)

## The determination

> When a vendor bifurcates an API across product versions — a new surface for
> the new major, the old surface still answering on legacy appliances — MEHO
> **carries both connector implementations**. The modern impl advertises the new
> version band and is what new appliances resolve to; the legacy impl advertises
> the old band and keeps existing appliances working. The resolver picks one per
> target by fingerprint. **We do not force-migrate to a single surface**, and we
> do not delete the legacy impl when we add the modern one.

Operator determination, 2026-08-19:

> "We should have both versions. The 9.x and onward should use the newest APIs,
> but we should also keep the version designed for legacy. Do this for all such
> cases."

## Why

Two independent pressures point the same way:

- **Agents/operators on legacy appliances must keep working.** A standalone
  Aria Suite Lifecycle 8.x appliance only speaks `/lcm/lcops/api/v2/*`. Dropping
  that impl to chase the VCF 9 surface would strand every pre-9 target.
- **New appliances should use the current, vendor-supported surface.** VCF 9's
  `/fleet-lcm/v1/*` is the documented, publicly-specified (Apache-2.0) API; the
  legacy `/lcm/*` answers on 9.0 only as back-compat and is not where the vendor
  is investing. New targets should land on the modern impl.

This is not a new architectural idea — it is [postulate 2 in
`CLAUDE.md`](../../CLAUDE.md) ("connectors are versioned and can have multiple
implementations against the same product … the resolver picks one per target per
dispatch") made into standing policy, with the explicit refinement: **adding the
modern impl never removes the legacy one.**

## The mechanics of record (verified 2026-08-19)

The multi-impl machinery is fully built and unit-tested; this decision commits us
to actually using it.

- **Registry** — `backend/src/meho_backplane/connectors/registry.py`: the v2
  table is keyed on the 3-tuple `(product, version, impl_id)`; classes
  self-register at import via `register_connector_v2(...)`.
- **Per-impl advertisement** — the class attrs on
  `backend/src/meho_backplane/connectors/base.py:82-97`: `product`, `version`,
  `impl_id`, **`supported_version_range`** (a PEP 440 `SpecifierSet`, e.g.
  `">=9.0,<10.0"` — this is where an impl declares which target versions it
  serves), and `priority`.
- **Resolution** — `backend/src/meho_backplane/connectors/resolver.py`:
  `resolve_connector(target)` filters candidates by `product` then by
  `supported_version_range` containing the target's fingerprinted version, then
  runs a tie-break ladder: **versioned beats wildcard → dispatch tier
  (`none` hand-coded > `profiled` > `bare` shim) → most-specific-version (smallest
  bounded span) → operator/tenant `preferred_impl_id` → class `priority`**.
  Overlapping bands are therefore resolved automatically (the narrower band wins),
  and an operator can always pin the other impl with `target.preferred_impl_id`.

**vcf-fleet (#3033) is the first real two-impl case.** Until now no product
registered two distinct impls — the `vmware-pyvmomi-7.0` / `vmware-rest-9.0`
pairing described in `CLAUDE.md` is illustrative and **not in code** (only
`vmware-rest-9.0` exists). The resolver ladder is unit-tested but has never
arbitrated two registered classes for one product; #3033 exercises it for real
and ships the resolution test that proves it.

## How to add the modern impl (checklist for "all such cases")

1. **Pin the modern spec.** If the vendor publishes it (e.g. Apache-2.0 in
   `vmware/vcf-api-specs`), pin it to the shelf with the OSS-provenance signoff
   form; otherwise the vendor-licensed form. Add a **reconcile lane** so the
   modern surface is guarded against drift the day it lands.
2. **Register a *dispatchable* class.** A bare runtime `meho connector ingest`
   alone yields a non-dispatchable `GenericRestConnector` shim. Dispatchability
   needs either a hand-coded `HttpConnector` subclass (the `VmwareRestConnector`
   pattern — its ingested `source_kind="ingested"` ops ride the class's
   `auth_headers`) or a stamped `ExecutionProfile` (`ProfiledRestConnector`).
3. **Advertise the new band** via `supported_version_range` on the modern class,
   and **broaden or lower the legacy band** so the two don't fight — rely on
   most-specific-match rather than fragile relabeling of the legacy
   `connector_id`.
4. **Prove it** with a resolver test: a new-version target resolves the modern
   impl, an old-version target resolves the legacy impl, and `preferred_impl_id`
   overrides.

## Scope / non-goals

- This decision is about **legacy↔modern splits of the same product**. Net-new
  vendor surfaces we do not connector yet (e.g. VCF 9's `sddc-lcm`,
  `vcf-installer`, `vsan-data-protection`) are coverage backlog, not splits.
- It does not mandate live-appliance verification as a merge gate for the modern
  impl; a modern impl may ship registered + spec-guarded + reconcile-armed ahead
  of a reachable appliance, with live dispatch verification tracked as a
  follow-up (the bar the legacy `fleet` skeleton already shipped at).
