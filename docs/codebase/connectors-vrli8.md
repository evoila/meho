# Connector: vrli8 (vRLI 8.x, legacy dual-impl of `vrli`)

## Overview

The `vrli-vrli8` connector makes **vRealize Log Insight 8.x / VMware Aria Operations
for Logs 8.x** (the log-management tier of a VCF-5.x-era estate) dispatchable under
the `(product="vrli", version="8.0", impl_id="vrli-vrli8")` registry triple. It is
the **legacy dual-impl** of `product="vrli"` filed under the legacy migration-source
connector-coverage initiative ([#3056](https://github.com/evoila/meho/issues/3056),
task #3068): vRLI 8.x is a migration **source** logging estate evoila brings
customers *off*, so MEHO must read and inventory it during discovery + onboarding.

Source: `backend/src/meho_backplane/connectors/vrli8/`. The modern impl is
[`connectors-vcf-logs.md`](connectors-vcf-logs.md) (`vrli-rest`, `>=9.0,<10.0`). This
is the **5th real two-impl case** (after `fleet`, `vcfa`, `sddc`, `vrops`).

## Why a thin subclass (not a standalone copy)

The vRLI `/api/v2/*` surface is **identical** on 8.x and 9.x — the v2 API shipped in
vRLI 8.0 and is stable through 8.18.x (the 8.12 vRealize→Aria rebrand is name-only):
same `POST /api/v2/sessions` → `Authorization: Bearer <sessionId>` auth, same
unauthenticated `GET /api/v2/version` fingerprint, same
`/api/v2/events/{constraints}` query, same `{401, 440}` session-expiry recovery. So
`Vrli8Connector` **subclasses**
`meho_backplane.connectors.vcf_logs.connector.VcfLogsConnector` and inherits the
session mint, the token cache, the `invalidate_session` / `invalidate_credentials`
(#2067) recovery, the profile-derived fingerprint, and the `vrli.event.query` typed
handler — overriding **only** the registration triple and the version band. Unlike
vROps 8.x, the `Bearer` scheme is unchanged across the boundary, so there is **no
auth override at all** (the subclass body is five class attributes).

The dispatcher rebinds the inherited `event_query` handler against the
resolver-chosen instance (`is_unbound_method` over `Vrli8Connector.__mro__` — the
bind9-E2E subclass pattern), so it runs on a `Vrli8Connector` instance with the 8.x
band.

## Dual-impl resolution (`vrli` = 5th two-impl case)

| Impl | Class | Band | Wildcard |
|---|---|---|---|
| modern `vrli-rest` | `VcfLogsConnector` | `>=9.0,<10.0` | **owns** `("vrli","","")` |
| legacy `vrli-vrli8` | `Vrli8Connector` | `>=8.0,<9.0` | none |

The bands are **disjoint**. The **modern** `vcf_logs` owns the `("vrli","","")`
wildcard, so `vrli-vrli8` registers **only** its versioned triple (the *fleet
inversion*): an *unfingerprinted* `vrli` target resolves to modern. **In practice a
non-issue**: `GET /api/v2/version` returns a real five-part `8.x` version string, so
a probed vRLI 8.x target fingerprints straight into `>=8.0,<9.0` and resolves here.
Matrix pinned in
[`test_connectors_vrli8_dual_impl_resolution.py`](../../backend/tests/test_connectors_vrli8_dual_impl_resolution.py).

## Scope: the modern typed read op, reused

The 8.x impl reuses the modern sibling's one typed op verbatim
(`source_kind="typed"`, fresh-boot with zero catalog ingest) — the registrar
(`register_vrli8_typed_operations`) upserts it under the `(vrli, 8.0, vrli-vrli8)`
triple:

| Group | Op | Endpoint |
|---|---|---|
| `vrli-events` | `vrli.event.query` | `GET /api/v2/events/{constraints}` |

The op_id and group key are shared with the modern impl on purpose (distinct
`endpoint_descriptor` / `OperationGroup` rows, scoped by the full connector triple).
The modern connector types only the event query; the rest of its browse surface
(hosts / content-packs / alerts / fields) is **generic-ingested** from an
internally-authored `vcf-logs-9.0/openapi.yaml`, not a VMware download. 8.x has no
committable spec to ingest, so a wider typed inventory surface is a documented
follow-up on #3068, not shipped speculatively.

## Spec reconcile lane

[`test_connectors_vrli8_spec_reconcile.py`](../../backend/tests/test_connectors_vrli8_spec_reconcile.py)
is an **op-set drift guard**, not a path pin: the subclass introduces no new paths
(the vRLI `/api/v2/*` surface lives on the modern `vcf_logs` module and is already
guarded by the modern lane). The lane pins that the 8.x impl reuses exactly the
modern typed op-set (`{vrli.event.query}`) and adds no `_*_PATH` constants of its
own. Evidenced exclusion (vRLI 8.x publishes no committable spec — a per-appliance
runtime Swagger UI at `/rest-api` only): the `vrli-vrli8` entry of
[`spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md).

## Key types

- **`Vrli8Connector`** (`connector.py`) — `VcfLogsConnector` subclass.
  `product="vrli"`, `version="8.0"`, `impl_id="vrli-vrli8"`,
  `supported_version_range=">=8.0,<9.0"`, `priority=1`. No method overrides.
  `connector_id` `"vrli-vrli8-8.0"` parses back to `("vrli", "8.0", "vrli-vrli8")`.
- Target shape, credential loader, and the `VRLI_EXECUTION_PROFILE`-derived session /
  fingerprint / `{401,440}` expiry set are **inherited** from the modern sibling
  (`meho_backplane.connectors.vcf_logs`), keyed under `product_label="vrli"`.
- Canonical constants (`__init__.py`): `VRLI8_PRODUCT`, `VRLI8_VERSION`,
  `VRLI8_IMPL_ID`, `VRLI8_CONNECTOR_ID`.

## Known issues / follow-ups

- **Live dispatch not verified against a real appliance.** No vRLI 8.x lab was
  dialled; the inherited event query is unit-tested end-to-end (respx). Live
  verification is the deferred tail.
- **Typed inventory breadth is a follow-up.** The event query is the 8.x typed
  surface; typed hosts / content-packs / alerts / fields (the migration-discovery
  inventory) would be a follow-up if a concrete need lands — the modern sibling
  serves these via ingested 9.0 catalog, which 8.x cannot use (no committable spec).
- **Unfingerprinted targets resolve to modern** (wildcard owned by `vrli-rest`); a
  probed 8.x target fingerprints its real version and resolves here.

## References

- Task: <https://github.com/evoila/meho/issues/3068>
- Parent initiative: <https://github.com/evoila/meho/issues/3056>
- Dual-version policy of record: <https://github.com/evoila/meho/issues/3033>
- Tests: [`test_connectors_vrli8_auth.py`](../../backend/tests/test_connectors_vrli8_auth.py)
  (inherited Bearer auth + fingerprint),
  [`test_connectors_vrli8_dual_impl_resolution.py`](../../backend/tests/test_connectors_vrli8_dual_impl_resolution.py)
  (resolution matrix),
  [`test_connectors_vrli8_spec_reconcile.py`](../../backend/tests/test_connectors_vrli8_spec_reconcile.py)
  (op-set drift guard).
- Modern sibling: [`connectors-vcf-logs.md`](connectors-vcf-logs.md).
- Sibling legacy connector (same session-arc): [`connectors-vrops8.md`](connectors-vrops8.md).
