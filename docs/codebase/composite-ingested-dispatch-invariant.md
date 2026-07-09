# Two-world dispatch invariant

Registration-time guard that enforces one half of the two-world operation
model (Goal #2247): **a code-shipped operation never dispatches through an
ingested catalog row.**

## Overview

`endpoint_descriptor.source_kind` partitions every operation into three
kinds (DB CHECK, `db/models.py`):

- `ingested` — raw L1 primitives from a spec (`GET:/vcenter/datastore`),
  landed only after an operator runs `meho connector ingest`.
- `typed` — self-contained code, transport via the connector class's own
  session, registered at connector init.
- `composite` — hand-authored orchestration over other code-shipped ops.

A `typed`/`composite` op is *code-shipped*: it works the moment the image
boots. Its correctness must not depend on mutable per-deploy catalog state
(is a sub-op ingested here? enabled here? schema-matched here?). The
invariant makes that impossible to regress: at registration, no
code-shipped op's declared sub-op may resolve to an `ingested` row.

## Key types

`operations/composite_invariant.py`:

- `IngestedDispatchDependencyError` — raised at connector-registration
  (lifespan) when a code-shipped op's declared sub-op resolves to an
  `ingested` descriptor row.
- `assert_no_ingested_dispatch_dependency(op_id, connector_id, sub_op_ids)`
  — the per-op primitive. Parses `connector_id` into `(product, version,
  impl_id)`, then probes `endpoint_descriptor` for each raw sub-op; a
  built-in/global (`tenant_id IS NULL`) row with `source_kind='ingested'`
  is a violation. Enablement is not filtered. `*.composite.*` recursion
  sub-ops are skipped (registrar-guaranteed, never ingested).
- `assert_registered_composites_have_no_ingested_dispatch()` — the
  platform-wide sweep over the composite-backing registry
  (`operations/composite_backing.py`). Connector-agnostic: a connector is
  covered the moment it registers a backing.

## Control flow

The sweep runs at the tail of `run_typed_op_registrars`
(`operations/typed_register.py`), invoked from the FastAPI lifespan after
every connector has registered its typed/composite rows. It runs on both
the real registrar pass and the amortized snapshot-replay test path,
because the verdict is a function of live descriptor state. A violation
propagates as a lifespan crash — the same crash-loud posture the registrar
runner already takes for a registration bug.

## Why keyed on the resolved `source_kind`

The invariant does not enumerate `METHOD:/path` shapes or hard-code
connector prefixes. It keys purely on what a declared sub-op *resolves to*.
A sub-op absent on this deploy is not this invariant's concern (absence is
the `composite_l2_missing` failure class the retired apparatus handles); a
sub-op resolving to `composite`/`typed` is allowed; only `ingested` is a
violation.

## Known issues / sequencing

github's `gh.composite.pr_status_summary` and the vmware composites still
declare raw ingested L2 sub-ops today; they are migrated to direct-session
sub-calls in Initiative #2248 (task #2249). github is folded into this one
shared check automatically because it registers a composite backing; its
bespoke import-time `UnbackedEnabledCompositeError` guard and the vmware
dispatch-time preflight are retired in #2259. Until #2249 lands, a deploy
that has ingested the gh catalog would trip this invariant on reboot — the
intended fail-closed signal that the composite must be migrated.

## References

- Goal #2247 (two-world op model), Initiative #2248, Task #2252.
- `operations/composite_backing.py` — the L2-dependency registry the sweep
  reads.
- `connectors/github/composites/_register.py` — the gh-only guard folded in.
