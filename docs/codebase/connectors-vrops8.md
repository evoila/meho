# Connector: vrops8 (vROps 8.x, legacy dual-impl of `vrops`)

## Overview

The `vrops-vrops8` connector makes **vRealize Operations 8.x / VMware Aria
Operations 8.x** (the monitoring tier of a VCF-5.x-era estate) dispatchable under
the `(product="vrops", version="8.0", impl_id="vrops-vrops8")` registry triple. It
is the **legacy dual-impl** of `product="vrops"` filed under the legacy
migration-source connector-coverage initiative
([#3056](https://github.com/evoila/meho/issues/3056), task #3067): vROps 8.x is a
migration **source** monitoring estate evoila brings customers *off*, so MEHO must
read and inventory it during discovery + onboarding.

Source: `backend/src/meho_backplane/connectors/vrops8/`. The modern impl is
[`connectors-vcf-operations.md`](connectors-vcf-operations.md) (`vrops-rest`,
`>=9.0,<10.0`). This is the **4th real two-impl case** (after `fleet`, `vcfa`,
`sddc`).

## Why a thin subclass (not a standalone copy)

Unlike the sibling legacy connectors `vcd` (#3057, net-new), `vcfa-vra8` (#3058,
divergent two-step CSP→IaaS auth), and `sddc-vcf5` (#3059, divergent read scope),
the vROps 8.x Suite API is **identical** to 9.x — same
`POST /suite-api/api/auth/token/acquire` session mint, same
`GET /suite-api/api/versions/current` fingerprint, same
`/suite-api/api/{alerts, resources/query, resources/stats/latest}` reads (all stable
since vROps 6.6/7.0; the 8.10 vRealize→Aria rebrand is name-only, `major` stays 8).
So `Vrops8Connector` **subclasses**
`meho_backplane.connectors.vcf_operations.connector.VcfOperationsConnector` and
inherits the acquire flow, the `auth_model` gate, the per-target session cache, the
`invalidate_session` / `invalidate_credentials` (#2067) recovery, the fingerprint,
and all four typed read handlers — overriding only the registration triple, the
version band, and the token **scheme** (below). Copying ~600 identical lines would
be cargo-cult, not discipline (postulates 2 + 3).

The dispatcher rebinds an inherited handler against the resolver-chosen instance
(`is_unbound_method` walks `Vrops8Connector.__mro__` and matches the base-class
function object — the documented bind9-E2E subclass pattern), so the inherited
handlers run on a `Vrops8Connector` instance with the 8.x scheme and band.

## The one behavioural delta — the token scheme

The 9.x connector presents `Authorization: OpsToken <token>` — the 9.x-native scheme
(#2395). An 8.x appliance predates the `OpsToken` alias and accepts only the
token-era `vRealizeOpsToken` scheme (stable across the whole 8.x line, confirmed
against the vendor 8.18 API programming guide). `Vrops8Connector.auth_headers`
therefore re-labels the scheme on the header the base returns:

```python
headers = dict(await super().auth_headers(target, operator))
_scheme, _sep, token = headers.get("Authorization", "").partition(" ")
headers["Authorization"] = f"vRealizeOpsToken {token}"
return headers
```

The security-sensitive `auth_model` gate and the token mint stay **single-sourced**
on the base — the override only alters the presented scheme, so a future change to
the base auth path is inherited rather than silently forked. (The modern connector
was left untouched; promoting its module-level `_OPS_TOKEN_SCHEME` to a class
attribute would have tripped the 600-line file-size gate on an already-oversized
file — the subclass-local override is the more surgical seam.)

## Dual-impl resolution (`vrops` = 4th two-impl case)

| Impl | Class | Band | Wildcard |
|---|---|---|---|
| modern `vrops-rest` | `VcfOperationsConnector` | `>=9.0,<10.0` | **owns** `("vrops","","")` |
| legacy `vrops-vrops8` | `Vrops8Connector` | `>=8.0,<9.0` | none |

The bands are **disjoint** (no specificity tie). The **modern** `vcf_operations` owns
the `("vrops","","")` wildcard (it shipped first), so `vrops-vrops8` registers **only**
its versioned triple — the *inversion* of the `fleet` case. Consequence: an
*unfingerprinted* `vrops` target resolves to modern. **In practice this is a
non-issue**: `GET /suite-api/api/versions/current` returns a *real* product
`releaseName` (a dotted release, e.g. `8.18.0.24178391`), so a probed vROps 8.x
target normally fingerprints straight into `>=8.0,<9.0` and resolves here without an
operator-asserted version — unlike `vcfa-vra8`, whose fingerprint yields only an
API-date label. **Caveat (residual risk):** the inherited fingerprint stores
`releaseName` raw, so resolution depends on it being PEP-440-parseable; a
non-parseable value falls back *open* to the modern-owned wildcard (which presents
the wrong 9.x `OpsToken` scheme). The observed `releaseName` is always dotted-numeric
(and the modern connector relies on the same field), so this is not expected — but it
is unverified live; the operator escape hatch is to pin `version` / `preferred_impl_id`.
Both the happy matrix and the open-fallback limitation are pinned in
[`test_connectors_vrops8_dual_impl_resolution.py`](../../backend/tests/test_connectors_vrops8_dual_impl_resolution.py).

## Scope: the modern audited typed read set, reused

The 8.x impl reuses the modern sibling's audited **4-op typed read set** verbatim
(`source_kind="typed"`, working on a fresh boot with zero catalog ingest) — the
registrar (`register_vrops8_typed_operations`) upserts the same op_ids under the
`(vrops, 8.0, vrops-vrops8)` triple:

| Group | Op | Endpoint |
|---|---|---|
| `vrops-liveness` | `vrops.liveness` | `GET /suite-api/api/versions/current` |
| `vrops-alert-triage` | `vrops.alert.list` | `GET /suite-api/api/alerts` |
| `vrops-resource-query` | `vrops.resource.query` | `POST /suite-api/api/resources/query` |
| `vrops-resource-stats` | `vrops.resource.stats` | `GET /suite-api/api/resources/stats/latest` |

Op ids are **shared** with the modern impl on purpose (the same logical operation
dispatched to whichever impl the target resolves to); `endpoint_descriptor` rows are
keyed by `(product, version, impl_id, op_id)`, so the 8.x rows are distinct. Group
keys are reused too — `OperationGroup` rows are scoped by
`(product, version, impl_id, group_key)`, so the 8.x groups are distinct rows. The
read payloads pass through the JSONFlux reducer largely unparsed, so 8.x
response-shape drift is absorbed rather than parsed.

The broader ingested browse catalog the modern connector also exposes (via the 9.x
OA3) is **not** available on 8.x (no committable 8.x spec to ingest) — a documented
follow-up, not shipped speculatively.

## Spec reconcile lane

[`test_connectors_vrops8_spec_reconcile.py`](../../backend/tests/test_connectors_vrops8_spec_reconcile.py)
is an **op-set drift guard**, not a path pin: the subclass introduces no new paths
(they are the modern connector's constants, already armed against the pinned
`vcf-operations-9.0` OpenAPI 3.0 served set by the modern lane — path existence is
stable across 8.x↔9.x). The lane pins that the 8.x impl reuses exactly the modern
typed op-set and adds no `_*_PATH` constants of its own, so a future 9.x-only op
cannot silently inherit into the 8.x surface. Evidenced exclusion (8.x publishes only
a per-instance Swagger 2.0 doc, moot for path coverage): the `vrops-vrops8` entry of
[`spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md).

## Key types

- **`Vrops8Connector`** (`connector.py`) — `VcfOperationsConnector` subclass.
  `product="vrops"`, `version="8.0"`, `impl_id="vrops-vrops8"`,
  `supported_version_range=">=8.0,<9.0"`, `priority=1`. Overrides `auth_headers`
  (scheme swap) and nothing else. `connector_id` `"vrops-vrops8-8.0"` parses back to
  `("vrops", "8.0", "vrops-vrops8")`.
- Target shape + credential loader are **inherited** from the modern sibling
  (`meho_backplane.connectors.vcf_operations.session`), keyed under
  `product_label="vrops"` — the credential contract is product-level.
- Canonical constants (`__init__.py`): `VROPS8_PRODUCT`, `VROPS8_VERSION`,
  `VROPS8_IMPL_ID`, `VROPS8_CONNECTOR_ID`.

## Known issues / follow-ups

- **Live dispatch not verified against a real appliance.** No vROps 8.x lab was
  dialled; the scheme swap + inherited handlers are unit-tested end-to-end (respx).
  Live verification (incl. confirming the `resources/stats/latest` payload shape on a
  live 8.x target) is the deferred tail.
- **Ingested browse breadth is 9.x-only.** The typed 4-op core is the 8.x surface; a
  wider inventory would need typed ops or an 8.x spec to ingest (none exists).
- **Unfingerprinted targets resolve to modern** (wildcard owned by `vrops-rest`); a
  probed 8.x target fingerprints its real version and resolves here.

## References

- Task: <https://github.com/evoila/meho/issues/3067>
- Parent initiative: <https://github.com/evoila/meho/issues/3056>
- Dual-version policy of record: <https://github.com/evoila/meho/issues/3033>
- Tests: [`test_connectors_vrops8_auth.py`](../../backend/tests/test_connectors_vrops8_auth.py)
  (scheme swap + inherited auth/fingerprint),
  [`test_connectors_vrops8_dual_impl_resolution.py`](../../backend/tests/test_connectors_vrops8_dual_impl_resolution.py)
  (resolution matrix),
  [`test_connectors_vrops8_spec_reconcile.py`](../../backend/tests/test_connectors_vrops8_spec_reconcile.py)
  (op-set drift guard).
- Modern sibling: [`connectors-vcf-operations.md`](connectors-vcf-operations.md).
- Sibling legacy connector (same session-arc): [`connectors-vrli8.md`](connectors-vrli8.md).
