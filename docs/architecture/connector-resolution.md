# Connector resolution

How MEHO maps a target endpoint to the best registered connector
implementation given the v2 registry keyed on
`(product, version, impl_id)`. Shipped with [G0.6-T2
(#393)](https://github.com/evoila/meho/issues/393); read alongside
[connectors.md](connectors.md) (the v0.2 transitional architecture) and
the upcoming `operations-substrate.md` (G0.6-T10, #400).

## Why a resolver

The shipped G0.2 registry keys connectors by product slug alone — one
class per product. That model breaks as soon as MEHO grows multiple
implementations for the same product:

- `vmware-pyvmomi-7.0` for the SOAP-era 7.x vSphere line.
- `vmware-rest-9.0` for the REST-API 9.x line.

Both connectors share `product = "vmware"` but advertise different
`(version, impl_id)` discriminators. A single-slug registry can hold
exactly one of them.

The v2 registry [`backend/src/meho_backplane/connectors/registry.py`](../../backend/src/meho_backplane/connectors/registry.py)
keys on the three-tuple `(product, version, impl_id)`. The resolver in
[`backend/src/meho_backplane/connectors/resolver.py`](../../backend/src/meho_backplane/connectors/resolver.py)
walks that table to pick the right class for a given target.

## Input shape

`resolve_connector(target)` reads three attributes from the target:

- `target.product` — required. The product slug stored on the Target
  row (`targets.product`). Filters the candidate set.
- `target.fingerprint.version` — optional. The version string from the
  most recent fingerprint (e.g. `"9.0.2"`). Drives the
  `supported_version_range` match; absent fingerprint = no versioned
  connectors are eligible (only v1-style entries with `None` range).
- `target.preferred_impl_id` — optional. Operator override pinning a
  specific implementation when the tie-break ladder can't decide. The
  column is added to the Target model by the [G0.3 amendments
  (#224)](https://github.com/evoila/meho/issues/224); until that lands,
  the resolver reads it via `getattr(..., None)` and tolerates absence.

### Canonical product identity

`target.product` is the single canonical product token a connector
registers under — the same token its `connector_id` round-trips to via
[`parse_connector_id`](../../backend/src/meho_backplane/operations/_lookup.py).
Every shipped connector satisfies
`parse_connector_id(f"{impl_id}-{version}")[0] == product`: the
registry product and the dispatch-derived product are one token, so the
filter above (`target.product`) and the product the dispatcher resolves
for a row never diverge.

This was not always so. Six VCF-suite connectors shipped with a *long*
registry product and a *short* dispatch-/parser-derived product
(`sddc-manager` vs `sddc`, `vcf-automation` vs `vcfa`, `vcf-fleet` vs
`fleet`, `vcf-operations` vs `vrops`, `hetzner-robot` vs `hetzner`, and
`vcf-logs` vs `vrli`), bridged by sanctioned band-aids. vRLI was
realigned in [#1798](https://github.com/evoila/meho/issues/1798); the
remaining five in
[#1814](https://github.com/evoila/meho/issues/1814) (Initiative
[#1810](https://github.com/evoila/meho/issues/1810)). The split is
retired — each connector now carries one short canonical token, and a
target's `product` is that token verbatim.

[`register_connector_v2`](../../backend/src/meho_backplane/connectors/registry.py)
logs a WARN when a registration's product does not round-trip; with the
family aligned this check is silent at boot and stays advisory until
[#1816](https://github.com/evoila/meho/issues/1816) promotes it to a
hard fail.

## Tie-break ladder

When two or more connectors advertise support for a target's
`(product, version)` pair:

### Step 0 — hand-rolled class beats auto-shim

The [spec-ingestion pipeline](spec-ingestion.md) auto-registers a
`GenericRestConnector` shim per ingested `(product, version, impl_id)`
so a freshly ingested spec resolves before any per-product class
exists. When a shipped hand-rolled `Connector` subclass and an
auto-shim are both candidates for the same `(product, version)` label,
the hand-rolled class wins and every `GenericRestConnector` candidate
is dropped — *before* the specificity step below.

Without this rung, a stray ingest under a novel `impl_id` shadows a
shipped connector: the shim's
[`derive_supported_version_range`](../../backend/src/meho_backplane/operations/ingest/connector_registration.py)
pins a *narrower* range around the exact ingested version than a
hand-rolled class's broad range, so the shim would win
most-specific-version-match before the hand-rolled class's `priority`
is ever read ([#1750](https://github.com/evoila/meho/issues/1750), a
v0.15.0 dogfood signal). The invariant is unconditional: a hand-rolled
class always outranks an auto-shim for the same label, independent of
version-range span or `priority`.

When only auto-shims exist for a label (a genuine catalog-first
staging connector not yet replaced by a hand-rolled subclass), this
rung is a no-op and the shim still resolves.

### Step 1 — most-specific-version-match wins

Specificity is measured by the size of the bounded interval the
connector's `supported_version_range` covers. Bounded ranges are
ranked by `(upper - lower)` distance; smaller is more specific.

A bounded range (`>=X,<Y`) is always more specific than a half-bounded
range (`>=X` alone); a half-bounded range is more specific than no
range advertised (`None`).

### Step 2 — operator/tenant preference

If step 1 leaves more than one candidate **and**
`target.preferred_impl_id` is set, the candidate whose `impl_id`
matches that override wins.

This is a tie-break for **specificity ties only** — when a more
specific candidate exists, it wins at step 1 and operator preference
is moot. The pattern is "operator nudges the default when two impls
are equally good", not "operator overrides any system decision".

### Step 3 — connector class priority

`Connector.priority: int = 0` class attribute, set by the connector
subclass. Higher wins. Use this when two impls share the same range
and no operator preference has been recorded (e.g. one is the
preferred default for the fleet, the other is a fallback).

### After all three steps

If two or more candidates remain, the resolver raises
`AmbiguousConnectorResolution` with the candidate list. Operators
break the tie by setting `target.preferred_impl_id` on the affected
Target row.

## Three worked examples

### Example 1 — narrow beats wide

```python
register_connector_v2(product="vmware", version="9.0",
                     impl_id="vmware-rest", cls=VmwareRest9)
# class VmwareRest9: supported_version_range = ">=9.0,<10.0"

register_connector_v2(product="vmware", version="legacy",
                     impl_id="vmware-wide", cls=VmwareWide)
# class VmwareWide: supported_version_range = ">=6.5,<10.0"

target = Target(product="vmware",
                fingerprint=FingerprintResult(version="9.0.2", ...))

resolve_connector(target)  # → VmwareRest9
```

Both connectors accept `9.0.2`. `VmwareRest9` spans 1.0 minor
versions; `VmwareWide` spans 3.5. Narrower wins at step 1.

### Example 2 — operator preference breaks a tie

```python
register_connector_v2(product="vmware", version="rest",
                     impl_id="vmware-rest", cls=VmwareWide)
register_connector_v2(product="vmware", version="pyvmomi",
                     impl_id="vmware-pyvmomi", cls=VmwarePyvmomi)
# Both advertise supported_version_range = ">=6.5,<10.0"

target = Target(product="vmware",
                fingerprint=FingerprintResult(version="9.0.2", ...),
                preferred_impl_id="vmware-pyvmomi")

resolve_connector(target)  # → VmwarePyvmomi
```

Step 1 leaves both candidates (same specificity score). Step 2 reads
`preferred_impl_id="vmware-pyvmomi"` and selects the matching
candidate.

### Example 3 — class priority as last resort

```python
register_connector_v2(product="vmware", version="a",
                     impl_id="vmware-a", cls=VmwareWide)
# class VmwareWide: supported_version_range = ">=6.5,<10.0"; priority = 0

register_connector_v2(product="vmware", version="b",
                     impl_id="vmware-b", cls=VmwareHighPriority)
# class VmwareHighPriority: supported_version_range = ">=6.5,<10.0"; priority = 10

target = Target(product="vmware",
                fingerprint=FingerprintResult(version="9.0.2", ...))
                # No preferred_impl_id set.

resolve_connector(target)  # → VmwareHighPriority
```

Step 1 ties (same range). Step 2 doesn't fire (no operator override).
Step 3 reads `priority` from each candidate class; higher wins.

## Backward compatibility

The shipped v1 entry point
`register_connector(product, cls)` still works — and it writes the
same class to **both** registry layers (v1 as `product → cls`, v2 as
`(product, "", "") → cls`). Existing `VaultConnector` and
`KubernetesConnector` registrations participate in v2 resolution
without code change.

A connector class whose `supported_version_range` is left at the
default `None` (the v0.6-T3 class attribute) matches any target
version — including targets without a fingerprint at all. This is the
behaviour the shipped Vault and Kubernetes connectors get for free.

## Error shapes

- `NoMatchingConnector` (subclass of `LookupError`) — zero candidates
  after filtering. Message names the `(product, version)` pair that
  came up empty so operators can spot a registration gap.
- `AmbiguousConnectorResolution` (subclass of `LookupError`) — two or
  more candidates after the full ladder. Carries a sorted
  `candidates: list[tuple[str, str, str]]` field for diagnostics; the
  message names the candidate set + the remediation step ("set
  `target.preferred_impl_id` to one of them") so operators can
  resolve the ambiguity from the message alone.

Both are `LookupError` subclasses so existing exception-handling
patterns (broad `except LookupError`) keep working without surface
changes.

### Shared exception-to-label helper

[`resolve_connector_or_label(target)`](../../backend/src/meho_backplane/connectors/resolver.py)
wraps `resolve_connector(target)` and translates the two exception
classes above to structured `(cls, label, exception_message)`
tuples. Both the dispatcher's connector-resolution step
([`_resolve_connector_instance`](../../backend/src/meho_backplane/operations/dispatcher.py))
and the `/api/v1/targets/{name}/probe` route consult this helper —
the single source of truth ensures the two surfaces never disagree on
whether a target's connector resolves. The pre-G0.14 asymmetry
(consumer feedback signal 19 in `claude-rdc-hetzner-dc#697` —
`/probe` consulted the v1 `get_connector` lookup while dispatch
consulted v2 `resolve_connector`) is closed by this helper.

## References

- [v0.1-spec L267-292 "Versioned connectors + targets"](../planning/v0.2-decisions.md) — fingerprint shape.
- [Initiative #388 G0.6](https://github.com/evoila/meho/issues/388) — substrate scope.
- [Task #393 G0.6-T2](https://github.com/evoila/meho/issues/393) — this resolver.
- [Task #394 G0.6-T3](https://github.com/evoila/meho/issues/394) — Connector ABC `version` / `impl_id` / `supported_version_range` / `priority` class attrs.
- [Task #224 G0.3](https://github.com/evoila/meho/issues/224) — Target model + `preferred_impl_id` column.
- PEP 440 — [version + specifier grammar](https://peps.python.org/pep-0440/) — the `packaging` library implementation backs `supported_version_range` matching.
