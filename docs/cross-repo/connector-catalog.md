<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Connector-spec catalog

> Operator-facing reference for the curated connector-spec catalog
> (Goal [#214](https://github.com/evoila/meho/issues/214) raw-REST ingest
> on-ramp; Task [#743](https://github.com/evoila/meho/issues/743)). The
> ingestion mechanics live in
> [`connector-ingestion.md`](connector-ingestion.md); this doc is the map
> of *which* spec to ingest for each shipped connector and *where* the
> vendor hosts it.

## Why this exists

MEHO's generic-ingestion path (the G0.7 pipeline) supports the full vendor
REST surface — but only once an operator ingests the vendor's OpenAPI
spec. Doing that by hand means knowing four things per connector: that a
connector is generic-ingestable at all, where the vendor publishes its
spec, which `spec.info.version` maps to which release, and which version
label aligns with a registered connector class. The v0.3.0 RDC dogfood
showed that friction directly: operators concluded "the vmware connector
is 13 composites and that's it" because the ingest on-ramp was implicit.

The catalog makes that knowledge a machine-readable artifact: one entry
per `(product, version)` with the recommended spec source(s) and the
connector class that covers the label.

## Where the catalog lives

The data file ships as **package data** at
[`backend/src/meho_backplane/operations/ingest/catalog.yaml`](../../backend/src/meho_backplane/operations/ingest/catalog.yaml),
colocated with its loader
([`catalog.py`](../../backend/src/meho_backplane/operations/ingest/catalog.py)).

> **Note on the path.** Task #743 first named `docs/connector-specs/
> catalog.yaml`, but the backend image build context is `backend/` and the
> wheel only packages `src/meho_backplane`, so a repo-root `docs/` file is
> not present in a deployed container. The catalog must be readable at
> startup and over the API, so it ships as package data resolved through
> `importlib.resources` — the same pattern as `alembic.ini` / `alembic/`
> (see `find_alembic_ini` in `db/migrations.py`). This document is the
> discoverable pointer in its place.

The backplane loads + validates the catalog at startup (a malformed
catalog crashes the lifespan, so CI's app-boot smoke fails) and serves it
read-only at `GET /api/v1/connectors/catalog`.

## Entry schema

Each entry is validated by `ConnectorSpecEntry`:

| Field | Meaning |
|---|---|
| `product` / `version` / `impl_id` | The connector triple. `(product, version)` is unique and is the `--catalog <product>/<version>` lookup key. |
| `requires_connector_class` | The registered connector class `__name__` that covers this version label. A regression test asserts every value is present in `all_connectors_v2()`. |
| `upstream` | Public URL(s) where the vendor hosts the spec. Operators (and the CLI) fetch directly — MEHO does not redistribute. `null` marks a **typed** connector with no ingestable spec. |
| `spec_info_version` | The `spec.info.version` MEHO observed when smoke-testing the spec; PEP 440 when present. `null` until a connector's spec is ingest-verified through MEHO — these are empirical values, not guesses. |
| `spec_info_versions_compatible` | **Optional label-vs-spec opt-in.** List of patterns the validator widens against. Each entry is either a glob (`"1.x"`, `"9.0.x"`) or a PEP 440 specifier set (`">=1.0,<2.0"`). When set, the ingest validator accepts a spec whose `info.version` matches any pattern even if it differs from this row's `version` label. `null` (default) preserves the historical verbatim/major-band check. See "Label-vs-spec decoupling" below. |
| `sha256` | Advisory content hash MEHO smoke-tested against; optional, not enforced. |
| `notes` | Operator context: sharp edges, version quirks, appliance-served vs portal-hosted, curation status. |
| `catalog_ingest` | **Whether `meho connector ingest --catalog` actually works for this row.** `"supported"` (default) — the upstream serves an OpenAPI spec directly; `--catalog` runs end-to-end. `"spec-only"` — the upstream is an HTML developer-portal landing page or an fqdn-templated appliance URL the catalog can't dereference server-side; the operator must fetch the spec and pass via `--spec`. `GET /api/v1/connectors` reads this field to emit an honest `next_step` hint (G0.18-T8 #1361). See "Spec-only entries" below. |

## Current entries

| Product / version | Shape | `catalog_ingest` | Spec source |
|---|---|---|---|
| `vmware` 9.0 | generic | `spec-only` | `vcenter.yaml` + `vi-json.yaml` (appliance-served at `https://<vcenter-fqdn>/`; mirrored on the Broadcom Developer Portal, which serves `text/html`) |
| `sddc-manager` 9.0 | generic | `spec-only` | SDDC Manager API (appliance-served; Broadcom Developer Portal mirror is `text/html`) |
| `harbor` 2.x | generic | `supported` | `goharbor` `api/v2.0/swagger.yaml` — **Swagger 2.0**, needs conversion to OpenAPI 3.x before ingest |
| `nsx` 4.2 | generic | `spec-only` | `/api/v1/spec/openapi/nsx_api.yaml` (appliance-served, `<nsx-mgr-fqdn>`-templated; Broadcom Developer Portal mirror is `text/html`) |
| `gh` v3 | generic | `supported` | `rest-api-description/main/descriptions/api.github.com/api.github.com.json` — OpenAPI 3.0.3, direct-resolvable, `info.version` 1.1.4 (in compat band `1.x.x` — see Label-vs-spec decoupling below), ~700 paths / ~40 tags. Auth: GitHub App installation tokens (or fine-grained PAT). First-day on-ramp: [`github-connector.md`](./github-connector.md); credential setup: [`github-app-credential.md`](./github-app-credential.md). |
| `vault` 1.x | typed | n/a (typed) | none (hand-coded connector) |
| `k8s` 1.x | typed | n/a (typed) | none (typed; per-minor OpenAPI ingest is a future Goal #214 investigation) |
| `bind9` 9.x | typed | n/a (typed) | none (SSH-only; no REST surface) |

`spec_info_version` and `sha256` are populated only after a connector's
spec has been ingest-verified through MEHO, not from desk research. The
`gh/v3` entry carries `spec_info_version: 1.1.4` observed against the
upstream main branch tip on 2026-05-27 — the value comes from a smoke
parse of the live spec, not the GitHub release tag (the public
`rest-api-description` release cadence lags by years; the spec is
regenerated daily on `main` from production, so `main` is the pin).
Refresh as part of each operator-cadenced re-ingest. The other entries
still ship `null`.

## Operator workflow

1. `meho connector catalog list` — print the catalog table: each
   `(product, version)` entry's `impl_id`, the connector class that
   covers it, whether that class is registered on this backplane (`reg`
   column), the observed `spec_info_version`, and notes. Read-only;
   operator role suffices.
2. `meho connector ingest --catalog <product>/<version>` — POST
   `{"catalog_entry": "<product>/<version>"}` to
   `/api/v1/connectors/ingest`; the backplane resolves the entry,
   fills in `product` + `version` + `impl_id` + `specs[]`, and runs
   the standard ingest pipeline. Typed-connector entries (`upstream:
   null`) and fqdn-templated upstream URLs are refused with a
   structured 422 hint pointing at the explicit-quadruple shape.
   `--catalog` is mutually exclusive with the manual
   `--product`/`--version`/`--impl`/`--spec` flags. Add `--dry-run`
   to validate before committing.
3. `meho connector review <connector_id>` → `meho connector enable <id>` —
   vet the LLM-summarised groups and turn the operations on.

REST-native clients (agent runtimes that can't shell out to the
CLI) hit the same path by POSTing the catalog-driven body shape
directly. See the *REST shape* section below.

For an entry the catalog can't ingest directly (typed, or fqdn-templated
upstream such as `nsx`), fall back to the explicit `meho connector ingest
--product … --version … --impl … --spec <url>` form documented in
[`connector-ingestion.md`](connector-ingestion.md), reading the `upstream`
URL(s) from `catalog list`.

## REST shape (G0.14-T9 / #1150)

`POST /api/v1/connectors/ingest` accepts two mutually-exclusive
request shapes. The catalog-driven shape moved server-side in
G0.14-T9 so REST-native agent runtimes (which can't shell out to
the CLI) reach the same ingest path the CLI's `--catalog` flag
hits.

**Catalog-driven shape** — pass `catalog_entry`; the server resolves
the entry against the packaged catalog:

```json
{ "catalog_entry": "vmware/9.0", "dry_run": false }
```

**Explicit-quadruple shape** — pass the resolved triple plus the
spec sources (MCP admin tool and historical clients):

```json
{
  "product": "vmware",
  "version": "9.0",
  "impl_id": "vmware-rest",
  "specs": [{ "uri": "https://example.lab/vcenter.yaml" }]
}
```

A body that sets both shapes fails 422 `catalog_entry_conflict`. A
body that sets neither fails 422 `ingest_request_underspecified`. A
body that sets `catalog_entry` but the entry is unknown / malformed
/ typed-only / fqdn-templated fails 422 with one of the four
classifier codes below — every shape follows the T11
[error-message-shape](../codebase/error-message-shape.md) convention:

| Failure | Code | When |
|---|---|---|
| Reference missing `/` separator | `catalog_entry_malformed` | `"vmware9.0"` |
| Reference well-formed, not in catalog | `catalog_entry_not_found` | `"foo/1.0"` |
| Resolved entry has `upstream: null` | `catalog_entry_typed_connector` | `"vault/1.x"` |
| Resolved entry's upstream has `<...>` placeholders | `catalog_entry_templated_upstream` | `"nsx/4.2"` |
| Both shapes supplied | `catalog_entry_conflict` | shape conflict |
| Neither shape supplied | `ingest_request_underspecified` | empty body |

## Label-vs-spec decoupling (`spec_info_versions_compatible`)

Most catalog rows pin their `version` label to the spec's
`info.version` (e.g. NSX 4.2's catalog label IS the vendor's release
designator that lands in `info.version`). For these the historical
validator behavior — verbatim or major-band match — is exactly right.

A handful of upstreams ship two semantically distinct version
fields: a product-line label and a separate documentation-version
field that drifts independently. The GitHub REST API is the canonical
example — `github.com` calls the API "v3", but the OpenAPI
description's `info.version` is currently `1.1.4`, regenerated daily
on `rest-api-description/main` and bumping minor-version on every
spec edit. Catalog-driven ingest against the live upstream would
fail `spec_label_mismatch: '3' is incompatible with info.version='1.1.4'`
without an opt-in.

`spec_info_versions_compatible` is that opt-in. The catalog row
declares the band of `info.version` values the validator should
accept under its operator-facing label:

```yaml
- product: gh
  version: "3"                            # product-line label
  spec_info_version: "1.1.4"              # observed on first smoke-test
  spec_info_versions_compatible:          # opt-in: accept any 1.x.x
    - "1.x.x"
```

A `1.1.5` or `1.2.0` upstream bump now ingests without a catalog
edit; only a `2.x` breaking change forces an operator decision
(extend the range, cut a new catalog row, or audit the change).

### Pattern syntax

| Form | Example | Meaning |
|---|---|---|
| Glob | `"1.x"` | Any release in the `1.*` band (`>=1,<2`) |
| Glob | `"1.x.x"` | Same as `"1.x"` — extra `.x` segments accepted |
| Glob | `"9.0.x"` | Any release in `9.0.*` (`>=9.0,<9.1`) |
| PEP 440 specifier set | `">=1.0,<2.0"` | The full `packaging.specifiers.SpecifierSet` grammar |
| PEP 440 specifier set | `"~=1.4"` | Any `1.4.*` and forward inside the same major |

Multiple patterns are accepted as any-of (a `1.1.4` matches the band
if any pattern accepts it). Non-PEP-440 spec `info.version` strings
fall through to the historical verbatim check — the opt-in is PEP
440 by design.

### When to use it

- **Use it** when the catalog `version` is a product-line label that
  diverges from `info.version` (GitHub REST, plausibly Stripe API
  versions, vendor APIs whose docs version isn't the same as the
  product line).
- **Don't use it** when the catalog `version` IS the spec's
  `info.version` (vCenter 9.0 vs `info.version=9.0.0.0` — the
  G0.16-T6 Finding 22 / Task #1312 H surface is exactly this
  decision).

The choice is per-connector-family and lives in
[`docs/codebase/api-shape-conventions.md` §9](../codebase/api-shape-conventions.md).

## Spec-only entries (`catalog_ingest: spec-only`)

A handful of upstreams in the curated catalog **cannot drive
`--catalog` ingest end-to-end** even though the row's
`(product, version, impl_id)` is correct. Two distinct shapes:

1. **HTML developer-portal landing pages** — `vmware/9.0` and
   `sddc-manager/9.0` cite the Broadcom Developer Portal
   (`https://developer.broadcom.com/xapis/...`). Those URLs serve
   `text/html`, not OpenAPI YAML/JSON; the route's
   `catalog_entry_upstream_not_spec` 422 fires on any catalog-driven
   ingest. The spec really is published by Broadcom (and served
   directly by a deployed vCenter / SDDC Manager appliance), but the
   public reference page is a portal, not the raw artefact.
2. **Fqdn-templated appliance URLs** — `nsx/4.2`'s first upstream is
   `https://<nsx-mgr-fqdn>/api/v1/spec/openapi/nsx_api.yaml`, with a
   placeholder the backplane can't dereference server-side
   (`catalog_entry_templated_upstream` 422). The Broadcom mirror
   listed alongside it is again `text/html`.

For these rows the catalog row sets `catalog_ingest: spec-only`.
That has two operator-visible consequences:

- **`GET /api/v1/connectors`** — the `state="registered"` row's
  `next_step` hint points at `meho connector ingest --product <p>
  --version <v> --impl <i> --spec <concrete-openapi-uri>` instead
  of the previous `--catalog <product>/<version>` (which would
  422). The rationale calls out the upstream-shape limitation so
  an operator or LLM agent reading the listing understands the
  catalog row isn't broken — the public spec source just isn't
  fetchable.
- **`POST /api/v1/connectors/ingest`** — unchanged from the
  existing G0.14-T9 / G0.15-T2 contract. The route's structured
  422 envelopes (`catalog_entry_upstream_not_spec`,
  `catalog_entry_templated_upstream`) still fire if a caller
  POSTs the catalog-driven shape against a spec-only row. The
  `catalog_ingest` field is the declarative input the **hint**
  reads, not a new validation gate; route behaviour is unchanged.

**Operator workflow for spec-only entries.** Fetch the raw spec
from the appliance the connector targets (vCenter, SDDC Manager,
or NSX Manager) — typically `https://<appliance-fqdn>/api/...` or
`https://<appliance-fqdn>/v1/...`. Save it locally
(`/tmp/vcenter.yaml`, etc.) and pass via the explicit `--spec`
flag:

```bash
meho connector ingest \
    --product vmware --version 9.0 --impl vmware-rest \
    --spec /tmp/vcenter.yaml --spec /tmp/vi-json.yaml
```

The `--product`/`--version`/`--impl` triple is what the catalog
row carries (the `next_step` hint pre-fills it).

**Curator workflow.** A new catalog row whose upstream is a
developer portal page or carries `<placeholder>` characters
should ship `catalog_ingest: spec-only`. If a vendor later
publishes the raw spec at a directly-fetchable URL, flip the
field back to `supported` (or omit it; the default is
`supported`) and update the `upstream` URL in the same commit.

## Adding or updating an entry

1. Add/edit the entry in
   [`catalog.yaml`](../../backend/src/meho_backplane/operations/ingest/catalog.yaml).
2. Set `requires_connector_class` to a class that is actually registered
   (`all_connectors_v2()`), or the regression test
   (`backend/tests/test_operations_ingest_catalog.py`) fails.
3. Populate `spec_info_version` / `sha256` only from a real
   MEHO-verified ingest of that spec.
4. Run `uv run pytest tests/test_operations_ingest_catalog.py` from
   `backend/`.

## References

- [`connector-ingestion.md`](connector-ingestion.md) — the ingest runbook
  this catalog feeds.
- [`docs/codebase/connector-release-readiness.md`](../codebase/connector-release-readiness.md)
  — the three-state (composite / dispatch-catalog / loader-wired) rubric
  the catalog operationalises.
- [Broadcom Developer Portal](https://developer.broadcom.com/xapis) —
  vSphere, NSX, SDDC Manager OpenAPI references.
- [Harbor swagger](https://github.com/goharbor/harbor/blob/main/api/v2.0/swagger.yaml).
